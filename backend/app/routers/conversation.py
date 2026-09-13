"""Conversation-memory endpoints — called by
agent/adk_agent/conversation_memory_service.py (the agent process's custom
ADK SessionService), never by a browser or the Teams webhook directly.

Protected exactly like every other agent-tool call into this backend:
`Depends(get_current_employee)` resolves identity from the
X-Internal-Service-Token + X-Employee-Id pair in production ("entra" mode)
or the bare X-Employee-Id header in local dev ("header" mode) — see
services/access_scope.py. There is deliberately no employee_id path/query
parameter anywhere here: the caller can only ever read or write its own
conversation memory, the identity the backend itself already resolved,
never one asserted in the request.

This is the one and only place agent/adk_agent/ reaches into
conversation_turns/conversation_memory — the agent process holds no
database credentials of its own (see DEVELOPMENT_LOG.md's 2026-09-02 entry
for why that boundary is deliberate)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Employee
from app.services import conversation_memory
from app.services.access_scope import get_current_employee

router = APIRouter(prefix="/api/internal/conversation", tags=["conversation_memory"])


class TurnOut(BaseModel):
    role: str
    content: str


class ContextOut(BaseModel):
    summary_text: str | None
    recent_turns: list[TurnOut]


class RecordTurnIn(BaseModel):
    conversation_id: str
    role: str  # "user" | "agent" — matches models.CONVERSATION_ROLE_VALUES
    content: str


@router.get("/context", response_model=ContextOut)
def get_context(
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    ctx = conversation_memory.get_context(db, employee)
    return ContextOut(
        summary_text=ctx.summary_text,
        recent_turns=[TurnOut(role=t.role, content=t.content) for t in ctx.recent_turns],
    )


@router.post("/turns", status_code=204)
def record_turn(
    payload: RecordTurnIn,
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    conversation_memory.record_turn(
        db, employee, conversation_id=payload.conversation_id, role=payload.role, content=payload.content
    )
