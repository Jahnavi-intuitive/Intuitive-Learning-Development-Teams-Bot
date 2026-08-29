"""Generate the local certification tracker workbook: one sheet per
certification (from scripts/seed_data/*.json plus the two real Puja
Rachchh submissions), header row only — per the BRD's Excel tracker spec,
exact columns:
  Name, Manager, Practice Director, Prep Start Date, End Date/Exam Date,
  Cert Name, Status, Credly Link, Practice, Expiration Date

No fabricated data rows are written — employees fill rows in over time (or
they're created via the submission form / API, which also writes straight
to the tracker DB directly, bypassing this file). The hidden sheet-GUID
(Z1) and per-row Record ID (column L) that services/excel_sync.py relies
on are populated lazily by the sync itself the first time it sees each
sheet/row, not here.

Run once, before the first sync: `python scripts/generate_tracker_template.py`
"""

import json
import os
import re
from pathlib import Path

import openpyxl

SEED_DATA_DIR = Path(__file__).parent / "seed_data"
OUTPUT_PATH = Path(
    os.environ.get(
        "TRACKER_XLSX_PATH", Path(__file__).parent.parent / "data" / "trackers" / "certification_tracker.xlsx"
    )
)

HEADERS = [
    "Name",
    "Manager",
    "Practice Director",
    "Prep Start Date",
    "End Date/Exam Date",
    "Cert Name",
    "Status",
    "Credly Link",
    "Practice",
    "Expiration Date",
]

# The two real submissions seeded directly via the API (scripts/seed_real_submissions.py)
# still get a tracker sheet, for consistency — a future manual entry for someone
# else earning the same cert has somewhere to go.
EXTRA_CERT_NAMES = [
    "Certified Partner Specialist Gemini Enterprise Agent Development",
    "Certified Partner Specialist Gemini Enterprise Deployment",
]

_INVALID_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")


def sanitize_sheet_title(name: str, used: set[str]) -> str:
    title = _INVALID_SHEET_CHARS.sub("-", name).strip()[:31]
    if title not in used:
        used.add(title)
        return title
    # Truncated collision — append a short numeric suffix, still <= 31 chars.
    for i in range(2, 100):
        suffix = f" ({i})"
        candidate = title[: 31 - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError(f"Could not generate a unique sheet title for '{name}'")


def collect_cert_names() -> list[str]:
    names = []
    for f in sorted(SEED_DATA_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        names.extend(c["name"] for c in data["certifications"])
    names.extend(EXTRA_CERT_NAMES)
    # de-dupe, preserve order
    seen = set()
    ordered = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def main():
    cert_names = collect_cert_names()
    if not cert_names:
        raise SystemExit(f"No certifications found in {SEED_DATA_DIR} — nothing to generate.")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    used_titles: set[str] = set()
    for name in cert_names:
        title = sanitize_sheet_title(name, used_titles)
        ws = wb.create_sheet(title=title)
        ws.append(HEADERS)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT_PATH)
    print(f"Wrote {len(cert_names)} sheets to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
