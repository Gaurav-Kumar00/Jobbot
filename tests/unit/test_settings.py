from __future__ import annotations

import pytest

from jobbot.settings import ConfigError, Settings
from tests.conftest import FAKE_TOKEN, fake_mongo_uri


def test_loads_from_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    settings = Settings()
    assert settings.secret("telegram_bot_token") == FAKE_TOKEN
    assert settings.telegram_chat_id == "42"
    assert settings.mongodb_db == "jobbot"


def test_loads_from_dotenv_file(tmp_path):
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID=7\n")
    settings = Settings()
    assert settings.telegram_chat_id == "7"


def test_require_lists_all_missing_names():
    with pytest.raises(ConfigError) as exc:
        Settings().require("telegram_bot_token", "telegram_chat_id")
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)
    assert "TELEGRAM_CHAT_ID" in str(exc.value)


def test_empty_values_count_as_missing(monkeypatch):
    # .env.example ships with empty values; they must not pass validation.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "  ")
    with pytest.raises(ConfigError):
        Settings().require("telegram_bot_token", "telegram_chat_id")


def test_secrets_hidden_in_repr(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("MONGODB_URI", fake_mongo_uri("hunter2"))
    rendered = repr(Settings()) + str(Settings().model_dump())
    assert FAKE_TOKEN not in rendered
    assert "hunter2" not in rendered
