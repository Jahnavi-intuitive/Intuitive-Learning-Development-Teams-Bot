"""Seed the two real, non-fabricated certification submissions the user
provided real Credly links for. Both badges were confirmed (via the fixed
credly.py parser) to belong to Puja Rachchh (E1002), issued by Google Cloud:
  - Certified Partner Specialist Gemini Enterprise Agent Development
  - Certified Partner Specialist Gemini Enterprise Deployment

These aren't in the Data+AI research seed (that set focused on the
Databricks/AWS/Claude/Gemini certs named in scope) — added to the catalog
here as "active" entries in their own right, since Puja genuinely holds
them, before submitting. Run after seed_test_employees.py and
seed_catalog_and_paths.py, with the backend up.
"""

import os

import httpx

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
ADMIN_EMPLOYEE_ID = os.environ.get("ADMIN_EMPLOYEE_ID")
if not ADMIN_EMPLOYEE_ID:
    raise SystemExit(
        "Set ADMIN_EMPLOYEE_ID to an employee_id with is_admin=true — /api/admin/catalog-entries "
        "is admin-gated as of the 2026-08-25 drives work."
    )
_ADMIN_HEADERS = {"X-Employee-Id": ADMIN_EMPLOYEE_ID}

REAL_CERTS = [
    {
        "name": "Certified Partner Specialist Gemini Enterprise Agent Development",
        "provider": "Google Cloud",
        "credly_link": "https://www.credly.com/badges/b4f74aad-0868-4060-b881-0fcb346a3677/public_url",
    },
    {
        "name": "Certified Partner Specialist Gemini Enterprise Deployment",
        "provider": "Google Cloud",
        "credly_link": "https://www.credly.com/badges/ef139173-6c58-4370-a83a-179ae3fcd4f8/public_url",
    },
]

EMPLOYEE_ID = "E1002"  # Puja Rachchh


def main():
    with httpx.Client(base_url=BACKEND_URL, timeout=30) as client:
        for cert in REAL_CERTS:
            cat_resp = client.post(
                "/api/admin/catalog-entries",
                headers=_ADMIN_HEADERS,
                json={"name": cert["name"], "provider": cert["provider"], "status": "active"},
            )
            print(f"catalog '{cert['name']}': {cat_resp.status_code}")

            sub_resp = client.post(
                "/api/certifications/submit",
                json={
                    "employee_id": EMPLOYEE_ID,
                    "cert_name": cert["name"],
                    "submission_status": "completed",
                    "credly_link": cert["credly_link"],
                },
            )
            if sub_resp.status_code >= 400:
                print(f"  FAILED submission: {sub_resp.status_code} {sub_resp.text}")
            else:
                body = sub_resp.json()
                verification = body.get("verification")
                print(
                    f"  submitted OK — verification_status="
                    f"{verification['status'] if verification else 'n/a'}"
                )


if __name__ == "__main__":
    main()
