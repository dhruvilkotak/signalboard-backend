"""pytest tests/test_ondemand_expiry.py — on-demand signals reset nightly at 11:59 PM ET."""

from datetime import datetime, timezone

from services.ondemand_signal_service import _session_expiry
from services.usage_service import decide

# 2026-11-12 is EST (UTC-5): 11:59 PM ET == 04:59 UTC next day
END_OF_DAY = datetime(2026, 11, 13, 4, 59, tzinfo=timezone.utc)


def test_morning_signal_expires_at_1159pm_et():
    assert _session_expiry(datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)) == END_OF_DAY   # 9 AM ET


def test_1pm_signal_expires_same_night():
    assert _session_expiry(datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)) == END_OF_DAY   # 1 PM ET


def test_just_after_midnight_lasts_whole_day():
    assert _session_expiry(datetime(2026, 11, 12, 5, 1, tzinfo=timezone.utc)) == END_OF_DAY    # 12:01 AM ET


def test_last_minute_rolls_to_next_night():
    exp = _session_expiry(datetime(2026, 11, 13, 4, 59, 30, tzinfo=timezone.utc))              # 11:59:30 PM ET
    assert exp == datetime(2026, 11, 14, 4, 59, tzinfo=timezone.utc)


def test_first_view_counts_even_if_another_user_generated_it():
    # The usage doc is per user; the shared cache doesn't make a first view free.
    now = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
    _, res = decide({}, "lifetime", "AAPL", now)
    assert res.counted


def test_review_free_until_1159pm_then_counts_again():
    gen = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
    doc = {"ondemand_used": 1, "ondemand_symbols": {"AAPL": _session_expiry(gen).isoformat()}}
    _, same_night = decide(doc, "lifetime", "AAPL", datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc))
    _, next_day = decide(doc, "lifetime", "AAPL", datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc))
    assert not same_night.counted and next_day.counted
