"""pytest tests/test_feature_flags.py  — no Firestore needed (fake client)."""

import pytest

from services.feature_flags import FLAG_NAMES, FeatureFlagService


class FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class FakeDoc:
    def __init__(self, store):
        self.store = store
        self.reads = 0
        self.fail = False

    def get(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError("firestore down")
        return FakeSnap(self.store.get("data"))

    def set(self, data, merge=False):
        cur = dict(self.store.get("data") or {})
        cur.update(data)
        self.store["data"] = cur


class FakeDB:
    def __init__(self, data=None):
        self.store = {"data": data}
        self.doc = FakeDoc(self.store)

    def collection(self, _):
        return self

    def document(self, _):
        return self.doc


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _no_server_timestamp(monkeypatch):
    import services.feature_flags as ff
    monkeypatch.setattr(ff.firestore, "SERVER_TIMESTAMP", "ts", raising=False)


def make(data=None, clock=None):
    db = FakeDB(data)
    return FeatureFlagService(db=db, clock=clock or Clock()), db


def test_missing_doc_means_everything_off():
    svc, _ = make(None)
    assert svc.resolve_for("u1") == {n: False for n in FLAG_NAMES}


def test_modes():
    svc, _ = make({
        "paywall": {"mode": "on"},
        "trial": {"mode": "allowlist", "uids": ["me"]},
        "billing": {"mode": "off", "uids": ["me"]},
    })
    assert svc.is_enabled("paywall", "anyone")
    assert svc.is_enabled("trial", "me")
    assert not svc.is_enabled("trial", "someone-else")
    assert not svc.is_enabled("trial", None)
    assert not svc.is_enabled("billing", "me")


def test_garbage_and_unknown_resolve_off():
    svc, _ = make({"paywall": {"mode": "ON!!"}, "coupons": "yes"})
    assert not svc.is_enabled("paywall", "u")
    assert not svc.is_enabled("coupons", "u")
    assert not svc.is_enabled("not_a_flag", "u")


def test_public_only_counts_fully_on():
    svc, _ = make({"terms_gate": {"mode": "on"}, "paywall": {"mode": "allowlist", "uids": ["me"]}})
    pub = svc.resolve_public()
    assert pub["terms_gate"] is True
    assert pub["paywall"] is False


def test_cache_ttl():
    clock = Clock()
    svc, db = make({"paywall": {"mode": "on"}}, clock)
    svc.is_enabled("paywall")
    svc.is_enabled("trial")
    assert db.doc.reads == 1
    clock.t = 61
    svc.is_enabled("paywall")
    assert db.doc.reads == 2


def test_firestore_error_serves_last_good_then_off():
    clock = Clock()
    svc, db = make({"paywall": {"mode": "on"}}, clock)
    assert svc.is_enabled("paywall")
    db.doc.fail = True
    clock.t = 120
    assert svc.is_enabled("paywall")  # stale copy kept

    cold, cold_db = make({"paywall": {"mode": "on"}})
    cold_db.doc.fail = True
    assert not cold.is_enabled("paywall")  # nothing cached -> off


def test_set_flag_writes_and_invalidates():
    svc, db = make(None)
    assert not svc.is_enabled("billing", "me")
    svc.set_flag("billing", "allowlist", [" me ", "me", "test"], actor_uid="admin")
    assert db.store["data"]["billing"]["uids"] == ["me", "test"]
    assert svc.is_enabled("billing", "me")


@pytest.mark.parametrize("name,mode", [("nope", "on"), ("billing", "maybe")])
def test_set_flag_validation(name, mode):
    svc, _ = make(None)
    with pytest.raises(ValueError):
        svc.set_flag(name, mode, [], actor_uid="admin")


def test_no_db_until_set_db_means_off():
    svc = FeatureFlagService()
    assert svc.resolve_for("u") == {n: False for n in FLAG_NAMES}
    with pytest.raises(RuntimeError):
        svc.set_flag("billing", "on", [], actor_uid="admin")
    db = FakeDB({"billing": {"mode": "on"}})
    svc.set_db(db)
    assert svc.is_enabled("billing", "u")