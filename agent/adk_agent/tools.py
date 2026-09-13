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
    """Search the certification catalog by name (autocomplete/lookup).

    Args:
        query: Partial or full certification name to search for.

    Returns:
        dict with status and, on success, a list of matching certifications
        (name, provider, level, prerequisites, validity period, status).
    """
    resp = await _client.get("/api/catalog/certifications", params={"q": query})
    return _handle(resp)


async def get_next_certification_for_practice(as_employee_id: str) -> dict:
    """Get the next recommended certification in the caller's own practice's
    learning path, skipping ones they've already completed. The practice is
    looked up from the employee's own record server-side — never ask the
    user which practice they're in, they're already assigned to one.

    IMPORTANT: this returns a LIST (next_certifications), not a single
    certification — the underlying path is a prerequisite graph, not a
    straight line, so more than one certification can be eligible at once
    (a real branch point, e.g. one completed cert opening up two valid
    next options) and that's normal, not an error. An empty list means the
    path is either fully completed or has nothing configured yet — check
    which by looking at whether path_name is set. Never collapse multiple
    results down to "pick one" on the user's behalf; present all of them
    (grouped by track_label where present) and let the user choose.

    If the response has practice_missing=true, this employee has no
    practice on file yet (e.g. a brand-new Teams user, auto-provisioned on
    first contact, before any directory sync or admin has filled it in) —
    say so plainly ("I don't have your practice on file yet, so I can't
    recommend a path — L&D can add it") and stop there. Never guess a
    practice or default to one; this is different from an empty
    next_certifications list with a real practice set.

    Args:
        as_employee_id: The employee_id of the person asking.

    Returns:
        dict with status and, on success, next_certifications (a list,
        each with its own certification details, optional track_label,
        and reason) plus a data-as-of timestamp.
    """
    resp = await _client.get(
        "/api/learning-paths/next",
        params={"employee_id": as_employee_id},
        headers=await _headers(as_employee_id),
    )
    return _handle(resp)


async def get_learning_path_overview(as_employee_id: str) -> dict:
    """Get the FULL certification prerequisite graph for the caller's own
    practice path, with their own status against every certification in
    it (completed / in_progress / gave_exam_but_failed / not_started) —
    not just the eligible-now frontier.

    Call this together with get_next_certification_for_practice (the
    rule-based ground truth of what's unlocked right now) and
    get_tracker_records (full submission history — dates, verification,
    failed attempts) whenever you're making a real recommendation, not
    just answering "what's next": this is the context that lets you
    explain WHY something is or isn't available, which locked track
    someone is closest to finishing, or which failed attempt they should
    probably retry before starting something new. Never use this to
    decide eligibility yourself — every certification's actual
    availability is exactly and only what get_next_certification_for_practice
    returns; this tool exists to add reasoning and personalization on top
    of that ground truth, never to override it.

    Args:
        as_employee_id: The employee_id of the person asking.

    Returns:
        dict with status and, on success, practice, path_name, and
        certifications (a list — each with the certification's catalog
        details, is_required, employee_status, prerequisite_logic, and
        incoming_prerequisites: the exact named edges required to unlock
        it) plus a data-as-of timestamp. If practice_missing=true, this
        employee has no practice on file yet — say so plainly rather than
        guessing one; same meaning as on get_next_certification_for_practice.
    """
    resp = await _client.get(
        "/api/learning-paths/overview",
        params={"employee_id": as_employee_id},
        headers=await _headers(as_employee_id),
    )
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
