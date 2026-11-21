from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app


@pytest.fixture()
def client(tmp_path):
    app.state.database_path = tmp_path / "test-finops.db"
    with TestClient(app) as test_client:
        yield test_client
