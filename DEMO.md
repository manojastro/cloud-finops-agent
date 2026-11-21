# Demonstration Runbook

## Start

```bash
docker compose up --build
```

Open <http://localhost:8102> and <http://localhost:8102/docs>. Confirm `GET /api/v1/readiness` reports `SyntheticAWSAdapter` and `llm_mode: mock`.

## Guided dashboard demo

1. Point out $320 observed cloud spend and $118.68 calculated validated savings.
2. Compare the 18% average CPU bar with the 84% day-28 peak.
3. Show why `Static m6i.large` is blocked despite offering $210 potential savings.
4. Open the four passing 31-day SLO guardrails plus the exact plan/replay/Terraform alignment checks.
5. Show the Terraform panel’s `REVIEW-ONLY` marker, workload-derived day-28 schedule, and rollback instruction.
6. Select **Approve safe proposal**. The dashboard shows the reviewer plus shortened immutable plan and Terraform SHA-256 attestations.
7. Select **Simulate apply**. The server rehashes the artifacts before calling the synthetic adapter. The default observation reports $160.99 post-change cost, so realized savings is calculated as $119.01 and status becomes `VERIFIED`.
8. Inspect `/api/v1/audit`, `/api/v1/evaluations`, and `/api/v1/metrics`.

## One-command API demo

With the service running:

```bash
python scripts/run_demo.py
```

Expected output includes:

```text
Unsafe option prevented: Static m6i.large (cheapest) -> Fails calculated guardrails: peak_cpu, p95_latency, error_rate, availability
Simulation: SUCCEEDED; realized savings $119.01/month
SLO outcome: No breach: p95 latency 138.65 ms, error rate 0.18%, availability 99.98%
```

## Manual API sequence

All mutating calls use the local identity abstraction. Read `recommendation_id` from the current response; IDs are content-derived rather than fixed:

```bash
curl -X POST http://localhost:8102/api/v1/demo/reset -H "X-Actor: reviewer" -H "X-Role: approver"
curl http://localhost:8102/api/v1/demo
curl -X POST http://localhost:8102/api/v1/recommendations/{recommendation_id}/approval -H "Content-Type: application/json" -H "X-Actor: reviewer" -H "X-Role: approver" -d '{"decision":"APPROVE","reason":"All shadow and alignment guardrails passed."}'
curl -X POST http://localhost:8102/api/v1/recommendations/{recommendation_id}/simulate-apply -H "X-Actor: reviewer" -H "X-Role: approver"
curl http://localhost:8102/api/v1/evaluations
```

## Safety demonstrations

- Retry approval with `X-Role: viewer`: returns 403.
- Reset, then call simulated apply before approval: returns 409.
- Reject instead of approve, then call simulated apply: returns 409.
- Inspect the Terraform string: the server never executes it.
- Try the `AWSAdapter` in a Python shell: it raises an explicit disabled-local error.
- Run the day-15 regression: the typed plan, replay, and Terraform cron all use day 15; no day-28 fallback remains.
- Run the drifting-peak regression: scheduled elasticity fails recurrence/alignment and cannot be selected.
- Run the breaching observer regression: restore is invoked, a separate observation verifies baseline state, and final realized savings is zero.
- Run the artifact-binding regressions: a day-15 plan and Terraform cannot be changed together to day 28 after validation; direct validation/approval tampering is rejected; removing an artifact trigger still cannot bypass execution-time rehashing.
- A migrated approval with no binding enters `REAPPROVAL_REQUIRED`; its NULL bindings remain visible in history and a fresh authorized review is required.

## Reset semantics

Reset advances a local demo generation and seeds a new scenario with a fresh synthetic adapter. It does not delete prior recommendations, validations, approvals, executions, evaluations, or audit evidence. Ordinary runs do not reuse cached incidents before detection: they collect current histories and re-evaluate thresholds, then de-duplicate only identical detected content within the current generation. Delete `data/finops.db` manually only when you explicitly want to discard all local history and the service is stopped.
