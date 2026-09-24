import csv
import json
from datetime import datetime

INPUT_FILE = "raw_transactions.csv"
OUTPUT_FILE = "clean_transactions_for_ai.json"

def clean_and_transform():
    clean_records = []
    rows_read = 0
    discarded_rows = 0
    missing_account_rows = 0
    missing_amount_rows = 0
    invalid_amount_rows = 0
    invalid_date_rows = 0
    print("[ETL] Iniciando procesamiento por lotes...")

    with open(INPUT_FILE, mode="r", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)
        for row in reader:
            rows_read += 1
            # 1. Manejo de valores nulos
            missing_reasons = []
            if not row["account_id"]:
                missing_account_rows += 1
                missing_reasons.append("account_id faltante")
            if not row["amount"]:
                missing_amount_rows += 1
                missing_reasons.append("amount faltante")
            if missing_reasons:
                discarded_rows += 1
                print(f"[ETL-SKIP] Fila ignorada: {', '.join(missing_reasons)}: {row}")
                continue

            try:
                amount = float(row["amount"])
            except ValueError:
                discarded_rows += 1
                invalid_amount_rows += 1
                continue

            # 2. Estandarización de moneda
            currency = (row.get("currency") or "USD").upper().strip()

            # 3. Estandarización de formatos de fecha a ISO 8601
            raw_date_value = row.get("tx_date", "")
            raw_date = raw_date_value.strip()
            standard_date = None
            for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    standard_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass

            date_valid = standard_date is not None
            if not date_valid:
                invalid_date_rows += 1

            # 4. Formato estructurado para el consumo de IA
            clean_records.append({
                "transaction_id": row["tx_id"],
                "account_number": row["account_id"].strip(),
                "amount": amount,
                "currency": currency,
                "date": standard_date,
                "date_valid": date_valid,
                "raw_date_value": raw_date_value,
                "ai_feature_vector": [amount, 1 if currency == "USD" else 0]
            })

    with open(OUTPUT_FILE, mode="w", encoding="utf-8") as outfile:
        json.dump(clean_records, outfile, indent=2)

    print("[ETL] Resumen:")
    print(f"  Filas leídas: {rows_read}")
    print(f"  Filas válidas y limpias: {len(clean_records)}")
    print(f"  Filas descartadas: {discarded_rows}")
    print(f"    - account_id faltante: {missing_account_rows}")
    print(f"    - amount faltante: {missing_amount_rows}")
    print(f"    - amount no numérico: {invalid_amount_rows}")
    print(f"  Filas con fecha inválida o vacía conservadas: {invalid_date_rows}")
    print(f"[ETL] Datos exportados a '{OUTPUT_FILE}'.")

if __name__ == "__main__":
    clean_and_transform()