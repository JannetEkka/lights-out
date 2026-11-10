"""Parse a keep date. Accepted: ``2026-12-15``, ``eod 2026-12-15`` (same thing), ``today``/``eod``,
``Nd``, ``Nw``, ``Nmo`` (``2w``, ``10d``, ``2mo``). A keep always ends at 23:59 on that day in
the configured time zone. A date in the past is rejected.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class KeepError(ValueError):
    pass


def resolve_tz(name: Optional[str]) -> tzinfo:
    """The configured zone, else the system's local zone."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise KeepError(f"unknown time zone {name!r}") from e
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def parse_keep(text: str, now: datetime, tz: tzinfo) -> datetime:
    """Return the aware datetime (23:59:00 local) the keep ends at."""
    s = (text or "").strip().lower()
    if not s:
        raise KeepError("say until when: a date like 2026-12-15, or 10d / 2w / 2mo")
    today = now.astimezone(tz).date()
    s = re.sub(r"^(eod|end of day|until)\s+", "", s)
    if s in ("eod", "today"):
        day = today
    elif m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError as e:
            raise KeepError(f"{text!r} is not a real date") from e
    elif m := re.fullmatch(r"(\d{1,4})\s*(d|w|mo)", s):
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            raise KeepError("the length must be at least 1")
        day = today + timedelta(days=n) if unit == "d" else today + timedelta(weeks=n) if unit == "w" else _add_months(today, n)
    else:
        raise KeepError(f"can't read {text!r}: use 2026-12-15, eod 2026-12-15, 10d, 2w or 2mo")
    end = datetime.combine(day, time(23, 59), tzinfo=tz)
    if end <= now:
        raise KeepError(f"{day.isoformat()} 23:59 is already past")
    if end - now > timedelta(days=366 * 2):
        raise KeepError("keeps are limited to two years; set a nearer date and extend it later")
    return end
