from fastapi import FastAPI
import requests
import time
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

from prometheus_fastapi_instrumentator import Instrumentator


def setup_tracing(service_name: str) -> None:
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")

    provider.add_span_processor(BatchSpanProcessor(exporter))

    # auto-instrument outgoing HTTP calls (requests)
    RequestsInstrumentor().instrument()


app = FastAPI(title="service-a")

# Prometheus metrics endpoint
Instrumentator().instrument(app).expose(app)  # exposes /metrics

# OpenTelemetry tracing
setup_tracing("service-a")
FastAPIInstrumentor().instrument_app(app)

SERVICE_B_URL = os.getenv("SERVICE_B_URL", "http://service-b:8001")


@app.get("/health")
def health():
    return {"status": "ok", "service": "service-a"}


@app.get("/work")
def work():
    start = time.time()

    try:
        response = requests.get(f"{SERVICE_B_URL}/work", timeout=3)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return {"status": "error", "message": "service-b unavailable"}

    latency = int((time.time() - start) * 1000)

    return {
        "status": "success",
        "service": "service-a",
        "latency_ms": latency,
        "downstream": data,
    }


@app.get("/work_with_dependency")
def work_with_dependency(mode: str = "work"):
    # mode can be: "work" or "slow"
    start = time.time()
    path = "/work" if mode == "work" else "/slow"

    try:
        response = requests.get(f"{SERVICE_B_URL}{path}", timeout=5)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return {"status": "error", "message": "service-b unavailable"}

    latency = int((time.time() - start) * 1000)

    return {
        "status": "success",
        "service": "service-a",
        "mode": mode,
        "latency_ms": latency,
        "downstream": data,
    }