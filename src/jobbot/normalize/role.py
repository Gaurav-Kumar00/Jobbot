"""Role category, seniority and employment type from the job title (plus light JD signals)."""

from __future__ import annotations

import re

# Ordered: the first matching category wins. More specific families come before the
# generic "software engineer" bucket, and non-engineering titles come first so
# "Technical Account Manager" or "Product Manager" never count as engineering.
_CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("non_software", re.compile(
        r"\b(sales|marketing|account (?:manager|executive|director)|relationship manager"
        r"|business development|bd[re]?|finance|accountant|accounts|audit|legal|counsel|hr"
        r"|human resources|recruit\w*|talent acquisition|people (?:partner|ops)|payroll"
        r"|customer (?:support|success|service|experience)"
        r"|support (?:specialist|executive|associate)"
        r"|operations (?:manager|executive|associate|analyst)|ops (?:manager|executive)"
        r"|content|copywriter|writer|editor|video|social media|seo|brand|designer|ux|ui/ux"
        r"|product manager|product owner|program manager|project manager|scrum master"
        r"|business analyst|consultant|solutions? (?:consultant|architect|specialist)"
        r"|pre-?sales|partnerships?|alliances?|banking solutions|growth|community|compliance|risk"
        r"|procurement|admin\w*|receptionist|office manager|chief of staff|strategy|coordinator"
        r"|(?:technical )?support engineer|technical support|(?:technical|professional) services"
        r"|services engineer|customer engineer|implementation engineer|solutions engineer"
        r"|sales engineer|field engineer|developer advocate|developer relations|devrel"
        r"|developer evangelist|service desk|help ?desk|it engineer|it support|desktop support"
        r"|value engineer|solutions? engineer|gtm engineer|go-to-market"
        r"|(?<!cloud )operations engineer|sustenance engineer|application support"
        r"|production support)\b"
    )),
    ("management", re.compile(
        r"\b(engineering manager|manager,? engineering|em|director|head of|vp|vice president"
        r"|cto|chief technology officer|tech(?:nical)? manager|development manager|manager)\b"
    )),
    ("ml_research", re.compile(
        r"\b(research (?:scientist|engineer)|applied scientist|research fellow|phd)\b"
    )),
    ("data_science", re.compile(
        r"\b(data scientist|data analyst|business intelligence|bi (?:developer|engineer|analyst)"
        r"|analytics (?:engineer|analyst)|statistician|data science|analyst)\b"
    )),
    ("qa", re.compile(
        r"\b(qa|sdet|quality assurance|quality engineer|test(?:ing)? engineer|tester"
        r"|test automation|engineer testing|automation (?:&|and) quality|quality focus"
        r"|automation (?:test|qa)\w*|engineer in test)\b"
    )),
    ("security", re.compile(
        r"\b(security|cyber\s?security|appsec|infosec|penetration|soc analyst|vulnerability)\b"
    )),
    ("mobile", re.compile(
        r"\b(android|ios|mobile|flutter|react native|swift|kotlin developer)\b"
    )),
    ("embedded", re.compile(
        r"\b(embedded|firmware|vlsi|asic|fpga|hardware|rtl|silicon|electronics|pcb)\b"
    )),
    ("systems", re.compile(
        r"\b(kernel|systems software|system software|file ?systems?|filesystem|device drivers?"
        r"|storage engineer|os engineer|hypervisor|virtualization engineer|compiler)\b"
    )),
    ("devops", re.compile(
        r"\b(devops|dev ops|devsecops|sre|site reliability|cloud engineer|infrastructure engineer"
        r"|infra engineer|release engineer|build engineer|systems administrator|sysadmin"
        r"|network engineer|dba|database administrator|kubernetes engineer|platform reliability"
        r"|cloud operations|cloud ops)\b"
    )),
    ("fullstack", re.compile(r"\b(full[\s-]?stack|mern|mean stack)\b")),
    ("frontend", re.compile(
        r"\b(front[\s-]?end|ui (?:software )?(?:engineer|developer)"
        r"|react(?:\.?js)? (?:developer|engineer)"
        r"|angular (?:developer|engineer)|vue(?:\.?js)? (?:developer|engineer)|web designer)\b"
    )),
    ("data_engineering", re.compile(
        r"\b(data engineer|data platform|etl|big data|data pipeline|analytics platform)\b"
    )),
    ("ai_engineering", re.compile(
        r"\b(ai engineer|ml engineer|machine learning engineer|llm|genai|gen ai|generative ai"
        r"|ai/ml|ml/ai|mlops|applied ai|ai developer|nlp engineer|computer vision engineer)\b"
    )),
    ("backend", re.compile(
        r"\b(back[\s-]?end|server[\s-]side|api (?:developer|engineer)|python (?:developer|engineer)"
        r"|django|fastapi|flask|golang|go (?:developer|engineer)|java (?:developer|engineer)"
        r"|node(?:\.?js)? (?:developer|engineer)|ruby (?:developer|engineer)|php developer"
        r"|\.net developer|scala (?:developer|engineer)|rust (?:developer|engineer)|microservices?"
        r"|distributed systems|platform engineer|integration engineer|payments engineer)\b"
    )),
    ("software_generic", re.compile(
        r"\b(software|sde|swe|developer|programmer|member of technical staff|mts|engineer"
        r"|coder|application engineer|product engineer|founding engineer)\b"
    )),
]  # fmt: skip

_SENIORITY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op|summer analyst)\b")),
    ("manager", re.compile(r"\b(manager|director|head of|vp|vice president|cto)\b")),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b")),
    ("staff", re.compile(r"(?<!technical )\b(staff|sde[\s-]?(?:iv|4)|level\s*[5-9]|l[5-9])\b")),
    ("lead", re.compile(r"\b(lead|tech lead|team lead|architect)\b")),
    ("senior", re.compile(
        r"\b(senior|sr|snr|sde[\s-]?(?:iii|3)|(?:engineer|developer|swe)[\s-]+(?:iii|3)"
        r"|level\s*4|l4)\b|\biii$"
    )),
    ("mid", re.compile(
        r"\b(sde[\s-]?(?:ii|2)|mts[\s-]?(?:ii|2)|(?:engineer|developer|swe)[\s-]+(?:ii|2)"
        r"|mid[\s-]?level|intermediate|level\s*3|l3)\b|\bii$"
    )),
    ("junior", re.compile(
        r"\b(junior|jr|associate (?:software|backend|engineer|developer)|entry[\s-]level|graduate"
        r"|new grad|fresher|trainee|apprentice|get|sde[\s-]?(?:i|1)|mts[\s-]?(?:i|1)"
        r"|(?:engineer|developer|swe)[\s-]+(?:i|1)|level\s*[12]|l[12])\b|\bi$"
    )),
]  # fmt: skip

_EMPLOYMENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op|apprentice(?:ship)?)\b")),
    (
        "contract",
        re.compile(
            r"\b(contract|contractual|contractor|freelance|freelancer|temporary|temp|fixed[\s-]term|ftc"
            r"|consultant \(contract\))\b"
        ),
    ),
    ("part_time", re.compile(r"\b(part[\s-]?time)\b")),
]
_HINT_EMPLOYMENT = {
    "intern": "intern",
    "internship": "intern",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "contract",
    "freelance": "contract",
    "part-time": "part_time",
    "part time": "part_time",
    "parttime": "part_time",
    "full-time": "full_time",
    "full time": "full_time",
    "fulltime": "full_time",
    "permanent": "full_time",
}
_DESC_INTERN = re.compile(
    r"\b(this is an? (?:\d+[\s-]month )?internship|internship duration|monthly stipend"
    r"|stipend of)\b"
)
_TITLE_NOISE = re.compile(r"[^a-z0-9+#./\s-]")


def clean_title(title: str) -> str:
    return " ".join(_TITLE_NOISE.sub(" ", title.lower()).split())


def classify_role(title: str) -> str:
    cleaned = clean_title(title)
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(cleaned):
            return category
    return "unknown"


def classify_seniority(title: str) -> str:
    cleaned = clean_title(title)
    for level, pattern in _SENIORITY_RULES:
        if pattern.search(cleaned):
            return level
    return "unknown"


def classify_employment(title: str, description: str = "", hint: str | None = None) -> str:
    # An explicit title ("SDE-1 (FTC)", "Backend Intern") beats a generic structured field
    # (Amazon marks fixed-term contracts as "full-time").
    cleaned = clean_title(title)
    for kind, pattern in _EMPLOYMENT_RULES:
        if pattern.search(cleaned):
            return kind
    if hint and (mapped := _HINT_EMPLOYMENT.get(hint.strip().lower())):
        return mapped
    if _DESC_INTERN.search(description.lower()):
        return "intern"
    return "full_time"
