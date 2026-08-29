# Deploying to GCP

This is a ready-to-run reference, not something that's been executed
against a real project — that needs your own GCP project ID, billing
account, and (for real auth) your Entra ID tenant details, none of which
exist in this dev environment. Replace every `YOUR_*` placeholder before
running anything.

## What goes where

| Component | GCP service |
|---|---|
| Backend (FastAPI) | Cloud Run |
| Agent (`adk api_server`) | Cloud Run (separate service, `--no-allow-unauthenticated`) |
| Postgres + pgvector | Cloud SQL for PostgreSQL, private IP |
| Static frontend | Served by the backend's own `StaticFiles` mount — no separate infra |
| Reminder sweep | Cloud Scheduler → `POST /internal/notifications/run-sweep` on the backend |
| Secrets | Secret Manager |
| Images | Artifact Registry |

## 1. One-time project setup

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export REGION=us-central1
gcloud config set project "$PROJECT_ID"

gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  vpcaccess.googleapis.com \
  aiplatform.googleapis.com

gcloud artifacts repositories create lnd-agent --repository-format=docker --location="$REGION"
```

## 2. Cloud SQL (private IP, no public access)

```bash
gcloud compute networks vpc-access connectors create lnd-connector \
  --region="$REGION" --network=default --range=10.8.0.0/28

gcloud sql instances create lnd-db \
  --database-version=POSTGRES_17 \
  --region="$REGION" \
  --tier=db-custom-1-3840 \
  --no-assign-ip \
  --network=default \
  --database-flags=cloudsql.iam_authentication=on

gcloud sql databases create lnd_certification --instance=lnd-db
gcloud sql users create lnd_app --instance=lnd-db --password="$(openssl rand -base64 24)"

# pgvector: connect once via the Cloud SQL Auth Proxy and run
#   CREATE EXTENSION IF NOT EXISTS vector;
#   CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

## 3. Secrets

```bash
echo -n "postgresql+psycopg://lnd_app:REAL_PASSWORD@/lnd_certification?host=/cloudsql/$PROJECT_ID:$REGION:lnd-db" \
  | gcloud secrets create database-url --data-file=-

echo -n "$(openssl rand -hex 32)" | gcloud secrets create sweep-trigger-secret --data-file=-

# Only if NOT using Vertex AI (direct API key path):
echo -n "YOUR_GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
```

## 4. Service accounts (least privilege — not the default compute SA)

```bash
gcloud iam service-accounts create lnd-backend-sa
gcloud iam service-accounts create lnd-agent-sa

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:lnd-backend-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"
gcloud secrets add-iam-policy-binding database-url \
  --member="serviceAccount:lnd-backend-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding sweep-trigger-secret \
  --member="serviceAccount:lnd-backend-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# If using Vertex AI instead of a direct Gemini API key:
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:lnd-backend-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:lnd-agent-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

## 5. Build + deploy the agent (deploy this first — the backend needs its URL)

```bash
docker build -f agent/Dockerfile -t "$REGION-docker.pkg.dev/$PROJECT_ID/lnd-agent/agent:latest" agent/
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/lnd-agent/agent:latest"

gcloud run deploy lnd-agent \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/lnd-agent/agent:latest" \
  --region="$REGION" \
  --service-account="lnd-agent-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --no-allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_GENAI_USE_ENTERPRISE=true,BACKEND_URL=https://YOUR-BACKEND-URL.run.app"

AGENT_URL=$(gcloud run services describe lnd-agent --region="$REGION" --format='value(status.url)')

# Only the backend's SA may invoke the agent:
gcloud run services add-iam-policy-binding lnd-agent \
  --region="$REGION" \
  --member="serviceAccount:lnd-backend-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 6. Build + deploy the backend (build context is the repo root — see backend/Dockerfile)

```bash
docker build -f backend/Dockerfile -t "$REGION-docker.pkg.dev/$PROJECT_ID/lnd-agent/backend:latest" .
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/lnd-agent/backend:latest"

gcloud run deploy lnd-backend \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/lnd-agent/backend:latest" \
  --region="$REGION" \
  --service-account="lnd-backend-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --add-cloudsql-instances="$PROJECT_ID:$REGION:lnd-db" \
  --vpc-connector=lnd-connector \
  --set-secrets="DATABASE_URL=database-url:latest,SWEEP_TRIGGER_SECRET=sweep-trigger-secret:latest" \
  --set-env-vars="AGENT_URL=$AGENT_URL,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,AUTH_MODE=entra,ENTRA_TENANT_ID=YOUR_TENANT_ID,ENTRA_AUDIENCE=YOUR_APP_CLIENT_ID,TRUSTED_SERVICE_ACCOUNTS=lnd-agent-sa@$PROJECT_ID.iam.gserviceaccount.com,NOTIFICATION_SWEEP_ENABLED=false"
  # NOTIFICATION_SWEEP_ENABLED=false here — Cloud Scheduler drives the
  # sweep instead (step 8). Leaving the in-process APScheduler on here
  # too would double-fire once this service scales beyond one instance.
```

Whether the backend itself needs `--no-allow-unauthenticated` depends on
who calls it directly: if a browser/Teams client hits it straight (no
separate API gateway), it must be publicly invokable at the Cloud Run
layer — `auth_mode=entra` is what actually protects it at the application
layer in that case. If a gateway/BFF sits in front of it instead, lock
Cloud Run down the same way as the agent (`--no-allow-unauthenticated` +
`roles/run.invoker` for that gateway's SA).

## 7. Real Entra ID auth — what you still need to provide

1. Register an app in Entra ID (App registrations) for this backend.
   `ENTRA_AUDIENCE` = that app's Application ID URI or client ID.
2. Declare an app role (App roles blade) named `LnD.Admin` (or update
   `EntraIdentity.is_admin` in `backend/app/services/entra_auth.py` to
   match whatever name you actually use) and assign it to your L&D
   admins.
3. `ENTRA_TENANT_ID` = your tenant ID.
4. Whatever calls this backend as a human (a Teams tab, a web frontend)
   needs to acquire a token for that same audience and send it as
   `Authorization: Bearer <token>`.

Until these are set, keep `AUTH_MODE=header` — the backend refuses to
start with `AUTH_MODE=entra` and either value missing (see the startup
guard in `backend/app/main.py`), rather than run half-configured.

## 8. Cloud Scheduler (the reminder sweep)

```bash
gcloud scheduler jobs create http lnd-reminder-sweep \
  --location="$REGION" \
  --schedule="0 6 * * *" \
  --uri="https://YOUR-BACKEND-URL.run.app/internal/notifications/run-sweep" \
  --http-method=POST \
  --headers="X-Sweep-Trigger-Secret=$(gcloud secrets versions access latest --secret=sweep-trigger-secret)"
```

## 9. Optional hardening (infra config, not application code)

- **Cloud Armor**: attach a security policy to the backend's load balancer
  if it's directly internet-facing, mainly for basic WAF rules and rate
  limiting on the Gemini-backed endpoints (cost control).
- **VPC Service Controls**: a perimeter around the project if your org
  requires stricter data-exfiltration controls — likely overkill at this
  scale, worth it if compliance requires it.
- **Cloud SQL**: confirm `require_ssl` is on (private-IP-only Cloud SQL
  instances created above already refuse public connections); enable
  automated backups + point-in-time recovery.
- **Cloud Audit Logs**: enable Data Access logs for Cloud SQL and Secret
  Manager — a separate layer from this app's own `audit_log` table
  (infra access trail vs. business action trail; you want both).
- Rotate whatever value is currently in `backend/.env`'s `GEMINI_API_KEY`
  before this project goes anywhere near a shared environment — treat a
  key that's ever lived in a plaintext `.env` file as already exposed.
