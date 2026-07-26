from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test gets an empty in-memory database."""
    db.close()
    db._conn = None
    db.reset_cache()
    db.connect(":memory:")
    yield db
    db.close()
    db._conn = None
    db.reset_cache()
