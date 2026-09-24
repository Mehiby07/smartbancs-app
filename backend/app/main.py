import time
import uuid
import logging
import psycopg2
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from app.observability import (
    ai_task_duration_seconds,
    ai_tasks_total,
    configure_logging,
    db_lock_wait_seconds,
    db_deadlocks_total,
    http_request_duration_seconds,
    observability_router,
    record_outcome,
    request_id_context,
    sla_breaches_total,
    transaction_business_duration_seconds,
    transaction_id_context,
    transactions_in_flight,
    update_db_diagnostic_gauges,
)
from app.database import get_db_connection, record_transaction_attempt, release_db_connection

configure_logging()
logger = logging.getLogger("smartbancs.api")

app = FastAPI(
    title="SmartBancs API",
    description="Microservicio transaccional de alta concurrencia con IA desacoplada",
    version="1.0.0"
)
app.include_router(observability_router)


@app.middleware("http")
async def observe_http_requests(request: Request, call_next):
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        if request.url.path != "/metrics":
            route = request.scope.get("route")
            route_template = getattr(route, "path", request.url.path)
            http_request_duration_seconds.labels(
                method=request.method,
                route=route_template,
                status_code=str(status_code),
            ).observe(time.perf_counter() - started_at)


@app.get("/metrics", include_in_schema=False)
def metrics():
    update_db_diagnostic_gauges()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    started_at = time.perf_counter()
    transaction_id = str(uuid.uuid4())
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    transaction_id_context.set(transaction_id)
    request_id_context.set(request_id)
    latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
    record_outcome("INVALID_REQUEST")
    record_transaction_attempt(transaction_id, "INVALID_REQUEST", "client", latency_ms)
    logger.warning(
        "Solicitud inválida",
        extra={
            "event": "tx_rejected",
            "reason": "INVALID_REQUEST",
            "latency_ms": latency_ms,
        },
    )
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
        headers={"X-Request-ID": request_id},
    )

# Esquema de datos de entrada
class TransactionRequest(BaseModel):
    source_account: str = Field(..., example="CTA-1001")
    destination_account: str = Field(..., example="CTA-2002")
    amount: float = Field(..., gt=0, example=150.00)

# Mock de IA no bloqueante (se ejecuta en segundo plano)
def process_ai_financial_recommendation(
    transaction_id: str,
    request_id: str,
    account_number: str,
    destination_account: str,
    amount: float,
):
    ai_started_at = time.perf_counter()
    ai_status = "error"
    transaction_id_context.set(transaction_id)
    request_id_context.set(request_id)
    logger.info(
        "Inicia procesamiento de recomendación IA",
        extra={
            "event": "ai_task_started",
            "source_account": account_number,
            "destination_account": destination_account,
            "amount": amount,
        },
    )
    time.sleep(1.5)  # Simula tiempo de procesamiento de la IA
    advice = f"Recomendación para {account_number}: Transferencia de ${amount:.2f} procesada. Saldo proyectado en rango operativo normal."
    
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_recommendations (transaction_id, account_number, recommendation_text)
                VALUES (%s, %s, %s);
                """,
                (transaction_id, account_number, advice)
            )
        conn.commit()
        ai_status = "ok"
        logger.info(
            "Recomendación IA registrada",
            extra={
                "event": "ai_task_completed",
                "source_account": account_number,
                "destination_account": destination_account,
                "amount": amount,
            },
        )
    except Exception as error:
        logger.error(
            "Error en procesamiento de recomendación IA",
            extra={
                "event": "ai_task_failed",
                "source_account": account_number,
                "destination_account": destination_account,
                "amount": amount,
                "error_type": type(error).__name__,
            },
            exc_info=True,
        )
    finally:
        ai_task_duration_seconds.observe(time.perf_counter() - ai_started_at)
        ai_tasks_total.labels(status=ai_status).inc()
        if conn:
            release_db_connection(conn)

# Endpoint para procesar transferencias
@app.post("/api/v1/transactions", status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    x_request_id: str | None = Header(default=None),
):
    start_time = time.time()
    business_started_at = time.perf_counter()
    business_outcome = "error"
    business_duration_recorded = False
    transactions_in_flight.inc()

    def record_business_duration(outcome):
        nonlocal business_duration_recorded
        if business_duration_recorded:
            return
        business_duration = time.perf_counter() - business_started_at
        transaction_business_duration_seconds.labels(outcome=outcome).observe(business_duration)
        if business_duration > 2.0:
            sla_breaches_total.inc()
        business_duration_recorded = True

    tx_id = str(uuid.uuid4())
    request_id = x_request_id or str(uuid.uuid4())
    transaction_id_context.set(tx_id)
    request_id_context.set(request_id)
    response.headers["X-Request-ID"] = request_id

    logger.info(
        "Solicitud de transferencia recibida",
        extra={
            "event": "tx_received",
            "source_account": payload.source_account,
            "destination_account": payload.destination_account,
            "amount": payload.amount,
        },
    )

    conn = None

    def release_transaction_connection():
        nonlocal conn
        if conn:
            release_db_connection(conn)
            conn = None

    try:
        if payload.source_account == payload.destination_account:
            raise HTTPException(status_code=400, detail="La cuenta de origen y destino no pueden ser iguales.")

        conn = get_db_connection()
        with conn.cursor() as cur:
            # Prevención de deadlocks: bloquear cuentas en orden alfabético
            accounts_to_lock = sorted([payload.source_account, payload.destination_account])
            lock_started_at = time.perf_counter()
            try:
                cur.execute(
                    "SELECT account_number, balance FROM accounts WHERE account_number IN (%s, %s) FOR UPDATE;",
                    (accounts_to_lock[0], accounts_to_lock[1])
                )
                locked_accounts = {row["account_number"]: row["balance"] for row in cur.fetchall()}
            finally:
                db_lock_wait_seconds.observe(time.perf_counter() - lock_started_at)
            logger.info(
                "Cuentas bloqueadas en orden",
                extra={
                    "event": "tx_locked_accounts",
                    "source_account": payload.source_account,
                    "destination_account": payload.destination_account,
                    "amount": payload.amount,
                    "latency_ms": round((time.time() - start_time) * 1000, 2),
                },
            )

            if payload.source_account not in locked_accounts:
                raise HTTPException(status_code=404, detail="Cuenta de origen no encontrada.")
            if payload.destination_account not in locked_accounts:
                raise HTTPException(status_code=404, detail="Cuenta de destino no encontrada.")

            current_balance = locked_accounts[payload.source_account]
            if current_balance < payload.amount:
                raise HTTPException(status_code=400, detail="Fondos insuficientes.")

            # Actualización atómica de saldos
            cur.execute(
                "UPDATE accounts SET balance = balance - %s, updated_at = CURRENT_TIMESTAMP WHERE account_number = %s;",
                (payload.amount, payload.source_account)
            )
            cur.execute(
                "UPDATE accounts SET balance = balance + %s, updated_at = CURRENT_TIMESTAMP WHERE account_number = %s;",
                (payload.amount, payload.destination_account)
            )

            # transactions.latency_ms se mide antes del commit; la métrica de negocio
            # usa el mismo inicio, pero termina al completar commit o rollback.
            latency_ms = round((time.time() - start_time) * 1000, 2)
            cur.execute(
                """
                INSERT INTO transactions (transaction_id, source_account, destination_account, amount, status, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (tx_id, payload.source_account, payload.destination_account, payload.amount, "COMPLETED", latency_ms)
            )

        conn.commit()
        release_transaction_connection()
        business_outcome = "success"
        record_business_duration(business_outcome)
        record_outcome("SUCCESS")
        record_transaction_attempt(
            tx_id,
            "SUCCESS",
            "none",
            round((time.time() - start_time) * 1000, 2),
        )
        logger.info(
            "Transacción confirmada",
            extra={
                "event": "tx_committed",
                "source_account": payload.source_account,
                "destination_account": payload.destination_account,
                "amount": payload.amount,
                "latency_ms": round((time.time() - start_time) * 1000, 2),
            },
        )

        # Enviar recomendación a segundo plano (asíncrono)
        background_tasks.add_task(
            process_ai_financial_recommendation, 
            tx_id, 
            request_id,
            payload.source_account, 
            payload.destination_account,
            payload.amount
        )

        total_latency = round((time.time() - start_time) * 1000, 2)

        return {
            "transaction_id": tx_id,
            "status": "COMPLETED",
            "amount": payload.amount,
            "source_account": payload.source_account,
            "destination_account": payload.destination_account,
            "latency_ms": total_latency,
            "ai_status": "PROCESSING_ASYNCHRONOUSLY",
            "ai_recommendation": "Sugerencia: Oportunidad de ahorro detectada basada en el flujo de esta transferencia."
        }

    except HTTPException as error:
        if conn:
            conn.rollback()
            release_transaction_connection()
        reason = "SAME_ACCOUNT"
        if "no encontrada" in error.detail:
            reason = "ACCOUNT_NOT_FOUND"
        elif "Fondos insuficientes" in error.detail:
            reason = "INSUFFICIENT_FUNDS"
        outcome = "INVALID_REQUEST" if reason == "SAME_ACCOUNT" else reason
        business_outcome = outcome.lower()
        record_business_duration(business_outcome)
        record_outcome(outcome)
        record_transaction_attempt(
            tx_id,
            outcome,
            "client",
            round((time.time() - start_time) * 1000, 2),
        )
        logger.warning(
            "Transacción rechazada",
            extra={
                "event": "tx_rejected",
                "reason": reason,
                "source_account": payload.source_account,
                "destination_account": payload.destination_account,
                "amount": payload.amount,
                "latency_ms": round((time.time() - start_time) * 1000, 2),
            },
        )
        raise
    except (psycopg2.errors.DeadlockDetected, psycopg2.errors.LockNotAvailable) as error:
        if conn:
            conn.rollback()
            release_transaction_connection()
        record_business_duration("system_error")
        record_outcome("SYSTEM_ERROR")
        db_deadlocks_total.inc()
        latency_ms = round((time.time() - start_time) * 1000, 2)
        record_transaction_attempt(tx_id, "SYSTEM_ERROR", "system", latency_ms)
        logger.error(
            "Contención de base de datos no resuelta",
            extra={
                "event": "tx_failed",
                "reason": "deadlock",
                "source_account": payload.source_account,
                "destination_account": payload.destination_account,
                "amount": payload.amount,
                "latency_ms": latency_ms,
                "error_type": type(error).__name__,
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail="La transferencia no pudo completarse por contención temporal de la base de datos.",
        )
    except Exception as ex:
        if conn:
            conn.rollback()
            release_transaction_connection()
        record_business_duration(business_outcome)
        record_outcome("SYSTEM_ERROR")
        record_transaction_attempt(
            tx_id,
            "SYSTEM_ERROR",
            "system",
            round((time.time() - start_time) * 1000, 2),
        )
        logger.error(
            "Error no controlado en transferencia",
            extra={
                "event": "tx_failed",
                "source_account": payload.source_account,
                "destination_account": payload.destination_account,
                "amount": payload.amount,
                "latency_ms": round((time.time() - start_time) * 1000, 2),
                "error_type": type(ex).__name__,
            },
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Error interno del servidor.")
    finally:
        if not business_duration_recorded:
            record_business_duration(business_outcome)
        transactions_in_flight.dec()
        if conn:
            release_db_connection(conn)

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "SmartBancs API"}