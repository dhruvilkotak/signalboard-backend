"""pytest tests/test_usage_service.py — pure decision logic + service routing."""

from datetime import datetime, timedelta, timezone

import pytest

from services.usage_service import (
    DAILY_FAIR_USE_LIMIT, FREE_LIFETIME_LIMIT, QuotaExceeded, Reservation, UsageService, decide,
)

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)       # 1pm ET
LATER = (NOW + timedelta(hours=3)).isoformat()
EARLIER = (NOW - timedelta(hours=1)).isoformat()


# ── decide(): lifetime ────────────────────────────────────────────────────

def test_lifetime_counts_new_symbol():
    doc, res = decide({}, "lifetime", "AAPL", NOW)
    assert res.counted and doc["ondemand_used"] == 1
    assert res.quota == {"used": 1, "limit": 5, "left": 4}


def test_lifetime_review_is_free():
    doc, res = decide({"ondemand_used": 2, "ondemand_symbols": {"AAPL": LATER}}, "lifetime", "AAPL", NOW)
    assert not res.counted and doc["ondemand_used"] == 2


def test_lifetime_expired_symbol_counts_again():
    doc, res = decide({"ondemand_used": 2, "ondemand_symbols": {"AAPL": EARLIER}}, "lifetime", "AAPL", NOW)
    assert res.counted and doc["ondemand_used"] == 3


def test_lifetime_sixth_new_symbol_paywalls():
    with pytest.raises(QuotaExceeded) as e:
        decide({"ondemand_used": FREE_LIFETIME_LIMIT}, "lifetime", "TSLA", NOW)
    assert e.value.status == 402 and e.value.detail["code"] == "PAYWALL"
    assert e.value.detail["ondemand"]["left"] == 0


def test_lifetime_review_still_free_after_limit():
    _, res = decide({"ondemand_used": 5, "ondemand_symbols": {"AAPL": LATER}}, "lifetime", "AAPL", NOW)
    assert not res.counted


# ── decide(): daily ───────────────────────────────────────────────────────

def test_daily_counts_and_resets_next_day():
    doc, res = decide({"ondemand_day": "2026-11-08", "ondemand_today": 49}, "daily", "AAPL", NOW)
    assert res.counted and doc["ondemand_day"] == "2026-11-09" and doc["ondemand_today"] == 1


def test_daily_cap():
    with pytest.raises(QuotaExceeded) as e:
        decide({"ondemand_day": "2026-11-09", "ondemand_today": DAILY_FAIR_USE_LIMIT}, "daily", "AAPL", NOW)
    assert e.value.status == 429 and e.value.detail["code"] == "DAILY_LIMIT"


def test_none_kind_never_counts():
    doc, res = decide({"ondemand_used": 99}, "none", "AAPL", NOW)
    assert not res.counted and doc["ondemand_used"] == 99


# ── UsageService routing (no Firestore) ───────────────────────────────────

class Flags:
    def __init__(self, paywall):
        self.paywall = paywall

    def is_enabled(self, name, uid=None):
        return name == "paywall" and self.paywall


class Access:
    def __init__(self, plan):
        self.plan = plan
        self.invalidated = []

    def get_access(self, uid, use_cache=True):
        return {"plan": self.plan, "trial_ends_at": None}

    def invalidate(self, uid=None):
        self.invalidated.append(uid)


@pytest.mark.parametrize("paywall,plan,kind", [
    (False, "free", "none"),
    (True, "admin", "none"),
    (True, "pro", "daily"),
    (True, "paid", "daily"),
    (True, "coupon", "daily"),
    (True, "trial", "lifetime"),
    (True, "free", "lifetime"),
])
def test_kind_for(paywall, plan, kind):
    svc = UsageService(Access(plan), Flags(paywall))
    assert svc._kind_for("u")[0] == kind


def test_no_db_reserve_is_uncounted():
    svc = UsageService(Access("free"), Flags(True))
    res = svc.reserve("u", "aapl")
    assert res == Reservation("AAPL", "none", counted=False)
    svc.release("u", res)
    svc.confirm("u", res, LATER)
