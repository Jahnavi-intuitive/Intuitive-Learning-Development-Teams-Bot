"""Connection/session for the L&D portal database — a physically separate
Postgres database (lnd_portal) from the main lnd_certification one, holding
only real org certification data ingested from REAL EXCEL SHEETS. Kept as
its own engine/Base/session (not reusing app.database's Base) so the two
schemas never collide and can be migrated independently."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.database import engine as main_engine
from app.db_types import register_integer_id_assignment

# On Databricks both schemas live in one Unity Catalog schema, so the portal
# shares the main engine (one connection pool to the warehouse).
portal_engine = main_engine if settings.use_databricks else create_engine(settings.portal_database_url, pool_pre_ping=True)
PortalSessionLocal = sessionmaker(bind=portal_engine, autoflush=False, autocommit=False)


class PortalBase(DeclarativeBase):
    pass


register_integer_id_assignment(PortalBase)


def get_portal_db():
    db = PortalSessionLocal()
    try:
        yield db
    finally:
        db.close()
