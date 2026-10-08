import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class EmployeeOut(BaseModel):
    id: uuid.UUID
    employee_id: str
    name: str
    email: str | None
    role: str | None
    manager_name: str | None
    practice_director_name: str | None
    practice: str | None
    is_test_data: bool
    is_admin: bool
    aad_object_id: str | None
    is_directory_synced: bool

    class Config:
        from_attributes = True


class EmployeeCreateIn(BaseModel):
    employee_id: str
    name: str
    email: str
    role: str | None = None
    manager_name: str | None = None
    practice_director_name: str | None = None
    practice: str
    is_test_data: bool = False


class CatalogEntryOut(BaseModel):
    id: uuid.UUID
    name: str
    short_name: str | None  # the real name<->short_name mapping (services/short_name.py) — see DEVELOPMENT_LOG.md 2026-09-13
    provider: str | None
    level: str | None
    prerequisites: list | None
    validity_period_months: int | None
    relevant_roles: list | None
    status: str
    prerequisite_logic: str

    class Config:
        from_attributes = True


class CatalogUpsertIn(BaseModel):
    """Admin/seed-time catalog creation — upserts by name. Distinct from the
    auto-created pending_setup rows the submission form and Excel sync
    create when they see a cert name that isn't in the catalog yet.

    `prerequisites` here is informational display text only (a free-form
    list of names/notes) — it is NOT the real prerequisite graph. Actual
    prerequisite edges are separate rows in certification_prerequisites,
    created via POST /api/admin/certification-prerequisites."""

    name: str
    provider: str | None = None
    level: str | None = None
    prerequisites: list[str] | None = None
    validity_period_months: int | None = None
    relevant_roles: list[str] | None = None
    status: str = "active"
    prerequisite_logic: str = "any"  # "any" | "all" — how multiple incoming edges are evaluated


class PrerequisiteEdgeIn(BaseModel):
    """One prerequisite-graph edge: certification_name requires
    prerequisite_certification_name. Both must already exist in the
    catalog. track_label is optional presentation metadata (e.g. "AWS
    track") used only when describing branches, never in the eligibility
    logic itself."""

    certification_name: str
    prerequisite_certification_name: str
    track_label: str | None = None


class LearningPathItemSeedIn(BaseModel):
    """Membership only — see LearningPathItem's docstring in models.py.
    Ordering/prerequisites are expressed separately via
    PrerequisiteEdgeIn / POST /api/admin/certification-prerequisites."""

    certification_name: str
    is_required: bool = True


class LearningPathSeedIn(BaseModel):
    practice: str
    name: str
    description: str | None = None
    items: list[LearningPathItemSeedIn]


class LearningPathItemOut(BaseModel):
    is_required: bool
    certification: CatalogEntryOut

    class Config:
        from_attributes = True


class LearningPathOut(BaseModel):
    id: uuid.UUID
    practice: str
    name: str
    description: str | None
    items: list[LearningPathItemOut]

    class Config:
        from_attributes = True


# --- Next-certification recommender (services/recommender/) ---------------
class RecommendationOut(BaseModel):
    provider: str
    certification_name: str
    level: str | None
    domains: list[str]
    kind: str  # mandatory | renewal | retry | partnership_gap | next_step | stretch
    score: float
    score_breakdown: dict[str, float]
    reasons: list[str]
    sources: list[str]
    partnership_programs: list[str]


class SkippableOut(BaseModel):
    provider: str
    certification_name: str
    level: str | None
    reason: str


class DomainProficiencyOut(BaseModel):
    domain: str
    domain_label: str
    level: int  # 0 none - 4 expert
    level_label: str
    basis: str


class HeldCertOut(BaseModel):
    provider: str | None
    certification_name: str
    status: str
    source: str


class LearnerProfileOut(BaseModel):
    employee_id: str
    practice: str | None
    role: str | None
    seniority: str  # early_career | mid_career | senior
    seniority_basis: str
    years_experience: float | None
    proficiency: list[DomainProficiencyOut]
    career_goals: list[str]
    completed: list[HeldCertOut]
    in_progress: list[HeldCertOut]
    missing_signals: list[str]


class NextCertificationsOut(BaseModel):
    """Ranked, explained, partner-catalog-only recommendations — the ground
    truth the agent explains, never re-derives. `recommendations` is
    ordered best-first after diversity re-ranking; `in_progress` in the
    profile is "finish what you started", deliberately not re-recommended;
    `skippable` lists catalog certs this person can skip and why (the
    experienced-hire case). `signals_unavailable` names inputs that were
    empty/unreachable (e.g. partner_catalog, partnership_requirements,
    peer_signal) so the agent can say what the ranking is *not* based on."""

    profile: LearnerProfileOut
    recommendations: list[RecommendationOut]
    skippable: list[SkippableOut]
    catalog_size: int
    catalog_source: str  # partner_catalog | portal_records
    signals_unavailable: list[str]
    data_as_of: datetime


class SkillProfileIn(BaseModel):
    """Partial update — omitted fields are left unchanged. expertise values
    are 0-4 or a level name (foundational/associate/professional/expert);
    keys and career_goals must be taxonomy domain keys (GET
    /api/recommendations/taxonomy)."""

    employee_id: str
    years_experience: float | None = Field(default=None, ge=0, le=60)
    expertise: dict[str, int | str] | None = None
    career_goals: list[str] | None = None


class RecommendationFeedbackIn(BaseModel):
    employee_id: str
    provider: str
    certification_name: str
    action: str  # dismissed | interested
    reason: str | None = None


class PartnershipGapOut(BaseModel):
    requirement_id: str
    provider: str
    program_name: str
    certification_name: str | None
    min_level: str | None
    skill_domain: str | None
    practice: str | None
    required_count: int
    current_count: int
    gap: int
    deadline: date | None
    priority: int


class PartnerCertificationIn(BaseModel):
    provider: str
    certification_name: str
    level: str | None = None
    skill_domains: list[str] = []
    relevant_practices: list[str] = []
    prerequisite_names: list[str] = []
    validity_months: int | None = None
    is_active: bool = True


class PartnerCertificationOut(PartnerCertificationIn):
    id: int


class PartnershipRequirementIn(BaseModel):
    provider: str
    program_name: str
    required_count: int = Field(..., ge=0)
    certification_name: str | None = None
    min_level: str | None = None
    skill_domain: str | None = None
    practice: str | None = None
    deadline: date | None = None
    priority: int = Field(default=3, ge=1, le=5)
    notes: str | None = None
    is_active: bool = True


class PartnershipRequirementOut(PartnershipRequirementIn):
    id: int


class SubmissionIn(BaseModel):
    """Fields the submission form in the test UI posts.

    employee_id here is the HRIS-style external employee_id string (e.g.
    "E1001"), not the internal UUID PK.
    """

    employee_id: str
    cert_name: str = Field(..., description="Must match certification_catalog.name")
    submission_status: str  # in_progress | completed | gave_exam_but_failed
    manager_name: str | None = None
    practice_director_name: str | None = None
    prep_start_date: date | None = None
    exam_end_date: date | None = None
    credly_link: str | None = None
    expiration_date: date | None = None


class VerificationResultOut(BaseModel):
    id: uuid.UUID
    method: str
    extracted_recipient_name: str | None
    extracted_badge_name: str | None
    extracted_issued_date: date | None
    extracted_expiration_date: date | None
    name_match_score: float | None
    cert_match: bool | None
    status: str
    verifier: str
    timestamp: datetime

    class Config:
        from_attributes = True


class CertificateRecordOut(BaseModel):
    id: uuid.UUID
    employee_name: str
    manager_name: str | None
    practice_director_name: str | None
    practice: str | None
    cert_name: str
    submission_status: str
    verification_status: str
    prep_start_date: date | None
    exam_end_date: date | None
    credly_link: str | None
    expiration_date: date | None
    needs_setup: bool
    is_test_data: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SubmissionOut(BaseModel):
    record: CertificateRecordOut
    verification: VerificationResultOut | None
    data_as_of: datetime


class TrackerQueryOut(BaseModel):
    records: list[CertificateRecordOut]
    data_as_of: datetime


class KBQueryIn(BaseModel):
    question: str
    top_k: int = 3


class KBMatchOut(BaseModel):
    id: uuid.UUID
    content: str
    tags: list | None
    similarity: float
    last_reviewed_date: date | None


class KBQueryOut(BaseModel):
    matches: list[KBMatchOut]
    data_as_of: datetime


class EscalationCreateIn(BaseModel):
    employee_id: str  # external employee_id
    question: str
    context: str | None = None
    routed_owner: str | None = None


class EscalationOut(BaseModel):
    id: uuid.UUID
    question: str
    context: str | None
    routed_owner: str | None
    status: str
    sla_due_date: datetime | None
    resolution_answer: str | None
    resolved_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class EscalationResolveIn(BaseModel):
    resolution_answer: str
    promote_to_kb: bool = False
    kb_tags: list[str] | None = None


# --- Admin: learning-path membership (bulk) + prerequisite edge removal ---
class PathMembershipIn(BaseModel):
    """Add one certification to one already-existing path's membership.
    For creating a whole new path with its initial items, use
    LearningPathSeedIn / POST /api/admin/learning-paths instead."""

    certification_name: str
    is_required: bool = True


class BulkAddToPathsIn(BaseModel):
    """Attach one certification to one or more existing learning paths in
    a single action. Pass explicit path_ids, or all=True to attach it to
    every learning path that exists (paths that already contain the
    certification are left untouched, not duplicated)."""

    path_ids: list[uuid.UUID] | None = None
    all: bool = False


class PrerequisiteEdgeDeleteIn(BaseModel):
    certification_name: str
    prerequisite_certification_name: str


# --- Admin: knowledge base CRUD -------------------------------------------
class KBItemUpdateIn(BaseModel):
    """Editing an existing entry only ever touches its content — the
    certification it's attached to, its tags, owner, and status are fixed
    at creation time (status still has its own dedicated deprecate
    endpoint). last_reviewed_date is never client-supplied; the server
    stamps it with today's date on every save, since saving is itself the
    review event. content re-embeds only if the hash actually changed —
    see services/rag_content.py."""

    content: str


class KBItemOut(BaseModel):
    id: uuid.UUID
    certification_id: uuid.UUID | None
    certification_name: str | None = None
    content: str
    tags: list | None
    status: str
    owner: str | None
    last_reviewed_date: date | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Admin: certification drives ------------------------------------------
class DriveEscalationRuleIn(BaseModel):
    """One escalation step: once days_remaining <= threshold_days, notify
    the enrollee's manager or practice director (resolved by name against
    the employees table — see services/notification_sweep.py) in addition
    to the employee's own reminder."""

    threshold_days: int = Field(..., ge=0)
    escalate_to: str  # "manager" | "practice_director"


class ReminderCadenceIn(BaseModel):
    """Shape for certification_drives.reminder_cadence (JSONB). Replaces
    the earlier flat day-list: `reminder_days` drives the employee's own
    reminders, `escalation` drives who else gets notified and at which
    threshold. Both are read fresh by the sweep job every run, never
    cached — see the 2026-08-25 "reminder sweep" dev log entry."""

    reminder_days: list[int] = Field(default_factory=list)
    escalation: list[DriveEscalationRuleIn] = Field(default_factory=list)


class DriveCreateIn(BaseModel):
    name: str
    description: str | None = None
    target_scope: str  # practice | department | organization
    # Required for practice/department scope (which practice); left unset
    # for organization scope, which targets every employee and has nothing
    # to name. Enforced in routers/drives.py, not here, so the 422 message
    # can name the offending scope.
    target_scope_value: str | None = None
    deadline: date
    reminder_cadence: ReminderCadenceIn | None = None
    certification_names: list[str] = Field(..., min_length=1)
    is_test_data: bool = False


class DriveUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    target_scope: str | None = None
    target_scope_value: str | None = None
    deadline: date | None = None
    reminder_cadence: ReminderCadenceIn | None = None


class DriveOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    target_scope: str
    target_scope_value: str | None
    start_date: date | None  # null until enroll_population activates the drive
    deadline: date
    reminder_cadence: ReminderCadenceIn | None
    status: str
    is_test_data: bool
    created_by: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DriveEnrollmentOut(BaseModel):
    id: uuid.UUID
    employee_id: str
    employee_name: str
    enrollment_status: str
    is_compliant: bool  # live, computed on read — never stored
    certificate_record_id: uuid.UUID | None
    exemption_reason: str | None
    enrolled_at: datetime


class DriveDetailOut(BaseModel):
    drive: DriveOut
    required_certifications: list[str]
    enrolled_count: int
    compliant_count: int
    exempted_count: int
    compliance_pct: float  # compliant_count / (enrolled_count - exempted_count), 0 if denominator is 0
    enrollments: list[DriveEnrollmentOut]
    data_as_of: datetime


class ExemptEnrollmentIn(BaseModel):
    reason: str


class DriveObligationOut(BaseModel):
    """One drive the caller is enrolled in — what the agent's
    get_my_drive_obligations tool returns. Structured metadata only
    (deadlines, status), never RAG-retrieved — see Section 5 of the
    2026-08-25 drives spec in DEVELOPMENT_LOG.md."""

    drive_name: str
    required_certifications: list[str]
    deadline: date
    enrollment_status: str
    is_compliant: bool


class MyDriveObligationsOut(BaseModel):
    obligations: list[DriveObligationOut]
    data_as_of: datetime


# --- Admin: notification settings + the reminder sweep --------------------
class NotificationSettingsOut(BaseModel):
    id: uuid.UUID
    expiry_reminder_days: list[int]
    retry_cap: int
    updated_at: datetime

    class Config:
        from_attributes = True


class NotificationSettingsUpdateIn(BaseModel):
    expiry_reminder_days: list[int] | None = None
    retry_cap: int | None = None


class NotificationOut(BaseModel):
    id: uuid.UUID
    dedupe_key: str
    notification_type: str
    recipient_employee_id: str  # external employee_id, not the internal UUID
    recipient_name: str
    channel: str
    message: str
    related_drive_id: uuid.UUID | None
    related_certificate_record_id: uuid.UUID | None
    threshold_days: int | None
    escalation_level: str | None
    status: str
    retry_count: int
    last_attempt_at: datetime | None
    is_test_data: bool
    created_at: datetime


class SweepTriggerIn(BaseModel):
    """Test-only control, not a real send-channel config: recipients in
    this list have their (simulated) send deliberately fail, so the retry
    pass can be verified deterministically without waiting for a real
    failure. Leave empty for a normal run."""

    simulate_failure_employee_ids: list[str] = Field(default_factory=list)


class SweepReportOut(BaseModel):
    drive_reminders_created: int
    drive_escalations_created: int
    cert_expiry_reminders_created: int
    prep_overdue_reminders_created: int = 0
    skipped_duplicate: int
    escalations_skipped_unresolved_recipient: int
    sent: int
    failed: int
    retried: int
    retried_now_sent: int
    ran_at: datetime
