"""Per-employee rate limiting — defense-in-depth against one misbehaving
client or a retry-loop bug, deliberately independent of whatever the
hosting platform's own edge protection provides (this is not a claim of
real DDoS protection, just cheap insurance at the application layer).

A plain in-process sliding window keyed by employee_id. Good enough for
this app's actual deployment shape (a single web-service instance on
Render's Free tier, or one local process) — it resets on every restart and
does not coordinate across multiple instances, which is an explicit,
acceptable limitation for what this is meant to catch, not a production
distributed-rate-limiter. If this service is ever run with more than one
instance behind a shared load balancer, this would need a shared backing
store (Postgres row or a Redis counter) instead — noted here rather than
silently outgrown.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException

from app.config import settings

_WINDOW_SECONDS = 60.0
_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(employee_id: str) -> None:
    """Raises HTTPException(429) if this employee has already made
    settings.rate_limit_per_minute or more calls in the trailing 60
    seconds. Call this once per agent-invoking request, right before the
    actual work starts."""
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return  # 0 or negative explicitly disables the limiter

    now = time.monotonic()
    window = _hits[employee_id]
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many requests — limit is {limit} per minute per employee. "
                "Please wait a moment and try again."
            ),
        )

    window.append(now)
