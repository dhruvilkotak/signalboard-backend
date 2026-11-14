"""pytest tests/test_access_expiry.py — liquidation state machine (fakes only)."""

import asyncio

import pytest

import services.access_expiry_service as aes
from services.access_expiry_service import AccessExpiryService


class Snap:
    def __init__(self, d):
        self._d = d

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return self._d


class AccessDoc:
    def __init__(self, store):
        self.store = store

    def collection(self, _):
        return self

    def document(self, _):
        return self

    def get(self):
        return Snap(self.store.get("access"))

    def set(self, data, merge=False):
        cur = dict(self.store.get("access") or {}) if merge else {}
        cur.update(data)
        self.store["access"] = cur

    def add(self, record):                     # job_failures
        self.store.setdefault("job_failures", []).append(record)


class DB:
    def __init__(self):
        self.store = {}

    def collection(self, _):
        return AccessDoc(self.store)


class Portfolio:
    def __init__(self, strategies):
        self.strategies = strategies          # {sk: {"is_paused": bool}}
        self.calls = []
        self.fail = set()                     # strategy keys whose stop_strategy raises

    async def get_all_strategies(self, uid):
        return {k: dict(v) for k, v in self.strategies.items()}

    async def pause_strategy(self, uid, sk, paused):
        self.calls.append(("pause", sk, paused))
        self.strategies[sk]["is_paused"] = paused

    async def stop_strategy(self, uid, sk, prices, trigger="stop_strategy"):
        self.calls.append(("stop", sk, trigger))
        if sk in self.fail:
            raise RuntimeError("alpaca down")
        self.strategies.pop(sk)
        return {"positions_closed": 2, "returned": 500.0}


class Prices:
    async def get_all(self):
        return {"AAPL": {"price": 200}}


class Access:
    def __init__(self, autotrader):
        self.autotrader = autotrader

    def get_access(self, uid, use_cache=True):
        return {"plan": "free", "features": {"autotrader": self.autotrader}}

    def invalidate(self, uid=None):
        pass


class Flags:
    def __init__(self, on):
        self.on = on

    def is_enabled(self, name, uid=None):
        return self.on


@pytest.fixture(autouse=True)
def _ts(monkeypatch):
    monkeypatch.setattr(aes.firestore, "SERVER_TIMESTAMP", "ts", raising=False)


def make(autotrader=False, flag=True, trading=True, strategies=None):
    portfolio = Portfolio(strategies if strategies is not None else
                          {"moderate": {"is_paused": False}, "aggressive": {"is_paused": True}})
    db = DB()
    alerts = []
    svc = AccessExpiryService(portfolio, Prices(), Access(autotrader), Flags(flag), db=db,
                              trading_allowed=lambda: trading, alert=alerts.append)
    svc.alerts = alerts
    return svc, portfolio, db


run = lambda coro: asyncio.run(coro)


def test_has_access_does_nothing():
    svc, p, _ = make(autotrader=True)
    assert run(svc.reconcile("u")) == "ok" and p.calls == []


def test_flag_off_is_dry_run():
    svc, p, db = make(flag=False)
    assert run(svc.reconcile("u")) == "dry_run"
    assert p.calls == [] and "access" not in db.store


def test_market_closed_pauses_and_waits():
    svc, p, db = make(trading=False)
    assert run(svc.reconcile("u")) == "pending"
    assert p.calls == [("pause", "moderate", True)]           # aggressive was already paused
    assert db.store["access"]["pending_liquidation"] is True
    assert db.store["access"]["paused_by_expiry"] == ["moderate"]
    assert run(svc.reconcile("u")) == "pending"               # second run doesn't re-pause
    assert len(p.calls) == 1


def test_market_open_liquidates_everything():
    svc, p, db = make(trading=True)
    assert run(svc.reconcile("u")) == "liquidated"
    stops = [c for c in p.calls if c[0] == "stop"]
    assert stops == [("stop", "aggressive", "access_expired"), ("stop", "moderate", "access_expired")]
    st = db.store["access"]
    assert st["pending_liquidation"] is False
    assert st["last_liquidation"]["positions_closed"] == 4 and st["last_liquidation"]["returned"] == 1000.0
    assert run(svc.reconcile("u")) == "none"                   # nothing left: idempotent


def test_access_regained_restores_only_what_we_paused():
    svc, p, db = make(trading=False)
    run(svc.reconcile("u"))
    svc.access = Access(autotrader=True)                       # e.g. redeemed a coupon
    assert run(svc.reconcile("u")) == "restored"
    assert p.strategies["moderate"]["is_paused"] is False      # we paused it -> resumed
    assert p.strategies["aggressive"]["is_paused"] is True     # user paused it -> left alone
    assert db.store["access"]["pending_liquidation"] is False


def test_no_strategies_is_noop():
    svc, p, _ = make(strategies={})
    assert run(svc.reconcile("u")) == "none" and p.calls == []


# ── once a day: retry next day, give up after 2 retries ───────────────────

def test_failure_retries_next_run_then_gives_up_after_two_retries():
    svc, p, db = make()
    p.fail = {"moderate"}
    assert run(svc.reconcile("u")) == "retry"                 # day 1: attempt 1
    st = db.store["access"]
    assert st["liquidation_attempts"] == 1 and st["liquidation_failed"] is False
    assert "aggressive" not in p.strategies                   # the healthy one was sold
    assert st["last_partial_liquidation"]["strategies"] == ["aggressive"]
    assert p.strategies["moderate"]["is_paused"] is True      # failed one stays paused
    assert svc.alerts == []

    assert run(svc.reconcile("u")) == "retry"                 # day 2: retry 1
    assert run(svc.reconcile("u")) == "failed"                # day 3: retry 2 -> give up
    st = db.store["access"]
    assert st["liquidation_attempts"] == 3 and st["liquidation_failed"] is True
    assert "alpaca down" in st["last_liquidation_error"]
    assert len(svc.alerts) == 1 and svc.alerts[0]["uid"] == "u" and svc.alerts[0]["attempts"] == 3
    assert len(db.store["job_failures"]) == 1

    stops_before = len([c for c in p.calls if c[0] == "stop"])
    assert run(svc.reconcile("u")) == "failed"                # day 4: skipped, no new alert
    assert len([c for c in p.calls if c[0] == "stop"]) == stops_before
    assert len(svc.alerts) == 1


def test_success_after_a_failure_clears_attempts():
    svc, p, db = make()
    p.fail = {"moderate"}
    run(svc.reconcile("u"))
    p.fail = set()
    assert run(svc.reconcile("u")) == "liquidated"
    st = db.store["access"]
    assert st["liquidation_attempts"] == 0 and st["liquidation_failed"] is False
    assert st["last_liquidation"]["attempts"] == 2


def test_reset_lets_admin_retry_with_fresh_attempts():
    svc, p, db = make()
    p.fail = {"moderate"}
    for _ in range(3):
        run(svc.reconcile("u"))
    assert db.store["access"]["liquidation_failed"] is True
    db.store["access"].update({"liquidation_failed": False, "liquidation_attempts": 0})   # what reset_failure writes
    p.fail = set()
    assert run(svc.reconcile("u")) == "liquidated"


def test_whole_step_exception_counts_as_attempt():
    svc, p, db = make()
    async def boom():
        raise RuntimeError("prices unavailable")
    svc.prices.get_all = boom
    assert run(svc.reconcile("u")) == "retry"
    assert "prices unavailable" in db.store["access"]["last_liquidation_error"]


def test_regaining_access_clears_failed_state():
    svc, p, db = make()
    p.fail = {"moderate"}
    for _ in range(3):
        run(svc.reconcile("u"))
    svc.access = Access(autotrader=True)
    assert run(svc.reconcile("u")) == "restored"
    assert db.store["access"]["liquidation_failed"] is False
    assert p.strategies["moderate"]["is_paused"] is False


def test_run_job_level_failure_alerts():
    svc, p, db = make()
    def broken():
        raise RuntimeError("firestore down")
    svc._uids_with_strategies = broken
    with pytest.raises(RuntimeError):
        run(svc.run())
    assert svc.alerts and svc.alerts[0]["status"] == "job_failed"


def test_run_for_specific_uids():
    svc, p, db = make()
    assert run(svc.run(["u"])) == {"users": 1, "liquidated": 1}
