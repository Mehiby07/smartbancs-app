import time
import uuid
import logging
from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from pydantic import BaseModel, Field
from app.database import get_db_connection

# Observabilidad y Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
logger = logging.getLogger("SmartBancs-API")

app = FastAPI(
    title="SmartBancs API",
    description="Microservicio transaccional de alta concurrencia con IA desacoplada",
    version="1.0.0"
)

# Esquema de datos de entrada
class TransactionRequest(BaseModel):
    source_account: str = Field(..., example="CTA-1001")
    destination_account: str = Field(..., example="CTA-2002")
    amount: float = Field(..., gt=0, example=150.00)

# Mock de IA no bloqueante (se ejecuta en segundo plano)
def process_ai_financial_recommendation(transaction_id: str, account_number: str, amount: float):
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
        logger.info(f"[TX: {transaction_id}] [IA-ENGINE] Recomendación registrada en segundo plano.")
    except Exception as e:
        logger.error(f"[TX: {transaction_id}] [IA-ERROR] Error en motor de IA: {str(e)}")
    finally:
        if conn:
            conn.close()

# Endpoint para procesar transferencias
@app.post("/api/v1/transactions", status_code=status.HTTP_201_CREATED)
def create_transaction(payload: TransactionRequest, background_tasks: BackgroundTasks):
    start_time = time.time()
    tx_id = str(uuid.uuid4())

    logger.info(f"[TX: {tx_id}] Recibida solicitud: {payload.source_account} -> {payload.destination_account} por ${payload.amount}")

    if payload.source_account == payload.destination_account:
        raise HTTPException(status_code=400, detail="La cuenta de origen y destino no pueden ser iguales.")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Prevención de deadlocks: bloquear cuentas en orden alfabético
            accounts_to_lock = sorted([payload.source_account, payload.destination_account])
            cur.execute(
                "SELECT account_number, balance FROM accounts WHERE account_number IN (%s, %s) FOR UPDATE;",
                (accounts_to_lock[0], accounts_to_lock[1])
            )
            locked_accounts = {row["account_number"]: row["balance"] for row in cur.fetchall()}

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

            # Registro de auditoría
            latency_ms = round((time.time() - start_time) * 1000, 2)
            cur.execute(
                """
                INSERT INTO transactions (transaction_id, source_account, destination_account, amount, status, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (tx_id, payload.source_account, payload.destination_account, payload.amount, "COMPLETED", latency_ms)
            )

        conn.commit()

        # Enviar recomendación a segundo plano (asíncrono)
        background_tasks.add_task(
            process_ai_financial_recommendation, 
            tx_id, 
            payload.source_account, 
            payload.amount
        )

        total_latency = round((time.time() - start_time) * 1000, 2)
        logger.info(f"[TX: {tx_id}] Transacción COMPLETADA en {total_latency} ms.")

        return {
            "transaction_id": tx_id,
            "status": "COMPLETED",
            "amount": payload.amount,
            "source_account": payload.source_account,
            "destination_account": payload.destination_account,
            "latency_ms": total_latency,
            "ai_status": "PROCESSING_ASYNCHRONOUSLY"
        }

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as ex:
        if conn:
            conn.rollback()
        logger.error(f"[TX: {tx_id}] Error: {str(ex)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor.")
    finally:
        if conn:
            conn.close()

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "SmartBancs API"}