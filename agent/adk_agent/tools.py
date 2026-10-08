"""Tool functions the agent calls — thin wrappers over the backend's REST
API. No tool reasons about certification/verification status itself; every
one just forwards to the one backend service that owns the tracker, so the
agent and the dashboard (built later) can never disagree (Design Principle
3/4 in the BRD).

Every function that touches employee-scoped data takes `as_employee_id`,
the caller's own employee_id (filled by the model from the `{employee_id}`
value templated into its instruction — see prompts.py). That value is sent
as the X-Employee-Id header, which the backend re-validates and uses to
resolve access scope itself (services/access_scope.py) — the tool cannot
grant itself broader access than the backend independently decides to
allow, even if the model gets confused about who's asking.

In production (backend auth_mode="entra"), X-Employee-Id alone isn't
enough — the backend also requires a Google-signed ID token proving this
is really the deployed agent service calling (X-Internal-Service-Token,
attached by gcp_id_token.py below), checked against an allow-list of
trusted service accounts (see
backend/app/services/entra_auth.py::validate_service_id_token). Locally,
where BACKEND_URL is localhost, that token fetch is a no-op and this
behaves exactly as before.

Every tool below is `async def` using httpx.AsyncClient, not a plain sync
function — this is load-bearing, not stylistic. `adk api_server` runs
tools directly on the single asyncio event loop with no thread-pool
offload unless the tool itself is a coroutine (confirmed by reading
google/adk/tools/function_tool.py directly: a sync callable only gets
offloaded if something upstream has bound a "_SYNC_CALLABLE_RUNNER"
context var, which nothing in api_server's request path actually does).
A plain sync httpx.Client call blocks that entire process for its full
duration — with a multi-tool recommendation question chaining three
sequential backend calls, that was long enough to blow past Render's
5-second health-check timeout and get the whole instance killed mid-
request (confirmed live: Render's own event log showed "HTTP health
check failed (timed out after 5 seconds)" exactly during that flow,
resolved by this async rewrite — see DEVELOPMENT_LOG.md's 2026-08-30
entry).
"""

import asyncio
import os

import httpx

from . import gcp_id_token

_BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
if _BACKEND_URL and "://" not in _BACKEND_URL:
    # Render's fromService env var wiring supplies a bare host:port for a
    # private service, not a full URL — see render.yaml and the matching
    # normalization in backend/app/config.py.
    _BACKEND_URL = f"http://{_BACKEND_URL}"
_client = httpx.AsyncClient(base_url=_BACKEND_URL, timeout=20.0)


async def _headers(as_employee_id: str) -> dict:
    # fetch_service_token_header does a blocking google-auth call under
    # the hood (fast-failing off-GCP, a real metadata-server round-trip
    # on GCP) — run it off the event loop rather than await-ing it
    # in-line, for the same reason every tool call itself is async now.
    service_header = await asyncio.to_thread(gcp_id_token.fetch_service_token_header, _BACKEND_URL)
    return {"X-Employee-Id": as_employee_id, **service_header}


def _handle(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return {"status": "error", "error_message": f"{resp.status_code}: {detail}"}
    return {"status": "success", "data": resp.json()}


async def search_certification_catalog(query: str) -> dict:
    """Look up certifications in the partner certification catalog (AWS,
    Azure/Microsoft, Google Cloud, Databricks, Anthropic —
    every level). Use it to turn whatever the user called a certification
    ("SAA", "the Azure admin one", "databricks DE pro") into its exact
    catalog name before starting/discussing it, or to answer "what
    certifications does X offer?". Matches rows containing every word of
    the query (provider name counts), so try shorter or different words if
    nothing comes back — e.g. "solutions architect" rather than "SAA".

    Args:
        query: Words to search for, e.g. "aws solutions architect" or
            "fabric data engineer". An empty string returns the whole catalog.

    Returns:
        dict with status and, on success, a list of certifications
        (provider, certification_name, level, skill_domains,
        prerequisite_names, validity_months).
    """
    params = {"q": query} if query and query.strip() else {}
    resp = await _client.get("/api/portal/catalog", params=params)
    return _handle(resp)


async def get_my_certification_progress(as_employee_id: str) -> dict:
    """The caller's own certifications, one row per certification (its
    latest record): status (in_progress | completed | gave_exam_but_failed),
    verification, dates, level — and for anything in progress, business
    days of prep so far vs. that level's target (target_date, is_overdue,
    overdue_business_days). Rows with source="partner_portal" come from the
    partner-portal trackers (L&D's synced workbooks) — they count as on file
    too, just without a prep clock. Also returns the person's name
    (employee_name, first_name) and status counts. Use this for any "my status" / "what am
    I working on" / "how long have I been preparing" question, and to check
    whether a certification the user mentions is already in progress or
    completed before offering to start it.

    Args:
        as_employee_id: The employee_id of the person asking.
    """
    resp = await _client.get(
        "/api/certifications/my-progress",
        params={"employee_id": as_employee_id},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def start_certification_prep(as_employee_id: str, cert_name: str) -> dict:
    """Mark a certification as IN PROGRESS for the caller, starting their
    prep clock today (business days are counted from now, and they'll be
    reminded in the app if prep runs past the level's target). Only call
    this AFTER the user has said yes to starting it — never on your own.
    No form is needed. Pass the exact catalog name when you know it (from
    search_certification_catalog); the backend also matches close names.
    If it's already in progress or completed for them, nothing new is
    created (already_tracked=true) and the existing record is returned.

    Args:
        as_employee_id: The employee_id of the person.
        cert_name: The certification they're starting.

    Returns:
        dict with status and, on success, already_tracked plus the
        certification (cert_name, level, prep_start_date,
        target_business_days, target_date).
    """
    resp = await _client.post(
        "/api/certifications/start",
        json={"employee_id": as_employee_id, "cert_name": cert_name},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def get_next_certification_recommendations(as_employee_id: str, top_n: int = 5) -> dict:
    """Ranked next-certification recommendations for the caller, chosen
    ONLY from the partner certification catalog (AWS, Google/Gemini,
    Anthropic, Databricks, Azure). The backend does all the reasoning about
    eligibility and priority — this is the single source of truth for what
    to recommend; never recommend a certification that isn't in the
    returned `recommendations` list.

    Each recommendation has a `kind` (mandatory | renewal | retry |
    partnership_gap | next_step | stretch), `reasons` (already
    human-readable — quote/paraphrase them), `score_breakdown`, and
    `partnership_programs` (partner programs short on people with it).
    The result also includes:
    - profile: seniority (early_career/mid_career/senior + why),
      per-domain proficiency with its evidence, completed and in_progress
      certs, and missing_signals — profile fields that aren't on file yet
      (years_experience, expertise, career_goals, practice).
    - skippable: catalog certs this person can skip, with the reason
      (e.g. already associate-level in that area).
    - signals_unavailable: inputs that were empty (partner_catalog,
      partnership_requirements, peer_signal, partner_portal) — the ranking
      isn't based on these, say so if relevant.

    Args:
        as_employee_id: The employee_id of the person asking.
        top_n: Upper bound only (default 5, max 15). The backend returns the
            single most-needed certification, or two when tied at the same
            priority — that focused result is intentional.
    """
    resp = await _client.get(
        "/api/recommendations/next-certifications",
        params={"employee_id": as_employee_id, "top_n": top_n},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def update_my_skill_profile(
    as_employee_id: str,
    years_experience: float | None = None,
    expertise: dict | None = None,
    career_goals: list[str] | None = None,
) -> dict:
    """Save what the employee TELLS you about their experience, so
    recommendations can skip levels they don't need. Only call this with
    information the user actually stated — never guess or infer values.

    Args:
        as_employee_id: The employee_id of the person.
        years_experience: Total years of professional experience, if stated.
        expertise: Map of skill-domain key -> proficiency, either 0-4 or a
            level name: 0/"none", 1/"foundational", 2/"associate",
            3/"professional", 4/"expert". Merged into what's already saved.
            Domain keys: cloud_architecture, devops, software_development,
            data_engineering, data_analytics, machine_learning,
            generative_ai, security, networking, database.
            e.g. "I've run AWS infrastructure for 5 years" ->
            {"cloud_architecture": "associate", "devops": "associate"};
            a self-described expert/lead in an area -> "professional".
        career_goals: Domain keys the person wants to grow into (replaces
            the saved list).

    Returns:
        dict with status and, on success, the updated learner profile.
    """
    payload = {"employee_id": as_employee_id}
    if years_experience is not None:
        payload["years_experience"] = years_experience
    if expertise is not None:
        payload["expertise"] = expertise
    if career_goals is not None:
        payload["career_goals"] = career_goals
    resp = await _client.put("/api/recommendations/profile", json=payload, headers=await _headers(as_employee_id))
    return _handle(resp)


async def record_recommendation_feedback(
    as_employee_id: str, provider: str, certification_name: str, action: str, reason: str | None = None
) -> dict:
    """Record the user's reaction to a recommendation. action="dismissed"
    when they say they're not interested / it's not relevant (it's hidden
    from their recommendations for 90 days; mandatory-drive certs are never
    hidden); action="interested" when they say they'll pursue it.

    Args:
        as_employee_id: The employee_id of the person.
        provider: The recommendation's provider value exactly as returned.
        certification_name: The certification name exactly as returned.
        action: "dismissed" or "interested".
        reason: Optional — the user's own words.
    """
    resp = await _client.post(
        "/api/recommendations/feedback",
        json={"employee_id": as_employee_id, "provider": provider, "certification_name": certification_name,
              "action": action, "reason": reason},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def get_partnership_gaps() -> dict:
    """Org-wide partnership scale-up status: every active partner program
    requirement with required vs. currently-certified headcount and the
    gap, largest gap first. Aggregate counts only. Use for "where are we
    short on certifications for our partnerships?" questions.

    Returns:
        dict with status and, on success, a list of requirements (provider,
        program_name, certification_name/min_level/skill_domain filter,
        required_count, current_count, gap, deadline, priority).
    """
    resp = await _client.get("/api/recommendations/partnership-gaps")
    return _handle(resp)


async def submit_certification(
    as_employee_id: str,
    cert_name: str,
    submission_status: str,
    manager_name: str | None = None,
    practice_director_name: str | None = None,
    prep_start_date: str | None = None,
    exam_end_date: str | None = None,
    credly_link: str | None = None,
    expiration_date: str | None = None,
) -> dict:
    """Submit or update a certification record for the current employee.

    Args:
        as_employee_id: The employee_id of the person submitting.
        cert_name: Certification name — should match the catalog.
        submission_status: One of "in_progress", "completed", "gave_exam_but_failed".
        manager_name: Optional; prefilled from a prior submission if omitted.
        practice_director_name: Optional; prefilled from a prior submission if omitted.
        prep_start_date: ISO date string (YYYY-MM-DD), optional.
        exam_end_date: ISO date string (YYYY-MM-DD), optional.
        credly_link: Required and only meaningful when submission_status is "completed" —
            this is what triggers automated verification.
        expiration_date: ISO date string (YYYY-MM-DD), optional.

    Returns:
        dict with status and, on success, the created record plus the
        verification outcome if one was triggered.
    """
    payload = {
        "employee_id": as_employee_id,
        "cert_name": cert_name,
        "submission_status": submission_status,
        "manager_name": manager_name,
        "practice_director_name": practice_director_name,
        "prep_start_date": prep_start_date,
        "exam_end_date": exam_end_date,
        "credly_link": credly_link,
        "expiration_date": expiration_date,
    }
    resp = await _client.post(
        "/api/certifications/submit", json=payload, headers=await _headers(as_employee_id)
    )
    return _handle(resp)


async def get_tracker_records(
    as_employee_id: str,
    for_employee_id: str | None = None,
    submission_status: str | None = None,
    verification_status: str | None = None,
    cert_name: str | None = None,
) -> dict:
    """Query certification tracker records — status, verification, dates.

    Args:
        as_employee_id: The employee_id of the person asking (drives access scope).
        for_employee_id: Optional — a specific employee_id to narrow to (e.g. a
            direct report). Omit to see everyone the caller is allowed to see
            (themselves, plus direct reports / practice if applicable). If the
            caller isn't allowed to see this employee, the backend returns an
            error rather than data.
        submission_status: Optional filter — "in_progress" | "completed" | "gave_exam_but_failed".
        verification_status: Optional filter — "not_applicable" | "pending" | "verified" | "exception".
        cert_name: Optional partial-name filter.

    Returns:
        dict with status and, on success, matching records plus a data-as-of timestamp.
    """
    params = {}
    if for_employee_id:
        params["employee_id"] = for_employee_id
    if submission_status:
        params["submission_status"] = submission_status
    if verification_status:
        params["verification_status"] = verification_status
    if cert_name:
        params["cert_name"] = cert_name
    resp = await _client.get("/api/tracker/records", params=params, headers=await _headers(as_employee_id))
    return _handle(resp)


async def get_at_risk_certifications(as_employee_id: str, window_days: int = 30) -> dict:
    """Certifications expiring within window_days, scoped to who the caller can see.

    Args:
        as_employee_id: The employee_id of the person asking.
        window_days: How many days out counts as "at risk" (default 30).

    Returns:
        dict with status and, on success, at-risk records plus a data-as-of timestamp.
    """
    resp = await _client.get(
        "/api/tracker/at-risk",
        params={"window_days": window_days},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def query_knowledge_base(question: str, top_k: int = 3) -> dict:
    """Semantic search over curated exam-prep knowledge-base content.

    Args:
        question: The exam-prep / certification question to search for.
        top_k: How many matches to return (default 3).

    Returns:
        dict with status and, on success, matches with a similarity score
        (0-1) — treat matches below ~0.6 similarity as thin/no coverage
        rather than a confident answer.
    """
    resp = await _client.post("/api/kb/query", json={"question": question, "top_k": top_k})
    return _handle(resp)


async def create_escalation(as_employee_id: str, question: str, context: str | None = None) -> dict:
    """Escalate a question to the routed SME/L&D owner when the knowledge
    base has no confident answer.

    Args:
        as_employee_id: The employee_id of the person asking.
        question: The question to escalate, verbatim.
        context: Optional extra context (e.g. what the agent already tried).

    Returns:
        dict with status and, on success, the created escalation (routed
        owner, SLA due date, status).
    """
    resp = await _client.post(
        "/api/escalations",
        json={"employee_id": as_employee_id, "question": question, "context": context},
    )
    return _handle(resp)


async def get_my_drive_obligations(as_employee_id: str) -> dict:
    """Get the caller's own mandatory certification drives ("campaigns") —
    e.g. "everyone in Data+AI must get an AWS cert by Q4" — that they're
    currently enrolled in. Closed drives are excluded. Structured metadata
    only (deadline, required certifications, compliance status) — this is
    a direct tool lookup, not RAG, so never answer a drive-obligation
    question from query_knowledge_base or general knowledge.

    Args:
        as_employee_id: The employee_id of the person asking.

    Returns:
        dict with status and, on success, obligations (a list — each with
        drive_name, required_certifications, deadline, enrollment_status,
        is_compliant) plus a data-as-of timestamp. An empty list means no
        active mandatory drives currently target this employee.
    """
    resp = await _client.get(
        "/api/drives/my-obligations",
        params={"employee_id": as_employee_id},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def open_certification_form(as_employee_id: str) -> dict:
    """Open the certification submission form in the chat UI, pre-filled
    with the employee's manager/practice director (from their most recent
    prior submission, or their directory record if they have none) and
    their practice. Call this whenever the user says they want to submit,
    log, or update a certification — don't try to collect cert name,
    status, dates, etc. yourself through chat questions; the form handles
    that. Never ask the user for their practice — it's already in their
    record and this tool returns it.

    Args:
        as_employee_id: The employee_id of the person asking.

    Returns:
        dict with status and, on success, prefill data (manager_name,
        practice_director_name, practice) for the form.
    """
    resp = await _client.get(f"/api/employees/{as_employee_id}/prefill", headers=await _headers(as_employee_id))
    result = _handle(resp)
    if result.get("status") == "success":
        result["action"] = "open_certification_form"
    return result
