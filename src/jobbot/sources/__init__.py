"""Source registry. Adding a source = one module + one entry here."""

from __future__ import annotations

from jobbot.sources.amazon import AmazonSource
from jobbot.sources.ashby import AshbySource
from jobbot.sources.atlassian import AtlassianSource
from jobbot.sources.base import FetchResult, Source
from jobbot.sources.greenhouse import GreenhouseSource
from jobbot.sources.keka import KekaSource
from jobbot.sources.lever import LeverSource
from jobbot.sources.microsoft import MicrosoftSource
from jobbot.sources.smartrecruiters import SmartRecruitersSource
from jobbot.sources.unstop import UnstopSource
from jobbot.sources.workable import WorkableSource

SOURCES: dict[str, Source] = {
    source.name: source
    for source in (
        GreenhouseSource(),
        LeverSource(),
        AshbySource(),
        SmartRecruitersSource(),
        WorkableSource(),
        KekaSource(),
        AmazonSource(),
        MicrosoftSource(),
        AtlassianSource(),
        UnstopSource(),
    )
}


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError:
        raise KeyError(
            f"unknown source {name!r}; available: {', '.join(sorted(SOURCES))}"
        ) from None


__all__ = ["SOURCES", "FetchResult", "Source", "get_source"]
