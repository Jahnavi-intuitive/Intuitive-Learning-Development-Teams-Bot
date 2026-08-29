"""Attaches a Google-signed ID token for calls to another Cloud Run
service (this backend -> the agent's `adk api_server`, via agent_proxy.py).

Cloud Run's own platform verifies this token when the target service is
deployed with `--no-allow-unauthenticated` — no application code on the
receiving end has to check anything; this module only covers the caller
side. Silently a no-op for a localhost/127.0.0.1 target (local dev) and
falls back to an unauthenticated call if token fetch fails for any other
reason (e.g. not actually running on GCP) — logged, not raised, since a
broken internal call is a 502 either way and we'd rather see the real
downstream error than one from this shim.
"""

import logging

logger = logging.getLogger(__name__)


def fetch_auth_header(target_url: str) -> dict[str, str]:
    if "localhost" in target_url or "127.0.0.1" in target_url:
        return {}
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        scheme, _, rest = target_url.partition("://")
        host = rest.split("/", 1)[0]
        audience = f"{scheme}://{host}"
        token = id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
        return {"Authorization": f"Bearer {token}"}
    except Exception:
        logger.warning(
            "Could not fetch a Google ID token for %s — calling unauthenticated.", target_url, exc_info=True
        )
        return {}
