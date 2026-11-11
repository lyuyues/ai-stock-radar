#!/usr/bin/env bash
# One-time install on a Linux server (Ubuntu/Debian). Clone the repo to ~/invest-radar, then as your login user:
#   cd ~/invest-radar && nano .env           # NTFY_TOPIC, RADAR_PUBLIC_URL, SEC_USER_AGENT
#   bash deploy/install.sh
# Isolation: system user `invest`, code in /opt/invest-radar, MemoryMax per unit.
# Does not touch the cloudflared config.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST=/opt/invest-radar

[ -f "$SRC/.env" ] || { echo "missing $SRC/.env (copy .env.example first)"; exit 1; }

id invest >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /var/lib/invest --shell /usr/sbin/nologin invest
sudo mkdir -p "$DST/data"
sudo rsync -a --delete --exclude .git --exclude .venv --exclude data --exclude '__pycache__' "$SRC/" "$DST/"
sudo chown -R invest:invest "$DST"
cd "$DST"   # sudo -u invest must not run from a dir invest cannot read (pip stats cwd)
sudo chmod 600 "$DST/.env"

sudo -u invest python3 -m venv "$DST/.venv"
sudo -u invest "$DST/.venv/bin/pip" install -q --upgrade pip
sudo -u invest "$DST/.venv/bin/pip" install -q -e "$DST"

sudo cp "$SRC"/deploy/systemd/*.service "$SRC"/deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now invest-radar-web.service
for t in prices edgar earnings macro markets heartbeat discover weekly; do
  sudo systemctl enable --now "invest-radar-$t.timer"
done

# First data load so the dashboard isn't empty
sudo systemctl start invest-radar@daily.service || true

curl -fsS http://127.0.0.1:8770/healthz && echo " ← dashboard OK"
systemctl list-timers 'invest-radar-*' --no-pager

cat <<'MSG'

Next (manual):
 1. Cloudflare tunnel — add BEFORE the final catch-all rule in /etc/cloudflared/config.yml:
      - hostname: invest.example.com
        service: http://127.0.0.1:8770
    then: sudo cloudflared tunnel route dns <tunnel-name> invest.example.com
          sudo systemctl restart cloudflared
 2. Cloudflare Zero Trust → Access → Applications → add invest.example.com, policy: your email only.
 3. Weekly report needs Claude Code for user `invest`:
      sudo -u invest -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
      sudo -u invest -H /var/lib/invest/.local/bin/claude      # log in once, then /exit
    and add to /opt/invest-radar/.env:
      CLAUDE_CMD=/var/lib/invest/.local/bin/claude -p --output-format text --allowedTools WebSearch,WebFetch
MSG
