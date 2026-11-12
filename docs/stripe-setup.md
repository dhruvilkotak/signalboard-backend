# Stripe setup (B5 #52)

Do everything in **Test mode** first (toggle at the top of the Stripe dashboard). Repeat in Live mode only at launch.

## 1. Product and price
Stripe → **Product catalog** → **Add product**
- Name: `SignalBoard`, price **$20.00 USD**, **Recurring**, **Monthly**.
- Copy the price ID (`price_...`).

## 2. Webhook
Stripe → **Developers → Webhooks** → **Add endpoint**
- URL: `https://signalboard.duckdns.org/api/billing/webhook`
- Events:
  - `checkout.session.completed`
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_failed`
  - `charge.refunded`
- Copy the signing secret (`whsec_...`).

## 3. Customer Portal
Stripe → **Settings → Billing → Customer portal**
- Allow: update payment method, view invoices, **cancel subscription → at end of billing period**.
- Turn off switching plans (there's only one).

## 4. Public details (Terms)
Stripe → **Settings → Public details**: add the Terms URL `https://signalboard-frontend.vercel.app/terms` (and privacy). Then you may set `STRIPE_TOS_CONSENT=1` to show the Terms checkbox in Checkout.

## 5. Environment on the VM
Add to the systemd `EnvironmentFile` (`/home/kotakdhruvil/signalboard-backend/.env`):

```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID=price_...
FRONTEND_URL=https://signalboard-frontend.vercel.app
STRIPE_TOS_CONSENT=0
```

Then `sudo systemctl restart signalboard`. Check `GET /api/billing/status` returns `"mode": "test"`.

## 6. nginx
Keep the webhook out of rate limiting so Stripe retries always land. In the server block, before the general `/api/` location:

```nginx
location = /api/billing/webhook {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

`sudo nginx -t && sudo systemctl reload nginx`

## 7. Test (flag `billing` on allowlist for a test user)
1. Paywall → **Subscribe · $20/month** → Stripe Checkout.
2. Pay with `4242 4242 4242 4242`, any future date, any CVC, any ZIP.
3. Back in the app: "Activating…" → "You're subscribed", header **★ Subscribed**, everything unlocked.
   Firestore `users/<uid>/data/subscription.status` = `active`.
4. **★ Subscribed** → Portal → cancel → status stays active with `cancel_at_period_end: true` until the period ends.
5. Failed payment: subscribe with `4000 0000 0000 0341`, or in the dashboard trigger a failed renewal → header **⚠ Payment failed · Update card**.
6. Full refund from the dashboard → subscription cancels → access ends → access_expiry job handles the Auto-Trader.
7. Logs: `sudo journalctl -u signalboard.service --since "10 min ago" | grep -E "stripe_webhook|subscription_synced|checkout_started"`

Local webhook testing (optional): `stripe listen --forward-to localhost:8000/api/billing/webhook`.

## Launch (Live mode)
Repeat 1–4 in Live mode, put the live keys in `.env`, restart, then turn `billing` on.
Decide on Stripe Tax first (Washington taxes digital services; check with a tax professional).
