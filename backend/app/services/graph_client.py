"""App-only Microsoft Graph client — the authoritative directory lookup for
a Teams identity that has no `employees` row yet (see routers/teams.py).

Deliberately narrow: this is not a general Graph SDK wrapper, just the two
calls the auto-provisioning path needs (`GET /users/{id}` — displayName,
mail/userPrincipalName, jobTitle, department — and `GET /users/{id}/manager`),
using the client-credentials (app-only) OAuth2
flow — there is no signed-in user token available on this path, since the
Teams activity only carries an `aad_object_id`, not a token for that person.

Requires an Entra app registration with the Microsoft Graph application
permission `User.Read.All` (admin consented) — see
`teams-app/AZURE_BOT_SETUP.md`'s twin, `teams-app/GRAPH_APP_SETUP.md`, for
the exact portal steps. `settings.graph_client_id`/`graph_client_secret`
being unset is treated as a normal lookup failure (`GraphLookupError`), not
a startup-time hard refusal — only a *new* Teams user actually needs Graph;
an already-recognized employee's messages never touch this module at all.
"""

import logging
import time

import requests
from pydantic import BaseModel

from app.config import settings
from app.services import curl_transport

curl_transport.apply()

logger = logging.getLogger(__name__)

_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_REQUEST_TIMEOUT = 15.0
# Refresh a little before the token's real expiry so a call never starts
# with a token that dies mid-request.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60

_token_cache: dict[str, float | str | None] = {"value": None, "expires_at": 0.0}


class GraphLookupError(Exception):
    """Raised for any reason a Graph-backed employee lookup can't be
    trusted right now — missing config, a failed token fetch, a non-2xx
    response, a network error. Callers (routers/teams.py) must treat this
    as "cannot enrich this identity yet", never fall back to guessing data
    from the Teams payload instead."""


class GraphUser(BaseModel):
    display_name: str
    email: str | None
    job_title: str | None
    manager_name: str | None
    department: str | None


def get_app_token() -> str:
    """Public: also reused by services/sharepoint_client.py — same app
    registration, same client-credentials token, just requested with the
    `.default` scope, which automatically covers every application
    permission the app registration currently holds (User.Read.All AND
    Sites.Selected). One shared in-process cache is correct and more
    efficient than each module fetching its own."""
    cached_value = _token_cache["value"]
    if cached_value and time.monotonic() < _token_cache["expires_at"]:
        return cached_value  # type: ignore[return-value]

    if not settings.graph_client_id or not settings.graph_client_secret or not settings.teams_tenant_id:
        raise GraphLookupError(
            "Microsoft Graph is not configured (graph_client_id/graph_client_secret/teams_tenant_id)."
        )

    try:
        resp = requests.post(
            _TOKEN_URL_TMPL.format(tenant_id=settings.teams_tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "scope": _GRAPH_SCOPE,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise GraphLookupError(f"Could not obtain a Microsoft Graph app token: {exc}") from exc

    token = payload.get("access_token")
    expires_in = payload.get("expires_in", 0)
    if not token:
        raise GraphLookupError("Microsoft Graph token response had no access_token.")

    _token_cache["value"] = token
    _token_cache["expires_at"] = time.monotonic() + max(expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS, 0)
    return token


def _graph_get(path: str, token: str) -> requests.Response:
    return requests.get(
        f"{_GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_REQUEST_TIMEOUT,
    )


def get_user_with_manager(aad_object_id: str) -> GraphUser:
    """The one entry point routers/teams.py's auto-provisioning path calls.
    Raises GraphLookupError on any failure — never returns a partially
    filled result."""
    token = get_app_token()

    try:
        user_resp = _graph_get(
            f"/users/{aad_object_id}?$select=displayName,mail,userPrincipalName,jobTitle,department",
            token,
        )
    except requests.RequestException as exc:
        raise GraphLookupError(f"Graph user lookup failed for {aad_object_id}: {exc}") from exc

    if not user_resp.ok:
        raise GraphLookupError(
            f"Graph user lookup for {aad_object_id} returned {user_resp.status_code}: {user_resp.text[:300]}"
        )
    user_data = user_resp.json()

    display_name = user_data.get("displayName")
    if not display_name:
        raise GraphLookupError(f"Graph user lookup for {aad_object_id} had no displayName.")

    manager_name: str | None = None
    try:
        manager_resp = _graph_get(f"/users/{aad_object_id}/manager?$select=displayName", token)
        if manager_resp.ok:
            manager_name = manager_resp.json().get("displayName")
        elif manager_resp.status_code != 404:
            # 404 just means "no manager set" (e.g. a top-of-org account) —
            # a real, expected case, not a lookup failure. Anything else
            # (401/403/5xx) is worth logging but shouldn't block
            # provisioning on manager data alone.
            logger.warning(
                "Graph manager lookup for %s returned %s: %s",
                aad_object_id,
                manager_resp.status_code,
                manager_resp.text[:300],
            )
    except requests.RequestException as exc:
        logger.warning("Graph manager lookup failed for %s: %s", aad_object_id, exc)

    return GraphUser(
        display_name=display_name,
        email=user_data.get("mail") or user_data.get("userPrincipalName"),
        job_title=user_data.get("jobTitle"),
        manager_name=manager_name,
        department=user_data.get("department"),
    )
