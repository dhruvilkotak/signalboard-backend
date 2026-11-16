"""pytest tests/test_billing.py — Stripe flows with a fake Stripe + fake Firestore."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import services.billing_service as bs
from services.billing_service import BillingError, BillingService, subscription_doc


# ── fakes ─────────────────────────────────────────────────────────────────

class AlreadyExists(Exception):
    pass


class Snap:
    def __init__(self, d):
        self._d = d

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return self._d


class Doc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def get(self):
        return Snap(self.store.get(self.path))

    def set(self, data, merge=False):
        cur = dict(self.store.get(self.path) or {}) if merge else {}
        cur.update(data)
        self.store[self.path] = cur

    def create(self, data):
        if self.path in self.store:
            raise AlreadyExists()
        self.store[self.path] = dict(data)

    def delete(self):
        self.store.pop(self.path, None)

    def collection(self, name):
        return Col(self.store, f"{self.path}/{name}")


class Col:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id):
        return Doc(self.store, f"{self.path}/{doc_id}")


class DB:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return Col(self.store, name)


SUB = {"id": "sub_1", "status": "active", "customer": "cus_1", "cancel_at_period_end": False,
       "metadata": {"uid": "u1"},
       "items": {"data": [{"current_period_end": 1800000000, "price": {"id": "price_20"}}]}}


def fake_stripe():
    calls = []
    s = SimpleNamespace(api_key=None, calls=calls)
    s.Customer = SimpleNamespace(create=lambda **kw: calls.append(("customer", kw)) or {"id": "cus_1"})
    s.checkout = SimpleNamespace(Session=SimpleNamespace(
        create=lambda **kw: calls.append(("checkout", kw)) or {"url": "https://checkout.stripe.test/s"}))
    s.billing_portal = SimpleNamespace(Session=SimpleNamespace(
        create=lambda **kw: calls.append(("portal", kw)) or {"url": "https://billing.stripe.test/p"}))
    s.Subscription = SimpleNamespace(
        retrieve=lambda sid: dict(SUB, id=sid),
        list=lambda **kw: {"data": [{"id": "sub_1"}]},
        cancel=lambda sid: calls.append(("cancel", sid)))
    s.Webhook = bs.stripe.Webhook
    return s


class Access:
    def __init__(self):
        self.invalidated = []

    def invalidate(self, uid=None):
        self.invalidated.append(uid)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # A developer's local .env (loaded by config/main) may hold real Stripe settings;
    # clear every billing variable so these tests only see what they set.
    for var in ("STRIPE_PRICE_ID", "STRIPE_PRICE_MONTHLY", "STRIPE_PRICE_ANNUAL", "STRIPE_PRICE_PASS",
                "STRIPE_PASS_DAYS", "STRIPE_AUTOMATIC_TAX", "STRIPE_TOS_CONSENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_20")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("FRONTEND_URL", "https://app.test/")
    monkeypatch.setattr(bs.firestore, "SERVER_TIMESTAMP", "ts", raising=False)


def make():
    db, s = DB(), fake_stripe()
    svc = BillingService(Access(), db=db, stripe_module=s)
    return svc, db, s


def event(etype, obj, eid="evt_1"):
    return {"id": eid, "type": etype, "data": {"object": obj}}


# ── tests ─────────────────────────────────────────────────────────────────

def test_subscription_doc_reads_item_period_end():
    d = subscription_doc(SUB)
    assert d["status"] == "active" and d["price_id"] == "price_20"
    assert d["current_period_end"].year == 2027


def test_mode():
    svc, _, _ = make()
    assert svc.mode == "test"


def test_checkout_creates_customer_once_and_uses_price():
    svc, db, s = make()
    assert svc.create_checkout("u1", "a@b.c") == "https://checkout.stripe.test/s"
    assert db.store["billing_customers/cus_1"] == {"uid": "u1", "mode": "test"}
    kw = [c for c in s.calls if c[0] == "checkout"][0][1]
    assert kw["line_items"] == [{"price": "price_20", "quantity": 1}]
    assert kw["client_reference_id"] == "u1" and kw["success_url"] == "https://app.test/?billing=success"
    assert "consent_collection" not in kw
    svc.create_checkout("u1", "a@b.c")
    assert len([c for c in s.calls if c[0] == "customer"]) == 1


def test_checkout_refused_when_already_active():
    svc, db, _ = make()
    db.store["users/u1/data/subscription"] = {"status": "active"}
    with pytest.raises(BillingError) as e:
        svc.create_checkout("u1", None)
    assert e.value.code == "ALREADY_SUBSCRIBED"


def test_portal_needs_customer():
    svc, db, _ = make()
    with pytest.raises(BillingError):
        svc.create_portal("u1")
    db.store["users/u1/data/subscription"] = {"stripe_customer_id": "cus_1"}
    assert svc.create_portal("u1").startswith("https://billing")


def test_checkout_completed_syncs_subscription():
    svc, db, _ = make()
    out = svc.handle_event(event("checkout.session.completed",
                                 {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_9"}))
    assert out == "checkout_completed"
    sub = db.store["users/u1/data/subscription"]
    assert sub["status"] == "active" and sub["subscription_id"] == "sub_9" and sub["stripe_customer_id"] == "cus_1"
    assert svc._access.invalidated == ["u1"]


def test_subscription_deleted_by_customer_lookup():
    svc, db, _ = make()
    db.store["billing_customers/cus_1"] = {"uid": "u1"}
    obj = dict(SUB, status="canceled", metadata={})
    assert svc.handle_event(event("customer.subscription.deleted", obj)) == "deleted"
    assert db.store["users/u1/data/subscription"]["status"] == "canceled"


def test_duplicate_event_ignored():
    svc, _, _ = make()
    e = event("customer.subscription.updated", SUB, eid="evt_dup")
    assert svc.handle_event(e) == "updated"
    assert svc.handle_event(e) == "duplicate"


def test_failed_handler_releases_claim(monkeypatch):
    svc, db, s = make()
    def boom(_):
        raise RuntimeError("stripe down")
    s.Subscription.retrieve = boom
    e = event("checkout.session.completed", {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"}, "evt_x")
    with pytest.raises(RuntimeError):
        svc.handle_event(e)
    assert "billing_events/evt_x" not in db.store


def test_full_refund_cancels_partial_keeps():
    svc, db, s = make()
    db.store["billing_customers/cus_1"] = {"uid": "u1"}
    assert svc.handle_event(event("charge.refunded", {"customer": "cus_1", "refunded": False}, "e1")) == "partial_refund"
    assert svc.handle_event(event("charge.refunded", {"customer": "cus_1", "refunded": True}, "e2")) == "refund_cancelled"
    assert ("cancel", "sub_1") in s.calls


def test_webhook_signature_real_verification():
    svc, _, _ = make()
    payload = json.dumps(event("ping", {})).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    ok = svc.construct_event(payload, f"t={ts},v1={sig}")
    assert ok["type"] == "ping"
    with pytest.raises(BillingError) as e:
        svc.construct_event(payload, f"t={ts},v1=deadbeef")
    assert e.value.code == "BAD_SIGNATURE"


# ── Stripe errors become readable 502s, not 500s ──────────────────────────

def test_stripe_error_on_checkout_is_502_with_reason():
    svc, db, s = make()
    def boom(**kw):
        raise bs.stripe.InvalidRequestError("No such price: 'price_20'", "line_items[0][price]",
                                            code="resource_missing")
    s.checkout.Session.create = boom
    with pytest.raises(BillingError) as e:
        svc.create_checkout("u1", "a@b.c")
    assert e.value.status == 502 and e.value.code == "STRIPE_ERROR"
    assert "No such price" in e.value.message


def test_missing_customer_is_recreated_once():
    svc, db, s = make()
    db.store["users/u1/data/subscription"] = {"stripe_customer_id": "cus_old"}
    tries = []
    def create(**kw):
        tries.append(kw["customer"])
        if kw["customer"] == "cus_old":
            raise bs.stripe.InvalidRequestError("No such customer: 'cus_old'", "customer", code="resource_missing")
        return {"url": "https://checkout.stripe.test/s"}
    s.checkout.Session.create = create
    assert svc.create_checkout("u1", "a@b.c").startswith("https://checkout")
    assert tries == ["cus_old", "cus_1"]
    assert db.store["users/u1/data/subscription"]["stripe_customer_id"] == "cus_1"


def test_customer_from_other_mode_is_replaced():
    svc, db, s = make()                                       # sk_test
    db.store["users/u1/data/subscription"] = {"stripe_customer_id": "cus_live", "stripe_customer_mode": "live"}
    svc.create_checkout("u1", "a@b.c")
    kw = [c for c in s.calls if c[0] == "checkout"][0][1]
    assert kw["customer"] == "cus_1"
    assert db.store["users/u1/data/subscription"]["stripe_customer_mode"] == "test"


def test_portal_stripe_error_is_502():
    svc, db, s = make()
    db.store["users/u1/data/subscription"] = {"stripe_customer_id": "cus_1"}
    def boom(**kw):
        raise bs.stripe.InvalidRequestError("You can't create a portal session in test mode until you save your customer portal settings", None)
    s.billing_portal.Session.create = boom
    with pytest.raises(BillingError) as e:
        svc.create_portal("u1")
    assert e.value.status == 502 and "portal" in e.value.message


# ── plans: monthly / annual (15% off) / one-time pass, tax on top ─────────

def make_plans(monkeypatch, tax=False):
    monkeypatch.setenv("STRIPE_PRICE_ANNUAL", "price_year")
    monkeypatch.setenv("STRIPE_PRICE_PASS", "price_pass")
    monkeypatch.setenv("STRIPE_PASS_DAYS", "30")
    if tax:
        monkeypatch.setenv("STRIPE_AUTOMATIC_TAX", "1")
    svc, db, s = make()
    amounts = {"price_20": (2117, {"interval": "month"}), "price_year": (21594, {"interval": "year"}),
               "price_pass": (2101, None)}
    s.Price = SimpleNamespace(retrieve=lambda pid: {"unit_amount": amounts[pid][0], "currency": "usd",
                                                    "recurring": amounts[pid][1]})
    s.PaymentIntent = SimpleNamespace(retrieve=lambda pid: {"metadata": {"plan": "pass", "uid": "u1"}})
    return svc, db, s


def checkout_kw(s):
    return [c for c in s.calls if c[0] == "checkout"][-1][1]


def test_plans_listed_with_amounts(monkeypatch):
    svc, _, _ = make_plans(monkeypatch)
    plans = {p["id"]: p for p in svc.plans()}
    assert plans["monthly"]["amount"] == 2117 and plans["monthly"]["interval"] == "month"
    assert plans["annual"]["interval"] == "year"
    assert plans["pass"]["interval"] is None and plans["pass"]["days"] == 30


def test_only_configured_plans_offered():
    svc, _, s = make()                                    # only STRIPE_PRICE_ID set
    s.Price = SimpleNamespace(retrieve=lambda pid: {"unit_amount": 2000, "currency": "usd",
                                                    "recurring": {"interval": "month"}})
    assert [p["id"] for p in svc.plans()] == ["monthly"]
    with pytest.raises(BillingError) as e:
        svc.create_checkout("u1", None, "annual")
    assert e.value.code == "UNKNOWN_PLAN"


def test_annual_is_a_subscription(monkeypatch):
    svc, _, s = make_plans(monkeypatch)
    svc.create_checkout("u1", "a@b.c", "annual")
    kw = checkout_kw(s)
    assert kw["mode"] == "subscription" and kw["line_items"][0]["price"] == "price_year"
    assert kw["subscription_data"]["metadata"] == {"uid": "u1", "plan": "annual"}


def test_pass_is_one_time_payment(monkeypatch):
    svc, _, s = make_plans(monkeypatch)
    svc.create_checkout("u1", "a@b.c", "pass")
    kw = checkout_kw(s)
    assert kw["mode"] == "payment" and kw["line_items"][0]["price"] == "price_pass"
    assert kw["payment_intent_data"]["metadata"]["days"] == "30"
    assert "subscription_data" not in kw and "allow_promotion_codes" not in kw


def test_tax_added_on_top_when_enabled(monkeypatch):
    svc, _, s = make_plans(monkeypatch, tax=True)
    svc.create_checkout("u1", "a@b.c", "monthly")
    kw = checkout_kw(s)
    assert kw["automatic_tax"] == {"enabled": True}
    assert kw["billing_address_collection"] == "required"
    assert kw["customer_update"] == {"address": "auto", "name": "auto"}


def test_no_tax_params_by_default(monkeypatch):
    svc, _, s = make_plans(monkeypatch)
    svc.create_checkout("u1", "a@b.c", "monthly")
    assert "automatic_tax" not in checkout_kw(s)


def pass_session(eid="evt_pass", days="30"):
    return event("checkout.session.completed", {
        "client_reference_id": "u1", "customer": "cus_1", "mode": "payment", "payment_status": "paid",
        "payment_intent": "pi_1", "amount_total": 2101, "metadata": {"plan": "pass", "days": days}}, eid)


def test_paid_pass_grants_access_and_extends(monkeypatch):
    svc, db, s = make_plans(monkeypatch)
    assert svc.handle_event(pass_session()) == "pass_granted"
    first = db.store["users/u1/data/subscription"]["paid_until"]
    remaining = first - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)
    assert svc.handle_event(pass_session("evt_pass2")) == "pass_granted"         # second pass stacks
    second = db.store["users/u1/data/subscription"]["paid_until"]
    assert second - first == timedelta(days=30)
    assert svc.handle_event(pass_session("evt_pass2")) == "duplicate"


def test_unpaid_pass_session_grants_nothing(monkeypatch):
    svc, db, _ = make_plans(monkeypatch)
    ev = pass_session()
    ev["data"]["object"]["payment_status"] = "unpaid"
    assert svc.handle_event(ev) == "pass_unpaid"
    assert "paid_until" not in (db.store.get("users/u1/data/subscription") or {})


def test_full_refund_of_pass_ends_access(monkeypatch):
    svc, db, s = make_plans(monkeypatch)
    svc.handle_event(pass_session())
    db.store["billing_customers/cus_1"] = {"uid": "u1"}
    out = svc.handle_event(event("charge.refunded", {"refunded": True, "customer": "cus_1",
                                                     "payment_intent": "pi_1"}, "evt_ref"))
    assert out == "pass_refunded"
    assert db.store["users/u1/data/subscription"]["paid_until"] <= datetime.now(timezone.utc)
    assert not [c for c in s.calls if c[0] == "cancel"]                      # no subscription touched
