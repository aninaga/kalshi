#!/bin/zsh
# Install the fund controller as a LaunchAgent (always-on, localhost:8777).
set -e
PYTHON="$(command -v python3)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST=~/Library/LaunchAgents/com.kalshi.fund.plist

mkdir -p ~/.kalshi_fund
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kalshi.fund</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/ops/controller.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$REPO</string>
    <key>PATH</key><string>$(dirname "$PYTHON"):/usr/local/bin:/usr/bin:/bin:$HOME/.nvm/versions/node/v20.19.2/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.kalshi_fund/launchd.out</string>
  <key>StandardErrorPath</key><string>$HOME/.kalshi_fund/launchd.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed + loaded. Dashboard: http://localhost:8777"
