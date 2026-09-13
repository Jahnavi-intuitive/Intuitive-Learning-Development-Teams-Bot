"""One-time backfill: assign short_name (services/short_name.py) to every
certification_catalog row that doesn't have one yet, then ensure every
row's SharePoint provider workbook + sheet exists
(services/excel_sync.py::ensure_certification_sheet_exists), via the
dedicated backend endpoint POST /api/admin/catalog-entries/{id}/ensure-
sharepoint-setup — never by upserting a partial payload through the
regular catalog-entries endpoint, which would silently reset every other
field on a real entry back to CatalogUpsertIn's schema defaults.

Confirmed before writing this (2026-09-13): the existing local
data/trackers/certification_tracker.xlsx has zero actual data rows across
all 50 of its sheets — every sheet is header-only. So there is no real
historical tracker data to migrate row-by-row here; this script only
backfills identity/structure (short_name + an empty SharePoint sheet per
certification), not any submission data. If that ever stops being true —
a future local tracker file genuinely has real rows in it — this script
would need a data-copying pass added, not just structure creation.

Idempotent: safe to re-run. Calls the same HTTP API every other script in
this directory uses (never a direct backend import), authenticating as a
real admin employee the same way.

Run with the backend up:
    python scripts/seed_sharepoint_provider_files.py
"""

import os
import sys

import httpx

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
ADMIN_EMPLOYEE_ID = os.environ.get("ADMIN_EMPLOYEE_ID")

if not ADMIN_EMPLOYEE_ID:
    sys.exit("Set ADMIN_EMPLOYEE_ID to an employee_id whose is_admin flag is true before running this.")

_HEADERS = {"X-Employee-Id": ADMIN_EMPLOYEE_ID}


def main() -> None:
    with httpx.Client(base_url=BACKEND_URL, timeout=60.0) as client:
        resp = client.get("/api/catalog/certifications", headers=_HEADERS)
        resp.raise_for_status()
        entries = resp.json()
        print(f"{len(entries)} certification_catalog rows total.")

        ok, failed, skipped_no_provider = 0, 0, 0
        for entry in entries:
            if not entry.get("provider"):
                skipped_no_provider += 1
                continue
            result = client.post(
                f"/api/admin/catalog-entries/{entry['id']}/ensure-sharepoint-setup",
                headers=_HEADERS,
            )
            if result.status_code == 200:
                ok += 1
            else:
                failed += 1
                print(f"  FAILED: {entry['name']!r} -> {result.status_code} {result.text[:200]}")

        print(f"Backfilled/confirmed {ok} entries; {failed} failed; {skipped_no_provider} skipped (no provider set).")


if __name__ == "__main__":
    main()
