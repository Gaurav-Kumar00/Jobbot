from __future__ import annotations

import pytest

from jobbot.models import RemoteScope, WorkMode
from jobbot.normalize.location import normalize_location

R, H, ON, U = WorkMode.REMOTE, WorkMode.HYBRID, WorkMode.ONSITE, WorkMode.UNKNOWN
NR = RemoteScope.NOT_REMOTE


@pytest.mark.parametrize(
    ("raw", "cities", "countries", "mode", "scope"),
    [
        ("Bengaluru", ["bengaluru"], ["IN"], U, NR),
        ("Bangalore", ["bengaluru"], ["IN"], U, NR),
        ("Bengaluru-VTP, India", ["bengaluru"], ["IN"], U, NR),
        ("BLR", ["bengaluru"], ["IN"], U, NR),
        ("Whitefield, Bangalore", ["bengaluru"], ["IN"], U, NR),
        ("Gurugram, Haryana, India", ["gurugram"], ["IN"], U, NR),
        ("Gurgaon", ["gurugram"], ["IN"], U, NR),
        ("Greater Noida", ["greater_noida"], ["IN"], U, NR),
        ("Noida, Uttar Pradesh", ["noida"], ["IN"], U, NR),
        ("New Delhi, India", ["delhi"], ["IN"], U, NR),
        ("Hyderabad, Telangana", ["hyderabad"], ["IN"], U, NR),
        ("Secunderabad", ["hyderabad"], ["IN"], U, NR),
        ("Bangalore / Hyderabad", ["bengaluru", "hyderabad"], ["IN"], U, NR),
        ("Mumbai, Maharashtra, India; Pune", ["mumbai", "pune"], ["IN"], U, NR),
        ("Chennai", ["chennai"], ["IN"], U, NR),
        ("India", [], ["IN"], U, NR),
        ("Karnataka", [], ["IN"], U, NR),
        ("Hybrid - Noida", ["noida"], ["IN"], H, NR),
        ("Bengaluru, Karnataka, India (Hybrid)", ["bengaluru"], ["IN"], H, NR),
        ("Bengaluru (On-site)", ["bengaluru"], ["IN"], ON, NR),
        ("Remote - India", [], ["IN"], R, RemoteScope.INDIA),
        ("Remote (India)", [], ["IN"], R, RemoteScope.INDIA),
        ("India - Remote", [], ["IN"], R, RemoteScope.INDIA),
        ("Remote - Anywhere in India", [], ["IN"], R, RemoteScope.INDIA),
        ("Remote, Bengaluru", ["bengaluru"], ["IN"], R, RemoteScope.INDIA),
        ("Remote: India, US, UK", [], ["IN", "US", "GB"], R, RemoteScope.INDIA),
        ("Anywhere", [], [], R, RemoteScope.GLOBAL),
        ("Remote (Worldwide)", [], [], R, RemoteScope.GLOBAL),
        ("Remote - Global", [], [], R, RemoteScope.GLOBAL),
        ("Remote, APAC", [], [], R, RemoteScope.APAC),
        ("Remote - Asia Pacific", [], [], R, RemoteScope.APAC),
        ("Remote (US only)", [], ["US"], R, RemoteScope.RESTRICTED),
        ("Remote - USA", [], ["US"], R, RemoteScope.RESTRICTED),
        ("Remote - EMEA", [], [], R, RemoteScope.RESTRICTED),
        ("Remote, Europe", [], [], R, RemoteScope.RESTRICTED),
        ("Remote - Canada", [], ["CA"], R, RemoteScope.RESTRICTED),
        ("Remote", [], [], R, RemoteScope.UNKNOWN),
        ("San Francisco, CA", [], ["US"], U, NR),
        ("Austin, TX", [], ["US"], U, NR),
        ("London, United Kingdom", [], ["GB"], U, NR),
        ("Singapore", [], ["SG"], U, NR),
        ("Kuala Lumpur, Malaysia", [], ["MY"], U, NR),
        ("Indianapolis, IN", [], [], U, NR),  # IN = Indiana, never India
        ("", [], [], U, NR),
    ],
)
def test_location_table(raw, cities, countries, mode, scope):
    info = normalize_location(raw)
    assert info.cities == cities
    assert info.countries == countries
    assert info.work_mode is mode
    assert info.remote_scope is scope


@pytest.mark.parametrize(
    ("raw", "regions"),
    [
        ("Gurugram", ["ncr"]),
        ("Noida", ["ncr"]),
        ("Delhi NCR", ["ncr"]),
        ("NCR", ["ncr"]),
        ("Faridabad", ["ncr"]),
        ("Bengaluru", []),
    ],
)
def test_ncr_region(raw, regions):
    assert normalize_location(raw).regions == regions


@pytest.mark.parametrize(
    ("hint", "mode"),
    [("remote", R), ("Hybrid", H), ("onsite", ON), ("on-site", ON), ("whatever", U), (None, U)],
)
def test_workplace_hint(hint, mode):
    assert normalize_location("", workplace_hint=hint).work_mode is mode


def test_location_text_beats_hint():
    assert normalize_location("Hybrid - Bengaluru", workplace_hint="remote").work_mode is H


@pytest.mark.parametrize(
    ("description", "mode"),
    [
        ("This is a hybrid role with 3 days in office.", H),
        ("We are a remote-first company.", R),
        ("This is a fully remote position.", R),
        ("Work from office, 5 days a week.", ON),
        ("Our office has great coffee.", U),
    ],
)
def test_work_mode_from_description(description, mode):
    assert normalize_location("Bengaluru", description=description).work_mode is mode


@pytest.mark.parametrize(
    ("description", "scope"),
    [
        ("Candidates must be located in the US.", RemoteScope.RESTRICTED),
        ("You must be authorized to work in the United States.", RemoteScope.RESTRICTED),
        ("This role is US-only.", RemoteScope.RESTRICTED),
        ("This role is remote within India.", RemoteScope.INDIA),
        ("Open to candidates based in India.", RemoteScope.INDIA),
        ("We are a fully distributed team; work from anywhere.", RemoteScope.GLOBAL),
        ("Great team, interesting problems.", RemoteScope.UNKNOWN),
    ],
)
def test_remote_scope_refined_from_description(description, scope):
    assert normalize_location("Remote", description=description).remote_scope is scope


def test_description_never_overrides_explicit_location_scope():
    info = normalize_location("Remote - India", description="Must be located in the US.")
    assert info.remote_scope is RemoteScope.INDIA
