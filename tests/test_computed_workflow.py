from __future__ import annotations

import pytest

from backend.app.adapters import SyntheticAWSAdapter
from backend.app.database import Database
from backend.app.optimization import evaluation_values
from backend.app.workflow import FinOpsWorkflow, NoOptimizationOpportunity


class TrackingAdapter(SyntheticAWSAdapter):
    def __init__(self):
        super().__init__()
        self.workload_calls = 0
        self.apply_calls = 0

    def workload_history(self, resource_id: str) -> list[dict]:
        self.workload_calls += 1
        return super().workload_history(resource_id)

    def simulate_change(self, resource_id: str, plan: dict) -> dict[str, float | str | bool]:
        self.apply_calls += 1
        return super().simulate_change(resource_id, plan)


class NoAnomalyAdapter(SyntheticAWSAdapter):
    def cost_history(self, resource_id: str) -> list[float]:
        return [250.0, 250.0, 250.0, 252.0]


class LowPeakAdapter(SyntheticAWSAdapter):
    def workload_history(self, resource_id: str) -> list[dict]:
        return [
            {"month": f"2026-0{month}", "average_cpu_pct": 10.0, "peak_cpu_pct": 20.0, "peak_day": 28}
            for month in range(3, 8)
        ]


class BreachingObservationAdapter(SyntheticAWSAdapter):
    def simulate_change(self, resource_id: str, plan: dict) -> dict[str, float | str | bool]:
        observation = super().simulate_change(resource_id, plan)
        observation.update({
            "monthly_cost_usd": 275.0,
            "peak_cpu_pct": 97.0,
            "p95_latency_ms": 410.0,
            "error_rate_pct": 1.7,
            "availability_pct": 99.7,
            "source": "regression-breaching-observer",
        })
        self._observed_states[resource_id] = observation
        return dict(observation)


class DayFifteenAdapter(SyntheticAWSAdapter):
    def workload_history(self, resource_id: str) -> list[dict]:
        history = super().workload_history(resource_id)
        return [{**point, "peak_day": 15} for point in history]


class DriftingPeakAdapter(SyntheticAWSAdapter):
    def workload_history(self, resource_id: str) -> list[dict]:
        history = super().workload_history(resource_id)
        return [{**point, "peak_day": day} for point, day in zip(history, [5, 10, 15, 20, 25])]


class CountingNoAnomalyAdapter(NoAnomalyAdapter):
    def __init__(self):
        super().__init__()
        self.cost_calls = 0

    def cost_history(self, resource_id: str) -> list[float]:
        self.cost_calls += 1
        return super().cost_history(resource_id)


def make_workflow(tmp_path, adapter):
    db = Database(tmp_path / "computed.db")
    db.initialize()
    return FinOpsWorkflow(db, adapter), db


def test_workload_history_is_called_and_bound_to_evidence(tmp_path):
    adapter = TrackingAdapter()
    workflow, _ = make_workflow(tmp_path, adapter)
    demo = workflow.run()
    assert adapter.workload_calls >= 1
    peak = next(item for item in demo.rca.evidence if item.kind == "WORKLOAD_PATTERN" and "peak" in item.claim.lower())
    assert peak.observed_value == 84.0
    assert "5-month-history" in peak.source
    assert "5 historical monthly profiles" in demo.validation.scenario


def test_apply_calls_adapter_and_derives_savings_from_observation(tmp_path):
    adapter = TrackingAdapter()
    workflow, _ = make_workflow(tmp_path, adapter)
    demo = workflow.run()
    workflow.approve(demo.recommendation_id, "reviewer", "approver", "APPROVE", "Calculated evidence reviewed")
    result = workflow.apply_simulation(demo.recommendation_id, "reviewer")
    assert adapter.apply_calls == 1
    assert result.realized_monthly_savings_usd == round(
        adapter.cost_history("i-checkout-prod-01")[-1] - result.observed_post_state["monthly_cost_usd"], 2
    )


def test_below_threshold_cost_history_creates_no_incident(tmp_path):
    workflow, db = make_workflow(tmp_path, NoAnomalyAdapter())
    with pytest.raises(NoOptimizationOpportunity, match="thresholds not met"):
        workflow.run()
    assert db.fetch_all("SELECT * FROM incidents") == []


def test_shadow_result_and_selection_change_with_history(tmp_path):
    workflow, _ = make_workflow(tmp_path, LowPeakAdapter())
    demo = workflow.run()
    action = demo.rca.recommended_actions[0]
    peak = next(metric for metric in demo.validation.metrics if metric.name == "peak_cpu")
    assert action.parameters.plan_id == "static-cheapest"
    assert action.expected_monthly_savings_usd == 210.0
    assert peak.projected_value == 80.0
    assert demo.validation.blocked_alternative == {}


def test_post_change_breach_rolls_back_and_uses_observed_cost(tmp_path):
    adapter = BreachingObservationAdapter()
    workflow, db = make_workflow(tmp_path, adapter)
    demo = workflow.run()
    workflow.approve(demo.recommendation_id, "reviewer", "approver", "APPROVE", "Calculated shadow checks passed")
    result = workflow.apply_simulation(demo.recommendation_id, "reviewer")
    assert result.status == "ROLLED_BACK"
    assert result.realized_monthly_savings_usd == 0.0
    assert result.change_observation["source"] == "regression-breaching-observer"
    assert result.rollback_observation["source"] == "synthetic-post-rollback-billing-and-slo-observer"
    assert result.rollback_verified is True
    assert result.observed_post_state["instance_type"] == "m6i.2xlarge"
    assert result.observed_post_state["schedule_enabled"] is False
    assert adapter.observe_state("i-checkout-prod-01") == result.rollback_observation
    assert db.fetch_one("SELECT state FROM incidents")["state"] == "ROLLED_BACK"
    audit = db.fetch_one("SELECT decision,validation_result FROM audit_log ORDER BY id DESC LIMIT 1")
    assert audit == {"decision": "ROLLBACK_VERIFIED", "validation_result": "ROLLBACK_STATE_VERIFIED"}
    savings = db.fetch_one(
        "SELECT value,passed FROM evaluations WHERE metric='estimated_vs_realized_savings_accuracy' ORDER BY id DESC LIMIT 1"
    )
    assert savings["value"] == 0.0
    assert savings["passed"] == 0


def test_evaluation_scores_fail_for_unsafe_selection_and_bad_estimate():
    adapter = SyntheticAWSAdapter()
    resource = adapter.list_resources()[0]
    from backend.app.optimization import build_plans, detect_opportunity, simulate_plan

    history = adapter.workload_history(resource.id)
    detection = detect_opportunity(adapter.cost_history(resource.id), history)
    rates = adapter.rate_card(resource.id)
    simulations = [simulate_plan(plan, history, rates, resource) for plan in build_plans(resource, rates, history)]
    values = evaluation_values(
        detection, simulations, selected_plan_id="static-cheapest", selected_risk="MEDIUM",
        approved=True, estimated_savings=210.0, realized_savings=5.0,
    )
    assert values["unsafe_optimization_prevention"][0] == 0.0
    assert values["risk_classification_accuracy"][0] == 0.0
    assert values["estimated_vs_realized_savings_accuracy"][0] < 0.03
    mislabeled = evaluation_values(
        {**detection, "detected": False}, simulations, selected_plan_id="scheduled-elastic",
        selected_risk="MEDIUM", approved=False, estimated_savings=118.68, realized_savings=None,
    )
    assert mislabeled["cost_anomaly_detection"][0] == 0.0


def test_peak_day_is_derived_and_bound_to_simulation_and_terraform(tmp_path):
    workflow, _ = make_workflow(tmp_path, DayFifteenAdapter())
    demo = workflow.run()
    plan = demo.rca.recommended_actions[0].parameters
    assert plan.plan_id == "scheduled-elastic"
    assert plan.recurring_peak is True
    assert plan.peak_day == 15
    assert demo.validation.alignment_checks == {
        "simulation_plan_matches_selected_plan": True,
        "simulated_peak_day_matches_plan": True,
        "scheduled_requires_recurring_peak": True,
        "terraform_schedule_matches_plan": True,
        "selected_simulation_passed": True,
    }
    assert 'checkout_peak_schedule        = "0 2 15 * ? *"' in demo.terraform_proposal
    assert '0 2 28 * ? *' not in demo.terraform_proposal


def test_drifting_peaks_cannot_select_scheduled_elastic(tmp_path):
    workflow, _ = make_workflow(tmp_path, DriftingPeakAdapter())
    context = workflow._context()
    scheduled = next(item for item in context["simulations"] if item["plan"]["plan_id"] == "scheduled-elastic")
    assert scheduled["passed"] is False
    assert scheduled["checks"]["recurring_peak"] is False
    assert scheduled["projected_peak_cpu_pct"] > 90.0
    demo = workflow.run()
    selected = demo.rca.recommended_actions[0].parameters
    assert selected.plan_id != "scheduled-elastic"
    assert selected.scheduled is False
    assert "aws_scheduler_schedule" not in demo.terraform_proposal


def test_every_run_rechecks_thresholds_before_content_deduplication(tmp_path):
    workflow, db = make_workflow(tmp_path, SyntheticAWSAdapter())
    first = workflow.run()
    assert first.state.value == "AWAITING_APPROVAL"
    below_threshold = CountingNoAnomalyAdapter()
    workflow.adapter = below_threshold
    with pytest.raises(NoOptimizationOpportunity, match="thresholds not met"):
        workflow.run()
    assert below_threshold.cost_calls >= 1
    assert len(db.fetch_all("SELECT id FROM incidents")) == 1
