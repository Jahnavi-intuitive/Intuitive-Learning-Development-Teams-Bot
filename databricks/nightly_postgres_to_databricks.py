# Databricks notebook source
# Nightly copy of the app's live Postgres database into Unity Catalog
# (`dbx-ai-agents`.lnd_agent) for reporting. Postgres stays the app's live
# database; this only overwrites the Databricks copy.
#
# Import into Databricks (Workspace > Import > this file), then schedule it:
# Workflows > Create job > Notebook task > this notebook > add a daily trigger.
#
# Needs a secret holding the Postgres password:
#   databricks secrets create-scope lnd-agent
#   databricks secrets put-secret lnd-agent pg-password

# COMMAND ----------

CATALOG = "dbx-ai-agents"
SCHEMA = "lnd_agent"
PG_HOST = "<render-postgres-host>"  # e.g. dpg-xxxx-a.oregon-postgres.render.com
PG_DB = "lnd_certification"
PG_USER = "lnd_app"

jdbc_url = f"jdbc:postgresql://{PG_HOST}:5432/{PG_DB}?sslmode=require"
props = {
    "user": PG_USER,
    "password": dbutils.secrets.get("lnd-agent", "pg-password"),
    "driver": "org.postgresql.Driver",
}

def pg_query(sql):
    return spark.read.jdbc(url=jdbc_url, table=f"({sql}) AS q", properties=props)

# COMMAND ----------

# Enums, pgvector embeddings and JSON columns have no Spark equivalent over
# JDBC, so they are cast to text in Postgres before reading.
columns = pg_query("""
    SELECT table_name, column_name, data_type, ordinal_position
    FROM information_schema.columns
    WHERE table_schema = 'public'
""").collect()
tables = sorted(
    r.table_name for r in pg_query("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
          AND table_name <> 'alembic_version'
    """).collect()
)

for table in tables:
    cols = sorted((c for c in columns if c.table_name == table), key=lambda c: c.ordinal_position)
    select_list = ", ".join(
        f'"{c.column_name}"::text AS "{c.column_name}"'
        if c.data_type in ("USER-DEFINED", "json", "jsonb", "uuid", "ARRAY")
        else f'"{c.column_name}"'
        for c in cols
    )
    df = pg_query(f'SELECT {select_list} FROM "{table}"')
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"`{CATALOG}`.{SCHEMA}.{table}")
    print(table, df.count())
