"""Generates the short_name (certification_catalog.short_name) used as an
Excel sheet name for a certification — Excel caps sheet names at 31
characters and forbids \\ / ? * [ ] : , while a real certification's full
name routinely exceeds both ("AWS Certified Solutions Architect -
Professional", "Certified Information Systems Security Professional
(CISSP)"). Called from exactly one place, services/catalog_provisioning.py,
the moment a certification name is first seen (from either an Excel sync
row or an employee's own submission) — never regenerated for an existing
catalog entry, since that would silently rename its sheet out from under
whatever's already in the SharePoint file.

Strategy, in order:
1. If the name already contains a parenthetical acronym the cert is
   commonly known by (e.g. "(CISSP)"), use that directly — the most
   human-recognizable short form there could be, and it's already right
   there in the name.
2. Otherwise, build one from initials: a small explicit table expands a
   handful of common vendor names to their well-known short form first
   (not guessed — only vendors actually worth special-casing), then the
   remaining significant words each contribute their first letter.
3. If neither produces anything distinctive (very short input, or the
   result collides with an existing short_name for a *different*
   certification), fall back to a clean truncation of the real name, with
   a numeric suffix on collision.

Deliberately not "smart" beyond this — it doesn't have to be perfect, just
distinctive and legible enough for an admin skimming a list of sheet tabs;
an admin can always rename a specific one later without touching this
generator.
"""

import re

_MAX_LEN = 31
_INVALID_CHARS_PATTERN = re.compile(r"[\\/?*\[\]:]")
_PARENTHETICAL_ACRONYM_PATTERN = re.compile(r"\(([A-Z][A-Z0-9]{1,9})\)")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")

_STOPWORDS = {"of", "the", "and", "for", "in", "on", "a", "an", "to"}

# Vendor names worth expanding to their commonly-known short form before
# building initials from the rest of the name. Deliberately small and
# explicit — add to this table, don't try to infer it.
_VENDOR_ABBREVIATIONS = {
    "amazon web services": "AWS",
    "microsoft": "MS",
    "google cloud": "GCP",
    "salesforce": "SF",
}

_RESERVED_SHEET_NAMES = {"history"}  # Excel reserves this one, case-insensitively


def _sanitize(candidate: str) -> str:
    cleaned = _INVALID_CHARS_PATTERN.sub("", candidate).strip()
    cleaned = cleaned.strip("'")  # Excel also forbids a leading/trailing single quote
    if not cleaned or cleaned.lower() in _RESERVED_SHEET_NAMES:
        cleaned = "Cert"
    return cleaned[:_MAX_LEN]


def _apply_vendor_abbreviations(name: str) -> str:
    working = name
    for phrase, abbrev in _VENDOR_ABBREVIATIONS.items():
        working = re.sub(rf"\b{re.escape(phrase)}\b", abbrev, working, flags=re.IGNORECASE)
    return working


def _build_from_initials(name: str) -> str:
    working = _apply_vendor_abbreviations(name)
    tokens = [t for t in _WORD_PATTERN.findall(working) if t.lower() not in _STOPWORDS]
    if not tokens:
        return ""

    # A leading token that's already acronym-shaped (e.g. "AWS", "MS" from
    # the substitution above, or a name that's already written that way)
    # is kept literally rather than reduced to its own first letter.
    if tokens[0].isupper() and 2 <= len(tokens[0]) <= 6:
        lead, rest = tokens[0], tokens[1:]
    else:
        lead, rest = None, tokens

    initials = "".join(t[0].upper() for t in rest)
    if lead and initials:
        return f"{lead}-{initials}"
    return lead or initials


def _dedupe(candidate: str, existing_short_names: set[str]) -> str:
    if candidate not in existing_short_names:
        return candidate
    base = candidate[: _MAX_LEN - 3]  # leave room for "-NN"
    for suffix in range(2, 100):
        attempt = f"{base}-{suffix}"
        if attempt not in existing_short_names:
            return attempt
    raise ValueError(f"Could not generate a unique short_name for {candidate!r} after 98 attempts.")


def generate_short_name(long_name: str, existing_short_names: set[str]) -> str:
    """long_name: the certification's real, full name (certification_catalog.name).
    existing_short_names: every short_name already in use, so the result is
    guaranteed unique against them (case-sensitive — Excel sheet names are
    themselves case-sensitive for this purpose)."""
    match = _PARENTHETICAL_ACRONYM_PATTERN.search(long_name)
    if match:
        candidate = match.group(1)
    else:
        candidate = _build_from_initials(long_name)
        if len(candidate) < 2:
            candidate = long_name  # not distinctive enough — fall back to truncation below

    candidate = _sanitize(candidate)
    return _dedupe(candidate, existing_short_names)
