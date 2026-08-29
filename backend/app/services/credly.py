"""Credly badge verification.

Structure CONFIRMED against two real earner badge URLs (not a template
page) on 2026-08-24:
  - The badge page's body is almost entirely JavaScript-rendered. A plain
    HTTP GET returns only a "please enable JavaScript" notice as visible
    text — none of "Issued to", "Issued on", "Expires on" etc. exist as
    static HTML on a real earner page. Regex-over-visible-text (this
    module's first version) never matches on a real page; it only ever
    matched on the unrelated template page it was originally tested
    against. Getting the dates would require a headless browser or
    Credly's internal JSON API — neither is in scope here.
  - The one reliably server-rendered field is the `og:title` meta tag, and
    on an earner page it is a full sentence, not just the badge name:
        "<Badge Name> was issued by <Issuer> to <Recipient Name>."
    e.g. "Certified Partner Specialist Gemini Enterprise Agent Development
    was issued by Google Cloud to Puja Rachchh." — confirmed verbatim from
    a real badge. On a badge *template* page (no earner), og:title is just
    the plain badge name with no "was issued by/to" clause — handled as a
    fallback below.
  - `og:description` carries the badge's marketing description, not
    anything usable for verification.

Because of this, extracted issued_date/expiration_date are always None —
that's an honest limitation, not a bug. Verification confidence rests on
the recipient-name and badge-name fuzzy matches only, and
verification.perform_verification already falls back to the submission
form's expiration_date when Credly doesn't supply one.

Safety property that does NOT depend on any of the above being exactly
right: if the og:title sentence can't be parsed into a recipient name, or
either fuzzy match is below threshold, the outcome is "exception" — never
"verified". Quietly defaulting to verified is the failure mode the BRD
calls out (Sections 3.1/7.2), not defaulting to manual review.
"""

import re
from dataclasses import dataclass
from datetime import date

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from app.config import settings

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# "<badge> was issued by <issuer> to <recipient>." — confirmed verbatim
# og:title format on a real earner page (see module docstring).
_TITLE_SENTENCE = re.compile(
    r"^(?P<badge>.+?)\s+was issued by\s+(?P<issuer>.+?)\s+to\s+(?P<recipient>.+?)\.?\s*$",
    re.IGNORECASE,
)


@dataclass
class CredlyExtraction:
    ok: bool
    recipient_name: str | None
    badge_name: str | None
    issuer: str | None
    issued_date: date | None  # always None today — see module docstring
    expiration_date: date | None  # always None today — see module docstring
    error: str | None = None


def fetch_and_extract(url: str) -> CredlyExtraction:
    try:
        resp = httpx.get(
            url, timeout=15, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return CredlyExtraction(
            ok=False,
            recipient_name=None,
            badge_name=None,
            issuer=None,
            issued_date=None,
            expiration_date=None,
            error=f"fetch_failed: {exc}",
        )

    soup = BeautifulSoup(resp.text, "lxml")

    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    og_title = og_title_tag["content"].strip() if og_title_tag and og_title_tag.get("content") else None

    badge_name = None
    issuer = None
    recipient_name = None

    if og_title:
        m = _TITLE_SENTENCE.match(og_title)
        if m:
            badge_name = m.group("badge").strip()
            issuer = m.group("issuer").strip()
            recipient_name = m.group("recipient").strip()
        else:
            # No "was issued by ... to ..." clause — this is a badge
            # *template* page (nobody's earned it), not an earner's page.
            badge_name = og_title
    elif soup.title and soup.title.string:
        badge_name = soup.title.string.replace(" - Credly", "").strip()

    return CredlyExtraction(
        ok=badge_name is not None,
        recipient_name=recipient_name,
        badge_name=badge_name,
        issuer=issuer,
        issued_date=None,
        expiration_date=None,
    )


@dataclass
class VerificationOutcome:
    status: str  # "verified" | "exception"
    extraction: CredlyExtraction
    name_match_score: float | None
    cert_match: bool | None


def verify_against_credly(
    url: str, *, expected_name: str, expected_cert_name: str
) -> VerificationOutcome:
    extraction = fetch_and_extract(url)

    if not extraction.ok or not extraction.recipient_name or not extraction.badge_name:
        return VerificationOutcome(
            status="exception", extraction=extraction, name_match_score=None, cert_match=None
        )

    # token_set_ratio, not token_sort_ratio: badge_name on the page often
    # carries extra qualifier tokens the submission's cert_name won't have
    # (e.g. "Certified Partner Specialist <X>" vs. just "<X>") — set-based
    # comparison doesn't penalize the shorter string for that, sorted-string
    # comparison does.
    name_score = fuzz.token_set_ratio(expected_name.lower(), extraction.recipient_name.lower()) / 100.0
    cert_score = fuzz.token_set_ratio(expected_cert_name.lower(), extraction.badge_name.lower()) / 100.0
    cert_match = cert_score >= settings.verification_confidence_threshold

    confident = name_score >= settings.verification_confidence_threshold and cert_match
    status = "verified" if confident else "exception"

    return VerificationOutcome(
        status=status, extraction=extraction, name_match_score=name_score, cert_match=cert_match
    )
