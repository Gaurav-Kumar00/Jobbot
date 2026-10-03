"""Free-text job location -> cities, regions, countries, work mode, remote eligibility."""

from __future__ import annotations

import re

from jobbot.models import LocationInfo, RemoteScope, WorkMode
from jobbot.normalize.vocab import location_vocab

_REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere|telecommute|virtual)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(
    r"\b(on[\s-]?site|in[\s-]office|office[\s-]based|work from office|wfo)\b", re.I
)
_GLOBAL = re.compile(r"\b(anywhere|worldwide|world[\s-]wide|global(?:ly)?|any location)\b", re.I)
# Two-letter US state after a comma ("Austin, TX"). Case-sensitive; IN (Indiana) excluded.
_US_STATE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV"
    r"|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)

# Description cues, used only when the location field itself is inconclusive.
_DESC_REMOTE = re.compile(
    r"\b(fully remote|100% remote|remote[\s-]first|this is a remote (?:role|position|job)"
    r"|work from home|remote (?:role|position|opportunity))\b",
    re.I,
)
_DESC_HYBRID = re.compile(r"\bhybrid (?:role|position|work|model|setup|mode|working)\b", re.I)
_DESC_ONSITE = re.compile(
    r"\b(work from office|in[\s-]office (?:role|position)|on[\s-]?site (?:role|position)"
    r"|5 days (?:a week )?(?:in|from) (?:the )?office)\b",
    re.I,
)
_DESC_INDIA = re.compile(
    r"(?:remote|work from home|wfh)[^.\n]{0,80}\bindia\b"
    r"|\bindia\b[^.\n]{0,40}\bremote\b"
    r"|\b(?:based|located|residing|reside) (?:anywhere )?in india\b",
    re.I,
)
_DESC_RESTRICTED = re.compile(
    r"\b(?:must|need to|should|required to)\s+(?:be\s+)?(?:located|based|reside|residing|live)"
    r"\s+(?:in|within)\s+(?:the\s+)?(?:us|u\.s\.|usa|united states|canada|uk|united kingdom"
    r"|europe|eu|emea|latam|americas)\b"
    r"|\bauthori[sz]ed to work in the (?:us|u\.s\.|united states|uk)\b"
    r"|\b(?:us|u\.s\.|usa)[- ]only\b",
    re.I,
)
_DESC_GLOBAL = re.compile(
    r"\b(work from anywhere|anywhere in the world|fully distributed team|globally distributed)\b",
    re.I,
)

_HINT_MODES = {
    "remote": WorkMode.REMOTE,
    "hybrid": WorkMode.HYBRID,
    "onsite": WorkMode.ONSITE,
    "on-site": WorkMode.ONSITE,
    "in-office": WorkMode.ONSITE,
    "office": WorkMode.ONSITE,
}


def normalize_location(
    location_raw: str, *, workplace_hint: str | None = None, description: str = ""
) -> LocationInfo:
    vocab = location_vocab()
    text = location_raw or ""

    cities = vocab.cities.find(text)
    if not cities and not _has_place(vocab, text) and not _REMOTE.search(text):
        # Location field is empty or vague ("N/A"): fall back to Indian cities named in
        # the description, e.g. "you'll join our Bengaluru office".
        cities = vocab.cities.find(description)
    regions = list(
        dict.fromkeys(
            [*vocab.region_aliases.find(text)]
            + [region for city in cities for region in vocab.city_regions.get(city, [])]
        )
    )
    countries = vocab.countries.find(text)
    if cities or regions or vocab.indian_states.find(text):
        countries.append("IN")
    countries.extend(vocab.foreign_cities.find(text))
    if _US_STATE.search(text):
        countries.append("US")
    countries = list(dict.fromkeys(countries))

    mode = _work_mode(text, workplace_hint, description)
    remote = mode is WorkMode.REMOTE or bool(_REMOTE.search(text))
    scope = _remote_scope(text, countries, description) if remote else RemoteScope.NOT_REMOTE
    return LocationInfo(
        cities=cities, regions=regions, countries=countries, work_mode=mode, remote_scope=scope
    )


def _has_place(vocab, text: str) -> bool:
    return bool(
        vocab.countries.find(text)
        or vocab.foreign_cities.find(text)
        or vocab.indian_states.find(text)
        or vocab.region_aliases.find(text)
        or _US_STATE.search(text)
    )


def _work_mode(text: str, hint: str | None, description: str) -> WorkMode:
    if _HYBRID.search(text):
        return WorkMode.HYBRID
    if _REMOTE.search(text):
        return WorkMode.REMOTE
    if _ONSITE.search(text):
        return WorkMode.ONSITE
    if hint and (mode := _HINT_MODES.get(hint.strip().lower())):
        return mode
    if _DESC_HYBRID.search(description):
        return WorkMode.HYBRID
    if _DESC_REMOTE.search(description):
        return WorkMode.REMOTE
    if _DESC_ONSITE.search(description):
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def _remote_scope(text: str, countries: list[str], description: str) -> RemoteScope:
    if "IN" in countries:
        return RemoteScope.INDIA
    if _GLOBAL.search(text):
        return RemoteScope.GLOBAL
    if countries:
        return RemoteScope.RESTRICTED
    regions = location_vocab().remote_regions.find(text)
    if "apac" in regions:
        return RemoteScope.APAC
    if "restricted" in regions:
        return RemoteScope.RESTRICTED
    if _DESC_RESTRICTED.search(description):
        return RemoteScope.RESTRICTED
    if _DESC_INDIA.search(description):
        return RemoteScope.INDIA
    if _DESC_GLOBAL.search(description):
        return RemoteScope.GLOBAL
    return RemoteScope.UNKNOWN
