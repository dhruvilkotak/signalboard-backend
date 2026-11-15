# Stripe setup (B5 #52)

Do everything in **Test mode** first (toggle at the top of the Stripe dashboard). Repeat in Live mode only at launch.

## 1. Product and prices
Stripe → **Product catalog** → **Add product** `SignalBoard Pro`, then add three prices.
For each price set **Include tax in price: No** (tax is added on top, see section 1b).

Prices are set so that **about $20/month reaches you after Stripe's fees**
(2.9% + 30¢ card, 0.7% Billing on subscriptions, 0.5% Stripe Tax):

| Plan | Type | Price | You receive (US card) | env var |
|---|---|---|---|---|
| Monthly | Recurring, monthly | **$21.17** | ≈ $20.00 / month | `STRIPE_PRICE_MONTHLY` |
| Annual (15% off monthly) | Recurring, yearly | **$215.93** | ≈ $206.78 / year | `STRIPE_PRICE_ANNUAL` |
| One-month pass | One-off | **$21.02** | ≈ $20.00 | `STRIPE_PRICE_PASS` |

Formula: `price = (net + 0.30) / (1 − 0.029 − 0.007 − 0.005)` (drop the 0.007 for the
one-off pass; drop 0.005 if Stripe Tax is off). Non-US cards cost +1.5%, so you net a
little less on those. Annual = 12 × $21.17 × 0.85.

Copy each price ID (`price_...`). Any plan whose env var is empty is simply not offered.

## 1b. Sales tax on top (like ChatGPT / Spotify)
Stripe → **Settings → Tax**:
- Origin address: your business address.
- Default tax code for the product: **Software as a service (SaaS) – personal use**.
- **Registrations → Add** Washington (after registering with WA Department of Revenue).
  Add other states only if you ever register there.
- Then set `STRIPE_AUTOMATIC_TAX=1`. Checkout asks for the billing address and adds the
  right tax for it; customers outside states you're registered in pay no tax.
Stripe Tax costs 0.5% per transaction (already in the prices above).

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
- Allow: update payment method, view invoices, **cancel subscription → at end of billing period**
  (customers can cancel anytime; they keep access until the paid month/year ends, no partial refunds).
- Optional: allow switching between Monthly and Annual (Products → add both prices).
- Click **Save** (portal sessions fail until the settings are saved once).
- One-month pass buyers have nothing to cancel; it just ends after 30 days.

## 4. Public details (Terms)
Stripe → **Settings → Public details**: add the Terms URL `https://signalboard-frontend.vercel.app/terms` (and privacy). Then you may set `STRIPE_TOS_CONSENT=1` to show the Terms checkbox in Checkout.

## 5. Environment on the VM
Add to the systemd `EnvironmentFile` (`/home/kotakdhruvil/signalboard-backend/.env`):

```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_MONTHLY=price_...      # (STRIPE_PRICE_ID still works as the monthly price)
STRIPE_PRICE_ANNUAL=price_...
STRIPE_PRICE_PASS=price_...
STRIPE_PASS_DAYS=30
STRIPE_AUTOMATIC_TAX=0               # 1 once Stripe Tax is set up (section 1b)
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
