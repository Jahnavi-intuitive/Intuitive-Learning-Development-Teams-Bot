"""Offline tests for the pure recommender core (services/recommender/core.py)
— no database needed, runs against the real seed partner catalog
(scripts/seed_data/partner_catalog.json)."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services.recommender import core
from app.services.recommender.taxonomy import cert_key, normalize_level, normalize_practice, parse_proficiency

TODAY = date(2026, 10, 1)
_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_data" / "partner_catalog.json"


@pytest.fixture(scope="module")
def catalog():
    entries = json.loads(_SEED.read_text(encoding="utf-8"))["certifications"]
    return [
        core.CatalogCert(
            provider=e["provider"], name=e["certification_name"], level=e["level"],
            domains=tuple(e["skill_domains"]), practices=tuple(e["relevant_practices"]),
            prerequisites=tuple(e["prerequisite_names"]), validity_months=e["validity_months"],
        )
        for e in entries
    ]


def _profile(years=None, expertise=None, goals=None, held=(), practice="Cloud & Platform Engineer", role=None):
    return core.build_profile(core.ProfileInput(
        employee_id="E1", name="Test Person", practice=practice, role=role, years_experience=years,
        declared_expertise=expertise or {}, career_goals=goals or [], held=list(held),
    ), TODAY)


def _run(profile, catalog, requirements=(), drives=(), peers=None, dismissed=(), top_n=5):
    return core.recommend(profile, catalog, list(requirements), list(drives), peers or core.PeerStats(),
                          set(dismissed), TODAY, top_n=top_n)


def _names(result):
    return [r.certification_name for r in result.recommendations]


# --- taxonomy ---------------------------------------------------------------
@pytest.mark.parametrize("level,name,expected", [
    (None, "AWS Certified Cloud Practitioner", "foundational"),
    (None, "Claude Certified Associate - Foundations", "foundational"),
    (None, "Databricks Certified Data Engineer Professional", "professional"),
    (None, "Google Cloud Associate Cloud Engineer", "associate"),
    (None, "AWS Certified Security - Specialty", "specialty"),
    (None, "Microsoft Certified: Azure Solutions Architect Expert", "expert"),
    ("Associate (Intermediate)", "whatever", "associate"),
    (None, "Meta Back-End Developer Professional Certificate", "associate"),
    (None, "Something Unlabelled", None),
])
def test_normalize_level(level, name, expected):
    assert normalize_level(level, name) == expected


def test_practice_and_key_normalization():
    assert normalize_practice("Data+AI") == normalize_practice("Data + AI")
    assert normalize_practice("Cloud & Platform Engineer") == normalize_practice("Cloud + Platform Engineering")
    assert cert_key("AWS", "AWS Certified Solutions Architect – Associate") == \
        cert_key("aws", "AWS Certified Solutions Architect - Associate")


def test_parse_proficiency():
    assert parse_proficiency("associate") == 2
    assert parse_proficiency(3) == 3
    with pytest.raises(ValueError):
        parse_proficiency(9)


# --- fresher vs experienced -----------------------------------------------------
def test_fresher_starts_at_foundational(catalog):
    result = _run(_profile(years=0.5), catalog)
    assert result.profile.seniority == core.SENIORITY_EARLY
    top = result.recommendations[0]
    assert top.level == "foundational"
    assert all(r.level not in ("professional", "expert") for r in result.recommendations[:3])


def test_experienced_hire_skips_foundational_with_declared_expertise(catalog):
    profile = _profile(years=8, expertise={"cloud_architecture": 2, "devops": 2})
    result = _run(profile, catalog)
    assert result.profile.seniority == core.SENIORITY_SENIOR
    skipped = {s.certification_name for s in result.skippable}
    assert "AWS Certified Cloud Practitioner" in skipped
    assert "Microsoft Certified: Azure Fundamentals" in skipped
    assert "AWS Certified Cloud Practitioner" not in _names(result)
    # Declared-but-uncertified expertise is NOT a credential: the associate
    # cert at the declared level stays available as a "validate it" quick win.
    assert "AWS Certified Solutions Architect - Associate" not in skipped
    full = _run(profile, catalog, top_n=15)
    sa = next(r for r in full.recommendations if r.certification_name == "AWS Certified Solutions Architect - Associate")
    assert any("quick win" in reason for reason in sa.reasons)
    # Next steps are professional-level in the declared domains.
    assert any(r.level in ("professional", "specialty", "expert")
               and {"cloud_architecture", "devops"} & set(r.domains) for r in result.recommendations)
    assert all(r.level != "foundational" for r in result.recommendations)


def test_senior_with_no_domain_evidence_enters_at_associate(catalog):
    result = _run(_profile(years=10, practice="Data+AI"), catalog)
    top = result.recommendations[0]
    assert top.level in ("associate", "specialty")


def test_cross_partner_evidence_makes_other_partner_foundational_skippable(catalog):
    held = [core.HeldCert(provider="aws", name="AWS Certified Solutions Architect - Associate", status="completed")]
    result = _run(_profile(held=held), catalog)
    assert result.profile.seniority == core.SENIORITY_MID  # inferred from the associate cert
    skipped = {s.certification_name for s in result.skippable}
    assert "Microsoft Certified: Azure Fundamentals" in skipped
    assert "Google Cloud Digital Leader" in skipped
    assert "AWS Certified Solutions Architect - Associate" not in _names(result)
    assert "years_experience" in result.profile.missing_signals


# --- partnership gaps ---------------------------------------------------------------
def test_partnership_gap_boosts_matching_cert(catalog):
    profile = _profile(years=4, practice="Data+AI")
    base = _run(profile, catalog, top_n=15)
    req = core.Requirement(
        requirement_id="r1", provider="databricks", program_name="Databricks partner scale-up",
        required_count=10, current_count=2, certification_name="Databricks Certified Data Engineer Associate",
        deadline=TODAY + timedelta(days=45), priority=5,
    )
    boosted = _run(profile, catalog, requirements=[req], top_n=15)
    assert boosted.recommendations[0].certification_name == "Databricks Certified Data Engineer Associate"
    assert boosted.recommendations[0].kind == "partnership_gap"
    assert "Databricks partner scale-up" in boosted.recommendations[0].partnership_programs
    assert any("8 short" in reason for reason in boosted.recommendations[0].reasons)
    before = {r.certification_name: r.score for r in base.recommendations}
    assert boosted.recommendations[0].score > before.get("Databricks Certified Data Engineer Associate", 0)


def test_met_requirement_adds_nothing(catalog):
    req = core.Requirement("r1", "aws", "AWS tier", required_count=5, current_count=7, min_level="professional")
    result = _run(_profile(years=8, expertise={"cloud_architecture": 2}), catalog, requirements=[req], top_n=15)
    assert all(r.score_breakdown["business_need"] == 0 for r in result.recommendations)


def test_gap_does_not_push_unfit_professional_above_fresher_next_step(catalog):
    req = core.Requirement("r1", "aws", "AWS Advanced Tier", required_count=4, current_count=0,
                           min_level="professional", priority=5, deadline=TODAY + timedelta(days=20))
    result = _run(_profile(years=0), catalog, requirements=[req], top_n=15)
    top = result.recommendations[0]
    assert top.level == "foundational"


def test_practice_scoped_requirement_ignored_for_other_practice(catalog):
    req = core.Requirement("r1", "azure", "Azure Data push", required_count=5, current_count=0,
                           domain="data_engineering", practice="Data+AI", priority=5)
    result = _run(_profile(years=3, practice="NetSec + SDx"), catalog, requirements=[req], top_n=15)
    assert all(not r.partnership_programs for r in result.recommendations)


# --- drives, renewals, retries, in-progress, dismissals ----------------------------
def test_mandatory_drive_is_pinned_even_outside_catalog(catalog):
    drive = core.DriveObligation("Q4 security drive", ("Some Internal Security Cert",), TODAY + timedelta(days=30), False)
    result = _run(_profile(years=5), catalog, drives=[drive])
    assert result.recommendations[0].certification_name == "Some Internal Security Cert"
    assert result.recommendations[0].kind == "mandatory"


def test_expiring_cert_is_recommended_for_renewal(catalog):
    held = [core.HeldCert("aws", "AWS Certified Solutions Architect - Associate", "completed",
                          expiry_date=TODAY + timedelta(days=20))]
    result = _run(_profile(held=held), catalog)
    renewal = [r for r in result.recommendations if r.kind == "renewal"]
    assert renewal and renewal[0].certification_name == "AWS Certified Solutions Architect - Associate"


def test_in_progress_and_dismissed_are_excluded(catalog):
    held = [core.HeldCert("aws", "AWS Certified Cloud Practitioner", "in_progress")]
    dismissed = {cert_key("aws", "AWS Certified AI Practitioner")}
    result = _run(_profile(years=0, held=held), catalog, dismissed=dismissed, top_n=15)
    names = _names(result)
    assert "AWS Certified Cloud Practitioner" not in names
    assert "AWS Certified AI Practitioner" not in names
    assert [h.name for h in result.profile.in_progress] == ["AWS Certified Cloud Practitioner"]


def test_failed_attempt_is_flagged_as_retry(catalog):
    held = [core.HeldCert("databricks", "Databricks Certified Data Engineer Associate", "failed")]
    result = _run(_profile(years=3, practice="Data+AI", held=held), catalog, top_n=15)
    retry = next(r for r in result.recommendations if r.certification_name == held[0].name)
    assert retry.kind == "retry"


# --- peers, goals, diversity, empty ---------------------------------------------
def test_peer_signal_and_career_goal_break_ties(catalog):
    peers = core.PeerStats(practice_share={cert_key("azure", "Microsoft Certified: Azure AI Fundamentals"): 0.6})
    result = _run(_profile(years=0, practice="Data+AI", goals=["generative_ai"]), catalog, peers=peers, top_n=15)
    names = _names(result)
    assert names.index("Microsoft Certified: Azure AI Fundamentals") < names.index("Microsoft Certified: Azure Data Fundamentals")


def test_diversity_caps_per_provider(catalog):
    result = _run(_profile(years=0), catalog, top_n=5)
    providers = [r.provider for r in result.recommendations]
    assert max(providers.count(p) for p in set(providers)) <= core.MAX_PER_PROVIDER


def test_empty_catalog_returns_empty_not_error():
    result = _run(_profile(years=3), [])
    assert result.recommendations == [] and result.catalog_size == 0


def test_every_recommendation_is_from_catalog_or_obligation(catalog):
    keys = {c.key for c in catalog}
    result = _run(_profile(years=6, expertise={"data_engineering": 3}, practice="Data+AI"), catalog, top_n=15)
    assert all(cert_key(r.provider, r.certification_name) in keys for r in result.recommendations)


# --- designation / practice / no-partner-data fallback ---------------------
def test_manager_title_is_senior_without_experience_on_file(catalog):
    profile = _profile(role="Managar (App + DB Mod)", practice="App + DB Mod")
    assert profile.seniority == core.SENIORITY_SENIOR
    assert "designation" in profile.seniority_basis
    result = _run(profile, catalog)
    assert result.recommendations
    # Senior with no domain evidence enters at associate, not foundational.
    assert all(r.level != "foundational" for r in result.recommendations[:3])


def test_intern_title_stays_early_career():
    assert _profile(role="Software Engineering Intern").seniority == core.SENIORITY_EARLY


def test_practice_domains_drive_relevance_when_title_names_no_skill(catalog):
    result = _run(_profile(role="Manager", practice="Data+AI"), catalog)
    domains = {d for r in result.recommendations for d in r.domains}
    assert domains & {"data_engineering", "data_analytics", "machine_learning", "generative_ai"}


def test_recommends_with_no_requirements_drives_or_peers(catalog):
    # Nothing from the partner side at all — still a full list from the catalog.
    result = _run(_profile(role="Cloud Engineer"), catalog)
    assert len(result.recommendations) == 5


def test_bundled_catalog_loads():
    from app.services.recommender import engine
    bundled = engine._bundled_catalog()
    assert len(bundled) >= 40 and all(c.source == "bundled_catalog" for c in bundled)


def test_match_practice_is_space_and_ampersand_insensitive():
    from app.models import PRACTICE_VALUES
    from app.services.recommender.taxonomy import match_practice
    assert match_practice("Data + AI", PRACTICE_VALUES) == "Data+AI"
    assert match_practice("Cloud and Platform", PRACTICE_VALUES) is None


# --- focus: one recommendation, two only on a same-priority tie ------------
def _rec(name, score):
    return core.Recommendation(provider="aws", certification_name=name, level="associate", domains=[],
                               kind="next_step", score=score, score_breakdown={}, reasons=[], sources=[],
                               partnership_programs=[])


def test_focus_returns_single_clear_winner():
    assert [r.certification_name for r in core.focus_recommendations([_rec("A", 0.80), _rec("B", 0.70)])] == ["A"]


def test_focus_offers_choice_only_on_tie():
    out = core.focus_recommendations([_rec("A", 0.512), _rec("B", 0.512), _rec("C", 0.512)])
    assert [r.certification_name for r in out] == ["A", "B"]  # never more than two


def test_focus_empty():
    assert core.focus_recommendations([]) == []
