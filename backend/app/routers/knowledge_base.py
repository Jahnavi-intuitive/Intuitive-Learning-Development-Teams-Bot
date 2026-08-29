from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificationCatalog, Employee, KnowledgeBaseItem
from app.schemas import KBItemOut, KBItemUpdateIn, KBMatchOut, KBQueryIn, KBQueryOut
from app.services.admin_auth import require_admin
from app.services.audited_crud import create_with_audit, update_with_audit
from app.services.embeddings import embed_text
from app.services.rag_content import embed_if_changed

router = APIRouter(prefix="/api/kb", tags=["knowledge_base"])


@router.post("/query", response_model=KBQueryOut)
def query_knowledge_base(payload: KBQueryIn, db: Session = Depends(get_db)):
    """RAG lookup over curated exam-prep content — the agent's tool for
    BR-A-02 exam-prep answers. Only searches status='active' content; thin
    or unreviewed content stays out until an L&D admin marks it active."""
    query_embedding = embed_text(payload.question)

    distance = KnowledgeBaseItem.embedding.cosine_distance(query_embedding)
    stmt = (
        select(KnowledgeBaseItem, distance.label("distance"))
        .where(KnowledgeBaseItem.status == "active")
        .order_by(distance)
        .limit(payload.top_k)
    )
    rows = db.execute(stmt).all()

    matches = [
        KBMatchOut(
            id=item.id,
            content=item.content,
            tags=item.tags,
            similarity=1 - dist,
            last_reviewed_date=item.last_reviewed_date,
        )
        for item, dist in rows
    ]
    return KBQueryOut(matches=matches, data_as_of=datetime.now(timezone.utc))


class KBItemIn(BaseModel):
    content: str
    certification_name: str | None = None
    tags: list[str] | None = None
    owner: str
    status: str = "active"
    last_reviewed_date: date | None = None


def _resolve_certification_id(db: Session, certification_name: str | None) -> object | None:
    if not certification_name:
        return None
    entry = db.scalar(
        select(CertificationCatalog).where(CertificationCatalog.name.ilike(certification_name.strip()))
    )
    return entry.id if entry else None


@router.post("/items")
def create_kb_entry(
    payload: KBItemIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """create_kb_entry — admin-authored exam-prep / company-info content.
    Same table the escalation-resolution promotion flow writes to
    (routers/escalations.py); admin-authored rows just carry a different
    owner, not a separate table. Admin-gated and audit-logged like every
    other admin-editable table. content_hash + embedding are computed
    together via services/rag_content.py so this is a create — always a
    hash change from None — the same shared function every other RAG
    write site uses."""
    certification_id = _resolve_certification_id(db, payload.certification_name)
    content_hash, embedding = embed_if_changed(previous_hash=None, text=payload.content)

    item = create_with_audit(
        db,
        KnowledgeBaseItem,
        actor=admin.employee_id,
        certification_id=certification_id,
        content=payload.content,
        embedding=embedding,
        content_hash=content_hash,
        tags=payload.tags,
        status=payload.status,
        owner=payload.owner,
        last_reviewed_date=payload.last_reviewed_date,
    )
    db.commit()
    db.refresh(item)
    return {"id": item.id, "status": item.status}


@router.get("/items", response_model=list[KBItemOut])
def list_kb_entries(
    status: str | None = None,
    certification_name: str | None = None,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """list_kb_entries — plain admin browsing/listing, distinct from
    POST /query's semantic search (which the agent uses)."""
    stmt = select(KnowledgeBaseItem)
    if status:
        stmt = stmt.where(KnowledgeBaseItem.status == status)
    if certification_name:
        cert_id = _resolve_certification_id(db, certification_name)
        stmt = stmt.where(KnowledgeBaseItem.certification_id == cert_id)
    stmt = stmt.order_by(KnowledgeBaseItem.updated_at.desc())
    return db.scalars(stmt).all()


@router.patch("/items/{item_id}", response_model=KBItemOut)
def update_kb_entry(
    item_id: str,
    payload: KBItemUpdateIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """update_kb_entry. Only content changes cost an embedding call — see
    services/rag_content.py embed_if_changed(). A save that only touches
    owner/status/last_reviewed_date leaves the stored embedding and
    content_hash untouched and makes zero embedding-model calls."""
    item = db.get(KnowledgeBaseItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge base item not found")

    changes = payload.model_dump(exclude_unset=True, exclude={"certification_name", "content"})
    if payload.certification_name is not None:
        changes["certification_id"] = _resolve_certification_id(db, payload.certification_name)

    if payload.content is not None:
        new_hash, embedding = embed_if_changed(previous_hash=item.content_hash, text=payload.content)
        changes["content"] = payload.content
        changes["content_hash"] = new_hash
        if embedding is not None:
            changes["embedding"] = embedding

    update_with_audit(db, item, actor=admin.employee_id, changes=changes)
    db.commit()
    db.refresh(item)
    return item


@router.post("/items/{item_id}/deprecate", response_model=KBItemOut)
def deprecate_kb_entry(
    item_id: str,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Soft-delete only — sets status='deprecated' so it drops out of
    query_knowledge_base's active-only search without losing history."""
    item = db.get(KnowledgeBaseItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge base item not found")
    update_with_audit(db, item, actor=admin.employee_id, changes={"status": "deprecated"})
    db.commit()
    db.refresh(item)
    return item
