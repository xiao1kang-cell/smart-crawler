#!/bin/bash
set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "usage: $0 <tailscale-ip>" >&2
  exit 2
fi

TS_IP="$1"
BASE="$HOME/.local/var/node_exporter"
TEXTDIR="$BASE/textfile_collector"

mkdir -p "$TEXTDIR" "$HOME/.local/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/node_exporter"

cat > "$HOME/.local/bin/smartcrawler_metrics.sh" <<'SH'
#!/bin/bash
set -euo pipefail

TEXTDIR="$HOME/.local/var/node_exporter/textfile_collector"
OUT="$TEXTDIR/smartcrawler.prom.$$"
FINAL="$TEXTDIR/smartcrawler.prom"
HOSTNAME_SHORT=$(hostname | tr "[:upper:]" "[:lower:]")

worker_count=$(ps ax -o command= | { grep -E "[p]ython.*-m app\\.worker" || true; } | wc -l | tr -d " ")
browser_count=$(ps ax -o command= | { grep -E "[p]laywright|[c]hromium|[c]hrome.*--remote-debugging" || true; } | wc -l | tr -d " ")

cat > "$OUT" <<METRICS
# HELP smartcrawler_worker_processes Number of smart-crawler app.worker processes on this node.
# TYPE smartcrawler_worker_processes gauge
smartcrawler_worker_processes{host="$HOSTNAME_SHORT"} $worker_count
# HELP smartcrawler_browser_processes Number of Playwright/browser related processes on this node.
# TYPE smartcrawler_browser_processes gauge
smartcrawler_browser_processes{host="$HOSTNAME_SHORT"} $browser_count
# HELP smartcrawler_textfile_last_success_unixtime Last successful local smart-crawler textfile metric update.
# TYPE smartcrawler_textfile_last_success_unixtime gauge
smartcrawler_textfile_last_success_unixtime{host="$HOSTNAME_SHORT"} $(date +%s)
METRICS

mv "$OUT" "$FINAL"
SH
chmod +x "$HOME/.local/bin/smartcrawler_metrics.sh"
"$HOME/.local/bin/smartcrawler_metrics.sh"

cat > "$HOME/Library/LaunchAgents/com.smartcrawler.textfile-metrics.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.smartcrawler.textfile-metrics</string>
  <key>ProgramArguments</key>
  <array>
    <string>${HOME}/.local/bin/smartcrawler_metrics.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>${HOME}/Library/Logs/node_exporter/textfile_stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${HOME}/Library/Logs/node_exporter/textfile_stderr.log</string>
</dict>
</plist>
PLIST

python3 - "$TS_IP" "$TEXTDIR" <<'PY'
from pathlib import Path
import sys

ts_ip = sys.argv[1]
textdir = sys.argv[2]
plist = Path.home() / "Library/LaunchAgents/com.smartcrawler.node-exporter.plist"
content = plist.read_text()
argument = f"    <string>--collector.textfile.directory={textdir}</string>\n"
if "--collector.textfile.directory" not in content:
    marker = f"    <string>--web.listen-address={ts_ip}:9100</string>\n"
    if marker not in content:
        raise SystemExit(f"listen marker not found in {plist}")
    content = content.replace(marker, marker + argument)
    plist.write_text(content)
PY

launchctl bootout "gui/$(id -u)/com.smartcrawler.textfile-metrics" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.smartcrawler.textfile-metrics.plist"
launchctl enable "gui/$(id -u)/com.smartcrawler.textfile-metrics"

launchctl bootout "gui/$(id -u)/com.smartcrawler.node-exporter" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.smartcrawler.node-exporter.plist"
launchctl enable "gui/$(id -u)/com.smartcrawler.node-exporter"

sleep 1
cat "$TEXTDIR/smartcrawler.prom"
