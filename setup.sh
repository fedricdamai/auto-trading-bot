#!/bin/bash
# Quick setup script for Oracle Cloud / any Ubuntu VPS
set -e

echo "=== Installing dependencies ==="
sudo apt update && sudo apt install -y python3 python3-pip git

echo "=== Installing Python packages ==="
pip3 install -r requirements.txt

echo "=== Setting up .env ==="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — edit it with: nano .env"
else
    echo ".env already exists, skipping"
fi

echo "=== Installing systemd service ==="
sudo cp tradingbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradingbot

echo ""
echo "=== Done! ==="
echo "1. Edit your config:  nano .env"
echo "2. Start the bot:     sudo systemctl start tradingbot"
echo "3. Check status:      sudo systemctl status tradingbot"
echo "4. View logs:         journalctl -u tradingbot -f"
echo "5. Stop the bot:      sudo systemctl stop tradingbot"
