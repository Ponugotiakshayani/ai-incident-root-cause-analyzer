import os
from typing import Any, Dict, Optional

import requests


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
PROM_TIMEOUT = float(os.getenv("PROM_TIMEOUT", "5.0"))


def _safe_get(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(url, params=params, timeout=PROM_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def query_prometheus(promql: str) -> Optional[Dict[str, Any]]:
    url = f"{PROMETHEUS_URL}/api/v1/query"
    return _safe_get(url, params={"query": promql})


def extract_first_value(result_json: Optional[Dict[str, Any]]) -> Optional[float]:
    if not result_json:
        return None

    data = result_json.get("data", {})
    results = data.get("result", [])
    if not results:
        return None

    try:
        value = results[0]["value"][1]
        return float(value)
    except Exception:
        return None