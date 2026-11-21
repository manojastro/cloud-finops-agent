from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY, value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, service TEXT NOT NULL, region TEXT NOT NULL,
  instance_type TEXT NOT NULL, monthly_cost REAL NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL, resource_id TEXT NOT NULL,
  title TEXT NOT NULL, state TEXT NOT NULL, detected_at TEXT NOT NULL, rca_json TEXT,
  FOREIGN KEY(resource_id) REFERENCES resources(id)
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, kind TEXT NOT NULL, claim TEXT NOT NULL,
  observed_value_json TEXT NOT NULL, unit TEXT, source TEXT NOT NULL, captured_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS recommendations (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, status TEXT NOT NULL, risk TEXT NOT NULL,
  action_json TEXT NOT NULL, estimated_savings REAL NOT NULL, terraform_text TEXT NOT NULL,
  created_at TEXT NOT NULL, FOREIGN KEY(incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS validations (
  id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL, passed INTEGER NOT NULL,
  result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recommendation_id TEXT NOT NULL, actor TEXT NOT NULL,
  role TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
  action_id TEXT, action_sha256 TEXT, validation_id TEXT,
  plan_sha256 TEXT, terraform_sha256 TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
);
CREATE TABLE IF NOT EXISTS executions (
  id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
  realized_savings REAL NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL NOT NULL, target REAL NOT NULL, passed INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, project TEXT NOT NULL,
  incident_id TEXT, agent TEXT NOT NULL, tool TEXT NOT NULL, action TEXT NOT NULL,
  input_json TEXT NOT NULL, output_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  decision TEXT, approval TEXT, execution_result TEXT, validation_result TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is immutable'); END;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(1,'initial_finops_schema',?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_state(key,value) VALUES('demo_generation',0)"
            )
            self._migrate_bound_approvals(connection)

    @staticmethod
    def _migrate_bound_approvals(connection: sqlite3.Connection) -> None:
        """Add fail-closed artifact attestations without blessing legacy approvals."""
        applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=2"
        ).fetchone()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        additions = {
            "action_id": "TEXT",
            "action_sha256": "TEXT",
            "validation_id": "TEXT",
            "plan_sha256": "TEXT",
            "terraform_sha256": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE approvals ADD COLUMN {name} {sql_type}")

        if not applied:
            # Old approval rows never attested to artifact bytes. Deliberately leave
            # their new binding columns NULL and require a fresh human decision.
            connection.execute(
                """UPDATE recommendations SET status='REAPPROVAL_REQUIRED'
                   WHERE status='APPROVED' AND EXISTS (
                     SELECT 1 FROM approvals a
                     WHERE a.recommendation_id=recommendations.id
                       AND a.decision='APPROVE'
                       AND (a.action_id IS NULL OR a.action_sha256 IS NULL
                            OR a.validation_id IS NULL OR a.plan_sha256 IS NULL
                            OR a.terraform_sha256 IS NULL)
                   )"""
            )
            connection.execute(
                """UPDATE incidents SET state='REAPPROVAL_REQUIRED'
                   WHERE id IN (
                     SELECT incident_id FROM recommendations
                     WHERE status='REAPPROVAL_REQUIRED'
                   )"""
            )
            connection.execute(
                "INSERT INTO schema_migrations(version,name,applied_at) VALUES(2,'bound_immutable_artifact_approvals',?)",
                (utc_now(),),
            )

        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS validations_no_update
            BEFORE UPDATE ON validations
            BEGIN SELECT RAISE(ABORT, 'validated artifact evidence is immutable'); END;

            CREATE TRIGGER IF NOT EXISTS validations_no_delete
            BEFORE DELETE ON validations
            BEGIN SELECT RAISE(ABORT, 'validated artifact evidence is immutable'); END;

            CREATE TRIGGER IF NOT EXISTS approvals_require_bindings
            BEFORE INSERT ON approvals
            WHEN NEW.action_id IS NULL OR length(trim(NEW.action_id))=0
              OR NEW.action_sha256 IS NULL OR length(NEW.action_sha256)<>64
              OR NEW.validation_id IS NULL OR length(trim(NEW.validation_id))=0
              OR NEW.plan_sha256 IS NULL OR length(NEW.plan_sha256)<>64
              OR NEW.terraform_sha256 IS NULL OR length(NEW.terraform_sha256)<>64
            BEGIN SELECT RAISE(ABORT, 'approval artifact bindings are required'); END;

            CREATE TRIGGER IF NOT EXISTS approvals_no_update
            BEFORE UPDATE ON approvals
            BEGIN SELECT RAISE(ABORT, 'approval attestations are immutable'); END;

            CREATE TRIGGER IF NOT EXISTS approvals_no_delete
            BEFORE DELETE ON approvals
            BEGIN SELECT RAISE(ABORT, 'approval attestations are immutable'); END;

            CREATE TRIGGER IF NOT EXISTS recommendation_artifacts_no_update
            BEFORE UPDATE OF action_json, terraform_text ON recommendations
            WHEN EXISTS (
              SELECT 1 FROM validations v WHERE v.recommendation_id=OLD.id
            )
            BEGIN SELECT RAISE(ABORT, 'validated recommendation artifacts are immutable'); END;

            CREATE TRIGGER IF NOT EXISTS recommendation_artifacts_no_delete
            BEFORE DELETE ON recommendations
            WHEN EXISTS (
              SELECT 1 FROM validations v WHERE v.recommendation_id=OLD.id
            ) OR EXISTS (
              SELECT 1 FROM approvals a WHERE a.recommendation_id=OLD.id
            )
            BEGIN SELECT RAISE(ABORT, 'validated recommendation artifacts are immutable'); END;
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as connection:
            connection.execute(sql, params)

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit(
        self,
        *,
        incident_id: str | None,
        agent: str,
        tool: str,
        action: str,
        input_data: Any,
        output_data: Any,
        evidence: Any = (),
        decision: str | None = None,
        approval: str | None = None,
        execution_result: str | None = None,
        validation_result: str | None = None,
    ) -> None:
        self.execute(
            """INSERT INTO audit_log(timestamp, project, incident_id, agent, tool, action,
               input_json, output_json, evidence_json, decision, approval, execution_result, validation_result)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                utc_now(), "02-cloud-finops-agent", incident_id, agent, tool, action,
                json.dumps(input_data, default=str), json.dumps(output_data, default=str),
                json.dumps(evidence, default=str), decision, approval, execution_result, validation_result,
            ),
        )

    def operational_reset(self) -> int:
        """Start a new demo generation without deleting governed history."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE runtime_state SET value=value+1 WHERE key='demo_generation'"
            )
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key='demo_generation'"
            ).fetchone()
            return int(row[0])
