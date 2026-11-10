from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import NOW
from lights_out.keep import KeepError, parse_keep

IST = ZoneInfo("Asia/Kolkata")


def test_iso_date_ends_2359_local():
    end = parse_keep("2026-12-15", NOW, IST)
    assert end == datetime(2026, 12, 15, 23, 59, tzinfo=IST)


def test_eod_date_same():
    assert parse_keep("eod 2026-12-15", NOW, IST) == parse_keep("2026-12-15", NOW, IST)


def test_weeks_days_months():
    assert parse_keep("2w", NOW, IST).date().isoformat() == "2026-11-25"
    assert parse_keep("10d", NOW, IST).date().isoformat() == "2026-11-21"
    assert parse_keep("2mo", NOW, IST).date().isoformat() == "2027-01-11"


def test_month_end_clamps():
    jan31 = datetime(2027, 1, 31, 6, 0, tzinfo=timezone.utc)
    assert parse_keep("1mo", jan31, IST).date().isoformat() == "2027-02-28"


def test_today_and_eod():
    assert parse_keep("eod", NOW, IST) == datetime(2026, 11, 11, 23, 59, tzinfo=IST)


@pytest.mark.parametrize("bad", ["2026-11-10", "2025-01-01", "yesterday", "", "0d", "2026-02-30", "3 years", "-2w"])
def test_rejected(bad):
    with pytest.raises(KeepError):
        parse_keep(bad, NOW, IST)


def test_today_after_2359_is_past():
    late = datetime(2026, 11, 11, 18, 30, tzinfo=timezone.utc)  # 00:00 IST on 12 Nov
    with pytest.raises(KeepError):
        parse_keep("2026-11-11", late, IST)


def test_timezone_matters():
    from datetime import timedelta
    assert parse_keep("2026-12-15", NOW, timezone.utc) - parse_keep("2026-12-15", NOW, IST) == timedelta(hours=5, minutes=30)


def test_more_than_two_years_rejected():
    with pytest.raises(KeepError):
        parse_keep("2030-01-01", NOW, IST)
