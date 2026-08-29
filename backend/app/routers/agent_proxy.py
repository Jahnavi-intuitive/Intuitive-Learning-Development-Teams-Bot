"""Same-origin proxy in front of `adk api_server` (agent_url, default
:8001), so the browser UI only ever talks to this backend — no CORS
configuration, no second origin for the frontend to know about.

This is a pure relay: it does not add or remove any guardrail the agent
itself doesn't already enforce (access scope is still the backend's own
services/access_scope.py, enforced when the agent calls back into this
same backend as a tool). It only translates the request/response shapes so
the frontend has one simple `POST /api/agent/chat` to call instead of
juggling ADK's session-create-then-run two-step REST flow directly.
"""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services import gcp_id_token

router = APIRouter(prefix="/api/agent", tags=["agent_proxy"])

_AGENT_APP_NAME = "adk_agent"
_client = httpx.Client(base_url=settings.agent_url, timeout=240.0)

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


@router.post("/chat", response_model=ChatOut)
def chat(payload: ChatIn):
    _ensure_session(payload.employee_id)

    try:
        resp = _client.post(
            "/run",
            json={
                "appName": _AGENT_APP_NAME,
                "userId": payload.employee_id,
                "sessionId": payload.employee_id,
                "newMessage": {"role": "user", "parts": [{"text": payload.message}]},
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
