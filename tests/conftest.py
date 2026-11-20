"""Pytest fixtures: each test gets an isolated SQLite file and a fresh app."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

# Make the app package importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    tmp_dir = tempfile.mkdtemp(prefix="exhibit-test-")
    db_path = os.path.join(tmp_dir, "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    init_db(db_path)
    # The startup event would also call init_db(); with TestClient context
    # manager it runs against the same path, which is a harmless no-op.
    with TestClient(app) as test_client:
        yield test_client
