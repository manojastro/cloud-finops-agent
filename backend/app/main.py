from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .adapters import SyntheticAWSAdapter
from .agents import AGENTS
from .config import PROJECT_DIR, settings
from .database import Database
from .models import ApprovalRequest, DemoRunResponse, ExecutionResult
from .security import InMemoryRateLimiter, actor_context, approver_context, rate_limit
from .workflow import FinOpsWorkflow


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(app.state.database_path)
    db.initialize()
    app.state.db = db
    app.state.workflow = FinOpsWorkflow(db, SyntheticAWSAdapter())
    app.state.rate_limiter = InMemoryRateLimiter(settings.rate_limit_per_minute)
    app.state.request_count = 0
    app.state.error_count = 0
    app.state.latency_total_ms = 0.0
    app.state.workflow.run()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Evidence-first local FinOps optimization with approval and SLO guardrails.",
    lifespan=lifespan,
)
app.state.database_path = settings.database_path
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Actor", "X-Role"],
)


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
        if response.status_code >= 500:
            request.app.state.error_count += 1
        return response
    except Exception:
        request.app.state.error_count += 1
        raise
    finally:
        request.app.state.request_count += 1
        request.app.state.latency_total_ms += (time.perf_counter() - started) * 1000


def workflow(request: Request) -> FinOpsWorkflow:
    return request.app.state.workflow


WorkflowDep = Annotated[FinOpsWorkflow, Depends(workflow)]
RateDep = Annotated[None, Depends(rate_limit)]


@app.get("/api/v1/health", tags=["operations"])
def health() -> dict:
    return {"status": "healthy", "service": "cloud-finops-agent", "version": "0.1.0"}


@app.get("/health", include_in_schema=False)
def health_alias() -> dict:
    return health()


@app.get("/api/v1/readiness", tags=["operations"])
def readiness(request: Request) -> dict:
    try:
        row = request.app.state.db.fetch_one("SELECT 1 AS ready")
        if not row:
            raise RuntimeError("database check returned no result")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ready", "database": "sqlite", "adapter": "SyntheticAWSAdapter", "llm_mode": settings.llm_mode}


@app.get("/ready", include_in_schema=False)
def readiness_alias(request: Request) -> dict:
    return readiness(request)


@app.get("/api/v1/metrics", tags=["operations"])
def metrics_json(request: Request) -> dict:
    count = request.app.state.request_count
    avg = request.app.state.latency_total_ms / count if count else 0
    db: Database = request.app.state.db
    return {
        "requests_total": count,
        "request_errors_total": request.app.state.error_count,
        "request_latency_average_ms": round(avg, 3),
        "workflow_state_transitions": len(db.fetch_all("SELECT id FROM audit_log")),
        "simulated_executions_total": len(db.fetch_all("SELECT id FROM executions")),
        "llm_calls_total": 0,
        "token_usage_total": 0,
    }


@app.get("/metrics", include_in_schema=False)
def metrics_prometheus(request: Request) -> Response:
    values = metrics_json(request)
    body = "\n".join(f"finops_{name} {value}" for name, value in values.items()) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.get("/api/v1/agents", tags=["governance"])
def agents() -> list[dict]:
    return [agent.model_dump(mode="json") for agent in AGENTS]


@app.get("/api/v1/resources", tags=["finops"])
def resources(request: Request) -> list[dict]:
    rows = request.app.state.db.fetch_all("SELECT * FROM resources ORDER BY monthly_cost DESC")
    return rows


@app.post("/api/v1/demo/run", response_model=DemoRunResponse, tags=["demo"])
def run_demo(_: RateDep, service: WorkflowDep) -> DemoRunResponse:
    return service.run()


@app.get("/api/v1/demo", response_model=DemoRunResponse, tags=["demo"])
def get_demo(service: WorkflowDep) -> DemoRunResponse:
    try:
        return service.get_demo()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/demo/reset", tags=["demo"])
def reset_demo(request: Request, _: RateDep, context: Annotated[tuple[str, str], Depends(approver_context)]) -> dict:
    actor, _role = context
    generation = request.app.state.db.operational_reset()
    request.app.state.workflow = FinOpsWorkflow(request.app.state.db, SyntheticAWSAdapter())
    result = request.app.state.workflow.run()
    request.app.state.db.audit(
        incident_id=result.incident_id, agent="Demo Operator", tool="database_allowlist",
        action="start_new_demo_generation", input_data={"actor": actor}, output_data={"state": result.state.value, "generation": generation},
    )
    return {"status": "reset", "incident_id": result.incident_id, "generation": generation}


@app.post("/api/v1/recommendations/{recommendation_id}/approval", tags=["governance"])
def approval(
    recommendation_id: str,
    body: ApprovalRequest,
    service: WorkflowDep,
    _: RateDep,
    context: Annotated[tuple[str, str], Depends(approver_context)],
) -> dict:
    actor, role = context
    try:
        return service.approve(recommendation_id, actor, role, body.decision, body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/recommendations/{recommendation_id}/simulate-apply", response_model=ExecutionResult, tags=["execution"])
def simulate_apply(
    recommendation_id: str,
    service: WorkflowDep,
    _: RateDep,
    context: Annotated[tuple[str, str], Depends(approver_context)],
) -> ExecutionResult:
    actor, _role = context
    try:
        return service.apply_simulation(recommendation_id, actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/audit", tags=["governance"])
def audit(request: Request, limit: int = 50) -> list[dict]:
    safe_limit = max(1, min(limit, 200))
    return request.app.state.db.fetch_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (safe_limit,))


@app.get("/api/v1/evaluations", tags=["evaluation"])
def evaluations(request: Request) -> list[dict]:
    return request.app.state.db.fetch_all("SELECT * FROM evaluations ORDER BY id DESC")


@app.get("/api/v1/dashboard", tags=["finops"])
def dashboard(service: WorkflowDep) -> dict:
    return service.dashboard()


FRONTEND_DIR = Path(PROJECT_DIR / "frontend")
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
