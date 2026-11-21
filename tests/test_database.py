import sqlite3

import pytest

from backend.app.database import Database


def test_audit_log_is_immutable(tmp_path):
    db = Database(tmp_path / "audit.db")
    db.initialize()
    db.audit(incident_id=None, agent="test", tool="test", action="test", input_data={}, output_data={})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE audit_log SET action='changed' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM audit_log WHERE id=1")
