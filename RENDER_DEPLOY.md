# Deploying to Render

You're logged into Render already, so this is the exact sequence — nothing
here is optional/reference-only the way `DEPLOYMENT.md` (GCP) was. Follow
it in order.

## Deploying for $0 — what that actually changes

Render's Free instances only exist for Web Services, Postgres, Key Value,
and static sites — **private services aren't eligible for Free at all**.
That forced one real architecture change from the original plan:

- `lnd-backend` — a public Free **Web Service** (FastAPI). This is the
  one URL you'll actually use: `https://lnd-backend-xxxx.onrender.com/`
  serves the employee chat UI, `/admin.html` the admin dashboard.
- `lnd-agent` (the ADK agent) — also a public Free **Web Service** now,
  not a private one. Free web services also can't receive Render's
  private-network traffic, so the two talk to each other over plain
  public HTTPS instead of Render's internal network — see Step 2.5.
- `lnd-db` — Free Postgres (pgvector + pgcrypto, both Render-supported on
  Postgres 16) — see the free-tier limits called out below before relying
  on this long-term.

Neither of your two Python venvs, `pg0.exe`, or the `curl_transport.py`
workaround travel with this — they're all local-machine-only and none of
them are needed on Render (real cloud egress, no subprocess-spawn
scrutiny slowing every Gemini call down).

## Free-tier limits to know about before you rely on this

- **Postgres expires in 30 days.** A Free Render Postgres database is
  deleted 14 days after that unless you upgrade it to a paid plan. Fine
  for trying this out; not a place to leave real data unattended long-term.
  Only one Free Postgres instance is allowed per workspace.
- **Both web services spin down after 15 minutes idle**, and take about a
  minute to spin back up on the next request — so the first chat message
  (or the first request after a quiet stretch) will feel slow, then it's
  normal-speed until the next idle gap. This is a Render platform
  behavior, unrelated to the curl-subprocess latency this was built to
  get away from.
- **The in-process reminder sweep may not run on schedule.** It only
  fires if `lnd-backend` happens to be awake at the scheduled hour — on a
  spun-down Free instance it usually won't be. Trigger it manually from
  the admin dashboard's "run sweep now" button instead, or see the
  optional free workaround at the bottom of this doc.
- **Free services can be restarted by Render at any time**, and have no
  persistent local disk — irrelevant here since everything durable lives
  in Postgres, not on the container's filesystem.

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
   you 3 resources: `lnd-db`, `lnd-agent`, `lnd-backend` — confirm all
   three show the **Free** plan before continuing.
4. It will prompt you for secret values (marked `sync: false` in
   `render.yaml`, so they're never stored in the file itself):
   - **`GEMINI_API_KEY`** (on `lnd-backend`) — the same real key you
     already have in `backend/.env`.
   - **`GOOGLE_API_KEY`** (on `lnd-agent`) — the *same* key value again,
     just under the name the ADK library itself expects. Not two
     different keys.
   - **`BACKEND_URL`** (on `lnd-agent`) and **`AGENT_URL`** (on
     `lnd-backend`) — leave these as the `placeholder` default for now;
     Step 2.5 fixes them once both services actually exist.
5. Click **Apply**. Render provisions the database, builds both Docker
   images, and deploys. First build takes a few minutes (installing
   `pyjwt`, `google-adk`, etc. — this happens once, not on every request,
   unlike the curl-subprocess overhead on your current machine).

## Step 2.5 — Wire the two services together

They can't know each other's URL until both exist, so `render.yaml` left
these as placeholders — fill them in now:

1. Open `lnd-backend`'s dashboard page, copy its URL from the top (looks
   like `https://lnd-backend-xxxx.onrender.com`).
2. Open `lnd-agent`'s dashboard page, copy its URL the same way.
3. `lnd-agent` → **Environment** tab → set `BACKEND_URL` to `lnd-backend`'s
   URL from step 1 (include `https://`, no trailing slash) → save (this
   triggers a redeploy of `lnd-agent`).
4. `lnd-backend` → **Environment** tab → set `AGENT_URL` to `lnd-agent`'s
   URL from step 2 → save (redeploys `lnd-backend`).

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

Right now the deployed database is empty — correct schema, no rows.
`scripts/db_dump/full_dump.sql` is a fresh dump of the current local
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

## Security note — read before sharing either URL with anyone

Two things stack here, both worth knowing before you share this with
anyone else:

1. `render.yaml` sets `AUTH_MODE=header` — the same X-Employee-Id
   trust-at-face-value model you've been using locally the whole time.
   Harmless when only `localhost` could reach it; **not** harmless once
   the backend URL is public — anyone who finds it could claim to be any
   employee_id, including an admin one, just by setting a header.
2. `lnd-agent` is now a public URL too (the Free-tier tradeoff described
   above), not the fully-locked-down private service the paid GCP/Render
   design used. It has no independent authority of its own, though —
   every real check (who can see what) still happens on `lnd-backend`,
   which `lnd-agent` calls into exactly like any other caller, so this is
   a reduction in defense-in-depth, not an open door to the database.

Both are fine while you're the only one using this. Before sharing either
URL with anyone else: wire up real Entra ID auth (`AUTH_MODE=entra` — see
`backend/app/services/entra_auth.py` and the "Real Entra ID auth" section
of `DEPLOYMENT.md`, which applies identically here, GCP specifics aside),
and consider moving `lnd-agent` to a paid plan + `type: pserv` to restore
full network isolation.

## Optional — a genuinely free way to run the reminder sweep on schedule

Render Cron Jobs aren't Free-tier eligible either, so the paid-plan
approach in `DEPLOYMENT.md`/the note above about Cloud Scheduler doesn't
apply here for $0. A free external cron ping does the same job: sign up
for a free account at something like cron-job.org, and have it send a
scheduled `POST` to
`https://YOUR-BACKEND-URL.onrender.com/internal/notifications/run-sweep`
with header `X-Sweep-Trigger-Secret: <value>` (Render auto-generated that
secret via `generateValue: true` — find it on `lnd-backend`'s Environment
tab). This also has a side benefit: it pings `lnd-backend` on a schedule,
which counts as inbound traffic and resets its 15-minute idle timer, so
the ping itself helps prevent the spin-down described above.

Until you set that up, trigger the sweep manually from the admin
dashboard's "run sweep now" button whenever you want it to run.
