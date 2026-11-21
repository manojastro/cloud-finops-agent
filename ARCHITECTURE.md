# Architecture

## System architecture

```mermaid
flowchart TB
    subgraph Sources
      INV[Resource inventory]
      BILL[Cost history]
      LOAD[Workload history]
      SLO[Application SLOs]
      TF[Terraform address metadata]
    end
    subgraph Adapter[CloudProviderAdapter boundary]
      SYN[SyntheticAWSAdapter]
      REAL[AWSAdapter<br/>disabled locally]
    end
    subgraph Control[Evidence-first control plane]
      DET[Deterministic detection]
      ORCH[FinOpsWorkflow]
      RISK[Reliability risk]
      SHADOW[31-day shadow simulator]
      IAC[Allow-listed IaC template]
      GATE[Human approval gate]
      SIM[Simulation-only change controller]
      OBS[Independent state observer]
      RESTORE[Simulation-only restore controller]
      VERIFY[Final-state savings verification]
    end
    DB[(Project-local SQLite)]
    API[FastAPI /api/v1]
    UI[Strata dashboard]
    Sources --> SYN --> DET --> ORCH
    REAL -. enterprise seam .-> DET
    ORCH --> RISK --> SHADOW --> IAC --> GATE --> SIM --> OBS --> VERIFY
    OBS -->|SLO breach| RESTORE --> OBS
    Control <--> DB
    DB --> API --> UI
```

The `CloudProviderAdapter` separates synthetic change, restore, and independent observation operations. `SyntheticAWSAdapter` changes only process-local state; the real `AWSAdapter` implementation fails closed on every operation. The executor only emits an `ExecutionResult(mode="SIMULATION")`; it never parses or runs the Terraform artifact.

## Agent workflow

```mermaid
stateDiagram-v2
    [*] --> DETECTED: cost deviation > 10%
    DETECTED --> INVESTIGATED: utilization + load + dependencies
    INVESTIGATED --> VALIDATED: typed RCA and plan
    VALIDATED --> AWAITING_APPROVAL: all shadow guardrails pass
    AWAITING_APPROVAL --> REJECTED: human rejects
    AWAITING_APPROVAL --> APPROVED: approver binds exact hashes
    REAPPROVAL_REQUIRED --> APPROVED: new bound review
    APPROVED --> VERIFIED: change + independent observation passes
    APPROVED --> ROLLED_BACK: breach + restore + independent verification
    APPROVED --> ROLLBACK_FAILED: restored state cannot be verified
    REJECTED --> [*]
    VERIFIED --> [*]
    ROLLED_BACK --> [*]
    ROLLBACK_FAILED --> [*]
```

Agent contracts list responsibility, inputs, outputs, tools, permissions, and failure behavior in `backend/app/agents.py` and at `GET /api/v1/agents`. `FinOpsWorkflow` is the orchestration abstraction; a future LangGraph adapter can call the same deterministic tools and must preserve these state preconditions.

## Validation workflow

```mermaid
flowchart LR
    P[Optimization candidates] --> C{Candidate}
    C -->|Static m6i.large| R1[Replay monthly peak]
    R1 --> F[336% capacity demand<br/>1000 ms capped p95]
    F --> B[BLOCK / CRITICAL]
    C -->|Scheduled elastic| R2[Replay all 31 days]
    R2 --> P1{One recurring peak day?}
    P1 -->|no| B
    P1 -->|yes; bind day to plan + IaC| G1{CPU <= 90%}
    G1 --> G2{p95 <= 250 ms}
    G2 --> G3{errors <= 1%}
    G3 --> G4{availability >= 99.95%}
    G4 --> A[AWAITING APPROVAL]
```

Validation is fail-closed: every required guardrail and exact alignment check must exist and pass. The resulting `ValidationResult` persists the typed peak day, plan and Terraform SHA-256 values, projected values, thresholds, comparisons, units, and decision. Approval recomputes the current hashes, compares them to that validation, and appends an immutable reviewer/action/recommendation/validation attestation. Execution recomputes the same bindings before the simulation controller is called. Every run collects current histories and evaluates thresholds first; only an identical detected content fingerprint within the current demo generation is de-duplicated afterward.

## Persistence

SQLite tables cover resources, incidents, evidence, recommendations, validations, approvals, executions, evaluations, runtime state, and audit history. Foreign keys are enabled, WAL supports local concurrent reads, and the schema migration ledger records the initial schema plus bound approvals. Triggers make audit, validation, and approval records append-only and freeze recommendation action/Terraform bytes after validation. Version-1 approvals are not backfilled: active ones become `REAPPROVAL_REQUIRED`. Demo reset increments a generation and preserves governed history.

## Error handling

- Missing or partial evidence stops recommendation generation.
- The real AWS adapter fails with an explicit local-mode error.
- Readiness returns 503 if SQLite is unavailable.
- Invalid state transitions return HTTP 409.
- Missing records return 404; malformed inputs return 422.
- Authorization failure returns 403; rate limits return 429.
- No LLM availability can affect the deterministic workflow in mock mode.
