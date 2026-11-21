from __future__ import annotations


APPROVER = {"X-Actor": "finops-lead", "X-Role": "approver"}


def recommendation_id(client) -> str:
    return client.get("/api/v1/demo").json()["recommendation_id"]


def test_operational_endpoints(client):
    assert client.get("/api/v1/health").json()["status"] == "healthy"
    ready = client.get("/api/v1/readiness").json()
    assert ready == {"status": "ready", "database": "sqlite", "adapter": "SyntheticAWSAdapter", "llm_mode": "mock"}
    assert "finops_requests_total" in client.get("/metrics").text
    assert client.get("/openapi.json").status_code == 200


def test_demo_is_typed_and_evidence_backed(client):
    response = client.get("/api/v1/demo")
    assert response.status_code == 200
    demo = response.json()
    assert demo["state"] == "AWAITING_APPROVAL"
    assert demo["rca"]["confidence"] >= 0.9
    assert len(demo["rca"]["evidence"]) >= 6
    assert all(h["supporting_evidence_ids"] for h in demo["rca"]["hypotheses"])
    assert "REVIEW-ONLY" in demo["terraform_proposal"]


def test_cheapest_option_is_prevented_by_shadow_slo(client):
    validation = client.get("/api/v1/demo").json()["validation"]
    blocked = validation["blocked_alternative"]
    assert validation["passed"] is True
    assert blocked["prevented"] is True
    assert blocked["risk"] == "CRITICAL"
    assert blocked["projected_p95_latency_ms"] > 250


def test_viewer_cannot_approve(client):
    rec_id = recommendation_id(client)
    response = client.post(
        f"/api/v1/recommendations/{rec_id}/approval",
        headers={"X-Actor": "viewer", "X-Role": "viewer"},
        json={"decision": "APPROVE", "reason": "looks acceptable"},
    )
    assert response.status_code == 403


def test_apply_is_blocked_before_approval(client):
    rec_id = recommendation_id(client)
    response = client.post(
        f"/api/v1/recommendations/{rec_id}/simulate-apply", headers=APPROVER
    )
    assert response.status_code == 409
    assert "approved recommendation" in response.json()["detail"]


def test_full_approval_apply_and_savings_verification(client):
    rec_id = recommendation_id(client)
    approval = client.post(
        f"/api/v1/recommendations/{rec_id}/approval",
        headers=APPROVER,
        json={"decision": "APPROVE", "reason": "All deterministic SLO guardrails passed."},
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "APPROVED"
    execution = client.post(
        f"/api/v1/recommendations/{rec_id}/simulate-apply", headers=APPROVER
    )
    assert execution.status_code == 200
    result = execution.json()
    assert result["mode"] == "SIMULATION"
    assert result["status"] == "SUCCEEDED"
    observed_cost = result["observed_post_state"]["monthly_cost_usd"]
    assert result["realized_monthly_savings_usd"] == round(280.0 - observed_cost, 2)
    assert result["estimated_monthly_savings_usd"] != result["realized_monthly_savings_usd"]
    assert "No breach" in result["slo_impact"]
    metrics = client.get("/api/v1/evaluations").json()
    savings = next(m for m in metrics if m["metric"] == "estimated_vs_realized_savings_accuracy")
    expected_accuracy = 1 - abs(
        result["estimated_monthly_savings_usd"] - result["realized_monthly_savings_usd"]
    ) / result["estimated_monthly_savings_usd"]
    assert savings["value"] == expected_accuracy


def test_rejection_prevents_apply(client):
    rec_id = recommendation_id(client)
    rejected = client.post(
        f"/api/v1/recommendations/{rec_id}/approval",
        headers=APPROVER,
        json={"decision": "REJECT", "reason": "Change freeze is active."},
    )
    assert rejected.json()["status"] == "REJECTED"
    assert client.post(
        f"/api/v1/recommendations/{rec_id}/simulate-apply", headers=APPROVER
    ).status_code == 409


def test_dashboard_exposes_domain_specific_sections(client):
    data = client.get("/api/v1/dashboard").json()
    assert data["cloud_spend_usd"] == 320.0
    assert data["potential_savings_usd"] > data["validated_savings_usd"]
    assert data["validation"]["blocked_alternative"]["prevented"] is True
    assert data["mode"] == "MOCK / SYNTHETIC"


def test_input_validation_and_unknown_recommendation(client):
    invalid = client.post(
        "/api/v1/recommendations/nope/approval", headers=APPROVER,
        json={"decision": "APPROVE", "reason": "ok"},
    )
    assert invalid.status_code == 422
    missing = client.post(
        "/api/v1/recommendations/nope/approval", headers=APPROVER,
        json={"decision": "APPROVE", "reason": "valid reason"},
    )
    assert missing.status_code == 404


def test_audit_history_records_governed_steps(client):
    rec_id = recommendation_id(client)
    client.post(
        f"/api/v1/recommendations/{rec_id}/approval", headers=APPROVER,
        json={"decision": "APPROVE", "reason": "Validation evidence reviewed."},
    )
    audit = client.get("/api/v1/audit").json()
    assert any(item["action"] == "investigate_and_validate" for item in audit)
    assert any(item["action"] == "approval_decision" for item in audit)
    assert all("LLM_API_KEY" not in item["input_json"] for item in audit)
