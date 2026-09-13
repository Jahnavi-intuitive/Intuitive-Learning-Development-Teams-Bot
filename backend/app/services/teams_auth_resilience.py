"""Two fixes for botframework-connector's OpenID-metadata/JWKS handling,
both confirmed against real failures, not hypothetical:

1. **Retry wrapper around `_OpenIdMetadata._refresh`** — its two plain
   synchronous `requests.get()` calls to Microsoft's Bot Framework
   endpoints are genuinely intermittently flaky on this machine: an
   isolated single call sometimes fails with an empty/non-JSON response
   (`JSONDecodeError: Expecting value`), the same symptom first seen
   under 10 concurrent calls, but reproduced here on just one solitary
   request too, so it isn't purely a concurrency artifact. Same kind of
   network-path unreliability `services/curl_transport.py` already works
   around for httpx (a different library) — a plain retry-with-backoff
   already proved sufficient in direct testing (every retried attempt
   succeeded), so that's the fix here too, scoped to just this call.

2. **`_OpenIdMetadata._find` crashes instead of signaling a cache miss.**
   `[x for x in self.keys if x["kid"] == key_id][0]` raises `IndexError`
   when no cached key matches — but its only caller (`get()`) checks
   `if not key:` to decide whether to refresh the cache and retry, i.e.
   `_find` returning `None` on a miss is clearly the intended contract.
   Because it raises instead, that whole refresh-and-retry fallback path
   never actually runs: any real token signed with a key not in the
   current cache (Azure's signing keys rotate; our own cache also only
   refreshes once a day, or once an hour after a miss) fails outright as
   `"list index out of range"` instead of triggering a fresh fetch.
   Confirmed live: a real Bot Service message got exactly this error and
   a 401, while the local warm-up minutes earlier had fetched a real,
   large JWKS set — the specific key just wasn't in it yet. Patched to
   catch the IndexError and return None, letting the SDK's own
   already-written fallback logic actually do its job.

Both applied once at import time (see routers/teams.py), same pattern as
curl_transport.apply().
"""

import asyncio
import functools
import logging

logger = logging.getLogger(__name__)

_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.5
_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    _applied = True

    from botframework.connector.auth.jwt_token_extractor import _OpenIdMetadata

    original_refresh = _OpenIdMetadata._refresh
    original_find = _OpenIdMetadata._find

    @functools.wraps(original_refresh)
    async def _refresh_with_retry(self):
        last_exc: Exception | None = None
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                return await original_refresh(self)
            except Exception as exc:  # noqa: BLE001 — deliberately broad, see module docstring
                last_exc = exc
                if attempt < _ATTEMPTS:
                    logger.warning(
                        "Teams OpenID metadata fetch failed (attempt %d/%d), retrying: %s",
                        attempt,
                        _ATTEMPTS,
                        exc,
                    )
                    await asyncio.sleep(_BACKOFF_SECONDS)
        raise last_exc

    @functools.wraps(original_find)
    def _find_or_none(self, key_id: str):
        try:
            return original_find(self, key_id)
        except IndexError:
            return None

    _OpenIdMetadata._refresh = _refresh_with_retry
    _OpenIdMetadata._find = _find_or_none
