"""In-process dedup for Bot Framework activity redelivery.

Azure Bot Service retries the same activity automatically when it doesn't
get a fast enough reply — documented behavior, and something this project
hit for real twice: once as the root cause of the SSLEOFError concurrency
bug (2026-09-06), and again on 2026-09-12 when a slow Graph token fetch
left a request hanging and Bot Service retried the same message several
times while the original was still stuck. Without this, a retried
delivery re-runs the entire pipeline from scratch: a duplicate agent call,
a duplicate conversation_turns row quietly skewing future summaries, and
potentially two separate replies landing in the same Teams thread for one
message.

Three states per activity_id, not just a plain seen-set, because a naive
"seen once, always drop" rule would make the 2026-09-12 hang *worse*
instead of better — if the original attempt genuinely crashes outright
(rather than merely being slow), every subsequent retry must still be
allowed to actually try again, or the user gets silence forever:

- no entry: never seen — go ahead, and the caller must report the outcome
  (mark_done or mark_failed) when finished.
- "in_progress", recent: another attempt for this same activity is
  currently running elsewhere — drop this one; the original will reply.
- "in_progress", older than _IN_PROGRESS_STALE_SECONDS: the original
  attempt has been "in flight" far longer than any real request should
  take — likely hung or the process that owned it died without ever
  calling mark_done/mark_failed. Treat as abandoned and let this retry
  proceed fresh, self-healing exactly the 2026-09-12 scenario.
- "done", within _DONE_TTL_SECONDS: already completed and replied to once
  — a late duplicate delivery, drop it.

Plain in-process dict, not a DB table — same "acceptable for this app's
single-instance shape" posture already established in services/rate_limit.py
(resets on restart, doesn't coordinate across multiple instances). Guarded
by a real threading.Lock, not just care around ordering: routers/teams.py
dispatches the actual work via asyncio.to_thread, meaning concurrent Teams
messages run in genuinely separate OS threads, not just interleaved
coroutines on one loop — an unguarded dict mutated from multiple real
threads at once (e.g. a Bot Service retry storm) can corrupt or crash, not
just race logically.
"""

import threading
import time

_DONE_TTL_SECONDS = 600.0
_IN_PROGRESS_STALE_SECONDS = 90.0

_lock = threading.Lock()
_state: dict[str, tuple[str, float]] = {}  # activity_id -> (status, monotonic_timestamp)


def should_process(activity_id: str) -> bool:
    """Call once, before doing any real work for this activity_id. Returns
    True if this call should actually do the work — the caller must then
    call mark_done or mark_failed when finished. Returns False if this is
    a duplicate delivery that should be dropped (ack cleanly, no
    reprocessing, no reply)."""
    now = time.monotonic()
    with _lock:
        _prune(now)
        entry = _state.get(activity_id)
        if entry is not None:
            status, ts = entry
            if status == "done":
                return False
            if now - ts < _IN_PROGRESS_STALE_SECONDS:
                return False
            # else: stale "in_progress" — treat as abandoned, fall through to claim it fresh
        _state[activity_id] = ("in_progress", now)
        return True


def mark_done(activity_id: str) -> None:
    with _lock:
        _state[activity_id] = ("done", time.monotonic())


def mark_failed(activity_id: str) -> None:
    """Processing genuinely errored out (not just slow) — remove the
    entry entirely so the next retry is treated as fresh, not dropped as a
    duplicate of a failed attempt nobody will ever retry otherwise."""
    with _lock:
        _state.pop(activity_id, None)


def _prune(now: float) -> None:
    # Called with _lock already held. Only "done" entries need TTL-based
    # cleanup; a stale "in_progress" entry is naturally replaced the next
    # time should_process sees it (no separate sweep needed for those).
    expired = [k for k, (status, ts) in _state.items() if status == "done" and now - ts > _DONE_TTL_SECONDS]
    for k in expired:
        del _state[k]
