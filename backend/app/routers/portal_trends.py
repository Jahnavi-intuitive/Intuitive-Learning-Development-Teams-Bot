"""Data endpoint for the L&D portal Trends tab (frontend/portal.html) —
reads exclusively from the lnd_portal database. Provider-wise only: every
number here is attributable to a single provider or a single certification,
never summed into one combined/org-wide total — that was an earlier design
pass explicitly backed out of, per direct instruction.

One combined `/overview` endpoint, same reasoning as routers/portal_dashboard.py:
the frontend never re-queries when a viewer switches the provider tab or the
Weekly/Monthly sub-tab, it just re-filters `providers` (already on the page).

Historical points before the real data begins are demo-only backfill (see
app/services/trend_series.py + scripts/seed_demo_trend_history.py) — each
point carries `is_seeded` so the UI can mark illustrative history visibly,
never silently blending it with real data.
"""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.portal_db import get_portal_db
from app.portal_models import CertificationRecord, Provider, ProviderTarget, StatusCanonical, TrendGranularity
from app.services.trend_series import (
    fetch_person_dates,
    merged_series,
    monthly_period_starts,
    tracked_certifications,
    weekly_period_starts,
)

router = APIRouter(prefix="/api/portal/trends", tags=["portal-trends"])

_IDENTITY = func.lower(func.trim(CertificationRecord.person_name))

# How many trailing months get a "new completions" stat tile in the Monthly
# sub-tab — "a few selected months", not the whole history, per direct
# instruction; tiles are differences of the same cumulative series charted
# above, never a separate query.
_MONTH_TILE_COUNT = 6


class SeriesPointOut(BaseModel):
    period: date
    value: int
    is_seeded: bool  # True = demo-only backfill, not a real record


class ComparisonSeriesOut(BaseModel):
    provider: str
    certification_name: str
    points: list[SeriesPointOut]


class LeaderOut(BaseModel):
    provider: str
    certification_name: str
    latest: int
    growth_pct: float | None  # None = no real/seeded baseline to compare against (a brand-new cert)


class HighlightsOut(BaseModel):
    fastest_growing: LeaderOut | None
    furthest_from_target_provider: str | None
    furthest_from_target_pct: float | None  # % of target reached — None if no provider has a target set


class ComparisonOut(BaseModel):
    months: list[date]
    series: list[ComparisonSeriesOut]  # one per provider's leading certification
    leaders: list[LeaderOut]  # every tracked certification, sorted by latest desc
    highlights: HighlightsOut


class WeeklyOut(BaseModel):
    this_week_label: date
    this_week_count: int
    previous_week_label: date
    previous_week_count: int
    comparator_first: SeriesPointOut
    comparator_last: SeriesPointOut
    favorable: bool  # more completions is good — up = favorable, never hardcoded to "green"


class MonthTileOut(BaseModel):
    month: date
    new_completed: int
    delta_vs_previous: int | None


class MonthlyOut(BaseModel):
    points: list[SeriesPointOut]
    target: int | None
    month_tiles: list[MonthTileOut]


class ProviderTrendOut(BaseModel):
    provider: str
    weekly: WeeklyOut | None  # None only if this provider has zero real completions
    monthly: MonthlyOut | None


class TrendsOverviewOut(BaseModel):
    data_as_of: datetime
    comparison: ComparisonOut
    providers: list[ProviderTrendOut]


def _provider_completed_total(db: Session, provider: Provider) -> int:
    return db.scalar(
        select(func.count(func.distinct(_IDENTITY))).where(
            CertificationRecord.provider == provider, CertificationRecord.status_canonical == StatusCanonical.completed
        )
    ) or 0


def _growth_pct(db: Session, provider: Provider, certification_name: str | None, today: date) -> tuple[int, float | None]:
    jan1 = date(today.year, 1, 1)
    current_month = date(today.year, today.month, 1)
    points = merged_series(db, provider, certification_name, TrendGranularity.monthly, [jan1, current_month])
    jan1_value, latest = points[0].value, points[1].value
    if jan1_value <= 0:
        return latest, None
    return latest, round((latest - jan1_value) / jan1_value * 100, 1)


@router.get("/overview", response_model=TrendsOverviewOut)
def trends_overview(db: Session = Depends(get_portal_db)):
    today = date.today()
    targets = dict(db.execute(select(ProviderTarget.provider, ProviderTarget.target_count)).all())

    # ---------------- Section 1: comparison chart + leaders ----------------
    comparison_series: list[ComparisonSeriesOut] = []
    all_months: set[date] = set()
    leaders: list[LeaderOut] = []

    for provider in Provider:
        certs = tracked_certifications(db, provider)
        if not certs:
            continue  # zero real completions — no chart line, no leaderboard rows, no fabricated growth

        leading_name = certs[0][0]
        leading_dates = fetch_person_dates(db, provider, leading_name)
        earliest = min((d for d in leading_dates if d is not None), default=None)
        months = monthly_period_starts(today, earliest)
        points = merged_series(db, provider, leading_name, TrendGranularity.monthly, months)
        all_months.update(months)
        comparison_series.append(
            ComparisonSeriesOut(
                provider=provider.value,
                certification_name=leading_name,
                points=[SeriesPointOut(period=p.period_start, value=p.value, is_seeded=p.is_seeded) for p in points],
            )
        )

        for cert_name, _count in certs:
            latest, growth_pct = _growth_pct(db, provider, cert_name, today)
            leaders.append(LeaderOut(provider=provider.value, certification_name=cert_name, latest=latest, growth_pct=growth_pct))

    leaders.sort(key=lambda l: l.latest, reverse=True)

    fastest = max((l for l in leaders if l.growth_pct is not None), key=lambda l: l.growth_pct, default=None)

    furthest_provider, furthest_pct = None, None
    for provider, target in targets.items():
        if target <= 0:
            continue
        completed = _provider_completed_total(db, provider)
        pct = round(completed / target * 100, 1)
        if furthest_pct is None or pct < furthest_pct:
            furthest_provider, furthest_pct = provider.value, pct

    comparison = ComparisonOut(
        months=sorted(all_months),
        series=comparison_series,
        leaders=leaders,
        highlights=HighlightsOut(
            fastest_growing=fastest,
            furthest_from_target_provider=furthest_provider,
            furthest_from_target_pct=furthest_pct,
        ),
    )

    # ---------------- Section 2: per-provider weekly/monthly ----------------
    provider_trends: list[ProviderTrendOut] = []
    for provider in Provider:
        total = _provider_completed_total(db, provider)
        if total == 0:
            provider_trends.append(ProviderTrendOut(provider=provider.value, weekly=None, monthly=None))
            continue

        week_starts = weekly_period_starts(today)
        week_points = merged_series(db, provider, None, TrendGranularity.weekly, week_starts)
        this_week = week_points[-1].value - week_points[-2].value
        previous_week = week_points[-2].value - week_points[-3].value
        weekly = WeeklyOut(
            this_week_label=week_points[-1].period_start,
            this_week_count=this_week,
            previous_week_label=week_points[-2].period_start,
            previous_week_count=previous_week,
            comparator_first=SeriesPointOut(period=week_points[0].period_start, value=week_points[0].value, is_seeded=week_points[0].is_seeded),
            comparator_last=SeriesPointOut(period=week_points[-1].period_start, value=week_points[-1].value, is_seeded=week_points[-1].is_seeded),
            favorable=week_points[-1].value >= week_points[0].value,
        )

        earliest = min((d for d in fetch_person_dates(db, provider, None) if d is not None), default=None)
        month_starts = monthly_period_starts(today, earliest)
        month_points = merged_series(db, provider, None, TrendGranularity.monthly, month_starts)
        start_offset = max(0, len(month_points) - _MONTH_TILE_COUNT)
        # new_completed for the first tile shown has no prior point in view to
        # diff against if it's also the very first real/seeded month overall —
        # fall back to its own cumulative value (i.e. "new" = "all of it, so far").
        tiles: list[MonthTileOut] = []
        prior_new: int | None = None
        for idx_in_full in range(start_offset, len(month_points)):
            p = month_points[idx_in_full]
            new_completed = p.value if idx_in_full == 0 else p.value - month_points[idx_in_full - 1].value
            delta_vs_previous = None if prior_new is None else new_completed - prior_new
            tiles.append(MonthTileOut(month=p.period_start, new_completed=new_completed, delta_vs_previous=delta_vs_previous))
            prior_new = new_completed

        monthly = MonthlyOut(
            points=[SeriesPointOut(period=p.period_start, value=p.value, is_seeded=p.is_seeded) for p in month_points],
            target=targets.get(provider),
            month_tiles=tiles,
        )
        provider_trends.append(ProviderTrendOut(provider=provider.value, weekly=weekly, monthly=monthly))

    return TrendsOverviewOut(data_as_of=datetime.now(timezone.utc), comparison=comparison, providers=provider_trends)
