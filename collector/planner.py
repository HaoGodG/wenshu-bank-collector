from __future__ import annotations
import calendar
from datetime import date, timedelta
from .models import Condition


def _last_day_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _add_months_first(d: date, months: int) -> date:
    idx = d.year * 12 + (d.month - 1) + months
    return date(idx // 12, idx % 12 + 1, 1)


def bisect_dates(start: date, end: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """Split an inclusive logical interval into two gap-free halves.

    If the range covers whole calendar months, split on a month boundary so a
    full year becomes Jan-Jun + Jul-Dec, matching the desired collection plan.
    Otherwise fall back to a day-count midpoint.
    """
    if start >= end:
        raise ValueError("单日区间不能继续按日期二分")

    full_month_range = start.day == 1 and end == _last_day_of_month(end)
    if full_month_range:
        month_count = (end.year - start.year) * 12 + (end.month - start.month) + 1
        if month_count >= 2:
            left_months = month_count // 2
            right_start = _add_months_first(start, left_months)
            left_end = right_start - timedelta(days=1)
            return (start, left_end), (right_start, end)

    days = (end - start).days
    mid = start + timedelta(days=days // 2)
    return (start, mid), (mid + timedelta(days=1), end)


def with_date_condition(conditions: list[Condition], start: date, end: date) -> list[Condition]:
    """Use the same inclusive date range that the website UI sends.

    The 2026-03-16~2026-04-01 HAR returns documents dated 2026-04-01 while
    queryParams reports s31 LESS 2026-04-01, so the backend's operator labels
    must not be interpreted as strict mathematical inequalities here.
    """
    base = [c for c in conditions if c.key != "cprq"]
    return base + [Condition("cprq", f"{start.isoformat()} TO {end.isoformat()}")]


def add_condition(conditions: list[Condition], key: str, value: str) -> list[Condition]:
    if key == "s21":
        return conditions + [Condition(key, value)]
    return [c for c in conditions if c.key != key] + [Condition(key, value)]
