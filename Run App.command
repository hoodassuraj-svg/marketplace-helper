#!/bin/bash
# Backup launcher / repair tool.
# If the "Marketplace Helper" icon won't open, double-click THIS once.
# It fixes permissions, removes the download block, sets up if needed,
# then launches the app. After this, the icon double-click works too.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Repair: remove the macOS download block and restore the app's exec bit.
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null
chmod +x "$DIR/Marketplace Helper.app/Contents/MacOS/Marketplace Helper" 2>/dev/null

# ── First-run setup ───────────────────────────────────────────────────────────
if [ ! -f ".venv/bin/python" ]; then
    clear
    echo "============================================"
    echo "  Marketplace Helper — First-time Setup"
    echo "  (this only happens once)"
    echo "============================================"
    echo ""
    if ! command -v python3 &>/dev/null; then
        echo "ERROR: Python 3 is not installed."
        echo "Download it from https://www.python.org/downloads/ then try again."
        read -p "Press Enter to close..."
        exit 1
    fi
    echo "Setting up (1/2)..."
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
    echo "Installing browser (2/2, may take a minute)..."
    .venv/bin/playwright install chromium
    echo ""
    echo "Setup complete! Launching..."
    echo ""
fi

# ── Launch the GUI directly (no Gatekeeper prompt this way) ───────────────────
".venv/bin/python" app.py

# Tidy up the Terminal window when the app quits.
osascript -e 'tell application "Terminal" to close (every window whose name contains "Run App")' 2>/dev/null &
