import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

DEFAULT_LOOKBACK_SECONDS = int(os.getenv("RCA_LOOKBACK_SECONDS", "1800"))  # 30 min


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
    Collect a compact snapshot of metrics that are usually enough for RCA:
    - up
    - request rate
    - error rate (if available)
    - latency p95/p99 (if histogram exists)
    """
    end = _now_ts()
    start = end - lookback_seconds

    context: Dict[str, Any] = {
        "time_window": {"start": start, "end": end, "step": 30},
        "services": services,
        "metrics": {},
    }

    # These queries assume you have common metric names.
    # If your names differ, replace them with your actual metrics.
    queries = {
        "up": 'up',
        "http_requests_rate": 'sum by (job) (rate(http_requests_total[5m]))',
        "http_5xx_rate": 'sum by (job) (rate(http_requests_total{status=~"5.."}[5m]))',
        "latency_p95": 'histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket[5m])))',
    }

    for key, q in queries.items():
        try:
            context["metrics"][key] = _query_range(q, start, end, step=30)
        except Exception as e:
            context["metrics"][key] = {"error": str(e), "query": q}

    return context


def _heuristic_rca(context: Dict[str, Any], symptoms: List[str]) -> Dict[str, Any]:
    """
    Fallback if no LLM key is provided.
    Gives a decent “first-pass” RCA from common patterns.
    """
    findings = []
    actions = []

    # Example: detect if any 'up' is 0
    up = context.get("metrics", {}).get("up", {})
    up_results = (((up or {}).get("data") or {}).get("result") or [])
    down_jobs = []
    for series in up_results:
        job = series.get("metric", {}).get("job")
        values = series.get("values") or []
        # if latest is 0
        if values and float(values[-1][1]) == 0.0:
            down_jobs.append(job)

    if down_jobs:
        findings.append(f"Some scrape targets look DOWN in Prometheus: {sorted(set(down_jobs))}.")
        actions += [
            "Check the service container is running and reachable from Prometheus network.",
            "Verify the service exposes /metrics and that the port in prometheus.yml is correct.",
            "Open the service logs and confirm the metrics endpoint is registered.",
        ]

    if any("404" in s.lower() and "metrics" in s.lower() for s in symptoms):
        findings.append("Symptom mentions 404 on /metrics, which usually means the metrics route is not mounted or wrong path.")
        actions += [
            "In the service code, ensure the instrumentation middleware/router is added before app startup completes.",
            "Hit the container directly: curl http://<service>:<port>/metrics",
            "Confirm Prometheus scrape path is /metrics (not /metric).",
        ]

    if not findings:
        findings.append("No obvious single failure detected from the limited snapshot. Likely needs deeper drilldown.")
        actions += [
            "Add error breakdown by route/status and correlate with deployments.",
            "Collect logs around the spike window and compare with latency histogram.",
            "Check downstream dependency health (DB, external calls).",
        ]

    return {
        "summary": " | ".join(findings),
        "likely_causes": findings,
        "recommended_actions": actions[:8],
        "confidence": "medium" if findings else "low",
        "used": "heuristic",
    }


def _call_openai_chat(prompt: str) -> str:
    """
    Minimal OpenAI chat call. If you use another provider, swap this out.
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": [
            {"role": "system", "content": "You are an SRE-style root cause analyzer. Be specific, cite evidence from metrics, and give actionable steps."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def llm_rca(context: Dict[str, Any], symptoms: List[str]) -> Dict[str, Any]:
    prompt = f"""
We have Prometheus query_range results (JSON) for a 30-minute window and current symptoms.
Symptoms:
{json.dumps(symptoms, indent=2)}

Prometheus context JSON:
{json.dumps(context, indent=2)}

Task:
1) Provide likely root cause(s), ranked.
2) Provide evidence (which metrics and what changed).
3) Provide concrete next checks and fixes.
4) Provide a confidence level (low/medium/high).
Return in JSON with keys: summary, likely_causes (list), evidence (list), recommended_actions (list), confidence.
"""

    if not OPENAI_API_KEY:
        return _heuristic_rca(context, symptoms)

    try:
        content = _call_openai_chat(prompt)
        # If the model returns JSON, parse it; if not, wrap it.
        try:
            parsed = json.loads(content)
            parsed["used"] = "llm"
            return parsed
        except Exception:
            return {"summary": content, "used": "llm", "confidence": "medium"}
    except Exception as e:
        out = _heuristic_rca(context, symptoms)
        out["error"] = f"LLM call failed: {e}"
        return out