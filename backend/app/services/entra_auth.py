"""Real Microsoft Entra ID (Azure AD) JWT validation — the production
replacement for the X-Employee-Id dev-auth stand-in in
services/access_scope.py.

Only used when settings.auth_mode == "entra" (see app/config.py and the
startup guard in app/main.py, which refuses to start in that mode unless
entra_tenant_id and entra_audience are both set — no silent partial
config). In that mode, access_scope.get_current_employee validates a real
`Authorization: Bearer <token>` header via `validate_bearer_token` below
and does NOT consult X-Employee-Id at all — there is no bypass path.

What this checks, on every request, before trusting anything in the token:
- Signature, against Microsoft's own published signing keys (JWKS),
  fetched over HTTPS and cached in-process.
- `iss` (issuer) — must be this exact tenant's v2.0 issuer, not just "any
  Microsoft-signed token" (which would let a token from a different Entra
  tenant/org through).
- `aud` (audience) — must be this app's own registration, not a token
  meant for some other API.
- `exp`/`iat` — expiry is enforced by pyjwt automatically once required.

Identity is taken from the token's `preferred_username`/`upn`/`email`
claim, matched against employees.email (already unique) — not from
anything the caller can freely assert. App-role authorization (admin
access) comes from the token's `roles` claim, populated by Entra ID from
the app registration's declared app roles + the user's role assignment —
not from a client-supplied flag.
"""

from dataclasses import dataclass, field

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from app.config import settings

_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        jwks_uri = f"https://login.microsoftonline.com/{settings.entra_tenant_id}/discovery/v2.0/keys"
        # cache_keys: avoid refetching Microsoft's JWKS on every request;
        # PyJWKClient still refetches automatically if it sees an unknown
        # key id (e.g. after Microsoft rotates signing keys).
        _jwk_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)
    return _jwk_client


@dataclass
class EntraIdentity:
    email: str
    roles: list[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        # SEAM: match this to whatever app role name gets declared on the
        # backend's app registration (App roles blade) and assigned to
        # L&D admins in Entra ID — "LnD.Admin" is a placeholder pending
        # that real app registration existing.
        return "LnD.Admin" in self.roles


def validate_bearer_token(authorization_header: str | None) -> EntraIdentity:
    if not settings.entra_tenant_id or not settings.entra_audience:
        # Belt-and-suspenders: app/main.py's startup guard should already
        # have refused to boot in this state, but never trust a token if
        # this module somehow got called without real config.
        raise HTTPException(status_code=500, detail="Entra auth is not configured on this server.")

    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization_header[len("Bearer ") :].strip()

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=settings.entra_audience,
            issuer=f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    email = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
    if not email:
        raise HTTPException(status_code=401, detail="Token has no usable identity claim.")

    return EntraIdentity(email=email, roles=list(claims.get("roles") or []))


def validate_service_id_token(token: str) -> str:
    """Verifies a Google-signed ID token from one of this project's own
    Cloud Run services — in practice, the agent calling back into the
    backend to relay a tool call (agent/adk_agent/tools.py). Separate
    from validate_bearer_token above on purpose: this proves "this really
    is our own agent service," never a real employee's identity, which is
    why services/access_scope.py only accepts it paired with an explicit
    X-Employee-Id header naming who the call is on behalf of, and never
    lets it grant admin access (see that module).

    Fails closed: an empty trusted_service_accounts list (the default)
    means every token is rejected, not implicitly trusted.
    """
    trusted = {e.strip() for e in settings.trusted_service_accounts.split(",") if e.strip()}
    if not trusted:
        raise HTTPException(status_code=401, detail="No trusted service accounts configured.")

    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token as google_id_token

        claims = google_id_token.verify_oauth2_token(token, google.auth.transport.requests.Request())
    except Exception as exc:  # noqa: BLE001 - google-auth raises several distinct error types here
        raise HTTPException(status_code=401, detail=f"Invalid service token: {exc}") from exc

    email = claims.get("email")
    if not email or not claims.get("email_verified", True) or email not in trusted:
        raise HTTPException(status_code=401, detail="Service token is not on the trusted allow-list.")
    return email
