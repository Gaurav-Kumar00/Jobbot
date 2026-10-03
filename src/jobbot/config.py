"""Loading of versioned YAML config (company registry, defaults, taxonomies)."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from jobbot.models import CompanyTarget
from jobbot.settings import ConfigError

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def config_dir() -> Path:
    return Path(os.environ.get("JOBBOT_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def load_companies(
    path: Path | None = None, *, known_sources: Iterable[str] | None = None
) -> list[CompanyTarget]:
    path = path or config_dir() / "companies.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"company registry not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from None

    entries = data.get("companies") or []
    companies: list[CompanyTarget] = []
    seen: set[tuple[str, str]] = set()
    known = set(known_sources) if known_sources is not None else None
    for index, entry in enumerate(entries):
        try:
            company = CompanyTarget.model_validate(entry)
        except ValidationError as exc:
            raise ConfigError(f"{path.name} entry #{index + 1} is invalid: {exc}") from None
        if known is not None and company.ats not in known:
            raise ConfigError(f"{path.name}: {company.key} uses unknown ats {company.ats!r}")
        if (company.ats, company.key) in seen:
            raise ConfigError(f"{path.name}: duplicate key {company.key!r} for ats {company.ats}")
        seen.add((company.ats, company.key))
        companies.append(company)
    return companies


def find_company(companies: list[CompanyTarget], ats: str, key: str) -> CompanyTarget | None:
    for company in companies:
        if company.ats == ats and key in (company.key, company.slug):
            return company
    return None
