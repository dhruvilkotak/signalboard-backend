# signalboard-backend

FastAPI backend for Signal Board — AI stock signal dashboard.

**Companion repo:** [signalboard-frontend](https://github.com/YOUR_USERNAME/signalboard-frontend)

---

## Stack
- **Python 3.11** + FastAPI
- **Alpaca API** — real-time prices, news, paper trading
- **Claude Haiku** — AI signal generation (~$0.30/month)
- **APScheduler** — background jobs (news every 30min, prices every 30s)
- **WebSocket** — live price streaming to frontend

## Local setup

```bash
git clone https://github.com/YOUR_USERNAME/signalboard-backend
cd signalboard-backend

python3.11 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — add your Alpaca + Anthropic keys

uvicorn main:app --reload --port 8000
```

API docs auto-generated at: http://localhost:8000/docs

## Project structure

```
signalboard-backend/
├── main.py                  # App entry, WebSocket, scheduler
├── config.py                # All settings from .env (single source)
├── requirements.txt
├── deploy.sh                # One-script VM deploy
├── .env.example             # Copy to .env, fill in keys
├── routers/
│   ├── prices.py            # GET /api/prices/
│   ├── news.py              # GET /api/news/
│   ├── signals.py           # GET /api/signals/
│   ├── trader.py            # GET/POST /api/trader/
│   ├── alerts.py            # GET/POST /api/alerts/
│   └── chat.py              # POST /api/chat/
├── services/
│   ├── price_service.py     # Alpaca price fetching + cache
│   ├── news_service.py      # Alpaca news fetching + cache
│   ├── signal_service.py    # Claude Haiku signal generation
│   └── trader_service.py    # Alpaca paper trading logic
├── models/
│   └── schemas.py           # All Pydantic models
└── tests/
    └── test_health.py
```

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/prices/` | All ticker prices |
| GET | `/api/prices/{symbol}` | Single ticker |
| GET | `/api/news/` | All news grouped by ticker |
| GET | `/api/news/{symbol}` | News for one ticker |
| GET | `/api/signals/` | AI signals for all tickers |
| GET | `/api/signals/{symbol}` | Signal for one ticker |
| GET | `/api/trader/account` | Paper account balance |
| GET | `/api/trader/positions` | Open positions |
| GET | `/api/trader/performance` | P&L vs $200 goal |
| GET | `/api/trader/trades` | Trade history |
| POST | `/api/trader/execute/{symbol}` | Execute AI trade |
| POST | `/api/trader/run-all` | Run AI trades on all tickers |
| POST | `/api/alerts/` | Create price alert |
| POST | `/api/chat/` | Ask AI about any stock |
| WS | `/ws/prices` | Live price stream |

## Deploy to VM

```bash
# SSH into your Oracle/GCP VM
ssh ubuntu@YOUR_VM_IP

git clone https://github.com/YOUR_USERNAME/signalboard-backend
cd signalboard-backend
cp .env.example .env
nano .env   # fill in keys

bash deploy.sh
```

## GitHub Secrets (for auto-deploy CI)

Add these in GitHub → Settings → Secrets:
- `VM_HOST` — your VM public IP
- `VM_USER` — `ubuntu` (GCP) or `opc` (Oracle)
- `VM_SSH_KEY` — your private SSH key contents

## Environment variables

See `.env.example` for all variables with descriptions.

Key ones:
```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ANTHROPIC_API_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # paper trading
```

To switch to real money trading, change `ALPACA_BASE_URL` to `https://api.alpaca.markets`.
