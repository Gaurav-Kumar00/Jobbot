"""Structured JSON logging with secret redaction.

GitHub Actions logs are public for this repo, so every log line passes through
`redact()` as a last line of defence against leaking tokens or DB passwords.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot tokens: <bot id>:<35-ish char secret>, also inside api URLs
    (re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),
    # Credentials embedded in connection strings: scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+@", re.IGNORECASE), r"\1<redacted>@"),
    # Generic bearer / api keys in headers or query strings
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{16,}=*"), r"\1<redacted>"),
    (re.compile(r"(?i)\b((?:api[_-]?key|app_key|token|secret)=)[^&\s]+"), r"\1<redacted>"),
]

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs (which contain the bot token) at INFO.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
