"""Attaches a Google-signed ID token when this agent calls back into the
backend (tools.py) — the counterpart to backend/app/services/gcp_id_token.py
and to backend/app/services/entra_auth.py::validate_service_id_token,
which is what actually checks this token on the receiving end.

No-op for a localhost/127.0.0.1 target (local dev) and falls back to no
header at all if the fetch fails for any other reason (not actually
running on GCP) — the backend's own auth_mode decides what to do with a
missing token; this module never raises.

2026-09-15: this ran on *every single tool call* the agent makes back
into the backend, and off-GCP (Render, included) `id_token.fetch_id_token`
doesn't fail fast — it retries the unreachable GCE metadata server with
backoff first. Measured directly on the deployed Render instance: the
backend's own /api/tracker/records answers in ~0.4s, but the same call
made as a tool from the agent took ~13s — this fetch was the entire gap,
paid again on every tool call in a turn. `K_SERVICE` is set automatically
on every real Cloud Run instance and nowhere else, so checking it first
skips the slow probe entirely anywhere that isn't actually Cloud Run.
"""

import logging
import os

logger = logging.getLogger(__name__)


def fetch_service_token_header(target_url: str) -> dict[str, str]:
    if "localhost" in target_url or "127.0.0.1" in target_url:
        return {}
    if not os.environ.get("K_SERVICE"):
        return {}
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        scheme, _, rest = target_url.partition("://")
        host = rest.split("/", 1)[0]
        audience = f"{scheme}://{host}"
        token = id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
        return {"X-Internal-Service-Token": token}
    except Exception:
        logger.warning(
            "Could not fetch a Google ID token for %s — calling without a service token.",
            target_url,
            exc_info=True,
        )
        return {}
