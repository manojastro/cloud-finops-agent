# Evaluation

## Ground-truth scenario

The checked-in synthetic scenario defines a known result: the checkout instance is underutilized on average, but has a repeatable day-28 monthly peak. Static downsizing to `m6i.large` is unsafe. Scheduled off-peak rightsizing is safe only when history has one recurring peak day and that exact day is bound into the typed plan, simulation, and Terraform artifact.

## Metrics

| Metric | Definition | Target | Initial demo |
|---|---|---:|---:|
| Cost anomaly detection | Threshold-derived label equals emitted detection | 1.00 | Calculated per run |
| Unsafe optimization prevention | Failing simulated plans not selected / failing plans | 1.00 | Calculated per run |
| Risk classification accuracy | Selected plan risk equals guardrail-derived risk | 1.00 | Calculated per run |
| Recommendation acceptance | Approved safe plans / proposed safe plans | ≥0.80 | 0 until approval, 1 after |
| Savings estimate accuracy | `1 - abs(estimated-realized)/estimated` | ≥0.90 | Calculated after observation |

Metrics are persisted rather than calculated only in the browser. Query `GET /api/v1/evaluations` after the demo. Historical rows distinguish the initial investigation from the post-execution run.

## SLO validation

The safe plan must simultaneously pass:

| Guardrail | Projected | Threshold |
|---|---:|---:|
| Peak CPU | 84% | ≤90% |
| p95 latency | 135.65 ms | ≤250 ms |
| Error rate | 0.18% | ≤1% |
| Availability | 99.98% | ≥99.95% |

The cheapest alternative calculates 336% capacity demand and a capped 1,000 ms p95 latency projection from the same workload history. That negative control confirms the selector prevents the unsafe option rather than approving every proposed saving. A separate stateful breaching-observer test proves the workflow calls restore, independently observes the restored baseline, persists both observations, reports `ROLLED_BACK`, and calculates savings accuracy against the final zero-savings state.

## Test coverage map

- Unit/tool: synthetic and disabled real adapter behavior, plus workload-history call tracking.
- Persistence: audit update and delete rejection.
- API: health, readiness, OpenAPI, metrics, typed validation errors.
- Agent/workflow: evidence linkage, RCA confidence, inert Terraform.
- Failure/security: unauthorized approval, pre-approval apply, rejected apply.
- Validation: unsafe alternative; day-15 typed plan/artifact alignment; drifting-peak schedule prevention; stateful restore and independent post-rollback observation.
- Freshness: a below-threshold second run after an earlier incident re-reads cost history and creates no new incident.
- Artifact integrity: exact approval attestations, day-15→day-28 joint-mutation rejection, direct database update/delete rejection, execution-time rehashing, and fail-closed legacy reapproval migration.
- Evaluation: formula-derived scores, including deliberately bad outcomes that must fail.

Run `python -m pytest`. Because all data and rules are deterministic, the suite is reproducible without network access or credentials.

## Interpretation caveat

This evaluates the application against one labeled POC scenario, not a statistically representative cloud estate. Reported values demonstrate measurable mechanics and safety gates; they are not claims of production model accuracy.
