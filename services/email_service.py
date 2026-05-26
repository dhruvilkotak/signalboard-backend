# services/email_service.py
# Resend email integration — approval notifications

import httpx
import logging
import os

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = "SignalBoard <onboarding@resend.dev>"
APP_URL        = "https://signalboard-frontend.vercel.app"


async def send_approval_email(to_email: str, name: str) -> bool:
    """Send account approval notification to user."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping email")
        return False

    first_name = name.split()[0] if name else "there"

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:520px;margin:40px auto;padding:0 16px">

    <!-- Header -->
    <div style="text-align:center;margin-bottom:32px">
      <h1 style="color:#e6edf3;font-size:24px;font-weight:700;margin:0;letter-spacing:-0.5px">
        SignalBoard
      </h1>
      <p style="color:#8b949e;font-size:13px;margin:4px 0 0">AI Stock Signals</p>
    </div>

    <!-- Card -->
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px">
      <div style="text-align:center;font-size:48px;margin-bottom:16px">🎉</div>
      <h2 style="color:#e6edf3;font-size:20px;font-weight:700;text-align:center;margin:0 0 16px">
        You're approved, {first_name}!
      </h2>
      <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0 0 24px;text-align:center">
        Your SignalBoard account has been approved. You now have full access to
        AI-powered stock signals, live prices, and the paper trading simulator.
      </p>

      <!-- CTA Button -->
      <div style="text-align:center;margin:24px 0">
        <a href="{APP_URL}"
           style="display:inline-block;background:#238636;color:#ffffff;text-decoration:none;
                  padding:12px 32px;border-radius:6px;font-size:15px;font-weight:600;
                  border:1px solid #2ea043">
          Sign In to SignalBoard →
        </a>
      </div>

      <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">

      <!-- Features -->
      <p style="color:#8b949e;font-size:13px;margin:0 0 12px;font-weight:600;color:#e6edf3">
        What's waiting for you:
      </p>
      <ul style="color:#8b949e;font-size:13px;line-height:1.8;margin:0;padding-left:20px">
        <li>📈 Live prices for 13+ stocks and ETFs</li>
        <li>🤖 AI-generated BUY/HOLD/SELL signals</li>
        <li>💰 Paper trading simulator (virtual money)</li>
        <li>💬 AI Chat for market questions</li>
      </ul>

      <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">

      <p style="color:#6e7681;font-size:12px;text-align:center;margin:0;line-height:1.6">
        ⚠️ Reminder: SignalBoard uses paper trading only — no real money involved.<br>
        Signals are for educational purposes and not financial advice.
      </p>
    </div>

    <!-- Footer -->
    <p style="color:#6e7681;font-size:11px;text-align:center;margin:24px 0 0;line-height:1.6">
      You received this because you signed up for SignalBoard.<br>
      Questions? Reply to this email or contact kotakdhruvil@gmail.com
    </p>
  </div>
</body>
</html>
"""

    text = f"""
Hi {first_name},

Your SignalBoard account has been approved! 🎉

Sign in at: {APP_URL}

What's waiting for you:
- Live prices for 13+ stocks and ETFs
- AI-generated BUY/HOLD/SELL signals
- Paper trading simulator (virtual money)
- AI Chat for market questions

Reminder: SignalBoard uses paper trading only — no real money involved.

Questions? Contact kotakdhruvil@gmail.com
"""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "from":    FROM_EMAIL,
                    "to":      [to_email],
                    "subject": "🎉 You're approved — Welcome to SignalBoard!",
                    "html":    html,
                    "text":    text,
                },
            )
            if res.status_code == 200:
                logger.info(f"Approval email sent to {to_email}")
                return True
            else:
                logger.error(f"Resend error {res.status_code}: {res.text}")
                return False
    except Exception as e:
        logger.error(f"Email send failed for {to_email}: {e}")
        return False


async def send_rejection_email(to_email: str, name: str) -> bool:
    """Send polite rejection notification."""
    if not RESEND_API_KEY:
        return False

    first_name = name.split()[0] if name else "there"

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:520px;margin:40px auto;padding:0 16px">
    <div style="text-align:center;margin-bottom:32px">
      <h1 style="color:#e6edf3;font-size:24px;font-weight:700;margin:0">SignalBoard</h1>
    </div>
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px">
      <h2 style="color:#e6edf3;font-size:18px;font-weight:700;margin:0 0 16px">
        Hi {first_name},
      </h2>
      <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0 0 16px">
        Thank you for your interest in SignalBoard. Unfortunately we're unable to
        approve your account request at this time.
      </p>
      <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0">
        If you think this was a mistake, please contact us at
        <a href="mailto:kotakdhruvil@gmail.com" style="color:#58a6ff">kotakdhruvil@gmail.com</a>.
      </p>
    </div>
  </div>
</body>
</html>
"""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "from":    FROM_EMAIL,
                    "to":      [to_email],
                    "subject": "SignalBoard access request update",
                    "html":    html,
                },
            )
            return res.status_code == 200
    except Exception as e:
        logger.error(f"Rejection email failed for {to_email}: {e}")
        return False