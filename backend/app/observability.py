import json
import logging
import hmac
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Header, HTTPException, Query
import psycopg2
from psycopg2.extras import RealDictCursor
from prometheus_client import Counter, Gauge, Histogram


transaction_id_context = ContextVar("transaction_id", default=None)
request_id_context = ContextVar("request_id", default=None)

PROMETHEUS_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0)

http_request_duration_seconds = Histogram(
    "smartbancs_http_request_duration_seconds",
    "Duración HTTP total de las solicitudes.",
    labelnames=("method", "route", "status_code"),
    buckets=PROMETHEUS_BUCKETS,
)
transaction_business_duration_seconds = Histogram(
    "smartbancs_transaction_business_duration_seconds",
    "Duración del tramo de negocio hasta commit o rollback.",
    labelnames=("outcome",),
    buckets=PROMETHEUS_BUCKETS,
)
db_lock_wait_seconds = Histogram(
    "smartbancs_db_lock_wait_seconds",
    "Tiempo esperando el bloqueo de cuentas en PostgreSQL.",
    buckets=PROMETHEUS_BUCKETS,
)
transactions_in_flight = Gauge(
    "smartbancs_transactions_in_flight",
    "Transacciones actualmente ejecutándose en el handler.",
)
sla_breaches_total = Counter(
    "smartbancs_sla_breaches_total",
    "Transacciones cuyo tiempo de negocio supera 2 segundos.",
)
ai_tasks_total = Counter(
    "smartbancs_ai_tasks_total",
    "Tareas IA terminadas por estado.",
    labelnames=("status",),
)
ai_task_duration_seconds = Histogram(
    "smartbancs_ai_task_duration_seconds",
    "Duración de las tareas IA en segundo plano.",
    buckets=PROMETHEUS_BUCKETS,
)
db_errors_total = Counter(
    "smartbancs_db_errors_total",
    "Errores al obtener conexiones de PostgreSQL.",
)
transactions_total = Counter(
    "smartbancs_transactions_total",
    "Intentos de transacción clasificados por resultado y clase de error.",
    labelnames=("outcome", "error_class"),
)
db_deadlocks_total = Counter(
    "smartbancs_db_deadlocks_total",
    "Deadlocks o locks no disponibles detectados en PostgreSQL.",
)
pg_blocked_sessions = Gauge(
    "smartbancs_pg_blocked_sessions",
    "Sesiones PostgreSQL actualmente bloqueadas.",
)
pg_longest_query_seconds = Gauge(
    "smartbancs_pg_longest_query_seconds",
    "Duración de la consulta PostgreSQL activa más antigua.",
)

observability_router = APIRouter(
    prefix="/api/v1/observability",
    tags=["observability"],
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://smartbancs_user:bancs_pass123@db:5432/smartbancs_db",
)


def record_outcome(outcome: str):
    # Las alertas de disponibilidad deben usar solo error_class="system";
    # los rechazos con error_class="client" no representan una caída del servicio.
    error_class_by_outcome = {
        "SUCCESS": "none",
        "INSUFFICIENT_FUNDS": "client",
        "ACCOUNT_NOT_FOUND": "client",
        "INVALID_REQUEST": "client",
        "SYSTEM_ERROR": "system",
    }
    transactions_total.labels(
        outcome=outcome,
        error_class=error_class_by_outcome[outcome],
    ).inc()


def _diagnostics_enabled():
    return os.getenv("ENABLE_DB_DIAGNOSTICS", "false").lower() == "true"


def _require_admin_token(admin_token):
    expected_token = os.getenv("ADMIN_TOKEN", "")
    if not expected_token or not admin_token or not hmac.compare_digest(admin_token, expected_token):
        raise HTTPException(status_code=401, detail="Token de administración inválido.")


def _open_diagnostic_connection():
    connection = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        options="-c statement_timeout=2000 -c default_transaction_read_only=on",
    )
    connection.set_session(readonly=True, autocommit=True)
    return connection


def _diagnostic_query(query, parameters=None):
    connection = None
    try:
        connection = _open_diagnostic_connection()
        with connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchall()
    finally:
        if connection:
            connection.close()


def _collect_db_activity():
    active_connections = _diagnostic_query(
        """
        SELECT state, COUNT(*) AS count
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
        GROUP BY state
        ORDER BY state;
        """
    )
    blocked_queries = _diagnostic_query(
        """
        SELECT
            a.pid AS blocked_pid,
            pg_blocking_pids(a.pid) AS blocking_pids,
            a.wait_event_type,
            a.wait_event,
            a.state,
            EXTRACT(EPOCH FROM (now() - a.query_start)) AS seconds_waiting,
            LEFT(COALESCE(a.query, ''), 200) AS query
        FROM pg_stat_activity AS a
        WHERE a.datname = current_database()
          AND a.pid <> pg_backend_pid()
          AND cardinality(pg_blocking_pids(a.pid)) > 0
        ORDER BY seconds_waiting DESC;
        """
    )
    long_running_queries = _diagnostic_query(
        """
        SELECT
            a.pid,
            EXTRACT(EPOCH FROM (now() - a.query_start)) AS seconds_running,
            a.state,
            LEFT(COALESCE(a.query, ''), 200) AS query
        FROM pg_stat_activity AS a
        WHERE a.datname = current_database()
          AND a.pid <> pg_backend_pid()
          AND a.state = 'active'
          AND a.query_start IS NOT NULL
          AND now() - a.query_start > INTERVAL '1 second'
        ORDER BY seconds_running DESC;
        """
    )
    locks_summary = _diagnostic_query(
        """
                SELECT l.locktype, l.mode, l.granted, COUNT(*) AS count
                FROM pg_locks AS l
                JOIN pg_database AS d ON d.oid = l.database
                WHERE d.datname = current_database()
                    AND (l.pid IS NULL OR l.pid <> pg_backend_pid())
                GROUP BY l.locktype, l.mode, l.granted
                ORDER BY l.locktype, l.mode, l.granted;
        """
    )
    idle_in_transaction = _diagnostic_query(
        """
        SELECT
            a.pid,
            EXTRACT(EPOCH FROM (now() - a.state_change)) AS seconds_idle,
            LEFT(COALESCE(a.query, ''), 200) AS query
        FROM pg_stat_activity AS a
        WHERE a.datname = current_database()
          AND a.pid <> pg_backend_pid()
          AND a.state = 'idle in transaction'
          AND now() - a.state_change > INTERVAL '5 seconds'
        ORDER BY seconds_idle DESC;
        """
    )
    return {
        "active_connections": [dict(row) for row in active_connections],
        "blocked_queries": [dict(row) for row in blocked_queries],
        "long_running_queries": [dict(row) for row in long_running_queries],
        "locks_summary": [dict(row) for row in locks_summary],
        "idle_in_transaction": [dict(row) for row in idle_in_transaction],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def update_db_diagnostic_gauges():
    try:
        activity = _collect_db_activity()
        pg_blocked_sessions.set(len(activity["blocked_queries"]))
        longest_query = max(
            (float(query["seconds_running"]) for query in activity["long_running_queries"]),
            default=0.0,
        )
        pg_longest_query_seconds.set(longest_query)
    except Exception as error:
        pg_blocked_sessions.set(0)
        pg_longest_query_seconds.set(0)
        logging.getLogger("smartbancs.observability").error(
            "No se pudieron actualizar los gauges de PostgreSQL",
            extra={"event": "db_diagnostics_gauges_failed", "error_type": type(error).__name__},
            exc_info=True,
        )


def _transaction_stats(window_minutes, bucket_minutes=None):
    rows = _diagnostic_query(
        """
        SELECT
            COUNT(*) AS total_transactions,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_ms,
            MAX(latency_ms) AS max_latency_ms,
            COUNT(*) FILTER (WHERE latency_ms > 2000) AS over_2000_ms
        FROM transactions
        WHERE created_at >= now() - (%s * INTERVAL '1 minute');
        """,
        (window_minutes,),
    )
    stats = dict(rows[0])
    for key in ("p50_ms", "p95_ms", "p99_ms", "max_latency_ms"):
        if stats[key] is not None:
            stats[key] = float(stats[key])
    stats["total_transactions"] = int(stats["total_transactions"])
    stats["over_2000_ms"] = int(stats["over_2000_ms"])

    try:
        outcomes = _diagnostic_query(
            """
            SELECT outcome, COUNT(*) AS count
            FROM transaction_attempts
            WHERE created_at >= now() - (%s * INTERVAL '1 minute')
            GROUP BY outcome
            ORDER BY outcome;
            """,
            (window_minutes,),
        )
        stats["outcomes"] = {row["outcome"]: int(row["count"]) for row in outcomes}
    except psycopg2.errors.UndefinedTable:
        stats["outcomes"] = {}
    if bucket_minutes:
        latency_series = _diagnostic_query(
            """
            SELECT
                date_bin((%s * INTERVAL '1 minute'), created_at, TIMESTAMP '2000-01-01') AS bucket,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_ms
            FROM transactions
            WHERE created_at >= now() - (%s * INTERVAL '1 minute')
            GROUP BY bucket
            ORDER BY bucket;
            """,
            (bucket_minutes, window_minutes),
        )
        stats["latency_series"] = [
            {
                "bucket": row["bucket"].isoformat(),
                "p50_ms": float(row["p50_ms"]),
                "p95_ms": float(row["p95_ms"]),
                "p99_ms": float(row["p99_ms"]),
            }
            for row in latency_series
        ]
        slow_transactions = _diagnostic_query(
            """
            SELECT transaction_id, amount, latency_ms, created_at
            FROM transactions
            WHERE created_at >= now() - (%s * INTERVAL '1 minute')
            ORDER BY latency_ms DESC NULLS LAST
            LIMIT 10;
            """,
            (window_minutes,),
        )
        stats["slow_transactions"] = [
            {
                "transaction_id": row["transaction_id"],
                "amount": float(row["amount"]),
                "latency_ms": float(row["latency_ms"]) if row["latency_ms"] is not None else None,
                "created_at": row["created_at"].isoformat(),
            }
            for row in slow_transactions
        ]
        try:
            attempts_timeline = _diagnostic_query(
                """
                SELECT
                    date_bin((%s * INTERVAL '1 minute'), created_at, TIMESTAMP '2000-01-01') AS bucket,
                    outcome,
                    COUNT(*) AS count
                FROM transaction_attempts
                WHERE created_at >= now() - (%s * INTERVAL '1 minute')
                GROUP BY bucket, outcome
                ORDER BY bucket, outcome;
                """,
                (bucket_minutes, window_minutes),
            )
            stats["attempts_timeline"] = [
                {
                    "bucket": row["bucket"].isoformat(),
                    "outcome": row["outcome"],
                    "count": int(row["count"]),
                }
                for row in attempts_timeline
            ]
        except psycopg2.errors.UndefinedTable:
            stats["attempts_timeline"] = []
    return stats


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _authorize_observability(admin_token):
    if not _diagnostics_enabled():
        raise HTTPException(status_code=404, detail="Diagnóstico no habilitado.")
    _require_admin_token(admin_token)


@observability_router.get("/db-activity")
def db_activity(x_admin_token: str | None = Header(default=None)):
    _authorize_observability(x_admin_token)
    try:
        return _collect_db_activity()
    except Exception as error:
        logging.getLogger("smartbancs.observability").error(
            "No se pudo consultar la actividad de PostgreSQL",
            extra={"event": "db_diagnostics_query_failed", "error_type": type(error).__name__},
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="No se pudo consultar la actividad de la base de datos.")


@observability_router.get("/summary")
def observability_summary(
    window_minutes: int = Query(default=60, ge=1),
    bucket_minutes: int = Query(default=1, ge=1),
    x_admin_token: str | None = Header(default=None),
):
    _authorize_observability(x_admin_token)
    try:
        selected_window = _transaction_stats(window_minutes, bucket_minutes)
        return {
            "window_minutes": window_minutes,
            "bucket_minutes": bucket_minutes,
            "totals": selected_window,
            "last_1h": _transaction_stats(60),
            "last_24h": _transaction_stats(1440),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as error:
        logging.getLogger("smartbancs.observability").error(
            "No se pudo consultar el resumen transaccional",
            extra={"event": "transaction_summary_query_failed", "error_type": type(error).__name__},
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="No se pudo consultar el resumen transaccional.")


@observability_router.get("/transactions/{transaction_id}")
def transaction_trace(transaction_id: str, x_admin_token: str | None = Header(default=None)):
    _authorize_observability(x_admin_token)
    try:
        transactions = _diagnostic_query(
            """
            SELECT transaction_id, source_account, destination_account, amount,
                   status, latency_ms, created_at
            FROM transactions
            WHERE transaction_id = %s;
            """,
            (transaction_id,),
        )
        if not transactions:
            raise HTTPException(status_code=404, detail="Transacción no encontrada.")
        attempts = _diagnostic_query(
            """
            SELECT id, transaction_id, outcome, error_class, latency_ms, created_at
            FROM transaction_attempts
            WHERE transaction_id = %s
            ORDER BY created_at;
            """,
            (transaction_id,),
        )
        recommendations = _diagnostic_query(
            """
            SELECT id, transaction_id, account_number, recommendation_text, created_at
            FROM ai_recommendations
            WHERE transaction_id = %s
            ORDER BY created_at;
            """,
            (transaction_id,),
        )
        return _json_safe({
            "transaction": dict(transactions[0]),
            "attempts": [dict(row) for row in attempts],
            "ai_recommendations": [dict(row) for row in recommendations],
        })
    except HTTPException:
        raise
    except Exception as error:
        logging.getLogger("smartbancs.observability").error(
            "No se pudo consultar la trazabilidad de la transacción",
            extra={"event": "transaction_trace_query_failed", "error_type": type(error).__name__},
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="No se pudo consultar la trazabilidad.")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "application_log"),
            "message": record.getMessage(),
            "service": "smartbancs-api",
            "transaction_id": transaction_id_context.get(),
            "request_id": request_id_context.get(),
            "source_account": getattr(record, "source_account", None),
            "destination_account": getattr(record, "destination_account", None),
            "amount": getattr(record, "amount", None),
            "latency_ms": getattr(record, "latency_ms", None),
            "reason": getattr(record, "reason", None),
            "error_type": getattr(record, "error_type", None),
            "exc_info": self.formatException(record.exc_info) if record.exc_info else None,
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)