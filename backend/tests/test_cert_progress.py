"""Business-day prep clock (services/cert_progress.py) — pure date maths, no DB."""

from datetime import date

from app.services.cert_progress import add_business_days, business_days_between

MON = date(2026, 9, 28)  # a Monday


def test_same_day_is_zero():
    assert business_days_between(MON, MON) == 0


def test_one_full_week_is_five():
    assert business_days_between(MON, date(2026, 10, 5)) == 5


def test_friday_to_monday_skips_weekend():
    fri = date(2026, 10, 2)
    assert business_days_between(fri, date(2026, 10, 5)) == 1


def test_weekend_days_add_nothing():
    fri = date(2026, 10, 2)
    assert business_days_between(fri, date(2026, 10, 4)) == 0  # Sat, Sun


def test_add_business_days_round_trips():
    for n in (1, 5, 10, 20, 37):
        assert business_days_between(MON, add_business_days(MON, n)) == n


def test_add_business_days_lands_on_a_weekday():
    assert add_business_days(date(2026, 10, 2), 1) == date(2026, 10, 5)  # Fri + 1 -> Mon
