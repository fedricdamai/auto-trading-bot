#!/bin/bash
# Setup script for Ubuntu VPS
set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Installing system dependencies ==="
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

echo "=== Setting up virtual environment ==="
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
    echo "Created venv"
else
    echo "venv already exists, skipping"
fi

echo "=== Installing Python packages ==="
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "=== Setting up .env ==="
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo "Created .env — edit it with: nano $INSTALL_DIR/.env"
else
    echo ".env already exists, skipping"
fi

echo "=== Installing systemd service ==="
sudo cp "$INSTALL_DIR/tradingbot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradingbot

echo ""
echo "=== Done! ==="
echo "1. Edit your config:  nano $INSTALL_DIR/.env"
echo "2. Start the bot:     sudo systemctl start tradingbot"
echo "3. Check status:      sudo systemctl status tradingbot"
echo "4. View logs:         journalctl -u tradingbot -f"
echo "5. Stop the bot:      sudo systemctl stop tradingbot"
