"""Run the complete API demo against a service listening on port 8102."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


BASE = "http://127.0.0.1:8102"
HEADERS = {"Content-Type": "application/json", "X-Actor": "demo-operator", "X-Role": "approver"}


def request(method: str, path: str, body: dict | None = None) -> dict:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=payload, headers=HEADERS, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def main() -> None:
    request("POST", "/api/v1/demo/reset")
    investigation = request("POST", "/api/v1/demo/run")
    recommendation_id = investigation["recommendation_id"]
    blocked = investigation["validation"]["blocked_alternative"]
    print(f"Unsafe option prevented: {blocked['option']} -> {blocked['reason']}")
    request("POST", f"/api/v1/recommendations/{recommendation_id}/approval", {
        "decision": "APPROVE", "reason": "Automated demo: all shadow guardrails passed."
    })
    result = request("POST", f"/api/v1/recommendations/{recommendation_id}/simulate-apply")
    print(f"Simulation: {result['status']}; realized savings ${result['realized_monthly_savings_usd']:.2f}/month")
    print(f"SLO outcome: {result['slo_impact']}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        raise SystemExit(f"Service unavailable at {BASE}: {exc}") from exc
