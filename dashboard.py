"""Panel Streamlit de observabilidad para SmartBancs."""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from prometheus_client.parser import text_string_to_metric_families


st.set_page_config(page_title="SmartBancs | Observabilidad", page_icon="S", layout="wide")
DEFAULT_API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
DEFAULT_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = 3
REFRESH_OPTIONS = {"5 segundos": 5, "10 segundos": 10, "30 segundos": 30, "Desactivado": None}
ERROR_COLORS = {"none": "#3ddc97", "client": "#f0b35b", "system": "#f06a6a"}


def inject_styles() -> None:
    """Aplica el estilo oscuro de consola operativa."""
    st.markdown(
        """
        <style>
        :root { color-scheme: dark; }
        .stApp { background: #0b1118; }
        [data-testid="stHeader"] { background: #0b1118; }
        .ops-header { position: sticky; top: 0; z-index: 10; padding: 1rem 0 .75rem;
          background: rgba(11,17,24,.96); border-bottom: 1px solid #263646; }
        .ops-title { color: #e9f0f6; font-size: 1.55rem; font-weight: 700; }
        .ops-subtitle { color: #8fa1b3; font-size: .82rem; }
        .status-pill { display: inline-block; padding: .25rem .6rem; border-radius: 999px;
          font-size: .78rem; font-weight: 700; }
        .status-ok { color: #061b12; background: #3ddc97; }
        .status-bad { color: #2a0b0b; background: #f06a6a; }
        div[data-testid="stMetric"] { background: #121c26; border: 1px solid #263646;
          border-radius: 8px; padding: .8rem; }
        .alert-line { border-left: 4px solid #f0b35b; background: #171d24;
          padding: .65rem .8rem; margin: .35rem 0; color: #dce6ef; }
        .alert-critical { border-left-color: #f06a6a; }
        .alert-ok { border-left-color: #3ddc97; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def headers(admin_token: str) -> dict[str, str]:
    """Construye el header de administración si existe."""
    return {"X-Admin-Token": admin_token} if admin_token else {}


@st.cache_data(ttl=5, show_spinner=False)
def fetch_health(api_base_url: str) -> dict[str, Any]:
    """Consulta el estado de la API."""
    try:
        response = requests.get(f"{api_base_url}/health", timeout=REQUEST_TIMEOUT_SECONDS)
        return {"ok": response.ok, "status": response.status_code, "data": response.json()}
    except (requests.RequestException, ValueError) as error:
        return {"ok": False, "status": None, "error": str(error)}


@st.cache_data(ttl=5, show_spinner=False)
def fetch_metrics(api_base_url: str) -> dict[str, Any]:
    """Descarga y parsea las familias Prometheus."""
    try:
        response = requests.get(f"{api_base_url}/metrics", timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        samples = []
        for family in text_string_to_metric_families(response.text):
            samples.extend({"name": item.name, "labels": dict(item.labels), "value": item.value} for item in family.samples)
        return {"ok": True, "samples": samples}
    except (requests.RequestException, ValueError) as error:
        return {"ok": False, "samples": [], "error": str(error)}


@st.cache_data(ttl=5, show_spinner=False)
def fetch_summary(api_base_url: str, admin_token: str, window_minutes: int) -> dict[str, Any]:
    """Consulta el resumen persistente y sus series por minuto."""
    try:
        response = requests.get(
            f"{api_base_url}/api/v1/observability/summary",
            params={"window_minutes": window_minutes, "bucket_minutes": 1},
            headers=headers(admin_token), timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in (401, 404):
            return {"ok": False, "status": response.status_code, "error": response.text}
        response.raise_for_status()
        return {"ok": True, "data": response.json()}
    except (requests.RequestException, ValueError) as error:
        return {"ok": False, "status": None, "error": str(error)}


@st.cache_data(ttl=5, show_spinner=False)
def fetch_db_activity(api_base_url: str, admin_token: str) -> dict[str, Any]:
    """Consulta actividad actual de PostgreSQL."""
    try:
        response = requests.get(
            f"{api_base_url}/api/v1/observability/db-activity",
            headers=headers(admin_token), timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in (401, 404):
            return {"ok": False, "status": response.status_code, "error": response.text}
        response.raise_for_status()
        return {"ok": True, "data": response.json()}
    except (requests.RequestException, ValueError) as error:
        return {"ok": False, "status": None, "error": str(error)}


@st.cache_data(ttl=5, show_spinner=False)
def fetch_trace(api_base_url: str, admin_token: str, transaction_id: str) -> dict[str, Any]:
    """Consulta la trazabilidad persistente de una transacción."""
    try:
        response = requests.get(
            f"{api_base_url}/api/v1/observability/transactions/{transaction_id}",
            headers=headers(admin_token), timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in (401, 404):
            return {"ok": False, "status": response.status_code, "error": response.text}
        response.raise_for_status()
        return {"ok": True, "data": response.json()}
    except (requests.RequestException, ValueError) as error:
        return {"ok": False, "status": None, "error": str(error)}


def metric_value(samples: list[dict[str, Any]], name: str, labels: dict[str, str] | None = None) -> float:
    """Obtiene una muestra por nombre y labels."""
    labels = labels or {}
    for sample in samples:
        if sample["name"] == name and all(sample["labels"].get(key) == value for key, value in labels.items()):
            return float(sample["value"])
    return 0.0


def counter_rows(samples: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Convierte un counter etiquetado en filas."""
    return [{**item["labels"], "count": float(item["value"])} for item in samples if item["name"] == name]


def aggregate_histogram(samples: list[dict[str, Any]], base_name: str, label_filter: dict[str, str] | None = None) -> dict[str, Any]:
    """Agrega buckets Prometheus y estima p95."""
    label_filter = label_filter or {}
    buckets: dict[float, float] = {}
    count = total = 0.0
    for item in samples:
        if not item["name"].startswith(f"{base_name}_"):
            continue
        if not all(item["labels"].get(key) == value for key, value in label_filter.items()):
            continue
        if item["name"].endswith("_bucket") and item["labels"].get("le") not in (None, "+Inf"):
            upper = float(item["labels"]["le"])
            buckets[upper] = buckets.get(upper, 0) + float(item["value"])
        elif item["name"].endswith("_count"):
            count += float(item["value"])
        elif item["name"].endswith("_sum"):
            total += float(item["value"])
    if not count:
        return {"count": 0, "sum": 0, "average": 0, "p95": 0, "buckets": []}
    target = count * .95
    p95 = max(buckets, default=0.0)
    for upper, cumulative in sorted(buckets.items()):
        if cumulative >= target:
            p95 = upper
            break
    return {"count": count, "sum": total, "average": total / count, "p95": p95,
            "buckets": [{"seconds": key, "count": value} for key, value in sorted(buckets.items())]}


def render_header(api_base_url: str, window_label: str) -> None:
    """Renderiza estado de API y hora de actualización."""
    health = fetch_health(api_base_url)
    if health["ok"]:
        pill = '<span class="status-pill status-ok">● API operativa</span>'
    else:
        pill = '<span class="status-pill status-bad">● API no disponible</span>'
        st.error("API no disponible. Revisa el backend y la URL configurada.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st.markdown(f'<div class="ops-header"><div class="ops-title">SmartBancs · Observabilidad Transaccional</div><div class="ops-subtitle">{pill} &nbsp; Ventana: {window_label} &nbsp; Actualizado: {now}</div></div>', unsafe_allow_html=True)


def render_kpis(summary: dict[str, Any], samples: list[dict[str, Any]], db_activity: dict[str, Any], sla_ms: int) -> None:
    """Renderiza KPIs y alertas operativas."""
    totals = summary.get("totals", {})
    outcomes = totals.get("outcomes", {})
    total = sum(outcomes.values())
    success = outcomes.get("SUCCESS", 0)
    p95 = totals.get("p95_ms")
    p99 = totals.get("p99_ms")
    breaches = totals.get("over_2000_ms", 0)
    cols = st.columns(5)
    cols[0].metric("Transacciones", f"{total:,.0f}")
    cols[1].metric("Tasa de éxito", f"{success / total * 100 if total else 0:.1f}%", f"{success:,.0f} exitosas")
    cols[2].metric("Latencia p95", "—" if p95 is None else f"{p95:,.0f} ms", "SLA 2,000 ms")
    cols[3].metric("Latencia p99", "—" if p99 is None else f"{p99:,.0f} ms")
    cols[4].metric("Incumplimientos SLA", f"{breaches:,.0f}", "latency_ms > 2,000")
    within = max(0.0, min(1.0, 1 - breaches / total if total else 1))
    st.caption(f"Transacciones dentro del SLA de {sla_ms:,.0f} ms: {within * 100:.1f}%")
    st.progress(within)
    rows = counter_rows(samples, "smartbancs_transactions_total")
    all_count = sum(row["count"] for row in rows)
    system_rate = sum(row["count"] for row in rows if row.get("error_class") == "system") / all_count * 100 if all_count else 0
    blocked = db_activity.get("blocked_queries", [])
    alerts: list[tuple[str, str]] = []
    if p95 is not None and p95 > 2000:
        alerts.append(("critical", f"p95 por encima del SLA: {p95:,.0f} ms"))
    elif p95 is not None and p95 > 1500:
        alerts.append(("warning", f"p95 en zona ámbar: {p95:,.0f} ms"))
    if system_rate > 1:
        alerts.append(("critical", f"Tasa de error del sistema: {system_rate:.2f}%"))
    if any(float(item.get("seconds_waiting", 0)) > 5 for item in blocked):
        alerts.append(("critical", "Sesiones bloqueadas durante más de 5 segundos"))
    st.subheader("Alertas activas")
    if not alerts:
        st.markdown('<div class="alert-line alert-ok">Sin alertas</div>', unsafe_allow_html=True)
    for level, message in alerts:
        css = "alert-critical" if level == "critical" else ""
        st.markdown(f'<div class="alert-line {css}">{level.upper()} · {message}</div>', unsafe_allow_html=True)


def render_latency_tab(summary: dict[str, Any], samples: list[dict[str, Any]], sla_ms: int) -> None:
    """Renderiza latencias por tiempo, buckets y comparación HTTP."""
    totals = summary.get("totals", {})
    series = pd.DataFrame(totals.get("latency_series", []))
    if not series.empty:
        series["bucket"] = pd.to_datetime(series["bucket"])
        figure = go.Figure()
        for column, color in (("p50_ms", "#5ca8ff"), ("p95_ms", "#f0b35b"), ("p99_ms", "#f06a6a")):
            figure.add_trace(go.Scatter(x=series["bucket"], y=series[column], mode="lines+markers", name=column.upper(), line={"color": color}))
        figure.add_hline(y=sla_ms, line_color="#f06a6a", line_dash="dash", annotation_text="SLA")
        figure.update_layout(template="plotly_dark", height=330, yaxis_title="Milisegundos")
        st.plotly_chart(figure, use_container_width=True)
    else:
        st.info("No hay transacciones persistidas para la serie temporal.")
    histogram = aggregate_histogram(samples, "smartbancs_transaction_business_duration_seconds")
    if histogram["buckets"]:
        frame = pd.DataFrame(histogram["buckets"])
        frame["bucket"] = frame["seconds"].map(lambda value: f"≤ {value:g}s")
        st.subheader("Distribución de latencia de negocio")
        st.plotly_chart(px.bar(frame, x="bucket", y="count", template="plotly_dark", height=300), use_container_width=True)
    business = aggregate_histogram(samples, "smartbancs_transaction_business_duration_seconds")
    http = aggregate_histogram(samples, "smartbancs_http_request_duration_seconds", {"route": "/api/v1/transactions"})
    comparison = pd.DataFrame({"Tipo": ["Negocio", "HTTP total"], "Promedio (ms)": [business["average"] * 1000, http["average"] * 1000], "p95 estimado (ms)": [business["p95"] * 1000, http["p95"] * 1000]})
    st.subheader("Latencia de negocio vs. HTTP total")
    st.plotly_chart(px.bar(comparison, x="Tipo", y=["Promedio (ms)", "p95 estimado (ms)"], barmode="group", template="plotly_dark", height=320), use_container_width=True)
    st.caption("La diferencia incluye overhead de red, framework y serialización.")
    st.subheader("Top 10 transacciones más lentas")
    st.dataframe(pd.DataFrame(totals.get("slow_transactions", [])), use_container_width=True, hide_index=True)


def render_results_tab(summary: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    """Renderiza outcomes, errores y deadlocks."""
    rows = counter_rows(samples, "smartbancs_transactions_total")
    frame = pd.DataFrame(rows)
    if not frame.empty:
        st.plotly_chart(px.bar(frame, x="outcome", y="count", color="error_class", barmode="stack", color_discrete_map=ERROR_COLORS, template="plotly_dark", height=330), use_container_width=True)
    total = frame["count"].sum() if not frame.empty else 0
    client = frame.loc[frame["error_class"] == "client", "count"].sum() if not frame.empty else 0
    system = frame.loc[frame["error_class"] == "system", "count"].sum() if not frame.empty else 0
    left, right = st.columns(2)
    left.metric("Tasa de error del cliente", f"{client / total * 100 if total else 0:.2f}%")
    right.metric("Tasa de error del SISTEMA", f"{system / total * 100 if total else 0:.2f}%")
    st.caption("Solo el error del sistema debe disparar alertas de disponibilidad.")
    timeline = pd.DataFrame(summary.get("totals", {}).get("attempts_timeline", []))
    if not timeline.empty:
        timeline["bucket"] = pd.to_datetime(timeline["bucket"])
        st.subheader("Intentos por resultado en el tiempo")
        st.plotly_chart(px.line(timeline, x="bucket", y="count", color="outcome", markers=True, template="plotly_dark", height=280), use_container_width=True)
    st.metric("Deadlocks reales detectados", f"{metric_value(samples, 'smartbancs_db_deadlocks_total'):,.0f}")


def render_database_tab(activity: dict[str, Any] | None, error: dict[str, Any] | None) -> None:
    """Renderiza actividad real de PostgreSQL."""
    if error:
        if error.get("status") == 404:
            st.warning("El diagnóstico de base de datos está deshabilitado en el backend.")
        elif error.get("status") == 401:
            st.warning("El token de administración para base de datos es inválido.")
        else:
            st.error("No se pudo consultar la actividad de PostgreSQL.")
        return
    activity = activity or {}
    active = activity.get("active_connections", [])
    blocked = activity.get("blocked_queries", [])
    long_running = activity.get("long_running_queries", [])
    idle = activity.get("idle_in_transaction", [])
    locks = activity.get("locks_summary", [])
    columns = st.columns(4)
    columns[0].metric("Conexiones activas", sum(int(item.get("count", 0)) for item in active))
    columns[1].metric("Sesiones bloqueadas", len(blocked))
    columns[2].metric("Consulta más larga", f"{max((float(item.get('seconds_running', 0)) for item in long_running), default=0):.2f} s")
    columns[3].metric("Idle in transaction", len(idle))
    st.subheader("Consultas bloqueadas")
    if blocked:
        st.dataframe(pd.DataFrame(blocked), use_container_width=True, hide_index=True)
    else:
        st.success("Sin contención detectada")
    st.subheader("Consultas de larga duración")
    st.dataframe(pd.DataFrame(long_running), use_container_width=True, hide_index=True)
    if locks:
        frame = pd.DataFrame(locks)
        frame["estado"] = frame["granted"].map({True: "granted", False: "waiting"}).fillna(frame["granted"].astype(str))
        st.subheader("Locks por tipo y modo")
        st.plotly_chart(px.bar(frame, x="locktype", y="count", color="estado", facet_col="mode", barmode="group", template="plotly_dark", height=360), use_container_width=True)


def run_transfer(api_base_url: str, source: str, destination: str, amount: float) -> dict[str, Any]:
    """Ejecuta una transferencia real y mide latencia de cliente."""
    started_at = time.perf_counter()
    try:
        response = requests.post(f"{api_base_url}/api/v1/transactions", json={"source_account": source, "destination_account": destination, "amount": amount}, timeout=REQUEST_TIMEOUT_SECONDS)
        return {"status": response.status_code, "latency_ms": (time.perf_counter() - started_at) * 1000, "body": response.text}
    except requests.RequestException as error:
        return {"status": None, "latency_ms": (time.perf_counter() - started_at) * 1000, "body": str(error)}


def render_traceability_tab(api_base_url: str, admin_token: str) -> None:
    """Renderiza trazabilidad, transferencia manual y carga real."""
    st.subheader("Buscar una transacción")
    with st.form("trace_form"):
        transaction_id = st.text_input("transaction_id", placeholder="UUID de la transacción")
        trace_submit = st.form_submit_button("Consultar trazabilidad")
    if trace_submit and transaction_id.strip():
        result = fetch_trace(api_base_url, admin_token, transaction_id.strip())
        if result["ok"]:
            data = result["data"]
            st.json(data)
            st.dataframe(pd.DataFrame(data.get("attempts", [])), use_container_width=True, hide_index=True)
        elif result.get("status") == 404:
            st.warning("Transacción no encontrada.")
        elif result.get("status") == 401:
            st.warning("Token de administración inválido.")
        else:
            st.error("No se pudo consultar la trazabilidad.")
    st.divider()
    st.subheader("Transferencia manual")
    with st.form("manual_transfer_form"):
        source = st.text_input("Cuenta origen", "CTA-1001")
        destination = st.text_input("Cuenta destino", "CTA-2002")
        amount = st.number_input("Monto", min_value=0.01, value=10.00, step=1.00)
        submit = st.form_submit_button("Ejecutar transferencia real")
    if submit:
        result = run_transfer(api_base_url, source, destination, amount)
        if result["status"] in (200, 201):
            st.success(f"Transferencia completada en {result['latency_ms']:.2f} ms")
            st.code(result["body"], language="json")
        else:
            st.error(f"Respuesta HTTP {result['status'] or 'sin conexión'}: {result['body']}")
    st.divider()
    st.subheader("Generador de carga real")
    with st.form("load_form"):
        total = st.number_input("Número de transferencias", min_value=1, max_value=500, value=20, step=1)
        threads = st.number_input("Concurrencia (threads)", min_value=1, max_value=50, value=10, step=1)
        load_amount = st.number_input("Monto por transferencia", min_value=0.01, value=0.01, step=0.01, format="%.2f")
        load_submit = st.form_submit_button("Iniciar carga real")
    if load_submit:
        progress = st.progress(0.0)
        results = []
        with ThreadPoolExecutor(max_workers=int(threads)) as executor:
            futures = [executor.submit(run_transfer, api_base_url, "CTA-1001" if index % 2 == 0 else "CTA-2002", "CTA-2002" if index % 2 == 0 else "CTA-1001", float(load_amount)) for index in range(int(total))]
            for completed, future in enumerate(as_completed(futures), start=1):
                results.append(future.result())
                progress.progress(completed / len(futures))
        latencies = sorted(item["latency_ms"] for item in results)
        p95_index = min(len(latencies) - 1, max(0, int(len(latencies) * .95) - 1))
        successes = sum(item["status"] in (200, 201) for item in results)
        rejected = sum(item["status"] in (400, 404, 422, 503) for item in results)
        st.write({"ejecutadas": len(results), "éxitos": successes, "rechazos": rejected, "errores": len(results) - successes - rejected, "p95_cliente_ms": round(latencies[p95_index], 2) if latencies else 0})
    with st.expander("Cómo leer este panel"):
        st.markdown("- **Resumen:** métricas Prometheus y reglas explícitas de SLA.\n- **Latencia:** negocio, HTTP total y buckets.\n- **Resultados:** logs y contadores por tipo.\n- **Base de datos:** `pg_stat_activity` y `pg_locks`.")


def render_live_panel(api_base_url: str, admin_token: str, window_label: str, window_minutes: int, sla_ms: int) -> None:
    """Carga fuentes reales y renderiza las cinco pestañas."""
    metrics = fetch_metrics(api_base_url)
    summary = fetch_summary(api_base_url, admin_token, window_minutes)
    activity = fetch_db_activity(api_base_url, admin_token)
    render_header(api_base_url, window_label)
    if not metrics["ok"] or not summary["ok"]:
        st.error("API no disponible o datos de observabilidad incompletos.")
        return
    samples = metrics["samples"]
    summary_data = summary["data"]
    tabs = st.tabs(["Resumen (SLA)", "Latencia", "Resultados", "Base de datos", "Trazabilidad y pruebas"])
    with tabs[0]:
        render_kpis(summary_data, samples, activity.get("data", {}) if activity["ok"] else {}, sla_ms)
    with tabs[1]:
        render_latency_tab(summary_data, samples, sla_ms)
    with tabs[2]:
        render_results_tab(summary_data, samples)
    with tabs[3]:
        render_database_tab(activity.get("data") if activity["ok"] else None, None if activity["ok"] else activity)
    with tabs[4]:
        render_traceability_tab(api_base_url, admin_token)


def main() -> None:
    """Configura preferencias y monta el panel refrescable."""
    inject_styles()
    st.sidebar.title("Controles operativos")
    api_base_url = st.sidebar.text_input("API base URL", DEFAULT_API_BASE_URL).rstrip("/")
    admin_token = st.sidebar.text_input("Admin token", DEFAULT_ADMIN_TOKEN, type="password")
    refresh_label = st.sidebar.selectbox("Auto-refresco", list(REFRESH_OPTIONS), index=1)
    windows = {"15 min": 15, "1 h": 60, "24 h": 1440}
    window_label = st.sidebar.selectbox("Ventana global", list(windows), index=1)
    sla_ms = st.sidebar.number_input("SLA de negocio (ms)", min_value=100, value=2000, step=100)
    args = (api_base_url, admin_token, window_label, windows[window_label], int(sla_ms))
    refresh_seconds = REFRESH_OPTIONS[refresh_label]
    if refresh_seconds and hasattr(st, "fragment"):
        @st.fragment(run_every=refresh_seconds)
        def refreshable_panel() -> None:
            render_live_panel(*args)
        refreshable_panel()
    else:
        render_live_panel(*args)


if __name__ == "__main__":
    main()