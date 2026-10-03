from __future__ import annotations

import json

import httpx
import respx

from jobbot.cli import main
from jobbot.settings import ConfigError
from tests.conftest import FAKE_TOKEN

BASE = f"https://api.telegram.org/bot{FAKE_TOKEN}"


def ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _configure(monkeypatch, chat_id: str = "42") -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)


@respx.mock
def test_ping_sends_message_to_owner_chat(monkeypatch, capsys):
    _configure(monkeypatch)
    respx.post(f"{BASE}/getMe").mock(return_value=ok({"id": 1, "username": "my_jobs_bot"}))
    send = respx.post(f"{BASE}/sendMessage").mock(return_value=ok({"message_id": 99}))

    assert main(["ping"]) == 0

    payload = json.loads(send.calls.last.request.content)
    assert payload["chat_id"] == "42"
    assert "JobBot is alive" in payload["text"]
    assert "@my_jobs_bot" in payload["text"]
    assert "message_id=99" in capsys.readouterr().out


def test_ping_without_config_exits_2(capsys):
    assert main(["ping"]) == 2
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err and "TELEGRAM_CHAT_ID" in err


@respx.mock
def test_ping_reports_telegram_error_without_token(monkeypatch, capsys):
    _configure(monkeypatch)
    respx.post(f"{BASE}/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    assert main(["ping"]) == 1
    captured = capsys.readouterr()
    assert "Unauthorized" in captured.err
    assert FAKE_TOKEN not in captured.err + captured.out


@respx.mock
def test_chat_id_lists_chats(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    respx.post(f"{BASE}/getUpdates").mock(
        return_value=ok(
            [
                {
                    "update_id": 1,
                    "message": {"chat": {"id": 555, "type": "private", "first_name": "G"}},
                },
                {
                    "update_id": 2,
                    "message": {"chat": {"id": 555, "type": "private", "first_name": "G"}},
                },
            ]
        )
    )
    assert main(["chat-id"]) == 0
    out = capsys.readouterr().out
    assert out.count("555") == 1


@respx.mock
def test_chat_id_with_no_updates(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    respx.post(f"{BASE}/getUpdates").mock(return_value=ok([]))
    assert main(["chat-id"]) == 1
    assert "Send any message" in capsys.readouterr().out


def test_db_init_without_uri_exits_2(capsys):
    assert main(["db-init"]) == 2
    assert "MONGODB_URI" in capsys.readouterr().err


def test_db_init_reports_mongo_errors_without_password(monkeypatch, capsys):
    from jobbot.storage import MongoRepository
    from jobbot.storage.mongo import connect

    uri = "mongodb://jobbot:hunter2@127.0.0.1:1/"
    monkeypatch.setenv("MONGODB_URI", uri)
    # Unreachable port + short timeout so the test fails fast.
    monkeypatch.setattr(
        "jobbot.cli.open_repository",
        lambda settings: MongoRepository(connect(uri, timeout_ms=200)["jobbot"]),
    )
    assert main(["db-init"]) == 1
    captured = capsys.readouterr()
    assert "MongoDB error" in captured.err
    assert "hunter2" not in captured.err + captured.out


GH_RAZORPAY = "https://boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs"


@respx.mock
def test_fetch_dry_run_prints_jobs(capsys):
    respx.get(GH_RAZORPAY).mock(
        return_value=ok_jobs(
            [
                {
                    "id": 7,
                    "title": "Backend Engineer",
                    "absolute_url": "https://x/7",
                    "location": {"name": "Bengaluru"},
                    "first_published": "2026-10-01T10:00:00Z",
                }
            ]
        )
    )
    assert main(["fetch", "greenhouse", "razorpay", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Razorpay (greenhouse:razorpaysoftwareprivatelimited): 1 jobs, 0 skipped" in out
    assert "2026-10-01  Bengaluru" in out and "Backend Engineer" in out


def test_fetch_unknown_company_exits_2(capsys):
    assert main(["fetch", "greenhouse", "not-configured", "--dry-run"]) == 2
    assert "--slug" in capsys.readouterr().err


@respx.mock
def test_fetch_missing_board_exits_1(capsys):
    respx.get("https://boards-api.greenhouse.io/v1/boards/ghost/jobs").mock(
        return_value=httpx.Response(404, json={"error": "Job not found"})
    )
    assert main(["fetch", "greenhouse", "ghost", "--slug", "--dry-run"]) == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_fetch_without_dry_run_requires_mongo(capsys):
    assert main(["fetch", "greenhouse", "razorpay"]) == 2
    assert "MONGODB_URI" in capsys.readouterr().err


def ok_jobs(jobs: list) -> httpx.Response:
    return httpx.Response(200, json={"jobs": jobs})


def test_scan_requires_mongo_and_telegram(capsys):
    assert main(["scan"]) == 2
    assert "MONGODB_URI" in capsys.readouterr().err


def test_scan_no_send_only_requires_mongo(monkeypatch, capsys):
    monkeypatch.setenv("MONGODB_URI", "mongodb://jobbot:pw@127.0.0.1:1/")
    monkeypatch.setattr(
        "jobbot.cli.open_repository",
        lambda settings: (_ for _ in ()).throw(ConfigError("stop here")),
    )
    assert main(["scan", "--no-send"]) == 2
    err = capsys.readouterr().err
    assert "stop here" in err and "TELEGRAM" not in err
