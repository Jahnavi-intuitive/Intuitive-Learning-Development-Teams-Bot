"""Shared curl-subprocess runner for services/curl_transport.py.

Kept separate from curl_transport.py only so the "run curl, parse the
response" logic isn't tangled up with the requests-specific monkeypatch
plumbing around it.
"""

import subprocess


def run_curl(
    method: str, url: str, headers, body: bytes | None, timeout: float
) -> tuple[int, list[tuple[str, str]], bytes]:
    cmd = ["curl", "-s", "-i", "-L", "--max-time", str(int(timeout)), "-X", method, url]
    for k, v in headers:
        # requests sets "Accept-Encoding: gzip, deflate" by default and
        # that header gets forwarded here verbatim — the server then
        # gzip-compresses the response, but plain curl (no --compressed
        # flag) never decompresses it, so the caller gets raw gzip bytes
        # back as if they were the plain response body. Confirmed
        # directly: login.botframework.com's OpenID metadata endpoint
        # compresses its response and this silently broke JSON parsing on
        # it — a real, previously-undiscovered bug, not a network issue.
        # Dropping the incoming header and forcing "identity" below means
        # the server never compresses in the first place, which is
        # simpler and more robust than teaching this parser to gunzip.
        if k.lower() == "accept-encoding":
            continue
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-H", "Accept-Encoding: identity"]
    if body:
        cmd += ["--data-binary", "@-"]

    proc = subprocess.run(cmd, input=body if body else None, capture_output=True, timeout=timeout + 15)
    return _parse_curl_output(proc.stdout)


def _parse_curl_output(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    header_blob, _, content = raw.partition(b"\r\n\r\n")
    # curl -i -L emits one header block per redirect hop; unwrap to the last one.
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
