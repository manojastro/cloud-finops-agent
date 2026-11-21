# Limitations

- The inventory, billing export, workload history, dependency graph, SLO registry, price alternatives, and post-change outcome are synthetic.
- Shadow simulation is deterministic and calculated from historical profiles, synthetic rate-card capacity factors, and the evaluated proposal; it is not a capacity planner or load-testing replacement.
- The POC detects a compute anomaly/inefficiency and exposes an unattached-volume sample, but does not yet implement every detector listed in the broader roadmap.
- Real AWS collection is an interface seam only and deliberately fails closed.
- Terraform is a review artifact, not a complete provider-valid module or plan.
- Apply, independent observation, restore, and post-restore observation are simulated in process-local state. They prove control flow and final-outcome accounting, not real infrastructure recovery or a real load test. No cloud credential, SDK mutation, Terraform binary, or shell runner exists.
- Header-based identity, in-memory rate limiting, and SQLite are local demonstration choices.
- Audit, validation, approval, and validated-artifact triggers prevent the in-database update/delete paths exercised by the application; execution also rehashes current bytes. A database/file administrator can still replace the database or remove triggers, so production needs externally signed attestations and remote tamper-evident audit storage.
- Metrics cover process-local API observations; OpenTelemetry export and multi-process aggregation are future work.
- Mock mode uses deterministic synthesis and makes no LLM call. It does not exercise provider outage, malformed model output, latency, cost, or token behaviors beyond reporting zero calls/tokens.
- Estimated and realized savings exclude contracts, taxes, data transfer, licensing, and interruption costs.

These constraints are visible in the UI and readiness response so reviewers do not confuse a safe local POC with a production cloud control plane.
