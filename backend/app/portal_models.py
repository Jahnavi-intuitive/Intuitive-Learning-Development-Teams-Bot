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

from sqlalchemy import Boolean, Date, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db_types import JSONText, PyEnum, UTCDateTime, utcnow
from app.portal_db import PortalBase


class Provider(str, enum.Enum):
    anthropic = "anthropic"
    gemini = "gemini"  # the real source file/folder calls this "Google" — same provider, one canonical value
    aws = "aws"
    databricks = "databricks"
    # Azure: kept in the catalog per direct instruction even
    # though there's no ingested data for it yet (no source Excel sheet) —
    # the dashboard shows a real, honest 0 rather than hiding the column.
    # "google" and "bedrock" were removed here (2026-09-17): never had any
    # real data under either value (confirmed empty across
    # certification_records/provider_targets/demo_trend_backfill before the
    # enum was actually narrowed), and "google" was a duplicate of "gemini"
    # for the same real provider, not a second one.
    azure = "azure"


class StatusCanonical(str, enum.Enum):
    completed = "completed"
    in_progress = "in_progress"
    not_cleared = "not_cleared"
    unknown = "unknown"


class CertificationRecord(PortalBase):
    __tablename__ = "certification_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), nullable=False, index=True)
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
        PyEnum(StatusCanonical, name="status_canonical"), nullable=False, index=True
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
    raw_row: Mapped[dict] = mapped_column(JSONText(binary=False), nullable=False)  # original row, for audit/debugging

    # Provenance for the live SharePoint sync (services/portal_sharepoint_sync.py)
    # — every record traces back to exactly which sheet row it came from and
    # when it was last confirmed against the source, per direct instruction.
    # source_row is 1-indexed within its sheet (header row excluded), matching
    # what an admin would count opening the sheet in Excel.
    source_sheet_row: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[object | None] = mapped_column(UTCDateTime())

    created_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow)


class ProviderTarget(PortalBase):
    """Org-set certified-employee goal per provider, for the leaderboard's
    progress-to-target bars (Section 1 of the portal Dashboard). A provider
    with no row here has genuinely no target set yet — the dashboard shows
    an explicit "no target set" state rather than a fabricated number.
    Admin-editable via routers/portal_admin.py, audited in the *main*
    app's audit_log (see that router's docstring for why this can't just
    reuse services/audited_crud.py directly)."""

    __tablename__ = "provider_targets"

    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), primary_key=True)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class TrendGranularity(str, enum.Enum):
    weekly = "weekly"
    monthly = "monthly"


class DemoTrendBackfill(PortalBase):
    """Illustrative-only historical backfill for the Trends tab —
    NEVER real data. Written exactly once, by scripts/seed_demo_trend_history.py,
    never by the real ingestion pipeline (scripts/ingest_real_excel_data.py),
    and never read by the Dashboard tab, the agent, or any compliance/audit
    reporting — routers/portal_trends.py is the only reader.

    Exists because real certificate-issuance *dates* are thin or entirely
    absent for some providers today (as of this table's introduction: AWS
    and Gemini have zero dated completion records at all — neither source
    workbook ever captured a real date for a completed cert — while
    Anthropic and Databricks already have 16+ months of real dated
    history and need no backfill). A flat/undated chart demos badly, so
    this table holds a smoothed, organic-looking growth curve that always
    ends exactly at the real current total for that series.

    Each row is a *cumulative* completed-count snapshot at one period
    boundary (a Monday for weekly, the 1st for monthly) for one series —
    either a specific certification (certification_name set, for Section
    1's per-provider leading-certification chart) or a whole provider
    (certification_name NULL, for Section 2's weekly/monthly provider
    trends). The reader prefers a real computed value for any period
    where one exists and only falls back to this table where it doesn't
    — so as real dated history accumulates past this launch, seeded
    periods stop being used with zero code change, and a period already
    covered by a real record is never overwritten here.
    """

    __tablename__ = "demo_trend_backfill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), nullable=False, index=True)
    certification_name: Mapped[str | None] = mapped_column(String(255), index=True)
    granularity: Mapped[TrendGranularity] = mapped_column(PyEnum(TrendGranularity, name="trend_granularity"), nullable=False)
    period_start: Mapped[object] = mapped_column(Date, nullable=False)
    cumulative_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="seed_demo")
    created_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "provider", "certification_name", "granularity", "period_start",
            name="uq_demo_trend_backfill_period",
        ),
    )


class CertificationAlias(PortalBase):
    """Maps a raw certification identifier exactly as it appears in a
    source workbook (a sheet/tab name, or a free-text column value like
    Databricks' "Cert Type") to one canonical certification_name — so a
    newly-added sheet or a typo'd/abbreviated variant doesn't silently
    create a second, separate line on the dashboard next to the
    already-known correct one.

    Auto-populated by services/portal_sharepoint_sync.py the moment a new
    raw value is seen for a provider (canonical defaults to the raw value
    itself, cleaned up, until an admin corrects it) — recognizing a newly
    added sheet or cert-type value never requires a code change. Not yet
    admin-editable through a UI (a natural follow-up to provider_targets'
    same pattern) — updated_by stays NULL until that exists."""

    __tablename__ = "certification_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), nullable=False, index=True)
    raw_key: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("provider", "raw_key", name="uq_certification_alias_provider_raw_key"),
    )


class IngestionError(PortalBase):
    """A row from a source Excel sheet that failed validation during a
    SharePoint sync — never silently dropped, never allowed to corrupt a
    real certification_records row. Logged with enough context (source
    file, sheet, row number, the actual raw row, and why) that an admin
    can go fix the source file directly. One bad row never blocks the
    rest of that file's sync."""

    __tablename__ = "ingestion_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), nullable=False, index=True)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sheet: Mapped[str | None] = mapped_column(String(255))
    source_row: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_row: Mapped[dict] = mapped_column(JSONText(binary=False), nullable=False)
    sync_run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow)


class IngestionLog(PortalBase):
    """One row per SharePoint sync run (scheduled every few minutes, or
    admin-triggered on demand) — what happened, at a glance, without
    digging through application logs. Powers the "last successful sync
    per provider" health signal (a plain query against this table, no
    dedicated UI yet)."""

    __tablename__ = "ingestion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[object | None] = mapped_column(UTCDateTime())
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)  # "scheduled" | "manual"
    providers_synced: Mapped[dict] = mapped_column(JSONText(binary=False), nullable=False, default=list)
    rows_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class PartnerCertification(PortalBase):
    """The partner certification catalog — the only list the next-
    certification recommender (services/recommender/) chooses from. One
    row per certification a partner (AWS, Google/Gemini, Anthropic,
    Databricks, Azure) offers and the org wants people to be able to be
    recommended. Deliberately separate from certification_records: that
    table only knows certifications someone has already attempted, so a
    brand-new partner cert nobody has taken yet would otherwise be
    unrecommendable.

    level is the canonical ladder value (foundational / associate /
    professional / specialty / expert — see services/recommender/taxonomy.py);
    skill_domains are taxonomy domain keys. Both are optional: left NULL /
    empty, the recommender infers them from the certification name.
    prerequisite_names are *recommended* prior certs, never an enforced
    gate — no partner in scope hard-blocks exam registration on them.

    Seeded by scripts/seed_partner_catalog.py, admin-editable via
    routers/recommendations.py. If this table is empty, the recommender
    falls back to the distinct certifications already present in
    certification_records (and says so in its response)."""

    __tablename__ = "partner_certifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), nullable=False, index=True)
    certification_name: Mapped[str] = mapped_column(String(255), nullable=False)
    level: Mapped[str | None] = mapped_column(String(31))
    skill_domains: Mapped[list] = mapped_column(JSONText(binary=False), nullable=False, default=list)
    relevant_practices: Mapped[list] = mapped_column(JSONText(binary=False), nullable=False, default=list)
    prerequisite_names: Mapped[list] = mapped_column(JSONText(binary=False), nullable=False, default=list)
    validity_months: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "certification_name", name="uq_partner_certification_provider_name"),
    )


class PartnershipRequirement(PortalBase):
    """One partnership scale-up need: "<program> needs <required_count>
    people certified in <a specific cert | any cert at min_level or above |
    any cert in a skill domain> from <provider>", optionally scoped to one
    practice and a target date. E.g. "AWS Advanced Tier — 4 people at
    professional level by 2026-12-31", or "Databricks partnership — 10
    Data Engineer Associate".

    The current certified count is never stored — it's recomputed on every
    read from certification_records (distinct people, same identity rule as
    the portal Dashboard), so a gap can't drift stale. ProviderTarget above
    is still honored as a coarser, provider-wide requirement; this table
    adds the granularity partner programs actually specify."""

    __tablename__ = "partnership_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[Provider] = mapped_column(PyEnum(Provider, name="provider"), nullable=False, index=True)
    program_name: Mapped[str] = mapped_column(String(255), nullable=False)
    certification_name: Mapped[str | None] = mapped_column(String(255))
    min_level: Mapped[str | None] = mapped_column(String(31))
    skill_domain: Mapped[str | None] = mapped_column(String(63))
    practice: Mapped[str | None] = mapped_column(String(127))
    required_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline: Mapped[object | None] = mapped_column(Date)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)  # 1 low - 5 critical
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
