"""Source registry. Adding a source = one module + one entry here."""

from __future__ import annotations

from jobbot.sources.base import FetchResult, Source
from jobbot.sources.greenhouse import GreenhouseSource

SOURCES: dict[str, Source] = {source.name: source for source in (GreenhouseSource(),)}


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError:
        raise KeyError(
            f"unknown source {name!r}; available: {', '.join(sorted(SOURCES))}"
        ) from None


__all__ = ["SOURCES", "FetchResult", "Source", "get_source"]
