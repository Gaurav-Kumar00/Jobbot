"""Runs every contract test against MemoryRepository and, when available, real MongoDB.

Mongo is enabled by either:
  * MONGODB_TEST_URI=<uri>      (CI uses a local mongo service container), or
  * JOBBOT_TEST_MONGO=1         (uses MONGODB_URI from the project's .env, e.g. Atlas).
Each test gets uniquely prefixed collections that are dropped afterwards, so tests
stay inside the `jobbot` database the Atlas user is allowed to write to.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from jobbot.settings import Settings
from jobbot.storage import MemoryRepository, MongoRepository
from jobbot.storage.mongo import connect

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _mongo_uri() -> str | None:
    if uri := os.environ.get("MONGODB_TEST_URI"):
        return uri
    if os.environ.get("JOBBOT_TEST_MONGO") == "1":
        settings = Settings(_env_file=PROJECT_ROOT / ".env")
        return settings.secret("mongodb_uri")
    return None


MONGO_URI = _mongo_uri()
MONGO_DB = os.environ.get("MONGODB_TEST_DB", "jobbot")


@pytest.fixture(scope="session")
def mongo_client():
    client = connect(MONGO_URI)
    yield client
    client.close()


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "mongo",
            marks=pytest.mark.skipif(
                MONGO_URI is None, reason="set MONGODB_TEST_URI or JOBBOT_TEST_MONGO=1"
            ),
        ),
    ]
)
def repo(request):
    if request.param == "memory":
        yield MemoryRepository()
        return
    client = request.getfixturevalue("mongo_client")
    repository = MongoRepository(client[MONGO_DB], prefix=f"test_{uuid.uuid4().hex[:10]}_")
    repository.ensure_indexes()
    try:
        yield repository
    finally:
        for collection in repository.collections():
            collection.drop()
