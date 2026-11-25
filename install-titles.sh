#!/bin/sh
# Install a launchd agent that keeps Warp session titles = one-line summaries (runs titles.py every 30s),
# and swap Warp's official Claude Code plugin for the fork in claude-plugin/ (labels idle sessions with the summary).
# Uninstall: launchctl bootout gui/$(id -u)/com.user.warp-session-titles; rm ~/Library/LaunchAgents/com.user.warp-session-titles.plist
#            claude plugin uninstall warp-digest@warp-session-digest; claude plugin enable warp@claude-code-warp
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
PY="$(python3 -c 'import sys;print(sys.executable)')"
LABEL=com.user.warp-session-titles
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/.cache/warp-session-digest/titles.log"
mkdir -p "$(dirname "$LOG")" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$REPO/titles.py</string></array>
  <key>StartInterval</key><integer>30</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# bootout is async; bootstrap fails with EIO while the old job is still registered
for _ in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
if command -v claude >/dev/null; then
  claude plugin marketplace add "$REPO" >/dev/null 2>&1 || claude plugin marketplace update warp-session-digest >/dev/null 2>&1 || true
  claude plugin install warp-digest@warp-session-digest || true
  claude plugin disable warp@claude-code-warp 2>/dev/null || true
  echo "Claude Code plugin: warp-digest enabled, warp@claude-code-warp disabled (applies to new sessions; /reload-plugins in open ones)"
fi
echo "installed $PLIST (log: $LOG)"
