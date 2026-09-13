"""Custom two-tier conversation-memory SessionService for the Teams bot.

Delegates all in-process Session/Event object mechanics (state deltas,
event trimming, copy semantics) to ADK's own InMemorySessionService — that
logic is already correct and well-tested; reimplementing it would be
needless risk. This class adds exactly one thing on top: durability
across a process restart / a reopened Teams chat, backed by the backend's
conversation_turns/conversation_memory tables (see
backend/app/services/conversation_memory.py and
backend/app/routers/conversation.py) — "the point is that it survives a
restart and a reopened Teams chat, not just a live process" (per the
brief this was built from).

This process never touches Postgres directly — every read/write here goes
through the backend's /api/internal/conversation/* endpoints, using the
exact same X-Internal-Service-Token + X-Employee-Id mechanism every other
tool in tools.py already uses to call the backend. Same "backend owns the
DB" boundary the whole system already keeps (see DEVELOPMENT_LOG.md's
2026-09-02 entry for why that boundary was kept rather than giving this
now-public-on-Render's-free-tier agent process its own DB credentials).

Registered via agent/services.py (ADK's plugin/service-registry mechanism
— note that file lives directly under agent/, not inside this adk_agent/
package, since that's where ADK's load_services_module actually looks;
see that file's own docstring for the full wiring). Wired in by default
via agent/Dockerfile's --session_service_uri=teamsmemory://local.

On a fresh get_session/create_session (nothing warm in the in-memory
delegate yet — true on every process cold-start, and true the first time
a given employee messages after this process started), the session is
rehydrated: the backend returns the current summary_text plus the last N
raw turns for this employee, which become synthetic Events seeded into a
fresh in-memory session before anything else runs. On append_event, any
real user/agent text turn is persisted to the backend (best-effort — a
transient persistence hiccup is logged and swallowed, never allowed to
break the live conversation turn that triggered it) in addition to the
normal in-memory bookkeeping.

Concurrency note (2026-09-13): routers/teams.py dispatches each Teams
message via asyncio.to_thread, so two messages from the same employee
arriving close together (e.g. a Bot Service retry landing while the
original is still in flight) can genuinely race here — one thread's
get_session/create_session check-then-act ("nothing warm yet, rehydrate
and seed") can interleave with another's. Guarded by one asyncio.Lock per
employee_id (_locks/_lock_for below): every public entry point acquires
it once at the top and calls the lock-free _rehydrate_or_get_locked
helper — never the other way around, since asyncio.Lock isn't reentrant
and nesting two acquisitions of the same lock would deadlock.
"""

import asyncio
import logging
import uuid as uuid_module
from typing import Any, Optional

import httpx
from google.genai import types as genai_types

from google.adk.events.event import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session

from . import gcp_id_token
from .tools import _BACKEND_URL

logger = logging.getLogger(__name__)

# Matches agent.py's root_agent.name — used as the "author" on synthetic
# replayed agent-authored events so they read the same as a live one.
_AGENT_NAME = "lnd_certification_agent"


class TeamsConversationMemoryService(BaseSessionService):
    def __init__(self) -> None:
        self._delegate = InMemorySessionService()
        self._client = httpx.Client(base_url=_BACKEND_URL, timeout=30.0)
        # One lock per employee_id, created lazily. Never evicted — same
        # accepted "grows for the life of the process" trade-off as the
        # InMemorySessionService delegate itself; revisit only if the
        # employee count grows enough to matter.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def _headers(self, employee_id: str) -> dict:
        service_header = gcp_id_token.fetch_service_token_header(_BACKEND_URL)
        return {"X-Employee-Id": employee_id, **service_header}

    def _fetch_context_sync(self, employee_id: str) -> dict:
        try:
            resp = self._client.get("/api/internal/conversation/context", headers=self._headers(employee_id))
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            logger.warning(
                "Could not fetch conversation context for %s — starting with no prior history.",
                employee_id,
                exc_info=True,
            )
            return {"summary_text": None, "recent_turns": []}

    def _record_turn_sync(self, employee_id: str, conversation_id: str, role: str, content: str) -> None:
        try:
            resp = self._client.post(
                "/api/internal/conversation/turns",
                json={"conversation_id": conversation_id, "role": role, "content": content},
                headers=self._headers(employee_id),
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "Could not persist a conversation turn for %s — continuing without it.",
                employee_id,
                exc_info=True,
            )

    @staticmethod
    def _build_context_events(context: dict) -> list[Event]:
        events: list[Event] = []
        summary = context.get("summary_text")
        if summary:
            events.append(
                Event(
                    id=str(uuid_module.uuid4()),
                    invocation_id="restored-summary",
                    author="user",
                    content=genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text=f"[Earlier conversation summary: {summary}]")],
                    ),
                )
            )
        for turn in context.get("recent_turns", []):
            role = turn.get("role")
            events.append(
                Event(
                    id=str(uuid_module.uuid4()),
                    invocation_id="restored-turn",
                    author="user" if role == "user" else _AGENT_NAME,
                    content=genai_types.Content(
                        role="user" if role == "user" else "model",
                        parts=[genai_types.Part(text=turn.get("content", ""))],
                    ),
                )
            )
        return events

    async def _rehydrate_or_get_locked(
        self, *, app_name: str, user_id: str, session_id: str, state: Optional[dict[str, Any]] = None
    ) -> Session:
        """Callers must already hold self._lock_for(user_id) — this method
        never acquires it itself, so it can be safely called from within
        create_session/get_session's own locked section without deadlock."""
        existing = await self._delegate.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
        if existing is not None:
            return existing

        context = await asyncio.to_thread(self._fetch_context_sync, user_id)
        session = await self._delegate.create_session(
            app_name=app_name, user_id=user_id, session_id=session_id, state=state
        )
        for event in self._build_context_events(context):
            # Seed directly via the delegate — NOT self.append_event —
            # so replayed history is never re-persisted to the backend
            # as though it were a brand-new turn.
            await self._delegate.append_event(session=session, event=event)
        return session

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        session_id = session_id or user_id
        async with self._lock_for(user_id):
            existing = await self._delegate.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
            if existing is not None:
                # A genuine second create for the same id — same contract as
                # InMemorySessionService itself (raises AlreadyExistsError).
                # backend/app/routers/agent_proxy.py's _ensure_session already
                # tolerates this; it's the same shape of error the unmodified
                # ADK default already produced here before this class existed.
                return await self._delegate.create_session(
                    app_name=app_name, user_id=user_id, session_id=session_id, state=state
                )
            return await self._rehydrate_or_get_locked(
                app_name=app_name, user_id=user_id, session_id=session_id, state=state
            )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        async with self._lock_for(user_id):
            await self._rehydrate_or_get_locked(app_name=app_name, user_id=user_id, session_id=session_id)
        return await self._delegate.get_session(app_name=app_name, user_id=user_id, session_id=session_id, config=config)

    async def list_sessions(self, *, app_name: str, user_id: Optional[str] = None) -> ListSessionsResponse:
        return await self._delegate.list_sessions(app_name=app_name, user_id=user_id)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        await self._delegate.delete_session(app_name=app_name, user_id=user_id, session_id=session_id)

    async def append_event(self, session: Session, event: Event) -> Event:
        async with self._lock_for(session.user_id):
            result = await self._delegate.append_event(session=session, event=event)

            if event.partial:
                return result

            text = self._extract_text(event)
            if text:
                role = "user" if event.author == "user" else "agent"
                # This system's calling convention (agent_proxy.py) always
                # uses employee_id as both userId and sessionId, one
                # persistent session per employee. session.id (== the
                # employee_id) is passed through as conversation_id here
                # too — it is NOT the real Teams activity.conversation.id;
                # nothing in this call chain threads that through. Fine
                # under today's one-active-thread-per-employee schema (see
                # models.py ConversationTurn's own docstring), but worth
                # knowing if that assumption is ever revisited.
                await asyncio.to_thread(self._record_turn_sync, session.user_id, session.id, role, text)

            return result

    @staticmethod
    def _extract_text(event: Event) -> str:
        if not event.content or not event.content.parts:
            return ""
        # Skip function-call/function-response bookkeeping parts entirely
        # — only genuine user/agent text turns are worth remembering as
        # conversation history (mirrors how agent_proxy.py's own reply
        # parsing already filters this same event stream the same way).
        texts = [
            part.text
            for part in event.content.parts
            if getattr(part, "text", None) and not getattr(part, "function_call", None)
        ]
        return "\n".join(texts).strip()
