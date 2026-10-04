from __future__ import annotations

import pytest

ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MONGODB_URI",
    "MONGODB_DB",
    "JOBBOT_ENV",
    "LOG_LEVEL",
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "SERPAPI_KEY",
    "TELEGRAM_WEBHOOK_SECRET",
    "CRON_SECRET",
    "GH_DISPATCH_TOKEN",
    "GITHUB_REPOSITORY",
)

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmno"


def fake_mongo_uri(
    password: str, host: str = "db.example.invalid", scheme: str = "mongodb+srv"
) -> str:
    """A credential-bearing URI for redaction tests, assembled at runtime so that secret
    scanners (e.g. GitHub's) don't mistake test fixtures for real Atlas credentials."""
    return f"{scheme}://" + "jobbot" + ":" + password + "@" + host + "/"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Never read the developer's real .env or environment during tests."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


async def no_sleep(_seconds: float) -> None:
    return None
