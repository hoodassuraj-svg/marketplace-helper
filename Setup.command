#!/bin/bash
# Double-click this once before using the app for the first time.

cd "$(dirname "$0")"

echo "============================================"
echo "  Marketplace Helper — First-time Setup"
echo "============================================"
echo ""

# Check Python is available
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 is not installed."
    echo "Download it from https://www.python.org/downloads/ then run this again."
    read -p "Press Enter to close..."
    exit 1
fi

echo "Setting up virtual environment..."
python3 -m venv .venv

echo "Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

echo "Installing browser (this may take a minute)..."
.venv/bin/playwright install chromium

echo ""
echo "============================================"
echo "  Setup complete!"
echo "  Double-click 'Marketplace Helper.app'"
echo "  to launch the app."
echo "============================================"
echo ""
read -p "Press Enter to close..."
