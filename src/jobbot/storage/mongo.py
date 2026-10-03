"""MongoDB (Atlas M0) implementation of `Repository`."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, InsertOne, MongoClient, ReplaceOne, UpdateOne
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError

from jobbot.models import (
    AlertRecord,
    AlertStatus,
    AlertTrigger,
    Job,
    MatchResult,
    Normalized,
    PreferencesDoc,
    RunSummary,
    SourceState,
    UpsertOutcome,
)
from jobbot.storage.base import StaleVersionError
from jobbot.storage.memory import CONTENT_FIELDS
from jobbot.timeutil import utcnow

PREFERENCES_ID = "owner"
RUNS_TTL_SECONDS = 30 * 24 * 3600


def connect(uri: str, *, timeout_ms: int = 10_000) -> MongoClient:
    # tz_aware: get timezone-aware UTC datetimes back, matching our models.
    return MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=timeout_ms, appname="jobbot")


class MongoRepository:
    def __init__(self, db: Database, *, prefix: str = "") -> None:
        """`prefix` namespaces collections (used by tests to stay inside one database)."""
        self._db = db
        self.jobs = db[f"{prefix}jobs"]
        self.matches = db[f"{prefix}matches"]
        self.alerts = db[f"{prefix}alerts"]
        self.source_state = db[f"{prefix}source_state"]
        self.runs = db[f"{prefix}runs"]
        self.preferences = db[f"{prefix}preferences"]
        self.preference_history = db[f"{prefix}preference_history"]
        self.meta = db[f"{prefix}meta"]

    def collections(self) -> list[Any]:
        return [
            self.jobs,
            self.matches,
            self.alerts,
            self.source_state,
            self.runs,
            self.preferences,
            self.preference_history,
            self.meta,
        ]

    # --- lifecycle ---
    def ensure_indexes(self) -> None:
        self.jobs.create_index([("fingerprint", ASCENDING), ("first_seen_at", DESCENDING)])
        self.jobs.create_index(
            [("source", ASCENDING), ("company", ASCENDING), ("is_active", ASCENDING)]
        )
        self.jobs.create_index([("first_seen_at", DESCENDING)])
        self.jobs.create_index([("is_active", ASCENDING), ("last_seen_at", ASCENDING)])
        self.matches.create_index([("decision", ASCENDING), ("score", DESCENDING)])
        self.alerts.create_index([("status", ASCENDING), ("claimed_at", DESCENDING)])
        self.alerts.create_index([("claimed_at", DESCENDING)])
        self.alerts.create_index([("fingerprint", ASCENDING)])
        self.runs.create_index([("started_at", ASCENDING)], expireAfterSeconds=RUNS_TTL_SECONDS)
        self.preference_history.create_index([("updated_at", DESCENDING)])

    def ping(self) -> None:
        self._db.client.admin.command("ping")

    def storage_bytes(self) -> int:
        stats = self._db.command("dbStats")
        return int(stats.get("dataSize", 0)) + int(stats.get("indexSize", 0))

    # --- jobs ---
    def upsert_job(self, job: Job) -> UpsertOutcome:
        return self.upsert_jobs([job])[0]

    def upsert_jobs(self, jobs: list[Job]) -> list[UpsertOutcome]:
        if not jobs:
            return []
        ids = list({job.id for job in jobs})
        known = {
            d["_id"]: (None if d.get("compacted") else d.get("content_hash"))
            for d in self.jobs.find({"_id": {"$in": ids}}, {"content_hash": 1, "compacted": 1})
        }
        ops: list[Any] = []
        outcomes: list[UpsertOutcome] = []
        for job in jobs:
            if job.id not in known:
                known[job.id] = job.content_hash
                ops.append(InsertOne(_job_to_doc(job)))
                outcomes.append(UpsertOutcome.NEW)
                continue
            doc = _job_to_doc(job)
            updates: dict[str, Any] = {
                "last_seen_at": job.last_seen_at,
                "is_active": True,
                "normalized": doc["normalized"],
                "fingerprint": job.fingerprint,
            }
            outcome = UpsertOutcome.UNCHANGED
            if known[job.id] != job.content_hash:  # edited, or compacted and seen again
                updates.update({f: doc[f] for f in CONTENT_FIELDS})
                updates["compacted"] = False
                known[job.id] = job.content_hash
                outcome = UpsertOutcome.CHANGED
            ops.append(UpdateOne({"_id": job.id}, {"$set": updates}))
            outcomes.append(outcome)
        try:
            self.jobs.bulk_write(ops, ordered=False)
        except BulkWriteError as exc:  # a concurrent insert of the same id is harmless
            if any(err.get("code") != 11000 for err in exc.details.get("writeErrors", [])):
                raise
        return outcomes

    def get_jobs(self, job_ids: Iterable[str]) -> dict[str, Job]:
        ids = list(job_ids)
        return {d["_id"]: _doc_to_job(d) for d in self.jobs.find({"_id": {"$in": ids}})}

    def get_job(self, job_id: str) -> Job | None:
        doc = self.jobs.find_one({"_id": job_id})
        return _doc_to_job(doc) if doc else None

    def set_duplicate_of(self, job_id: str, original_id: str | None) -> None:
        self.jobs.update_one({"_id": job_id}, {"$set": {"duplicate_of": original_id}})

    def save_derived(self, job_id: str, normalized: Normalized, fingerprint: str) -> None:
        self.jobs.update_one(
            {"_id": job_id},
            {"$set": {"normalized": normalized.model_dump(), "fingerprint": fingerprint}},
        )

    def find_by_fingerprint(
        self, fingerprint: str, *, since: datetime, exclude_id: str | None = None
    ) -> list[Job]:
        query: dict[str, Any] = {"fingerprint": fingerprint, "first_seen_at": {"$gte": since}}
        if exclude_id:
            query["_id"] = {"$ne": exclude_id}
        return [_doc_to_job(d) for d in self.jobs.find(query).sort("first_seen_at", ASCENDING)]

    def list_jobs(
        self, *, active_only: bool = True, since: datetime | None = None, limit: int = 0
    ) -> list[Job]:
        query: dict[str, Any] = {}
        if active_only:
            query["is_active"] = True
        if since is not None:
            query["first_seen_at"] = {"$gte": since}
        cursor = self.jobs.find(query).sort("first_seen_at", DESCENDING).limit(limit)
        return [_doc_to_job(d) for d in cursor]

    def mark_missing_inactive(self, source: str, company: str, seen_ids: Iterable[str]) -> int:
        result = self.jobs.update_many(
            {
                "source": source,
                "company": company,
                "is_active": True,
                "_id": {"$nin": list(seen_ids)},
            },
            {"$set": {"is_active": False}},
        )
        return result.modified_count

    # --- housekeeping ---
    def stale_job_ids(
        self, *, inactive_before: datetime, include_compacted: bool = False
    ) -> list[str]:
        query: dict[str, Any] = {"is_active": False, "last_seen_at": {"$lt": inactive_before}}
        if not include_compacted:
            query["compacted"] = {"$ne": True}
        return [d["_id"] for d in self.jobs.find(query, {"_id": 1})]

    def compact_jobs(self, job_ids: Iterable[str]) -> int:
        ids = list(job_ids)
        if not ids:
            return 0
        result = self.jobs.update_many(
            {"_id": {"$in": ids}, "compacted": {"$ne": True}},
            {"$set": {"description": "", "raw": {}, "compacted": True}},
        )
        return result.modified_count

    def delete_jobs(self, job_ids: Iterable[str]) -> int:
        ids = list(job_ids)
        if not ids:
            return 0
        self.matches.delete_many({"_id": {"$in": ids}})
        return self.jobs.delete_many({"_id": {"$in": ids}}).deleted_count

    def get_meta(self, key: str) -> dict[str, Any] | None:
        doc = self.meta.find_one({"_id": key}, {"_id": 0})
        return doc.get("value") if doc else None

    def set_meta(self, key: str, value: dict[str, Any]) -> None:
        self.meta.replace_one({"_id": key}, {"_id": key, "value": value}, upsert=True)

    # --- matches ---
    def save_match(self, match: MatchResult) -> None:
        doc = match.model_dump()
        doc["_id"] = doc.pop("job_id")
        self.matches.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    def save_matches(self, matches: list[MatchResult]) -> None:
        if not matches:
            return
        ops = []
        for match in matches:
            doc = match.model_dump()
            doc["_id"] = doc.pop("job_id")
            ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
        self.matches.bulk_write(ops, ordered=False)

    def get_matches(self, job_ids: Iterable[str]) -> dict[str, MatchResult]:
        ids = list(job_ids)
        return {d["_id"]: _doc_to_match(d) for d in self.matches.find({"_id": {"$in": ids}})}

    def get_match(self, job_id: str) -> MatchResult | None:
        doc = self.matches.find_one({"_id": job_id})
        return _doc_to_match(doc) if doc else None

    def list_matches(
        self, *, decision: str | None = None, min_score: int | None = None, limit: int = 0
    ) -> list[MatchResult]:
        query: dict[str, Any] = {}
        if decision is not None:
            query["decision"] = decision
        if min_score is not None:
            query["score"] = {"$gte": min_score}
        cursor = self.matches.find(query).sort("score", DESCENDING).limit(limit)
        return [_doc_to_match(d) for d in cursor]

    # --- alerts ---
    def claim_alert(
        self,
        job_id: str,
        trigger: AlertTrigger,
        score: int | None,
        *,
        force: bool = False,
        fingerprint: str = "",
    ) -> bool:
        record = AlertRecord(
            job_id=job_id,
            status=AlertStatus.SENDING,
            trigger=trigger,
            score=score,
            fingerprint=fingerprint,
        )
        doc = record.model_dump(mode="python")
        doc["_id"] = doc.pop("job_id")
        try:
            self.alerts.insert_one(doc)
            return True
        except DuplicateKeyError:
            pass
        reclaimable = [AlertStatus.FAILED.value] + ([AlertStatus.SENT.value] if force else [])
        result = self.alerts.update_one(
            {"_id": job_id, "status": {"$in": reclaimable}},
            {
                "$set": {
                    "status": AlertStatus.SENDING.value,
                    "trigger": trigger.value,
                    "score": score,
                    **({"fingerprint": fingerprint} if fingerprint else {}),
                    "claimed_at": utcnow(),
                    "error": None,
                },
                "$inc": {"attempts": 1},
            },
        )
        return result.modified_count == 1

    def mark_alert_sent(self, job_id: str, message_id: str) -> None:
        self.alerts.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": AlertStatus.SENT.value,
                    "message_id": message_id,
                    "sent_at": utcnow(),
                    "error": None,
                }
            },
        )

    def mark_alert_failed(self, job_id: str, error: str) -> None:
        self.alerts.update_one(
            {"_id": job_id}, {"$set": {"status": AlertStatus.FAILED.value, "error": error}}
        )

    def get_alert(self, job_id: str) -> AlertRecord | None:
        doc = self.alerts.find_one({"_id": job_id})
        return _doc_to_alert(doc) if doc else None

    def suppress_alert(self, job_id: str, fingerprint: str = "") -> bool:
        record = AlertRecord(
            job_id=job_id,
            status=AlertStatus.BASELINE,
            trigger=AlertTrigger.NEW,
            fingerprint=fingerprint,
        )
        doc = record.model_dump(mode="python")
        doc["_id"] = doc.pop("job_id")
        try:
            self.alerts.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    def get_alerts(self, job_ids: Iterable[str]) -> dict[str, AlertRecord]:
        ids = list(job_ids)
        return {d["_id"]: _doc_to_alert(d) for d in self.alerts.find({"_id": {"$in": ids}})}

    def alerted_fingerprints(self, fingerprints: Iterable[str]) -> dict[str, str]:
        wanted = [fp for fp in set(fingerprints) if fp]
        if not wanted:
            return {}
        cursor = self.alerts.find(
            {
                "fingerprint": {"$in": wanted},
                "status": {
                    "$in": [
                        AlertStatus.SENT.value,
                        AlertStatus.SENDING.value,
                        AlertStatus.BASELINE.value,
                    ]
                },
            },
            {"fingerprint": 1},
        )
        return {d["fingerprint"]: d["_id"] for d in cursor}

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        since: datetime | None = None,
        limit: int = 0,
    ) -> list[AlertRecord]:
        query: dict[str, Any] = {}
        if status is not None:
            query["status"] = status.value
        if since is not None:
            query["claimed_at"] = {"$gte": since}
        cursor = self.alerts.find(query).sort("claimed_at", DESCENDING).limit(limit)
        return [_doc_to_alert(d) for d in cursor]

    # --- source health ---
    def get_source_state(self, key: str) -> SourceState | None:
        doc = self.source_state.find_one({"_id": key})
        return _doc_to_source_state(doc) if doc else None

    def save_source_state(self, state: SourceState) -> None:
        doc = state.model_dump()
        doc["_id"] = doc.pop("key")
        self.source_state.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    def list_source_states(self) -> list[SourceState]:
        return [_doc_to_source_state(d) for d in self.source_state.find().sort("_id", ASCENDING)]

    # --- runs ---
    def record_run(self, run: RunSummary) -> None:
        self.runs.insert_one(run.model_dump())

    def latest_runs(self, limit: int = 10) -> list[RunSummary]:
        cursor = self.runs.find({}, {"_id": 0}).sort("started_at", DESCENDING).limit(limit)
        return [RunSummary.model_validate(d) for d in cursor]

    # --- preferences ---
    def get_preferences(self) -> PreferencesDoc | None:
        doc = self.preferences.find_one({"_id": PREFERENCES_ID}, {"_id": 0})
        return PreferencesDoc.model_validate(doc) if doc else None

    def save_preferences(
        self, data: dict[str, Any], *, expected_version: int, note: str = ""
    ) -> PreferencesDoc:
        new = PreferencesDoc(version=expected_version + 1, data=dict(data))
        if expected_version == 0:
            try:
                self.preferences.insert_one({"_id": PREFERENCES_ID, **new.model_dump()})
            except DuplicateKeyError:
                raise StaleVersionError("preferences already exist") from None
        else:
            result = self.preferences.replace_one(
                {"_id": PREFERENCES_ID, "version": expected_version},
                {"_id": PREFERENCES_ID, **new.model_dump()},
            )
            if result.matched_count != 1:
                raise StaleVersionError(f"expected version {expected_version} is outdated")
        self.preference_history.insert_one({**new.model_dump(), "note": note})
        return new


def _job_to_doc(job: Job) -> dict[str, Any]:
    doc = job.model_dump()
    doc["_id"] = doc.pop("id")
    return doc


def _doc_to_job(doc: dict[str, Any]) -> Job:
    return Job.model_validate({**doc, "id": doc["_id"]})


def _doc_to_match(doc: dict[str, Any]) -> MatchResult:
    return MatchResult.model_validate({**doc, "job_id": doc["_id"]})


def _doc_to_alert(doc: dict[str, Any]) -> AlertRecord:
    return AlertRecord.model_validate({**doc, "job_id": doc["_id"]})


def _doc_to_source_state(doc: dict[str, Any]) -> SourceState:
    return SourceState.model_validate({**doc, "key": doc["_id"]})
