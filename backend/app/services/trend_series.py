"""Shared real+backfill trend-series logic for the Trends tab.

Used by both scripts/seed_demo_trend_history.py (writes the backfill rows,
once) and routers/portal_trends.py (reads real data live and merges in a
seeded fallback only where no real signal exists for that period). Keeping
this in one place is what makes "prefer real over seeded once real data
exists" automatic rather than something either side has to remember to
re-check: merged_series always recomputes the real/seeded boundary fresh
against the current certification_records, never trusts a period's seeded
row without first checking whether real data has since caught up to it.
"""

import datetime
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.portal_models import CertificationRecord, DemoTrendBackfill, Provider, StatusCanonical, TrendGranularity

# Calendar-year-to-date monthly window (confirmed decision): Jan 1 of this
# year through the 1st of the current month, extended earlier automatically
# per series if real dated history goes back further.
_MIN_MONTHLY_START_MONTH = 1

# 8-week comparator (ISO Monday-start weeks, confirmed decision), inclusive
# of the current week.
WEEKLY_PERIODS = 8

_TREND_DATE = func.coalesce(
    CertificationRecord.completion_date,
    CertificationRecord.submission_date,
    CertificationRecord.exam_date,
)
_IDENTITY = func.lower(func.trim(CertificationRecord.person_name))


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    month0 = d.month - 1 + n
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    return date(year, month, 1)


def _monday_of_week(d: date) -> date:
    return d - datetime.timedelta(days=d.weekday())


def monthly_period_starts(today: date, earliest_real_month: date | None) -> list[date]:
    start = date(today.year, _MIN_MONTHLY_START_MONTH, 1)
    if earliest_real_month is not None and earliest_real_month < start:
        start = _month_start(earliest_real_month)
    current = _month_start(today)
    periods = []
    m = start
    while m <= current:
        periods.append(m)
        m = _add_months(m, 1)
    return periods


def weekly_period_starts(today: date) -> list[date]:
    current_monday = _monday_of_week(today)
    return [current_monday - datetime.timedelta(weeks=i) for i in range(WEEKLY_PERIODS - 1, -1, -1)]


def fetch_person_dates(db: Session, provider: Provider, certification_name: str | None) -> list[date | None]:
    """One entry per distinct completed person on this series (their
    earliest known trend date, or None if genuinely undated in the source
    data). certification_name=None means the whole-provider series (every
    certification under that provider, combined)."""
    stmt = (
        select(_IDENTITY, func.min(_TREND_DATE))
        .where(CertificationRecord.provider == provider, CertificationRecord.status_canonical == StatusCanonical.completed)
        .group_by(_IDENTITY)
    )
    if certification_name is not None:
        stmt = stmt.where(CertificationRecord.certification_name == certification_name)
    return [d for _, d in db.execute(stmt).all()]


def tracked_certifications(db: Session, provider: Provider) -> list[tuple[str, int]]:
    """Every certification under this provider with at least one real
    completion, as (certification_name, completed_count), sorted by count
    descending. completed_count is a real distinct-person count."""
    rows = db.execute(
        select(CertificationRecord.certification_name, func.count(func.distinct(_IDENTITY)))
        .where(
            CertificationRecord.provider == provider,
            CertificationRecord.status_canonical == StatusCanonical.completed,
            CertificationRecord.certification_name.is_not(None),
        )
        .group_by(CertificationRecord.certification_name)
        .order_by(func.count(func.distinct(_IDENTITY)).desc())
    ).all()
    return [(name, count) for name, count in rows]


@dataclass
class SeriesPoint:
    period_start: date
    value: int
    is_seeded: bool  # True = this point came from demo_trend_backfill, not real data


def merged_series(
    db: Session,
    provider: Provider,
    certification_name: str | None,
    granularity: TrendGranularity,
    period_starts: list[date],
) -> list[SeriesPoint]:
    """Real data wins for any period where real dated evidence exists at or
    before that boundary, no matter what's sitting in demo_trend_backfill —
    a stale seeded row for a now-real period is simply never read. The
    final period in the window is always the true total (dated + undated
    real completions), same reasoning as the seed script: we fold in
    real-but-undated completions at "today" rather than guess when they
    happened."""
    person_dates = fetch_person_dates(db, provider, certification_name)
    dated = sorted(d for d in person_dates if d is not None)
    total = len(person_dates)

    seeded_rows = dict(
        db.execute(
            select(DemoTrendBackfill.period_start, DemoTrendBackfill.cumulative_completed).where(
                DemoTrendBackfill.provider == provider,
                DemoTrendBackfill.certification_name.is_(certification_name)
                if certification_name is None
                else DemoTrendBackfill.certification_name == certification_name,
                DemoTrendBackfill.granularity == granularity,
            )
        ).all()
    )

    points = []
    last_idx = len(period_starts) - 1
    for i, boundary in enumerate(period_starts):
        if i == last_idx:
            points.append(SeriesPoint(boundary, total, is_seeded=False))
            continue
        real_count = sum(1 for d in dated if d <= boundary)
        if real_count > 0:
            points.append(SeriesPoint(boundary, real_count, is_seeded=False))
        elif boundary in seeded_rows:
            points.append(SeriesPoint(boundary, seeded_rows[boundary], is_seeded=True))
        else:
            points.append(SeriesPoint(boundary, 0, is_seeded=False))
    return points
