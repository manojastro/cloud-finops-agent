# Operations

## Probes

- Liveness: `GET /api/v1/health`
- Readiness: `GET /api/v1/readiness`
- Metrics: `GET /metrics`

The Docker healthcheck uses liveness. Readiness additionally performs `SELECT 1` against the configured SQLite path.

## Persistence

Docker Compose mounts the named `finops-data` volume at `/app/data`. Local Python defaults to `data/finops.db`. WAL and sidecar files are expected while the service is running.

## Recovery

If the local database is corrupt, stop the service and preserve the database for investigation before creating a new file. A clean file is migrated and seeded during application lifespan startup. The API does not expose file deletion.

## Logs and metrics

Uvicorn emits access and application logs. The application collects request count, errors, average latency, workflow audit count, simulated execution count, LLM calls, and tokens. The latter two remain zero in mock mode.

## Ports

The service binds container and host port 8102 by default to remain independent of the other portfolio projects.
