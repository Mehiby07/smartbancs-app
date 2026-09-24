import os
import logging
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from app.observability import db_errors_total

logger = logging.getLogger("smartbancs.database")

DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://smartbancs_user:bancs_pass123@db:5432/smartbancs_db"
)

connection_pool = ThreadedConnectionPool(
    minconn=2,
    maxconn=20,
    dsn=DATABASE_URL,
    cursor_factory=RealDictCursor,
)

def get_db_connection():
    try:
        connection = connection_pool.getconn()
        logger.info(
            "Conexión obtenida del pool",
            extra={"event": "db_connection_opened"},
        )
        return connection
    except Exception as error:
        db_errors_total.inc()
        logger.error(
            "No se pudo obtener una conexión del pool",
            extra={"event": "db_connection_error", "error_type": type(error).__name__},
            exc_info=True,
        )
        raise

def release_db_connection(conn):
    connection_pool.putconn(conn)


def record_transaction_attempt(transaction_id, outcome, error_class, latency_ms):
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO transaction_attempts
                    (transaction_id, outcome, error_class, latency_ms)
                VALUES (%s, %s, %s, %s);
                """,
                (transaction_id, outcome, error_class, latency_ms),
            )
        connection.commit()
    except Exception as error:
        if connection:
            connection.rollback()
        logger.error(
            "No se pudo registrar el intento de transacción",
            extra={"event": "transaction_attempt_persist_failed", "error_type": type(error).__name__},
            exc_info=True,
        )
    finally:
        if connection:
            release_db_connection(connection)