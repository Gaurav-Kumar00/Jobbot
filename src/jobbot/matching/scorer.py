"""Deterministic, explainable relevance scoring.

Each job goes through hard filters (a reason is always recorded) and gets a 0-100
score from six components. Each component produces a fraction 0..1 that is scaled
by its weight in `Preferences.weights`, plus a short human explanation.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobbot.matching.preferences import Preferences, Profile
from jobbot.models import Job, MatchResult, Normalized, RemoteScope, WorkMode
from jobbot.normalize import normalize
from jobbot.normalize.dedup import normalize_title

BACKEND_LANGUAGES = {"java", "golang", "nodejs", "ruby", "php", "csharp", "scala", "rust", "kotlin"}
BACKEND_SIGNALS = {
    "python", "django", "drf", "fastapi", "flask", "nodejs", "golang", "java", "kafka", "celery",
    "microservices", "rest_api", "postgresql", "mysql", "mongodb", "redis",
}  # fmt: skip
SKILL_LABELS = {
    "python": "Python", "django": "Django", "drf": "DRF", "fastapi": "FastAPI", "flask": "Flask",
    "kafka": "Kafka", "celery": "Celery", "nifi": "NiFi", "mongodb": "MongoDB",
    "postgresql": "PostgreSQL", "mysql": "MySQL", "aws": "AWS", "docker": "Docker",
    "redis": "Redis", "rest_api": "REST APIs", "microservices": "microservices",
    "event_driven": "event-driven", "distributed_systems": "distributed systems",
    "async_processing": "async processing", "message_queues": "message queues", "llm": "LLMs",
    "system_design": "system design", "golang": "Go", "java": "Java", "nodejs": "Node.js",
    "gcp": "GCP", "ci_cd": "CI/CD", "kubernetes": "Kubernetes", "sql": "SQL", "cpp": "C++",
    "linux": "Linux", "data_structures_algorithms": "DSA", "csharp": "C#", "ruby": "Ruby",
    "rust": "Rust", "kotlin": "Kotlin", "scala": "Scala", "php": "PHP", "git": "Git",
    "caching": "caching", "observability": "observability", "javascript": "JavaScript",
}  # fmt: skip
CITY_LABELS = {"bengaluru": "Bengaluru", "hyderabad": "Hyderabad", "ncr": "Delhi NCR"}
ROLE_LABELS = {
    "backend": "Backend role",
    "software_generic": "Software engineering role",
    "fullstack": "Full-stack role",
    "ai_engineering": "AI/LLM engineering role",
    "data_engineering": "Data engineering role",
    "systems": "Systems/kernel role",
    "unknown": "Role type unclear",
}


@dataclass
class Component:
    fraction: float
    detail: str
    positive: bool = True  # False for neutral "not stated" notes, never shown as a reason


def match_job(job: Job, prefs: Preferences, profile: Profile, prefs_version: int) -> MatchResult:
    n = job.normalized or normalize(job)
    weights = prefs.weights.model_dump()
    components = {
        "role": _role(job, n, prefs),
        "tech": _tech(n, prefs),
        "experience": _experience(n),
        "location": _location(n, prefs),
        "salary": _salary(n, prefs),
        "profile": _profile(n, profile, prefs),
    }
    breakdown: dict[str, int] = {}
    total = 0.0
    for name, component in components.items():
        points = max(0.0, min(1.0, component.fraction)) * weights[name]
        if name == "location" and _remote_eligible(n) and prefs.allow_remote:
            points = min(weights[name], points + prefs.remote_bonus)
        breakdown[name] = round(points)
        total += points
    score = max(0, min(100, round(total)))

    reject_reason = hard_filter(job, n, prefs)
    if reject_reason is None and score < prefs.min_score:
        reject_reason = f"score {score} is below your minimum of {prefs.min_score}"
    return MatchResult(
        job_id=job.id,
        score=score,
        decision="reject" if reject_reason else "match",
        reject_reason=reject_reason,
        reasons=_reasons(components, breakdown, weights),
        breakdown=breakdown,
        details={name: c.detail for name, c in components.items()},
        prefs_version=prefs_version,
    )


def tier(score: int) -> str:
    return "🔥" if score >= 80 else "✅" if score >= 65 else "🟡"


# --------------------------------------------------------------------------- filters


def hard_filter(job: Job, n: Normalized, prefs: Preferences) -> str | None:
    """Return why the job can never be a match for these preferences, or None."""
    if job.duplicate_of:
        return f"duplicate of {job.duplicate_of}"
    if n.employment_type not in prefs.allowed_employment:
        return f"{n.employment_type.replace('_', '-')} position"
    if n.role_category in prefs.excluded_categories:
        return f"{n.role_category.replace('_', ' ')} role"
    if n.seniority in prefs.excluded_seniority:
        return f"{n.seniority}-level title"
    if prefs.require_python and "python" not in n.skills:
        others = [s for s in n.skills if s in BACKEND_LANGUAGES | {"cpp"}]
        if others:
            return f"no Python in the stack ({_join(others[:3])})"
    exp = n.experience
    if exp.min_years is not None and exp.min_years > prefs.max_min_years:
        return f"needs {exp.min_years:g}+ years of experience"
    if (reason := _location_reject(n, prefs)) is not None:
        return reason
    salary = n.salary
    if salary and salary.currency == "INR" and salary.min is not None:
        top = salary.max if salary.max is not None else salary.min
        if top < prefs.floor_lpa:
            label = "base" if salary.kind == "base" else "pay"
            return f"{label} ₹{top:g} LPA is below your ₹{prefs.floor_lpa:g} LPA floor"
    return None


def _location_reject(n: Normalized, prefs: Preferences) -> str | None:
    loc = n.location
    if _in_preferred_place(n, prefs):
        return None
    remote = _is_remote(n)
    if remote:
        if not prefs.allow_remote:
            return "remote role (remote is turned off)"
        if loc.remote_scope is RemoteScope.RESTRICTED:
            return "remote, but restricted to other countries"
        return None
    if loc.cities:
        return (
            f"location {', '.join(c.replace('_', ' ').title() for c in loc.cities)} not preferred"
        )
    if loc.countries and "IN" not in loc.countries:
        return f"outside India ({', '.join(loc.countries)})"
    if not loc.countries and not prefs.allow_unknown_location:
        return "location not stated or not recognisably in India"
    return None


# --------------------------------------------------------------------------- components


def _role(job: Job, n: Normalized, prefs: Preferences) -> Component:
    title = f" {normalize_title(job.title)} "
    category = n.role_category
    if any(f" {normalize_title(t)} " in title for t in prefs.primary_titles):
        fraction = 1.0 if category in ("backend", "software_generic", "unknown") else 0.6
        return Component(fraction, f"{ROLE_LABELS.get(category, 'Role')} (preferred title)")
    if category == "backend":
        return Component(0.96, ROLE_LABELS["backend"])
    if any(f" {normalize_title(t)} " in title for t in prefs.secondary_titles):
        return Component(0.84, f"{ROLE_LABELS.get(category, 'Role')} (secondary title)")
    if category == "software_generic":
        return Component(0.84, ROLE_LABELS["software_generic"])
    if category == "fullstack":
        heavy = "backend" in title or len(set(n.skills) & BACKEND_SIGNALS) >= 3
        return Component(
            0.76 if heavy else 0.52, "Full-stack, backend-heavy" if heavy else "Full-stack role"
        )
    if category == "ai_engineering":
        return Component(0.6, ROLE_LABELS["ai_engineering"])
    if category == "data_engineering":
        return Component(0.4, ROLE_LABELS["data_engineering"])
    if category == "systems":
        return Component(0.3, ROLE_LABELS["systems"])
    return Component(0.16, ROLE_LABELS.get(category, f"{category} role"))


def _tech(n: Normalized, prefs: Preferences) -> Component:
    skills = set(n.skills)
    frameworks = [s for s in prefs.primary_skills if s in skills and s != "python"]
    secondary = [s for s in prefs.secondary_skills if s in skills]
    if "python" in skills:
        # Python listed after another language ("C++, Java, Python scripting") is secondary.
        languages = [s for s in n.skills if s == "python" or s in BACKEND_LANGUAGES | {"cpp"}]
        primary_python = languages[0] == "python" or "python" in n.title_skills
        fraction = 0.48 if primary_python else 0.3
        fraction += 0.32 if len(frameworks) >= 2 else 0.24 if frameworks else 0.0
        fraction += min(0.2, 0.04 * len(secondary))
        if "python" in n.title_skills:
            fraction += 0.08
        label = "Python" + (f" + {_join(frameworks)}" if frameworks else "")
        if not primary_python:
            label = f"Python (secondary to {_join([languages[0]])})"
        return Component(fraction, label)
    other = sorted(skills & BACKEND_LANGUAGES)
    if other:
        return Component(
            0.24 + min(0.2, 0.04 * len(secondary)), f"No Python; stack is {_join(other)}"
        )
    if not skills:
        return Component(0.32, "Tech stack not stated", positive=False)
    return Component(0.16 + min(0.2, 0.04 * len(secondary)), "No Python or backend language listed")


def _experience(n: Normalized) -> Component:
    exp = n.experience
    low, high = exp.min_years, exp.max_years
    if exp.fresher and (low is None or low <= 0.5):
        return Component(1.0, f"Fresher-friendly{_years_label(low, high, prefix=' ')}")
    if low is None:
        by_level = {"junior": (0.85, "Junior-level title"), "mid": (0.25, "Mid-level title")}
        if n.seniority in by_level:
            return Component(*by_level[n.seniority])
        return Component(0.55, "Experience not stated", positive=False)
    if low <= 0.5:
        if high is not None and high <= 1:
            return Component(1.0, f"Entry level{_years_label(low, high, prefix=' ')}")
        if high is not None and high <= 2:
            return Component(0.9, _years_label(low, high))
        return Component(0.8, _years_label(low, high))
    if low <= 1:
        return Component(0.7 if high is None or high <= 2 else 0.6, _years_label(low, high))
    if low <= 2:
        return Component(0.3, _years_label(low, high))
    return Component(0.0, _years_label(low, high))


def _location(n: Normalized, prefs: Preferences) -> Component:
    loc = n.location
    mode = "" if loc.work_mode is WorkMode.UNKNOWN else f" · {loc.work_mode.value}"
    if _in_preferred_place(n, prefs):
        places = [p for p in prefs.locations if p in loc.cities or p in loc.regions]
        names = ", ".join(CITY_LABELS.get(p, p.replace("_", " ").title()) for p in places)
        return Component(0.8, f"{names}{mode}")
    if loc.cities and not _is_remote(n):
        names = ", ".join(c.replace("_", " ").title() for c in loc.cities)
        return Component(0.2, f"{names} (not in your locations)")
    if _is_remote(n):
        scope = loc.remote_scope
        if scope is RemoteScope.RESTRICTED:
            return Component(0.1, "Remote, restricted to other countries")
        if scope in (RemoteScope.INDIA, RemoteScope.GLOBAL):
            where = "India" if scope is RemoteScope.INDIA else "worldwide"
            return Component(0.8, f"Remote ({where})")
        if scope is RemoteScope.APAC:
            return Component(0.6, "Remote (APAC)")
        return Component(0.5, "Remote (eligibility not stated)", positive=False)
    if "IN" in loc.countries:
        return Component(0.5, "India (city not stated)", positive=False)
    if loc.countries:
        return Component(0.0, f"Outside India ({', '.join(loc.countries)})")
    return Component(0.3, "Location not stated", positive=False)


def _salary(n: Normalized, prefs: Preferences) -> Component:
    s = n.salary
    if s is None or s.currency != "INR" or s.min is None:
        return Component(0.5, "Salary not disclosed", positive=False)
    low = s.min
    label = f"₹{low:g}{f'–{s.max:g}' if s.max else ''} LPA"
    top = s.max if s.max is not None else low
    if s.kind == "base":
        if low >= prefs.target_base_lpa or top >= prefs.target_base_lpa:
            return Component(1.0, f"Base {label}")
        return Component(0.6, f"Base {label} (below ₹{prefs.target_base_lpa:g} target)")
    kind = "CTC" if s.kind == "ctc" else "Pay"
    if top >= prefs.target_base_lpa:
        return Component(0.7, f"{kind} {label} (base not stated)")
    return Component(0.4, f"{kind} {label} (base not stated)")


def _profile(n: Normalized, profile: Profile, prefs: Preferences) -> Component:
    job_skills = set(n.skills)
    if not job_skills:
        return Component(0.5, "No skills listed to compare", positive=False)
    overlap = job_skills & set(profile.skills)
    fraction = 0.5 * min(1.0, len(overlap) / 5) + 0.5 * (len(overlap) / len(job_skills))
    extras = [s for s in sorted(overlap, key=_skill_rank(prefs)) if s not in prefs.primary_skills]
    if extras:
        return Component(fraction, f"Also matches: {_join(extras[:4])}")
    if overlap:
        return Component(fraction, f"{len(overlap)} of {len(job_skills)} listed skills are yours")
    return Component(fraction, "Little overlap with your skills", positive=False)


# --------------------------------------------------------------------------- helpers


def _in_preferred_place(n: Normalized, prefs: Preferences) -> bool:
    wanted = set(prefs.locations)
    return bool(wanted & set(n.location.cities) or wanted & set(n.location.regions))


def _is_remote(n: Normalized) -> bool:
    return n.location.work_mode is WorkMode.REMOTE or (
        n.location.remote_scope is not RemoteScope.NOT_REMOTE
    )


def _remote_eligible(n: Normalized) -> bool:
    return _is_remote(n) and n.location.remote_scope in (RemoteScope.INDIA, RemoteScope.GLOBAL)


def _years_label(low: float | None, high: float | None, prefix: str = "") -> str:
    if low is None:
        return ""
    text = f"{low:g}–{high:g} yrs" if high is not None else f"{low:g}+ yrs"
    return f"{prefix}({text})" if prefix else text


def _join(skills: list[str]) -> str:
    return ", ".join(SKILL_LABELS.get(s, s.replace("_", " ")) for s in skills)


def _skill_rank(prefs: Preferences):
    order = {s: i for i, s in enumerate([*prefs.primary_skills, *prefs.secondary_skills])}
    return lambda s: (order.get(s, len(order)), s)


def _reasons(
    components: dict[str, Component], breakdown: dict[str, int], weights: dict
) -> list[str]:
    """Explanations for the components that scored well, strongest first."""
    strong = [
        (breakdown[name] / weights[name] if weights[name] else 0, comp.detail)
        for name, comp in components.items()
        if comp.positive and weights[name] and breakdown[name] / weights[name] >= 0.5
    ]
    return [detail for _, detail in sorted(strong, key=lambda item: -item[0])]
