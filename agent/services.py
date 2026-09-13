"""Registers adk_agent.conversation_memory_service.TeamsConversationMemoryService
as a custom ADK session-service backend, via the exact services.py plugin
mechanism documented in google.adk.cli.service_registry.

Must live here, directly under agent/ (== agents_dir, per Dockerfile's
`WORKDIR /app` + `CMD ["sh","-c","adk api_server ... ."]`, run against the
agent/ directory itself) — NOT inside agent/adk_agent/. ADK's
load_services_module(agents_dir) does `importlib.import_module("services")`
after putting agents_dir on sys.path, so this file has to sit at that top
level for ADK to find it at all; adk_agent/ is loaded as an app subfolder
underneath it, which is why the import below is absolute
(adk_agent.conversation_memory_service), not relative.

agent/Dockerfile's CMD passes --session_service_uri=teamsmemory://local by
default — every environment gets durable, cross-restart conversation
memory, including local dev (running the local test UI benefits from this
the same way Teams will: reopening the browser tab after a backend
restart keeps the conversation going). If the backend is briefly
unreachable, conversation_memory_service.py degrades gracefully (logs a
warning, continues with no prior history) rather than failing the
request, so there's no real downside to this being the default everywhere
rather than an opt-in flag.

The URI's own content is ignored — this service's real backing store is
the backend's Postgres database, reached over HTTP (see
conversation_memory_service.py), not a URI-addressable connection string
of its own. `teamsmemory://local` is just a scheme selector. Omitting
--session_service_uri entirely falls back to ADK's plain in-memory
default, if that's ever needed for a quick one-off test run."""

from google.adk.cli.service_registry import get_service_registry

from adk_agent.conversation_memory_service import TeamsConversationMemoryService


def _teams_memory_session_factory(uri: str, **kwargs):
    return TeamsConversationMemoryService()


get_service_registry().register_session_service("teamsmemory", _teams_memory_session_factory)
