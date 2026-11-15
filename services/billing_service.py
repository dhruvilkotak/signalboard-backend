"""
BillingService — Stripe Checkout, Customer Portal (manage/cancel anytime) and
webhooks (the ONLY thing that changes what a user has paid for).

Plans (each offered only if its price is configured):
    monthly  recurring monthly subscription            STRIPE_PRICE_MONTHLY (or STRIPE_PRICE_ID)
    annual   recurring yearly subscription (15% off)   STRIPE_PRICE_ANNUAL
    pass     one-time payment, N days, no auto-renew   STRIPE_PRICE_PASS, STRIPE_PASS_DAYS (30)
Subscriptions are cancelled in the Customer Portal and keep access until the
end of the paid period. Prices themselves (amounts, the 15% annual discount,
fees passed to the customer) are set in Stripe; see docs/stripe-setup.md.

Env (in .env; sandbox keys until launch):
    STRIPE_SECRET_KEY        sk_test_... / sk_live_...
    STRIPE_WEBHOOK_SECRET    whsec_...
    STRIPE_PRICE_MONTHLY / STRIPE_PRICE_ANNUAL / STRIPE_PRICE_PASS   price_...
    STRIPE_PASS_DAYS         days a one-time pass lasts (default 30)
    STRIPE_AUTOMATIC_TAX     "1" = Stripe Tax adds sales tax on top of the price
                             (needs Stripe Tax set up + prices "exclusive" of tax)
    FRONTEND_URL             https://signalboard-frontend.vercel.app
    STRIPE_TOS_CONSENT       "1" to require the Terms checkbox in Checkout
                             (needs a Terms URL in Stripe → Settings → Public details)

Firestore (server-only):
    users/{uid}/data/subscription   stripe_customer_id, subscription_id, status,
                                    current_period_end, cancel_at_period_end, price_id,
                                    paid_until, last_pass (one-time passes)
    billing_customers/{cus_id}      uid                (customer → user lookup)
    billing_events/{evt_id}         type, processed_at (webhook idempotency)

AccessService treats status "active" / "trialing", or paid_until in the
future, as plan "paid". When a
subscription ends, the access_expiry job handles the Auto-Trader like any
other loss of access.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe

StripeError = getattr(stripe, "StripeError", None) or stripe.error.StripeError
from firebase_admin import firestore

logger = logging.getLogger(__name__)

ACTIVE = ("active", "trialing")
PLANS = ("monthly", "annual", "pass")
PRICE_CACHE_SECONDS = 600


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
        self.prices = {k: v for k, v in {
            "monthly": os.getenv("STRIPE_PRICE_MONTHLY") or os.getenv("STRIPE_PRICE_ID"),
            "annual": os.getenv("STRIPE_PRICE_ANNUAL"),
            "pass": os.getenv("STRIPE_PRICE_PASS"),
        }.items() if v}
        self.pass_days = int(os.getenv("STRIPE_PASS_DAYS", "30") or 30)
        self.automatic_tax = os.getenv("STRIPE_AUTOMATIC_TAX", "") == "1"
        self._price_cache: dict = {}
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        self.frontend = os.getenv("FRONTEND_URL", "https://signalboard-frontend.vercel.app").rstrip("/")
        self.tos_consent = os.getenv("STRIPE_TOS_CONSENT", "") == "1"

    def set_db(self, db) -> None:
        self._db = db

    @property
    def mode(self) -> str:
        key = self.stripe.api_key or ""
        return "live" if key.startswith("sk_live") else "test" if key.startswith("sk_test") else "unconfigured"

    @property
    def price_id(self) -> Optional[str]:            # monthly; kept for older callers
        return self.prices.get("monthly")

    def _need(self):
        if self._db is None or not self.stripe.api_key or not self.prices:
            raise BillingError("BILLING_UNAVAILABLE", "Billing isn't configured yet.", 503)

    def plans(self) -> list[dict]:
        """Configured plans with their Stripe amounts, for the paywall. Cached 10 min."""
        out = []
        for plan in PLANS:
            pid = self.prices.get(plan)
            if not pid:
                continue
            cached = self._price_cache.get(pid)
            if not cached or time.time() - cached[0] > PRICE_CACHE_SECONDS:
                try:
                    price = self.stripe.Price.retrieve(pid)
                except StripeError as e:
                    logger.warning("stripe_price_lookup_failed", extra={"event": "stripe_price_lookup_failed",
                                                                        "plan": plan, "stripe_message": str(e)})
                    continue
                recurring = _get(price, "recurring") or {}
                cached = (time.time(), {
                    "amount": _get(price, "unit_amount"),          # cents
                    "currency": _get(price, "currency", "usd"),
                    "interval": _get(recurring, "interval"),       # month | year | None (one-time)
                })
                self._price_cache[pid] = cached
            out.append({"id": plan, **cached[1], "days": self.pass_days if plan == "pass" else None})
        return out

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
        self._db.collection("billing_customers").document(customer_id).set({"uid": uid, "mode": self.mode})
        self._sub_ref(uid).set({"stripe_customer_id": customer_id, "stripe_customer_mode": self.mode}, merge=True)

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

    def _customer_for(self, uid: str, email: Optional[str], fresh: bool = False) -> str:
        sub = self._get_sub(uid)
        existing = sub.get("stripe_customer_id")
        # A customer made with the live key doesn't exist in sandbox (and vice versa).
        same_mode = sub.get("stripe_customer_mode") in (None, self.mode)
        if existing and same_mode and not fresh:
            return existing
        customer = self.stripe.Customer.create(email=email, metadata={"uid": uid})
        self._link_customer(uid, customer["id"])
        logger.info("stripe_customer_created", extra={"event": "stripe_customer_created", "uid": uid,
                                                      "mode": self.mode, "replaced": existing})
        return customer["id"]

    def _stripe_failed(self, action: str, uid: str, e: Exception) -> BillingError:
        """Log Stripe's real reason; return a readable error instead of a 500."""
        msg = getattr(e, "user_message", None) or str(e) or type(e).__name__
        logger.error("stripe_error", extra={"event": "stripe_error", "action": action, "uid": uid,
                                            "mode": self.mode, "type": type(e).__name__,
                                            "code": getattr(e, "code", None),
                                            "request_id": getattr(e, "request_id", None), "stripe_message": str(e)})
        return BillingError("STRIPE_ERROR", f"Couldn't {action}: {msg}", 502)

    def create_checkout(self, uid: str, email: Optional[str], plan: str = "monthly") -> str:
        self._need()
        if plan not in self.prices:
            raise BillingError("UNKNOWN_PLAN", f"The {plan} plan isn't available.", 400)
        if self._get_sub(uid).get("status") in ACTIVE:
            raise BillingError("ALREADY_SUBSCRIBED", "You already have an active subscription.", 409)
        try:
            customer = self._customer_for(uid, email)
            try:
                session = self.stripe.checkout.Session.create(**self._checkout_params(uid, customer, plan))
            except StripeError as e:
                if getattr(e, "code", None) != "resource_missing" or getattr(e, "param", None) != "customer":
                    raise
                # Stored customer is gone (deleted, or from the other mode): make a new one once.
                customer = self._customer_for(uid, email, fresh=True)
                session = self.stripe.checkout.Session.create(**self._checkout_params(uid, customer, plan))
        except StripeError as e:
            raise self._stripe_failed("start checkout", uid, e)
        logger.info("checkout_started", extra={"event": "checkout_started", "uid": uid, "mode": self.mode,
                                               "plan": plan})
        return session["url"]

    def _checkout_params(self, uid: str, customer: str, plan: str = "monthly") -> dict:
        meta = {"uid": uid, "plan": plan}
        params = {
            "customer": customer,
            "line_items": [{"price": self.prices[plan], "quantity": 1}],
            "client_reference_id": uid,
            "metadata": meta,
            "success_url": f"{self.frontend}/?billing=success",
            "cancel_url": f"{self.frontend}/?billing=cancel",
        }
        if plan == "pass":
            params["mode"] = "payment"
            params["payment_intent_data"] = {"metadata": {**meta, "days": str(self.pass_days)}}
        else:
            params["mode"] = "subscription"
            params["subscription_data"] = {"metadata": meta}
            params["allow_promotion_codes"] = True
        if self.automatic_tax:
            # Sales tax is added on top (prices are tax-exclusive); Stripe needs the address.
            params["automatic_tax"] = {"enabled": True}
            params["billing_address_collection"] = "required"
            params["customer_update"] = {"address": "auto", "name": "auto"}
        if self.tos_consent:
            params["consent_collection"] = {"terms_of_service": "required"}
        return params

    def create_portal(self, uid: str) -> str:
        self._need()
        customer = self._get_sub(uid).get("stripe_customer_id")
        if not customer:
            raise BillingError("NO_CUSTOMER", "No billing account yet. Subscribe first.", 404)
        try:
            session = self.stripe.billing_portal.Session.create(customer=customer, return_url=f"{self.frontend}/")
        except StripeError as e:
            raise self._stripe_failed("open the billing portal", uid, e)
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
            if _get(obj, "mode") == "payment":
                if _get(obj, "payment_status") != "paid":
                    return "pass_unpaid"
                return self._grant_pass(uid, obj)
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
            pi_id = _get(obj, "payment_intent")
            if pi_id:
                pi_meta = _get(self.stripe.PaymentIntent.retrieve(pi_id), "metadata", {}) or {}
                if _get(pi_meta, "plan") == "pass":
                    return self._revoke_pass(uid, pi_id)
            subs = self.stripe.Subscription.list(customer=customer, status="active")
            for sub in _get(subs, "data", []) or []:
                self.stripe.Subscription.cancel(sub["id"])     # .deleted webhook syncs the status
            logger.info("refund_cancelled_subscription", extra={"event": "refund_cancelled_subscription", "uid": uid})
            return "refund_cancelled"

        return "ignored"

    # ── one-time pass ────────────────────────────────────────────────────

    def _grant_pass(self, uid: str, session) -> str:
        """Paid one-time pass: extend paid_until by N days (from now, or from the current end)."""
        meta = _get(session, "metadata", {}) or {}
        days = int(_get(meta, "days") or self.pass_days)
        now = datetime.now(timezone.utc)
        current = self._get_sub(uid).get("paid_until")
        if isinstance(current, str):
            current = datetime.fromisoformat(current.replace("Z", "+00:00"))
        start = current if current and current > now else now
        until = start + timedelta(days=days)
        self._sub_ref(uid).set({
            "paid_until": until,
            "last_pass": {"at": now, "days": days, "payment_intent": _get(session, "payment_intent"),
                          "amount_total": _get(session, "amount_total")},
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        self._access.invalidate(uid)
        logger.info("pass_granted", extra={"event": "pass_granted", "uid": uid, "days": days,
                                           "paid_until": until.isoformat()})
        return "pass_granted"

    def _revoke_pass(self, uid: str, payment_intent: str) -> str:
        """Fully refunded pass: access ends now."""
        self._sub_ref(uid).set({"paid_until": datetime.now(timezone.utc),
                                "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        self._access.invalidate(uid)
        logger.info("pass_refunded", extra={"event": "pass_refunded", "uid": uid, "payment_intent": payment_intent})
        return "pass_refunded"
