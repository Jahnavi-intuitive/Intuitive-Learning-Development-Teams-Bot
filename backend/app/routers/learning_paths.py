"""Learning-path *membership* admin (which certifications belong to which
practice's path, for catalog browsing). The per-employee "next
certification" logic that used to live here (/next, /overview — a
frontier walk over one practice's prerequisite DAG) was removed on
2026-10-01 and replaced by routers/recommendations.py +
services/recommender/, which also accounts for experience level,
cross-partner evidence, partnership gaps, drives and peers."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import CertificationCatalog, Employee, LearningPath, LearningPathItem
from app.schemas import BulkAddToPathsIn, LearningPathOut, PathMembershipIn
from app.services.admin_auth import require_admin
from app.services.audited_crud import create_with_audit

router = APIRouter(prefix="/api/learning-paths", tags=["learning_paths"])


def _get_path(db: Session, practice: str) -> LearningPath | None:
    return db.scalar(
        select(LearningPath)
        .where(LearningPath.practice == practice)
        .options(selectinload(LearningPath.items))
    )


@router.post("/{path_id}/certifications", status_code=201)
def add_certification_to_path(
    path_id: str,
    payload: PathMembershipIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """add_certification_to_path: attach one certification to one
    already-existing path's membership. Idempotent — adding a
    certification that's already a member is a no-op, not an error."""
    path = db.get(LearningPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Learning path not found")
    cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == payload.certification_name))
    if not cert:
        raise HTTPException(status_code=422, detail=f"Unknown certification '{payload.certification_name}'.")

    existing = db.scalar(
        select(LearningPathItem).where(
            LearningPathItem.path_id == path.id, LearningPathItem.certification_id == cert.id
        )
    )
    if existing:
        return {"path_id": str(path.id), "certification": cert.name, "already_member": True}

    create_with_audit(
        db, LearningPathItem, actor=admin.employee_id,
        path_id=path.id, certification_id=cert.id, is_required=payload.is_required,
    )
    db.commit()
    return {"path_id": str(path.id), "certification": cert.name, "already_member": False}


@router.post("/certifications/{certification_name}/add-to-paths")
def add_certification_to_paths(
    certification_name: str,
    payload: BulkAddToPathsIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """add_certification_to_paths bulk variant: attach one certification to
    every path named in path_ids, or to ALL existing learning paths when
    all=True. Paths that already contain the certification are skipped,
    not duplicated."""
    cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == certification_name))
    if not cert:
        raise HTTPException(status_code=422, detail=f"Unknown certification '{certification_name}'.")

    if payload.all:
        paths = db.scalars(select(LearningPath)).all()
    elif payload.path_ids:
        paths = db.scalars(select(LearningPath).where(LearningPath.id.in_(payload.path_ids))).all()
    else:
        raise HTTPException(status_code=422, detail="Provide path_ids or set all=true.")

    already_member_path_ids = set(
        db.scalars(
            select(LearningPathItem.path_id).where(LearningPathItem.certification_id == cert.id)
        ).all()
    )

    added, skipped = [], []
    for path in paths:
        if path.id in already_member_path_ids:
            skipped.append(str(path.id))
            continue
        create_with_audit(
            db, LearningPathItem, actor=admin.employee_id,
            path_id=path.id, certification_id=cert.id, is_required=True,
        )
        added.append(str(path.id))

    db.commit()
    return {"certification": cert.name, "added_to_paths": added, "already_member_paths": skipped}


# Registered last, deliberately: FastAPI/Starlette match routes in
# registration order, and this catch-all single-segment path param would
# otherwise swallow any fixed single-segment route added above it.
@router.get("/{practice}", response_model=LearningPathOut)
def get_learning_path(practice: str, db: Session = Depends(get_db)):
    path = _get_path(db, practice)
    if not path:
        raise HTTPException(status_code=404, detail=f"No learning path configured for practice '{practice}' yet")
    return path
