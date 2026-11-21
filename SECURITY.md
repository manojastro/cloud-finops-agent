# Security

## Safety model

The application has no code path for a real cloud change. `SyntheticAWSAdapter` implements change, restore, and observation against process-local state only; `AWSAdapter` fails closed. Terraform is persisted and displayed as inert review text, and the executor’s only valid mode is the Pydantic literal `SIMULATION`. No arbitrary command or shell tool exists in the agent tool list.

The change sequence is enforced server-side:

`PROPOSE → RISK ASSESS → SHADOW VALIDATE → REQUEST APPROVAL → SIMULATE → VERIFY`

The safe proposal is `MEDIUM` risk because it touches a production checkout instance on a payment path. It includes a specific rollback trigger and procedure. The static cheapest proposal is `CRITICAL` and cannot be selected by the workflow.

## Controls implemented

- Bounded Pydantic inputs and enumerated approval decisions.
- Authentication/authorization seam using `X-Actor` and `X-Role`.
- `approver` or `admin` required for reset, decisions, and simulated apply.
- Passing validation required for approval; approval required for execution.
- A scheduled plan requires a recurring history-derived peak day, exact plan/replay/Terraform alignment, and persisted plan/artifact SHA-256 evidence.
- Approval recomputes that evidence and appends reviewer identity, role, recommendation/action/validation IDs, action hash, plan hash, and Terraform hash. Execution independently recomputes and compares every binding before calling the adapter.
- SQLite triggers reject changes or deletion of validation evidence and approval attestations, and reject action/Terraform changes or deletion once a validation exists.
- Legacy approvals without bindings remain unbound and migrate to `REAPPROVAL_REQUIRED`; only a new reviewer decision can authorize execution.
- A post-change breach invokes restore; `ROLLED_BACK` is emitted only after a separate observation verifies baseline instance, disabled schedule, and all SLOs. Otherwise the workflow fails closed as `ROLLBACK_FAILED`/`BLOCKED`.
- Current histories and thresholds are evaluated before identical-event de-duplication, so a cached incident cannot bypass a later below-threshold result.
- Fixed internal SQL and tool allow-lists; no user SQL or arbitrary shell.
- Explicit CORS origin allow-list and method/header restrictions.
- Per-process IP rate limiting for mutation endpoints.
- Immutable audit, validation, and approval records enforced by SQLite triggers. Demo reset advances a generation instead of deleting governed records.
- No credentials are required, logged, or stored; `.env` and databases are ignored.
- Non-root Docker user and container healthcheck.

## Production gaps

The header identity seam is intentionally not secure authentication. Production should use OIDC/JWT validation, short-lived identities, policy-as-code, separation of approver and executor duties, durable distributed rate limits, TLS, database encryption and backups, signed artifacts, tamper-evident external audit storage, and a separate deployment service with narrowly scoped cloud permissions.

## Secrets

`LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` are environment-only extension points. The implemented mock path makes zero LLM calls. Do not place credentials in source, requests, Terraform text, or demo data.

## Threat boundaries

- Browser input is untrusted and validated by the API.
- Synthetic adapter output is treated as evidence, not execution authority.
- An agent may recommend but cannot approve.
- An approver may authorize the simulation but cannot cause a real apply.
- IaC output is human-readable data, never executable input to this process.
