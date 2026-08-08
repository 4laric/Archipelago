#!/usr/bin/env bash
# hetzner_setup.sh — provision a Hetzner box (Ubuntu 24.04) to host the off-box
# head-to-head: Python MultiServer + peliarch, both reachable from your laptop.
#
# Run ON the box, after you've copied your Archipelago folder over (see
# HETZNER_HEADTOHEAD.md). Idempotent — safe to re-run.
#
#   bash ~/Archipelago/hetzner_setup.sh [APDIR]
set -euo pipefail
APDIR="${1:-$HOME/Archipelago}"
cd "$APDIR"

echo "[setup] installing system packages…"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip golang-go git build-essential

echo "[setup] python venv + AP server runtime deps (no protobuf/world auto-install)…"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -U pip
pip install -q "websockets<14" PyYAML jellyfish schema orjson platformdirs \
    colorama typing_extensions psutil cython bsdiff4

echo "[setup] building Cython _speedups (so the Python server uses the same fast path)…"
( cythonize -b -i _speedups.pyx ) || echo "[setup] _speedups build failed — Python server will use the slower pure-Python path (still fine)"

echo "[setup] building peliarch…"
( cd archipelago-go && go build -o peliarch . )

echo
echo "[setup] done."
echo "  python : $(python3 --version)"
echo "  go     : $(go version)"
MD="$(ls -t loadtest_out/*.zip loadtest_out/*.archipelago 2>/dev/null | head -1 || true)"
if [ -n "$MD" ]; then
  echo "  room   : $MD"
else
  echo "  room   : MISSING — no multidata in loadtest_out/."
  echo "           Either scp one over, or generate on the box:"
  echo "           . .venv/bin/activate && SKIP_REQUIREMENTS_UPDATE=1 \\"
  echo "             python3 gen_yamls.py --count 1000 --game ChecksFinder --out loadtest_players && \\"
  echo "             SKIP_REQUIREMENTS_UPDATE=1 python3 Generate.py \\"
  echo "             --player_files_path loadtest_players --outputpath loadtest_out --spoiler 0"
fi
echo
echo "next: bash $APDIR/start_servers.sh"
