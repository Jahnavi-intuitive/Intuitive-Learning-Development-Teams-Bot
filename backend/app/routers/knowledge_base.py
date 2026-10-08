import math
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


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / norm if norm else 1.0


@router.post("/query", response_model=KBQueryOut)
def query_knowledge_base(payload: KBQueryIn, db: Session = Depends(get_db)):
    """RAG lookup over curated exam-prep content — the agent's tool for
    BR-A-02 exam-prep answers. Only searches status='active' content; thin
    or unreviewed content stays out until an L&D admin marks it active."""
    query_embedding = embed_text(payload.question)

    if db.get_bind().dialect.name == "postgresql":
        distance = KnowledgeBaseItem.embedding.cosine_distance(query_embedding)
        stmt = (
            select(KnowledgeBaseItem, distance.label("distance"))
            .where(KnowledgeBaseItem.status == "active")
            .order_by(distance)
            .limit(payload.top_k)
        )
        rows = db.execute(stmt).all()
    else:
        # No vector search on Databricks: score the (small) set of active
        # items in Python with the same cosine distance pgvector uses.
        items = db.execute(
            select(KnowledgeBaseItem).where(
                KnowledgeBaseItem.status == "active", KnowledgeBaseItem.embedding.is_not(None)
            )
        ).scalars().all()
        rows = sorted(((i, _cosine_distance(i.embedding, query_embedding)) for i in items), key=lambda r: r[1])
        rows = rows[: payload.top_k]

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


def _resolve_certification_id(db: Session, certification_name: str | None) -> object | None:
    if not certification_name:
        return None
    entry = db.scalar(
        select(CertificationCatalog).where(CertificationCatalog.name.ilike(certification_name.strip()))
    )
    return entry.id if entry else None


def _kb_item_to_out(item: KnowledgeBaseItem, certification_name: str | None) -> dict:
    return {
        "id": item.id,
        "certification_id": item.certification_id,
        "certification_name": certification_name,
        "content": item.content,
        "tags": item.tags,
        "status": item.status,
        "owner": item.owner,
        "last_reviewed_date": item.last_reviewed_date,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _certification_name_for(db: Session, certification_id: object | None) -> str | None:
    if not certification_id:
        return None
    cert = db.get(CertificationCatalog, certification_id)
    return cert.name if cert else None


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
        last_reviewed_date=date.today(),
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
    rows = db.scalars(stmt).all()

    cert_ids = {r.certification_id for r in rows if r.certification_id}
    cert_names: dict[object, str] = {}
    if cert_ids:
        for cert in db.scalars(select(CertificationCatalog).where(CertificationCatalog.id.in_(cert_ids))):
            cert_names[cert.id] = cert.name

    return [_kb_item_to_out(r, cert_names.get(r.certification_id)) for r in rows]


@router.patch("/items/{item_id}", response_model=KBItemOut)
def update_kb_entry(
    item_id: str,
    payload: KBItemUpdateIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """update_kb_entry. content is the only thing an existing entry's edit
    can change (certification/tags/owner/status are fixed at creation —
    see KBItemUpdateIn). Re-embeds only if the content hash actually
    changed (services/rag_content.py embed_if_changed()), but
    last_reviewed_date always advances to today, since submitting this
    edit is itself the review event."""
    item = db.get(KnowledgeBaseItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge base item not found")

    new_hash, embedding = embed_if_changed(previous_hash=item.content_hash, text=payload.content)
    changes: dict = {
        "content": payload.content,
        "content_hash": new_hash,
        "last_reviewed_date": date.today(),
    }
    if embedding is not None:
        changes["embedding"] = embedding

    update_with_audit(db, item, actor=admin.employee_id, changes=changes)
    db.commit()
    db.refresh(item)
    return _kb_item_to_out(item, _certification_name_for(db, item.certification_id))


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
    return _kb_item_to_out(item, _certification_name_for(db, item.certification_id))
