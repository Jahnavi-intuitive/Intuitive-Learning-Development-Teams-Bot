"""Seed the 5 test employees via the backend API.

Every row is written with is_test_data=True so scripts/delete_test_data.sql
can remove them all in one shot later. `role` is None for all 5 — the user
didn't have job titles on hand, and `role` isn't used by any access-scope
or matching logic (that's all manager_name/practice_director_name), so it
was made nullable rather than blocking seeding on a cosmetic field.

Run with the backend up (docker compose up, or `uvicorn app.main:app` from
backend/): `python scripts/seed_test_employees.py`
"""

import os

import httpx

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

# Real 5 test employees provided by the user (2026-08-24).
TEST_EMPLOYEES = [
    {
        "employee_id": "E1001",
        "name": "Jahnavi Patel",
        "email": "jahnavi.patel@intuitive.ai",
        "role": None,
        "manager_name": "Jayesh Purohit",
        "practice_director_name": "Prakash Patil",
        "practice": "App + DB Mod",
    },
    {
        "employee_id": "E1002",
        "name": "Puja Rachchh",
        "email": "puja.rachchh@intuitive.ai",
        "role": None,
        "manager_name": "Jayesh Purohit",
        "practice_director_name": "Peter Turner",
        "practice": "Data+AI",
    },
    {
        "employee_id": "E1003",
        "name": "Nancy Vaghela",
        "email": "nancy.vaghela@intuitive.ai",
        "role": None,
        "manager_name": "Jayesh Purohit",
        "practice_director_name": "Prakash Patil",
        "practice": "App + DB Mod",
    },
    {
        "employee_id": "E1004",
        "name": "Arya Kayastha",
        "email": "arya.kayastha@intuitive.ai",
        "role": None,
        "manager_name": "Jayesh Purohit",
        "practice_director_name": "Mukesh Kumar",
        "practice": "NetSec + SDx",
    },
    {
        "employee_id": "E1005",
        "name": "Priyash Balwani",
        "email": "priyansh.balwani@intuitive.ai",
        "role": None,
        "manager_name": "Shriram Mani",
        "practice_director_name": "Bhavesh Shah",
        "practice": "Cloud & Platform Engineer",
    },
]


def main():
    if not TEST_EMPLOYEES:
        raise SystemExit(
            "TEST_EMPLOYEES is empty — fill in the 5 real test employees in this "
            "file before running (see the TODO comment above)."
        )

    with httpx.Client(base_url=BACKEND_URL, timeout=10) as client:
        for emp in TEST_EMPLOYEES:
            resp = client.post("/api/admin/test-employees", json={**emp, "is_test_data": True})
            if resp.status_code >= 400:
                print(f"FAILED {emp['employee_id']}: {resp.status_code} {resp.text}")
            else:
                print(f"OK {emp['employee_id']}: {resp.json()}")


if __name__ == "__main__":
    main()
