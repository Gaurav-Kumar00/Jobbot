"""Vocabulary (config/*.yaml) and a phrase matcher used by the normalisers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from jobbot.config import config_dir


class PhraseMatcher:
    """Finds canonical names for any of their aliases in text.

    Case-insensitive; aliases must not be glued to letters/digits on either side,
    so "java" doesn't match "javascript" and "c++" / "node.js" / "ci/cd" still work.
    At each position the longest alias wins ("greater noida" over "noida").
    """

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._canonical: dict[str, str] = {}
        for canonical, aliases in mapping.items():
            for alias in [*aliases]:
                self._canonical[alias.lower().strip()] = canonical
        ordered = sorted(self._canonical, key=len, reverse=True)
        alternation = "|".join(re.escape(alias) for alias in ordered)
        self._pattern = (
            re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])") if ordered else None
        )

    def find(self, text: str) -> list[str]:
        """Canonical names found, in order of first appearance, without duplicates."""
        return list(dict.fromkeys(canonical for canonical, _ in self.find_with_spans(text)))

    def find_with_spans(self, text: str) -> list[tuple[str, tuple[int, int]]]:
        if not self._pattern or not text:
            return []
        lowered = text.lower()
        return [(self._canonical[m.group(0)], m.span()) for m in self._pattern.finditer(lowered)]


@dataclass(frozen=True)
class LocationVocab:
    cities: PhraseMatcher
    region_aliases: PhraseMatcher
    city_regions: dict[str, list[str]]  # city -> regions containing it
    region_cities: dict[str, list[str]]
    countries: PhraseMatcher
    indian_states: PhraseMatcher
    foreign_cities: PhraseMatcher
    remote_regions: PhraseMatcher


def _load_yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((config_dir() / name).read_text(encoding="utf-8")) or {}
    _require_string_keys(data, name)
    return data


def _require_string_keys(node: Any, where: str) -> None:
    """Fail loudly on YAML traps like an unquoted `NO:` (Norway) parsing as False."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise ValueError(f"{where}: key {key!r} is not a string; quote it in the YAML")
            _require_string_keys(value, f"{where}.{key}")
    elif isinstance(node, list):
        for value in node:
            _require_string_keys(value, where)


@cache
def location_vocab() -> LocationVocab:
    data = _load_yaml("locations.yaml")
    regions: dict[str, list[str]] = data.get("regions", {})
    city_regions: dict[str, list[str]] = {}
    for region, members in regions.items():
        for city in members:
            city_regions.setdefault(city, []).append(region)
    return LocationVocab(
        cities=PhraseMatcher(data["cities"]),
        region_aliases=PhraseMatcher(data.get("region_aliases", {})),
        city_regions=city_regions,
        region_cities=regions,
        countries=PhraseMatcher(data["countries"]),
        indian_states=PhraseMatcher({"IN": data.get("indian_states", [])}),
        foreign_cities=PhraseMatcher(data.get("foreign_cities", {})),
        remote_regions=PhraseMatcher(data.get("remote_regions", {})),
    )


@cache
def skill_matcher() -> PhraseMatcher:
    return PhraseMatcher(_load_yaml("taxonomy.yaml")["skills"])
