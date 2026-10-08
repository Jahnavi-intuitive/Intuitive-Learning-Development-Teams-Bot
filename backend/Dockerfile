# Backend (FastAPI) — Cloud Run image.
#
# Runs as a non-root user, listens on $PORT (Cloud Run's convention — it
# injects PORT and expects the container to bind to it, not a hardcoded
# 8000). No curl.exe workaround applies here: services/curl_transport.py
# is a no-op on any non-Windows platform (see that module's docstring).
#
# BUILD CONTEXT IS THE REPO ROOT, not backend/ — main.py serves ../frontend
# (a sibling of backend/), which Docker can't COPY from a parent directory
# relative to a backend/-scoped context. Build with:
#   docker build -f backend/Dockerfile -t lnd-backend .
# (run from the repo root, not from inside backend/).

FROM python:3.12-slim

WORKDIR /app

# libpq is needed by psycopg[binary]'s C extension at import time on slim images.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini .

# main.py finds the frontend via 3 parent-directories-up from itself
# (repo layout: backend/app/main.py -> backend/app -> backend -> repo
# root -> repo_root/frontend). Mirror that exact nesting here: this
# WORKDIR (/app) plays the role of "backend/", so frontend must land at
# /frontend (a sibling of /app), NOT /app/frontend — an absolute
# destination path, not one relative to WORKDIR.
COPY frontend /frontend

RUN useradd --create-home --uid 1000 appuser
USER appuser

ENV PORT=8080
EXPOSE 8080

# On Postgres, run migrations first (a no-op once the DB is current). On
# Databricks there are no Alembic migrations — tables are built by
# scripts/load_databricks_from_postgres.py — so it's skipped.
CMD ["sh", "-c", "if [ -z \"$DATABRICKS_SERVER_HOSTNAME\" ]; then alembic upgrade head; fi && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
