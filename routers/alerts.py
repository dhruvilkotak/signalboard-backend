"""routers/alerts.py"""
import os
from fastapi import APIRouter
from pydantic import BaseModel
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

router = APIRouter()

class AlertConfig(BaseModel):
    symbol: str
    condition: str   # "above" or "below"
    price: float
    email: str

_alert_configs: list = []

@router.get("/")
def get_alerts():
    return _alert_configs

@router.post("/")
def create_alert(config: AlertConfig):
    _alert_configs.append(config.dict())
    return {"status": "created", "alert": config}

@router.delete("/{index}")
def delete_alert(index: int):
    if 0 <= index < len(_alert_configs):
        removed = _alert_configs.pop(index)
        return {"status": "deleted", "alert": removed}
    return {"status": "not_found"}

def send_email_alert(symbol: str, condition: str, price: float, current_price: float):
    try:
        sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
        message = Mail(
            from_email=os.getenv("ALERT_EMAIL_FROM"),
            to_emails=os.getenv("ALERT_EMAIL_TO"),
            subject=f"🚨 Signal Board Alert: {symbol} {condition} ${price}",
            html_content=f"""
            <h2>Price Alert Triggered</h2>
            <p><strong>{symbol}</strong> is now <strong>${current_price}</strong></p>
            <p>Your alert: price {condition} ${price}</p>
            <p>Check your Signal Board dashboard for the latest AI signal.</p>
            """
        )
        sg.send(message)
    except Exception as e:
        pass   # Non-critical, log in production
