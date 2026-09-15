"""Structured per-call log of every real LLM API call this backend makes
or triggers (Gemini generation calls via the agent's /run response, and
this backend's own direct embedding calls).

Written to two places on every call:
- backend/logs/llm_calls.jsonl — one JSON line per call, for local
  tail/grep convenience.
- Postgres (models.py::LlmCallLog) — the actual durable record. The file
  alone isn't enough: Render's free web services have no persistent disk
  (see RENDER_DEPLOY.md), so it's wiped on every restart/redeploy/idle
  spin-down there. Postgres is the one store in this project that's
  actually durable everywhere it runs.

Uses its own short-lived SessionLocal() rather than accepting a `db`
parameter from callers — logging a call must never depend on, or share a
transaction with, the request the call itself is part of (agent_proxy.py,
embeddings.py, and teams.py each call this from different points in their
own request lifecycles).

Never raises: a failure to write this log (file or DB) must never break
the actual LLM call it's describing.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "llm_calls.jsonl"
_lock = threading.Lock()


def current_env() -> str:
    # RENDER is set automatically on every Render service (free or paid);
    # nothing equivalent exists locally, so its absence means local dev.
    return "production" if os.environ.get("RENDER") else "local"


def log_llm_call(
    *,
    agent_id: str,
    request_id: str,
    provider: str,
    model: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    latency_ms: float | None = None,
    status: str,
    error_type: str | None = None,
    endpoint: str,
    env: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    record = {
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "agent_id": agent_id,
        "request_id": request_id,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "status": status,
        "error_type": error_type,
        "metadata": {"endpoint": endpoint, "env": env if env is not None else current_env()},
    }
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock, _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        logger.warning("Could not write to the LLM call log file at %s", _LOG_PATH, exc_info=True)

    try:
        # Imported here, not at module level, purely to keep this module
        # importable (and the file-logging half working) even if app.database
        # ever fails to construct its engine — a DB problem should never
        # take down the one part of this logger that doesn't need Postgres.
        from app.database import SessionLocal
        from app.models import LlmCallLog

        db = SessionLocal()
        try:
            db.add(
                LlmCallLog(
                    call_timestamp=datetime.fromisoformat(record["timestamp"]),
                    agent_id=agent_id,
                    request_id=request_id,
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                    status=status,
                    error_type=error_type,
                    endpoint=endpoint,
                    env=record["metadata"]["env"],
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning("Could not write the LLM call log to Postgres", exc_info=True)
