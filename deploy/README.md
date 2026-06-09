# Deploying the 24/7 shadow run (no capital)

The **shadow run** answers *"how much would the bot have made?"* authoritatively, with **zero capital
and zero credentials**. It runs `kalshi-arb monitor` continuously, polling **real** order books (with
**real depth**) every ~20s and recording each fee-aware gap episode to an append-only JSONL ledger.
Because public APIs don't expose *historical* depth, the only way to get a trustworthy number is to
capture it **going forward** — that's what this does.

> `monitor` **without `--execute` never constructs the executor** (see `tools/monitor_arb.py`). No
> orders, no money. `EXECUTION_MODE=paper` and the disarmed live lock are belt-and-suspenders.

After ~7 days: `kalshi-arb analyze-ledger --path market_data/shadow/ledger.jsonl --session-hours 168`.

---

## Option A — systemd on an EC2 t3.micro (recommended)

A t3.micro (1 GB RAM) is plenty — the monitor is IO-bound polling.

```bash
# 1. Launch Ubuntu 22.04 / Amazon Linux 2023 t3.micro, 20 GB gp3.
# 2. System user + code at /opt/kalshi
sudo useradd -r -m -d /opt/kalshi kalshi
sudo -u kalshi git clone <repo> /opt/kalshi
cd /opt/kalshi
sudo -u kalshi python3.11 -m venv venv
sudo -u kalshi ./venv/bin/pip install -e .       # installs the `kalshi-arb` script
# put the venv on PATH for the unit, or point ExecStart at ./venv/bin/kalshi-arb

# 3. Config (shadow needs NO keys; keys are only for live execution)
sudo -u kalshi cp .env.example .env && sudo chmod 600 .env

# 4. Generate the allowlist once (review held-for-review pairs before trusting them)
sudo -u kalshi ./venv/bin/kalshi-arb find-arb --output market_data/matching/match_allowlist.json
#   (or: kalshi-arb machine --allowlist market_data/matching/match_allowlist.json)

# 5. Install + start the service
sudo mkdir -p /var/log/kalshi-arb && sudo chown kalshi /var/log/kalshi-arb
sudo cp deploy/kalshi-arb-shadow.service /etc/systemd/system/
sudo cp deploy/kalshi-arb.logrotate /etc/logrotate.d/kalshi-arb
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-arb-shadow

# 6. Watch it
journalctl -u kalshi-arb-shadow -f       # OPEN/CLOSE episode events
wc -l /opt/kalshi/market_data/shadow/ledger.jsonl
```

Restart-on-crash is built in (`Restart=on-failure`, `RestartSec=10`, crash-loop guard). The ledger
lives on the EBS root volume and survives restarts.

## Option B — Docker

```bash
cp .env.example .env            # edit if needed; shadow needs no keys
docker compose -f deploy/docker-compose.yml up --build -d
docker compose -f deploy/docker-compose.yml logs -f
# ledger persists on the host at ./market_data/shadow/ledger.jsonl
```

`restart: unless-stopped` handles crashes; `json-file` logging rotates at 10 MB × 5.

---

## Analyzing the run

```bash
kalshi-arb analyze-ledger --path market_data/shadow/ledger.jsonl --session-hours <wall-clock hours>
```

Reports capturable net, episode/duration distribution, per-market breakdown, and a run-rate projection
(flagged capital-locked / marquee-event dependent — not a steady rate).

## Important

- **Never logrotate the ledger JSONL** — it's the audit record. `kalshi-arb.logrotate` deliberately
  targets only `*.log`.
- The shadow number is the *capturable upper bound at real depth* — what a perfectly-timed taker could
  grab. A live-money run (separate, gated; see the plan's Phase 3) is what produces *realized* P&L.
