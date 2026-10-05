"""NYSE trading calendar: regular sessions, holidays, early closes, special closures.

Rule-based, valid for SUPPORTED_YEARS only (outside that range it raises rather than guessing).
Sources: NYSE Rule 7.2 holiday conventions and published closure notices. The special-closure
list must be extended when the exchange announces a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
SUPPORTED_YEARS = range(2000, 2031)
_NS_PER_DAY = 86_400 * 1_000_000_000
_EPOCH = date(1970, 1, 1)

SPECIAL_CLOSURES: dict[date, str] = {
    date(2001, 9, 11): "September 11 attacks",
    date(2001, 9, 12): "September 11 attacks",
    date(2001, 9, 13): "September 11 attacks",
    date(2001, 9, 14): "September 11 attacks",
    date(2004, 6, 11): "National day of mourning (Reagan)",
    date(2007, 1, 2): "National day of mourning (Ford)",
    date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "National day of mourning (G.H.W. Bush)",
    date(2025, 1, 9): "National day of mourning (Carter)",
}


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime  # UTC
    close: datetime  # UTC
    early_close: bool

    @property
    def minutes(self) -> int:
        return int((self.close - self.open) / timedelta(minutes=1))


def _check_year(year: int) -> None:
    if year not in SUPPORTED_YEARS:
        raise ValueError(
            f"NYSE calendar supports {SUPPORTED_YEARS.start}-{SUPPORTED_YEARS.stop - 1}; "
            f"got {year}. Extend and review the rules before using other years."
        )


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following_month = date(year + (month == 12), month % 12 + 1, 1)
    last = following_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    weekday_shift = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_shift) // 451
    month = (h + weekday_shift - 7 * m + 114) // 31
    day = (h + weekday_shift - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(day: date) -> date:
    """Saturday holidays move to Friday, Sunday holidays to Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def holidays(year: int) -> dict[date, str]:
    _check_year(year)
    result: dict[date, str] = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        result[new_year + timedelta(days=1)] = "New Year's Day (observed)"
    elif new_year.weekday() != 5:  # Saturday: not observed on the prior Friday
        result[new_year] = "New Year's Day"
    result[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    result[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    result[_easter(year) - timedelta(days=2)] = "Good Friday"
    result[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        result[_observed(date(year, 6, 19))] = "Juneteenth"
    result[_observed(date(year, 7, 4))] = "Independence Day"
    result[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    result[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    result[_observed(date(year, 12, 25))] = "Christmas Day"
    for day, reason in SPECIAL_CLOSURES.items():
        if day.year == year:
            result[day] = reason
    return result


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def _is_early_close(day: date) -> bool:
    if day == _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1):
        return True  # day after Thanksgiving
    if day.month == 12 and day.day == 24:
        return True
    return day.month == 7 and day.day == 3 and date(day.year, 7, 4).weekday() < 5


def session(day: date) -> Session | None:
    if not is_session(day):
        return None
    early = _is_early_close(day)
    opened = datetime.combine(day, REGULAR_OPEN, NEW_YORK).astimezone(UTC)
    closed = datetime.combine(day, EARLY_CLOSE if early else REGULAR_CLOSE, NEW_YORK)
    return Session(day, opened, closed.astimezone(UTC), early)


def session_bounds_ns(
    event_ns: npt.NDArray[np.int64],
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Session open/close (UTC ns) for each timestamp's UTC day; -1 where the exchange is closed.

    A regular or early NYSE session lies within one UTC calendar day, so a bar's UTC date is its
    New York trading date.
    """
    days = event_ns // _NS_PER_DAY
    unique = np.unique(days)
    opens = np.full(unique.shape, -1, dtype=np.int64)
    closes = np.full(unique.shape, -1, dtype=np.int64)
    for i, day_index in enumerate(unique):
        s = session(_EPOCH + timedelta(days=int(day_index)))
        if s is not None:
            opens[i] = _to_ns(s.open)
            closes[i] = _to_ns(s.close)
    pos = np.searchsorted(unique, days)
    return opens[pos], closes[pos]


def _to_ns(moment: datetime) -> int:
    return int((moment - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1)) * 1000


def sessions(start: date, end: date) -> list[Session]:
    """Sessions with start <= day <= end."""
    result = []
    day = start
    while day <= end:
        s = session(day)
        if s is not None:
            result.append(s)
        day += timedelta(days=1)
    return result
