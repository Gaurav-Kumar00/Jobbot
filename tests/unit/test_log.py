from __future__ import annotations

import json
import logging

import pytest

from jobbot.log import JsonFormatter, redact, setup_logging
from tests.conftest import FAKE_TOKEN, fake_mongo_uri


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage", FAKE_TOKEN),
        (
            fake_mongo_uri("s3cr3t-P4ss", "cluster0.example.invalid") + "?retryWrites=true",
            "s3cr3t-P4ss",
        ),
        ("Authorization: Bearer gsk_abcdefghijklmnopqrstuvwxyz123456", "gsk_abcdefghijklmnop"),
        ("GET /v1/api/jobs/in/search/1?app_id=1&app_key=deadbeefcafe1234", "deadbeefcafe1234"),
    ],
)
def test_redact_removes_secrets(raw, secret):
    assert secret not in redact(raw)


def test_redact_leaves_ordinary_text_alone():
    text = "Fetched 21 jobs from greenhouse:razorpay in 1.2s (ratio 10:30)"
    assert redact(text) == text


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.makeLogRecord({"name": "jobbot.test", "levelname": "INFO", "msg": msg})
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_extra_fields_and_redacts():
    line = JsonFormatter().format(_record(f"token {FAKE_TOKEN}", source="greenhouse", jobs=21))
    payload = json.loads(line)
    assert payload["source"] == "greenhouse"
    assert payload["jobs"] == 21
    assert payload["level"] == "info"
    assert FAKE_TOKEN not in line


def test_json_formatter_redacts_exceptions():
    try:
        raise RuntimeError(f"boom at https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    except RuntimeError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()
    line = JsonFormatter().format(record)
    assert FAKE_TOKEN not in line
    assert "RuntimeError" in json.loads(line)["exc"]


def test_setup_logging_silences_httpx_url_logging():
    setup_logging("DEBUG")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger().level == logging.DEBUG
