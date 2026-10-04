"""Serverless web app (Vercel): Telegram webhook, hourly tick, health check."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jobbot.bot.commands import BotContext, handle
from jobbot.bot.github import dispatch_scan
from jobbot.bot.watchdog import tick
from jobbot.log import setup_logging
from jobbot.matching.preferences import load_profile
from jobbot.notify.base import OutgoingMessage
from jobbot.notify.telegram import TelegramClient, TelegramNotifier
from jobbot.settings import Settings
from jobbot.storage import open_repository
from jobbot.storage.base import Repository
from jobbot.timeutil import utcnow

setup_logging(Settings().log_level)
log = logging.getLogger(__name__)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_repo: Repository | None = None  # reused across warm serverless invocations


def get_repo(settings: Settings) -> Repository:
    global _repo
    if _repo is None:
        _repo = open_repository(settings)
    return _repo


def _secret_ok(given: str | None, expected: Any) -> bool:
    if expected is None or not expected.get_secret_value():
        return False  # never run unauthenticated
    return hmac.compare_digest((given or "").encode(), expected.get_secret_value().encode())


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/api/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    settings = Settings()
    if not _secret_ok(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token"), settings.telegram_webhook_secret
    ):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        update = await request.json()
    except ValueError:
        return JSONResponse({"ok": True})  # ignore junk; never make Telegram retry
    message = (update or {}).get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = message.get("text")
    if not text or chat_id != str(settings.telegram_chat_id):
        return JSONResponse({"ok": True})  # only the owner gets answers; others are ignored

    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        notifier = TelegramNotifier(client, chat_id)

        async def dispatch(force: bool) -> str:
            return await dispatch_scan(settings, force=force, trigger="telegram")

        ctx = BotContext(
            repo=get_repo(settings),
            notifier=notifier,
            dispatch_scan=dispatch,
            profile=load_profile(),
            now=utcnow(),
        )
        try:
            replies = await handle(text, ctx)
        except Exception:
            log.exception("command failed", extra={"command": text.split()[0][:40]})
            replies = [OutgoingMessage("⚠️ Something went wrong handling that. Try again shortly.")]
        for reply in replies:
            await notifier.send(reply)
    return JSONResponse({"ok": True})


@app.get("/api/tick")
async def hourly_tick(request: Request, key: str | None = None) -> JSONResponse:
    settings = Settings()
    if not _secret_ok(key or request.headers.get("X-Cron-Secret"), settings.cron_secret):
        return JSONResponse({"ok": False}, status_code=401)
    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        notifier = TelegramNotifier(client, settings.telegram_chat_id)

        async def dispatch(force: bool) -> str:
            return await dispatch_scan(settings, force=force, trigger="tick")

        result = await tick(get_repo(settings), notifier, dispatch, utcnow())
    return JSONResponse({"ok": True, **result})
