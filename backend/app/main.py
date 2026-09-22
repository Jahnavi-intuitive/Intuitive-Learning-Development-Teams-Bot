import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException

# Without this, Python's root logger defaults to WARNING — every
# logger.info(...) call anywhere in this app's own code (routers/teams.py's
# "unrecognized user" log included) was silently dropped, never written
# anywhere, confirmed directly: a real Teams message that hit that exact
# log line left no trace in the log file at all. uvicorn configures its
# own "uvicorn"/"uvicorn.access" loggers separately (which is why request
# lines like "POST /api/teams/messages ... 200 OK" were visible) — this
# basicConfig call is what makes this app's own logger.info/.warning calls
# visible too, app-wide, not just for Teams.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    conversation,
    dashboards,
    drives,
    employees,
    escalations,
    internal,
    knowledge_base,
    learning_paths,
    notifications,
    portal_admin,
    portal_dashboard,
    portal_data_download,
    portal_sync,
    portal_team_search,
    portal_trends,
    teams,
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
from app.services.excel_sync import ensure_certification_sheet_exists, sync_all_provider_workbooks
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.short_name import generate_short_name

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
app.include_router(dashboards.router)
app.include_router(portal_dashboard.router)
app.include_router(portal_admin.router)
app.include_router(portal_data_download.router)
app.include_router(portal_sync.router)
app.include_router(portal_team_search.router)
app.include_router(portal_trends.router)
app.include_router(drives.router)
app.include_router(notifications.router)
app.include_router(internal.router)
app.include_router(conversation.router)
app.include_router(teams.router)


async def _warm_teams_metadata_cache() -> None:
    """One-time, at-startup fetch of Bot Framework's OpenID metadata/signing
    keys (routers/teams.py's JWT validation needs these on every request).
    Best-effort — if this fails, the first real Teams message just pays
    for the fetch itself instead, same as before this existed. Purely a
    latency/reliability nicety: warming this here means that fetch never
    has to happen on the shared event loop while real traffic is already
    flowing.

    Awaited directly (not via asyncio.run) — a sync FastAPI startup
    handler already executes inside uvicorn's live event loop, so
    asyncio.run() raises "cannot be called from a running event loop"
    there; confirmed by hitting exactly that error on the first attempt.

    The actual retry-on-flaky-network logic lives in
    services/teams_auth_resilience.py (applied when routers/teams.py is
    imported, which happens before this function ever runs) — this call
    already benefits from it automatically."""
    if not settings.teams_bot_app_id:
        return

    from botframework.connector.auth.authentication_constants import AuthenticationConstants
    from botframework.connector.auth.jwt_token_extractor import JwtTokenExtractor

    metadata = JwtTokenExtractor.get_open_id_metadata(
        AuthenticationConstants.TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL
    )
    try:
        await metadata._refresh()  # noqa: SLF001 — intentionally reaching into the SDK's own cache object
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not pre-warm the Teams OpenID metadata cache at startup, even after retrying — "
            "the first real Teams message will pay for that fetch instead.",
            exc_info=True,
        )


@app.on_event("startup")
async def _startup():
    start_scheduler()
    await _warm_teams_metadata_cache()


@app.on_event("shutdown")
def _shutdown():
    stop_scheduler()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/admin/sync-tracker")
def sync_tracker(db: Session = Depends(get_db)):
    """Manually trigger an Excel tracker sync across every provider
    workbook in the SharePoint Certification Trackers library (see
    services/excel_sync.py::sync_all_provider_workbooks — 2026-09-13,
    replacing the single-local-file version). Not agent-facing — an L&D
    admin or the dev workflow calls this."""
    report = sync_all_provider_workbooks(db)
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
    real admin employee's X-Employee-Id, same as any other caller).

    2026-09-13: also the one place `provider` actually gets set on a
    catalog entry today (there's no separate "assign provider" endpoint) —
    the moment it's set (or changed) to a real value, ensures that
    provider's SharePoint workbook and this certification's sheet within
    it both exist (services/excel_sync.py::ensure_certification_sheet_exists).
    Also backfills short_name here if somehow still missing (an admin
    directly creating an entry through this endpoint bypasses
    services/catalog_provisioning.py entirely, unlike the auto-discovery
    paths, so it wouldn't otherwise get one) — no admin notification here
    though, unlike catalog_provisioning's own path: an admin deliberately
    using this CRUD endpoint already knows about the certification they're
    creating."""
    entry = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == payload.name))
    fields = payload.model_dump(exclude={"name"})
    previous_provider = entry.provider if entry else None

    if entry:
        update_with_audit(db, entry, actor=admin.employee_id, changes=fields)
    else:
        entry = create_with_audit(db, CertificationCatalog, actor=admin.employee_id, name=payload.name, **fields)

    if not entry.short_name:
        existing_short_names = set(
            db.scalars(
                select(CertificationCatalog.short_name).where(CertificationCatalog.short_name.is_not(None))
            ).all()
        )
        entry.short_name = generate_short_name(entry.name, existing_short_names)
        db.flush()

    if entry.provider and entry.provider != previous_provider:
        ensure_certification_sheet_exists(db, entry)

    db.commit()
    db.refresh(entry)
    return entry


@app.post("/api/admin/catalog-entries/{catalog_id}/ensure-sharepoint-setup", response_model=CatalogEntryOut)
def ensure_catalog_entry_sharepoint_setup(
    catalog_id: str,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Backfill/repair endpoint, deliberately separate from
    upsert_catalog_entry above: that endpoint's own short_name/SharePoint-
    sheet side effects only fire on a genuine field *change* (upserting
    with a partial payload would otherwise silently clobber an existing
    entry's other real fields back to CatalogUpsertIn's schema defaults —
    not safe for a bulk backfill over already-real data). This endpoint
    only ever reads/sets short_name and calls ensure_certification_sheet_exists
    — it never touches any other field, so it's safe to call on every
    existing catalog entry unconditionally. Used by
    scripts/seed_sharepoint_provider_files.py (2026-09-13) to backfill
    short_name and create every provider's SharePoint workbook for
    certifications that existed before this migration; also a reasonable
    one-off "repair" action for the admin dashboard later if a
    provider's file/sheet is ever deleted out from under a catalog entry."""
    entry = db.get(CertificationCatalog, catalog_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Certification not found")

    if not entry.short_name:
        existing_short_names = set(
            db.scalars(
                select(CertificationCatalog.short_name).where(CertificationCatalog.short_name.is_not(None))
            ).all()
        )
        entry.short_name = generate_short_name(entry.name, existing_short_names)
        db.flush()

    if entry.provider:
        ensure_certification_sheet_exists(db, entry)

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


@app.get("/api/admin/learning-paths/{practice}/graph")
def learning_path_graph(
    practice: str,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Pure structural view of one practice's learning path — every
    certification on the path plus every real prerequisite edge between
    them, with no employee-specific overlay (that's
    routers/learning_paths.py's /overview, which is self-only and
    per-employee). Built for the admin dashboard's visual path diagram —
    edges are only included when both ends are on this same path, so a
    prerequisite living in a different practice's path never shows up as
    a dangling reference here."""
    path = db.scalar(select(LearningPath).where(LearningPath.practice == practice))
    if not path:
        return {"practice": practice, "path_id": None, "path_name": None, "nodes": [], "edges": []}

    cert_ids = {item.certification_id for item in path.items}
    edges = db.scalars(
        select(CertificationPrerequisite).where(
            CertificationPrerequisite.certification_id.in_(cert_ids),
            CertificationPrerequisite.prerequisite_certification_id.in_(cert_ids),
        )
    ).all()

    return {
        "practice": practice,
        "path_id": str(path.id),
        "path_name": path.name,
        "nodes": [
            {
                "id": str(item.certification_id),
                "name": item.certification.name,
                "provider": item.certification.provider,
                "level": item.certification.level,
                "status": item.certification.status,
                "is_required": item.is_required,
                "prerequisite_logic": item.certification.prerequisite_logic,
            }
            for item in path.items
        ],
        "edges": [
            {
                "certification_id": str(e.certification_id),
                "prerequisite_certification_id": str(e.prerequisite_certification_id),
                "certification_name": e.certification.name,
                "prerequisite_certification_name": e.prerequisite.name,
                "track_label": e.track_label,
            }
            for e in edges
        ],
    }


class _NoCacheStaticFiles(StaticFiles):
    """Plain StaticFiles left the browser free to heuristically cache
    frontend/*.html with no explicit Cache-Control header at all — real,
    hit during this project's own active development: edits saved and
    confirmed served by a direct curl were still invisible in the browser
    after a plain reload (and sometimes even a hard reload), since nothing
    told the browser it had to revalidate. `no-cache` (not `no-store`)
    still lets the browser reuse a cached response once the server
    confirms via ETag/Last-Modified it's unchanged (a real 304), it just
    removes the option to skip asking entirely."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mounted last, deliberately: FastAPI/Starlette match routes in the order
# they're registered, so every /api/... route above gets first look. This
# catch-all StaticFiles mount at "/" only serves what nothing above already
# claimed — the custom single-page frontend (frontend/index.html and
# assets), same-origin with the API so the browser never needs CORS config.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
app.mount("/", _NoCacheStaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
