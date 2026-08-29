"""Embedding generation for the knowledge base and escalation questions.

Uses Gemini embeddings (gemini-embedding-001) via the google-genai SDK,
requested at settings.embedding_dim so it matches the fixed-width vector
columns in app.models. The client is created lazily so importing this
module doesn't fail before credentials are set.

Two auth paths, chosen automatically by whether settings.google_cloud_project
is set:
- Set (production/GCP): Vertex AI, authenticated via the environment's
  Application Default Credentials (a service account's identity on Cloud
  Run — no API key ever touches this process or its config).
- Unset (local dev): direct Gemini API via settings.gemini_api_key, per
  https://adk.dev/agents/models/google-gemini/ — matches how the ADK
  agent itself picks direct-API vs. Vertex.

Applies services/curl_transport.py at import time — a no-op outside
Windows (see that module's docstring); on this specific local machine it
routes around a network/EDR issue that has no equivalent on GCP.
"""

from functools import lru_cache

from google import genai
from google.genai import types

from app.config import settings
from app.services import curl_transport

curl_transport.apply()


class EmbeddingConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    if settings.google_cloud_project:
        return genai.Client(
            vertexai=True,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
    if not settings.gemini_api_key:
        raise EmbeddingConfigError(
            "Neither GOOGLE_CLOUD_PROJECT (Vertex AI) nor GEMINI_API_KEY (direct API) is set. "
            "Set one before using the knowledge base or escalation embedding features."
        )
    return genai.Client(api_key=settings.gemini_api_key)


def embed_text(text: str) -> list[float]:
    """Return a settings.embedding_dim-length embedding vector for `text`."""
    result = _client().models.embed_content(
        model=settings.embedding_model,
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=settings.embedding_dim),
    )
    return list(result.embeddings[0].values)
