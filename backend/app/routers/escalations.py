from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Employee, Escalation, KnowledgeBaseItem
from app.schemas import EscalationCreateIn, EscalationOut, EscalationResolveIn
from app.services.audited_crud import create_with_audit
from app.services.embeddings import embed_text
from app.services.rag_content import embed_if_changed

router = APIRouter(prefix="/api/escalations", tags=["escalations"])

# Section 8 of the BRD leaves the SME escalation SLA "to be agreed" with
# L&D/PMO. Using a 48h placeholder so the field is populated and testable;
# replace with the signed-off SLA policy once decided.
_DEFAULT_SLA = timedelta(hours=48)
_DEFAULT_ROUTED_OWNER = "L&D Admin Team"


@router.post("", response_model=EscalationOut)
def create_escalation(payload: EscalationCreateIn, db: Session = Depends(get_db)):
    employee = db.scalar(select(Employee).where(Employee.employee_id == payload.employee_id))
    if not employee:
        raise HTTPException(status_code=404, detail="Unknown employee_id")

    escalation = Escalation(
        employee_id=employee.id,
        question=payload.question,
        question_embedding=embed_text(payload.question),
        context=payload.context,
        routed_owner=payload.routed_owner or _DEFAULT_ROUTED_OWNER,
        status="open",
        sla_due_date=datetime.now(timezone.utc) + _DEFAULT_SLA,
    )
    db.add(escalation)
    db.commit()
    db.refresh(escalation)
    return escalation


@router.get("", response_model=list[EscalationOut])
def list_escalations(status: str | None = None, employee_id: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Escalation)
    if status:
        stmt = stmt.where(Escalation.status == status)
    if employee_id:
        employee = db.scalar(select(Employee).where(Employee.employee_id == employee_id))
        if not employee:
            raise HTTPException(status_code=404, detail="Unknown employee_id")
        stmt = stmt.where(Escalation.employee_id == employee.id)
    stmt = stmt.order_by(Escalation.created_at.desc())
    return db.scalars(stmt).all()


@router.patch("/{escalation_id}/resolve", response_model=EscalationOut)
def resolve_escalation(escalation_id: str, payload: EscalationResolveIn, db: Session = Depends(get_db)):
    escalation = db.get(Escalation, escalation_id)
    if not escalation:
        raise HTTPException(status_code=404, detail="Escalation not found")

    escalation.resolution_answer = payload.resolution_answer
    escalation.status = "resolved"
    escalation.resolved_at = datetime.now(timezone.utc)

    if payload.promote_to_kb:
        # Routed through the same shared embed_if_changed() every other
        # RAG-content write site uses (services/rag_content.py) — always
        # embeds here since this is a brand-new row (previous_hash=None),
        # but this keeps content_hash populated so a later admin edit to
        # this same row can skip re-embedding when only owner/status change.
        content_hash, embedding = embed_if_changed(previous_hash=None, text=payload.resolution_answer)
        kb_item = create_with_audit(
            db,
            KnowledgeBaseItem,
            actor="system",
            content=payload.resolution_answer,
            embedding=embedding,
            content_hash=content_hash,
            tags=payload.kb_tags,
            status="pending_review",  # an L&D admin reviews before it's trusted as active content
            owner=escalation.routed_owner,
        )
        db.flush()
        escalation.promoted_kb_id = kb_item.id

    db.commit()
    db.refresh(escalation)
    return escalation
