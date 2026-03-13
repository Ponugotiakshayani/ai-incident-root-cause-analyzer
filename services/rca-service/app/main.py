from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from prometheus_client import Gauge
import os
import time
import threading
import requests
from typing import Any, Dict, List, Optional

from .prometheus_client import query_prometheus, extract_first_value

app = FastAPI(title="RCA Service")

# ----------------------------
# Config
# ----------------------------
SERVICE_A_URL = os.getenv("SERVICE_A_URL", "http://service-a:8000")
TIMEOUT_SECONDS = float(os.getenv("RCA_HTTP_TIMEOUT", "3.0"))

POLL_ENABLED = os.getenv("RCA_POLL_ENABLED", "true").lower() == "true"
POLL_INTERVAL_SECONDS = int(os.getenv("RCA_POLL_INTERVAL_SECONDS", "15"))
DEFAULT_MODE = os.getenv("RCA_DEFAULT_MODE", "slow")  # slow|normal (depends on your service-a implementation)

SLOW_DOWNSTREAM_MS = int(os.getenv("RCA_SLOW_DOWNSTREAM_MS", "1000"))
SLOW_UPSTREAM_MS = int(os.getenv("RCA_SLOW_UPSTREAM_MS", "800"))

# ----------------------------
# Prometheus custom metrics
# ----------------------------
RCA_UPSTREAM_LATENCY_MS = Gauge("rca_upstream_latency_ms", "Last observed service-a latency in ms")
RCA_DOWNSTREAM_LATENCY_MS = Gauge("rca_downstream_latency_ms", "Last observed downstream(service-b) latency in ms")
RCA_INCIDENT_ACTIVE = Gauge("rca_incident_active", "1 if incident detected, else 0")
RCA_LAST_CONFIDENCE = Gauge("rca_last_confidence", "Confidence mapped to number: low=0, medium=1, high=2")

# In-memory incident store (simple demo)
INCIDENTS: List[Dict[str, Any]] = []
MAX_INCIDENTS = 20


def _safe_get_json(url: str, timeout: float = TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _confidence_to_number(conf: str) -> float:
    conf = (conf or "").lower()
    if conf == "high":
        return 2.0
    if conf == "medium":
        return 1.0
    return 0.0


def _classify_root_cause(a_latency: int, b_latency: int) -> Dict[str, Any]:
    observations: List[str] = []
    likely_causes: List[Dict[str, Any]] = []
    next_steps: List[str] = []
    confidence = "low"

    observations.append(f"service-a end-to-end latency: {a_latency} ms")
    observations.append(f"downstream (service-b) latency: {b_latency} ms")

    if b_latency >= SLOW_DOWNSTREAM_MS:
        likely_causes.append({
            "cause": "Downstream slowness in service-b (or its dependency)",
            "why": f"Downstream latency is {b_latency} ms which is above the {SLOW_DOWNSTREAM_MS} ms threshold and dominates end-to-end time."
        })
        confidence = "high"
        next_steps += [
            "Open service-b logs around the same timestamp and look for slow endpoint warnings or errors.",
            "Check whether /slow or an intentional sleep path is being triggered (test traffic patterns).",
            "If service-b calls any external dependency, verify that dependency latency (DB/network) is stable.",
            "Try hitting service-b directly (/work and /slow) to confirm if the slowness reproduces without service-a.",
        ]

    if a_latency >= SLOW_UPSTREAM_MS and b_latency < SLOW_DOWNSTREAM_MS:
        likely_causes.insert(0, {
            "cause": "Upstream slowness inside service-a (logic before/after downstream call)",
            "why": f"service-a latency is {a_latency} ms but downstream is only {b_latency} ms, so the delay is likely inside service-a."
        })
        confidence = "medium"
        next_steps += [
            "Add timing breakdown in service-a (before call / after call) to isolate where time is spent.",
            "Check CPU usage / blocking calls inside service-a.",
            "Inspect traces in Jaeger for spans inside service-a to identify the slow step.",
        ]

    if a_latency < SLOW_UPSTREAM_MS and b_latency < SLOW_DOWNSTREAM_MS:
        likely_causes = [{
            "cause": "No clear incident",
            "why": "Both upstream and downstream latencies are under configured thresholds."
        }]
        confidence = "low"
        next_steps = [
            "Run the analysis again during a real spike.",
            "Generate traffic that increases latency to validate alerts."
        ]

    top_cause = likely_causes[0]["cause"] if likely_causes else "unknown"
    if "service-b" in top_cause.lower() or "downstream" in top_cause.lower():
        root_cause_label = "service-b latency spike"
    elif "service-a" in top_cause.lower() or "upstream" in top_cause.lower():
        root_cause_label = "service-a processing delay"
    else:
        root_cause_label = "unknown"

    summary = (
        f"High latency detected. The strongest signal points to: {top_cause}. "
        f"(a={a_latency} ms, b={b_latency} ms, confidence={confidence})"
    )

    return {
        "incident": "high latency detected" if (a_latency >= SLOW_UPSTREAM_MS or b_latency >= SLOW_DOWNSTREAM_MS) else "no incident",
        "root_cause": root_cause_label,
        "summary": summary,
        "observations": observations,
        "likely_root_causes": likely_causes,
        "recommended_next_steps": next_steps[:8],
        "confidence": confidence,
    }


def _run_analysis(mode: str) -> Dict[str, Any]:
    url = f"{SERVICE_A_URL}/work_with_dependency?mode={mode}"
    data = _safe_get_json(url)

    if not data:
        result = {
            "status": "error",
            "message": "service-a unavailable",
            "incident": "unknown",
            "root_cause": "service-a unavailable",
            "summary": "RCA could not run because service-a did not respond.",
            "confidence": "high",
            "recommended_next_steps": [
                "Check whether service-a container is running.",
                "Verify service-a is reachable from rca-service network.",
                "Check service-a logs for startup errors.",
            ],
        }
        return result

    a_latency = int(data.get("latency_ms", -1))
    downstream = data.get("downstream") or {}
    b_latency = int(downstream.get("latency_ms", -1))

    # Prometheus error check
    error_query = 'rate(http_requests_total{job="service-b",status=~"5.."}[1m])'
    error_json = query_prometheus(error_query)
    service_b_error_rate = extract_first_value(error_json) or 0.0

    if a_latency < 0 or b_latency < 0:
        return {
            "status": "error",
            "message": "unexpected response format from service-a",
            "raw": data,
            "incident": "unknown",
            "root_cause": "unexpected response format",
            "summary": "RCA could not run because upstream response was missing latency fields.",
            "confidence": "medium",
            "recommended_next_steps": [
                "Confirm service-a returns latency_ms and downstream.latency_ms fields.",
                "Check service-a /work_with_dependency implementation.",
            ],
        }

    rca = _classify_root_cause(a_latency=a_latency, b_latency=b_latency)

    if service_b_error_rate > 0:
        rca["incident"] = "service errors detected"

        rca["observations"].append(
        f"service-b 5xx error rate detected from Prometheus: {service_b_error_rate:.4f} req/s"
    )

        rca["likely_root_causes"].insert(0, {
            "cause": "service-b is returning server errors",
            "why": f"Prometheus shows a non-zero 5xx error rate for service-b ({service_b_error_rate:.4f} req/s), which may be contributing to the slowdown."
        })

        rca["summary"] = (
            f"Service errors detected. Prometheus shows service-b returning 5xx responses "
            f"at {service_b_error_rate:.4f} req/s, so service-b is the most likely root cause."
        )

        rca["root_cause"] = "service-b returning errors"    
        rca["confidence"] = "high"

    # Update custom Prometheus metrics
    RCA_UPSTREAM_LATENCY_MS.set(a_latency)
    RCA_DOWNSTREAM_LATENCY_MS.set(b_latency)
    RCA_INCIDENT_ACTIVE.set(1.0 if rca["incident"] != "no incident" else 0.0)
    RCA_LAST_CONFIDENCE.set(_confidence_to_number(rca["confidence"]))

    # Store incident if active
    if rca["incident"] != "no incident":
        incident_record = {
            "ts": int(time.time()),
            "mode": mode,
            "service_a_latency": a_latency,
            "service_b_latency": b_latency,
            **rca,
        }
        INCIDENTS.append(incident_record)
        if len(INCIDENTS) > MAX_INCIDENTS:
            del INCIDENTS[0:len(INCIDENTS) - MAX_INCIDENTS]

    return {
        "service_a_latency": a_latency,
        "service_b_latency": b_latency,
        **rca,
    }


def _poll_loop():
    while True:
        try:
            _run_analysis(DEFAULT_MODE)
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


# ----------------------------
# Instrumentation (/metrics)
# ----------------------------
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
def on_startup():
    if POLL_ENABLED:
        t = threading.Thread(target=_poll_loop, daemon=True)
        t.start()


# ----------------------------
# Existing API
# ----------------------------
@app.get("/analyze")
def analyze(mode: str = DEFAULT_MODE):
    return _run_analysis(mode)


# ----------------------------
# NEW: Webhook endpoint for Alertmanager
# ----------------------------
@app.post("/webhook/alertmanager")
async def alertmanager_webhook(request: Request):
    payload = await request.json()
    alerts = payload.get("alerts", [])

    # When an alert comes in, run immediate RCA once and return the report
    report = _run_analysis(DEFAULT_MODE)
    return {"received_alerts": len(alerts), "rca_report": report}


# ----------------------------
# NEW: Read the latest incident (for UI/Grafana later)
# ----------------------------
@app.get("/incidents/latest")
def latest_incident():
    if not INCIDENTS:
        return {"status": "ok", "message": "no incidents yet"}
    return {"status": "ok", "incident": INCIDENTS[-1]}


@app.get("/incidents")
def list_incidents(limit: int = 10):
    limit = max(1, min(limit, 50))
    return {"status": "ok", "incidents": INCIDENTS[-limit:]}