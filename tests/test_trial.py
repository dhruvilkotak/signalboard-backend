"""pytest tests/test_trial.py — trial start + feature gates (no Firebase)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import middleware.access as access_mw
import services.trial_service as ts

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


# ── start_trial ───────────────────────────────────────────────────────────

class Snap:
    def __init__(self, d):
        self._d = d

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return self._d


class Ref:
    def __init__(self, store):
        self.store = store

    def collection(self, _):
        return self

    def document(self, _):
        return self

    def get(self):
        return Snap(self.store.get("doc"))

    def set(self, data, merge=False):
        cur = dict(self.store.get("doc") or {}) if merge else {}
        cur.update(data)
        self.store["doc"] = cur


class DB:
    def __init__(self, doc=None):
        self.store = {"doc": doc}

    def collection(self, _):
        return Ref(self.store)


@pytest.fixture(autouse=True)
def _ts(monkeypatch):
    monkeypatch.setattr(ts.firestore, "SERVER_TIMESTAMP", "ts", raising=False)


def test_start_trial_sets_seven_days():
    db = DB()
    ends = ts.start_trial(db, "u", now=NOW)
    assert ends == NOW + timedelta(days=7)
    assert db.store["doc"]["trial_ends_at"] == ends
    assert db.store["doc"]["trial_source"] == "approval"


def test_start_trial_is_idempotent():
    first = NOW - timedelta(days=30)
    db = DB({"trial_ends_at": first, "coupon_until": "keep"})
    assert ts.start_trial(db, "u", now=NOW) == first
    assert db.store["doc"] == {"trial_ends_at": first, "coupon_until": "keep"}


def test_start_trial_keeps_other_fields():
    db = DB({"coupon_until": "keep"})
    ts.start_trial(db, "u", now=NOW)
    assert db.store["doc"]["coupon_until"] == "keep"


# ── require_feature / require_feature_optional ────────────────────────────

class Access:
    def __init__(self, features):
        self.features = features

    def get_access(self, uid, use_cache=True):
        return {"plan": "free", "features": self.features, "ondemand": {}, "trial_ends_at": None}


class Flags:
    def __init__(self, public):
        self.public = public

    def resolve_public(self):
        return self.public


@pytest.fixture
def wire(monkeypatch):
    def _wire(features, public=None):
        monkeypatch.setattr(access_mw, "access_svc", Access(features))
        monkeypatch.setattr(access_mw, "flag_svc", Flags(public or {}))
    return _wire


def test_require_feature_allows_and_blocks(wire):
    wire({"ai_chat": True})
    assert access_mw.require_feature("ai_chat")(user={"uid": "u"}) == {"uid": "u"}
    wire({"ai_chat": False})
    with pytest.raises(HTTPException) as e:
        access_mw.require_feature("ai_chat")(user={"uid": "u"})
    assert e.value.status_code == 402 and e.value.detail["feature"] == "ai_chat"


def test_optional_guest_allowed_while_flag_not_fully_on(wire):
    wire({}, public={"trial": False})
    assert access_mw.require_feature_optional("ai_signals")(user=None) is None


def test_optional_guest_blocked_once_flag_on(wire):
    wire({}, public={"trial": True})
    with pytest.raises(HTTPException) as e:
        access_mw.require_feature_optional("ai_signals")(user=None)
    assert e.value.status_code == 401


def test_optional_signed_in_user_checked(wire):
    wire({"ai_signals": False})
    with pytest.raises(HTTPException) as e:
        access_mw.require_feature_optional("ai_signals")(user={"uid": "u"})
    assert e.value.status_code == 402


def test_unknown_feature_rejected():
    with pytest.raises(ValueError):
        access_mw.require_feature("nope")
