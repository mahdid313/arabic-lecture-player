#!/bin/bash
# Run this once in WSL to install the auto-start downloader service
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
USER="$(whoami)"

echo "Installing arabic-downloader systemd service..."
echo "  Repo: $REPO_DIR"
echo "  User: $USER"

sudo tee /etc/systemd/system/arabic-downloader.service > /dev/null << EOF
[Unit]
Description=Arabic Lecture Downloader
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $REPO_DIR/downloader.py
Restart=always
RestartSec=10
User=$USER
WorkingDirectory=$REPO_DIR
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable arabic-downloader
sudo systemctl restart arabic-downloader

echo ""
echo "Done! Service is running. Check status with:"
echo "  sudo systemctl status arabic-downloader"
echo "  journalctl -u arabic-downloader -f"
