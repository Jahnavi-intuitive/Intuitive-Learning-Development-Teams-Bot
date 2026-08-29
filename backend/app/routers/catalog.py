from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificationCatalog
from app.schemas import CatalogEntryOut

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/certifications", response_model=list[CatalogEntryOut])
def search_certifications(
    q: str | None = None, status: str | None = None, db: Session = Depends(get_db)
):
    """Autocomplete/validate against the catalog (used by the submission form)."""
    stmt = select(CertificationCatalog)
    if q:
        stmt = stmt.where(CertificationCatalog.name.ilike(f"%{q}%"))
    if status:
        stmt = stmt.where(CertificationCatalog.status == status)
    stmt = stmt.order_by(CertificationCatalog.name)
    return db.scalars(stmt).all()


@router.get("/certifications/{catalog_id}", response_model=CatalogEntryOut)
def get_certification(catalog_id: str, db: Session = Depends(get_db)):
    entry = db.get(CertificationCatalog, catalog_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Certification not found")
    return entry
