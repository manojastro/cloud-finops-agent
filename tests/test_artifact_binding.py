from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from backend.app.adapters import SyntheticAWSAdapter
from backend.app.database import Database
from backend.app.workflow import FinOpsWorkflow


class DayFifteenAdapter(SyntheticAWSAdapter):
    def workload_history(self, resource_id: str) -> list[dict]:
        return [{**point, "peak_day": 15} for point in super().workload_history(resource_id)]


def make_workflow(tmp_path, adapter=None):
    db = Database(tmp_path / "artifact-binding.db")
    db.initialize()
    return FinOpsWorkflow(db, adapter or SyntheticAWSAdapter()), db


def test_approval_attests_exact_validated_artifacts_and_reviewer(tmp_path):
    workflow, db = make_workflow(tmp_path)
    demo = workflow.run()
    response = workflow.approve(
        demo.recommendation_id, "finops-reviewer", "approver", "APPROVE",
        "Validated artifact hashes reviewed",
    )
    row = db.fetch_one("SELECT * FROM approvals WHERE recommendation_id=?", (demo.recommendation_id,))
    recommendation = db.fetch_one("SELECT * FROM recommendations WHERE id=?", (demo.recommendation_id,))
    action = json.loads(recommendation["action_json"])
    expected_plan_hash = hashlib.sha256(
        json.dumps(action["parameters"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected_terraform_hash = hashlib.sha256(recommendation["terraform_text"].encode()).hexdigest()

    assert row["actor"] == "finops-reviewer"
    assert row["role"] == "approver"
    assert row["recommendation_id"] == demo.recommendation_id
    assert row["action_id"] == f"{demo.recommendation_id}:{action['action_type']}"
    assert row["validation_id"] == demo.validation.validation_id
    assert row["plan_sha256"] == demo.validation.plan_sha256 == expected_plan_hash
    assert row["terraform_sha256"] == demo.validation.terraform_sha256 == expected_terraform_hash
    assert row["action_sha256"] == hashlib.sha256(recommendation["action_json"].encode()).hexdigest()
    assert response["artifact_attestation"]["plan_sha256"] == expected_plan_hash


def test_day15_to_day28_joint_mutation_after_validation_is_rejected(tmp_path):
    workflow, db = make_workflow(tmp_path, DayFifteenAdapter())
    demo = workflow.run()
    recommendation = db.fetch_one("SELECT * FROM recommendations WHERE id=?", (demo.recommendation_id,))
    action = json.loads(recommendation["action_json"])
    action["parameters"]["peak_day"] = 28
    changed_terraform = recommendation["terraform_text"].replace("0 2 15 * ? *", "0 2 28 * ? *")

    with pytest.raises(sqlite3.IntegrityError, match="validated recommendation artifacts are immutable"):
        db.execute(
            "UPDATE recommendations SET action_json=?, terraform_text=? WHERE id=?",
            (json.dumps(action, separators=(",", ":")), changed_terraform, demo.recommendation_id),
        )

    persisted = db.fetch_one("SELECT action_json,terraform_text FROM recommendations WHERE id=?", (demo.recommendation_id,))
    assert json.loads(persisted["action_json"])["parameters"]["peak_day"] == 15
    assert "0 2 15 * ? *" in persisted["terraform_text"]
    assert "0 2 28 * ? *" not in persisted["terraform_text"]


def test_direct_db_tamper_of_validation_and_approval_is_rejected(tmp_path):
    workflow, db = make_workflow(tmp_path)
    demo = workflow.run()
    workflow.approve(
        demo.recommendation_id, "reviewer", "approver", "APPROVE",
        "Validated artifacts reviewed",
    )

    with pytest.raises(sqlite3.IntegrityError, match="validated artifact evidence is immutable"):
        db.execute("UPDATE validations SET result_json='{}' WHERE recommendation_id=?", (demo.recommendation_id,))
    with pytest.raises(sqlite3.IntegrityError, match="approval attestations are immutable"):
        db.execute("UPDATE approvals SET plan_sha256=? WHERE recommendation_id=?", ("0" * 64, demo.recommendation_id))
    with pytest.raises(sqlite3.IntegrityError, match="approval attestations are immutable"):
        db.execute("DELETE FROM approvals WHERE recommendation_id=?", (demo.recommendation_id,))
    with pytest.raises(sqlite3.IntegrityError, match="validated recommendation artifacts are immutable"):
        db.execute("DELETE FROM recommendations WHERE id=?", (demo.recommendation_id,))


def test_approval_rehash_blocks_changed_terraform_if_storage_guard_is_removed(tmp_path):
    workflow, db = make_workflow(tmp_path)
    demo = workflow.run()
    recommendation = db.fetch_one("SELECT terraform_text FROM recommendations WHERE id=?", (demo.recommendation_id,))
    with db.connect() as connection:
        connection.execute("DROP TRIGGER recommendation_artifacts_no_update")
        connection.execute(
            "UPDATE recommendations SET terraform_text=? WHERE id=?",
            (recommendation["terraform_text"] + "\n# unauthorized mutation", demo.recommendation_id),
        )

    with pytest.raises(ValueError, match="differ from the validated artifacts"):
        workflow.approve(
            demo.recommendation_id, "reviewer", "approver", "APPROVE",
            "Attempted review of altered bytes",
        )
    assert db.fetch_all("SELECT * FROM approvals") == []


def test_execution_rehashes_current_action_even_if_db_trigger_is_removed(tmp_path):
    adapter = SyntheticAWSAdapter()
    workflow, db = make_workflow(tmp_path, adapter)
    demo = workflow.run()
    workflow.approve(
        demo.recommendation_id, "reviewer", "approver", "APPROVE",
        "Validated artifacts reviewed",
    )
    recommendation = db.fetch_one("SELECT action_json FROM recommendations WHERE id=?", (demo.recommendation_id,))
    action = json.loads(recommendation["action_json"])
    action["description"] = "Tampered after approval"
    with db.connect() as connection:
        connection.execute("DROP TRIGGER recommendation_artifacts_no_update")
        connection.execute(
            "UPDATE recommendations SET action_json=? WHERE id=?",
            (json.dumps(action, separators=(",", ":")), demo.recommendation_id),
        )

    with pytest.raises(ValueError, match="Approval attestation does not match"):
        workflow.apply_simulation(demo.recommendation_id, "executor")
    assert adapter._observed_states == {}
    assert db.fetch_all("SELECT * FROM executions") == []


def test_legacy_unbound_approval_requires_and_allows_new_bound_review(tmp_path):
    workflow, db = make_workflow(tmp_path)
    demo = workflow.run()
    workflow.approve(
        demo.recommendation_id, "legacy-reviewer", "approver", "APPROVE",
        "Original legacy-shaped review",
    )

    # Reproduce a version-1 database: approval existed, but no immutable binding
    # columns were populated. Migration must not infer or backfill their values.
    with db.connect() as connection:
        connection.executescript(
            """
            DROP TRIGGER approvals_no_update;
            DROP TRIGGER approvals_no_delete;
            DROP TRIGGER approvals_require_bindings;
            """
        )
        connection.execute(
            """UPDATE approvals SET action_id=NULL, action_sha256=NULL,
               validation_id=NULL, plan_sha256=NULL, terraform_sha256=NULL"""
        )
        connection.execute("DELETE FROM schema_migrations WHERE version=2")

    db.initialize()
    assert db.fetch_one("SELECT status FROM recommendations WHERE id=?", (demo.recommendation_id,))["status"] == "REAPPROVAL_REQUIRED"
    assert db.fetch_one("SELECT state FROM incidents WHERE id=?", (demo.incident_id,))["state"] == "REAPPROVAL_REQUIRED"
    legacy = db.fetch_one("SELECT * FROM approvals ORDER BY id LIMIT 1")
    assert legacy["plan_sha256"] is None
    assert legacy["terraform_sha256"] is None
    with pytest.raises(ValueError, match="approved recommendation"):
        workflow.apply_simulation(demo.recommendation_id, "executor")

    renewed = workflow.approve(
        demo.recommendation_id, "new-reviewer", "approver", "APPROVE",
        "Fresh bound review of exact validated bytes",
    )
    assert renewed["status"] == "APPROVED"
    latest = db.fetch_one("SELECT * FROM approvals ORDER BY id DESC LIMIT 1")
    assert latest["actor"] == "new-reviewer"
    assert latest["plan_sha256"] == demo.validation.plan_sha256
    assert latest["terraform_sha256"] == demo.validation.terraform_sha256
    assert len(db.fetch_all("SELECT id FROM approvals")) == 2


def test_reset_starts_new_generation_without_deleting_bound_history(tmp_path):
    workflow, db = make_workflow(tmp_path)
    first = workflow.run()
    workflow.approve(
        first.recommendation_id, "reviewer", "approver", "APPROVE",
        "Validated artifacts reviewed",
    )
    assert db.operational_reset() == 1

    second = FinOpsWorkflow(db, SyntheticAWSAdapter()).run()
    assert second.recommendation_id != first.recommendation_id
    assert len(db.fetch_all("SELECT id FROM recommendations")) == 2
    assert len(db.fetch_all("SELECT id FROM validations")) == 2
    assert len(db.fetch_all("SELECT id FROM approvals")) == 1
