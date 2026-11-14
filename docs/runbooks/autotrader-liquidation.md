# Runbook — Auto-Trader liquidation failed

## What the job does

When a user loses Auto-Trader access (trial or coupon ended, subscription
cancelled, Pro removed), `access_expiry` pauses their Auto-Trader strategies
and sells them to cash. Manual Trades positions are never touched.

- **Schedule:** once per trading day, Mon–Fri **9:45 AM ET** (6:45 AM PT).
- **Retries:** if selling fails for a user, the next day's run tries again.
  After **2 retries (3 attempts in all)** the user is marked failed and skipped
  until you reset them. Their strategies stay **paused**, so nothing trades
  while you investigate.
- **Flag:** `autotrader_liquidation` must be on for the user, otherwise the job
  only logs what it would sell.

## How you find out

1. **Email** to `ADMIN_ALERT_EMAIL` (defaults to `GMAIL_USER`), subject
   `[SignalBoard] Liquidation failed: <uid>`, with the error and what was
   already sold. Needs `GMAIL_APP_PASSWORD` in `.env` (already used for
   approval emails).
2. **Firestore** `job_failures/{id}`: `uid, attempts, error, partial,
   resolved: false`.
3. **Logs**, structured event `autotrader_liquidation_failed`:
   ```bash
   sudo journalctl -u signalboard --since "today" | grep -i "liquidat\|access_expiry"
   ```
4. **Job-level failure** (job couldn't start, e.g. Firestore down): same email
   with `status: job_failed`, plus the usual `job_failed` log event.

Per-user state lives on `users/{uid}/data/access`:
`liquidation_attempts`, `liquidation_failed`, `last_liquidation_error`,
`last_liquidation_attempt_at`, `last_partial_liquidation`, `pending_liquidation`.

## Fix it

All commands on the VM:

```bash
cd /home/kotakdhruvil/signalboard-backend
S="sudo -u kotakdhruvil venv/bin/python scripts/run_access_expiry.py"
```

1. **See what failed**
   ```bash
   $S --list-failed
   $S --uid <UID>            # plan, access, strategies, attempts, last error
   ```
2. **Find the cause** from `last_liquidation_error` and the logs. Common ones:
   - price feed / Alpaca error → check prices load in the app, wait for it to recover
   - strategy doc in a bad state → inspect `users/<UID>/strategies/<key>` in Firestore
   - user actually should still have access (coupon/sub issue) → fix access;
     the next run resumes their strategies automatically
3. **Reset and run now** (during trading hours, weekdays 7:30 AM–6 PM ET /
   4:30 AM–3 PM PT):
   ```bash
   $S --uid <UID> --reset --apply
   ```
   Result `liquidated` = done; the email/`job_failures` entry is marked
   resolved by `--reset`. Outside trading hours `--apply` only pauses; run it
   again during hours, or just `--reset` and let the next 9:45 AM ET run do it.
4. **Still failing?** Leave the user marked failed (strategies stay paused,
   nothing trades) and fix the code. As a last resort, stop the strategy from
   the admin UI / `portfolio_svc.stop_strategy` by hand.

## Other manual runs

```bash
$S                       # status of everyone with strategies (changes nothing)
$S --apply               # run the whole job now (same as the daily run)
```

A missed daily run (service restarting at 9:45 AM ET) is not caught up
automatically; run `$S --apply` or wait for the next day.

## Optional: GCP alert on the log event

Logs-based alert in Cloud Monitoring, filter:
```
jsonPayload.event="autotrader_liquidation_failed" OR (jsonPayload.event="job_failed" AND jsonPayload.job="access_expiry")
```
