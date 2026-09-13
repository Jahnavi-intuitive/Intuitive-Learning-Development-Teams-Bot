from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://lnd_app:change_me_locally@localhost:5432/lnd_certification"
    gemini_api_key: str = ""
    tracker_xlsx_path: str = "./data/trackers/certification_tracker.xlsx"
    dev_default_employee_id: str = ""
    agent_url: str = "http://localhost:8001"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        """Render (and Heroku, and most other PaaS Postgres add-ons) hand
        you a bare `postgres://` or `postgresql://` URL — SQLAlchemy then
        defaults to psycopg2, which isn't installed here (requirements.txt
        pins psycopg[binary]==3, i.e. psycopg 3). Rewrite the scheme so the
        right driver is picked regardless of what the platform gave us."""
        if v.startswith("postgres://"):
            return "postgresql+psycopg://" + v[len("postgres://") :]
        if v.startswith("postgresql://") and not v.startswith("postgresql+psycopg://"):
            return "postgresql+psycopg://" + v[len("postgresql://") :]
        return v

    @field_validator("agent_url")
    @classmethod
    def _normalize_agent_url(cls, v: str) -> str:
        """Render's fromService env var wiring (see render.yaml) supplies
        a private service's address as a bare `host:port`, not a full URL —
        Render's internal network is plain HTTP, no TLS termination needed
        between services on it."""
        if v and "://" not in v:
            return f"http://{v}"
        return v

    # Embedding model + output dimension. Kept in one place because the
    # knowledge_base.embedding and escalations.question_embedding columns
    # are declared with a fixed vector width that must match this.
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768

    # Name-match / cert-match confidence threshold for Credly verification.
    verification_confidence_threshold: float = 0.85

    # Reminder sweep (drives/expiry notifications) — schedule only. The
    # actual reminder-window/escalation/retry-cap *values* live in the
    # notification_settings table and certification_drives.reminder_cadence
    # (adjustable without a redeploy) per BR-D-05's own "not fixed"
    # instruction — this is only "what time of day does the job run."
    notification_sweep_enabled: bool = True
    notification_sweep_hour: int = 6  # local hour, 24h clock
    notification_sweep_minute: int = 0

    # --- Production auth seam -------------------------------------------
    # "header": the X-Employee-Id dev stand-in used throughout local
    #   development — trusts the header at face value, no verification.
    # "entra": real Microsoft Entra ID JWT validation (services/entra_auth.py).
    #   The header path is NOT consulted at all in this mode — there is no
    #   bypass. Refuses to start with auth_mode="entra" unless both
    #   entra_tenant_id and entra_audience are set (see app/main.py).
    auth_mode: str = "header"
    entra_tenant_id: str = ""
    entra_audience: str = ""  # the backend app registration's Application ID URI or client ID

    # Comma-separated service-account emails allowed to use the internal
    # service-token relay path (X-Internal-Service-Token) in "entra" mode —
    # in practice, just the agent Cloud Run service's own service account.
    # Empty means the relay path is refused entirely (fail closed): the
    # agent's tool calls simply won't authenticate until this is set,
    # rather than silently trusting an unlisted caller. See
    # services/entra_auth.py::validate_service_id_token.
    trusted_service_accounts: str = ""

    # Shared secret Cloud Scheduler sends as X-Sweep-Trigger-Secret to
    # POST /internal/notifications/run-sweep (see routers/internal.py).
    # That endpoint has no employee identity to check — it's protected by
    # this secret instead, since Cloud Run's per-service IAM auth can't be
    # scoped to a single route on a service that also serves the public
    # API. Empty (default) means the endpoint refuses every request —
    # local dev triggers the sweep via the admin-gated dashboard endpoint
    # instead, never this one.
    sweep_trigger_secret: str = ""

    # --- Vertex AI (production Gemini access) ---------------------------
    # Leave both unset for direct-API-key access via gemini_api_key (local
    # dev, per https://adk.dev/agents/models/google-gemini/). Setting
    # google_cloud_project switches services/embeddings.py to Vertex AI —
    # IAM/service-account auth instead of a long-lived API key.
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"

    # --- Microsoft Teams / Bot Framework -------------------------------
    # Empty by default — every value here is a real credential/tenant fact
    # that must come from the person doing the Azure setup (see
    # teams-app/AZURE_BOT_SETUP.md), never invented. routers/teams.py
    # refuses to authenticate any request while teams_bot_app_id is unset.
    teams_bot_app_id: str = ""
    teams_bot_app_password: str = ""
    # The Entra/M365 tenant this bot is allowed to serve — checked against
    # every incoming activity's channelData.tenant.id server-side (belt-
    # and-suspenders alongside Teams' own app-catalog scoping, and the
    # bot registration's own "Single Tenant" app type).
    teams_tenant_id: str = ""

    # --- Microsoft Graph (app-only, services/graph_client.py + services/sharepoint_client.py) -
    # A separate Entra app registration from the bot's own (teams_bot_app_id
    # above needs no Graph permissions at all). Two application permissions
    # on this same app registration, for two different purposes:
    # - User.Read.All (graph_client.py): look up *any* employee's directory
    #   profile, not just a signed-in one, for Teams auto-provisioning.
    # - Sites.Selected (sharepoint_client.py): read/write exactly one
    #   SharePoint site's document library — the "Certification Trackers"
    #   library — granted via a one-time site-specific permission call, not
    #   tenant-wide access (see DEVELOPMENT_LOG.md's 2026-09-13 entry for the
    #   exact grant steps).
    # Reuses teams_tenant_id as the token-issuing tenant, since it's the
    # same organization. Empty by default; routers/teams.py's auto-
    # provisioning path treats "not configured" as a normal, gracefully-
    # handled Graph lookup failure, not a startup-time refusal — an
    # already-recognized employee's messages never need Graph at all.
    graph_client_id: str = ""
    graph_client_secret: str = ""

    # The SharePoint site + document library holding one Excel file per
    # certification provider (services/sharepoint_client.py). Both are
    # real, tenant-specific Graph IDs resolved once during setup — not
    # something to guess or reconstruct from a URL, so they're stored
    # directly rather than re-resolved by path on every sync.
    sharepoint_site_id: str = ""
    sharepoint_drive_id: str = ""  # the "Certification Trackers" library's drive id specifically

    # --- Two-tier conversation memory (services/conversation_memory.py) -
    # Tier 1 (conversation_turns) is never replayed in full — only the
    # most recent N turns are read as live context per message.
    conversation_recent_turns: int = 10
    # Tier 2 (conversation_memory.summary_text) is refreshed by folding
    # everything older than the live window into one LLM-summarized
    # paragraph, triggered by whichever of these two comes first:
    conversation_summarize_every_n_turns: int = 15
    conversation_summarize_token_budget: int = 4000  # rough chars/4 estimate, not a real tokenizer count
    # The running summary isn't kept forever: once the turns folded into it
    # since it was last reset exceed this, it's discarded (not truncated —
    # the raw conversation_turns rows are untouched either way, this only
    # bounds what stays folded into the live long-term-memory tier). Tuned
    # for this bot's actual use (short, transactional Q&A) where content
    # from well beyond this many turns back isn't worth carrying forward.
    conversation_summary_max_age_turns: int = 50
    # After a summarization LLM call fails, wait at least this long before
    # trying again, instead of retrying on every single subsequent turn.
    conversation_summarize_retry_cooldown_seconds: int = 900

    # --- Per-employee rate limiting (services/rate_limit.py) ------------
    # Defense-in-depth against one misbehaving client or a retry-loop bug —
    # deliberately independent of whatever the hosting platform's own edge
    # protection provides. Applies per employee_id, not globally.
    rate_limit_per_minute: int = 20


settings = Settings()
