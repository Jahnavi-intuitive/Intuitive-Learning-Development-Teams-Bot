"""Pure recommendation core — no database, no HTTP. Everything here takes
plain dataclasses in and returns plain dataclasses out, so the whole
ranking pipeline is unit-testable offline (tests/test_recommender.py) and
the DB-loading side (engine.py) can change without touching the logic.

Pipeline (shape borrowed from large-scale recommenders — LinkedIn's
candidate-generation → ranking → re-ranking split, Netflix GenRec's
"LLM explains/refines, never invents candidates" stance):

  1. build_profile     — who is this person *as a learner*: seniority band,
                         per-domain proficiency (cert evidence ∪ self-declared)
  2. generate          — multiple independent candidate sources, unioned:
                         career-ladder next steps, partnership gaps,
                         mandatory drives, renewals, retries, peer signal,
                         practice relevance
  3. score             — transparent multi-objective weighted score, every
                         component kept (score_breakdown) with human-readable
                         reasons — the agent explains from these, it never
                         re-derives eligibility itself
  4. rerank            — diversity (cap per partner / per domain) so the top
                         N isn't five near-identical certs from one partner

Levels & experience ("fresher vs experienced hire"): proficiency per
domain is the max of certification evidence and self-declared expertise.
The *target* level in a domain is one step above current proficiency; for
a domain with no proficiency at all, the entry point depends on the
seniority band — an early-career hire starts at foundational, a mid/senior
hire enters directly at associate. A certification at or below the
person's proficiency is not recommended (it's reported in
`skippable` with the evidence that makes it skippable) unless the business
specifically needs it (a named partnership requirement or a mandatory
drive).
"""

from dataclasses import dataclass, field
from datetime import date

from app.services.recommender.taxonomy import (
    DOMAINS,
    RANK_LABELS,
    cert_key,
    domains_for_practice,
    infer_domains,
    infer_domains_from_role,
    level_rank,
    normalize_level,
    normalize_practice,
    seniority_from_title,
)

# --- Tunables ---------------------------------------------------------------
# Weights sum to 1.0 so a plain (non-mandatory, non-renewal) score is in
# [0, 1]. Business need is the single largest term on purpose: partnership
# scale-up is the reason this recommender exists beyond a career ladder —
# but it's multiplied by fit (see _score), so a gap never pushes a
# certification someone isn't ready for.
WEIGHTS = {
    "business_need": 0.30,
    "level_fit": 0.25,
    "relevance": 0.15,
    "career_goal": 0.10,
    "peer_signal": 0.10,
    "readiness": 0.10,
}
MANDATORY_BOOST = 1.0  # always above any organic score
RENEWAL_BOOST = 0.35
RETRY_BOOST = 0.10
RENEWAL_WINDOW_DAYS = 90
MAX_PER_PROVIDER = 2
MAX_PER_DOMAIN = 3

SENIORITY_EARLY = "early_career"
SENIORITY_MID = "mid_career"
SENIORITY_SENIOR = "senior"


# --- Inputs -------------------------------------------------------------------
@dataclass(frozen=True)
class CatalogCert:
    provider: str
    name: str
    level: str | None = None  # canonical level name; inferred from name if None
    domains: tuple[str, ...] = ()  # inferred from name if empty
    practices: tuple[str, ...] = ()  # practices this cert is especially relevant to
    prerequisites: tuple[str, ...] = ()  # *recommended* prior cert names (never an enforced gate)
    validity_months: int | None = None
    source: str = "partner_catalog"  # "partner_catalog" | "portal_records" | "bundled_catalog" | "drive"

    @property
    def key(self) -> tuple[str, str]:
        return cert_key(self.provider, self.name)

    @property
    def level_name(self) -> str | None:
        return self.level or normalize_level(None, self.name)

    @property
    def rank(self) -> int:
        return level_rank(self.level_name)

    @property
    def domain_list(self) -> tuple[str, ...]:
        return self.domains or infer_domains(self.name, self.provider)


@dataclass(frozen=True)
class HeldCert:
    provider: str | None
    name: str
    status: str  # "completed" | "in_progress" | "failed"
    level: str | None = None
    completion_date: date | None = None
    expiry_date: date | None = None
    source: str = "tracker"

    @property
    def key(self) -> tuple[str, str]:
        return cert_key(self.provider, self.name)

    @property
    def name_key(self) -> str:
        # Tracker rows in the main app carry no partner string that lines
        # up with the portal's provider enum, so held-vs-catalog matching
        # is by normalized certification name only.
        return cert_key(None, self.name)[1]


@dataclass(frozen=True)
class Requirement:
    """One partnership scale-up need: "the AWS partnership needs N people
    holding <cert | any cert at level ≥ L | any cert in domain D>".
    current_count is computed by the caller from real tracker data."""

    requirement_id: str
    provider: str
    program_name: str
    required_count: int
    current_count: int
    certification_name: str | None = None
    min_level: str | None = None
    domain: str | None = None
    practice: str | None = None  # None = open to every practice
    deadline: date | None = None
    priority: int = 3  # 1 (low) - 5 (critical)

    @property
    def gap(self) -> int:
        return max(0, self.required_count - self.current_count)

    def matches(self, cert: CatalogCert, employee_practice: str | None) -> bool:
        if self.provider.lower() != cert.provider.lower():
            return False
        if self.practice and normalize_practice(self.practice) != normalize_practice(employee_practice):
            return False
        if self.certification_name and cert_key(self.provider, self.certification_name) != cert.key:
            return False
        if self.min_level and cert.rank < level_rank(self.min_level):
            return False
        if self.domain and self.domain not in cert.domain_list:
            return False
        return True


@dataclass(frozen=True)
class DriveObligation:
    drive_name: str
    certification_names: tuple[str, ...]
    deadline: date | None
    is_compliant: bool


@dataclass
class PeerStats:
    """practice_share: share of people in the employee's practice holding
    each cert. co_holder_share: among people who hold at least one of the
    employee's certs, the share also holding each cert ("people with your
    certifications went on to…"). Both keyed by cert_key; both 0-1."""

    practice_share: dict[tuple[str, str], float] = field(default_factory=dict)
    co_holder_share: dict[tuple[str, str], float] = field(default_factory=dict)
    practice_population: int = 0
    co_holder_population: int = 0


@dataclass
class ProfileInput:
    employee_id: str
    name: str
    practice: str | None
    role: str | None
    years_experience: float | None
    declared_expertise: dict[str, int]  # domain -> 0-4
    career_goals: list[str]  # domains
    held: list[HeldCert]


# --- Outputs ------------------------------------------------------------------
@dataclass
class DomainProficiency:
    domain: str
    level: int  # max(evidence_level, declared_level)
    basis: str  # human-readable evidence
    evidence_level: int = 0  # from certifications actually held
    declared_level: int = 0  # self-declared (employee_skill_profiles)


@dataclass
class LearnerProfile:
    employee_id: str
    name: str
    practice: str | None
    role: str | None
    seniority: str
    seniority_basis: str
    years_experience: float | None
    proficiency: dict[str, DomainProficiency]
    career_goals: list[str]
    completed_keys: set  # normalized certification names (HeldCert.name_key)
    in_progress: list[HeldCert]
    failed: list[HeldCert]
    completed: list[HeldCert]
    missing_signals: list[str]  # what the agent should ask to improve recommendations


@dataclass
class Recommendation:
    provider: str
    certification_name: str
    level: str | None
    domains: list[str]
    kind: str  # "mandatory" | "renewal" | "retry" | "partnership_gap" | "next_step" | "stretch"
    score: float
    score_breakdown: dict[str, float]
    reasons: list[str]
    sources: list[str]
    partnership_programs: list[str]


@dataclass
class Skippable:
    provider: str
    certification_name: str
    level: str | None
    reason: str


@dataclass
class RecommendationResult:
    profile: LearnerProfile
    recommendations: list[Recommendation]
    skippable: list[Skippable]
    catalog_size: int
    catalog_source: str


# --- 1. Profile ---------------------------------------------------------------
_BAND_ORDER = {SENIORITY_EARLY: 0, SENIORITY_MID: 1, SENIORITY_SENIOR: 2}


def _seniority(years: float | None, top_rank: int, role: str | None = None) -> tuple[str, str]:
    if years is not None:
        if years < 2:
            return SENIORITY_EARLY, f"{years:g} years of experience on file"
        if years < 6:
            return SENIORITY_MID, f"{years:g} years of experience on file"
        return SENIORITY_SENIOR, f"{years:g} years of experience on file"
    # No experience on file: infer from the strongest credential held, or
    # the designation (job title), whichever implies the higher band.
    if top_rank >= 3:
        cred = (SENIORITY_SENIOR, "inferred — holds a professional-level certification (no experience on file)")
    elif top_rank == 2:
        cred = (SENIORITY_MID, "inferred — holds an associate-level certification (no experience on file)")
    else:
        cred = None
    title_band = seniority_from_title(role)
    if title_band and (cred is None or _BAND_ORDER[title_band] > _BAND_ORDER[cred[0]]):
        return title_band, f"inferred from your designation ({role}) — no experience on file"
    if cred:
        return cred
    return SENIORITY_EARLY, "assumed — no experience, designation or associate-level certification on file yet"


def build_profile(p: ProfileInput, today: date) -> LearnerProfile:
    completed = [h for h in p.held if h.status == "completed"]
    in_progress = [h for h in p.held if h.status == "in_progress"]
    completed_names = {h.name_key for h in completed}
    failed = [h for h in p.held if h.status == "failed" and h.name_key not in completed_names]
    in_progress = [h for h in in_progress if h.name_key not in completed_names]

    proficiency: dict[str, DomainProficiency] = {}
    top_rank = 0
    for h in completed:
        rank = level_rank(h.level or normalize_level(None, h.name))
        top_rank = max(top_rank, rank)
        for domain in infer_domains(h.name, h.provider):
            current = proficiency.get(domain)
            if current is None or rank > current.evidence_level:
                proficiency[domain] = DomainProficiency(domain, rank, f"holds {h.name}", evidence_level=rank)
    for domain, declared in p.declared_expertise.items():
        current = proficiency.setdefault(domain, DomainProficiency(domain, 0, ""))
        current.declared_level = declared
        if declared > current.evidence_level:
            current.level = declared
            current.basis = f"self-declared {RANK_LABELS[declared]} expertise"
    proficiency = {d: v for d, v in proficiency.items() if v.level > 0}

    seniority, basis = _seniority(p.years_experience, top_rank, p.role)

    missing = []
    if p.years_experience is None:
        missing.append("years_experience")
    if not p.declared_expertise:
        missing.append("expertise")
    if not p.career_goals:
        missing.append("career_goals")
    if not p.practice:
        missing.append("practice")

    return LearnerProfile(
        employee_id=p.employee_id,
        name=p.name,
        practice=p.practice,
        role=p.role,
        seniority=seniority,
        seniority_basis=basis,
        years_experience=p.years_experience,
        proficiency=proficiency,
        career_goals=list(p.career_goals),
        completed_keys=completed_names,
        in_progress=in_progress,
        failed=failed,
        completed=completed,
        missing_signals=missing,
    )


# --- 2/3. Candidates + scoring -----------------------------------------------
def _entry_rank(seniority: str) -> int:
    """Where someone with zero proficiency in a domain should start."""
    return 1 if seniority == SENIORITY_EARLY else 2


def _level_fit(cert: CatalogCert, profile: LearnerProfile) -> tuple[float, str, str]:
    """Best (fit, domain, explanation) across the cert's domains. fit=0
    means redundant — the person is already at or above this level."""
    best = (0.0, cert.domain_list[0] if cert.domain_list else "", "")
    for domain in cert.domain_list or ("",):
        prof = profile.proficiency.get(domain)
        p = prof.level if prof else 0
        r = cert.rank
        label = DOMAINS.get(domain, domain)
        if prof and prof.evidence_level >= r:
            # Already holds a credential at or above this level here.
            fit, why = 0.0, f"already at {RANK_LABELS[prof.evidence_level]} level in {label} (holds a certification there)"
        elif prof and r < prof.declared_level:
            fit, why = 0.0, f"below your self-declared {RANK_LABELS[prof.declared_level]} expertise in {label}"
        elif prof and r == prof.declared_level:
            # Declared skill but no credential yet — certifying it is a
            # quick win for the person and counts for partner programs.
            fit = 0.85
            why = f"validates the {RANK_LABELS[r]}-level {label} expertise you already have — likely a quick win"
        else:
            target = _entry_rank(profile.seniority) if p == 0 else min(p + 1, 4)
            if r == target:
                fit = 1.0
                why = (f"right next level in {label}" if p else
                       f"right entry point into {label} for your {profile.seniority.replace('_', '-')} profile")
            elif r == target + 1:
                fit = 0.7 if profile.seniority == SENIORITY_SENIOR else 0.5
                why = f"a stretch one level above your next step in {label}"
            elif r < target:
                fit = 0.4 if profile.seniority == SENIORITY_MID else 0.2
                why = f"optional warm-up — with your experience you can likely go straight to associate in {label}"
            else:
                fit = 0.1
                why = f"a big jump from your current level in {label}"
        if fit > best[0] or not best[2]:
            best = (fit, domain, why)
    return best


def _readiness(cert: CatalogCert, profile: LearnerProfile) -> tuple[float, str | None]:
    if cert.prerequisites:
        held = [n for n in cert.prerequisites if cert_key(None, n)[1] in profile.completed_keys]
        share = len(held) / len(cert.prerequisites)
        if share < 1:
            missing = [n for n in cert.prerequisites if n not in held]
            return max(share, 0.2), f"recommended prep first: {', '.join(missing)}"
        return 1.0, None
    if cert.rank >= 3:
        # Professional/expert with no domain evidence at associate+ at all.
        if not any((profile.proficiency.get(d).level if profile.proficiency.get(d) else 0) >= 2
                   for d in cert.domain_list):
            return 0.3, "no associate-level evidence in this area yet"
    return 1.0, None


def _urgency(deadline: date | None, today: date) -> float:
    if deadline is None:
        return 0.6
    days = (deadline - today).days
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.85
    return 0.7


def _business_need(cert: CatalogCert, profile: LearnerProfile, requirements: list[Requirement], today: date):
    best, programs, reasons = 0.0, [], []
    for req in requirements:
        if req.gap <= 0 or not req.matches(cert, profile.practice):
            continue
        need = (req.priority / 5) * (req.gap / max(req.required_count, 1)) * _urgency(req.deadline, today)
        programs.append(req.program_name)
        deadline = f", target {req.deadline.isoformat()}" if req.deadline else ""
        reasons.append(
            f"{req.program_name}: {req.current_count}/{req.required_count} certified, {req.gap} short{deadline}"
        )
        best = max(best, need)
    return min(best, 1.0), programs, reasons


def _relevance(cert: CatalogCert, profile: LearnerProfile) -> tuple[float, str | None]:
    score, why = 0.0, []
    practice = normalize_practice(profile.practice)
    if cert.practices:
        if practice and practice in {normalize_practice(x) for x in cert.practices}:
            score += 0.6
            why.append(f"mapped to your practice ({profile.practice})")
    else:
        score += 0.3  # untagged — neutral rather than penalized
    role_domains = infer_domains_from_role(profile.role)
    if role_domains & set(cert.domain_list):
        score += 0.4
        why.append(f"matches your role ({profile.role})")
    elif not role_domains and domains_for_practice(profile.practice) & set(cert.domain_list):
        # Title names no skill area (e.g. "Manager") — fall back to what the
        # person's practice/department works in.
        score += 0.3
        why.append(f"core skill area for {profile.practice}")
    return min(score, 1.0), "; ".join(why) or None


def _peer(cert: CatalogCert, peers: PeerStats) -> tuple[float, str | None]:
    ps = peers.practice_share.get(cert.key, 0.0)
    cs = peers.co_holder_share.get(cert.key, 0.0)
    score = min(1.0, 0.6 * ps + 0.4 * cs) if (ps or cs) else 0.0
    parts = []
    if ps >= 0.1:
        parts.append(f"held by {ps:.0%} of certified people in your practice")
    if cs >= 0.1:
        parts.append(f"{cs:.0%} of people with your certifications also hold it")
    return score, "; ".join(parts) or None


def recommend(
    profile: LearnerProfile,
    catalog: list[CatalogCert],
    requirements: list[Requirement],
    drives: list[DriveObligation],
    peers: PeerStats,
    dismissed: set,
    today: date,
    top_n: int = 5,
    catalog_source: str = "partner_catalog",
) -> RecommendationResult:
    by_key = {c.key: c for c in catalog}

    # Drive-required certs may not exist in the partner catalog — they
    # still have to be surfaced, so add them as ad-hoc candidates.
    mandatory: dict[tuple, str] = {}
    for d in drives:
        if d.is_compliant:
            continue
        for name in d.certification_names:
            match = next((c for c in catalog if c.key[1] == cert_key(None, name)[1]), None)
            cert = match or CatalogCert(provider="", name=name, source="drive")
            by_key.setdefault(cert.key, cert)
            due = f" (deadline {d.deadline.isoformat()})" if d.deadline else ""
            mandatory[cert.key] = f"required by mandatory drive '{d.drive_name}'{due}"

    renewals: dict[str, str] = {}
    catalog_names = {k[1] for k in by_key}
    for h in profile.completed:
        if h.expiry_date and (h.expiry_date - today).days <= RENEWAL_WINDOW_DAYS:
            state = "expired" if h.expiry_date < today else "expires"
            renewals[h.name_key] = f"your {h.name} {state} on {h.expiry_date.isoformat()} — renew to stay counted"
            if h.name_key not in catalog_names:
                c = CatalogCert(provider=h.provider or "", name=h.name, level=h.level, source="tracker")
                by_key[c.key] = c
    retries = {h.name_key for h in profile.failed}
    in_progress_names = {h.name_key for h in profile.in_progress}

    scored: list[Recommendation] = []
    skippable: list[Skippable] = []
    for key, cert in by_key.items():
        name = key[1]
        if name in in_progress_names and key not in mandatory:
            continue  # reported separately as "finish what you started"
        if key in dismissed and key not in mandatory:
            continue
        is_renewal = name in renewals
        if name in profile.completed_keys and not is_renewal:
            continue

        fit, domain, fit_why = _level_fit(cert, profile)
        need, programs, need_reasons = _business_need(cert, profile, requirements, today)
        if is_renewal:
            fit, fit_why = 1.0, "keeps an existing credential current"

        named_requirement = any(r.certification_name and r.matches(cert, profile.practice) and r.gap > 0
                                for r in requirements)
        if fit == 0.0 and key not in mandatory and not named_requirement:
            skippable.append(Skippable(cert.provider, cert.name, cert.level_name, f"You can skip this — {fit_why}."))
            continue

        readiness, ready_why = _readiness(cert, profile)
        relevance, rel_why = _relevance(cert, profile)
        goal = 1.0 if set(cert.domain_list) & set(profile.career_goals) else 0.0
        peer, peer_why = _peer(cert, peers)

        # A gap only counts in proportion to how well the cert fits the
        # person — never recommend a Professional exam to a fresher just
        # because the partnership is short on Professionals.
        effective_need = need * (0.4 + 0.6 * fit)
        breakdown = {
            "business_need": round(WEIGHTS["business_need"] * effective_need, 4),
            "level_fit": round(WEIGHTS["level_fit"] * fit, 4),
            "relevance": round(WEIGHTS["relevance"] * relevance, 4),
            "career_goal": round(WEIGHTS["career_goal"] * goal, 4),
            "peer_signal": round(WEIGHTS["peer_signal"] * peer, 4),
            "readiness": round(WEIGHTS["readiness"] * readiness, 4),
        }
        reasons: list[str] = []
        sources: list[str] = [cert.source]
        if key in mandatory:
            breakdown["mandatory"] = MANDATORY_BOOST
            reasons.append(mandatory[key][0].upper() + mandatory[key][1:])
            sources.append("mandatory_drive")
        if is_renewal:
            breakdown["renewal"] = RENEWAL_BOOST
            reasons.append(renewals[name][0].upper() + renewals[name][1:])
            sources.append("renewal")
        if name in retries:
            breakdown["retry"] = RETRY_BOOST
            reasons.append("You attempted this before — a retake builds on prep you've already done")
            sources.append("retry")
        reasons.extend(need_reasons)
        if need_reasons:
            sources.append("partnership_gap")
        if fit_why:
            reasons.append(fit_why[0].upper() + fit_why[1:])
        for extra in (rel_why, peer_why, ready_why):
            if extra:
                reasons.append(extra[0].upper() + extra[1:])
        if goal:
            reasons.append("In an area you said you want to grow in")
            sources.append("career_goal")
        if peer_why:
            sources.append("peer_signal")

        if key in mandatory:
            kind = "mandatory"
        elif is_renewal:
            kind = "renewal"
        elif name in retries:
            kind = "retry"
        elif need > 0:
            kind = "partnership_gap"
        elif fit >= 0.85:
            kind = "next_step"
        else:
            kind = "stretch"

        scored.append(Recommendation(
            provider=cert.provider,
            certification_name=cert.name,
            level=cert.level_name,
            domains=list(cert.domain_list),
            kind=kind,
            score=round(sum(breakdown.values()), 4),
            score_breakdown=breakdown,
            reasons=reasons,
            sources=sorted(set(sources), key=sources.index),
            partnership_programs=programs,
        ))

    scored.sort(key=lambda r: (-r.score, r.provider, r.certification_name))
    skippable.sort(key=lambda s: (s.provider, s.certification_name))
    return RecommendationResult(
        profile=profile,
        recommendations=rerank_with_diversity(scored, top_n),
        skippable=skippable[:10],
        catalog_size=len(catalog),
        catalog_source=catalog_source,
    )


# --- Focus (applied by engine.py after ranking): one pick, two only on a tie --
# Two scores within this margin are treated as the same priority.
TIE_MARGIN = 0.01


def focus_recommendations(ranked: list[Recommendation], tie_margin: float = TIE_MARGIN) -> list[Recommendation]:
    """The single most-needed certification. A second one is offered only
    when it's at the same priority as the first (score within tie_margin)
    — then the person gets a real choice between the two; otherwise more
    options just dilute the advice. Expects best-first order (as returned
    by recommend())."""
    if not ranked:
        return []
    top = ranked[0]
    tied = next((r for r in ranked[1:] if top.score - r.score <= tie_margin), None)
    return [top, tied] if tied else [top]


# --- 4. Diversity re-rank -----------------------------------------------------
def rerank_with_diversity(
    scored: list[Recommendation],
    top_n: int,
    max_per_provider: int = MAX_PER_PROVIDER,
    max_per_domain: int = MAX_PER_DOMAIN,
) -> list[Recommendation]:
    """Greedy capped selection: walk the list best-first, taking an item
    unless its partner or primary domain is already at its cap. Mandatory
    and renewal items are never held back. If caps leave the list short,
    backfill from what was held back, still in score order."""
    chosen, held_back = [], []
    per_provider: dict[str, int] = {}
    per_domain: dict[str, int] = {}
    for rec in scored:
        if len(chosen) >= top_n:
            break
        primary = rec.domains[0] if rec.domains else ""
        exempt = rec.kind in ("mandatory", "renewal")
        if not exempt and (per_provider.get(rec.provider, 0) >= max_per_provider
                           or per_domain.get(primary, 0) >= max_per_domain):
            held_back.append(rec)
            continue
        chosen.append(rec)
        per_provider[rec.provider] = per_provider.get(rec.provider, 0) + 1
        per_domain[primary] = per_domain.get(primary, 0) + 1
    for rec in held_back:
        if len(chosen) >= top_n:
            break
        chosen.append(rec)
    return chosen
