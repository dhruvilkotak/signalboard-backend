"""pytest tests/test_legal_service.py — in-memory fake Firestore."""

import itertools

import pytest

import services.legal_service as ls
from services.legal_service import LegalService, TermsVersionMismatch

_ids = itertools.count()


class Snap:
    def __init__(self, data):
        self._d = data

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class Doc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def get(self):
        return Snap(self.store.get(self.path))

    def set(self, data, merge=False):
        cur = dict(self.store.get(self.path) or {}) if merge else {}
        cur.update(data)
        self.store[self.path] = cur

    def collection(self, name):
        return Col(self.store, f"{self.path}/{name}")


class Col:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id=None):
        return Doc(self.store, f"{self.path}/{doc_id or f'auto{next(_ids)}'}")


class Batch:
    def __init__(self):
        self.ops = []

    def set(self, ref, data, merge=False):
        self.ops.append((ref, data, merge))

    def commit(self):
        for ref, data, merge in self.ops:
            ref.set(data, merge=merge)


class DB:
    def __init__(self, store=None):
        self.store = store or {}

    def collection(self, name):
        return Col(self.store, name)

    def batch(self):
        return Batch()


@pytest.fixture(autouse=True)
def _ts(monkeypatch):
    monkeypatch.setattr(ls.firestore, "SERVER_TIMESTAMP", "ts", raising=False)


def test_defaults_to_v1_when_no_config():
    assert LegalService(DB()).get_current()["terms_version"] == "v1.0"


def test_existing_v1_signup_needs_nothing():
    db = DB({"users/u1": {"agreed_terms_version": "v1.0"}})
    st = LegalService(db).status("u1")
    assert st["accepted_version"] == "v1.0" and st["needs_acceptance"] is False


def test_unknown_user_needs_acceptance():
    assert LegalService(DB()).status("ghost")["needs_acceptance"] is True


def test_publish_then_accept_flow():
    db = DB({"users/u1": {"agreed_terms_version": "v1.0"}})
    svc = LegalService(db)
    svc.publish("v1.1", "Adds subscription terms", actor_uid="admin")
    assert svc.status("u1")["needs_acceptance"] is True

    with pytest.raises(TermsVersionMismatch):
        svc.accept("u1", "v1.0")

    svc.accept("u1", "v1.1", platform="web", user_agent="UA")
    st = svc.status("u1")
    assert st["accepted_version"] == "v1.1" and st["needs_acceptance"] is False
    assert db.store["users/u1/data/consent"]["terms_version"] == "v1.1"
    assert db.store["users/u1"]["agreed_terms_version"] == "v1.1"
    logs = [k for k in db.store if k.startswith("users/u1/consent_log/")]
    assert len(logs) == 1


@pytest.mark.parametrize("version,summary", [("1.1", "x"), ("v1.0", "x"), ("v1.1", "  ")])
def test_publish_validation(version, summary):
    with pytest.raises(ValueError):
        LegalService(DB()).publish(version, summary, actor_uid="admin")


def test_no_db():
    svc = LegalService()
    assert svc.get_current()["terms_version"] == "v1.0"
    with pytest.raises(RuntimeError):
        svc.accept("u", "v1.0")
