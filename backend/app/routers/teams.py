"""Bot Framework webhook — the real Microsoft Teams entry point.

POST /api/teams/messages receives one Activity per incoming Teams message.
Every request goes through three hard checks, in order, before anything
else runs (plus a fourth, non-identity check — see services/activity_dedup.py
— against Bot Service redelivering the same activity, applied right after
these three and before any real work starts):

1. **Bot Framework signature/issuer validation**
   (`JwtTokenValidation.authenticate_request`) — the SDK's own JWT
   verification, never hand-rolled token parsing (per the working
   agreement). Proves this genuinely came from Azure Bot Service.
2. **Tenant restriction** — `channelData.tenant.id` must match
   `settings.teams_tenant_id`. Belt-and-suspenders alongside Teams' own
   app-catalog scoping and the bot registration's Single-Tenant app type;
   not a replacement for either.
3. **Identity resolution** — `activity.from.aadObjectId` is looked up
   against `employees.aad_object_id`, server-side. There is no
   `employee_id` anywhere in this request's payload at all — the opposite
   trust direction from the local test UI's X-Employee-Id header, which is
   why this can't reuse that dependency. No match does **not** block the
   conversation: Microsoft Graph is queried for this identity's real
   directory profile (`services/graph_client.py`) and a new `employees`
   row is provisioned from *that*, in the same request (see
   `_auto_provision_employee` below), and the chat proceeds immediately.
   An earlier version of this file required an admin to manually link a
   new employee's Teams identity before the bot would respond to them at
   all — reverted (see DEVELOPMENT_LOG.md's 2026-09-07 entry) because that
   made a legitimate employee wait on an admin just to get a first reply,
   which defeats the point of a self-service bot. The tenant check above
   is the real "keep outsiders out" boundary; identity resolution here is
   about recognizing *which* real employee is asking, never about
   gatekeeping *whether* they may ask at all.

   Fields on the new row are **never taken from the Teams activity
   itself** (2026-09-12 change) — `activity.from.name`/`.aadObjectId` are
   just the trigger to look someone up, not an authoritative data source a
   caller could spoof. Only Microsoft Graph (or an existing `employees`
   row) is trusted for `name`/`email`/`role`/`manager_name`. If the Graph
   lookup fails for any reason (not configured, network error, non-2xx),
   no row is created at all — a clean "try again shortly" reply is sent
   instead, rather than persisting a half-real record that never gets
   revisited. `practice`/`practice_director_name` stay null either way —
   no HRIS/Graph source for those exists yet; that's still a SEAM.

Only after all three pass does this call `run_agent_chat` — the same core
agent_proxy.py's local-UI `/api/agent/chat` endpoint uses (same rate
limiting, same agent call, same reply parsing), just with a server-
resolved identity instead of a client-supplied one, and a Bot Framework
reply instead of an HTTP JSON response.

This route IS `async def`, unlike every other router in this backend —
a deliberate, tested exception, not an inconsistency. The first version of
this file made the route plain sync and used `asyncio.run(...)` per
request to call the one genuinely-async SDK call (matching every sibling
router's style, since a sync FastAPI route already gets auto-offloaded to
a worker thread). That turned out to be a real bug, not just a stylistic
mismatch: reproduced directly against real concurrent traffic (Azure Bot
Service retries the same activity several times in quick succession when
it doesn't get a fast reply) — 10 concurrent `asyncio.run()` calls in 10
threadpool workers, each spinning up its own throwaway event loop and its
own fresh HTTPS connection to Microsoft's OpenID metadata endpoint, failed
with `SSLEOFError: EOF occurred in violation of protocol` on every single
one, while the exact same call made sequentially (or even 10x concurrently
via plain `requests` with no `asyncio.run()` involved) succeeded every
time. The one shared event loop this project's normal uvicorn process
already runs doesn't have that problem — only creating *many* of them
concurrently, one per worker thread, does. So: this route awaits the SDK's
JWT validation directly on that one shared loop, and everything else
(the DB employee lookup, `run_agent_chat`, sending the reply — all
sync/blocking) is dispatched to `asyncio.to_thread(...)` instead of ever
touching the loop directly, for the same reason `tools.py`'s async
rewrite exists (see DEVELOPMENT_LOG.md's 2026-08-30 entry) — never call a
blocking function directly from code running on the shared loop."""

import asyncio
import logging

from botbuilder.schema import Activity, Attachment
from botframework.connector import ConnectorClient
from botframework.connector.auth import (
    JwtTokenValidation,
    MicrosoftAppCredentials,
    SimpleCredentialProvider,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import PRACTICE_VALUES, Employee
from app.routers.agent_proxy import run_agent_chat
from app.routers.certifications import submit_certification
from app.schemas import SubmissionIn
from app.services import activity_dedup, teams_auth_resilience
from app.services.audit import serialize_row, write_audit_entry
from app.services.graph_client import GraphLookupError, GraphUser, get_user_with_manager

teams_auth_resilience.apply()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams", tags=["teams"])


def _auto_provision_employee(db: Session, aad_object_id: str, graph_user: GraphUser) -> Employee:
    """First-contact auto-provisioning: an unrecognized Teams identity gets
    its own employees row immediately, in the same request that triggered
    it — no admin step, no delay. Populated from a real Microsoft Graph
    lookup (`graph_user`), never from the Teams activity payload itself
    (see the module docstring's 2026-09-12 note) — `name`/`email`/`role`/
    `manager_name` all come from Graph's own `displayName`/`mail`/
    `jobTitle`/manager relationship. `practice` is taken from Graph's
    `department` attribute, but only if it's an *exact* match for one of
    `PRACTICE_VALUES` (models.py) — `practice` is a strict Postgres enum,
    not free text, so a department value that doesn't match one of the
    four known practices exactly (case-sensitive) is left null rather than
    risking an enum-violation crash or silently inventing a mapping. This
    means practice enrichment depends entirely on Entra's `department`
    field actually being set to one of those four exact strings; anything
    else (blank, a different spelling, a free-text department name) falls
    back to null, same as before — every practice-dependent feature
    already treats a null practice as a real, distinct "not set up yet"
    case (routers/learning_paths.py). `practice_director_name` still has
    no source at all — still a SEAM. `employee_id` (this system's business
    key everywhere else — tracker
    records, ADK session/user IDs, rate limiting) has no HRIS equivalent to
    draw on yet either, so it's set to the aad_object_id itself: unique,
    stable, always available, and never collides with a real "E1234"-
    shaped HRIS id.

    is_directory_synced=True here (unlike the pre-Graph version of this
    function) because this row's identity fields were just genuinely
    verified against Microsoft's own directory, not guessed from a chat
    payload — the flag means "reconciled with an authoritative source",
    and that's exactly what just happened; it doesn't mean practice was
    necessarily resolved too (tracked separately, by the column being
    null or not). practice_director_name remains unenriched either way.

    Audited exactly like every other state change in this system, with
    the one difference that no employee actor exists to attribute it to
    yet — actor="system", same pattern the reminder-sweep job
    (services/notification_sweep.py) already uses for its own writes."""
    department = (graph_user.department or "").strip()
    practice = department if department in PRACTICE_VALUES else None
    if department and practice is None:
        logger.warning(
            "Graph department %r for aad_object_id=%s doesn't match any known practice "
            "(%s) — leaving practice unset.",
            department,
            aad_object_id,
            ", ".join(PRACTICE_VALUES),
        )

    employee = Employee(
        employee_id=aad_object_id,
        aad_object_id=aad_object_id,
        name=graph_user.display_name,
        email=graph_user.email,
        role=graph_user.job_title,
        manager_name=graph_user.manager_name,
        practice=practice,
        is_test_data=False,
        is_admin=False,
        is_directory_synced=True,
    )
    db.add(employee)
    db.flush()  # assign the PK before audit-logging it
    write_audit_entry(
        db,
        entity_type="employees",
        entity_id=employee.id,
        action="employee_auto_created",
        actor="system",
        old_value=None,
        new_value=serialize_row(employee),
    )
    db.commit()
    db.refresh(employee)
    logger.info(
        "Auto-provisioned a new employee for an unrecognized Teams user (employee_id=%s, name=%s).",
        employee.employee_id,
        employee.name,
    )
    return employee


_STATUS_CHOICES = [
    {"title": "In progress", "value": "in_progress"},
    {"title": "Completed", "value": "completed"},
    {"title": "Attempted - not cleared", "value": "gave_exam_but_failed"},
]
_STATUS_LABELS = {c["value"]: c["title"] for c in _STATUS_CHOICES}


def _build_certification_form_card(prefill: dict) -> Attachment:
    """Step 1 of 2 — the Teams equivalent of the local test UI's actual
    HTML submission form (frontend/index.html); that form only exists in
    the browser, so without this the agent's open_certification_form
    action had nothing to render in Teams at all. manager_name/
    practice_director_name are carried in the Submit action's own `data`
    rather than as editable inputs — same "never ask the user for
    something already known server-side" rule tools.py's own docstring
    states for the chat path.

    Deliberately just cert name + status here, not every field at once:
    which of exam_end_date/credly_link even make sense depends on the
    status the user is about to pick (you can't have an exam end date or
    a badge link for something still in progress), and a static Adaptive
    Card can't conditionally show/hide a field based on another field's
    live value without a round trip — so step 2 (built once status is
    known) only asks for what's actually applicable. expiration_date is
    never asked at all, on either step — perform_verification derives it
    from the Credly badge itself once verified (services/verification.py),
    so a manually-typed value would just be second-guessing that.
    """
    practice = prefill.get("practice") or "Not on file"
    manager_name = prefill.get("manager_name") or "Not on file"
    practice_director_name = prefill.get("practice_director_name") or "Not on file"
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "Submit a certification", "weight": "Bolder", "size": "Medium"},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Practice", "value": practice},
                    {"title": "Manager", "value": manager_name},
                    {"title": "Practice Director", "value": practice_director_name},
                ],
            },
            {
                "type": "Input.Text",
                "id": "cert_name",
                "label": "Certification name",
                "placeholder": "e.g. AWS Certified Solutions Architect - Associate",
                "isRequired": True,
                "errorMessage": "Certification name is required.",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "submission_status",
                "label": "Status",
                "style": "compact",
                "value": "in_progress",
                "isRequired": True,
                "choices": _STATUS_CHOICES,
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Continue",
                "data": {
                    "form_type": "certification_form_basics",
                    "manager_name": prefill.get("manager_name"),
                    "practice_director_name": prefill.get("practice_director_name"),
                },
            }
        ],
    }
    return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card)


def _build_certification_details_card(
    cert_name: str, submission_status: str, manager_name: str | None, practice_director_name: str | None
) -> Attachment:
    """Step 2 of 2 — only the fields that make sense for the status chosen
    in step 1. Per direct instruction, exam_end_date and credly_link are
    editable only when submission_status == "completed" — not for
    in_progress (nothing to date or link yet) and not for
    gave_exam_but_failed either (no badge; the attempt date isn't asked
    for at submission time). credly_link is required when shown, matching
    routers/certifications.py's own 422 if it's missing on a completed
    submission. expiration_date never appears here either, same reason as
    step 1's docstring."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"{cert_name} — {_STATUS_LABELS.get(submission_status, submission_status)}",
            "weight": "Bolder",
            "wrap": True,
        },
        {"type": "Input.Date", "id": "prep_start_date", "label": "Prep start date (optional)"},
    ]
    if submission_status == "completed":
        body.append({"type": "Input.Date", "id": "exam_end_date", "label": "Exam / completion date (optional)"})
        body.append(
            {
                "type": "Input.Text",
                "id": "credly_link",
                "label": "Credly badge link",
                "placeholder": "https://www.credly.com/badges/...",
                "isRequired": True,
                "errorMessage": "A Credly badge link is required for a completed certification.",
            }
        )
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Submit",
                "data": {
                    "form_type": "certification_submission",
                    "cert_name": cert_name,
                    "submission_status": submission_status,
                    "manager_name": manager_name,
                    "practice_director_name": practice_director_name,
                },
            }
        ],
    }
    return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card)


def _handle_certification_form_basics(activity: Activity, connector: ConnectorClient) -> None:
    """Handles step 1's Continue — sends step 2, tailored to the status
    just picked. Never touches the database; nothing's actually submitted
    until step 2's own Action.Submit."""
    value = activity.value if isinstance(activity.value, dict) else {}
    cert_name = (value.get("cert_name") or "").strip()
    submission_status = value.get("submission_status") or ""
    if not cert_name or submission_status not in _STATUS_LABELS:
        reply = activity.create_reply("Sorry, that didn't come through right — please try submitting again.")
        connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
        return

    reply = activity.create_reply("Just a couple more details:")
    reply.attachments = [
        _build_certification_details_card(
            cert_name, submission_status, value.get("manager_name"), value.get("practice_director_name")
        )
    ]
    connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)


def _handle_certification_form_submission(
    activity: Activity, connector: ConnectorClient, employee: Employee, db: Session
) -> None:
    """Step 2's Action.Submit — the actual write. Even if a card had
    existed at all before this fix, its Action.Submit arrives as a Teams
    activity with `value` set and no `text` at all — the old code's
    `if not user_text: return` above would have silently swallowed it.
    Calls the same submit_certification the local test UI's form posts to
    directly (same verification, drive-enrollment linking, and SharePoint
    write-back), never through the LLM — this is a structured submission,
    not a conversational one. expiration_date is intentionally never read
    from `value` here — it's never on either card; perform_verification
    fills it in from the Credly badge itself once verified."""
    value = activity.value if isinstance(activity.value, dict) else {}
    try:
        payload = SubmissionIn(
            employee_id=employee.employee_id,
            cert_name=(value.get("cert_name") or "").strip(),
            submission_status=value.get("submission_status") or "",
            manager_name=value.get("manager_name") or None,
            practice_director_name=value.get("practice_director_name") or None,
            prep_start_date=value.get("prep_start_date") or None,
            exam_end_date=value.get("exam_end_date") or None,
            credly_link=value.get("credly_link") or None,
        )
    except ValidationError as exc:
        reply = activity.create_reply(f"Sorry, that submission looked malformed: {exc}")
        connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
        return

    try:
        result = submit_certification(payload, db=db, current=employee)
    except HTTPException as exc:
        reply = activity.create_reply(f"Sorry, couldn't submit that: {exc.detail}")
        connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
        return

    record = result.record
    reply_text = (
        f"Recorded **{record.cert_name}** as **{record.submission_status}** "
        f"(verification: {record.verification_status})."
    )
    reply = activity.create_reply(reply_text)
    connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)


def _handle_recognized_or_reply(activity: Activity, db: Session) -> None:
    """Everything after auth/tenant checks pass — sync/blocking end to
    end (DB lookup, the agent call, sending the Bot Framework reply),
    always run via asyncio.to_thread from the route below, never inline
    on the event loop."""
    connector = ConnectorClient(
        MicrosoftAppCredentials(
            settings.teams_bot_app_id,
            settings.teams_bot_app_password,
            channel_auth_tenant=settings.teams_tenant_id,
        ),
        base_url=activity.service_url,
    )

    aad_object_id = activity.from_property.aad_object_id if activity.from_property else None
    employee = (
        db.scalar(select(Employee).where(Employee.aad_object_id == aad_object_id)) if aad_object_id else None
    )

    if not employee and aad_object_id:
        try:
            graph_user = get_user_with_manager(aad_object_id)
        except GraphLookupError as exc:
            # Deliberately do NOT create a partial employees row here — a
            # half-real record with a Teams-supplied name and nothing else
            # verified would never get revisited. Fail closed instead: no
            # row, no chat this turn, a clean retry-able message. The next
            # message from this same person tries the Graph lookup again
            # from scratch.
            logger.warning("Graph lookup failed for aad_object_id=%s: %s", aad_object_id, exc)
            reply = activity.create_reply(
                "Sorry, I couldn't verify your account with our directory just now — please try again shortly."
            )
            connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
            return
        employee = _auto_provision_employee(db, aad_object_id, graph_user)
    elif not employee:
        # No aad_object_id at all on the activity — genuinely can't identify
        # who's asking (shouldn't happen for a real Teams message; this is
        # a hard stop, not a "not recognized yet" case an auto-provision
        # could fix, since there's no identity to provision *from*).
        logger.warning("Teams activity had no from.aadObjectId at all — cannot identify the sender.")
        reply = activity.create_reply("Sorry, I couldn't identify who's asking — please try again.")
        connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
        return

    if isinstance(activity.value, dict):
        form_type = activity.value.get("form_type")
        if form_type == "certification_form_basics":
            _handle_certification_form_basics(activity, connector)
            return
        if form_type == "certification_submission":
            _handle_certification_form_submission(activity, connector, employee, db)
            return

    user_text = (activity.text or "").strip()
    if not user_text:
        return  # non-text activity (e.g. a reaction, a typing indicator) — nothing to answer

    try:
        chat_out = run_agent_chat(employee.employee_id, user_text, endpoint="/api/teams/messages")
    except HTTPException as exc:
        reply = activity.create_reply(f"Sorry, something went wrong: {exc.detail}")
        connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
        return

    reply = activity.create_reply(chat_out.reply_text or "(I didn't get a text reply for that — please try rephrasing.)")
    # The other half of the fix: chat_out.actions used to be dropped
    # entirely here — the LLM would say "I've opened the form" (per
    # prompts.py's instruction to always call open_certification_form
    # rather than collect fields conversationally) with nothing for the
    # user to actually see or fill in.
    form_action = next(
        (a for a in chat_out.actions if a.get("action") == "open_certification_form" and a.get("data")), None
    )
    if form_action:
        reply.attachments = [_build_certification_form_card(form_action["data"])]
    connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)


@router.post("/messages")
async def messages(payload: dict, request: Request, db: Session = Depends(get_db)):
    if not settings.teams_bot_app_id:
        raise HTTPException(status_code=503, detail="Teams bot is not configured on this server yet.")
    if not settings.teams_tenant_id:
        raise HTTPException(status_code=503, detail="Teams tenant restriction is not configured on this server yet.")

    activity = Activity().deserialize(payload)
    auth_header = request.headers.get("Authorization", "")
    credentials = SimpleCredentialProvider(settings.teams_bot_app_id, settings.teams_bot_app_password)

    try:
        await JwtTokenValidation.authenticate_request(activity, auth_header, credentials)
    except Exception as exc:
        # Deliberately broad: a malformed/forged/expired token can fail
        # inside the SDK's own JWT-decoding library (jwt.exceptions.*), not
        # just via the PermissionError the SDK itself raises for a
        # recognized-but-invalid case — either way, bad credentials are a
        # 401 from a caller's perspective, never a 500 (which would
        # otherwise wrongly imply a server bug for something as mundane as
        # a bad token).
        logger.info("Bot Framework authentication failed: %s", exc)
        raise HTTPException(status_code=401, detail="Bot Framework authentication failed.") from exc

    incoming_tenant_id = None
    if activity.channel_data:
        incoming_tenant_id = (activity.channel_data.get("tenant") or {}).get("id")
    if incoming_tenant_id != settings.teams_tenant_id:
        logger.warning("Rejected a Teams activity from an unexpected tenant: %s", incoming_tenant_id)
        raise HTTPException(status_code=403, detail="This bot is not available for your organization.")

    # Bot Service redelivers an activity it didn't get a fast reply to (see
    # activity_dedup.py's docstring for the two real incidents that proved
    # this isn't hypothetical). activity.id is missing only for synthetic/
    # malformed payloads that wouldn't have a stable identity to dedup
    # against anyway — process those as before rather than dropping them.
    if activity.id and not activity_dedup.should_process(activity.id):
        logger.info("Ignoring a redelivered/duplicate Teams activity (id=%s).", activity.id)
        return {}

    try:
        await asyncio.to_thread(_handle_recognized_or_reply, activity, db)
    except Exception:
        if activity.id:
            activity_dedup.mark_failed(activity.id)
        raise
    if activity.id:
        activity_dedup.mark_done(activity.id)
    return {}


@router.get("/user-display-name/{aad_object_id}")
def get_teams_user_display_name(aad_object_id: str):
    """Portal.html (a static Teams tab, not this bot) only gets an AAD
    object id from Teams' plain getContext() — real displayName requires
    full Teams SSO, which isn't set up. Reuses the exact same app-only
    Graph lookup this bot already uses for a new employee's identity
    (get_user_with_manager, User.Read.All application permission, already
    consented) instead of standing up SSO + an On-Behalf-Of token exchange
    for what is otherwise just a header display string."""
    try:
        user = get_user_with_manager(aad_object_id)
    except GraphLookupError:
        raise HTTPException(status_code=404, detail="Could not resolve this Teams user via Graph.")
    return {"display_name": user.display_name}
