from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import CertificationCatalog, CertificationPrerequisite, Employee, LearningPath, LearningPathItem
from app.routers import (
    agent_proxy,
    catalog,
    certifications,
    drives,
    employees,
    escalations,
    internal,
    knowledge_base,
    learning_paths,
    notifications,
    tracker,
    verification,
)
from app.schemas import (
    CatalogEntryOut,
    CatalogUpsertIn,
    EmployeeCreateIn,
    EmployeeOut,
    LearningPathOut,
    LearningPathSeedIn,
    PrerequisiteEdgeDeleteIn,
    PrerequisiteEdgeIn,
)
from app.services.admin_auth import require_admin
from app.services.audited_crud import create_with_audit, delete_with_audit, update_with_audit
from app.services.excel_sync import sync_tracker_workbook
from app.services.scheduler import start_scheduler, stop_scheduler

# Fail fast, not silently: auth_mode="entra" with missing tenant/audience
# would otherwise leave services/entra_auth.py raising a 500 on every
# request instead of the server simply refusing to come up misconfigured.
if settings.auth_mode == "entra" and not (settings.entra_tenant_id and settings.entra_audience):
    raise RuntimeError(
        "auth_mode is 'entra' but ENTRA_TENANT_ID/ENTRA_AUDIENCE are not both set - "
        "refusing to start rather than run with real-auth mode half-configured."
    )

app = FastAPI(title="L&D & Certification Agent — Backend", version="0.1.0")

app.include_router(employees.router)
app.include_router(catalog.router)
app.include_router(learning_paths.router)
app.include_router(certifications.router)
app.include_router(tracker.router)
app.include_router(verification.router)
app.include_router(knowledge_base.router)
app.include_router(escalations.router)
app.include_router(agent_proxy.router)
app.include_router(drives.router)
app.include_router(notifications.router)
app.include_router(internal.router)


@app.on_event("startup")
def _startup():
    start_scheduler()


@app.on_event("shutdown")
def _shutdown():
    stop_scheduler()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/admin/sync-tracker")
def sync_tracker(db: Session = Depends(get_db)):
    """Manually trigger an Excel tracker sync (see services/excel_sync.py).
    Not agent-facing — an L&D admin or the dev workflow calls this."""
    report = sync_tracker_workbook(db, settings.tracker_xlsx_path)
    return report.__dict__


@app.post("/api/admin/test-employees", response_model=EmployeeOut)
def create_test_employee(payload: EmployeeCreateIn, db: Session = Depends(get_db)):
    """Dev-only seeding endpoint (scripts/seed_test_employees.py). Not part
    of the BRD's HRIS access surface — a stand-in until Entra/Graph sync
    exists (see services/access_scope.py seam note)."""
    employee = Employee(**payload.model_dump())
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee


@app.post("/api/admin/catalog-entries", response_model=CatalogEntryOut)
def upsert_catalog_entry(
    payload: CatalogUpsertIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """create_certification + update_certification in one call, upserting
    by name (certification_catalog.name is unique, so upsert-by-name is a
    sound API for this resource rather than two separate endpoints).
    Distinct from the auto-created pending_setup rows the submission form
    / Excel sync create when they encounter a cert name not yet in the
    catalog.

    Admin-gated and audit-logged via the shared audited_crud helpers
    (services/audited_crud.py) — same plumbing every other admin-editable
    table now uses, not hand-written per table. Used by both the admin
    dashboard and scripts/seed_catalog_and_paths.py (which now must pass a
    real admin employee's X-Employee-Id, same as any other caller)."""
    entry = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == payload.name))
    fields = payload.model_dump(exclude={"name"})

    if entry:
        update_with_audit(db, entry, actor=admin.employee_id, changes=fields)
    else:
        entry = create_with_audit(db, CertificationCatalog, actor=admin.employee_id, name=payload.name, **fields)

    db.commit()
    db.refresh(entry)
    return entry


@app.post("/api/admin/certifications/{catalog_id}/deprecate", response_model=CatalogEntryOut)
def deprecate_certification(
    catalog_id: str,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Soft-delete only — sets status='deprecated'. Never hard-delete a
    certification: certificate_records rows reference it by
    certification_id (ON DELETE SET NULL would silently orphan history)."""
    entry = db.get(CertificationCatalog, catalog_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Certification not found")
    update_with_audit(db, entry, actor=admin.employee_id, changes={"status": "deprecated"})
    db.commit()
    db.refresh(entry)
    return entry


@app.post("/api/admin/certification-prerequisites")
def create_prerequisite_edge(
    payload: PrerequisiteEdgeIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Create (or update track_label on) one edge in the certification
    prerequisite DAG. Both certifications must already exist in the
    catalog. Upserts on the (certification, prerequisite) pair — safe to
    re-run. The any/all evaluation flag lives on the certification itself
    (CatalogUpsertIn.prerequisite_logic), not the edge — update it via
    upsert_catalog_entry."""
    cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == payload.certification_name))
    if not cert:
        raise HTTPException(status_code=422, detail=f"Unknown certification '{payload.certification_name}'.")
    prereq = db.scalar(
        select(CertificationCatalog).where(CertificationCatalog.name == payload.prerequisite_certification_name)
    )
    if not prereq:
        raise HTTPException(
            status_code=422, detail=f"Unknown prerequisite '{payload.prerequisite_certification_name}'."
        )

    edge = db.scalar(
        select(CertificationPrerequisite).where(
            CertificationPrerequisite.certification_id == cert.id,
            CertificationPrerequisite.prerequisite_certification_id == prereq.id,
        )
    )
    if edge:
        update_with_audit(db, edge, actor=admin.employee_id, changes={"track_label": payload.track_label})
    else:
        edge = create_with_audit(
            db,
            CertificationPrerequisite,
            actor=admin.employee_id,
            certification_id=cert.id,
            prerequisite_certification_id=prereq.id,
            track_label=payload.track_label,
        )

    db.commit()
    return {"id": edge.id, "certification": cert.name, "prerequisite": prereq.name}


@app.delete("/api/admin/certification-prerequisites")
def delete_prerequisite_edge(
    payload: PrerequisiteEdgeDeleteIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Remove one edge from the prerequisite DAG (e.g. a track being
    retired). Hard-deleted — an edge carries no history of its own worth
    preserving the way a drive's enrollments do; its creation/removal is
    still fully visible in audit_log."""
    cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == payload.certification_name))
    prereq = db.scalar(
        select(CertificationCatalog).where(CertificationCatalog.name == payload.prerequisite_certification_name)
    )
    if not cert or not prereq:
        raise HTTPException(status_code=422, detail="Unknown certification or prerequisite name.")
    edge = db.scalar(
        select(CertificationPrerequisite).where(
            CertificationPrerequisite.certification_id == cert.id,
            CertificationPrerequisite.prerequisite_certification_id == prereq.id,
        )
    )
    if not edge:
        raise HTTPException(status_code=404, detail="No such prerequisite edge.")
    delete_with_audit(db, edge, actor=admin.employee_id)
    db.commit()
    return {"deleted": True, "certification": cert.name, "prerequisite": prereq.name}


@app.post("/api/admin/learning-paths", response_model=LearningPathOut)
def seed_learning_path(
    payload: LearningPathSeedIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """create_learning_path: a whole new path with its initial items. To
    add one certification to an existing path afterward, use
    routers/learning_paths.py's add_certification_to_path /
    add_certification_to_paths instead — this endpoint only creates."""
    path = create_with_audit(
        db, LearningPath, actor=admin.employee_id,
        practice=payload.practice, name=payload.name, description=payload.description,
    )

    for item_in in payload.items:
        cert = db.scalar(
            select(CertificationCatalog).where(CertificationCatalog.name == item_in.certification_name)
        )
        if not cert:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown certification '{item_in.certification_name}' — seed the catalog entry first.",
            )
        create_with_audit(
            db, LearningPathItem, actor=admin.employee_id,
            path_id=path.id, certification_id=cert.id, is_required=item_in.is_required,
        )

    db.commit()
    db.refresh(path)
    return path


# Mounted last, deliberately: FastAPI/Starlette match routes in the order
# they're registered, so every /api/... route above gets first look. This
# catch-all StaticFiles mount at "/" only serves what nothing above already
# claimed — the custom single-page frontend (frontend/index.html and
# assets), same-origin with the API so the browser never needs CORS config.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
