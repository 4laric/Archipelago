#!/usr/bin/env bash
# start_servers.sh — launch BOTH servers on the Hetzner box for the off-box head-to-head.
# Python MultiServer (real save ON) and archipela-go run simultaneously on two ports;
# the laptop driver hits them one at a time, so the idle one (~0 CPU) doesn't interfere.
#
#   bash ~/Archipelago/start_servers.sh [APDIR] [PY_PORT] [GO_PORT] [LOCS_PER_SLOT]
set -euo pipefail
APDIR="${1:-$HOME/Archipelago}"
PY_PORT="${2:-38281}"
GO_PORT="${3:-38291}"
LOCS="${4:-25}"   # match the room's per-slot location count (ChecksFinder = 25)
cd "$APDIR"
# shellcheck disable=SC1091
. .venv/bin/activate
export SKIP_REQUIREMENTS_UPDATE=1   # never let ModuleUpdate pip-install / clobber deps

MD="$(ls -t loadtest_out/*.zip loadtest_out/*.archipelago 2>/dev/null | head -1 || true)"
[ -n "$MD" ] || { echo "no multidata in loadtest_out/ — see hetzner_setup.sh output"; exit 1; }
rm -f loadtest_out/*.apsave   # fresh room

pkill -f "MultiServer.py" 2>/dev/null || true
pkill -f "archipela-go"   2>/dev/null || true
sleep 1

nohup python3 MultiServer.py "$MD" --host 0.0.0.0 --port "$PY_PORT" > py_server.log 2>&1 &
echo "[start] python MultiServer pid $! on :$PY_PORT  (room $MD)"
nohup ./archipelago-go/archipela-go --host 0.0.0.0 --port "$GO_PORT" --locs-per-slot "$LOCS" > go_server.log 2>&1 &
echo "[start] archipela-go     pid $! on :$GO_PORT  (locs/slot $LOCS)"
sleep 3

echo
echo "[start] both up. Open the firewall if you haven't:"
echo "        sudo ufw allow OpenSSH && sudo ufw allow $PY_PORT && sudo ufw allow $GO_PORT && sudo ufw --force enable"
echo "[start] (optional) sample server CPU during a run, in another SSH session:"
echo "        . .venv/bin/activate && python3 sample_server.py --port $PY_PORT --out py_cpu.csv"
echo "        . .venv/bin/activate && python3 sample_server.py --port $GO_PORT --out go_cpu.csv"
echo "[start] now drive from your LAPTOP:"
echo "        python headtohead_remote.py --host <THIS_BOX_IP> --py-port $PY_PORT --go-port $GO_PORT --slots 250"
