"""Webhook app: authentication, owner-only replies, robustness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import jobbot.bot.app as app_module
from jobbot.models import RunSummary
from jobbot.notify.base import OutgoingMessage
from jobbot.storage import MemoryRepository
from tests.conftest import FAKE_TOKEN

OWNER = "111222333"  # fake chat id


class FakeTelegram:
    sent: list[tuple[str, OutgoingMessage]] = []

    def __init__(self, token: str, **kwargs) -> None:
        assert token == FAKE_TOKEN

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def send_message(self, chat_id, message, **kwargs):
        FakeTelegram.sent.append((str(chat_id), message))
        return {"message_id": len(FakeTelegram.sent)}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("MONGODB_URI", "unused-in-tests")
    repo = MemoryRepository()
    monkeypatch.setattr(app_module, "_repo", repo)
    monkeypatch.setattr(app_module, "TelegramClient", FakeTelegram)
    dispatched: list[bool] = []

    async def fake_dispatch(settings, *, force, trigger):
        dispatched.append(force)
        return "https://github.com/x/y/actions"

    monkeypatch.setattr(app_module, "dispatch_scan", fake_dispatch)

    async def nothing_running(settings):
        return False

    monkeypatch.setattr(app_module, "scan_in_flight", nothing_running)
    FakeTelegram.sent = []
    test_client = TestClient(app_module.app)
    test_client.repo = repo  # type: ignore[attr-defined]
    test_client.dispatched = dispatched  # type: ignore[attr-defined]
    return test_client


def update(text: str, chat_id: str = OWNER) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": int(chat_id)}, "text": text}}


HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


@pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}])
def test_webhook_rejects_missing_or_wrong_secret(client, headers):
    assert client.post("/api/telegram", json=update("/status"), headers=headers).status_code == 401
    assert FakeTelegram.sent == []


def test_webhook_refuses_to_run_without_a_configured_secret(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    assert client.post("/api/telegram", json=update("/status"), headers=HEADERS).status_code == 401


def test_owner_command_gets_a_reply(client):
    resp = client.post("/api/telegram", json=update("/status"), headers=HEADERS)
    assert resp.status_code == 200
    ((chat, message),) = FakeTelegram.sent
    assert chat == OWNER and "JobBot status" in message.text


def test_strangers_are_ignored(client):
    resp = client.post("/api/telegram", json=update("/status", chat_id="999"), headers=HEADERS)
    assert resp.status_code == 200 and FakeTelegram.sent == []


@pytest.mark.parametrize(
    "body", [{"update_id": 1}, {"update_id": 2, "edited_message": {}}, {"message": {"chat": {}}}]
)
def test_non_text_updates_are_acknowledged(client, body):
    assert client.post("/api/telegram", json=body, headers=HEADERS).status_code == 200
    assert FakeTelegram.sent == []


def test_invalid_json_is_acknowledged(client):
    resp = client.post("/api/telegram", content=b"not json", headers=HEADERS)
    assert resp.status_code == 200


def test_handler_crash_returns_200_and_apologises(client, monkeypatch):
    async def boom(text, ctx):
        raise RuntimeError("bug")

    monkeypatch.setattr(app_module, "handle", boom)
    resp = client.post("/api/telegram", json=update("/status"), headers=HEADERS)
    assert resp.status_code == 200  # never make Telegram retry-storm us
    assert "Something went wrong" in FakeTelegram.sent[0][1].text


def test_scan_now_dispatches(client):
    client.post("/api/telegram", json=update("/scan_now"), headers=HEADERS)
    assert client.dispatched == [True]


def test_tick_requires_secret(client):
    assert client.get("/api/tick").status_code == 401
    assert client.get("/api/tick?key=nope").status_code == 401


def test_tick_dispatches_when_scans_are_overdue(client):
    resp = client.get("/api/tick?key=cron-secret").json()
    assert resp["ok"] and resp["dispatched"] and client.dispatched == [False]
    assert resp["stale"] is True
    assert "No successful scan" in FakeTelegram.sent[0][1].text


def test_tick_does_nothing_when_a_scan_just_ran(client):
    now = datetime.now(UTC)
    client.repo.record_run(
        RunSummary(
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=8),
            stats={"sources_ok": 5},
        )
    )
    resp = client.get("/api/tick", headers={"X-Cron-Secret": "cron-secret"}).json()
    assert resp["dispatched"] is False and resp["stale"] is False
    assert client.dispatched == [] and FakeTelegram.sent == []
