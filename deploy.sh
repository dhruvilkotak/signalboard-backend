#!/bin/bash
# deploy.sh — run this on your Oracle/GCP VM to set up the backend
# Usage: bash deploy.sh

set -e

echo "=== Signal Board Backend Deploy ==="

# 1. Update system
sudo apt-get update -y
sudo apt-get install -y python3.11 python3-pip python3.11-venv nginx

# 2. Create app directory
mkdir -p ~/signalboard
cd ~/signalboard

# 3. Copy your .env (must exist before running this)
if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example to .env and fill in your keys."
  exit 1
fi

# 4. Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# 5. Install dependencies
pip install -r requirements.txt

# 6. Create systemd service for auto-restart
sudo tee /etc/systemd/system/signalboard.service > /dev/null <<EOF
[Unit]
Description=Signal Board FastAPI Backend
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/signalboard
Environment=PATH=$HOME/signalboard/venv/bin
ExecStart=$HOME/signalboard/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable signalboard
sudo systemctl restart signalboard

# 7. Setup nginx as reverse proxy (optional, for port 80)
sudo tee /etc/nginx/sites-available/signalboard > /dev/null <<EOF
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/signalboard /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx

echo ""
echo "=== Deploy complete ==="
echo "Backend running at: http://$(curl -s ifconfig.me):8000"
echo "Health check: curl http://$(curl -s ifconfig.me):8000/health"
