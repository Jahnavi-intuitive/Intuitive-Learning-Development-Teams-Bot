"""Global monkeypatch: route `requests` traffic through a curl.exe subprocess.

Same root cause and rationale as agent/adk_agent/curl_transport.py — on this
machine, Python's OpenSSL-backed TLS (used by `requests`/urllib3) gets its
connections to generativelanguage.googleapis.com (and other hosts —
api.github.com, www.google.com) actively terminated mid-handshake, while
curl.exe (Windows' native Schannel TLS) reaches the same hosts fine. See
DEVELOPMENT_LOG.md for the full diagnostic trail.

The backend's installed google-genai version uses `requests.Session`
(unlike the newer version pulled in by google-adk, which uses httpx — see
the agent-side twin of this module) for its embedding calls. Patching
`requests.adapters.HTTPAdapter.send` at the class level means every
`requests.Session` created afterwards — including the one google-genai
builds internally in services/embeddings.py — sends through curl instead
of urllib3/OpenSSL.

Known limitation: buffers the full response before returning — no
incremental streaming. Irrelevant for the embedding calls this backend
makes (single JSON request/response, no streaming).

Windows-only, deliberately: this shells out to `curl.exe`, a binary that
doesn't exist by that name in a Linux container (Cloud Run, or any other
GCP deployment target). `apply()` is a no-op everywhere except Windows —
production never needs this workaround in the first place, since it
exists for one specific machine's local network/EDR behavior, not
anything inherent to calling Gemini's API.
"""

import platform

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from app.services import curl_http

_TIMEOUT_DEFAULT = 60


def _curl_send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
    body = request.body
    if isinstance(body, str):
        body = body.encode("utf-8")

    status_code, headers, content = curl_http.run_curl(
        request.method or "GET",
        request.url,
        request.headers.items(),
        body,
        timeout or _TIMEOUT_DEFAULT,
    )

    response = requests.Response()
    response.status_code = status_code
    response.headers = CaseInsensitiveDict(headers)
    response._content = content
    response.url = request.url
    response.request = request
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


def apply() -> None:
    if platform.system() != "Windows":
        return
    if getattr(HTTPAdapter, "_curl_patched", False):
        return
    HTTPAdapter.send = _curl_send
    HTTPAdapter._curl_patched = True
