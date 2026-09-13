import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

# --- Enums -------------------------------------------------------------
# Practice is hardcoded for local dev. Later this is sourced from the
# Teams/Entra user profile at sync time (see services/excel_sync.py and
# routers/employees.py) rather than typed here by hand; changing the list
# of practices is a migration, not a code edit elsewhere.
PRACTICE_VALUES = (
    "App + DB Mod",
    "Data+AI",
    "NetSec + SDx",
    "Cloud & Platform Engineer",
)
practice_enum = PGEnum(*PRACTICE_VALUES, name="practice_enum")

CATALOG_STATUS_VALUES = ("pending_setup", "active", "deprecated")
catalog_status_enum = PGEnum(*CATALOG_STATUS_VALUES, name="catalog_status_enum")

SUBMISSION_STATUS_VALUES = ("in_progress", "completed", "gave_exam_but_failed")
submission_status_enum = PGEnum(*SUBMISSION_STATUS_VALUES, name="submission_status_enum")

VERIFICATION_STATUS_VALUES = ("not_applicable", "pending", "verified", "exception")
verification_status_enum = PGEnum(*VERIFICATION_STATUS_VALUES, name="verification_status_enum")

VERIFICATION_METHOD_VALUES = ("credly_link_check", "manual")
verification_method_enum = PGEnum(*VERIFICATION_METHOD_VALUES, name="verification_method_enum")

VERIFICATION_RESULT_STATUS_VALUES = ("verified", "exception", "failed")
verification_result_status_enum = PGEnum(
    *VERIFICATION_RESULT_STATUS_VALUES, name="verification_result_status_enum"
)

KB_STATUS_VALUES = ("active", "pending_review", "deprecated")
kb_status_enum = PGEnum(*KB_STATUS_VALUES, name="kb_status_enum")

ESCALATION_STATUS_VALUES = ("open", "in_progress", "resolved")
escalation_status_enum = PGEnum(*ESCALATION_STATUS_VALUES, name="escalation_status_enum")

AUDIT_ACTION_VALUES = (
    "created",
    "status_changed",
    "verified",
    "field_updated",
    "deleted",
    "employee_auto_created",
)
audit_action_enum = PGEnum(*AUDIT_ACTION_VALUES, name="audit_action_enum")

# --- Certification drives (mandatory campaigns) -------------------------
# A drive targets a scope (one practice, one department, or the whole
# organization) with one or more required certifications and a deadline.
# Drives come and go — a practice gets targeted for a quarter, then the
# next drive targets someone else — so these are their own lifecycle
# objects, never folded into certification_catalog or learning_paths.
DRIVE_TARGET_SCOPE_VALUES = ("practice", "department", "organization")
drive_target_scope_enum = PGEnum(*DRIVE_TARGET_SCOPE_VALUES, name="drive_target_scope_enum")

DRIVE_STATUS_VALUES = ("draft", "active", "closed")
drive_status_enum = PGEnum(*DRIVE_STATUS_VALUES, name="drive_status_enum")

ENROLLMENT_STATUS_VALUES = ("not_started", "in_progress", "completed", "exempted")
enrollment_status_enum = PGEnum(*ENROLLMENT_STATUS_VALUES, name="enrollment_status_enum")

# --- Notifications (reminder sweep) --------------------------------------
NOTIFICATION_TYPE_VALUES = (
    "drive_reminder",
    "drive_escalation",
    "cert_expiry_reminder",
    "new_certification_added",
)
notification_type_enum = PGEnum(*NOTIFICATION_TYPE_VALUES, name="notification_type_enum")

NOTIFICATION_CHANNEL_VALUES = ("teams",)  # SEAM: only channel until a real Teams bot integration exists
notification_channel_enum = PGEnum(*NOTIFICATION_CHANNEL_VALUES, name="notification_channel_enum")

NOTIFICATION_STATUS_VALUES = ("pending", "sent", "failed")
notification_status_enum = PGEnum(*NOTIFICATION_STATUS_VALUES, name="notification_status_enum")

ESCALATION_LEVEL_VALUES = ("employee", "manager", "practice_director")
escalation_level_enum = PGEnum(*ESCALATION_LEVEL_VALUES, name="escalation_level_enum")

# Governs how multiple incoming CertificationPrerequisite edges into the
# same certification are evaluated: 'any' = at least one prerequisite must
# be completed (a branch converging back together); 'all' = every listed
# prerequisite must be completed. Defaults to 'any' everywhere today since
# nothing in the current seed data actually requires 'all' yet — added now
# so a future converging-AND track doesn't need its own migration.
PREREQUISITE_LOGIC_VALUES = ("any", "all")
prerequisite_logic_enum = PGEnum(*PREREQUISITE_LOGIC_VALUES, name="prerequisite_logic_enum")

# --- Conversation memory (Teams bot session continuity) -----------------
CONVERSATION_ROLE_VALUES = ("user", "agent")
conversation_role_enum = PGEnum(*CONVERSATION_ROLE_VALUES, name="conversation_role_enum")


def uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --- Employees (local HRIS/directory stand-in) --------------------------
class Employee(Base):
    """Local stand-in for the HRIS/directory (Section 3, BRD).

    SEAM: in production this table is populated/kept in sync from Microsoft
    Graph (Entra ID User + manager relationship), not edited by hand. Until
    that sync exists, rows are seeded via scripts/seed_test_employees.py,
    entered by L&D admins, or auto-provisioned on first Teams contact (see
    routers/teams.py) — see services/excel_sync.py `resolve_employee` for
    the one place that would be swapped for a Graph lookup.
    """

    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # The Teams/Bot Framework identity join key (activity.from.aad_object_id)
    # — nullable because the local test roster predates any real tenant and
    # has no real AAD object IDs yet; populated once the directory sync job
    # (services/excel_sync.py resolve_employee, same SEAM as practice above)
    # starts writing real Entra object IDs, or immediately on first contact
    # for an auto-provisioned row (routers/teams.py). Unique so two
    # employees can never resolve to the same Teams identity by accident.
    aad_object_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nullable: an auto-provisioned employee (routers/teams.py) has no email
    # at all — Bot Framework's ChannelAccount carries only id/name/
    # aad_object_id, nothing else — until directory sync fills it in.
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    role: Mapped[str | None] = mapped_column(String(255))
    manager_name: Mapped[str | None] = mapped_column(String(255))
    practice_director_name: Mapped[str | None] = mapped_column(String(255))
    # Nullable for the same reason as email — an auto-provisioned employee's
    # practice isn't knowable from a Teams activity alone. Every
    # practice-dependent feature (routers/learning_paths.py's /next and
    # /overview) must treat None as "not set up yet, say so plainly" rather
    # than crashing or guessing a default practice.
    practice: Mapped[str | None] = mapped_column(practice_enum)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Data-provenance flag, not an access gate: False for an auto-provisioned
    # row (routers/teams.py) until a real Entra/HRIS directory sync confirms
    # and enriches it (still a SEAM — that sync doesn't exist yet). Never
    # read anywhere as a permission check; it only tells an admin which
    # records still need enriching. Existing pre-auto-provisioning rows are
    # backfilled to True in migration 0007 — they're already fully seeded,
    # not stubs waiting on enrichment.
    is_directory_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # SEAM: local stand-in for a real Entra app-role check. Every admin-
    # gated endpoint (drives, catalog CRUD, KB CRUD) reads this one flag
    # through services/admin_auth.py's require_admin — that is the single
    # place to swap in the real role check later.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Certification catalog & learning paths -----------------------------
class CertificationCatalog(Base):
    __tablename__ = "certification_catalog"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # The real Excel sheet name for this cert's tab — distinct from `name`
    # because Excel sheet names are capped at 31 chars and can't contain
    # \ / ? * [ ] : , while a real cert name routinely exceeds that (see
    # services/short_name.py, which generates this). Which SharePoint file
    # a cert's sheet lives in is determined by `provider` below, not by
    # short_name itself — one file per provider, one sheet per cert.
    # Nullable only because a not-yet-backfilled pre-2026-09-13 row may not
    # have one yet; every row created after that migration always does.
    short_name: Mapped[str | None] = mapped_column(String(31), unique=True)
    provider: Mapped[str | None] = mapped_column(String(255))
    level: Mapped[str | None] = mapped_column(String(100))
    prerequisites: Mapped[list | None] = mapped_column(JSONB)  # informational display only — the
    # real, queryable prerequisite graph is CertificationPrerequisite; this JSONB free-text list is
    # not read by any eligibility logic and exists only for a quick human-readable summary.
    validity_period_months: Mapped[int | None] = mapped_column(Integer)
    relevant_roles: Mapped[list | None] = mapped_column(JSONB)  # tags
    status: Mapped[str] = mapped_column(catalog_status_enum, default="pending_setup", nullable=False)
    prerequisite_logic: Mapped[str] = mapped_column(prerequisite_logic_enum, default="any", nullable=False)
    source_sheet_id: Mapped[str | None] = mapped_column(String(64))  # hidden GUID from Excel tab
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LearningPath(Base):
    __tablename__ = "learning_paths"

    id: Mapped[uuid.UUID] = uuid_pk()
    practice: Mapped[str] = mapped_column(practice_enum, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list["LearningPathItem"]] = relationship(back_populates="path")


class LearningPathItem(Base):
    """Membership only: which certifications belong to which named
    practice/path, for catalog browsing. Ordering and prerequisites are
    NOT this table's job — see CertificationPrerequisite. (Until
    2026-08-24 this table also carried sequence_order and a single
    prerequisite_certification_id, modeling the path as a straight line;
    that couldn't express a real branch — e.g. one completed cert opening
    up two valid next certifications — so it was replaced with a proper
    prerequisite graph.)
    """

    __tablename__ = "learning_path_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    path_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"))
    certification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("certification_catalog.id", ondelete="RESTRICT")
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    path: Mapped["LearningPath"] = relationship(back_populates="items")
    certification: Mapped["CertificationCatalog"] = relationship(foreign_keys=[certification_id])


class CertificationPrerequisite(Base):
    """One edge in the certification prerequisite DAG: `certification_id`
    requires `prerequisite_certification_id`. A certification can have
    multiple incoming edges (a real branch point, or a converging
    capstone reachable from more than one track) — see
    CertificationCatalog.prerequisite_logic for how multiple edges into
    the same certification are evaluated ('any' vs 'all').

    track_label is presentation-only metadata (e.g. "AWS track" vs
    "Databricks track") for grouping branches when the agent describes
    them — it plays no role in the eligibility logic itself.
    """

    __tablename__ = "certification_prerequisites"
    __table_args__ = (
        UniqueConstraint(
            "certification_id", "prerequisite_certification_id", name="uq_certification_prerequisite_edge"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    certification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("certification_catalog.id", ondelete="CASCADE"), nullable=False
    )
    prerequisite_certification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("certification_catalog.id", ondelete="CASCADE"), nullable=False
    )
    track_label: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    certification: Mapped["CertificationCatalog"] = relationship(foreign_keys=[certification_id])
    prerequisite: Mapped["CertificationCatalog"] = relationship(foreign_keys=[prerequisite_certification_id])


# --- Certificate records (the tracker) -----------------------------------
class CertificateRecord(Base):
    """The certification tracker — single source of truth (Section 3, BRD).

    All writes to this table MUST go through
    services.repository.CertificateRecordRepository so that every create/
    update is audit-logged exactly once, in one place (see services/audit.py).
    """

    __tablename__ = "certificate_records"
    __table_args__ = (
        UniqueConstraint("source_sheet_id", "source_row_id", name="uq_cert_record_source"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    employee_name: Mapped[str] = mapped_column(String(255), nullable=False)  # snapshot
    employee_email: Mapped[str | None] = mapped_column(String(255))  # snapshot; also an alt match key, see excel_sync.resolve_employee
    manager_name: Mapped[str | None] = mapped_column(String(255))
    manager_email: Mapped[str | None] = mapped_column(String(255))
    practice_director_name: Mapped[str | None] = mapped_column(String(255))
    practice_director_email: Mapped[str | None] = mapped_column(String(255))
    practice: Mapped[str | None] = mapped_column(practice_enum)  # auto-populated from employees, not the sheet

    certification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("certification_catalog.id", ondelete="SET NULL")
    )
    cert_name: Mapped[str] = mapped_column(String(255), nullable=False)  # snapshot / as-submitted

    submission_status: Mapped[str] = mapped_column(submission_status_enum, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        verification_status_enum, default="not_applicable", nullable=False
    )

    prep_start_date: Mapped[date | None] = mapped_column(Date)
    exam_end_date: Mapped[date | None] = mapped_column(Date)
    credly_link: Mapped[str | None] = mapped_column(String(1024))
    expiration_date: Mapped[date | None] = mapped_column(Date)

    # Excel sync identity (Section: hidden Record ID) — null for records
    # created directly through the submission form / agent, not via sync.
    source_sheet_id: Mapped[str | None] = mapped_column(String(64))
    source_row_id: Mapped[str | None] = mapped_column(String(64))
    needs_setup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("certificate_records.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(verification_method_enum, nullable=False)
    extracted_recipient_name: Mapped[str | None] = mapped_column(String(255))
    extracted_badge_name: Mapped[str | None] = mapped_column(String(255))
    extracted_issued_date: Mapped[date | None] = mapped_column(Date)
    extracted_expiration_date: Mapped[date | None] = mapped_column(Date)
    name_match_score: Mapped[float | None] = mapped_column()
    cert_match: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(verification_result_status_enum, nullable=False)
    verifier: Mapped[str] = mapped_column(String(255), nullable=False)  # "system" or an admin's name
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Knowledge base & escalations (RAG) -----------------------------------
class KnowledgeBaseItem(Base):
    __tablename__ = "knowledge_base"

    id: Mapped[uuid.UUID] = uuid_pk()
    certification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("certification_catalog.id", ondelete="SET NULL")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    # SHA-256 hex digest of `content` as of the last time it was embedded —
    # see services/rag_content.py embed_if_changed(). Lets a save that only
    # touches owner/status/last_reviewed_date skip the embedding call
    # entirely, since those fields don't feed the vector. Nullable only for
    # rows written before this column existed (see migration 0003) or if
    # embedding ever fails; ordinary content-only rows always have it.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(kb_status_enum, default="active", nullable=False)
    owner: Mapped[str | None] = mapped_column(String(255))
    last_reviewed_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    context: Mapped[str | None] = mapped_column(Text)
    routed_owner: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(escalation_status_enum, default="open", nullable=False)
    sla_due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_answer: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_kb_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_base.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Certification drives (mandatory campaigns) --------------------------
class CertificationDrive(Base):
    """A mandatory certification campaign targeting a scope (practice /
    department / organization) for a window of time. Compliance % is never
    stored here or anywhere else — it's computed on every read from
    drive_enrollments joined to certificate_records (see routers/drives.py
    get_drive_detail), so it can never drift stale.
    """

    __tablename__ = "certification_drives"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    target_scope: Mapped[str] = mapped_column(drive_target_scope_enum, nullable=False)
    # Nullable: only meaningful (and required, see DriveCreateIn) for a
    # practice-scoped drive — an organization-scoped drive targets every
    # employee, so there's no value to record.
    target_scope_value: Mapped[str | None] = mapped_column(String(255))  # e.g. a practice name
    # Nullable at creation — a drive starts as a "draft" with no start date
    # yet. Set automatically (to the activation date) the moment
    # enroll_population promotes it to "active" (see routers/drives.py) —
    # never admin-entered, per the 2026-09-02 drives-form simplification.
    start_date: Mapped[date | None] = mapped_column(Date)
    deadline: Mapped[date] = mapped_column(Date, nullable=False)
    reminder_cadence: Mapped[list | None] = mapped_column(JSONB)  # e.g. [30, 14, 7, 1] days-before-deadline
    status: Mapped[str] = mapped_column(drive_status_enum, default="draft", nullable=False)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)  # actor employee_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    required_certifications: Mapped[list["DriveCertification"]] = relationship(back_populates="drive")
    enrollments: Mapped[list["DriveEnrollment"]] = relationship(back_populates="drive")


class DriveCertification(Base):
    """Join table: a drive can require more than one certification (e.g.
    "AWS cert OR either of two Azure certs")."""

    __tablename__ = "drive_certifications"
    __table_args__ = (
        UniqueConstraint("drive_id", "certification_id", name="uq_drive_certification"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    drive_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("certification_drives.id", ondelete="CASCADE"))
    certification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("certification_catalog.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    drive: Mapped["CertificationDrive"] = relationship(back_populates="required_certifications")
    certification: Mapped["CertificationCatalog"] = relationship()


class DriveEnrollment(Base):
    """One employee's participation record in one drive. enrollment_status
    is an admin-facing workflow marker set at enroll/exempt time — it is
    NOT the source of truth for compliance %, which is always recomputed
    live from certificate_record_id -> certificate_records.submission_status
    (see routers/drives.py). certificate_record_id is only ever set once the
    employee actually submits a matching certification (wired from
    routers/certifications.py submit_certification)."""

    __tablename__ = "drive_enrollments"
    __table_args__ = (
        UniqueConstraint("drive_id", "employee_id", name="uq_drive_enrollment"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    drive_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("certification_drives.id", ondelete="CASCADE"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    enrollment_status: Mapped[str] = mapped_column(
        enrollment_status_enum, default="not_started", nullable=False
    )
    certificate_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("certificate_records.id", ondelete="SET NULL")
    )
    exemption_reason: Mapped[str | None] = mapped_column(Text)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    drive: Mapped["CertificationDrive"] = relationship(back_populates="enrollments")
    employee: Mapped["Employee"] = relationship()
    certificate_record: Mapped["CertificateRecord | None"] = relationship()


# --- Notifications (reminder sweep) --------------------------------------
class NotificationSettings(Base):
    """Organization-wide notification config — a single row, read/written
    like any other admin-editable setting rather than hardcoded in the
    sweep job (BR-D-05 explicitly calls for this to be adjustable). Always
    exactly one row; routers/notifications.py enforces that by upserting
    the same row rather than ever inserting a second one.
    """

    __tablename__ = "notification_settings"

    id: Mapped[uuid.UUID] = uuid_pk()
    expiry_reminder_days: Mapped[list] = mapped_column(JSONB, nullable=False)  # e.g. [90, 60, 30, 7]
    retry_cap: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Notification(Base):
    """One reminder/escalation the sweep job decided to send (or is about
    to retry). `dedupe_key` is what makes the sweep safely re-runnable —
    see services/notification_sweep.py — and the unique constraint on it,
    not application logic, is what actually prevents a duplicate send if
    the job somehow runs concurrently with itself.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    notification_type: Mapped[str] = mapped_column(notification_type_enum, nullable=False)
    recipient_employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(notification_channel_enum, default="teams", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    related_drive_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("certification_drives.id", ondelete="CASCADE")
    )
    related_certificate_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("certificate_records.id", ondelete="CASCADE")
    )
    threshold_days: Mapped[int | None] = mapped_column(Integer)
    escalation_level: Mapped[str | None] = mapped_column(escalation_level_enum)
    status: Mapped[str] = mapped_column(notification_status_enum, default="pending", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    recipient: Mapped["Employee"] = relationship()


# --- Audit log (append-only) ----------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(audit_action_enum, nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Conversation memory (Teams bot session continuity) -------------------
class ConversationTurn(Base):
    """Append-only raw log of every chat turn — same audit posture as the
    rest of the system (nothing here is ever edited or deleted in place).
    This is tier 1 of two-tier conversation memory: services/conversation_memory.py
    replays only the most recent N of these per employee as live context,
    trimming/folding older ones into ConversationMemory.summary_text instead
    of ever replaying the full history on every message.

    conversation_id is the Teams activity's conversationId — kept even
    though today's schema assumes one active thread per employee (see
    ConversationMemory below), so a future multi-thread-per-person design
    doesn't need a migration to retrofit it."""

    __tablename__ = "conversation_turns"

    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(conversation_role_enum, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    employee: Mapped["Employee"] = relationship()


class ConversationMemory(Base):
    """Tier 2: one row per employee, folding everything older than the
    live raw-turn window into a running summary. Updated by
    services/conversation_memory.py's summarization pass — every ~15 turns
    (SUMMARIZE_EVERY_N_TURNS) or when the raw window's token estimate
    crosses SUMMARIZE_TOKEN_BUDGET, whichever comes first — via a single
    LLM call that folds the turns being trimmed into summary_text, never by
    replaying the entire raw history.

    The summary itself isn't kept forever: turns_folded_since_reset tracks
    how many turns have been folded into the *current* summary lineage,
    and once that crosses conversation_summary_max_age_turns, the summary
    is discarded outright (not truncated) rather than left to grow without
    bound — see conversation_memory.py's _summarize. This never touches
    conversation_turns, which stays a permanent, untrimmed raw log either
    way; it only bounds what the long-term-memory tier carries forward.

    last_summarize_attempt_at is set on every attempt, success or failure —
    used to back off after a failure instead of retrying the same failing
    LLM call on every subsequent turn."""

    __tablename__ = "conversation_memory"

    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    summary_text: Mapped[str | None] = mapped_column(Text)
    last_summarized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    turns_since_last_summary: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turns_folded_since_reset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_summarize_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped["Employee"] = relationship()
