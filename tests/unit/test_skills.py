from __future__ import annotations

import pytest

from jobbot.normalize.skills import extract_skills
from jobbot.normalize.vocab import PhraseMatcher


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Python, Django and Django REST Framework", ["python", "django", "drf"]),
        ("FastAPI or Flask microservices", ["fastapi", "flask", "microservices", "python"]),
        ("Kafka, Celery, RabbitMQ", ["kafka", "celery", "rabbitmq", "python"]),  # implied
        ("Apache NiFi pipelines", ["nifi"]),
        ("PostgreSQL / MySQL / MongoDB / Redis", ["postgresql", "mysql", "mongodb", "redis"]),
        ("Postgres and Mongo", ["postgresql", "mongodb"]),
        ("AWS, Docker, Kubernetes (k8s)", ["aws", "docker", "kubernetes"]),
        ("RESTful APIs and gRPC", ["rest_api", "grpc"]),
        ("CI/CD with GitHub Actions", ["ci_cd"]),
        ("event-driven, distributed systems", ["event_driven", "distributed_systems"]),
        ("C++ and C# experience", ["cpp", "csharp"]),
        ("Node.js and TypeScript", ["nodejs", "typescript"]),
        ("React Native apps", ["react_native"]),
        ("React and Redux", ["react"]),
        ("Golang services", ["golang"]),
        ("LLM and GenAI platforms", ["llm"]),
        ("system design and DSA", ["system_design", "data_structures_algorithms"]),
    ],
)
def test_skill_extraction(text, expected):
    skills, _ = extract_skills("", text)
    assert skills == expected


@pytest.mark.parametrize(
    "text",
    [
        "Take a rest on weekends",  # bare "rest" is not REST APIs
        "JavaScript only",  # no "java" inside "javascript"
        "Let's go build things",  # bare "go" is not Golang
        "springboard to success",
        "a pythonic mindset",
    ],
)
def test_no_false_positives(text):
    skills, _ = extract_skills("", text)
    assert "rest_api" not in skills
    assert "java" not in skills
    assert "golang" not in skills
    assert "spring" not in skills
    assert "python" not in skills


def test_title_skills_tracked_separately():
    skills, title_skills = extract_skills("Python Developer", "Django, Kafka")
    assert title_skills == ["python"]
    assert skills == ["python", "django", "kafka"]


def test_phrase_matcher_prefers_longest_alias():
    matcher = PhraseMatcher({"noida": ["noida"], "greater_noida": ["greater noida"]})
    assert matcher.find("Greater Noida") == ["greater_noida"]
    assert matcher.find("Noida and Greater Noida") == ["noida", "greater_noida"]


def test_phrase_matcher_empty():
    assert PhraseMatcher({}).find("anything") == []


@pytest.mark.parametrize(
    "framework", ["Django", "Django REST Framework", "FastAPI", "Flask", "Celery"]
)
def test_python_frameworks_imply_python(framework):
    skills, _ = extract_skills("Backend Developer", f"Strong {framework} experience")
    assert "python" in skills


def test_python_not_duplicated_when_stated():
    skills, _ = extract_skills("", "Python and Django")
    assert skills.count("python") == 1
