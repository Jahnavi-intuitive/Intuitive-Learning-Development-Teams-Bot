"""Seed certification_catalog, the certification prerequisite DAG,
learning_paths, and knowledge_base from the research files in
scripts/seed_data/*.json (one file per practice).

Two JSON shapes are supported, since not every seed_data file has been
migrated to the new one yet (2026-08-24 DAG restructure — see
DEVELOPMENT_LOG.md):
  - new: "prerequisite_names": [...] (a list; empty = none), plus optional
    "prerequisite_logic" ("any"/"all", default "any") and "track_label".
    Currently used by app_db_mod.json and data_ai.json.
  - old: "prerequisite_name": "..." or null (a single string). Currently
    still used by netsec_sdx.json and cloud_platform_engineer.json.
Both normalize to the same list-of-prerequisite-names shape internally.

Runs in two passes across ALL files before doing anything path-specific,
because certifications are shared across practices (e.g. "AWS Certified
Cloud Practitioner" appears in more than one seed_data file) and a
prerequisite referenced in one file's edges might only be *defined* as a
full entry in another file:
  1. Upsert every certification_catalog entry + its knowledge_base row,
     across all files.
  2. Create prerequisite edges and learning-path membership, across all
     files — safe now that every certification named anywhere exists.

Run with the backend up: `python scripts/seed_catalog_and_paths.py`

Every endpoint this script calls (catalog, prerequisites, learning paths,
KB items) is admin-gated as of the 2026-08-25 drives work (see
services/admin_auth.py) — this script now needs to authenticate as a real
admin employee via the same X-Employee-Id header everything else uses, not
a special seed-only bypass. Set ADMIN_EMPLOYEE_ID to an employee_id whose
is_admin flag is true.
"""

import json
import os
from pathlib import Path

import httpx

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
ADMIN_EMPLOYEE_ID = os.environ.get("ADMIN_EMPLOYEE_ID")
if not ADMIN_EMPLOYEE_ID:
    raise SystemExit(
        "Set ADMIN_EMPLOYEE_ID to an employee_id with is_admin=true before running this script — "
        "every endpoint it calls is admin-gated."
    )
_ADMIN_HEADERS = {"X-Employee-Id": ADMIN_EMPLOYEE_ID}
SEED_DATA_DIR = Path(__file__).parent / "seed_data"


def _prerequisite_names(cert: dict) -> list[str]:
    if "prerequisite_names" in cert:
        return cert["prerequisite_names"] or []
    return [cert["prerequisite_name"]] if cert.get("prerequisite_name") else []


def seed_catalog_and_kb(client: httpx.Client, data: dict) -> None:
    practice = data["practice"]
    certs = data["certifications"]
    print(f"\n=== {practice}: catalog + KB ({len(certs)} certs) ===")

    for cert in certs:
        prereq_names = _prerequisite_names(cert)
        resp = client.post(
            "/api/admin/catalog-entries",
            headers=_ADMIN_HEADERS,
            json={
                "name": cert["name"],
                "provider": cert.get("provider"),
                "level": cert.get("level"),
                "prerequisites": prereq_names,  # informational display list only — real edges below
                "prerequisite_logic": cert.get("prerequisite_logic", "any"),
                "validity_period_months": cert.get("validity_period_months"),
                "relevant_roles": cert.get("relevant_roles"),
                "status": "active",
            },
        )
        if resp.status_code >= 400:
            print(f"  FAILED catalog entry '{cert['name']}': {resp.status_code} {resp.text}")
            continue
        print(f"  catalog OK: {cert['name']}")

        kb_resp = client.post(
            "/api/kb/items",
            headers=_ADMIN_HEADERS,
            json={
                "content": cert["kb_content"],
                "certification_name": cert["name"],
                "tags": cert.get("relevant_roles"),
                "owner": "L&D Admin (seeded from research)",
                "status": "active",
            },
        )
        if kb_resp.status_code >= 400:
            print(f"  FAILED kb item for '{cert['name']}': {kb_resp.status_code} {kb_resp.text}")
        else:
            print(f"  kb OK: {cert['name']}")


def seed_edges_and_path(client: httpx.Client, data: dict) -> None:
    practice = data["practice"]
    certs = data["certifications"]
    print(f"\n=== {practice}: prerequisite edges + path membership ===")

    for cert in certs:
        for prereq_name in _prerequisite_names(cert):
            edge_resp = client.post(
                "/api/admin/certification-prerequisites",
                headers=_ADMIN_HEADERS,
                json={
                    "certification_name": cert["name"],
                    "prerequisite_certification_name": prereq_name,
                    "track_label": cert.get("track_label"),
                },
            )
            if edge_resp.status_code >= 400:
                print(f"  FAILED edge '{cert['name']}' <- '{prereq_name}': {edge_resp.status_code} {edge_resp.text}")
            else:
                print(f"  edge OK: {cert['name']} <- {prereq_name}")

    path_resp = client.post(
        "/api/admin/learning-paths",
        headers=_ADMIN_HEADERS,
        json={
            "practice": practice,
            "name": data.get("learning_path_name") or f"{practice} path",
            "description": data.get("notes"),
            "items": [{"certification_name": c["name"], "is_required": True} for c in certs],
        },
    )
    if path_resp.status_code >= 400:
        print(f"  FAILED learning path for '{practice}': {path_resp.status_code} {path_resp.text}")
    else:
        print(f"  learning path OK: {practice}")


def main():
    files = sorted(SEED_DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"No seed files found in {SEED_DATA_DIR}")

    datasets = [json.loads(f.read_text(encoding="utf-8")) for f in files]

    with httpx.Client(base_url=BACKEND_URL, timeout=30) as client:
        for data in datasets:
            seed_catalog_and_kb(client, data)
        for data in datasets:
            seed_edges_and_path(client, data)


if __name__ == "__main__":
    main()
