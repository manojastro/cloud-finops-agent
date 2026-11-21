from __future__ import annotations

import json
import hashlib
from typing import Any
from uuid import uuid4

from .adapters import CloudProviderAdapter, CloudResource
from .database import Database, utc_now
from .models import Action, DemoRunResponse, Evidence, ExecutionResult, Hypothesis, OptimizationPlan, RCAResult
from .models import RiskLevel, ShadowMetric, ValidationResult, WorkflowState
from .optimization import build_plans, choose_plan, detect_opportunity, evaluation_values, simulate_plan


class NoOptimizationOpportunity(RuntimeError):
    pass


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, separators=(",", ":"), default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _plan_sha256(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


class FinOpsWorkflow:
    """Deterministic, evidence-first orchestration behind a replaceable agent seam."""

    def __init__(self, db: Database, adapter: CloudProviderAdapter):
        self.db = db
        self.adapter = adapter

    def _resource(self) -> CloudResource:
        return next(r for r in self.adapter.list_resources() if r.id == "i-checkout-prod-01")

    def _context(self) -> dict[str, Any]:
        resource = self._resource()
        cost_history = self.adapter.cost_history(resource.id)
        workload_history = self.adapter.workload_history(resource.id)
        rates = self.adapter.rate_card(resource.id)
        detection = detect_opportunity(cost_history, workload_history)
        plans = build_plans(resource, rates, workload_history) if workload_history and rates else []
        simulations = [simulate_plan(plan, workload_history, rates, resource) for plan in plans]
        return {
            "resource": resource,
            "cost_history": cost_history,
            "workload_history": workload_history,
            "rates": rates,
            "detection": detection,
            "simulations": simulations,
            "selected": choose_plan(simulations) if simulations else None,
        }

    def _event_ids(self, context: dict[str, Any]) -> tuple[str, str, str, str]:
        generation_row = self.db.fetch_one(
            "SELECT value FROM runtime_state WHERE key='demo_generation'"
        )
        payload = {
            "resource": context["resource"].as_dict(),
            "cost_history": context["cost_history"],
            "workload_history": context["workload_history"],
            "rates": context["rates"],
            "demo_generation": int(generation_row["value"]) if generation_row else 0,
        }
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        suffix = fingerprint[:12]
        return f"inc-{suffix}", f"rec-{suffix}", f"val-{suffix}", suffix

    @staticmethod
    def _evidence(
        resource: CloudResource,
        detection: dict[str, Any],
        history: list[dict],
        suffix: str,
    ) -> list[Evidence]:
        captured = utc_now()
        return [
            Evidence(id=f"ev-cost-{suffix}", kind="COST_ANOMALY", claim=f"Monthly compute cost increased above the {detection['cost_threshold_pct']:.0f}% alert threshold", observed_value=detection["cost_increase_pct"], unit="percent", source="synthetic-billing-export", captured_at=captured),
            Evidence(id=f"ev-average-{suffix}", kind="UTILIZATION", claim=f"Historical mean CPU is below the {detection['utilization_threshold_pct']:.0f}% underutilization threshold", observed_value=detection["average_cpu_pct"], unit="percent", source=f"synthetic-cloudwatch:{len(history)}-month-history", captured_at=captured),
            Evidence(id=f"ev-peak-{suffix}", kind="WORKLOAD_PATTERN", claim=(f"Historical workload shows a recurring day-{detection['peak_day']} monthly CPU peak" if detection["recurring_peak"] else "Historical workload peak days drift and are not safe for a fixed monthly schedule"), observed_value=detection["peak_cpu_pct"], unit="percent", source=f"synthetic-cloudwatch:{len(history)}-month-history", captured_at=captured),
            Evidence(id=f"ev-duration-{suffix}", kind="WORKLOAD_PATTERN", claim="The observed peak window affects the checkout SLO capacity model", observed_value=resource.peak_duration_hours, unit="hours", source="synthetic-service-profile", captured_at=captured),
            Evidence(id=f"ev-deps-{suffix}", kind="DEPENDENCY", claim="Checkout capacity is on the payment transaction path", observed_value=", ".join(resource.dependencies), source="synthetic-service-catalog", captured_at=captured),
            Evidence(id=f"ev-slo-{suffix}", kind="SLO", claim="Checkout p95 latency must stay within its production SLO", observed_value=resource.slo_latency_ms, unit="ms", source="synthetic-slo-registry", captured_at=captured),
        ]

    @staticmethod
    def _risk_for(selected: dict[str, Any]) -> RiskLevel:
        if not selected["passed"]:
            return RiskLevel.CRITICAL
        return RiskLevel.INFO if selected["plan"]["plan_id"] == "retain-current" else RiskLevel.MEDIUM

    def _rca(self, incident_id: str, evidence: list[Evidence], selected: dict[str, Any]) -> RCAResult:
        plan = selected["plan"]
        evidence_by_kind = {item.kind: item.id for item in evidence}
        workload_ids = [item.id for item in evidence if item.kind == "WORKLOAD_PATTERN"]
        description = (
            f"Run {plan['normal_instance_type']} during normal demand and restore {plan['peak_instance_type']} for the recurring settlement window."
            if plan["scheduled"] else f"Use {plan['normal_instance_type']} for the full month."
        )
        action = Action(
            action_type=("SCHEDULED_ELASTIC_RIGHTSIZE" if plan["scheduled"] else ("NO_CHANGE_RETAIN_CAPACITY" if plan["plan_id"] == "retain-current" else "STATIC_RIGHTSIZE")),
            description=description,
            risk=self._risk_for(selected),
            blast_radius="Single checkout-api production instance on the payment path",
            reversibility=f"Fully reversible by restoring the existing {self._resource().instance_type} instance type",
            expected_monthly_savings_usd=plan["estimated_monthly_savings_usd"],
            confidence=0.93,
            required_approval=True,
            rollback_plan="If p95 latency exceeds 250 ms, error rate exceeds 1%, or availability falls below 99.95%, restore m6i.2xlarge and disable the schedule.",
            parameters=OptimizationPlan.model_validate(plan),
        )
        return RCAResult(
            incident_id=incident_id,
            probable_root_cause=("Capacity is provisioned for a recurring monthly peak, leaving the instance underutilized during normal demand." if plan["recurring_peak"] else "Capacity is underutilized on average, but drifting peak days prevent a safe fixed downsizing schedule."),
            confidence=0.95 if evidence else 0.0,
            hypotheses=[
                Hypothesis(description="The instance can be statically moved to the cheapest size.", confidence=0.08, supporting_evidence_ids=[evidence_by_kind["UTILIZATION"], evidence_by_kind["WORKLOAD_PATTERN"]]),
                Hypothesis(description="Peak capacity must be preserved; scheduling is allowed only when the historical peak day recurs.", confidence=0.95, supporting_evidence_ids=[*workload_ids, evidence_by_kind["DEPENDENCY"], evidence_by_kind["SLO"]]),
            ],
            evidence=evidence,
            recommended_actions=[action],
        )

    @staticmethod
    def _terraform_proposal(plan: dict[str, Any] | OptimizationPlan) -> str:
        if isinstance(plan, OptimizationPlan):
            plan = plan.model_dump(mode="json")
        if plan["scheduled"]:
            if not plan.get("recurring_peak") or plan.get("peak_day") is None:
                raise RuntimeError("A scheduled plan requires an evidence-derived recurring peak day")
            change = f'''locals {{
  checkout_normal_instance_type = "{plan['normal_instance_type']}"
  checkout_peak_instance_type   = "{plan['peak_instance_type']}"
  checkout_peak_schedule        = "0 2 {plan['peak_day']} * ? *"
}}

# ~ aws_instance.checkout_api
#     instance_type = local.checkout_normal_instance_type

# + aws_scheduler_schedule.checkout_monthly_peak
#     schedule_expression = local.checkout_peak_schedule
#     target action: restore local.checkout_peak_instance_type before settlement'''
        else:
            change = f'''# ~ aws_instance.checkout_api
#     instance_type = "{plan['normal_instance_type']}"'''
        return f'''# REVIEW-ONLY ARTIFACT — never executed by this application
# Proposal selected after a calculated 31-day workload replay.
{change}

# Rollback: set instance_type back to "m6i.2xlarge" and disable any schedule.
'''

    @staticmethod
    def _shadow_validation(
        selected: dict[str, Any], simulations: list[dict[str, Any]], recommendation_id: str,
        validation_id: str, terraform: str,
    ) -> ValidationResult:
        plan = selected["plan"]
        metrics = [
            ShadowMetric(name="peak_cpu", projected_value=selected["projected_peak_cpu_pct"], threshold=90.0, comparison="lte", passed=selected["checks"]["peak_cpu"], unit="percent"),
            ShadowMetric(name="p95_latency", projected_value=selected["projected_p95_latency_ms"], threshold=250.0, comparison="lte", passed=selected["checks"]["p95_latency"], unit="ms"),
            ShadowMetric(name="error_rate", projected_value=selected["projected_error_rate_pct"], threshold=1.0, comparison="lte", passed=selected["checks"]["error_rate"], unit="percent"),
            ShadowMetric(name="availability", projected_value=selected["projected_availability_pct"], threshold=99.95, comparison="gte", passed=selected["checks"]["availability"], unit="percent"),
        ]
        cheapest = min(simulations, key=lambda item: item["plan"]["projected_monthly_cost_usd"])
        blocked: dict[str, Any] = {}
        if not cheapest["passed"]:
            failed_checks = [name for name, passed in cheapest["checks"].items() if not passed]
            blocked = {
                "option": cheapest["plan"]["label"],
                "estimated_monthly_savings_usd": cheapest["plan"]["estimated_monthly_savings_usd"],
                "risk": "CRITICAL",
                "projected_peak_cpu_pct": cheapest["projected_peak_cpu_pct"],
                "projected_p95_latency_ms": cheapest["projected_p95_latency_ms"],
                "reason": f"Fails calculated guardrails: {', '.join(failed_checks)}",
                "prevented": cheapest["plan"]["plan_id"] != selected["plan"]["plan_id"],
            }
        expected_schedule = f'checkout_peak_schedule        = "0 2 {plan["peak_day"]} * ? *"'
        alignment_checks = {
            "simulation_plan_matches_selected_plan": any(
                item["plan"] == plan and item["passed"] == selected["passed"] for item in simulations
            ),
            "simulated_peak_day_matches_plan": selected.get("peak_day") == plan.get("peak_day"),
            "scheduled_requires_recurring_peak": (
                not plan["scheduled"] or (bool(plan.get("recurring_peak")) and plan.get("peak_day") is not None)
            ),
            "terraform_schedule_matches_plan": (
                expected_schedule in terraform if plan["scheduled"] else "aws_scheduler_schedule" not in terraform
            ),
            "selected_simulation_passed": bool(selected["passed"]),
        }
        plan_sha256 = _plan_sha256(plan)
        terraform_sha256 = _sha256_text(terraform)
        return ValidationResult(
            validation_id=validation_id,
            recommendation_id=recommendation_id,
            scenario=f"31-day replay calculated from {selected['history_points']} historical monthly profiles",
            days_simulated=selected["days_simulated"],
            passed=all(metric.passed for metric in metrics) and all(alignment_checks.values()),
            metrics=metrics,
            alignment_checks=alignment_checks,
            plan_sha256=plan_sha256,
            terraform_sha256=terraform_sha256,
            blocked_alternative=blocked,
            summary=f"{plan['label']} passed calculated guardrails and exact plan/artifact alignment" + (f"; {cheapest['plan']['label']} was blocked as unsafe." if blocked else "."),
        )

    def run(self) -> DemoRunResponse:
        context = self._context()
        detection = context["detection"]
        if not detection["detected"]:
            raise NoOptimizationOpportunity(f"Optimization thresholds not met: {detection['reason']}")
        if context["selected"] is None:
            raise NoOptimizationOpportunity("No optimization plan passed the reliability guardrails")
        incident_id, recommendation_id, validation_id, suffix = self._event_ids(context)
        # Every call above re-collects current evidence and re-evaluates thresholds. Only
        # identical detected content is de-duplicated after detection.
        if self.db.fetch_one("SELECT id FROM incidents WHERE id=?", (incident_id,)):
            return self.get_demo(recommendation_id)
        resource: CloudResource = context["resource"]
        for discovered in self.adapter.list_resources():
            self.db.execute(
                "INSERT INTO resources(id,name,service,region,instance_type,monthly_cost,metadata_json) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,service=excluded.service,region=excluded.region,instance_type=excluded.instance_type,monthly_cost=excluded.monthly_cost,metadata_json=excluded.metadata_json",
                (discovered.id, discovered.name, discovered.service, discovered.region, discovered.instance_type, discovered.monthly_cost_usd, _json(discovered.as_dict())),
            )
        evidence = self._evidence(resource, detection, context["workload_history"], suffix)
        self.db.execute(
            "INSERT INTO incidents(id,kind,severity,resource_id,title,state,detected_at,rca_json) VALUES(?,?,?,?,?,?,?,?)",
            (incident_id, "COST_ANOMALY_AND_CAPACITY_INEFFICIENCY", "MEDIUM", resource.id, "Checkout cost anomaly with peak-capacity constraint", WorkflowState.INVESTIGATED.value, utc_now(), None),
        )
        for item in evidence:
            self.db.execute(
                "INSERT INTO evidence(id,incident_id,kind,claim,observed_value_json,unit,source,captured_at) VALUES(?,?,?,?,?,?,?,?)",
                (item.id, incident_id, item.kind, item.claim, _json(item.observed_value), item.unit, item.source, item.captured_at),
            )
        rca = self._rca(incident_id, evidence, context["selected"])
        plan = context["selected"]["plan"]
        terraform = self._terraform_proposal(plan)
        validation = self._shadow_validation(context["selected"], context["simulations"], recommendation_id, validation_id, terraform)
        action = rca.recommended_actions[0]
        if not validation.passed:
            raise RuntimeError("Exact plan, simulation, and Terraform artifact alignment validation failed")
        self.db.execute("UPDATE incidents SET rca_json=? WHERE id=?", (_json(rca), incident_id))
        self.db.execute(
            "INSERT INTO recommendations(id,incident_id,status,risk,action_json,estimated_savings,terraform_text,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (recommendation_id, incident_id, WorkflowState.AWAITING_APPROVAL.value, action.risk.value, _json(action), action.expected_monthly_savings_usd, terraform, utc_now()),
        )
        self.db.execute(
            "INSERT INTO validations(id,recommendation_id,passed,result_json,created_at) VALUES(?,?,?,?,?)",
            (validation.validation_id, recommendation_id, int(validation.passed), _json(validation), utc_now()),
        )
        self.db.execute("UPDATE incidents SET state=? WHERE id=?", (WorkflowState.AWAITING_APPROVAL.value, incident_id))
        self._record_evaluations(f"initial-{suffix}", detection, context["simulations"], plan["plan_id"], action.risk.value, False, action.expected_monthly_savings_usd, None)
        evidence_by_kind = {item.kind: item.id for item in evidence}
        workload_ids = [item.id for item in evidence if item.kind == "WORKLOAD_PATTERN"]
        audit_steps = [
            ("Cost Anomaly Agent", "cost_history", "detect_cost_anomaly", [evidence_by_kind["COST_ANOMALY"]], {"threshold_pct": detection["cost_threshold_pct"], "observed_pct": detection["cost_increase_pct"], "detected": detection["cost_anomaly"]}),
            ("Resource Analysis Agent", "list_resources", "inspect_utilization_dependencies", [evidence_by_kind["UTILIZATION"], evidence_by_kind["DEPENDENCY"]], {"historical_average_cpu_pct": detection["average_cpu_pct"], "underutilized": detection["underutilized"]}),
            ("Workload Pattern Agent", "workload_history", "detect_recurring_monthly_peak", workload_ids, {"history_points": len(context["workload_history"]), "peak_day": detection["peak_day"], "recurring": detection["recurring_peak"]}),
            ("Optimization Agent", "proposal_cost_model", "compare_capacity_options", [evidence_by_kind["UTILIZATION"], evidence_by_kind["WORKLOAD_PATTERN"]], {"evaluated_plans": len(context["simulations"]), "selected": plan["plan_id"]}),
            ("Reliability Risk Agent", "calculated_guardrails", "classify_change_risk", [evidence_by_kind["DEPENDENCY"], evidence_by_kind["SLO"]], {"selected_risk": action.risk.value, "unsafe_plans": sum(not item["passed"] for item in context["simulations"])}),
            ("IaC Agent", "allowlisted_template", "render_review_only_proposal", [], {"plan_id": plan["plan_id"], "peak_day": plan.get("peak_day"), "terraform_sha256": validation.terraform_sha256, "executed": False}),
            ("Validation Agent", "shadow_simulator", "replay_31_days", [evidence_by_kind["WORKLOAD_PATTERN"], evidence_by_kind["SLO"]], {"passed": validation.passed, "alignment_checks": validation.alignment_checks, "unsafe_alternative_prevented": bool(validation.blocked_alternative.get("prevented"))}),
        ]
        for agent, tool, action_name, evidence_ids, output in audit_steps:
            self.db.audit(incident_id=incident_id, agent=agent, tool=tool, action=action_name, input_data={"resource_id": resource.id}, output_data=output, evidence=evidence_ids, decision="CONTINUE" if output.get("passed", True) else "STOP", validation_result="PASSED" if agent == "Validation Agent" and validation.passed else None)
        self.db.audit(
            incident_id=incident_id, agent="FinOps Orchestrator", tool="synthetic_adapter + deterministic_rules",
            action="investigate_and_validate", input_data={"resource_id": resource.id, "history_points": len(context["workload_history"])},
            output_data={"recommendation_id": recommendation_id, "plan_id": plan["plan_id"], "peak_day": plan.get("peak_day"), "state": "AWAITING_APPROVAL"},
            evidence=[item.id for item in evidence], decision=f"PROPOSE_{action.action_type}", validation_result="PASSED",
        )
        return DemoRunResponse(incident_id=incident_id, recommendation_id=recommendation_id, state=WorkflowState.AWAITING_APPROVAL, rca=rca, validation=validation, terraform_proposal=terraform)

    def get_demo(self, recommendation_id: str | None = None) -> DemoRunResponse:
        if recommendation_id:
            recommendation = self.db.fetch_one("SELECT * FROM recommendations WHERE id=?", (recommendation_id,))
        else:
            recommendation = self.db.fetch_one("SELECT * FROM recommendations ORDER BY created_at DESC, id DESC LIMIT 1")
        incident = self.db.fetch_one("SELECT * FROM incidents WHERE id=?", (recommendation["incident_id"],)) if recommendation else None
        validation = self.db.fetch_one("SELECT * FROM validations WHERE recommendation_id=?", (recommendation["id"],)) if recommendation else None
        if not incident or not recommendation or not validation:
            raise LookupError("Demo has not been initialized")
        return DemoRunResponse(
            incident_id=incident["id"], recommendation_id=recommendation["id"],
            state=WorkflowState(incident["state"]), rca=RCAResult.model_validate_json(incident["rca_json"]),
            validation=ValidationResult.model_validate_json(validation["result_json"]), terraform_proposal=recommendation["terraform_text"],
        )

    def approve(self, recommendation_id: str, actor: str, role: str, decision: str, reason: str) -> dict[str, Any]:
        recommendation = self.db.fetch_one("SELECT * FROM recommendations WHERE id=?", (recommendation_id,))
        if not recommendation:
            raise LookupError("Recommendation not found")
        approvable_states = {
            WorkflowState.AWAITING_APPROVAL.value,
            WorkflowState.REAPPROVAL_REQUIRED.value,
        }
        if recommendation["status"] not in approvable_states:
            raise ValueError(f"Recommendation is {recommendation['status']}, not awaiting approval")
        validation_row = self.db.fetch_one(
            "SELECT * FROM validations WHERE recommendation_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (recommendation_id,),
        )
        binding = self._verified_artifact_binding(recommendation, validation_row)
        final_status = WorkflowState.APPROVED.value if decision == "APPROVE" else WorkflowState.REJECTED.value
        self.db.execute(
            """INSERT INTO approvals(
                 recommendation_id,actor,role,decision,reason,action_id,action_sha256,
                 validation_id,plan_sha256,terraform_sha256,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                recommendation_id, actor, role, decision, reason,
                binding["action_id"], binding["action_sha256"], binding["validation_id"],
                binding["plan_sha256"], binding["terraform_sha256"], utc_now(),
            ),
        )
        self.db.execute("UPDATE recommendations SET status=? WHERE id=?", (final_status, recommendation_id))
        self.db.execute("UPDATE incidents SET state=? WHERE id=?", (final_status, recommendation["incident_id"]))
        self.db.audit(
            incident_id=recommendation["incident_id"], agent="Human Approval Gate", tool="rbac + sha256_attestation",
            action="approval_decision",
            input_data={"actor": actor, "role": role, "reason": reason, "recommendation_id": recommendation_id},
            output_data={"status": final_status, "attestation": binding}, decision=decision, approval=final_status,
        )
        return {
            "recommendation_id": recommendation_id,
            "status": final_status,
            "actor": actor,
            "artifact_attestation": binding,
        }

    @staticmethod
    def _verified_artifact_binding(
        recommendation: dict[str, Any], validation_row: dict[str, Any] | None,
    ) -> dict[str, str]:
        if not validation_row or not validation_row["passed"]:
            raise ValueError("A passing shadow validation is required before approval")
        try:
            validation = ValidationResult.model_validate_json(validation_row["result_json"])
            action = Action.model_validate_json(recommendation["action_json"])
        except Exception as exc:
            raise ValueError("Validated recommendation artifacts are malformed") from exc
        if (
            not validation.passed
            or validation.validation_id != validation_row["id"]
            or validation.recommendation_id != recommendation["id"]
            or not all(validation.alignment_checks.values())
            or not all(metric.passed for metric in validation.metrics)
        ):
            raise ValueError("The stored validation does not attest to this recommendation")
        plan_hash = _plan_sha256(action.parameters.model_dump(mode="json"))
        terraform_hash = _sha256_text(recommendation["terraform_text"])
        if plan_hash != validation.plan_sha256 or terraform_hash != validation.terraform_sha256:
            raise ValueError("Current plan or Terraform bytes differ from the validated artifacts")
        return {
            "recommendation_id": recommendation["id"],
            "action_id": f"{recommendation['id']}:{action.action_type}",
            "action_sha256": _sha256_text(recommendation["action_json"]),
            "validation_id": validation.validation_id,
            "plan_sha256": plan_hash,
            "terraform_sha256": terraform_hash,
        }

    def _require_current_bound_approval(self, recommendation: dict[str, Any]) -> dict[str, str]:
        validation_row = self.db.fetch_one(
            "SELECT * FROM validations WHERE recommendation_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (recommendation["id"],),
        )
        binding = self._verified_artifact_binding(recommendation, validation_row)
        approval = self.db.fetch_one(
            """SELECT * FROM approvals WHERE recommendation_id=?
               ORDER BY id DESC LIMIT 1""",
            (recommendation["id"],),
        )
        if not approval or approval["decision"] != "APPROVE":
            raise ValueError("A current bound approval is required before simulated apply")
        if approval["role"] not in {"approver", "admin"} or not approval["actor"].strip():
            raise ValueError("The stored approval reviewer identity is not authorized")
        for field in (
            "recommendation_id", "action_id", "action_sha256", "validation_id",
            "plan_sha256", "terraform_sha256",
        ):
            if approval.get(field) != binding[field]:
                raise ValueError("Approval attestation does not match the current validated artifacts")
        return binding

    def apply_simulation(self, recommendation_id: str, actor: str) -> ExecutionResult:
        recommendation = self.db.fetch_one("SELECT * FROM recommendations WHERE id=?", (recommendation_id,))
        if not recommendation:
            raise LookupError("Recommendation not found")
        if recommendation["status"] != WorkflowState.APPROVED.value:
            raise ValueError("An approved recommendation is required before simulated apply")
        artifact_binding = self._require_current_bound_approval(recommendation)
        prior = self.db.fetch_one("SELECT result_json FROM executions WHERE recommendation_id=?", (recommendation_id,))
        if prior:
            return ExecutionResult.model_validate_json(prior["result_json"])

        action = Action.model_validate_json(recommendation["action_json"])
        plan = action.parameters.model_dump(mode="json")
        resource = self._resource()
        baseline_cost = float(self.adapter.cost_history(resource.id)[-1])
        change_controller_result = self.adapter.simulate_change(resource.id, plan)
        change_observation = self.adapter.observe_state(resource.id)
        checks = {
            "peak_cpu": float(change_observation["peak_cpu_pct"]) <= 90.0,
            "p95_latency": float(change_observation["p95_latency_ms"]) <= resource.slo_latency_ms,
            "error_rate": float(change_observation["error_rate_pct"]) <= 1.0,
            "availability": float(change_observation["availability_pct"]) >= resource.slo_availability_pct,
        }
        succeeded = all(checks.values())
        rollback_controller_result: dict[str, float | str | bool] = {}
        rollback_observation: dict[str, float | str | bool] = {}
        rollback_verified = False
        if succeeded:
            final_observation = change_observation
            execution_status = "SUCCEEDED"
            final_state = WorkflowState.VERIFIED.value
        else:
            rollback_controller_result = self.adapter.restore_change(
                resource.id,
                {"instance_type": resource.instance_type, "monthly_cost_usd": baseline_cost, "schedule_enabled": False},
            )
            rollback_observation = self.adapter.observe_state(resource.id)
            rollback_checks = {
                "peak_cpu": float(rollback_observation["peak_cpu_pct"]) <= 90.0,
                "p95_latency": float(rollback_observation["p95_latency_ms"]) <= resource.slo_latency_ms,
                "error_rate": float(rollback_observation["error_rate_pct"]) <= 1.0,
                "availability": float(rollback_observation["availability_pct"]) >= resource.slo_availability_pct,
                "instance_type": rollback_observation.get("instance_type") == resource.instance_type,
                "schedule_disabled": rollback_observation.get("schedule_enabled") is False,
            }
            rollback_verified = all(rollback_checks.values())
            final_observation = rollback_observation
            execution_status = "ROLLED_BACK" if rollback_verified else "BLOCKED"
            final_state = WorkflowState.ROLLED_BACK.value if rollback_verified else WorkflowState.ROLLBACK_FAILED.value
        realized_savings = round(baseline_cost - float(final_observation["monthly_cost_usd"]), 2)
        result = ExecutionResult(
            execution_id=f"exec-{uuid4().hex[:10]}", recommendation_id=recommendation_id,
            status=execution_status, realized_monthly_savings_usd=realized_savings,
            slo_impact=(
                f"No breach: p95 latency {change_observation['p95_latency_ms']} ms, error rate {change_observation['error_rate_pct']}%, availability {change_observation['availability_pct']}%"
                if succeeded else
                f"Change breached {', '.join(name for name, passed in checks.items() if not passed)}; "
                + ("baseline restore was independently observed and verified" if rollback_verified else "baseline restore could not be independently verified")
            ),
            rollback_available=True,
            message=(
                "Simulated selected change applied and independently observed; no cloud or Terraform command was executed."
                if succeeded else
                ("Post-change SLO breach triggered a simulated restore; an independent observation verified the baseline state."
                 if rollback_verified else "Post-change SLO breach triggered a simulated restore, but independent verification failed closed.")
            ),
            estimated_monthly_savings_usd=action.expected_monthly_savings_usd,
            observed_post_state=final_observation,
            change_observation=change_observation,
            rollback_observation=rollback_observation,
            rollback_verified=rollback_verified,
        )
        self.db.execute("INSERT INTO executions(id,recommendation_id,mode,status,realized_savings,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (result.execution_id, recommendation_id, result.mode, result.status, result.realized_monthly_savings_usd, _json(result), utc_now()))
        self.db.execute("UPDATE recommendations SET status=? WHERE id=?", (final_state, recommendation_id))
        self.db.execute("UPDATE incidents SET state=? WHERE id=?", (final_state, recommendation["incident_id"]))
        context = self._context()
        self._record_evaluations(result.execution_id, context["detection"], context["simulations"], plan["plan_id"], action.risk.value, True, action.expected_monthly_savings_usd, realized_savings)
        self.db.audit(
            incident_id=recommendation["incident_id"], agent="Savings Verification Agent", tool="synthetic_change_controller + independent_observer + rollback_controller",
            action="simulate_apply_and_verify", input_data={"recommendation_id": recommendation_id, "actor": actor, "plan": plan, "artifact_attestation": artifact_binding},
            output_data={**result.model_dump(mode="json"), "change_controller_result": change_controller_result, "rollback_controller_result": rollback_controller_result},
            decision="KEEP_CHANGE" if succeeded else ("ROLLBACK_VERIFIED" if rollback_verified else "ROLLBACK_FAILED_CLOSED"),
            execution_result=execution_status,
            validation_result="SLO_GUARDRAILS_PASSED" if succeeded else ("ROLLBACK_STATE_VERIFIED" if rollback_verified else "ROLLBACK_STATE_UNVERIFIED"),
        )
        return result

    def _record_evaluations(self, run_id: str, detection: dict[str, Any], simulations: list[dict[str, Any]], selected_plan_id: str, selected_risk: str, approved: bool, estimated_savings: float, realized_savings: float | None) -> None:
        values = evaluation_values(detection, simulations, selected_plan_id, selected_risk, approved, estimated_savings, realized_savings)
        for metric, (value, target) in values.items():
            self.db.execute("INSERT INTO evaluations(run_id,metric,value,target,passed,created_at) VALUES(?,?,?,?,?,?)", (run_id, metric, value, target, int(value >= target), utc_now()))

    def dashboard(self) -> dict[str, Any]:
        demo = self.run()
        resources = self.db.fetch_all("SELECT * FROM resources ORDER BY monthly_cost DESC")
        rec = self.db.fetch_one("SELECT * FROM recommendations WHERE id=?", (demo.recommendation_id,))
        execution = self.db.fetch_one("SELECT * FROM executions WHERE recommendation_id=? ORDER BY created_at DESC LIMIT 1", (demo.recommendation_id,))
        approval = self.db.fetch_one("SELECT * FROM approvals WHERE recommendation_id=? ORDER BY id DESC LIMIT 1", (demo.recommendation_id,))
        audits = self.db.fetch_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 12")
        evaluations = self.db.fetch_all("SELECT metric,value,target,passed,created_at FROM evaluations ORDER BY id DESC LIMIT 12")
        potential = float(demo.validation.blocked_alternative.get("estimated_monthly_savings_usd", rec["estimated_savings"]))
        risk_score = {"INFO": 10, "LOW": 30, "MEDIUM": 56, "HIGH": 78, "CRITICAL": 95}[rec["risk"]]
        return {
            "mode": "MOCK / SYNTHETIC", "cloud_spend_usd": round(sum(row["monthly_cost"] for row in resources), 2),
            "potential_savings_usd": potential, "validated_savings_usd": rec["estimated_savings"] if demo.validation.passed else 0.0,
            "realized_savings_usd": execution["realized_savings"] if execution else 0.0,
            "approval_queue": 1 if rec["status"] in {
                WorkflowState.AWAITING_APPROVAL.value,
                WorkflowState.REAPPROVAL_REQUIRED.value,
            } else 0,
            "risk_score": risk_score, "recommendation_status": rec["status"], "recommendation_id": demo.recommendation_id,
            "incident": {"id": demo.incident_id, "title": "Checkout cost anomaly with monthly peak", "state": demo.state.value},
            "resource": self._resource().as_dict(), "rca": demo.rca.model_dump(mode="json"),
            "validation": demo.validation.model_dump(mode="json"), "terraform_proposal": demo.terraform_proposal,
            "approval_attestation": ({
                "status": "BOUND" if approval.get("plan_sha256") else "REAPPROVAL_REQUIRED",
                "reviewer": approval["actor"], "role": approval["role"],
                "action_id": approval.get("action_id"), "validation_id": approval.get("validation_id"),
                "plan_sha256": approval.get("plan_sha256"), "terraform_sha256": approval.get("terraform_sha256"),
            } if approval else None),
            "audits": audits, "evaluations": evaluations,
        }
