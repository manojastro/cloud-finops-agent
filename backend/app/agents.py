from __future__ import annotations

from .models import AgentDefinition


AGENTS = [
    AgentDefinition(
        name="Cost Anomaly Agent", responsibility="Detect deterministic spend deviations.",
        inputs=["cost history"], outputs=["anomaly incident"], tools=["cost_history"],
        permission_boundary="Read-only cloud telemetry.", failure_behavior="Records unavailable data and stops the workflow.",
    ),
    AgentDefinition(
        name="Resource Analysis Agent", responsibility="Inspect utilization and dependencies.",
        inputs=["resource inventory", "utilization"], outputs=["evidence"], tools=["list_resources"],
        permission_boundary="Read-only inventory.", failure_behavior="Makes no recommendation without resource evidence.",
    ),
    AgentDefinition(
        name="Workload Pattern Agent", responsibility="Identify recurring peaks hidden by averages.",
        inputs=["monthly workload history"], outputs=["peak pattern evidence"], tools=["workload_history"],
        permission_boundary="Read-only telemetry.", failure_behavior="Treats missing peak data as unsafe uncertainty.",
    ),
    AgentDefinition(
        name="Optimization Agent", responsibility="Compare costed capacity alternatives.",
        inputs=["evidence", "rate card"], outputs=["typed action"], tools=["deterministic_sizing"],
        permission_boundary="May propose only; cannot execute.", failure_behavior="Keeps current capacity when confidence is insufficient.",
    ),
    AgentDefinition(
        name="Reliability Risk Agent", responsibility="Score blast radius, reversibility, and SLO risk.",
        inputs=["candidate", "dependencies", "SLOs"], outputs=["risk classification"], tools=["risk_matrix"],
        permission_boundary="Analysis only.", failure_behavior="Escalates unknown risk to HIGH.",
    ),
    AgentDefinition(
        name="IaC Agent", responsibility="Render a reviewable Terraform proposal.",
        inputs=["approved plan"], outputs=["inert Terraform text"], tools=["allowlisted_template"],
        permission_boundary="Never runs Terraform or shell commands.", failure_behavior="Returns no artifact for unsupported actions.",
    ),
    AgentDefinition(
        name="Validation Agent", responsibility="Replay a 31-day shadow workload against SLO guardrails.",
        inputs=["proposal", "historical load", "SLOs"], outputs=["typed validation"], tools=["shadow_simulator"],
        permission_boundary="Simulation only.", failure_behavior="Fails closed on any missing guardrail.",
    ),
    AgentDefinition(
        name="Savings Verification Agent", responsibility="Compare estimated and simulated realized savings.",
        inputs=["execution", "baseline cost"], outputs=["evaluation metrics"], tools=["savings_calculator"],
        permission_boundary="Observes simulated outcome only.", failure_behavior="Reports zero validated savings without outcome data.",
    ),
]
