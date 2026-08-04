from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arxiv_expert.config import AppPaths
from arxiv_expert.database import Database


@pytest.fixture
def paths(tmp_path):
    return AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "logs")


@pytest.fixture
def db(paths):
    database = Database(paths.database_file)
    database.initialize()
    return database


@pytest.fixture
def created_at():
    return datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
