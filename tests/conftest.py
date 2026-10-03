from __future__ import annotations

import pytest

ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MONGODB_URI",
    "MONGODB_DB",
    "JOBBOT_ENV",
    "LOG_LEVEL",
)

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmno"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Never read the developer's real .env or environment during tests."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


async def no_sleep(_seconds: float) -> None:
    return None
