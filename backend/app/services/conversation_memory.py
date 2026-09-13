"""Two-tier conversation memory: the storage/summarization half.

Tier 1 (`ConversationTurn`, models.py) is an append-only raw log — every
turn is written once and never edited or deleted, same audit posture as
the rest of this system. It is deliberately never replayed in full: every
read here returns at most `settings.conversation_recent_turns` of the most
recent rows, bounding token cost/latency regardless of how long a
relationship with the bot has been going on.

Tier 2 (`ConversationMemory`, one row per employee) holds a running
LLM-folded summary of everything older than that live window.
`record_turn` triggers a re-fold whenever either configured threshold is
crossed (turn count, or a rough token-budget estimate on the turns that
would need folding) — a single LLM call that replaces summary_text, never
a fold-per-message.

This module is called from two places: the Teams webhook
(routers/teams.py, once identity is already resolved) and — the same shape
of call — agent/adk_agent/conversation_memory_service.py's custom
SessionService, via the new /api/internal/conversation/* endpoints
(routers/conversation.py), using the exact same X-Internal-Service-Token
mechanism every other agent tool call already goes through. The agent
process itself never touches Postgres directly — this module is the one
and only place conversation_turns/conversation_memory get written, same
"backend owns the DB" boundary the whole system already keeps.

Deliberately does NOT delete old turns once folded into a summary — that
would break this project's own established "never hard-delete real
history" convention (see services/audited_crud.py's docstring). Instead,
"trim the raw buffer" (per the original brief) is interpreted as "stop
including it in the live context window," not "delete the row" — every
turn a user ever sent stays in conversation_turns permanently; only the
*read* side is bounded.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from google import genai

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ConversationMemory, ConversationTurn, Employee

logger = logging.getLogger(__name__)


class ConversationMemoryConfigError(RuntimeError):
    pass


def _client() -> genai.Client:
    # Deliberately not shared with services/embeddings.py's _client() —
    # that module is embeddings-only and this is a separate, smaller
    # concern (one text-generation call for summarization); duplicating
    # this handful of lines avoids coupling two otherwise-independent
    # services just to save a few lines, matching how config.py's own
    # bare-host normalization is already duplicated in tools.py rather
    # than shared.
    if settings.google_cloud_project:
        return genai.Client(vertexai=True, project=settings.google_cloud_project, location=settings.google_cloud_location)
    if not settings.gemini_api_key:
        raise ConversationMemoryConfigError(
            "Neither GOOGLE_CLOUD_PROJECT (Vertex AI) nor GEMINI_API_KEY (direct API) is set. "
            "Set one before conversation summarization can run."
        )
    return genai.Client(api_key=settings.gemini_api_key)


@dataclass
class TurnOut:
    role: str
    content: str
    created_at: datetime


@dataclass
class ConversationContext:
    summary_text: str | None
    recent_turns: list[TurnOut]


def _get_or_create_memory_row(db: Session, employee: Employee) -> ConversationMemory:
    """Two concurrent first-ever messages for the same brand-new employee
    (a real, plausible race — see the Bot Framework retry note on
    record_turn below) can both see "no row" and both try to insert;
    employee_id is unique, so the loser hits an IntegrityError. Caught via
    a SAVEPOINT (begin_nested), not a full db.rollback() — this function
    is called mid-transaction from record_turn (which may already have a
    pending ConversationTurn insert staged), so only this insert attempt
    may be undone, never the caller's other pending work."""
    row = db.scalar(select(ConversationMemory).where(ConversationMemory.employee_id == employee.id))
    if row:
        return row
    try:
        with db.begin_nested():
            row = ConversationMemory(employee_id=employee.id, turns_since_last_summary=0)
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.scalar(select(ConversationMemory).where(ConversationMemory.employee_id == employee.id))
        if row is None:
            raise  # genuinely unexpected — not the race this was written for
    return row


def get_context(db: Session, employee: Employee) -> ConversationContext:
    """The live context for the next agent call: the current summary plus
    the most recent `settings.conversation_recent_turns` raw turns, oldest
    first (reading order)."""
    memory = _get_or_create_memory_row(db, employee)
    db.commit()  # persists a freshly-created blank memory row, if this was the first call for this employee
    rows = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.employee_id == employee.id)
        .order_by(ConversationTurn.created_at.desc())
        .limit(settings.conversation_recent_turns)
    ).all()
    recent = list(reversed(rows))  # oldest first, matching how a transcript reads
    return ConversationContext(
        summary_text=memory.summary_text,
        recent_turns=[TurnOut(role=r.role, content=r.content, created_at=r.created_at) for r in recent],
    )


def _estimate_tokens(text: str) -> int:
    # Rough, deliberately conservative heuristic (chars/4) — not a real
    # tokenizer call. Good enough for "should we fold now," not used for
    # anything billing-accurate.
    return len(text) // 4


def record_turn(db: Session, employee: Employee, conversation_id: str, role: str, content: str) -> None:
    """Appends one raw turn, then folds older turns into the running
    summary if either configured threshold has been crossed. Commits the
    caller's transaction itself (this is the one write path for both
    tables, called standalone from routers/conversation.py — see that
    router for why it isn't left to the caller).

    A genuine duplicate call for the same logical turn (e.g. a Teams
    activity redelivered by Bot Service before routers/teams.py's own
    activity-id dedup existed to stop it) would double-write here — that
    dedup is the actual fix for that; this function has no turn-level
    idempotency of its own and isn't meant to."""
    db.add(
        ConversationTurn(
            employee_id=employee.id,
            conversation_id=conversation_id,
            role=role,
            content=content,
        )
    )
    memory = _get_or_create_memory_row(db, employee)
    memory.turns_since_last_summary += 1
    db.flush()

    if memory.turns_since_last_summary >= settings.conversation_summarize_every_n_turns:
        _summarize(db, employee, memory)
        db.commit()
        return

    # Token-budget check: only the turns that would actually get folded
    # (everything past the live recent-turns window) count toward this —
    # the live window itself is never summarized away.
    foldable = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.employee_id == employee.id)
        .order_by(ConversationTurn.created_at.desc())
        .offset(settings.conversation_recent_turns)
        .limit(500)  # a sane cap — this is an estimate, not an exhaustive scan
    ).all()
    if foldable and _estimate_tokens("".join(t.content for t in foldable)) >= settings.conversation_summarize_token_budget:
        _summarize(db, employee, memory)

    db.commit()


def _summarize(db: Session, employee: Employee, memory: ConversationMemory) -> None:
    """One LLM call folding everything past the live recent-turns window
    into an updated summary_text — unless the summary has aged out (see
    below), in which case it's discarded instead, no LLM call needed.
    Never raises past this function on an LLM failure — a summarization
    hiccup should never break the live conversation turn that triggered
    it — but does log one now (previously silent) and backs off via
    last_summarize_attempt_at instead of retrying on every subsequent
    turn."""
    now = datetime.now(timezone.utc)
    if memory.last_summarize_attempt_at is not None:
        cooldown_elapsed = (now - memory.last_summarize_attempt_at).total_seconds()
        if cooldown_elapsed < settings.conversation_summarize_retry_cooldown_seconds:
            return  # still cooling down after a recent attempt (success or failure) — try again later

    all_turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.employee_id == employee.id)
        .order_by(ConversationTurn.created_at.asc())
    ).all()
    cutoff = max(0, len(all_turns) - settings.conversation_recent_turns)
    to_fold = all_turns[:cutoff]
    if not to_fold:
        memory.turns_since_last_summary = 0
        return

    # The summary isn't kept forever — once the turns folded into its
    # current lineage cross conversation_summary_max_age_turns, discard it
    # outright rather than growing it without bound. conversation_turns
    # (the raw log) is untouched either way; this only bounds what feeds
    # the live long-term-memory tier. No LLM call needed for a reset.
    if memory.turns_folded_since_reset + len(to_fold) > settings.conversation_summary_max_age_turns:
        memory.summary_text = None
        memory.last_summarized_at = None
        memory.turns_folded_since_reset = 0
        memory.turns_since_last_summary = 0
        memory.last_summarize_attempt_at = now
        return

    transcript = "\n".join(f"{t.role}: {t.content}" for t in to_fold)
    prompt = (
        "You maintain a running memory of an ongoing conversation between an "
        "employee and an L&D certification assistant. Fold the NEW transcript "
        "below into the EXISTING summary, producing one updated summary "
        "paragraph (a few sentences) that preserves anything a future reply "
        "would need to stay contextually consistent — what the employee has "
        "asked about, decided, or been told, not a turn-by-turn recap.\n\n"
        f"EXISTING SUMMARY:\n{memory.summary_text or '(none yet)'}\n\n"
        f"NEW TRANSCRIPT TO FOLD IN:\n{transcript}\n\n"
        "Updated summary:"
    )
    memory.last_summarize_attempt_at = now
    try:
        # Same model the agent itself runs on (agent/adk_agent/agent.py) —
        # a plain one-off text-generation call, not a second embedding.
        result = _client().models.generate_content(model="gemini-flash-latest", contents=prompt)
        summary = (result.text or "").strip()
    except Exception:
        logger.warning(
            "Conversation summarization failed for employee_id=%s — will retry after %ds cooldown.",
            employee.employee_id,
            settings.conversation_summarize_retry_cooldown_seconds,
            exc_info=True,
        )
        return  # leave summary_text/turns_since_last_summary as-is; retried after the cooldown elapses

    if summary:
        memory.summary_text = summary
        memory.last_summarized_at = now
        memory.turns_since_last_summary = 0
        memory.turns_folded_since_reset += len(to_fold)
