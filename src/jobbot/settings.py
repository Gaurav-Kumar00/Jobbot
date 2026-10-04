"""Runtime configuration loaded from environment variables (and `.env` locally).

Secrets are typed as SecretStr so they never appear in reprs, logs or tracebacks.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    mongodb_uri: SecretStr | None = None
    mongodb_db: str = "jobbot"

    # Optional aggregator keys (Phase 9). Missing keys only disable that source.
    adzuna_app_id: str | None = None
    adzuna_app_key: SecretStr | None = None
    serpapi_key: SecretStr | None = None

    # Bot webhook / hourly tick (Phase 8)
    telegram_webhook_secret: SecretStr | None = None  # Telegram echoes it in a header
    cron_secret: SecretStr | None = None  # required by /api/tick
    gh_dispatch_token: SecretStr | None = None  # fine-grained PAT: Actions read & write
    github_repository: str = "Gaurav-Kumar00/Jobbot"

    jobbot_env: str = "dev"
    log_level: str = "INFO"

    def require(self, *names: str) -> None:
        """Raise ConfigError naming every missing setting (empty strings count as missing)."""
        missing = [name for name in names if not _has_value(getattr(self, name))]
        if missing:
            env_names = ", ".join(name.upper() for name in missing)
            raise ConfigError(
                f"missing required setting(s): {env_names}. "
                "Set them in .env for local runs, or in GitHub Secrets / Vercel env in production."
            )

    def secret(self, name: str) -> str:
        """Return a required secret's plain value."""
        self.require(name)
        return getattr(self, name).get_secret_value()


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value().strip())
    return bool(str(value).strip())
