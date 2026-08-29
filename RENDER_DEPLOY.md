# Deploying to Render

You're logged into Render already, so this is the exact sequence — nothing
here is optional/reference-only the way `DEPLOYMENT.md` (GCP) was. Follow
it in order.

## What you'll end up with

- `lnd-backend` — a public Render **Web Service** (FastAPI). This is the
  one URL you'll actually use: `https://lnd-backend-xxxx.onrender.com/`
  serves the employee chat UI, `/admin.html` the admin dashboard.
- `lnd-agent` — a **Private Service** (the ADK agent). No public URL at
  all — only `lnd-backend` can reach it, over Render's internal network.
  Stronger isolation than anything we could add in application code.
- `lnd-db` — managed Postgres (pgvector + pgcrypto, both Render-supported
  on Postgres 16).

Neither of your two Python venvs, `pg0.exe`, or the `curl_transport.py`
workaround travel with this — they're all local-machine-only and none of
them are needed on Render (real cloud egress, no subprocess-spawn
scrutiny slowing every Gemini call down).

## Step 1 — Push this repo to GitHub

This repo already has `git init` run and the first commit made locally
(verified: no `.env`, no API keys, no database dump made it in). Render
deploys from a connected Git repo, so:

1. Go to https://github.com/new, create an **empty** repository named
   `Intuitive-Learning-Development-Agent` (GitHub repo names can't contain
   spaces or `&` — this is the closest match to "Intuitive Learning &
   Development agent"; rename it after creation if you want a different
   exact name). Don't let GitHub add a README/license — there are already
   files here.
2. Copy its URL, then run these two commands from this folder:
   ```
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin master
   ```

## Step 2 — Create the Blueprint in Render

1. Render dashboard → **New** → **Blueprint**.
2. Connect the GitHub repo you just pushed.
3. Render reads `render.yaml` from the repo root automatically and shows
   you 3 resources: `lnd-db`, `lnd-agent`, `lnd-backend`.
4. It will prompt you for 2 secret values (marked `sync: false` in
   `render.yaml`, so they're never stored in the file itself):
   - **`GEMINI_API_KEY`** (on `lnd-backend`) — the same real key you
     already have in `backend/.env`.
   - **`GOOGLE_API_KEY`** (on `lnd-agent`) — the *same* key value again,
     just under the name the ADK library itself expects. Not two
     different keys.
5. Click **Apply**. Render provisions the database, builds both Docker
   images, and deploys. First build takes a few minutes (installing
   `pyjwt`, `google-adk`, etc. — this happens once, not on every request,
   unlike the curl-subprocess overhead on your current machine).

## Step 3 — Watch the first deploy, then check it's actually up

The backend's container runs `alembic upgrade head` automatically before
starting `uvicorn` (see `backend/Dockerfile`) — on a brand-new empty
database this creates all 16 tables fresh, with zero data in them yet.

Once `lnd-backend`'s dashboard shows "Live":
```
curl https://YOUR-BACKEND-URL.onrender.com/health
```
should return `{"status":"ok"}`. If it doesn't, check the backend
service's Logs tab — the most likely first-run issue is a typo in one of
the two secret values from Step 2.

## Step 4 — Restore your real data (employees, catalog, KB embeddings)

Right now the deployed database is empty — correct schema, no rows. I
regenerated `scripts/db_dump/full_dump.sql` fresh from your current local
database (all 4 migrations, all real data: 5 employees, 50 catalog
entries, 48 KB rows with real Gemini embeddings) — it's gitignored (real
employee PII), so it never went to GitHub; it's sitting on this machine
only.

1. In Render, open **`lnd-db`** → **Connect** tab → copy the **External
   Database URL** (starts with `postgresql://`, usable from outside
   Render — different from the internal one `lnd-backend` uses).
2. Run this from this project folder (adjust the path to `psql.exe` if
   you don't have it elsewhere — pg0's bundled one works fine):
   ```
   & "$env:USERPROFILE\.pg0\installation\18.1.0\bin\psql.exe" "PASTE_THE_EXTERNAL_URL_HERE" -f scripts\db_dump\full_dump.sql
   ```
   This dump was generated with `--clean --if-exists`, so it's safe to
   run even though the tables already exist from Step 3's migration —
   it drops and recreates everything, ending with your exact real data.
3. Re-check `/health` and open the deployed URL in a browser — the
   employee dropdown should now show your 5 real employees.

## Step 5 — Try it

- `https://YOUR-BACKEND-URL.onrender.com/` — employee chat UI.
- `https://YOUR-BACKEND-URL.onrender.com/admin.html` — admin dashboard
  (E1001 / Jahnavi Patel is already `is_admin=true` in the restored data).

## Security note — read before sharing this URL with anyone

`render.yaml` sets `AUTH_MODE=header` — the same X-Employee-Id
trust-at-face-value model you've been using locally the whole time. That
was harmless when only `localhost` could reach it; it is **not** harmless
once the URL is public on the internet — anyone who finds it could claim
to be any employee_id, including an admin one, just by setting a header.

Fine for now while you're the only one using it. Before sharing this URL
with anyone else, wire up real Entra ID auth (`AUTH_MODE=entra` — see
`backend/app/services/entra_auth.py` and the "Real Entra ID auth" section
of `DEPLOYMENT.md`, which applies identically here, GCP specifics aside).

## Optional next step — the reminder sweep on a real schedule

The in-process scheduler (`NOTIFICATION_SWEEP_ENABLED=true` in
`render.yaml`) works fine on Render's single-instance free/starter plans.
If you ever scale `lnd-backend` to multiple instances, switch to a Render
**Cron Job** service instead (same reasoning as Cloud Scheduler in
`DEPLOYMENT.md`): set `NOTIFICATION_SWEEP_ENABLED=false`, create a Cron
Job hitting `POST https://YOUR-BACKEND-URL.onrender.com/internal/notifications/run-sweep`
with header `X-Sweep-Trigger-Secret: <value>` — Render auto-generated that
secret (`generateValue: true`); find it on `lnd-backend`'s Environment
tab.
