"""Shared vocabulary for the next-certification recommender: a canonical
level ladder and a small skill-domain taxonomy, plus the normalizers that
map free-text partner data onto them.

Why a taxonomy at all (the Spotify "Technology Career Steps" lesson): a
career ladder has to be expressed in terms of *levels within a skill
area*, not in terms of one practice's hand-drawn list of certifications.
Once every certification — whichever partner issues it — is placed on
(domain, level), cross-partner evidence becomes usable: an AWS Solutions
Architect Associate is real evidence of associate-level cloud
architecture, so Azure Fundamentals is correctly identified as a skip for
that person rather than recommended as their "next step".

Levels in partner data are free text today (see the seed JSON files: "Associate
(Intermediate)", "Foundations", "Professional / Architect", ...), and portal
tracker rows mostly carry no level at all — so normalization falls back to
the certification's own name, which in practice encodes its level for
every partner in scope (AWS, Google/Gemini, Anthropic, Databricks, Azure).
An explicit level on a partner_certifications row always wins over
inference.
"""

import re

# --- Level ladder ------------------------------------------------------------
# rank 0 is "no knowledge" on the proficiency scale; certifications start at 1.
LEVELS = {
    "foundational": 1,
    "associate": 2,
    "professional": 3,
    # Specialty sits at professional rank on the ladder (AWS/Azure both
    # position specialties as post-associate depth credentials).
    "specialty": 3,
    "expert": 4,
}
RANK_LABELS = {0: "none", 1: "foundational", 2: "associate", 3: "professional", 4: "expert"}

# Evaluated in order — first match wins. "foundation" is checked before
# "associate" on purpose: Anthropic's "Claude Certified Associate -
# Foundations" is an entry credential, not an associate-tier one.
_LEVEL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("expert", re.compile(r"\bexpert\b|\badvanced\b")),
    ("professional", re.compile(r"\bprofessional\b(?!\s+certificate)")),
    ("specialty", re.compile(r"\bspecialty\b|\bspecialist\b")),
    ("foundational", re.compile(
        r"foundation|fundamental|practitioner|\bleader\b|essentials|\bentry\b|\bcore\b|microcredential|beginner"
    )),
    ("associate", re.compile(r"\bassociate\b|\bintermediate\b|\bdeveloper\b|\bengineer\b|\badministrator\b")),
]


def normalize_level(level_text: str | None, certification_name: str | None = None) -> str | None:
    """Canonical level name for a certification, or None if genuinely
    undeterminable. Tries the explicit level text first, then the name."""
    for text in (level_text, certification_name):
        if not text:
            continue
        lowered = text.lower()
        if lowered in LEVELS:
            return lowered
        for label, pattern in _LEVEL_PATTERNS:
            if pattern.search(lowered):
                return label
    return None


def level_rank(level: str | None) -> int:
    """Unknown level is treated as associate (rank 2) — the median tier —
    rather than 0, so an unlabelled certification is neither assumed
    trivially skippable nor assumed out of reach."""
    return LEVELS.get(level or "", 2)


# --- Skill domains -----------------------------------------------------------
DOMAINS = {
    "cloud_architecture": "Cloud architecture & infrastructure",
    "devops": "DevOps, platform & operations",
    "software_development": "Application / software development",
    "data_engineering": "Data engineering & data platforms",
    "data_analytics": "Data analytics & BI",
    "machine_learning": "Machine learning",
    "generative_ai": "Generative AI & agents",
    "security": "Security",
    "networking": "Networking",
    "database": "Databases",
}

_DOMAIN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("generative_ai", re.compile(
        r"generative|\bgen ?ai\b|\bllm|claude|gemini|\bagent|bedrock|prompt|\bai practitioner|\bai engineer|"
        r"\bai leader|\bai apps|\bai-\d|azure ai|\bai\b"
    )),
    ("machine_learning", re.compile(r"machine learning|\bml\b|mlops|data scien")),
    ("data_engineering", re.compile(r"data engineer|\betl\b|lakehouse|spark|data platform|fabric data")),
    ("data_analytics", re.compile(r"analyst|analytics|power bi|\bbi\b|tableau|looker")),
    ("security", re.compile(r"security|cyber|fortinet|\bnse\b|identity")),
    ("networking", re.compile(r"network|ccna|ccnp")),
    ("database", re.compile(r"database|\bsql\b|\bdba\b|cosmos")),
    ("devops", re.compile(r"devops|sysops|kubernetes|terraform|\bcka\b|\bckad\b|site reliability|administrator")),
    ("software_development", re.compile(r"developer|application|django|back-end|front-end|software")),
    ("cloud_architecture", re.compile(
        r"architect|cloud practitioner|cloud engineer|cloud digital leader|azure fundamentals|\bcloud\b|solutions"
    )),
]

# Used only when nothing in the name/level matches any domain pattern.
_PROVIDER_DEFAULT_DOMAIN = {
    "aws": "cloud_architecture",
    "azure": "cloud_architecture",
    "gemini": "cloud_architecture",
    "databricks": "data_engineering",
    "anthropic": "generative_ai",
}


def infer_domains(certification_name: str | None, provider: str | None = None) -> tuple[str, ...]:
    """Every domain whose keywords appear in the certification name, most
    specific first (pattern order). Falls back to the provider's default
    domain, so every certification always lands in at least one domain."""
    lowered = (certification_name or "").lower()
    found = [domain for domain, pattern in _DOMAIN_PATTERNS if pattern.search(lowered)]
    if not found and provider in _PROVIDER_DEFAULT_DOMAIN:
        found = [_PROVIDER_DEFAULT_DOMAIN[provider]]
    return tuple(found)


def infer_domains_from_role(role: str | None) -> set[str]:
    """Job-title → domains, reusing the same keyword taxonomy (a "Data
    Engineer" role maps to data_engineering exactly as a "Data Engineer
    Associate" certification does)."""
    return set(infer_domains(role)) if role else set()


# Designation → seniority band, for when there's no experience on file.
# Checked in order; first match wins. "manag" (not "manager") so a typo'd
# directory title like "Managar" still reads as a manager.
_TITLE_SENIORITY: list[tuple[str, re.Pattern]] = [
    ("early_career", re.compile(r"\bintern\b|\btrainee\b|\bjunior\b|\bjr\b|\bfresher\b|\bgraduate\b|\bapprentice\b")),
    ("senior", re.compile(
        r"manag|\blead\b|\bsenior\b|\bsr\b|principal|architect|director|\bhead\b|\bvp\b|vice president|"
        r"\bstaff\b|\bchief\b|\bcto\b|\bcio\b|partner"
    )),
    ("mid_career", re.compile(r"engineer|developer|consultant|analyst|specialist|administrator|scientist")),
]


def seniority_from_title(role: str | None) -> str | None:
    """Seniority band implied by a job title, or None if the title says
    nothing about level (or there's no title)."""
    lowered = (role or "").lower()
    for band, pattern in _TITLE_SENIORITY:
        if pattern.search(lowered):
            return band
    return None


# Practice (department) → the skill domains that practice works in. Used
# for relevance when the job title alone names no domain (e.g. "Manager").
_PRACTICE_DOMAINS = {
    "app+dbmod": {"software_development", "database", "cloud_architecture", "devops"},
    "data+ai": {"data_engineering", "data_analytics", "machine_learning", "generative_ai"},
    "netsec+sdx": {"security", "networking"},
    "cloud+platformengineer": {"cloud_architecture", "devops"},
}


def domains_for_practice(practice: str | None) -> set[str]:
    return set(_PRACTICE_DOMAINS.get(normalize_practice(practice) or "", ()))


def match_practice(department: str | None, practices: tuple[str, ...]) -> str | None:
    """The practice a free-text department names, compared on the same
    whitespace/&-insensitive form as normalize_practice ("Data + AI" →
    "Data+AI"). None when it matches none of them."""
    key = normalize_practice(department)
    if not key:
        return None
    for p in practices:
        if normalize_practice(p) == key:
            return p
    return None


# --- Identity / name normalization -------------------------------------------
_PRACTICE_ALIASES = {
    # main-app enum value vs. portal canonical value for the same practice
    "cloud+platformengineering": "cloud+platformengineer",
}


def normalize_practice(practice: str | None) -> str | None:
    """"Data+AI" (main app enum) and "Data + AI" (portal canonical) are the
    same practice — compare on a whitespace-free, &→+ form."""
    if not practice:
        return None
    key = re.sub(r"\s+", "", practice.replace("&", "+")).lower()
    return _PRACTICE_ALIASES.get(key, key)


def cert_key(provider: str | None, certification_name: str) -> tuple[str, str]:
    """Identity of one certification across both databases."""
    name = re.sub(r"[\s–—-]+", " ", certification_name).strip().lower()
    return ((provider or "").lower(), name)


def parse_proficiency(value) -> int:
    """Accepts 0-4 or a level label ("associate") for self-declared
    expertise, so the agent can pass whichever it has."""
    if isinstance(value, bool):
        raise ValueError("proficiency must be 0-4 or a level name")
    if isinstance(value, (int, float)):
        rank = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        rank = int(value.strip())
    elif isinstance(value, str) and value.strip().lower() in LEVELS:
        rank = LEVELS[value.strip().lower()]
    elif isinstance(value, str) and value.strip().lower() in ("none", "beginner"):
        rank = 0
    else:
        raise ValueError(f"unrecognized proficiency {value!r}")
    if not 0 <= rank <= 4:
        raise ValueError("proficiency must be between 0 and 4")
    return rank
