"""Job preferences (editable at runtime) and the resume-backed skill profile."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from jobbot.config import config_dir
from jobbot.settings import ConfigError
from jobbot.storage.base import Repository


class Weights(BaseModel):
    """Maximum points per scoring component (sums to 100 by default)."""

    role: int = 25
    tech: int = 25
    experience: int = 20
    location: int = 10
    salary: int = 10
    profile: int = 10


class Preferences(BaseModel):
    """Everything the matcher needs to decide relevance. Stored independently of code."""

    paused: bool = False
    min_score: int = 55

    primary_titles: list[str] = Field(default_factory=list)
    secondary_titles: list[str] = Field(default_factory=list)
    excluded_categories: list[str] = Field(default_factory=list)
    excluded_seniority: list[str] = Field(default_factory=list)

    max_min_years: float = 2

    locations: list[str] = Field(default_factory=list)
    allow_remote: bool = True
    # Jobs whose location can't be resolved at all ("N/A", unfamiliar foreign city).
    allow_unknown_location: bool = False
    remote_bonus: int = 2

    target_base_lpa: float = 12
    floor_lpa: float = 10

    allowed_employment: list[str] = Field(default_factory=lambda: ["full_time"])

    # Exclude jobs whose stack names other backend languages but not Python.
    require_python: bool = True
    primary_skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)

    weights: Weights = Field(default_factory=Weights)


class Profile(BaseModel):
    skills: list[str] = Field(default_factory=list)


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"missing config file: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from None


def default_preferences(path: Path | None = None) -> Preferences:
    path = path or config_dir() / "defaults.yaml"
    try:
        return Preferences.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"{path.name} is invalid: {exc}") from None


def load_profile(path: Path | None = None) -> Profile:
    path = path or config_dir() / "profile.yaml"
    try:
        return Profile.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"{path.name} is invalid: {exc}") from None


def current_preferences(repo: Repository | None) -> tuple[Preferences, int]:
    """Live preferences and their version; falls back to defaults.yaml (version 0)."""
    doc = repo.get_preferences() if repo is not None else None
    if doc is None:
        return default_preferences(), 0
    # Validation fills defaults for fields added after the document was written.
    return Preferences.model_validate(doc.data), doc.version
