from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

import time
import random
import os
import json
import requests
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

from prometheus_fastapi_instrumentator import Instrumentator

from prometheus_client import Counter

ERROR_COUNTER = Counter(
    "service_b_errors_total",
    "Total number of errors in service-b"
)
# ----------------------------
# Tracing setup (as you have)
# ----------------------------
def setup_tracing(service_name: str):
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))

    RequestsInstrumentor().instrument()


# ----------------------------
# AI Root Cause Analyzer helpers
# ----------------------------
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
DEFAULT_LOOKBACK_SECONDS = int(os.getenv("RCA_LOOKBACK_SECONDS", "1800"))  # 30 mins

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _query_range(promql: str, start: int, end: int, step: int = 30) -> Dict[str, Any]:
    r = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": step},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def collect_rca_context(services: List[str], lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS) -> Dict[str, Any]:
    """
    Collect a compact set of Prometheus time-series that are usually useful for RCA.
    If your metric names differ, we’ll adjust queries later.
    """
    end = _now_ts()
    start = end - lookback_seconds

    context: Dict[str, Any] = {
        "time_window": {"start": start, "end": end, "step": 30},
        "services": services,
        "prometheus_url": PROMETHEUS_URL,
        "metrics": {},
    }

    # Common metric names (instrumentator uses http_requests_total, request_duration buckets vary by setup)
    queries = {
        "up": "up",
        "http_requests_rate_by_job": 'sum by (job) (rate(http_requests_total[5m]))',
        "http_5xx_rate_by_job": 'sum by (job) (rate(http_requests_total{status=~"5.."}[5m]))',
        # This one may or may not exist depending on your instrumentator version/config.
        "latency_p95_by_job": 'histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket[5m])))',
    }

    for key, q in queries.items():
        try:
            context["metrics"][key] = {
                "query": q,
                "data": _query_range(q, start, end, step=30),
            }
        except Exception as e:
            context["metrics"][key] = {
                "query": q,
                "error": str(e),
            }

    return context


def _heuristic_rca(context: Dict[str, Any], symptoms: List[str]) -> Dict[str, Any]:
    """
    Fallback RCA when no OPENAI_API_KEY is set.
    Produces a sensible first-pass RCA from common patterns (up=0, /metrics 404, etc.).
    """
    findings: List[str] = []
    evidence: List[str] = []
    actions: List[str] = []

    up_blob = (context.get("metrics", {}).get("up", {}) or {}).get("data", {})
    up_results = (((up_blob or {}).get("data") or {}).get("result") or [])

    down_jobs: List[str] = []
    for series in up_results:
        job = (series.get("metric") or {}).get("job")
        values = series.get("values") or []
        if not values:
            continue
        try:
            latest = float(values[-1][1])
            if latest == 0.0 and job:
                down_jobs.append(job)
        except Exception:
            continue

    if down_jobs:
        uniq = sorted(set(down_jobs))
        findings.append(f"Prometheus shows some targets as DOWN (up=0): {uniq}.")
        evidence.append("The 'up' metric latest sample is 0 for at least one job.")
        actions += [
            "Check those service containers are running (docker ps) and healthy.",
            "Confirm Prometheus scrape config points to the correct host:port and path (/metrics).",
            "Curl the target from inside the Prometheus container network to verify connectivity.",
        ]

    if any("metrics" in s.lower() and "404" in s.lower() for s in symptoms):
        findings.append("Symptoms mention 404 on /metrics, which usually means the metrics endpoint is not exposed or Prometheus is scraping the wrong path/port.")
        evidence.append("Scrape logs / symptoms indicate HTTP 404 for /metrics.")
        actions += [
            "In the service that returns 404, ensure Instrumentator().instrument(app).expose(app) runs after app creation.",
            "Verify the service listens on the same port Prometheus scrapes.",
            "Check prometheus.yml: metrics_path should be /metrics (default) unless changed.",
        ]

    if not findings:
        findings.append("No single obvious failure detected from the basic snapshot; needs deeper drill-down into per-route errors/latency and logs.")
        actions += [
            "Add per-route error and latency breakdown and correlate with the time window.",
            "Check recent restarts/deployments around the spike time.",
            "Inspect traces in Jaeger for the slow endpoints during the window.",
        ]

    return {
        "summary": " ".join(findings),
        "likely_causes": findings,
        "evidence": evidence,
        "recommended_actions": actions[:10],
        "confidence": "medium" if findings else "low",
        "used": "heuristic",
    }


def _call_openai_chat(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are an SRE-style root cause analyzer. Be specific, cite evidence from the given metrics JSON, and give actionable steps.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    r = requests.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        headers=headers,
        data=json.dumps(payload),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def llm_rca(context: Dict[str, Any], symptoms: List[str]) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return _heuristic_rca(context, symptoms)

    prompt = f"""
We have Prometheus query_range results (JSON) for a time window and current symptoms.

Symptoms:
{json.dumps(symptoms, indent=2)}

Prometheus context JSON:
{json.dumps(context, indent=2)}

Return STRICT JSON with keys:
summary (string),
likely_causes (list of strings ranked),
evidence (list of strings),
recommended_actions (list of strings),
confidence (low|medium|high)
"""

    try:
        content = _call_openai_chat(prompt)
        try:
            parsed = json.loads(content)
            parsed["used"] = "llm"
            return parsed
        except Exception:
            # If the model returned non-JSON, wrap it
            return {"summary": content, "used": "llm", "confidence": "medium"}
    except Exception as e:
        out = _heuristic_rca(context, symptoms)
        out["error"] = f"LLM call failed: {str(e)}"
        return out


# ----------------------------
# FastAPI app (yours + new RCA endpoint)
# ----------------------------
app = FastAPI(title="service-b")

# Instrumentation
Instrumentator().instrument(app).expose(app)  # exposes /metrics
setup_tracing("service-b")
FastAPIInstrumentor.instrument_app(app)


class RCARequest(BaseModel):
    services: List[str] = ["service-a", "service-b"]
    symptoms: List[str] = []
    lookback_seconds: Optional[int] = DEFAULT_LOOKBACK_SECONDS


@app.get("/health")
def health():
    return {"status": "ok", "service": "service-b"}


@app.get("/work")
def work():
    latency = random.randint(50, 150)
    time.sleep(latency / 1000)
    return {"status": "success", "service": "service-b", "latency_ms": latency}


@app.get("/slow")
def slow():
    latency = random.randint(1000, 3000)
    time.sleep(latency / 1000)
    return {"status": "slow", "latency_ms": latency}


@app.get("/fail")
def fail():
    return {"status": "error", "message": "service-b failure"}

@app.get("/error")
def error():
    ERROR_COUNTER.inc()
    raise HTTPException(status_code=500, detail="Simulated service failure")

# ✅ NEW: AI Root Cause Analyzer
@app.post("/ai/root-cause")
def root_cause(req: RCARequest):
    ctx = collect_rca_context(req.services, lookback_seconds=req.lookback_seconds or DEFAULT_LOOKBACK_SECONDS)
    result = llm_rca(ctx, req.symptoms)
    return {"input": req.model_dump(), "result": result}