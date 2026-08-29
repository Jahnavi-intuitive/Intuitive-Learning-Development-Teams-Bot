from . import curl_transport

# Must run before google-genai (pulled in by google-adk) opens any httpx
# client — see curl_transport.py for why. Applying it here, at package
# import time, guarantees it's in place before `agent.py` builds the Agent.
curl_transport.apply()

from . import agent  # noqa: F401,E402 - required so `adk api_server`/`adk web` can discover root_agent
