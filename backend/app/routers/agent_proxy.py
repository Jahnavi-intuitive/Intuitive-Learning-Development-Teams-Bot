"""Same-origin proxy in front of `adk api_server` (agent_url, default
:8001), so the browser UI only ever talks to this backend — no CORS
configuration, no second origin for the frontend to know about.

This is a pure relay: it does not add or remove any guardrail the agent
itself doesn't already enforce (access scope is still the backend's own
services/access_scope.py, enforced when the agent calls back into this
same backend as a tool). It only translates the request/response shapes so
the frontend has one simple `POST /api/agent/chat` to call instead of
juggling ADK's session-create-then-run two-step REST flow directly.

`run_agent_chat` is the shared core, called from two places: this file's
own HTTP endpoint (local test UI / admin dashboard, employee_id taken from
the request body — self-only enforced below) and routers/teams.py's Bot
Framework webhook (employee_id is never taken from the request there at
all — it's resolved server-side from the activity's aad_object_id, exactly
the identity-spoofing gap this shared function closes for both callers at
once). Found and fixed during the 2026-09-02 Teams-integration work: this
endpoint had no `Depends(get_current_employee)` at all until now — the
same shape of gap the 2026-08-27 security pass fixed on five other
endpoints (see DEVELOPMENT_LOG.md), just missed here since this route was
added afterward.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.models import Employee
from app.services import gcp_id_token
from app.services.access_scope import get_current_employee
from app.services.rate_limit import check_rate_limit

router = APIRouter(prefix="/api/agent", tags=["agent_proxy"])

_AGENT_APP_NAME = "adk_agent"
# 240s wasn't always enough on this dev machine's own documented slow
# network path (see DEVELOPMENT_LOG.md's curl_transport.py history) —
# confirmed directly: a real request took ~4.5 minutes just to get from
# "session created" to "sending the request to Gemini" (before Gemini's
# own response time even starts counting), and got cut off by this
# timeout mid-flight. Raised, not eliminated — this is inherent to this
# one machine's environment, not something a longer timeout "fixes" so
# much as tolerates.
_client = httpx.Client(base_url=settings.agent_url, timeout=480.0)

# Canonical copy is agent/adk_agent/prompts.py's STARTER_PROMPTS (BR-A-14:
# "the agent offers suggested starter prompts on first use"). Duplicated
# here, not imported, because the backend and the agent are separate
# processes/venvs with no shared package — same reason curl_transport.py
# exists as two copies rather than one shared module. Keep these two lists
# in sync by hand if either changes.
STARTER_PROMPTS = [
    "What certifications should I pursue for my role?",
    "Is my certification submission verified yet?",
    "I have a question about an exam topic — can you help me prepare?",
    "Which of my reportees have a certification expiring in the next 30 days?",
]


@router.get("/starter-prompts")
def starter_prompts():
    return {"prompts": STARTER_PROMPTS}


def _ensure_session(employee_id: str) -> None:
    try:
        _client.post(
            f"/apps/{_AGENT_APP_NAME}/users/{employee_id}/sessions/{employee_id}",
            json={"employee_id": employee_id},
            headers=gcp_id_token.fetch_auth_header(settings.agent_url),
        )
    except httpx.HTTPError:
        pass  # already exists, or agent server briefly unreachable — surfaced on the chat call instead


class ChatIn(BaseModel):
    employee_id: str
    message: str


class ChatOut(BaseModel):
    reply_text: str
    actions: list[dict]  # UI-triggering tool calls the agent made, e.g. [{"name": "open_certification_form", "data": {...}}]
    raw_events: list[dict]


def run_agent_chat(employee_id: str, message: str) -> ChatOut:
    """The actual agent call, shared by every caller — always called with
    an already-resolved, already-trusted employee_id, never with one taken
    from a request body/query param at this layer. Rate-limited per
    employee_id regardless of which surface (local UI, Teams) triggered
    the call."""
    check_rate_limit(employee_id)
    _ensure_session(employee_id)

    try:
        resp = _client.post(
            "/run",
            json={
                "appName": _AGENT_APP_NAME,
                "userId": employee_id,
                "sessionId": employee_id,
                "newMessage": {"role": "user", "parts": [{"text": message}]},
            },
            headers=gcp_id_token.fetch_auth_header(settings.agent_url),
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't reach the agent: {exc}") from exc

    events = resp.json()
    reply_parts: list[str] = []
    actions: list[dict] = []

    for event in events:
        content = event.get("content") or {}
        if content.get("role") != "model":
            continue
        for part in content.get("parts", []):
            if part.get("text"):
                reply_parts.append(part["text"])

    # UI-triggering tool calls are paired function_call/function_response
    # events with a "user" role (ADK logs tool results as role="user" back
    # to the model) — pull out the ones with an explicit "action" field the
    # frontend cares about (see agent/adk_agent/tools.py open_certification_form).
    for event in events:
        content = event.get("content") or {}
        for part in content.get("parts", []):
            fr = part.get("functionResponse")
            response = fr.get("response") if fr else None
            if isinstance(response, dict) and response.get("action"):
                actions.append(
                    {"name": fr.get("name"), "action": response["action"], "data": response.get("data", {})}
                )

    return ChatOut(
        reply_text="\n".join(reply_parts) if reply_parts else "",
        actions=actions,
        raw_events=events,
    )


@router.post("/chat", response_model=ChatOut)
def chat(payload: ChatIn, current: Employee = Depends(get_current_employee)):
    if payload.employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only chat as yourself.")
    return run_agent_chat(payload.employee_id, payload.message)
