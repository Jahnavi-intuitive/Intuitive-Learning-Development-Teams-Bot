"""Models for the L&D portal database (lnd_portal) — real org certification
data ingested from REAL EXCEL SHEETS (Anthropic, Gemini, AWS, Databricks).

One denormalized fact table, not a snowflake of employee/certification
dimension tables: none of the 4 source files share a reliable join key
(names/emails are typo'd, re-cased, and span two email domains — see
scripts/ingest_real_excel_data.py's cleaning pass), so a strict FK-based
employee dimension would either invent false identity matches or refuse
rows the source data genuinely contains. Every record instead carries its
own denormalized person/practice fields (raw as scanned, plus a best-effort
canonical form for grouping), mirroring how CertificateRecord in the main
schema already snapshots employee_name/practice rather than trusting a join.
"""

import enum

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.portal_db import PortalBase


class Provider(str, enum.Enum):
    anthropic = "anthropic"
    gemini = "gemini"
    aws = "aws"
    databricks = "databricks"
    # Added for the provider leaderboard/heatmap/drill-down dashboard —
    # these 4 have no rows in certification_records yet (no source Excel
    # sheet ingested for them), but the dashboard shows all 8 fixed-color
    # providers per direct instruction, reading a real, honest 0 rather
    # than hiding the column until data exists.
    azure = "azure"
    bedrock = "bedrock"
    google = "google"
    snowflake = "snowflake"


class StatusCanonical(str, enum.Enum):
    completed = "completed"
    in_progress = "in_progress"
    not_cleared = "not_cleared"
    unknown = "unknown"


class CertificationRecord(PortalBase):
    __tablename__ = "certification_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    provider: Mapped[Provider] = mapped_column(Enum(Provider, name="provider"), nullable=False, index=True)
    # Which cert track/sheet within the provider file this row came from —
    # e.g. Gemini's 3 tracks ("Gemini Agent Development", "GCP GenAI Leader"),
    # or the source Excel sheet name for Databricks. Null where the whole
    # file is one cert (Anthropic).
    source_sheet: Mapped[str | None] = mapped_column(String(255))

    certification_name: Mapped[str | None] = mapped_column(String(255), index=True)
    certification_level: Mapped[str | None] = mapped_column(String(63))  # AWS: Foundation/Associate/Professional/Specialty

    person_name: Mapped[str] = mapped_column(String(255), nullable=False)
    person_email: Mapped[str | None] = mapped_column(String(255))
    person_email_normalized: Mapped[str | None] = mapped_column(String(255), index=True)  # lowercased + trimmed

    practice_raw: Mapped[str | None] = mapped_column(String(255))
    practice_canonical: Mapped[str | None] = mapped_column(String(127), index=True)

    manager_name: Mapped[str | None] = mapped_column(String(255))
    pd_leader_name: Mapped[str | None] = mapped_column(String(255))

    status_raw: Mapped[str | None] = mapped_column(String(255))
    status_canonical: Mapped[StatusCanonical] = mapped_column(
        Enum(StatusCanonical, name="status_canonical"), nullable=False, index=True
    )

    prep_start_date: Mapped[object | None] = mapped_column(Date)
    exam_date: Mapped[object | None] = mapped_column(Date)
    submission_date: Mapped[object | None] = mapped_column(Date)
    completion_date: Mapped[object | None] = mapped_column(Date)
    target_date: Mapped[object | None] = mapped_column(Date)
    expiry_date: Mapped[object | None] = mapped_column(Date)

    certificate_number: Mapped[str | None] = mapped_column(String(127))
    comments: Mapped[str | None] = mapped_column(Text)

    consent_given: Mapped[bool | None] = mapped_column(Boolean)  # AWS Skill Builder consent
    total_certs_held: Mapped[int | None] = mapped_column(Integer)  # AWS "Total Certifications Held"
    certs_at_this_level: Mapped[int | None] = mapped_column(Integer)  # AWS "Certifications at This Level"

    # Known source-data defects this row tripped during ingestion (e.g. the
    # Anthropic sheet's one row with a date string leaked into the Practice
    # column) — null for every normal row. Never silently dropped or
    # guessed-around; flagged so the dashboard/API can choose to surface or
    # exclude it instead of quietly miscounting.
    data_quality_flag: Mapped[str | None] = mapped_column(String(255))

    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_row: Mapped[dict] = mapped_column(JSON, nullable=False)  # original row, for audit/debugging

    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderTarget(PortalBase):
    """Org-set certified-employee goal per provider, for the leaderboard's
    progress-to-target bars (Section 1 of the portal Dashboard). A provider
    with no row here has genuinely no target set yet — the dashboard shows
    an explicit "no target set" state rather than a fabricated number.
    Admin-editable via routers/portal_admin.py, audited in the *main*
    app's audit_log (see that router's docstring for why this can't just
    reuse services/audited_crud.py directly)."""

    __tablename__ = "provider_targets"

    provider: Mapped[Provider] = mapped_column(Enum(Provider, name="provider"), primary_key=True)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
