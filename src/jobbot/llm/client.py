"""One OpenAI-compatible chat client for every provider (Groq, Gemini, OpenRouter, ...).

Swapping providers is configuration: base URL, model and key. Responses are validated
with Pydantic, so a provider that ignores the JSON schema can't inject garbage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from jobbot.models import ROLE_TYPES, AIInsight, Job

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 3500  # ~900 tokens: keeps each call ~1.1K tokens (Groq free TPM 8K)

SYSTEM_PROMPT = (
    "You extract facts from a single job posting for a job-matching bot. Use only what the "
    "posting says. Use null or 'unknown' when something is not stated; never guess.\n"
    "- min_years/max_years: required professional experience in years (fresher = 0).\n"
    "- fresher_friendly: true only if freshers / new grads / 0 years are explicitly welcome.\n"
    "- role_type: what the person mostly does day to day.\n"
    "  'backend' = builds server-side application services/APIs (e.g. Python/Django, Go, "
    "Java services, microservices, queues).\n"
    "  'fullstack_backend_heavy' = full-stack but mostly server-side; 'fullstack' = balanced.\n"
    "  'data_engineering' = data pipelines, ETL/ELT, warehouses (Snowflake, Airflow, dbt, Spark).\n"
    "  'data_science' = analytics, research, quantitative research/trading models, statistics.\n"
    "  'ml' = training or deploying ML/LLM models. 'devops' = infra, SRE, platform operations.\n"
    "  'support' = support/solutions/sales/customer engineering. 'other' = non-engineering.\n"
    "  When unsure between backend and another type, do not choose backend.\n"
    "- python_used: true if Python (or a Python framework) is part of the job.\n"
    "- remote_india: can someone living in India do this role remotely? 'not_remote' if "
    "on-site/hybrid.\n"
    "- salary_kind: 'base' if a fixed/base figure is stated, 'ctc' if total/CTC, "
    "'unspecified' if an amount is given without saying which, 'none' if no amount. "
    "salary_*_lpa in lakhs per annum (INR only; otherwise null).\n"
    "- summary: at most 2 short sentences, under 220 characters in total, on what the "
    "engineer will actually build or own. No company marketing."
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "min_years": {"type": ["number", "null"]},
        "max_years": {"type": ["number", "null"]},
        "fresher_friendly": {"type": "boolean"},
        "role_type": {"type": "string", "enum": list(ROLE_TYPES)},
        "python_used": {"type": "boolean"},
        "remote_india": {"type": "string", "enum": ["yes", "no", "unknown", "not_remote"]},
        "salary_kind": {"type": "string", "enum": ["base", "ctc", "unspecified", "none"]},
        "salary_min_lpa": {"type": ["number", "null"]},
        "salary_max_lpa": {"type": ["number", "null"]},
        "summary": {"type": "string"},
    },
    "required": [
        "min_years", "max_years", "fresher_friendly", "role_type", "python_used",
        "remote_india", "salary_kind", "salary_min_lpa", "salary_max_lpa", "summary",
    ],
}  # fmt: skip


class LLMError(Exception):
    """A provider call failed; the caller falls back (or skips the LLM entirely)."""


class LLMRateLimited(LLMError):
    def __init__(self, provider: str, retry_after: float | None):
        self.retry_after = retry_after
        super().__init__(f"{provider}: rate limited (retry after {retry_after}s)")


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str
    strict_schema: bool = True  # json_schema strict; False -> json_object + our validation
    reasoning_effort: str | None = None  # gpt-oss models: "low" keeps calls fast and cheap

    async def extract(self, http: httpx.AsyncClient, job: Job) -> AIInsight:
        body: dict = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _job_text(job)},
            ],
            "response_format": (
                {
                    "type": "json_schema",
                    "json_schema": {"name": "job_facts", "strict": True, "schema": SCHEMA},
                }
                if self.strict_schema
                else {"type": "json_object"}
            ),
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        try:
            resp = await http.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=45,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name}: network error ({type(exc).__name__})") from None
        if resp.status_code == 429:
            raise LLMRateLimited(self.name, _retry_after(resp))
        if resp.status_code >= 400:
            raise LLMError(f"{self.name}: HTTP {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            return AIInsight.model_validate({**data, "model": f"{self.name}:{self.model}"})
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise LLMError(f"{self.name}: unusable response ({type(exc).__name__})") from None


def _job_text(job: Job) -> str:
    description = job.description[:MAX_DESCRIPTION_CHARS]
    return (
        f"Title: {job.title}\nCompany: {job.company_name or job.company}\n"
        f"Location: {job.location_raw or 'not stated'}\n\n{description}"
    )


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None
