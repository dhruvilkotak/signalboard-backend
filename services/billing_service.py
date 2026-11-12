"""
BillingService — Stripe Checkout (subscribe), Customer Portal (manage/cancel)
and webhooks (the ONLY thing that changes subscription status).

Env (systemd EnvironmentFile; test keys until launch):
    STRIPE_SECRET_KEY        sk_test_... / sk_live_...
    STRIPE_WEBHOOK_SECRET    whsec_...
    STRIPE_PRICE_ID          price_... ($20/month recurring)
    FRONTEND_URL             https://signalboard-frontend.vercel.app
    STRIPE_TOS_CONSENT       "1" to require the Terms checkbox in Checkout
                             (needs a Terms URL in Stripe → Settings → Public details)

Firestore (server-only):
    users/{uid}/data/subscription   stripe_customer_id, subscription_id, status,
                                    current_period_end, cancel_at_period_end, price_id
    billing_customers/{cus_id}      uid                (customer → user lookup)
    billing_events/{evt_id}         type, processed_at (webhook idempotency)

AccessService treats status "active" / "trialing" as plan "paid". When a
subscription ends, the access_expiry job handles the Auto-Trader like any
other loss of access.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from firebase_admin import firestore

logger = logging.getLogger(__name__)

ACTIVE = ("active", "trialing")


class BillingError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _get(obj, key, default=None):
    """Works on StripeObject and plain dicts (tests)."""
    try:
        v = obj[key]
        return default if v is None else v
    except (KeyError, TypeError, AttributeError):
        return default


def subscription_doc(sub) -> dict:
    """Fields we keep from a Stripe Subscription object."""
    period_end = _get(sub, "current_period_end")
    items = _get(_get(sub, "items", {}), "data", []) or []
    if period_end is None and items:                      # newer API versions: per item
        period_end = _get(items[0], "current_period_end")
    price_id = _get(_get(items[0], "price", {}), "id") if items else None
    return {
        "subscription_id": _get(sub, "id"),
        "status": _get(sub, "status"),
        "current_period_end": (datetime.fromtimestamp(int(period_end), tz=timezone.utc)
                               if period_end else None),
        "cancel_at_period_end": bool(_get(sub, "cancel_at_period_end", False)),
        "price_id": price_id,
    }


class BillingService:
    def __init__(self, access_svc, db=None, stripe_module=stripe):
        self._access = access_svc
        self._db = db
        self.stripe = stripe_module
        self.stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        self.price_id = os.getenv("STRIPE_PRICE_ID")
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        self.frontend = os.getenv("FRONTEND_URL", "https://signalboard-frontend.vercel.app").rstrip("/")
        self.tos_consent = os.getenv("STRIPE_TOS_CONSENT", "") == "1"

    def set_db(self, db) -> None:
        self._db = db

    @property
    def mode(self) -> str:
        key = self.stripe.api_key or ""
        return "live" if key.startswith("sk_live") else "test" if key.startswith("sk_test") else "unconfigured"

    def _need(self):
        if self._db is None or not self.stripe.api_key or not self.price_id:
            raise BillingError("BILLING_UNAVAILABLE", "Billing isn't configured yet.", 503)

    # ── Firestore helpers ────────────────────────────────────────────────

    def _sub_ref(self, uid: str):
        return self._db.collection("users").document(uid).collection("data").document("subscription")

    def _get_sub(self, uid: str) -> dict:
        snap = self._sub_ref(uid).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def _uid_for_customer(self, customer_id: Optional[str]) -> Optional[str]:
        if not customer_id:
            return None
        snap = self._db.collection("billing_customers").document(customer_id).get()
        return (snap.to_dict() or {}).get("uid") if snap.exists else None

    def _link_customer(self, uid: str, customer_id: str) -> None:
        self._db.collection("billing_customers").document(customer_id).set({"uid": uid})
        self._sub_ref(uid).set({"stripe_customer_id": customer_id}, merge=True)

    def _write_subscription(self, uid: str, sub) -> dict:
        doc = {**subscription_doc(sub), "updated_at": firestore.SERVER_TIMESTAMP}
        customer = _get(sub, "customer")
        if customer:
            doc["stripe_customer_id"] = customer
        self._sub_ref(uid).set(doc, merge=True)
        self._access.invalidate(uid)
        logger.info("subscription_synced", extra={"event": "subscription_synced", "uid": uid,
                                                   "status": doc["status"], "subscription_id": doc["subscription_id"]})
        return doc

    # ── user actions ─────────────────────────────────────────────────────

    def _customer_for(self, uid: str, email: Optional[str]) -> str:
        existing = self._get_sub(uid).get("stripe_customer_id")
        if existing:
            return existing
        customer = self.stripe.Customer.create(email=email, metadata={"uid": uid})
        self._link_customer(uid, customer["id"])
        return customer["id"]

    def create_checkout(self, uid: str, email: Optional[str]) -> str:
        self._need()
        if self._get_sub(uid).get("status") in ACTIVE:
            raise BillingError("ALREADY_SUBSCRIBED", "You already have an active subscription.", 409)
        customer = self._customer_for(uid, email)
        params = {
            "mode": "subscription",
            "customer": customer,
            "line_items": [{"price": self.price_id, "quantity": 1}],
            "client_reference_id": uid,
            "subscription_data": {"metadata": {"uid": uid}},
            "allow_promotion_codes": True,
            "success_url": f"{self.frontend}/?billing=success",
            "cancel_url": f"{self.frontend}/?billing=cancel",
        }
        if self.tos_consent:
            params["consent_collection"] = {"terms_of_service": "required"}
        session = self.stripe.checkout.Session.create(**params)
        logger.info("checkout_started", extra={"event": "checkout_started", "uid": uid, "mode": self.mode})
        return session["url"]

    def create_portal(self, uid: str) -> str:
        self._need()
        customer = self._get_sub(uid).get("stripe_customer_id")
        if not customer:
            raise BillingError("NO_CUSTOMER", "No billing account yet. Subscribe first.", 404)
        session = self.stripe.billing_portal.Session.create(customer=customer, return_url=f"{self.frontend}/")
        return session["url"]

    # ── webhooks ─────────────────────────────────────────────────────────

    def construct_event(self, payload: bytes, signature: Optional[str]):
        if not self.webhook_secret:
            raise BillingError("WEBHOOK_UNCONFIGURED", "Webhook secret not set.", 503)
        try:
            return self.stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
        except Exception as e:                              # bad payload or bad signature
            raise BillingError("BAD_SIGNATURE", f"Invalid webhook: {type(e).__name__}", 400)

    def _claim_event(self, event_id: str, event_type: str) -> bool:
        """First delivery wins; Stripe retries are ignored."""
        ref = self._db.collection("billing_events").document(event_id)
        try:
            ref.create({"type": event_type, "processed_at": firestore.SERVER_TIMESTAMP})
            return True
        except Exception as e:
            if type(e).__name__ in ("AlreadyExists", "Conflict"):
                return False
            raise

    def handle_event(self, event) -> str:
        if self._db is None:
            raise BillingError("BILLING_UNAVAILABLE", "Billing isn't configured yet.", 503)
        etype, obj = event["type"], event["data"]["object"]
        if not self._claim_event(event["id"], etype):
            return "duplicate"
        try:
            return self._dispatch(etype, obj)
        except Exception:
            # Let Stripe retry: release the claim so the retry is processed.
            self._db.collection("billing_events").document(event["id"]).delete()
            raise

    def _dispatch(self, etype: str, obj) -> str:
        if etype == "checkout.session.completed":
            uid = _get(obj, "client_reference_id")
            customer, sub_id = _get(obj, "customer"), _get(obj, "subscription")
            if not uid:
                return "ignored_no_uid"
            if customer:
                self._link_customer(uid, customer)
            if sub_id:
                self._write_subscription(uid, self.stripe.Subscription.retrieve(sub_id))
            return "checkout_completed"

        if etype in ("customer.subscription.created", "customer.subscription.updated",
                     "customer.subscription.deleted"):
            uid = _get(_get(obj, "metadata", {}), "uid") or self._uid_for_customer(_get(obj, "customer"))
            if not uid:
                return "ignored_unknown_customer"
            self._write_subscription(uid, obj)
            return etype.rsplit(".", 1)[1]

        if etype == "invoice.payment_failed":
            uid = self._uid_for_customer(_get(obj, "customer"))
            sub_id = _get(obj, "subscription") or _get(
                _get(_get(obj, "parent", {}), "subscription_details", {}), "subscription")
            if uid and sub_id:
                self._write_subscription(uid, self.stripe.Subscription.retrieve(sub_id))
            return "payment_failed"

        if etype == "charge.refunded":
            if not _get(obj, "refunded"):                    # partial refund: keep access
                return "partial_refund"
            customer = _get(obj, "customer")
            uid = self._uid_for_customer(customer)
            if not uid:
                return "ignored_unknown_customer"
            subs = self.stripe.Subscription.list(customer=customer, status="active")
            for sub in _get(subs, "data", []) or []:
                self.stripe.Subscription.cancel(sub["id"])     # .deleted webhook syncs the status
            logger.info("refund_cancelled_subscription", extra={"event": "refund_cancelled_subscription", "uid": uid})
            return "refund_cancelled"

        return "ignored"
