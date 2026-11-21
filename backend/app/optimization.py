from __future__ import annotations

from statistics import mean
from typing import Any

from .adapters import CloudResource


HOURS_PER_MONTH = 730.0
COST_ANOMALY_THRESHOLD_PCT = 10.0
UNDERUTILIZED_CPU_THRESHOLD_PCT = 25.0


def detect_opportunity(cost_history: list[float], workload_history: list[dict]) -> dict[str, Any]:
    if len(cost_history) < 3 or not workload_history:
        return {"detected": False, "reason": "insufficient_history"}
    baseline_cost = mean(float(value) for value in cost_history[:-1])
    current_cost = float(cost_history[-1])
    cost_increase_pct = ((current_cost - baseline_cost) / baseline_cost * 100.0) if baseline_cost else 0.0
    average_cpu_pct = mean(float(point["average_cpu_pct"]) for point in workload_history)
    peak_cpu_pct = max(float(point["peak_cpu_pct"]) for point in workload_history)
    peak_days = [int(point["peak_day"]) for point in workload_history]
    recurring_peak = len(peak_days) >= 3 and len(set(peak_days)) == 1
    cost_anomaly = cost_increase_pct >= COST_ANOMALY_THRESHOLD_PCT
    underutilized = average_cpu_pct < UNDERUTILIZED_CPU_THRESHOLD_PCT
    return {
        "detected": cost_anomaly and underutilized,
        "cost_anomaly": cost_anomaly,
        "underutilized": underutilized,
        "cost_increase_pct": round(cost_increase_pct, 2),
        "baseline_cost_usd": round(baseline_cost, 2),
        "current_cost_usd": round(current_cost, 2),
        "average_cpu_pct": round(average_cpu_pct, 2),
        "peak_cpu_pct": round(peak_cpu_pct, 2),
        "recurring_peak": recurring_peak,
        "peak_day": peak_days[-1],
        "cost_threshold_pct": COST_ANOMALY_THRESHOLD_PCT,
        "utilization_threshold_pct": UNDERUTILIZED_CPU_THRESHOLD_PCT,
        "reason": "cost_anomaly_and_underutilization" if cost_anomaly and underutilized else "thresholds_not_met",
    }


def build_plans(
    resource: CloudResource,
    rates: dict[str, dict[str, float]],
    workload_history: list[dict],
) -> list[dict[str, Any]]:
    peak_hours = float(resource.peak_duration_hours)
    normal_hours = HOURS_PER_MONTH - peak_hours
    peak_days = [int(point["peak_day"]) for point in workload_history]
    recurring_peak = len(peak_days) >= 3 and len(set(peak_days)) == 1
    recurring_peak_day = peak_days[0] if recurring_peak else None

    def cost(normal_type: str, peak_type: str, scheduled: bool) -> float:
        if not scheduled:
            return rates[normal_type]["monthly_cost_usd"]
        return (
            normal_hours * rates[normal_type]["monthly_cost_usd"] / HOURS_PER_MONTH
            + peak_hours * rates[peak_type]["monthly_cost_usd"] / HOURS_PER_MONTH
        )

    definitions = [
        ("static-cheapest", "Static m6i.large (cheapest)", "m6i.large", "m6i.large", False),
        ("scheduled-elastic", "Scheduled elastic capacity", "m6i.xlarge", resource.instance_type, True),
        ("retain-current", "Retain current capacity", resource.instance_type, resource.instance_type, False),
    ]
    plans = []
    for plan_id, label, normal_type, peak_type, scheduled in definitions:
        monthly_cost = round(cost(normal_type, peak_type, scheduled), 2)
        plans.append({
            "plan_id": plan_id,
            "label": label,
            "normal_instance_type": normal_type,
            "peak_instance_type": peak_type,
            "normal_hours": normal_hours if scheduled else HOURS_PER_MONTH,
            "peak_hours": peak_hours if scheduled else 0.0,
            "scheduled": scheduled,
            "recurring_peak": recurring_peak,
            "peak_day": recurring_peak_day if scheduled else None,
            "projected_monthly_cost_usd": monthly_cost,
            "estimated_monthly_savings_usd": round(resource.monthly_cost_usd - monthly_cost, 2),
        })
    return plans


def simulate_plan(
    plan: dict[str, Any], workload_history: list[dict], rates: dict[str, dict[str, float]], resource: CloudResource
) -> dict[str, Any]:
    average_demand = mean(float(point["average_cpu_pct"]) for point in workload_history)
    peak_demand = max(float(point["peak_cpu_pct"]) for point in workload_history)
    observed_peak_days = sorted({int(point["peak_day"]) for point in workload_history})
    peak_day = plan.get("peak_day")
    schedule_is_grounded = (
        not plan["scheduled"]
        or (
            bool(plan.get("recurring_peak"))
            and peak_day is not None
            and observed_peak_days == [int(peak_day)]
        )
    )
    projected_days = []
    for day in range(1, 32):
        is_peak_day = day in observed_peak_days
        demand = peak_demand if is_peak_day else average_demand
        schedule_covers_peak = plan["scheduled"] and schedule_is_grounded and day == peak_day
        instance_type = plan["peak_instance_type"] if schedule_covers_peak else plan["normal_instance_type"]
        projected_days.append(demand / rates[instance_type]["capacity_factor"])
    projected_average = mean(projected_days)
    projected_peak = max(projected_days)
    overload = max(0.0, projected_peak - 70.0)
    latency = min(1000.0, 120.0 * (1.0 + (overload / 50.0) ** 1.6))
    error_rate = 0.18 + max(0.0, projected_peak - 90.0) * 0.008
    availability = 99.98 - max(0.0, projected_peak - 90.0) * 0.01
    checks = {
        "recurring_peak": schedule_is_grounded,
        "peak_cpu": projected_peak <= 90.0,
        "p95_latency": latency <= resource.slo_latency_ms,
        "error_rate": error_rate <= 1.0,
        "availability": availability >= resource.slo_availability_pct,
    }
    return {
        "plan": plan,
        "days_simulated": len(projected_days),
        "peak_day": peak_day,
        "observed_peak_days": observed_peak_days,
        "history_points": len(workload_history),
        "daily_projected_cpu_pct": [round(value, 2) for value in projected_days],
        "projected_average_cpu_pct": round(projected_average, 2),
        "projected_peak_cpu_pct": round(projected_peak, 2),
        "projected_p95_latency_ms": round(latency, 2),
        "projected_error_rate_pct": round(error_rate, 3),
        "projected_availability_pct": round(availability, 3),
        "checks": checks,
        "passed": all(checks.values()),
    }


def choose_plan(simulations: list[dict[str, Any]]) -> dict[str, Any]:
    safe = [item for item in simulations if item["passed"] and item["plan"]["estimated_monthly_savings_usd"] >= 0]
    if not safe:
        raise RuntimeError("No proposal passed all reliability guardrails")
    return min(safe, key=lambda item: item["plan"]["projected_monthly_cost_usd"])


def evaluation_values(
    detection: dict[str, Any], simulations: list[dict[str, Any]], selected_plan_id: str,
    selected_risk: str, approved: bool, estimated_savings: float, realized_savings: float | None,
) -> dict[str, tuple[float, float]]:
    threshold_label = bool(
        detection.get("cost_increase_pct", 0) >= detection.get("cost_threshold_pct", COST_ANOMALY_THRESHOLD_PCT)
        and detection.get("average_cpu_pct", 100) < detection.get("utilization_threshold_pct", UNDERUTILIZED_CPU_THRESHOLD_PCT)
    )
    unsafe = [item for item in simulations if not item["passed"]]
    prevented = [item for item in unsafe if item["plan"]["plan_id"] != selected_plan_id]
    selected = next(item for item in simulations if item["plan"]["plan_id"] == selected_plan_id)
    expected_risk = "CRITICAL" if not selected["passed"] else ("MEDIUM" if selected_plan_id != "retain-current" else "INFO")
    values = {
        "cost_anomaly_detection": (1.0 if bool(detection.get("detected")) == threshold_label else 0.0, 1.0),
        "unsafe_optimization_prevention": (len(prevented) / len(unsafe) if unsafe else 1.0, 1.0),
        "risk_classification_accuracy": (1.0 if selected_risk == expected_risk else 0.0, 1.0),
        "recommendation_acceptance": (1.0 if approved else 0.0, 0.8),
    }
    if realized_savings is not None:
        accuracy = 1.0 if estimated_savings == realized_savings == 0 else max(
            0.0, 1.0 - abs(estimated_savings - realized_savings) / max(abs(estimated_savings), 0.01)
        )
        values["estimated_vs_realized_savings_accuracy"] = (accuracy, 0.9)
    return values
