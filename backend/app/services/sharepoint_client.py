"""Graph-based file I/O against the SharePoint "Certification Trackers"
document library — replaces local-disk access as the actual home for the
per-provider tracker workbooks (see services/excel_sync.py, which used to
read/write settings.tracker_xlsx_path directly on local disk).

Uses the same app-only Graph client credentials as services/graph_client.py
(same app registration, same client-credentials token — see that module's
get_app_token(), reused here rather than duplicated) but a different Graph
permission: `Sites.Selected`, granted to only this one SharePoint site via
a one-time site-specific permission grant (see DEVELOPMENT_LOG.md's
2026-09-13 entry for the exact steps that performed it) — not tenant-wide
SharePoint access.

Deliberately the simplest correct approach, not SharePoint's live cell-
range Excel API: download the whole file, edit in-process with openpyxl
exactly as before (see excel_sync.py), re-upload the whole file. This is a
periodic sync job triggered by an admin action, not real-time co-authoring,
so a full-file round trip per sync is the right level of complexity — and
it means excel_sync.py's carefully-designed openpyxl logic (hidden sheet-
id/record-id cells, header validation) needed zero changes, just a
different source/destination for the bytes.

Known limitation, accepted rather than solved here: no protection against
a genuine edit conflict if an admin has the same file open in Excel
Desktop at the exact moment a sync runs — a plain overwrite-on-upload, no
ETag/version check. Low real risk for how this is actually used (an
admin-triggered sync, not continuous), but worth knowing if that changes.
"""

import logging
import urllib.parse

import requests

from app.config import settings
from app.services import curl_transport
from app.services.graph_client import get_app_token

curl_transport.apply()

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_REQUEST_TIMEOUT = 60.0


class SharePointError(Exception):
    """Any failure talking to the Certification Trackers library — network,
    auth, a missing/misconfigured drive id. Callers should treat this like
    graph_client.GraphLookupError elsewhere in this project: fail closed,
    never silently proceed as if a file were empty or absent when the real
    problem is that it couldn't even be checked."""


def _require_config() -> None:
    if not settings.sharepoint_drive_id:
        raise SharePointError("SharePoint isn't configured yet (sharepoint_drive_id unset).")


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_app_token()}"}


def list_files() -> list[str]:
    """Every .xlsx file in the Certification Trackers library's root — no
    folder recursion, since this library is meant to stay flat: one file
    per provider, nothing nested underneath."""
    _require_config()
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/drives/{settings.sharepoint_drive_id}/root/children",
            headers=_auth_header(),
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SharePointError(f"Listing Certification Trackers failed: {exc}") from exc
    if not resp.ok:
        raise SharePointError(f"Listing Certification Trackers failed: {resp.status_code} {resp.text[:300]}")
    return [item["name"] for item in resp.json().get("value", []) if item.get("name", "").endswith(".xlsx")]


def download_file(filename: str) -> bytes | None:
    """Returns the file's raw bytes, or None if it doesn't exist yet — the
    normal case for a certification whose provider has no file yet;
    callers create a fresh workbook in that case rather than treating it
    as an error."""
    _require_config()
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/drives/{settings.sharepoint_drive_id}/root:/{filename}:/content",
            headers=_auth_header(),
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SharePointError(f"Downloading {filename!r} failed: {exc}") from exc
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise SharePointError(f"Downloading {filename!r} failed: {resp.status_code} {resp.text[:300]}")
    return resp.content


def _encoded_path(*segments: str) -> str:
    """Graph's `root:/{path}:/...` addressing takes a real path, but each
    segment (a folder or file name) still needs URL-encoding for spaces and
    the parens several of these real filenames contain — quote() each
    segment individually so a literal "/" in a name is never mistaken for a
    path separator."""
    return "/".join(urllib.parse.quote(s, safe="") for s in segments)


def list_files_in_folder(folder: str) -> list[str]:
    """Every .xlsx file directly inside `folder` (one level, no recursion)
    — used for the "REAL EXCEL SHEETS" subfolder holding the real
    Anthropic/Gemini/AWS/Databricks org data, a different part of this same
    library from the per-provider tracker exports at the root that
    list_files()/download_file()/upload_file() above manage."""
    _require_config()
    path = _encoded_path(folder)
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/drives/{settings.sharepoint_drive_id}/root:/{path}:/children",
            headers=_auth_header(),
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SharePointError(f"Listing folder {folder!r} failed: {exc}") from exc
    if not resp.ok:
        raise SharePointError(f"Listing folder {folder!r} failed: {resp.status_code} {resp.text[:300]}")
    return [item["name"] for item in resp.json().get("value", []) if item.get("name", "").endswith(".xlsx")]


def download_file_in_folder(folder: str, filename: str) -> bytes | None:
    """Same as download_file(), but for a file inside a subfolder rather
    than the library root."""
    _require_config()
    path = _encoded_path(folder, filename)
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/drives/{settings.sharepoint_drive_id}/root:/{path}:/content",
            headers=_auth_header(),
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SharePointError(f"Downloading {folder}/{filename} failed: {exc}") from exc
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise SharePointError(f"Downloading {folder}/{filename} failed: {resp.status_code} {resp.text[:300]}")
    return resp.content


def upload_file(filename: str, content: bytes) -> None:
    """Creates the file if it doesn't exist, replaces it if it does — one
    PUT either way (Graph's own upsert-by-path behavior for drive item
    content)."""
    _require_config()
    try:
        resp = requests.put(
            f"{_GRAPH_BASE}/drives/{settings.sharepoint_drive_id}/root:/{filename}:/content",
            headers={**_auth_header(), "Content-Type": _XLSX_CONTENT_TYPE},
            data=content,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SharePointError(f"Uploading {filename!r} failed: {exc}") from exc
    if not resp.ok:
        raise SharePointError(f"Uploading {filename!r} failed: {resp.status_code} {resp.text[:300]}")
    logger.info("Uploaded %s to the Certification Trackers library (%d bytes).", filename, len(content))
