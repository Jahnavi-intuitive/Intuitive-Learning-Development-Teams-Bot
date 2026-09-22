"""ONE-TIME, DEMO-ONLY historical backfill for the Trends tab
(frontend/portal.html). Writes exclusively to lnd_portal.demo_trend_backfill
(backend/app/portal_models.py::DemoTrendBackfill) — never to
certification_records, never run as part of scripts/ingest_real_excel_data.py,
and never read by the Dashboard tab, the agent, or any compliance/audit
reporting. routers/portal_trends.py (via app/services/trend_series.py) is
the only real reader, and it always prefers a real computed value over a
seeded one for any period where real data exists — a stale seeded row left
behind by this script is simply never read once real data catches up.

WHY THIS EXISTS, CONCRETELY (checked against the real data, not assumed):
Anthropic and Databricks already have 16-27 months of real dated completion
history (completion/submission/exam dates on individual records) and need
almost no backfill in the standard chart window. AWS and Gemini have ZERO
dated completion records at all today — neither source workbook ever
captured a real date for a completed cert — so every period before "today"
for those two providers is synthetic; only the final (current) point is
ever real.

HOW THE CURVE IS BUILT (see app/services/trend_series.py + generate_organic_curve
below): the real current total (dated + undated completions) is read first
and is NEVER altered — every generated curve ends exactly there. Real dated
history is used wherever it exists; a period is only backfilled if no real
record could plausibly place someone there. The synthetic prefix is a
monotonic, non-decreasing curve from a small random starting fraction of the
end value up to that value, with randomized per-step jitter around the
smoothed average step (not a straight line), seeded per
(provider, certification_name, granularity) via a stable hash — so
re-running this script reproduces the same numbers, but every series gets
its own distinct growth shape.

Backfills every tracked certification with at least one real completion
(not just each provider's single leading certification) plus one
whole-provider series per provider — the Trends tab's "Leaders" ranking
needs a growth figure for every certification it lists, not just the 8
comparison-chart lines. A provider with zero real completions
(no certification and no whole-provider series) gets no backfill at all —
no fabricated growth for a provider nobody's actually certified in yet.

Run from the repo root, with the lnd_portal Postgres server reachable:
    python scripts/seed_demo_trend_history.py
"""

import random
import sys
import zlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.portal_db import PortalBase, PortalSessionLocal, portal_engine  # noqa: E402
from app.portal_models import DemoTrendBackfill, Provider, TrendGranularity  # noqa: E402
from app.services.trend_series import (  # noqa: E402
    fetch_person_dates,
    monthly_period_starts,
    tracked_certifications,
    weekly_period_starts,
)

TODAY = date.today()


def _stable_seed(key: str) -> int:
    return zlib.crc32(key.encode())


def generate_organic_curve(target_end_value: int, num_points: int, seed_key: str) -> list[int]:
    """Monotonic, non-decreasing, organically-jittered curve of `num_points`
    integers ending exactly at `target_end_value`. Deterministic per
    seed_key (stable hash, not Python's randomized hash()) so re-running
    this script is reproducible, but distinct series never look identical."""
    rng = random.Random(_stable_seed(seed_key))
    if num_points <= 0:
        return []
    if num_points == 1 or target_end_value <= 0:
        return [target_end_value] * num_points

    start_frac = rng.uniform(0.04, 0.22)
    values = [min(target_end_value, max(0, round(target_end_value * start_frac)))]
    steps = num_points - 1
    for i in range(steps):
        periods_left = steps - i
        remaining = target_end_value - values[-1]
        avg_step = remaining / periods_left
        jitter = rng.uniform(0.55, 1.35)  # organic: not a straight line
        nxt = min(target_end_value, max(values[-1], round(values[-1] + avg_step * jitter)))
        values.append(int(nxt))
    values[-1] = target_end_value  # never invent the endpoint — force-exact
    for i in range(1, len(values)):  # guard against any rounding non-monotonicity
        if values[i] < values[i - 1]:
            values[i] = values[i - 1]
    return values


@dataclass
class SeriesResult:
    period_starts: list[date]
    values: list[int]
    backfilled_prefix_len: int


def build_series(person_dates: list[date | None], period_starts: list[date], seed_key: str) -> SeriesResult:
    dated = sorted(d for d in person_dates if d is not None)
    total = len(person_dates)
    real_values = [sum(1 for d in dated if d <= boundary) for boundary in period_starts]
    if real_values:
        real_values[-1] = total

    first_signal_idx = next((i for i, v in enumerate(real_values[:-1]) if v > 0), None)
    if first_signal_idx is None:
        anchor_idx = len(period_starts) - 1
        anchor_value = total
    else:
        anchor_idx = first_signal_idx
        anchor_value = real_values[first_signal_idx]

    if anchor_idx == 0:
        return SeriesResult(period_starts, real_values, 0)

    backfill_values = generate_organic_curve(anchor_value, anchor_idx + 1, seed_key)
    final_values = backfill_values[:-1] + real_values[anchor_idx:]
    return SeriesResult(period_starts, final_values, anchor_idx)


def main():
    print("=" * 70)
    print("DEMO-ONLY trend backfill — not real history. See this script's")
    print("module docstring. Writes only to demo_trend_backfill, never to")
    print("certification_records.")
    print("=" * 70)

    PortalBase.metadata.create_all(portal_engine)
    db = PortalSessionLocal()
    try:
        deleted = db.query(DemoTrendBackfill).delete()
        print(f"Cleared {deleted} existing seeded row(s) (idempotent re-run).")

        rows_to_insert = []
        for provider in Provider:
            series_specs: list[tuple[str | None, str]] = [(None, "provider-level")]
            for cert_name, _count in tracked_certifications(db, provider):
                series_specs.append((cert_name, "certification"))

            for certification_name, label in series_specs:
                person_dates = fetch_person_dates(db, provider, certification_name)
                total = len(person_dates)
                if total == 0:
                    continue  # no fabricated growth for zero real completions

                earliest_real_month = min((d for d in person_dates if d is not None), default=None)

                monthly_periods = monthly_period_starts(TODAY, earliest_real_month)
                monthly_seed = f"{provider.value}|{certification_name or ''}|monthly"
                monthly = build_series(person_dates, monthly_periods, monthly_seed)

                weekly_periods = weekly_period_starts(TODAY)
                weekly_seed = f"{provider.value}|{certification_name or ''}|weekly"
                weekly = build_series(person_dates, weekly_periods, weekly_seed)

                print(
                    f"{provider.value:12} {label:14} cert={certification_name or '(all)'!s:50} "
                    f"total={total:4} monthly_backfilled={monthly.backfilled_prefix_len}/{len(monthly_periods)} "
                    f"weekly_backfilled={weekly.backfilled_prefix_len}/{len(weekly_periods)}"
                )

                for granularity, series in ((TrendGranularity.monthly, monthly), (TrendGranularity.weekly, weekly)):
                    for i in range(series.backfilled_prefix_len):
                        rows_to_insert.append(
                            dict(
                                provider=provider,
                                certification_name=certification_name,
                                granularity=granularity,
                                period_start=series.period_starts[i],
                                cumulative_completed=series.values[i],
                                source="seed_demo",
                            )
                        )

        if rows_to_insert:
            db.bulk_insert_mappings(DemoTrendBackfill, rows_to_insert)
        db.commit()
        print(f"\nInserted {len(rows_to_insert)} seeded (demo-only) row(s) into demo_trend_backfill.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
