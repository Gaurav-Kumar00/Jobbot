from __future__ import annotations

import pytest

from jobbot.normalize.role import classify_employment, classify_role, classify_seniority


@pytest.mark.parametrize(
    ("title", "category", "seniority"),
    [
        # primary targets
        ("Backend Engineer", "backend", "unknown"),
        ("Software Engineer - Backend", "backend", "unknown"),
        ("Backend Software Engineer", "backend", "unknown"),
        ("SDE 1 - Backend", "backend", "junior"),
        ("Junior Backend Engineer", "backend", "junior"),
        ("Junior Python Developer", "backend", "junior"),
        ("Python Developer", "backend", "unknown"),
        ("Django Developer", "backend", "unknown"),
        ("Backend Developer (Node.js)", "backend", "unknown"),
        ("Golang Developer", "backend", "unknown"),
        ("Software Engineer", "software_generic", "unknown"),
        ("Software Development Engineer I", "software_generic", "junior"),
        ("SDE-1", "software_generic", "junior"),
        ("Software Engineer I (Payments)", "software_generic", "junior"),
        ("Associate Software Engineer", "software_generic", "junior"),
        ("Graduate Engineer Trainee", "software_generic", "junior"),
        ("Member of Technical Staff", "software_generic", "unknown"),
        ("MTS 2", "software_generic", "mid"),
        ("Founding Engineer", "software_generic", "unknown"),
        # seniority above target
        ("SDE II", "software_generic", "mid"),
        ("Software Engineer II", "software_generic", "mid"),
        ("Senior Backend Engineer", "backend", "senior"),
        ("Sr. Software Engineer", "software_generic", "senior"),
        ("SDE III", "software_generic", "senior"),
        ("Staff Software Engineer", "software_generic", "staff"),
        ("Principal Engineer", "software_generic", "principal"),
        ("Lead Backend Engineer", "backend", "lead"),
        # adjacent families
        ("Full Stack Developer (Backend heavy)", "fullstack", "unknown"),
        ("Full-Stack Engineer", "fullstack", "unknown"),
        ("Data Engineer", "data_engineering", "unknown"),
        ("ML Engineer", "ai_engineering", "unknown"),
        ("AI Engineer - LLM", "ai_engineering", "unknown"),
        ("Platform Engineer", "backend", "unknown"),
        # excluded families
        ("Frontend Engineer", "frontend", "unknown"),
        ("Software Engineer, Frontend", "frontend", "unknown"),
        ("React Developer", "frontend", "unknown"),
        ("Android Developer", "mobile", "unknown"),
        ("iOS Engineer", "mobile", "unknown"),
        ("QA Automation Engineer (Python)", "qa", "unknown"),
        ("SDET", "qa", "unknown"),
        ("DevOps Enginer", "devops", "unknown"),
        ("Site Reliability Engineer", "devops", "unknown"),
        ("Data Scientist", "data_science", "unknown"),
        ("Research Scientist", "ml_research", "unknown"),
        ("Security Engineer", "security", "unknown"),
        ("Embedded Software Engineer", "embedded", "unknown"),
        ("Engineering Manager", "management", "manager"),
        # non-software titles from real Razorpay / Groww boards
        ("Product Manager II", "non_software", "manager"),
        ("Technical Account Manager", "non_software", "manager"),
        ("Solutions Consultant", "non_software", "unknown"),
        ("Relationship Manager", "non_software", "manager"),
        ("Senior Executive, Finance", "non_software", "senior"),
        ("Associate - Content (Digest)", "non_software", "unknown"),
        ("Video Editor Intern", "non_software", "intern"),
        # found in the Phase 4 spot-check on real MongoDB / Okta / Databricks / HackerRank boards
        ("Software Development Engineer in Test II", "qa", "mid"),
        ("Principal UI Software Engineer", "frontend", "principal"),
        ("Sr. Manager, AI Forward Deployed Engineering", "management", "manager"),
        ("Cloud Operations Engineer", "devops", "unknown"),
        ("Coordinator, Shared Services", "non_software", "unknown"),
        ("Staff SRE for K8s Platform Team (AWS, Kubernetes)", "devops", "staff"),
        ("Software Engineer 3 - Enterprise Architecture", "software_generic", "senior"),
        ("Intermediate Backend Engineer, India", "backend", "mid"),
        ("", "unknown", "unknown"),
    ],
)
def test_role_and_seniority(title, category, seniority):
    assert classify_role(title) == category
    assert classify_seniority(title) == seniority


@pytest.mark.parametrize(
    ("title", "description", "hint", "expected"),
    [
        ("Backend Engineer", "", None, "full_time"),
        ("Software Engineering Intern", "", None, "intern"),
        ("Backend Engineer - Internship (6 months)", "", None, "intern"),
        ("Backend Developer - Contract", "", None, "contract"),
        ("Freelance Python Developer", "", None, "contract"),
        ("Part-time Backend Engineer", "", None, "part_time"),
        (
            "Backend Engineer",
            "This is a 6-month internship with a monthly stipend.",
            None,
            "intern",
        ),
        ("Backend Engineer", "We also run an internship program.", None, "full_time"),
        ("Backend Engineer", "", "Internship", "intern"),
        ("Backend Engineer", "", "Full-time", "full_time"),
        ("Backend Engineer", "", "Contract", "contract"),
        ("Intern", "", "Full-time", "full_time"),  # structured hint wins
    ],
)
def test_employment_type(title, description, hint, expected):
    assert classify_employment(title, description, hint=hint) == expected
