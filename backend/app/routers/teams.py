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

from botbuilder.schema import Activity
from botframework.connector import ConnectorClient
from botframework.connector.auth import (
    JwtTokenValidation,
    MicrosoftAppCredentials,
    SimpleCredentialProvider,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import PRACTICE_VALUES, Employee
from app.routers.agent_proxy import run_agent_chat
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

    user_text = (activity.text or "").strip()
    if not user_text:
        return  # non-text activity (e.g. a reaction, a typing indicator) — nothing to answer

    try:
        chat_out = run_agent_chat(employee.employee_id, user_text)
    except HTTPException as exc:
        reply = activity.create_reply(f"Sorry, something went wrong: {exc.detail}")
        connector.conversations.reply_to_activity(activity.conversation.id, activity.id, reply)
        return

    reply = activity.create_reply(chat_out.reply_text or "(I didn't get a text reply for that — please try rephrasing.)")
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
