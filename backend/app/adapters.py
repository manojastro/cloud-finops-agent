from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CloudResource:
    id: str
    name: str
    service: str
    region: str
    instance_type: str
    monthly_cost_usd: float
    average_cpu_pct: float
    p95_cpu_pct: float
    monthly_peak_cpu_pct: float
    monthly_peak_day: int
    peak_duration_hours: int
    slo_latency_ms: float
    slo_availability_pct: float
    dependencies: tuple[str, ...]
    terraform_address: str

    def as_dict(self) -> dict:
        value = asdict(self)
        value["dependencies"] = list(self.dependencies)
        return value


class CloudProviderAdapter(ABC):
    """Evidence plus simulation-only change/restore/observe interface."""

    @abstractmethod
    def list_resources(self) -> list[CloudResource]: ...

    @abstractmethod
    def cost_history(self, resource_id: str) -> list[float]: ...

    @abstractmethod
    def workload_history(self, resource_id: str) -> list[dict]: ...

    @abstractmethod
    def rate_card(self, resource_id: str) -> dict[str, dict[str, float]]: ...

    @abstractmethod
    def simulate_change(self, resource_id: str, plan: dict) -> dict[str, float | str | bool]: ...

    @abstractmethod
    def restore_change(self, resource_id: str, baseline: dict) -> dict[str, float | str | bool]: ...

    @abstractmethod
    def observe_state(self, resource_id: str) -> dict[str, float | str | bool]: ...


class SyntheticAWSAdapter(CloudProviderAdapter):
    """Deterministic local dataset with a hidden-in-averages monthly peak."""

    def __init__(self) -> None:
        self._observed_states: dict[str, dict[str, float | str | bool]] = {}

    def list_resources(self) -> list[CloudResource]:
        return [
            CloudResource(
                id="i-checkout-prod-01",
                name="checkout-api-prod",
                service="EC2",
                region="us-east-1",
                instance_type="m6i.2xlarge",
                monthly_cost_usd=280.00,
                average_cpu_pct=18.0,
                p95_cpu_pct=41.0,
                monthly_peak_cpu_pct=84.0,
                monthly_peak_day=28,
                peak_duration_hours=8,
                slo_latency_ms=250.0,
                slo_availability_pct=99.95,
                dependencies=("orders-rds-prod", "payments-api", "redis-checkout"),
                terraform_address="aws_instance.checkout_api",
            ),
            CloudResource(
                id="vol-analytics-orphan",
                name="analytics-scratch-orphan",
                service="EBS",
                region="us-east-1",
                instance_type="gp3-500gb",
                monthly_cost_usd=40.00,
                average_cpu_pct=0.0,
                p95_cpu_pct=0.0,
                monthly_peak_cpu_pct=0.0,
                monthly_peak_day=0,
                peak_duration_hours=0,
                slo_latency_ms=0.0,
                slo_availability_pct=0.0,
                dependencies=(),
                terraform_address="aws_ebs_volume.analytics_scratch",
            ),
        ]

    def cost_history(self, resource_id: str) -> list[float]:
        if resource_id == "i-checkout-prod-01":
            return [244.0, 247.0, 249.0, 251.0, 280.0]
        return [40.0] * 5

    def workload_history(self, resource_id: str) -> list[dict]:
        if resource_id != "i-checkout-prod-01":
            return []
        # Five monthly observations; the peak is brief enough to disappear in averages.
        return [
            {"month": "2026-03", "average_cpu_pct": 17.0, "peak_cpu_pct": 79.0, "peak_day": 28},
            {"month": "2026-04", "average_cpu_pct": 18.0, "peak_cpu_pct": 81.0, "peak_day": 28},
            {"month": "2026-05", "average_cpu_pct": 17.5, "peak_cpu_pct": 82.0, "peak_day": 28},
            {"month": "2026-06", "average_cpu_pct": 18.5, "peak_cpu_pct": 83.0, "peak_day": 28},
            {"month": "2026-07", "average_cpu_pct": 19.0, "peak_cpu_pct": 84.0, "peak_day": 28},
        ]

    def rate_card(self, resource_id: str) -> dict[str, dict[str, float]]:
        if resource_id != "i-checkout-prod-01":
            return {}
        # Synthetic effective rates include the account's negotiated pricing.
        return {
            "m6i.2xlarge": {"monthly_cost_usd": 280.0, "capacity_factor": 1.0},
            "m6i.xlarge": {"monthly_cost_usd": 160.0, "capacity_factor": 0.5},
            "m6i.large": {"monthly_cost_usd": 70.0, "capacity_factor": 0.25},
        }

    def simulate_change(self, resource_id: str, plan: dict) -> dict[str, float | str | bool]:
        """Produce a post-change observation from the executed plan, not a canned outcome."""
        resource = next(item for item in self.list_resources() if item.id == resource_id)
        rates = self.rate_card(resource_id)
        history = self.workload_history(resource_id)
        if not history or plan["normal_instance_type"] not in rates or plan["peak_instance_type"] not in rates:
            raise RuntimeError("Post-change observation cannot be produced for this plan")
        # Observed peak window differs slightly from the eight-hour estimate.
        peak_hours = 6.0 if plan["plan_id"] == "scheduled-elastic" else float(plan["peak_hours"])
        normal_hours = 730.0 - peak_hours
        normal_rate = rates[plan["normal_instance_type"]]["monthly_cost_usd"] / 730.0
        peak_rate = rates[plan["peak_instance_type"]]["monthly_cost_usd"] / 730.0
        observed_cost = normal_hours * normal_rate + peak_hours * peak_rate
        peak_demand = max(float(point["peak_cpu_pct"]) for point in history)
        peak_capacity = rates[plan["peak_instance_type"]]["capacity_factor"]
        projected_peak = peak_demand / peak_capacity
        overload = max(0.0, projected_peak - 70.0)
        latency = min(1000.0, 120.0 * (1.0 + (overload / 50.0) ** 1.6)) + 3.0
        error_rate = 0.18 + max(0.0, projected_peak - 90.0) * 0.008
        availability = 99.98 - max(0.0, projected_peak - 90.0) * 0.01
        observation = {
            "source": "synthetic-post-change-billing-and-slo-observer",
            "monthly_cost_usd": round(observed_cost, 2),
            "peak_cpu_pct": round(projected_peak, 2),
            "p95_latency_ms": round(latency, 2),
            "error_rate_pct": round(error_rate, 3),
            "availability_pct": round(availability, 3),
            "normal_hours": normal_hours,
            "peak_hours": peak_hours,
            "instance_type": plan["normal_instance_type"],
            "schedule_peak_day": plan.get("peak_day") or 0,
        }
        self._observed_states[resource_id] = observation
        return dict(observation)

    def restore_change(self, resource_id: str, baseline: dict) -> dict[str, float | str | bool]:
        resource = next(item for item in self.list_resources() if item.id == resource_id)
        restored = {
            "source": "synthetic-rollback-controller",
            "restored": True,
            "instance_type": baseline.get("instance_type", resource.instance_type),
            "schedule_enabled": False,
        }
        self._observed_states[resource_id] = {
            "source": "synthetic-post-rollback-billing-and-slo-observer",
            "monthly_cost_usd": float(baseline.get("monthly_cost_usd", self.cost_history(resource_id)[-1])),
            "peak_cpu_pct": resource.monthly_peak_cpu_pct,
            "p95_latency_ms": 123.0,
            "error_rate_pct": 0.18,
            "availability_pct": 99.98,
            "instance_type": resource.instance_type,
            "schedule_enabled": False,
        }
        return restored

    def observe_state(self, resource_id: str) -> dict[str, float | str | bool]:
        if resource_id not in self._observed_states:
            self.restore_change(resource_id, {"instance_type": self.list_resources()[0].instance_type})
        return dict(self._observed_states[resource_id])


class AWSAdapter(CloudProviderAdapter):
    """Deliberately non-operational seam for a credentialed enterprise adapter."""

    def list_resources(self) -> list[CloudResource]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC; use SyntheticAWSAdapter")

    def cost_history(self, resource_id: str) -> list[float]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC")

    def workload_history(self, resource_id: str) -> list[dict]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC")

    def rate_card(self, resource_id: str) -> dict[str, dict[str, float]]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC")

    def simulate_change(self, resource_id: str, plan: dict) -> dict[str, float | str | bool]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC")

    def restore_change(self, resource_id: str, baseline: dict) -> dict[str, float | str | bool]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC")

    def observe_state(self, resource_id: str) -> dict[str, float | str | bool]:
        raise RuntimeError("AWSAdapter is not enabled in this local POC")
