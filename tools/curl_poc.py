"""One-off diagnostic: call Gemini's embedContent REST endpoint via a curl
subprocess instead of Python's requests/OpenSSL, to see if curl's (Schannel-
backed) TLS stack gets past whatever is blocking Python's. Reads the API
key from backend/.env so it's never typed into a command or printed."""

import json
import subprocess
from pathlib import Path

env_path = Path(__file__).parent.parent / "backend" / ".env"
api_key = None
for line in env_path.read_text().splitlines():
    if line.startswith("GEMINI_API_KEY="):
        api_key = line.split("=", 1)[1].strip()
        break

if not api_key:
    raise SystemExit("GEMINI_API_KEY not found in backend/.env")

url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={api_key}"
body = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": "hello world"}]}}

proc = subprocess.run(
    ["curl", "-s", "-X", "POST", url, "-H", "Content-Type: application/json",
     "-d", json.dumps(body), "-w", "\nHTTP_STATUS:%{http_code}"],
    capture_output=True, text=True, timeout=30,
)
# Never print the URL (it contains the key) — just the outcome.
redacted_stdout = proc.stdout.replace(api_key, "[REDACTED]")
print("returncode:", proc.returncode)
print("stderr:", proc.stderr[:300].replace(api_key, "[REDACTED]"))
print("stdout (first 800 chars):", redacted_stdout[:800])
