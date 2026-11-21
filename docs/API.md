# API Surface

All domain routes are versioned under `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness |
| GET | `/api/v1/readiness` | SQLite, adapter, and mock-mode readiness |
| GET | `/api/v1/metrics` | JSON operational metrics |
| GET | `/metrics` | Prometheus text metrics |
| GET | `/api/v1/agents` | Agent contracts and permissions |
| GET | `/api/v1/resources` | Synthetic resource inventory |
| GET | `/api/v1/demo` | Current typed investigation |
| POST | `/api/v1/demo/run` | Idempotently initialize/run the scenario |
| POST | `/api/v1/demo/reset` | Reset operational demo state; approver required |
| POST | `/api/v1/recommendations/{id}/approval` | Approve/reject validated proposal |
| POST | `/api/v1/recommendations/{id}/simulate-apply` | Simulation-only execution after approval |
| GET | `/api/v1/audit` | Immutable-style activity history |
| GET | `/api/v1/evaluations` | Objective evaluation rows |
| GET | `/api/v1/dashboard` | Aggregated dashboard view model |

FastAPI generates the authoritative schemas at `/openapi.json` and interactive documentation at `/docs`.
