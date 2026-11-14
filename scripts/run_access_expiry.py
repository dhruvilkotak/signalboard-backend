"""
Manual run of the Auto-Trader expiry / liquidation job (runbook:
docs/runbooks/autotrader-liquidation.md). Uses the same code as the daily job.

    cd /home/kotakdhruvil/signalboard-backend
    sudo -u kotakdhruvil venv/bin/python scripts/run_access_expiry.py --list-failed
    sudo -u kotakdhruvil venv/bin/python scripts/run_access_expiry.py --uid UID            # status only
    sudo -u kotakdhruvil venv/bin/python scripts/run_access_expiry.py --uid UID --reset    # clear failed mark
    sudo -u kotakdhruvil venv/bin/python scripts/run_access_expiry.py --uid UID --apply    # run now
    sudo -u kotakdhruvil venv/bin/python scripts/run_access_expiry.py --apply              # everyone (= daily job)

Without --apply nothing changes except --reset. Selling only happens during
trading hours (weekdays 7:30 AM–6 PM ET); outside them --apply only pauses.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import firebase_admin  # noqa: E402
from firebase_admin import credentials, firestore  # noqa: E402

from services.access_expiry_service import AccessExpiryService  # noqa: E402
from services.access_service import AccessService  # noqa: E402
from services.feature_flags import FeatureFlagService  # noqa: E402
from services.portfolio_service import PortfolioService  # noqa: E402
from services.price_service import PriceService  # noqa: E402
from utils.market_hours import get_current_session, is_trading_allowed  # noqa: E402

STATE_KEYS = ("pending_liquidation", "paused_by_expiry", "liquidation_attempts", "liquidation_failed",
              "last_liquidation_error", "last_liquidation_attempt_at", "last_partial_liquidation",
              "last_liquidation")


def build():
    cred_path = (os.environ.get("FIREBASE_CREDENTIALS") or os.environ.get("FIREBASE_CREDENTIALS_PATH")
                 or "/home/kotakdhruvil/signalboard-backend/firebase-credentials.json")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_path))
    db = firestore.client()
    flags = FeatureFlagService(); flags.set_db(db)
    access = AccessService(flags); access.set_db(db)
    portfolio = PortfolioService(); portfolio.set_db(db)
    prices = PriceService()
    return db, AccessExpiryService(portfolio, prices, access, flags, db=db), access, portfolio, prices


async def status(svc, access, portfolio, uid):
    a = access.get_access(uid, use_cache=False)
    st = svc._read_state(uid)
    strategies = await portfolio.get_all_strategies(uid)
    print(f"\n── {uid}")
    print(f"   plan={a.get('plan')}  autotrader={a['features'].get('autotrader')}  "
          f"liquidation_flag={svc.flags.is_enabled('autotrader_liquidation', uid)}")
    print(f"   strategies: " + (", ".join(f"{k}{' (paused)' if v.get('is_paused') else ''}"
                                         for k, v in sorted(strategies.items())) or "none"))
    for k in STATE_KEYS:
        if st.get(k) not in (None, [], 0, False):
            print(f"   {k}: {st[k]}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", action="append", help="user id (repeatable); default: everyone with strategies")
    ap.add_argument("--apply", action="store_true", help="run the job now (pause / sell)")
    ap.add_argument("--reset", action="store_true", help="clear the failed mark + attempts for --uid")
    ap.add_argument("--list-failed", action="store_true", help="show unresolved job_failures")
    args = ap.parse_args()

    db, svc, access, portfolio, prices = build()
    print(f"market session: {get_current_session()}  (selling allowed: {is_trading_allowed()})")

    if args.list_failed:
        docs = list(db.collection("job_failures").where("resolved", "==", False).stream())
        print(f"{len(docs)} unresolved failure(s)")
        for d in docs:
            r = d.to_dict()
            print(f"  [{d.id}] uid={r.get('uid')} attempts={r.get('attempts')} error={r.get('error')}")
        if not (args.uid or args.apply):
            return

    if args.reset:
        if not args.uid:
            sys.exit("--reset needs --uid")
        for uid in args.uid:
            svc.reset_failure(uid)
            print(f"reset {uid}: failed mark cleared, 3 fresh attempts")

    uids = args.uid or svc._uids_with_strategies()
    for uid in uids:
        await status(svc, access, portfolio, uid)

    if not args.apply:
        print("\nstatus only — add --apply to run the job for these users")
        return
    if is_trading_allowed():
        await prices.update_cache()
    else:
        print("\nmarket closed: --apply will pause strategies but not sell")
    print("\nresult:", await svc.run(uids))
    for uid in uids:
        await status(svc, access, portfolio, uid)


if __name__ == "__main__":
    asyncio.run(main())
