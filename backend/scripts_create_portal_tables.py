"""One-off: creates every lnd_portal table from the current ORM models
(app/portal_models.py) against whatever PORTAL_DATABASE_URL points at.
Safe to run more than once — create_all() skips tables that already exist.

Usage (from backend/):
  PORTAL_DATABASE_URL="postgresql+psycopg://<render-connection-string>" ../.venv/Scripts/python.exe scripts_create_portal_tables.py
"""

from app.portal_db import portal_engine, PortalBase
from app import portal_models  # noqa: F401 - import registers all models on PortalBase

PortalBase.metadata.create_all(portal_engine)
print("Portal tables created (or already present).")
