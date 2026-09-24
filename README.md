# SmartBancs API

Microservicio transaccional de alta concurrencia con PostgreSQL, procesamiento IA asíncrono y observabilidad operativa.

## Stack tecnológico

- **Backend:** Python 3.11, FastAPI y Uvicorn.
- **Base de datos:** PostgreSQL 15 sobre Docker, con `SELECT ... FOR UPDATE` y pool `ThreadedConnectionPool`.
- **Observabilidad:** logs JSON, métricas Prometheus y consultas de diagnóstico PostgreSQL.
- **Dashboard:** Streamlit, pandas, Plotly y datos persistentes del backend.
- **ETL:** limpieza de transacciones en `data_pipeline/`.

## Ejecución local

Prerrequisito: Docker Desktop activo. Para el dashboard se necesita Python con sus dependencias locales.

1. Levanta PostgreSQL y la API:

   ```bash
   docker compose up --build
   ```

2. Instala las dependencias del dashboard en otra terminal:

   ```bash
   pip install -r dashboard_requirements.txt
   ```

3. Inicia el dashboard:

   ```bash
   python -m streamlit run dashboard.py
   ```

4. Abre las interfaces:

   - API y OpenAPI: <http://localhost:8000/docs>
   - Dashboard Streamlit: <http://localhost:8501>
   - Endpoint de scrape Prometheus: <http://localhost:8000/metrics>

El repositorio no incluye un servicio Prometheus ni una UI en `localhost:9090`; un servidor Prometheus externo debe configurarse para scrapear `/metrics`.

## Observabilidad

### Implementado

| Capacidad | Ubicación real |
| --- | --- |
| Logs JSON correlacionables por `transaction_id` y `request_id` | `backend/app/observability.py`, `backend/app/main.py` y `backend/app/database.py` |
| Métricas Prometheus en `/metrics` | `backend/app/main.py` y `backend/app/observability.py` |
| Conteo por resultado (`SUCCESS`, errores de cliente y sistema) | `backend/app/observability.py`, `backend/app/main.py` y `db/init.sql` |
| Diagnóstico de PostgreSQL: actividad, bloqueos, locks e idle transactions | `backend/app/observability.py` |
| Resumen persistente, intentos y trazabilidad por transacción | `backend/app/observability.py`, `backend/app/database.py` y `db/init.sql` |
| Dashboard persistente con auto-refresco y carga real | `dashboard.py` y `dashboard_requirements.txt` |
| Pool de conexiones PostgreSQL | `backend/app/database.py` |
| Métricas SLA y umbrales operativos del panel | `backend/app/observability.py` y `dashboard.py` |

El endpoint de diagnóstico requiere `ENABLE_DB_DIAGNOSTICS=true` y el header `X-Admin-Token`. Los valores de Docker son de demostración.


## Verificación reproducible

Comprobar health y métricas:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/metrics
```

Filtrar logs JSON por una transacción concreta. `fromjson?` ignora las líneas no JSON de Uvicorn:

```bash
docker compose logs --no-log-prefix backend 2>&1 \
  | jq -R 'fromjson? | select(.transaction_id == "<TRANSACTION_ID>")'
```

Consultar el resumen y el diagnóstico PostgreSQL usando el token demo configurado en `docker-compose.yml`:

```bash
curl -H "X-Admin-Token: demo-admin-token" \
  "http://localhost:8000/api/v1/observability/summary?window_minutes=60&bucket_minutes=1"

curl -H "X-Admin-Token: demo-admin-token" \
  http://localhost:8000/api/v1/observability/db-activity
```

URLs disponibles:

- API: <http://localhost:8000/docs>
- Métricas para Prometheus: <http://localhost:8000/metrics>
- Dashboard: <http://localhost:8501>
- UI de Prometheus: no se levanta en este `docker-compose`; debe ser externa.

Generar carga real alternando `CTA-1001` y `CTA-2002`:

```bash
python load_test.py --threads 30 --total 300
```

## Límites actuales

El dashboard ya no usa datos de sesión local para construir métricas: cada pestaña lee el backend, PostgreSQL o el exposition format de Prometheus, por lo que dos navegadores muestran los mismos datos persistentes. El resumen se calcula desde PostgreSQL y las métricas Prometheus viven en memoria del proceso; esto no sustituye un sistema de series temporales con retención a largo plazo.

## Declaración de uso de IA

Se utilizó GitHub Copilot para apoyar la instrumentación de observabilidad del backend y el rediseño del dashboard Streamlit.

## Arquitectura transaccional

Las transferencias bloquean las cuentas en orden alfabético con `SELECT ... FOR UPDATE`, usan `commit`/`rollback` y registran los intentos en `transaction_attempts`. La recomendación IA se procesa con `BackgroundTasks` después del commit de la transferencia.