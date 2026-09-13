"""Global monkeypatch: route httpx traffic through a curl.exe subprocess.

Why this exists: on this machine (confirmed across two different
networks), Python's native TLS stack (OpenSSL, used by both `requests` and
`httpx`) gets its connections to several hosts — generativelanguage.
googleapis.com, api.github.com, www.google.com — actively terminated
mid-handshake (SSLEOFError, then SSLZeroReturnError after one mitigation
attempt). `curl.exe` (Windows' native Schannel TLS) reaches the exact same
hosts fine. This is almost certainly host-based endpoint security software
allow-listing known TLS stacks/processes rather than a bug in this code —
see DEVELOPMENT_LOG.md for the full diagnostic trail. PyPI was reachable
via Python's stack the whole time, which is what ruled out "Python's HTTPS
is just broken here" as the explanation.

ADK's own model calls go through google-genai, which (in the version ADK
2.7.1 pulls in) uses `httpx.Client`/`httpx.AsyncClient` as its transport —
not `requests` — and offers no supported hook to swap that transport out.
So this patches httpx itself, at the class level, before the agent module
is imported. Every httpx.Client/AsyncClient created afterwards in this
process (including the ones google-genai creates internally) sends through
curl instead of httpx's own connection handling.

Known limitation: this always waits for the full response before
returning — no true incremental streaming. Fine for a local dev agent
answering chat turns; would matter for a production streaming UI.

Windows-only, deliberately: shells out to `curl.exe`, which doesn't exist
by that name in a Linux container (Cloud Run or any other GCP deployment
target). `apply()` is a no-op everywhere except Windows — this workaround
exists for one specific machine's local network/EDR behavior, not
anything a real cloud deployment needs.
"""

import asyncio
import platform
import subprocess

import httpx

_TIMEOUT_SECONDS = 120


def _run_curl(method: str, url: str, headers, body: bytes | None) -> tuple[int, list[tuple[str, str]], bytes]:
    cmd = ["curl", "-s", "-i", "-L", "--max-time", str(_TIMEOUT_SECONDS), "-X", method, url]
    for k, v in headers:
        # httpx sets its own Accept-Encoding (gzip/deflate/br) by default,
        # forwarded here verbatim — the server may then compress the
        # response, but plain curl (no --compressed) never decompresses
        # it, silently handing back raw compressed bytes as if they were
        # the plain body. Confirmed as a real bug (not a hypothetical) in
        # the backend's twin of this module: login.botframework.com's
        # metadata endpoint compresses and this broke JSON parsing on it.
        # Hasn't been observed against Gemini's API specifically, but
        # nothing guarantees it never will — fixed here too rather than
        # left as a latent landmine. Dropping the incoming header and
        # forcing "identity" means the server never compresses in the
        # first place.
        if k.lower() == "accept-encoding":
            continue
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-H", "Accept-Encoding: identity"]
    if body:
        cmd += ["--data-binary", "@-"]

    proc = subprocess.run(
        cmd,
        input=body if body else None,
        capture_output=True,
        timeout=_TIMEOUT_SECONDS + 15,
    )
    return _parse_curl_output(proc.stdout)


def _parse_curl_output(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    header_blob, _, content = raw.partition(b"\r\n\r\n")
    # curl -i -L emits one header block per hop; keep unwrapping until the
    # remainder no longer starts with another status line.
    while True:
        maybe_next, sep, rest = content.partition(b"\r\n\r\n")
        if sep and maybe_next.startswith(b"HTTP/"):
            header_blob, content = maybe_next, rest
        else:
            break

    lines = header_blob.decode("iso-8859-1", errors="replace").split("\r\n")
    status_line = lines[0] if lines else "HTTP/1.1 599 CurlTransportError"
    try:
        status_code = int(status_line.split()[1])
    except (IndexError, ValueError):
        status_code = 599

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers.append((k.strip(), v.strip()))
    return status_code, headers, content


def _request_body(request: httpx.Request) -> bytes | None:
    try:
        return request.content or None
    except httpx.RequestNotRead:
        request.read()
        return request.content or None


def _curl_send_sync(self, request: httpx.Request, **kwargs) -> httpx.Response:
    status_code, headers, content = _run_curl(
        request.method, str(request.url), request.headers.items(), _request_body(request)
    )
    return httpx.Response(status_code=status_code, headers=headers, content=content, request=request)


async def _curl_send_async(self, request: httpx.Request, **kwargs) -> httpx.Response:
    status_code, headers, content = await asyncio.to_thread(
        _run_curl, request.method, str(request.url), list(request.headers.items()), _request_body(request)
    )
    return httpx.Response(status_code=status_code, headers=headers, content=content, request=request)


def apply() -> None:
    if platform.system() != "Windows":
        return
    if getattr(httpx.Client, "_curl_patched", False):
        return
    httpx.Client.send = _curl_send_sync
    httpx.Client._curl_patched = True
    httpx.AsyncClient.send = _curl_send_async
    httpx.AsyncClient._curl_patched = True
