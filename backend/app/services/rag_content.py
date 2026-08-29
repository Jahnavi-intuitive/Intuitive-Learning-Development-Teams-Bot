"""Selective re-embedding — the one shared function every RAG-content write
site calls (admin knowledge-base create/edit, the escalation-promotion-to-
KB flow). Never call embed_text() directly from a router for content that
already has a stored content_hash; call embed_if_changed() instead.

The rule: compute a SHA-256 hash over the exact text that gets embedded,
compare it to the hash stored alongside the row's vector, and only spend an
embedding-model call if the hash changed. A save that only touches owner,
status, or last_reviewed_date — fields that don't feed the embedding —
must cost zero embedding calls, because the hash of the embedded text
itself didn't change. This runs synchronously in the same request that
saves the edit, not as a batch job, so the vector store is never stale.
"""

import hashlib

from app.services.embeddings import embed_text


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_if_changed(previous_hash: str | None, text: str) -> tuple[str, list[float] | None]:
    """Returns (new_hash, embedding). `embedding` is None when new_hash ==
    previous_hash — the caller must leave the stored embedding column
    untouched in that case (don't overwrite it with None), and should only
    write the (unchanged) hash back if it wasn't already stored."""
    new_hash = sha256_hex(text)
    if new_hash == previous_hash:
        return new_hash, None
    return new_hash, embed_text(text)
