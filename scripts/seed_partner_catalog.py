"""Seed/refresh lnd_portal.partner_certifications — the partner certificate
list the next-certification recommender chooses from — from
scripts/seed_data/partner_catalog.json. Idempotent: upserts on
(provider, certification_name); never deletes rows an admin added.

This seeds the *catalog only* (what certifications exist). It writes no
tracker/certification_records data and no partnership requirements —
those come from the real partner portal sync and from L&D (POST
/api/admin/partnership-requirements), respectively.

Usage (from backend/, after `python scripts_create_portal_tables.py`):
  .venv/Scripts/python.exe ../scripts/seed_partner_catalog.py [--dry-run]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.portal_db import PortalSessionLocal  # noqa: E402
from app.portal_models import PartnerCertification, Provider  # noqa: E402

SEED = Path(__file__).parent / "seed_data" / "partner_catalog.json"


def main(dry_run: bool) -> None:
    entries = json.loads(SEED.read_text(encoding="utf-8"))["certifications"]
    db = PortalSessionLocal()
    inserted = updated = 0
    try:
        for e in entries:
            provider = Provider(e["provider"])
            row = db.scalar(select(PartnerCertification).where(
                PartnerCertification.provider == provider,
                PartnerCertification.certification_name == e["certification_name"],
            ))
            fields = {k: v for k, v in e.items() if k not in ("provider", "certification_name")}
            if row is None:
                db.add(PartnerCertification(provider=provider, certification_name=e["certification_name"],
                                            updated_by="seed_partner_catalog", **fields))
                inserted += 1
            else:
                for k, v in fields.items():
                    setattr(row, k, v)
                row.updated_by = "seed_partner_catalog"
                updated += 1
        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()
    print(f"{'[dry-run] ' if dry_run else ''}partner_certifications: {inserted} inserted, {updated} updated")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
