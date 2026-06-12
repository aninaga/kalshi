#!/usr/bin/env bash
# Self-healing watchdog for the shadow run: if the wrapper (which auto-restarts
# the monitor) is dead, relaunch it; then print a status line. Called every ~4min
# by the heartbeat Monitor. Keeps the run going "no matter what" within this
# container (only container reclaim can stop it — that's what the EC2 deploy is for).
cd /home/user/kalshi || exit 0
L=market_data/shadow/ledger.jsonl
WP=$(cat market_data/shadow/wrapper.pid 2>/dev/null)
if ! ps -p "${WP:-0}" >/dev/null 2>&1; then
  nohup setsid bash -c 'while true; do
    python3 -m kalshi_arbitrage.cli monitor --interval 20 --snapshot-every 60 --min-net-edge 0.003 --ledger market_data/shadow/ledger.jsonl --watchlist-cache market_data/shadow/watchlist.json >> market_data/shadow/monitor.log 2>&1
    echo "[wrapper] restart $(date -u)" >> market_data/shadow/monitor.log
    sleep 5
  done' >/dev/null 2>&1 &
  echo $! > market_data/shadow/wrapper.pid
  ST=RESTARTED
else
  ST=UP
fi
SN=$(grep -c snapshot "$L" 2>/dev/null || echo 0)
NET=$(grep snapshot "$L" 2>/dev/null | tail -1 | python3 -c "import sys,json;d=json.load(sys.stdin);print('clean=\$%.2f@%d basis=\$%.2f'%(d.get('open_net',0),d.get('open_count',0),d.get('open_net_uncertain',0)))" 2>/dev/null || echo "warming")
echo "shadow=$ST snaps=$SN $NET $(date -u +%H:%MZ)"
