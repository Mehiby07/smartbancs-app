"""Prueba de carga concurrente para las transferencias del backend.

Uso:
	python load_test.py --threads 100 --total 500

Requisitos:
	pip install requests psycopg2-binary

El script toma DATABASE_URL del entorno para consultar PostgreSQL. Si no se
define, usa la publicación local del contenedor configurada en docker-compose.
"""

import argparse
import logging
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from math import ceil

import psycopg2
import requests


API_URL = os.getenv("LOAD_TEST_URL", "http://localhost:8000/api/v1/transactions")
DATABASE_URL = os.getenv(
	"DATABASE_URL",
	"postgresql://smartbancs_user:bancs_pass123@localhost:5432/smartbancs_db",
)
SOURCE_ACCOUNT = "CTA-1001"
DESTINATION_ACCOUNT = "CTA-2002"
TRANSFER_AMOUNT = 10.00
REQUEST_TIMEOUT_SECONDS = 30

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("SmartBancs-load-test")


def parse_args():
	parser = argparse.ArgumentParser(description="Prueba de carga de transferencias concurrentes.")
	parser.add_argument(
		"--threads",
		type=int,
		default=50,
		help="Número máximo de hilos concurrentes (default: 50).",
	)
	parser.add_argument(
		"--total",
		type=int,
		default=300,
		help="Número total de transferencias a disparar (default: 300).",
	)
	args = parser.parse_args()
	if args.threads < 1 or args.total < 1:
		parser.error("--threads y --total deben ser mayores que cero.")
	return args


def get_accounts_balance_sum():
	connection = psycopg2.connect(DATABASE_URL)
	try:
		with connection.cursor() as cursor:
			cursor.execute(
				"""
				SELECT COALESCE(SUM(balance), 0)
				FROM accounts
				WHERE account_number IN (%s, %s);
				""",
				(SOURCE_ACCOUNT, DESTINATION_ACCOUNT),
			)
			return cursor.fetchone()[0]
	finally:
		connection.close()


def percentile(values, percentile_value):
	ordered_values = sorted(values)
	index = max(0, ceil(percentile_value * len(ordered_values)) - 1)
	return ordered_values[index]


def run_transfer(request_number):
	if request_number % 2 == 0:
		source_account, destination_account = SOURCE_ACCOUNT, DESTINATION_ACCOUNT
	else:
		source_account, destination_account = DESTINATION_ACCOUNT, SOURCE_ACCOUNT

	payload = {
		"source_account": source_account,
		"destination_account": destination_account,
		"amount": TRANSFER_AMOUNT,
	}
	started_at = time.perf_counter()
	status_code = None
	result = "error"
	error_message = None

	try:
		response = requests.post(
			API_URL,
			json=payload,
			timeout=REQUEST_TIMEOUT_SECONDS,
		)
		status_code = response.status_code
		response_detail = ""
		try:
			response_detail = str(response.json().get("detail", "")).lower()
		except (ValueError, AttributeError):
			pass

		if 200 <= status_code < 300:
			result = "success"
		elif status_code == 400 and "fondos insuficientes" in response_detail:
			result = "insufficient_funds"
		else:
			response_body = response.text.strip()
			error_message = f"HTTP {status_code}: {response_body or 'respuesta vacía'}"
	except requests.RequestException as error:
		error_message = f"{type(error).__name__}: {error}"
		logger.error("request=%d result=error detail=%s", request_number, error_message)

	latency_ms = (time.perf_counter() - started_at) * 1000
	logger.info(
		"request=%d status=%s latency_ms=%.2f result=%s",
		request_number,
		status_code if status_code is not None else "N/A",
		latency_ms,
		result,
	)
	return result, latency_ms, error_message


def print_summary(results, initial_balance_sum, final_balance_sum, duration_seconds):
	latencies = [latency_ms for _, latency_ms, _ in results]
	successful = sum(result == "success" for result, _, _ in results)
	insufficient_funds = sum(result == "insufficient_funds" for result, _, _ in results)
	server_errors = sum(result == "error" for result, _, _ in results)
	error_counts = Counter(
		error_message
		for result, _, error_message in results
		if result == "error" and error_message
	)

	print("\n[LOAD TEST] Resumen")
	print(f"  Total ejecutadas: {len(results)}")
	print(f"  Exitosas: {successful}")
	print(f"  Fondos insuficientes: {insufficient_funds}")
	print(f"  Errores de servidor/red: {server_errors}")
	print(f"  Latencia promedio: {sum(latencies) / len(latencies):.2f} ms")
	print(f"  Latencia p95: {percentile(latencies, 0.95):.2f} ms")
	print(f"  Latencia p99: {percentile(latencies, 0.99):.2f} ms")
	print(f"  Duración total del test: {duration_seconds:.2f} s")
	print("  Errores más frecuentes:")
	if error_counts:
		for error_message, count in error_counts.most_common(5):
			print(f"    - {error_message}: {count}")
	else:
		print("    - Ninguno")
	print(f"  Balance inicial ({SOURCE_ACCOUNT} + {DESTINATION_ACCOUNT}): {initial_balance_sum}")
	print(f"  Balance final ({SOURCE_ACCOUNT} + {DESTINATION_ACCOUNT}): {final_balance_sum}")
	print(f"  Invariante de dinero: {'OK' if initial_balance_sum == final_balance_sum else 'FALLO'}")


def main():
	args = parse_args()

	try:
		initial_balance_sum = get_accounts_balance_sum()
	except psycopg2.Error as error:
		print(f"No se pudo consultar el balance inicial: {error}", file=sys.stderr)
		return 1

	test_started_at = time.perf_counter()
	with ThreadPoolExecutor(max_workers=args.threads) as executor:
		results = list(executor.map(run_transfer, range(args.total)))
	duration_seconds = time.perf_counter() - test_started_at

	try:
		final_balance_sum = get_accounts_balance_sum()
	except psycopg2.Error as error:
		print(f"No se pudo consultar el balance final: {error}", file=sys.stderr)
		return 1

	print_summary(results, initial_balance_sum, final_balance_sum, duration_seconds)
	return 0 if initial_balance_sum == final_balance_sum else 1


if __name__ == "__main__":
	raise SystemExit(main())
