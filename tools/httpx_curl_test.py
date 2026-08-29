"""Standalone test of adk_agent/curl_transport.py against a real httpx.Client,
before trusting it inside the actual ADK agent."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))

from adk_agent import curl_transport  # noqa: E402

curl_transport.apply()

import httpx  # noqa: E402

env_path = Path(__file__).parent.parent / "backend" / ".env"
api_key = None
for line in env_path.read_text().splitlines():
    if line.startswith("GEMINI_API_KEY="):
        api_key = line.split("=", 1)[1].strip()
        break

url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
body = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": "hello world"}]}}

with httpx.Client() as client:
    resp = client.post(url, json=body, headers={"x-goog-api-key": api_key})
    print("status:", resp.status_code)
    data = resp.json()
    print("has embedding.values:", "embedding" in data and "values" in data.get("embedding", {}))
    if "embedding" in data:
        print("vector length:", len(data["embedding"]["values"]))
    else:
        print("response body:", str(data)[:500])
