"""pytest tests/test_access_service.py — no Firebase needed."""

from datetime import datetime, timedelta, timezone

import pytest

from services.access_service import ADMIN_UID, FEATURES, AccessService

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


class Flags:
    def __init__(self, **on):
        self.on = on

    def is_enabled(self, name, uid=None):
        return self.on.get(name, False)


class Snap:
    def __init__(self, id_, data):
        self.id, self._d = id_, data

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return self._d


class Ref:
    def __init__(self, name):
        self.name = name

    def collection(self, _):
        return self

    def document(self, name):
        return Ref(name)


class DB:
    def __init__(self, access=None, subscription=None, usage=None):
        self.docs = {"access": access, "subscription": subscription, "usage": usage}
        self.calls = 0

    def collection(self, _):
        return Ref("users")

    def get_all(self, refs):
        self.calls += 1
        return [Snap(r.name, self.docs.get(r.name)) for r in refs]


def svc(flags=None, role="user", **docs):
    return AccessService(
        flags or Flags(),
        db=DB(**docs),
        role_lookup=lambda uid: role,
        now=lambda: NOW,
    )


ALL = {f: True for f in FEATURES}


def test_flags_off_is_legacy_everything():
    a = svc().get_access("u")
    assert a["plan"] == "legacy"
    assert a["features"] == ALL
    assert a["ondemand"]["limit"] is None


def test_admin_uid_always_admin():
    a = svc(Flags(paywall=True, trial=True)).get_access(ADMIN_UID)
    assert a["plan"] == "admin" and a["features"] == ALL


def test_pro_needs_flag():
    assert svc(Flags(paywall=True, trial=True), role="pro").get_access("u")["plan"] == "free"
    a = svc(Flags(pro_role=True, paywall=True, trial=True), role="pro").get_access("u")
    assert a["plan"] == "pro" and a["features"] == ALL


def test_paid_subscription():
    a = svc(Flags(paywall=True, trial=True), subscription={"status": "active"}).get_access("u")
    assert a["plan"] == "paid" and a["features"] == ALL


def test_past_due_is_not_paid():
    a = svc(Flags(paywall=True, trial=True), subscription={"status": "past_due"}).get_access("u")
    assert a["plan"] == "free"


def test_coupon_active_and_expired():
    on = Flags(paywall=True, trial=True)
    assert svc(on, access={"coupon_until": NOW + timedelta(days=3)}).get_access("u")["plan"] == "coupon"
    assert svc(on, access={"coupon_until": NOW - timedelta(seconds=1)}).get_access("u")["plan"] == "free"


def test_trial_active_gets_trial_features_and_quota():
    a = svc(Flags(paywall=True, trial=True),
            access={"trial_ends_at": NOW + timedelta(days=2)}, usage={"ondemand_used": 2}).get_access("u")
    assert a["plan"] == "trial"
    assert a["features"]["ai_chat"] and a["features"]["autotrader"] and a["features"]["ai_signals"]
    assert a["features"]["ondemand"] is True
    assert a["ondemand"] == {"used": 2, "limit": 5, "left": 3}


def test_trial_expired_and_quota_used_up():
    a = svc(Flags(paywall=True, trial=True),
            access={"trial_ends_at": NOW - timedelta(days=1)}, usage={"ondemand_used": 5}).get_access("u")
    assert a["plan"] == "free"
    assert not any(a["features"].values())
    assert a["ondemand"]["left"] == 0


def test_only_paywall_on_leaves_ai_features_open():
    a = svc(Flags(paywall=True), usage={"ondemand_used": 5}).get_access("u")
    assert a["features"]["ondemand"] is False
    assert a["features"]["ai_chat"] is True


def test_only_trial_on_leaves_ondemand_open():
    a = svc(Flags(trial=True)).get_access("u")
    assert a["features"]["ondemand"] is True
    assert a["features"]["ai_chat"] is False


def test_iso_string_dates_accepted():
    a = svc(Flags(trial=True), access={"trial_ends_at": (NOW + timedelta(hours=1)).isoformat()}).get_access("u")
    assert a["plan"] == "trial"


def test_no_db_is_legacy_when_flags_off_and_free_when_on():
    s = AccessService(Flags(), role_lookup=lambda u: "user", now=lambda: NOW)
    assert s.get_access("u")["plan"] == "legacy"
    s2 = AccessService(Flags(trial=True), role_lookup=lambda u: "user", now=lambda: NOW)
    assert s2.get_access("u")["features"]["ai_chat"] is False


def test_role_lookup_failure_falls_back_to_user():
    def boom(uid):
        raise RuntimeError("auth down")
    s = AccessService(Flags(pro_role=True), db=DB(), role_lookup=boom, now=lambda: NOW)
    assert s.get_role("u") == "user"


def test_access_cache_and_bypass():
    s = svc()
    s.get_access("u")
    s.get_access("u")
    assert s._db.calls == 1
    s.get_access("u", use_cache=False)
    assert s._db.calls == 2


def test_invalid_role_rejected():
    with pytest.raises(ValueError):
        svc().set_role("u", "admin", actor_uid="a")
    with pytest.raises(ValueError):
        svc().set_role(ADMIN_UID, "user", actor_uid="a")
