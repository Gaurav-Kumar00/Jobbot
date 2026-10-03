"""Technology/skill extraction against config/taxonomy.yaml."""

from __future__ import annotations

from jobbot.normalize.vocab import skill_matcher


def extract_skills(title: str, description: str) -> tuple[list[str], list[str]]:
    """Return (all skills found, skills found in the title).

    Title mentions are kept separately because "Python Developer" is a much stronger
    signal than "Python" appearing once in a nice-to-have list.
    """
    matcher = skill_matcher()
    title_skills = matcher.find(title)
    skills = list(dict.fromkeys([*title_skills, *matcher.find(description)]))
    return skills, title_skills
