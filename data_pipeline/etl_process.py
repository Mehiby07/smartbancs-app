import csv
import json
from datetime import datetime

INPUT_FILE = "raw_transactions.csv"
OUTPUT_FILE = "clean_transactions_for_ai.json"

def clean_and_transform():
    clean_records = []
    print("[ETL] Iniciando procesamiento por lotes...")

    with open(INPUT_FILE, mode="r", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)
        for row in reader:
            # 1. Manejo de valores nulos
            if not row["account_id"] or not row["amount"]:
                print(f"[ETL-SKIP] Fila ignorada por datos faltantes: {row}")
                continue

            try:
                amount = float(row["amount"])
            except ValueError:
                continue

            # 2. Estandarización de moneda
            currency = (row.get("currency") or "USD").upper().strip()

            # 3. Estandarización de formatos de fecha a ISO 8601
            raw_date = row["tx_date"].strip()
            standard_date = None
            for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    standard_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass

            if not standard_date:
                standard_date = datetime.utcnow().strftime("%Y-%m-%d")

            # 4. Formato estructurado para el consumo de IA
            clean_records.append({
                "transaction_id": row["tx_id"],
                "account_number": row["account_id"].strip(),
                "amount": amount,
                "currency": currency,
                "date": standard_date,
                "ai_feature_vector": [amount, 1 if currency == "USD" else 0]
            })

    with open(OUTPUT_FILE, mode="w", encoding="utf-8") as outfile:
        json.dump(clean_records, outfile, indent=2)

    print(f"[ETL] Limpieza completada. {len(clean_records)} registros exportados a '{OUTPUT_FILE}'.")

if __name__ == "__main__":
    clean_and_transform()