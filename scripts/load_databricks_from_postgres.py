"""Rebuilds every app table in Databricks (catalog/schema from DATABRICKS_*
in backend/.env) from the app's own model definitions, then copies all rows
from the local Postgres databases into it.

Uses CREATE OR REPLACE TABLE, so each table's previous Databricks version
stays recoverable with Delta time travel (RESTORE TABLE ... TO VERSION AS OF).
Foreign keys are left out: Databricks only treats them as informational.

Run from backend/:  .venv/Scripts/python.exe ../scripts/load_databricks_from_postgres.py
"""

import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.schema import CreateTable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import app.models  # noqa: E402,F401
import app.portal_models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.portal_db import PortalBase  # noqa: E402

# Databricks caps a statement at 256 bound parameters.
MAX_PARAMS = 250

if not settings.use_databricks:
    sys.exit("DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH / DATABRICKS_TOKEN are not set in backend/.env")

target = create_engine(settings.databricks_url())
sources = [
    (create_engine(settings.database_url), Base.metadata),
    (create_engine(settings.portal_database_url), PortalBase.metadata),
]

for source, metadata in sources:
    for table in metadata.sorted_tables:
        started = time.monotonic()
        with source.connect() as src:
            rows = [dict(r._mapping) for r in src.execute(select(table))]
        ddl = str(CreateTable(table, include_foreign_key_constraints=[]).compile(target))
        ddl = ddl.replace("CREATE TABLE", "CREATE OR REPLACE TABLE", 1)
        chunk = max(1, MAX_PARAMS // len(table.columns))
        with target.begin() as dst:
            dst.execute(text(ddl))
            for i in range(0, len(rows), chunk):
                dst.execute(insert(table).values(rows[i : i + chunk]))
            loaded = dst.execute(select(func.count()).select_from(table)).scalar()
        status = "OK" if loaded == len(rows) else "MISMATCH"
        print(f"{status:8} {table.name:30} source={len(rows):>5} databricks={loaded:>5}  ({time.monotonic() - started:.0f}s)")
