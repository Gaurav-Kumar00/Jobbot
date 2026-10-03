"""Storage layer. Callers depend on `Repository`; `open_repository` picks the backend."""

from __future__ import annotations

from jobbot.settings import Settings
from jobbot.storage.base import Repository, StaleVersionError
from jobbot.storage.memory import MemoryRepository
from jobbot.storage.mongo import MongoRepository, connect

__all__ = [
    "MemoryRepository",
    "MongoRepository",
    "Repository",
    "StaleVersionError",
    "open_repository",
]


def open_repository(settings: Settings) -> MongoRepository:
    client = connect(settings.secret("mongodb_uri"))
    return MongoRepository(client[settings.mongodb_db])
