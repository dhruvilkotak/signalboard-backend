# services/email_service.py
# Gmail SMTP email integration — approval/rejection notifications
# Uses Gmail App Password — sends to any email address

import smtplib
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText

logger = logging.getLogger(__name__)

GMAIL_USER     = os.getenv("GMAIL_USER", "kotakdhruvil@gmail.com")
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
APP_URL        = "https://signalboard-frontend.vercel.app"


def _send_email(to_email: str, subject: str, html: str, text: str) -> bool:
    """Send email via Gmail SMTP."""
    if not GMAIL_PASSWORD:
        logger.warning("GMAIL_APP_PASSWORD not set — skipping email")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"SignalBoard <{GMAIL_USER}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html,  "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.sendmail(GMAIL_USER, to_email, msg.as_string())

        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Gmail SMTP failed for {to_email}: {e}")
        return False


async def send_approval_email(to_email: str, name: str) -> bool:
    """Send account approval notification to user."""
    first_name = name.split()[0] if name else "there"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:520px;margin:40px auto;padding:0 16px">

    <div style="text-align:center;margin-bottom:32px">
      <h1 style="color:#e6edf3;font-size:24px;font-weight:700;margin:0;letter-spacing:-0.5px">SignalBoard</h1>
      <p style="color:#8b949e;font-size:13px;margin:4px 0 0">AI Stock Signals</p>
    </div>

    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px">
      <div style="text-align:center;font-size:48px;margin-bottom:16px">🎉</div>
      <h2 style="color:#e6edf3;font-size:20px;font-weight:700;text-align:center;margin:0 0 16px">
        You're approved, {first_name}!
      </h2>
      <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0 0 24px;text-align:center">
        Your SignalBoard account has been approved. You now have full access to
        AI-powered stock signals, live prices, and the paper trading simulator.
      </p>

      <div style="text-align:center;margin:24px 0">
        <a href="{APP_URL}"
           style="display:inline-block;background:#238636;color:#ffffff;text-decoration:none;
                  padding:12px 32px;border-radius:6px;font-size:15px;font-weight:600;
                  border:1px solid #2ea043">
          Sign In to SignalBoard →
        </a>
      </div>

      <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">

      <p style="color:#e6edf3;font-size:13px;margin:0 0 12px;font-weight:600">What's waiting for you:</p>
      <ul style="color:#8b949e;font-size:13px;line-height:1.8;margin:0;padding-left:20px">
        <li>📈 Live prices for 13+ stocks and ETFs</li>
        <li>🤖 AI-generated BUY/HOLD/SELL signals</li>
        <li>💰 Paper trading simulator (virtual money only)</li>
        <li>💬 AI Chat for market questions</li>
      </ul>

      <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">

      <p style="color:#6e7681;font-size:12px;text-align:center;margin:0;line-height:1.6">
        ⚠️ Reminder: SignalBoard uses paper trading only — no real money involved.<br>
        Signals are for educational purposes and not financial advice.
      </p>
    </div>

    <p style="color:#6e7681;font-size:11px;text-align:center;margin:24px 0 0;line-height:1.6">
      You received this because you signed up for SignalBoard.<br>
      Questions? Contact <a href="mailto:{GMAIL_USER}" style="color:#58a6ff">{GMAIL_USER}</a>
    </p>
  </div>
</body>
</html>"""

    text = f"""Hi {first_name},

Your SignalBoard account has been approved! 🎉

Sign in at: {APP_URL}

What's waiting for you:
- Live prices for 13+ stocks and ETFs
- AI-generated BUY/HOLD/SELL signals
- Paper trading simulator (virtual money only)
- AI Chat for market questions

Reminder: SignalBoard uses paper trading only — no real money involved.
Signals are for educational purposes and not financial advice.

Questions? Contact {GMAIL_USER}
"""

    return _send_email(
        to_email,
        "🎉 You're approved — Welcome to SignalBoard!",
        html,
        text,
    )


async def send_rejection_email(to_email: str, name: str) -> bool:
    """Send polite rejection notification."""
    first_name = name.split()[0] if name else "there"

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:520px;margin:40px auto;padding:0 16px">
    <div style="text-align:center;margin-bottom:32px">
      <h1 style="color:#e6edf3;font-size:24px;font-weight:700;margin:0">SignalBoard</h1>
    </div>
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px">
      <h2 style="color:#e6edf3;font-size:18px;font-weight:700;margin:0 0 16px">Hi {first_name},</h2>
      <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0 0 16px">
        Thank you for your interest in SignalBoard. Unfortunately we're unable to
        approve your account request at this time.
      </p>
      <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0">
        If you think this was a mistake, please contact us at
        <a href="mailto:{GMAIL_USER}" style="color:#58a6ff">{GMAIL_USER}</a>.
      </p>
    </div>
  </div>
</body>
</html>"""

    text = f"""Hi {first_name},

Thank you for your interest in SignalBoard. Unfortunately we're unable to
approve your account request at this time.

If you think this was a mistake, please contact {GMAIL_USER}.
"""

    return _send_email(
        to_email,
        "SignalBoard access request update",
        html,
        text,
    )