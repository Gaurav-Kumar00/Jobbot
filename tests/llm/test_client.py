from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobbot.llm.chain import GROQ_URL, LLMChain, build_chain
from jobbot.llm.client import LLMError, LLMRateLimited, Provider
from jobbot.models import Job
from jobbot.settings import Settings

URL = f"{GROQ_URL}/chat/completions"
FACTS = {
    "min_years": 0, "max_years": 1, "fresher_friendly": True, "role_type": "backend",
    "python_used": True, "remote_india": "not_remote", "salary_kind": "none",
    "salary_min_lpa": None, "salary_max_lpa": None, "summary": "Builds payment APIs.",
}  # fmt: skip


def completion(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


JOB = Job(id="lever:acme:1", source="lever", company="acme", company_name="Acme",
          title="SDE 1", url="u", location_raw="Bengaluru", description="x" * 9000)  # fmt: skip
PROVIDER = Provider("groq", GROQ_URL, "openai/gpt-oss-20b", "gsk_test_key", reasoning_effort="low")


@respx.mock
async def test_request_uses_strict_schema_and_trims_description():
    route = respx.post(URL).mock(return_value=completion(FACTS))
    async with httpx.AsyncClient() as http:
        insight = await PROVIDER.extract(http, JOB)
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "openai/gpt-oss-20b" and body["temperature"] == 0
    assert body["reasoning_effort"] == "low"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert len(body["messages"][1]["content"]) < 4000  # description trimmed
    assert route.calls.last.request.headers["Authorization"] == "Bearer gsk_test_key"
    assert insight.role_type == "backend" and insight.model == "groq:openai/gpt-oss-20b"


@respx.mock
async def test_json_object_mode_for_non_strict_providers():
    route = respx.post("https://example.test/v1/chat/completions").mock(
        return_value=completion(FACTS)
    )
    provider = Provider("gemini", "https://example.test/v1", "m", "k", strict_schema=False)
    async with httpx.AsyncClient() as http:
        await provider.extract(http, JOB)
    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"} and "reasoning_effort" not in body


@respx.mock
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, headers={"retry-after": "7"}), LLMRateLimited),
        (httpx.Response(500), LLMError),
        (completion("not json at all"), LLMError),
        (completion({"role_type": 5}), LLMError),
        (httpx.Response(200, json={"choices": []}), LLMError),
    ],
)
async def test_failures_raise_and_never_leak_the_key(response, error):
    respx.post(URL).mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(error) as exc:
            await PROVIDER.extract(http, JOB)
    assert "gsk_test_key" not in str(exc.value)
    if error is LLMRateLimited:
        assert exc.value.retry_after == 7


@respx.mock
async def test_out_of_range_values_are_cleaned():
    respx.post(URL).mock(
        return_value=completion(
            {**FACTS, "min_years": 99, "role_type": "wizard", "summary": "a " * 400}
        )
    )
    async with httpx.AsyncClient() as http:
        insight = await PROVIDER.extract(http, JOB)
    assert insight.min_years is None and insight.role_type == "other"
    assert len(insight.summary) <= 300


# --- chain ------------------------------------------------------------------------------


class Scripted:
    """Provider stub that plays back responses/exceptions."""

    def __init__(self, name, *results):
        self.name, self.model, self.results, self.calls = name, "m", list(results), 0

    async def extract(self, http, job):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def no_sleep(_):
    return None


async def test_chain_retries_short_rate_limit_then_succeeds():
    from jobbot.models import AIInsight

    ok = AIInsight(model="a", role_type="backend")
    first = Scripted("a", LLMRateLimited("a", 2.0), ok)
    chain = LLMChain([first], sleep=no_sleep)
    assert await chain.extract(None, JOB) == ok and first.calls == 2


async def test_chain_falls_back_and_marks_exhausted():
    from jobbot.models import AIInsight

    ok = AIInsight(model="b", role_type="backend")
    first = Scripted("a", LLMRateLimited("a", 3600.0))
    second = Scripted("b", ok, ok)
    chain = LLMChain([first, second], sleep=no_sleep)
    assert await chain.extract(None, JOB) == ok
    assert await chain.extract(None, JOB) == ok
    assert first.calls == 1  # quota gone for this run: not retried
    assert chain.available


async def test_chain_returns_none_when_everything_fails():
    chain = LLMChain([Scripted("a", LLMError("boom")), Scripted("b", LLMRateLimited("b", None))])
    assert await chain.extract(None, JOB) is None
    assert len(chain.errors) == 2
    assert chain.available  # "a" erred (not exhausted); "b" is exhausted


def test_build_chain(monkeypatch):
    assert build_chain(Settings()) is None  # no keys: LLM layer off
    monkeypatch.setenv("GROQ_TOKEN", "gsk_alias")  # the name the user chose works too
    chain = build_chain(Settings())
    assert [p.model for p in chain.providers] == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert [p.name for p in build_chain(Settings()).providers][-1] == "gemini"
