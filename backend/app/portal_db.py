"""Connection/session for the L&D portal database — a physically separate
Postgres database (lnd_portal) from the main lnd_certification one, holding
only real org certification data ingested from REAL EXCEL SHEETS. Kept as
its own engine/Base/session (not reusing app.database's Base) so the two
schemas never collide and can be migrated independently."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

portal_engine = create_engine(settings.portal_database_url, pool_pre_ping=True)
PortalSessionLocal = sessionmaker(bind=portal_engine, autoflush=False, autocommit=False)


class PortalBase(DeclarativeBase):
    pass


def get_portal_db():
    db = PortalSessionLocal()
    try:
        yield db
    finally:
        db.close()
