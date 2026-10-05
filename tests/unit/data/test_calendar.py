"""NYSE calendar checked against published holiday and early-close schedules."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from oracle.data import calendar


def test_2024_holidays_match_published_schedule() -> None:
    assert sorted(calendar.holidays(2024)) == [
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 2, 19),
        date(2024, 3, 29),
        date(2024, 5, 27),
        date(2024, 6, 19),
        date(2024, 7, 4),
        date(2024, 9, 2),
        date(2024, 11, 28),
        date(2024, 12, 25),
    ]


def test_2025_includes_special_closure() -> None:
    h = calendar.holidays(2025)
    assert date(2025, 1, 9) in h  # Carter national day of mourning
    assert date(2025, 4, 18) in h  # Good Friday
    assert len(h) == 11


def test_saturday_new_year_is_not_observed_on_friday() -> None:
    assert calendar.is_session(date(2021, 12, 31))
    assert date(2022, 1, 1) not in calendar.holidays(2022)
    assert len(calendar.holidays(2022)) == 9


def test_sunday_juneteenth_observed_on_monday() -> None:
    assert not calendar.is_session(date(2022, 6, 20))


@pytest.mark.parametrize(("year", "expected"), [(2022, 251), (2023, 250), (2024, 252)])
def test_trading_day_counts(year: int, expected: int) -> None:
    assert len(calendar.sessions(date(year, 1, 1), date(year, 12, 31))) == expected


def test_early_closes() -> None:
    for day in (date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24), date(2023, 7, 3)):
        s = calendar.session(day)
        assert s is not None
        assert s.early_close
        assert s.minutes == 210
    regular = calendar.session(date(2024, 7, 2))
    assert regular is not None
    assert not regular.early_close
    assert regular.minutes == 390


def test_session_times_in_utc_follow_dst() -> None:
    winter = calendar.session(date(2024, 1, 2))
    summer = calendar.session(date(2024, 7, 2))
    assert winter is not None
    assert summer is not None
    assert winter.open == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert summer.close == datetime(2024, 7, 2, 20, 0, tzinfo=UTC)


def test_unsupported_year_raises() -> None:
    with pytest.raises(ValueError, match="supports"):
        calendar.holidays(1999)


@pytest.mark.parametrize(
    ("year", "easter"),
    [(2019, date(2019, 4, 21)), (2024, date(2024, 3, 31)), (2025, date(2025, 4, 20))],
)
def test_easter_dates(year: int, easter: date) -> None:
    assert calendar._easter(year) == easter
