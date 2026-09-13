# Agent (ADK `adk api_server`) — Cloud Run image.
#
# Build context is this agent/ directory (unlike the backend, the agent
# doesn't reach outside its own folder for anything):
#   docker build -f agent/Dockerfile -t lnd-agent agent/
#
# This service should be deployed with --no-allow-unauthenticated — only
# the backend calls it (agent_proxy.py), never a browser directly. Grant
# the backend's Cloud Run service account roles/run.invoker on this
# service so that call is itself authenticated (a Google-signed ID token
# the backend attaches, verified by Cloud Run's platform, not app code).

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY adk_agent ./adk_agent
COPY services.py .

RUN useradd --create-home --uid 1000 appuser
USER appuser

# Must run as the same user that starts the server below — adk's telemetry
# preference is stored per-user, and an unanswered "Enable telemetry?"
# prompt otherwise blocks api_server forever under a non-interactive
# container (same issue hit locally — see DEVELOPMENT_LOG.md).
RUN adk telemetry disable 2>/dev/null || true

ENV PORT=8080
EXPOSE 8080

# --session_service_uri=teamsmemory:// wires in the custom two-tier
# conversation-memory SessionService (services.py registers the
# "teamsmemory" scheme — see that file and
# adk_agent/conversation_memory_service.py) instead of ADK's plain
# in-memory default, so a conversation survives a container restart and a
# reopened Teams chat, not just a live process. Degrades gracefully (logs
# and continues with no history) if the backend is briefly unreachable —
# never blocks startup or crashes a request over it.
CMD ["sh", "-c", "adk api_server --host 0.0.0.0 --port ${PORT} --session_service_uri=teamsmemory://local ."]
