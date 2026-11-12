"""pytest tests/test_usage_service.py — on-demand quota: free/trial 5 total, paid plans 5/week."""

from datetime import datetime, timedelta, timezone

import pytest

from services.usage_service import (
    FREE_LIFETIME_LIMIT, WEEKLY_LIMIT, QuotaExceeded, Reservation, UsageService, decide, peek_decision, week_key, week_resets_at, week_used,
)

# Thursday 2026-11-12, 1pm ET
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
WEEK = "2026-11-09"                                            # that week's Monday
LATER = (NOW + timedelta(hours=3)).isoformat()
EARLIER = (NOW - timedelta(hours=1)).isoformat()


def used(n, symbols=None):
    return {"ondemand_week": WEEK, "ondemand_week_count": n, "ondemand_symbols": symbols or {}}


# ── week maths ────────────────────────────────────────────────────────────

def test_week_key_and_reset():
    assert week_key(NOW) == WEEK
    assert week_resets_at(NOW) == datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)   # Mon 00:00 ET (EST)


def test_week_boundary_is_monday_midnight_et():
    sunday_late_et = datetime(2026, 11, 16, 4, 59, tzinfo=timezone.utc)   # Sun 11:59pm ET
    monday_et = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)         # Mon 00:00 ET
    assert week_key(sunday_late_et) == WEEK
    assert week_key(monday_et) == "2026-11-16"


def test_week_used_resets_on_new_week():
    assert week_used(used(4), NOW) == 4
    assert week_used({"ondemand_week": "2026-11-02", "ondemand_week_count": 5}, NOW) == 0
    assert week_used({}, NOW) == 0


# ── decide(): weekly (pro / paid / coupon) ────────────────────────────────

def test_weekly_counts_new_symbol():
    doc, res = decide({}, "weekly", "AAPL", NOW, plan="paid")
    assert res.counted and doc["ondemand_week"] == WEEK and doc["ondemand_week_count"] == 1
    assert res.quota["left"] == 4 and res.quota["period"] == "week"
    assert res.quota["resets_at"].startswith("2026-11-16")


def test_weekly_review_is_free():
    doc, res = decide(used(2, {"AAPL": LATER}), "weekly", "AAPL", NOW)
    assert not res.counted and doc["ondemand_week_count"] == 2


def test_weekly_expired_symbol_counts_again():
    doc, res = decide(used(2, {"AAPL": EARLIER}), "weekly", "AAPL", NOW)
    assert res.counted and doc["ondemand_week_count"] == 3


def test_weekly_sixth_signal_is_429():
    with pytest.raises(QuotaExceeded) as e:
        decide(used(WEEKLY_LIMIT), "weekly", "TSLA", NOW, plan="pro")
    assert e.value.status == 429 and e.value.detail["code"] == "WEEKLY_LIMIT"
    assert e.value.detail["ondemand"]["left"] == 0


def test_weekly_resets_next_week():
    old = {"ondemand_week": "2026-11-02", "ondemand_week_count": 5}
    doc, res = decide(old, "weekly", "AAPL", NOW)
    assert res.counted and doc["ondemand_week_count"] == 1 and doc["ondemand_week"] == WEEK


# ── decide(): lifetime (free / trial) ─────────────────────────────────────

def test_lifetime_counts_and_never_resets():
    doc, res = decide({"ondemand_used": 4, "ondemand_week": "2026-11-02", "ondemand_week_count": 0},
                      "lifetime", "AAPL", NOW)
    assert res.counted and doc["ondemand_used"] == 5
    assert res.quota == {"used": 5, "limit": 5, "left": 0, "period": "total", "resets_at": None}


def test_lifetime_sixth_signal_is_402_even_next_week():
    later_week = NOW + timedelta(days=30)
    with pytest.raises(QuotaExceeded) as e:
        decide({"ondemand_used": FREE_LIFETIME_LIMIT}, "lifetime", "TSLA", later_week)
    assert e.value.status == 402 and e.value.detail["code"] == "PAYWALL"


def test_lifetime_review_still_free_after_limit():
    _, res = decide({"ondemand_used": 5, "ondemand_symbols": {"AAPL": LATER}}, "lifetime", "AAPL", NOW)
    assert not res.counted


def test_none_kind_never_counts():
    doc, res = decide(used(99), "none", "AAPL", NOW)
    assert not res.counted and doc["ondemand_week_count"] == 99


# ── UsageService routing (no Firestore) ───────────────────────────────────

class Flags:
    def __init__(self, paywall):
        self.paywall = paywall

    def is_enabled(self, name, uid=None):
        return name == "paywall" and self.paywall


class Access:
    def __init__(self, plan, role="user"):
        self.plan, self.role = plan, role

    def get_access(self, uid, use_cache=True):
        return {"plan": self.plan, "role": self.role, "trial_ends_at": None}

    def invalidate(self, uid=None):
        pass


@pytest.mark.parametrize("paywall,plan,role,kind", [
    (False, "free", "user", "none"),
    (True, "admin", "admin", "none"),
    (True, "pro", "pro", "weekly"),
    (True, "paid", "user", "weekly"),
    (True, "coupon", "user", "weekly"),
    (True, "trial", "user", "lifetime"),
    (True, "free", "user", "lifetime"),
])
def test_kind_for(paywall, plan, role, kind):
    svc = UsageService(Access(plan, role), Flags(paywall))
    assert svc._kind_for("u")[0] == kind


def test_no_db_reserve_is_uncounted():
    svc = UsageService(Access("free"), Flags(True))
    res = svc.reserve("u", "aapl")
    assert res == Reservation("AAPL", "none", counted=False)
    svc.release("u", res)
    svc.confirm("u", res, LATER)


# ── peek (read-only preview for the UI) ───────────────────────────────────

def test_peek_none_is_unlimited():
    assert peek_decision(used(99), "none", "AAPL", NOW)["status"] == "unlimited"


def test_peek_weekly_new_symbol_uses_one_and_reports_current_count():
    p = peek_decision(used(2), "weekly", "AAPL", NOW)
    assert p["status"] == "uses_one"
    assert p["quota"]["used"] == 2 and p["quota"]["left"] == 3 and p["quota"]["period"] == "week"


def test_peek_review_is_free():
    p = peek_decision(used(5, {"AAPL": LATER}), "weekly", "AAPL", NOW)
    assert p["status"] == "free" and p["quota"]["left"] == 0


def test_peek_lifetime_blocked_is_paywall():
    p = peek_decision({"ondemand_used": 5}, "lifetime", "MSFT", NOW)
    assert p["status"] == "blocked" and p["code"] == "PAYWALL" and p["quota"]["left"] == 0


def test_peek_weekly_blocked_is_weekly_limit():
    p = peek_decision(used(5), "weekly", "MSFT", NOW)
    assert p["status"] == "blocked" and p["code"] == "WEEKLY_LIMIT"


def test_peek_lifetime_fresh_user():
    p = peek_decision({}, "lifetime", "MSFT", NOW)
    assert p["status"] == "uses_one" and p["quota"] == {
        "used": 0, "limit": FREE_LIFETIME_LIMIT, "left": 5, "period": "total", "resets_at": None}


def test_peek_does_not_mutate_input():
    doc = {"ondemand_used": 1, "ondemand_symbols": {}}
    peek_decision(doc, "lifetime", "MSFT", NOW)
    assert doc == {"ondemand_used": 1, "ondemand_symbols": {}}


def test_no_db_peek_is_unlimited():
    assert UsageService(Access("free"), Flags(True)).peek("u", "aapl")["status"] == "unlimited"
