"""Exports every table in both local databases (main app + portal) to
Parquet files under databricks_export/, for upload into a Databricks
Unity Catalog volume. Re-runnable — each run overwrites the files.

Postgres-only types are flattened to plain values Databricks can read:
UUIDs and enums to strings, JSON/JSONB and pgvector embeddings to JSON
text. Contains real employee data — databricks_export/ is gitignored.

Run from backend/:  .venv/Scripts/python.exe ../scripts/export_to_parquet.py
"""

import json
import sys
import uuid
from pathlib import Path

import pandas as pd
from sqlalchemy import Date, DateTime, create_engine, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.config import settings  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "databricks_export"
SKIP = {"alembic_version"}


def _flatten(value):
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    if hasattr(value, "tolist"):  # numpy array, e.g. a pgvector embedding
        return json.dumps(value.tolist())
    return value


def export(db_label: str, url: str) -> None:
    engine = create_engine(url)
    with engine.connect() as conn:
        for table in sorted(set(inspect(conn).get_table_names()) - SKIP):
            # numpy_nullable keeps integer/boolean columns with blanks as
            # integers/booleans instead of coercing them to floats/objects.
            df = pd.read_sql(text(f'SELECT * FROM "{table}"'), conn, dtype_backend="numpy_nullable")
            # That backend reads plain DATE columns back as text — restore them
            # from the column's real type so Databricks gets a DATE, not a string.
            date_cols = [
                c["name"] for c in inspect(conn).get_columns(table)
                if isinstance(c["type"], Date) and not isinstance(c["type"], DateTime)
            ]
            for col in date_cols:
                df[col] = pd.to_datetime(df[col]).dt.date
            for col in df.columns[df.dtypes == object]:
                if col in date_cols:
                    continue
                df[col] = df[col].map(_flatten).astype("string")
            # Spark (Databricks) can't read nanosecond Parquet timestamps.
            df.to_parquet(OUT / f"{table}.parquet", index=False, coerce_timestamps="us", allow_truncated_timestamps=True)
            print(f"{db_label:6} {table:30} {len(df):>6} rows")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    export("main", settings.database_url)
    export("portal", settings.portal_database_url)
    print(f"\nWritten to {OUT}")
