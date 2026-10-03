from __future__ import annotations

import pytest

from jobbot.config import find_company, load_companies
from jobbot.settings import ConfigError
from jobbot.sources import SOURCES


def write(tmp_path, body: str):
    path = tmp_path / "companies.yaml"
    path.write_text(body)
    return path


def test_repo_company_registry_is_valid():
    companies = load_companies(known_sources=SOURCES)
    assert companies, "config/companies.yaml should list at least one company"
    assert (
        find_company(companies, "greenhouse", "razorpay").slug == "razorpaysoftwareprivatelimited"
    )
    # lookup also works by slug
    assert find_company(companies, "greenhouse", "razorpaysoftwareprivatelimited").key == "razorpay"
    assert find_company(companies, "lever", "razorpay") is None


def test_defaults_and_options(tmp_path):
    path = write(
        tmp_path,
        """
companies:
  - {key: acme, name: Acme, ats: greenhouse, slug: acme}
  - {key: beta, name: Beta, ats: greenhouse, slug: beta-inc, enabled: false, options: {site: x}}
""",
    )
    acme, beta = load_companies(path)
    assert acme.enabled is True and acme.options == {}
    assert beta.enabled is False and beta.options == {"site": "x"}


def test_empty_file_gives_empty_list(tmp_path):
    assert load_companies(write(tmp_path, "")) == []


def test_missing_field_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="entry #1"):
        load_companies(write(tmp_path, "companies:\n  - {key: acme, ats: greenhouse}\n"))


def test_unknown_ats_is_rejected(tmp_path):
    body = "companies:\n  - {key: acme, name: Acme, ats: workdayy, slug: acme}\n"
    with pytest.raises(ConfigError, match="unknown ats"):
        load_companies(write(tmp_path, body), known_sources=SOURCES)


def test_duplicate_keys_are_rejected(tmp_path):
    body = (
        "companies:\n"
        "  - {key: acme, name: Acme, ats: greenhouse, slug: a}\n"
        "  - {key: acme, name: Acme 2, ats: greenhouse, slug: b}\n"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_companies(write(tmp_path, body))


def test_invalid_yaml_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_companies(write(tmp_path, "companies: [unclosed"))
    with pytest.raises(ConfigError, match="not found"):
        load_companies(tmp_path / "nope.yaml")
