"""One-shot ingestion of the 4 real certification-tracker workbooks in
"REAL EXCEL SHEETS/" into the lnd_portal database (backend/app/portal_models.py).

Creates the lnd_portal database itself (if missing) and its tables, then
truncates and reloads certification_records — safe/idempotent to re-run
whenever a fresher export of the same 4 files is dropped into that folder.

Every source file has its own shape and its own mess (see the structural
notes inline below, and DEVELOPMENT_LOG.md); this script's cleaning rules
were derived by first reading every sheet of every file directly (headers,
sample rows, distinct status/practice values, corrupted rows) rather than
assumed, since this is real org data.

Run from the repo root, with the lnd_portal Postgres server reachable
(same host as lnd_certification): `python scripts/ingest_real_excel_data.py`
"""

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import psycopg
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parent.parent
EXCEL_DIR = REPO_ROOT / "REAL EXCEL SHEETS"
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.portal_db import PortalBase, PortalSessionLocal, portal_engine  # noqa: E402
from app.portal_models import CertificationRecord, Provider, StatusCanonical  # noqa: E402


# --------------------------------------------------------------------------
# generic cleaning helpers
# --------------------------------------------------------------------------

def _is_na(value) -> bool:
    """pd.isna() handles None/NaN/NaT/pd.NA uniformly — unlike an
    `isinstance(value, float)` guard, which misses pd.NaT entirely (NaT is,
    surprisingly, an instance of datetime.date/datetime.datetime, so it
    would otherwise slip through and reach date-object handling below)."""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def clean_str(value) -> str | None:
    """Trim whitespace (incl. non-breaking spaces) and collapse internal
    runs of whitespace — several source rows have double spaces or a
    leading \\xa0 (e.g. 'Aniket  Purohit ', '\\xa0jcntc6jwdjur')."""
    if _is_na(value):
        return None
    s = str(value).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def normalize_email(value) -> str | None:
    s = clean_str(value)
    if not s or "@" not in s:
        return None
    return s.lower()


_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y")


def parse_date(value) -> date | None:
    if _is_na(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = clean_str(value)
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None  # unparseable string (shouldn't happen given the sheets scanned) — left null, not guessed


def json_safe(value):
    if _is_na(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def raw_row_dict(row: pd.Series) -> dict:
    return {str(k): json_safe(v) for k, v in row.items()}


# --------------------------------------------------------------------------
# practice canonicalization — real Practice values are free text and spelled
# inconsistently everywhere (casing, "&" vs "+", abbreviations). Only
# collapse spellings that are unambiguously the same practice; anything not
# recognized here is kept as its own bucket (cleaned, not guessed away).
# --------------------------------------------------------------------------

def _practice_key(raw: str) -> str:
    s = raw.replace("&amp;", "+").replace("&", "+")
    s = re.sub(r"\s*\+\s*", "+", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


_PRACTICE_CANONICAL_MAP = {
    "data+ai": "Data + AI",
    "cloud+platform engineering": "Cloud + Platform Engineering",
    "cloud+platform eng": "Cloud + Platform Engineering",  # Databricks' abbreviated form
    "engineering": "Engineering",
    "cybersecurity": "Cybersecurity",
    "app+db mod": "App + DB Mod",
    "network engineering": "Network Engineering",
    "netsec+sdx": "NetSec + SDx",
    "netsec": "NetSec",
    "pmo": "PMO",
    "dataops/dbre": "DataOps/DBRE",
    "cms": "CMS",
    "cybersec+sdx": "Cybersec + SDx",
    "cloud native": "Cloud Native",
}

_LOOKS_LIKE_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def canonicalize_practice(raw: str | None) -> tuple[str | None, str | None]:
    """Returns (canonical, data_quality_flag)."""
    if not raw:
        return None, None
    if _LOOKS_LIKE_DATE.fullmatch(raw):
        # Known Anthropic-sheet defect: one row has a date string leaked
        # into the Practice column (a column-alignment slip for that row).
        return "Unknown", "practice_field_corrupted_contains_date"
    canonical = _PRACTICE_CANONICAL_MAP.get(_practice_key(raw))
    return (canonical or raw), None


# --------------------------------------------------------------------------
# database bootstrap
# --------------------------------------------------------------------------

def ensure_portal_database_exists():
    url = make_url(settings.portal_database_url)
    maintenance_conn = psycopg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password,
        dbname="postgres", autocommit=True,
    )
    try:
        exists = maintenance_conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
        ).fetchone()
        if not exists:
            maintenance_conn.execute(f'CREATE DATABASE "{url.database}"')
            print(f"Created database {url.database!r}")
        else:
            print(f"Database {url.database!r} already exists")
    finally:
        maintenance_conn.close()


# --------------------------------------------------------------------------
# per-provider readers — each returns a list of CertificationRecord kwargs dicts
# --------------------------------------------------------------------------

ANTHROPIC_STATUS_MAP = {
    "Certified": StatusCanonical.completed,
    "In Progress": StatusCanonical.in_progress,
    "Attempted - Not Cleared": StatusCanonical.not_cleared,
}

DATABRICKS_CERT_NAME_MAP = {
    "databricks certified data analyst associate": "Databricks Certified Data Analyst Associate",
    "databricks certified machine learning associate": "Databricks Certified Machine Learning Associate",
    "databricks certified generative ai engineer associate": "Databricks Certified Generative AI Engineer Associate",
    "databricks certified data engineer associate": "Databricks Certified Data Engineer Associate",
    "databricks certified data engineer professional": "Databricks Certified Data Engineer Professional",
    "databricks certified data engineer assoociate": "Databricks Certified Data Engineer Associate",  # typo
    "databricks associate data engineer": "Databricks Certified Data Engineer Associate",  # word-order swap
    "databricks certified machine learning professional": "Databricks Certified Machine Learning Professional",
    "databricks certified data engineer": None,  # truncated in source; level not determinable, left unspecified
}

AWS_CONSENT_MAP = {
    "Yes, consent given": True,
    "No, not yet": False,
}


def read_anthropic() -> list[dict]:
    path = EXCEL_DIR / "Anthropic_Claude_Architect_Certification_Tracker (2).xlsx"
    df = pd.read_excel(path, sheet_name="Detailed-Certification Tracker")
    records = []
    for _, row in df.iterrows():
        name = clean_str(row.get("Name"))
        if not name:
            continue
        practice_raw = clean_str(row.get("Practice"))
        practice_canonical, dq_flag = canonicalize_practice(practice_raw)
        status_raw = clean_str(row.get("Status"))
        records.append(dict(
            provider=Provider.anthropic,
            source_sheet=None,
            certification_name="Claude Architect Certification",
            certification_level=None,
            person_name=name,
            person_email=clean_str(row.get("Email ID")),
            person_email_normalized=normalize_email(row.get("Email ID")),
            practice_raw=practice_raw,
            practice_canonical=practice_canonical,
            manager_name=clean_str(row.get("Manager")),
            pd_leader_name=clean_str(row.get("PD/Leader")),
            status_raw=status_raw,
            status_canonical=ANTHROPIC_STATUS_MAP.get(status_raw, StatusCanonical.unknown),
            prep_start_date=parse_date(row.get("Prep Start Date")),
            exam_date=parse_date(row.get("Exam Date")),
            submission_date=parse_date(row.get("Cerification submission Date")),
            completion_date=None,
            target_date=None,
            expiry_date=parse_date(row.get("Expiry Date")),
            certificate_number=clean_str(row.get("Certificate Number")),
            comments=clean_str(row.get("Comments")),
            consent_given=None,
            total_certs_held=None,
            certs_at_this_level=None,
            data_quality_flag=dq_flag,
            source_file="Anthropic_Claude_Architect_Certification_Tracker (2).xlsx",
            raw_row=raw_row_dict(row),
        ))
    return records


def read_gemini() -> list[dict]:
    path = EXCEL_DIR / "Gemini Certification Tracker (1).xlsx"
    sheets = ["Gemini Agent Development", "Gemini Enterprise Deployment", "GCP GenAI Leader"]
    records = []
    for sheet in sheets:
        df = pd.read_excel(path, sheet_name=sheet)
        for _, row in df.iterrows():
            name = clean_str(row.get("Name"))
            status_raw = clean_str(row.get("Status"))
            if not name or status_raw != "Certified":
                continue  # blank Status = not certified in this track yet, not a certification record
            practice_raw = clean_str(row.get("Practice"))
            practice_canonical, dq_flag = canonicalize_practice(practice_raw)
            records.append(dict(
                provider=Provider.gemini,
                source_sheet=sheet,
                certification_name=sheet,
                certification_level=None,
                person_name=name,
                person_email=clean_str(row.get("Email ID")),
                person_email_normalized=normalize_email(row.get("Email ID")),
                practice_raw=practice_raw,
                practice_canonical=practice_canonical,
                manager_name=clean_str(row.get("Manager")),
                pd_leader_name=clean_str(row.get("PD/Leader")),
                status_raw=status_raw,
                status_canonical=StatusCanonical.completed,
                prep_start_date=parse_date(row.get("Prep Start Date")),
                exam_date=parse_date(row.get("Exam Date")),
                submission_date=parse_date(row.get("Cerification submission Date")),
                completion_date=None,
                target_date=None,
                expiry_date=parse_date(row.get("Expiry Date")),
                certificate_number=clean_str(row.get("Certificate Number")),
                comments=clean_str(row.get("Comments")),
                consent_given=None,
                total_certs_held=None,
                certs_at_this_level=None,
                data_quality_flag=dq_flag,
                source_file="Gemini Certification Tracker (1).xlsx",
                raw_row=raw_row_dict(row),
            ))
    return records


_PD_PRACTICE_SPLIT = re.compile(r"^(?P<pd>.*?)\s*\((?P<practice>[^)]*)\)\s*$")
_DATABRICKS_STATUS_MAP = {
    "Completed": StatusCanonical.completed,
    "In-progress": StatusCanonical.in_progress,
}


def read_databricks() -> list[dict]:
    path = EXCEL_DIR / "Databricks_Certifications_Record.xlsx"
    df = pd.read_excel(path, sheet_name="Certifications")
    # Rows 178-190 (0-indexed 177+) are an embedded pivot/totals block, not
    # employee data (Name holds strings like "Total eligible") — confirmed
    # by direct inspection. Belt-and-suspenders: also drop anything whose
    # Completed value isn't one of the two real statuses.
    df = df.iloc[:177]
    records = []
    for _, row in df.iterrows():
        name = clean_str(row.get("Name"))
        completed_raw = clean_str(row.get("Completed"))
        if not name or completed_raw not in _DATABRICKS_STATUS_MAP:
            continue
        pd_field = clean_str(row.get("Practice Director"))
        pd_leader_name, practice_raw = None, None
        if pd_field:
            m = _PD_PRACTICE_SPLIT.match(pd_field)
            if m:
                pd_leader_name, practice_raw = m.group("pd").strip() or None, m.group("practice").strip() or None
            else:
                pd_leader_name = pd_field
        practice_canonical, dq_flag = canonicalize_practice(practice_raw)
        cert_type_raw = clean_str(row.get("Cert Type"))
        cert_name = None
        if cert_type_raw:
            cert_name = DATABRICKS_CERT_NAME_MAP.get(cert_type_raw.lower(), cert_type_raw)
        comments = " | ".join(filter(None, [clean_str(row.get("Comments")), clean_str(row.get("Comment"))])) or None
        records.append(dict(
            provider=Provider.databricks,
            source_sheet="Certifications",
            certification_name=cert_name,
            certification_level=None,
            person_name=name,
            person_email=clean_str(row.get("Email ID")),
            person_email_normalized=normalize_email(row.get("Email ID")),
            practice_raw=practice_raw,
            practice_canonical=practice_canonical,
            manager_name=clean_str(row.get("Reports To")),
            pd_leader_name=pd_leader_name,
            status_raw=completed_raw,
            status_canonical=_DATABRICKS_STATUS_MAP[completed_raw],
            prep_start_date=None,
            exam_date=None,
            submission_date=None,
            completion_date=parse_date(row.get("Completion Date")),
            target_date=parse_date(row.get("Target Date")),
            expiry_date=None,
            certificate_number=None,
            comments=comments,
            consent_given=None,
            total_certs_held=None,
            certs_at_this_level=None,
            data_quality_flag=dq_flag,
            source_file="Databricks_Certifications_Record.xlsx",
            raw_row=raw_row_dict(row),
        ))
    return records


def read_aws() -> list[dict]:
    path = EXCEL_DIR / "AWS Certification Completion (1).xlsx"
    df = pd.read_excel(path, sheet_name="Certification Register", skiprows=3)
    records = []
    for _, row in df.iterrows():
        name = clean_str(row.get("Person"))
        if not name:
            continue
        consent_raw = clean_str(row.get("Skill Builder Consent"))
        total_held = row.get("Total Certifications Held")
        certs_at_level = row.get("Certifications at This Level")
        records.append(dict(
            provider=Provider.aws,
            source_sheet="Certification Register",
            certification_name=clean_str(row.get("Certification Name")),
            certification_level=clean_str(row.get("Certification Level")),
            person_name=name,
            person_email=None,
            person_email_normalized=None,
            practice_raw=None,
            practice_canonical=None,
            manager_name=None,
            pd_leader_name=None,
            status_raw=None,
            status_canonical=StatusCanonical.completed,  # the Register lists certifications already held
            prep_start_date=None,
            exam_date=None,
            submission_date=None,
            completion_date=None,
            target_date=None,
            expiry_date=None,
            certificate_number=None,
            comments=None,
            consent_given=AWS_CONSENT_MAP.get(consent_raw),
            total_certs_held=int(total_held) if pd.notna(total_held) else None,
            certs_at_this_level=int(certs_at_level) if pd.notna(certs_at_level) else None,
            data_quality_flag=None,
            source_file="AWS Certification Completion (1).xlsx",
            raw_row=raw_row_dict(row),
        ))
    return records


def dedupe_records(records: list[dict]) -> list[dict]:
    """Drop exact-duplicate rows the source sheets themselves contain (e.g.
    the Anthropic tracker has "Safir Ahamed" as two fully identical rows,
    both Certified) — counting the same real person's same certification
    twice would inflate every KPI/chart derived from this table. Keying on
    (provider, certification_name, identity, status) rather than the whole
    row: two rows for the same person/cert/status are the same real-world
    fact even if some incidental column (e.g. a stray blank Comments cell)
    differs, whereas a *different* status for the same person/cert (a
    resubmission) is a real distinct event and must NOT be deduped away."""
    seen = set()
    deduped = []
    dropped = []
    for r in records:
        # Keyed on name, not email: the Anthropic sheet has "Venkata Edara"
        # carrying "Sivapriya Jeganathan"'s email by copy-paste error — an
        # email-based key would wrongly merge these two different real
        # people and silently drop one of them.
        identity = r["person_name"].strip().lower()
        key = (r["provider"], r["certification_name"], identity, r["status_canonical"])
        if key in seen:
            dropped.append(r["person_name"])
            continue
        seen.add(key)
        deduped.append(r)
    if dropped:
        print(f"Dropped {len(dropped)} exact-duplicate record(s): {dropped}")
    return deduped


def main():
    ensure_portal_database_exists()
    PortalBase.metadata.create_all(portal_engine)

    all_records = read_anthropic() + read_gemini() + read_databricks() + read_aws()
    print(f"Parsed {len(all_records)} certification records "
          f"(anthropic/gemini/databricks/aws) from REAL EXCEL SHEETS/")
    all_records = dedupe_records(all_records)

    session = PortalSessionLocal()
    try:
        deleted = session.query(CertificationRecord).delete()
        session.bulk_insert_mappings(CertificationRecord, all_records)
        session.commit()
        print(f"Replaced {deleted} existing rows with {len(all_records)} freshly ingested rows.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
