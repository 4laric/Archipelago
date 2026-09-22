#!/usr/bin/env bash
# bootstrap-box.sh -- bare Ubuntu server to a running Peliarch, in one idempotent pass.
#
#   # as root, on a fresh Ubuntu 26.04 Hetzner box (ssh key already installed by Hetzner):
#   curl -fsSL https://raw.githubusercontent.com/4laric/Archipelago/main/deploy/docker/bootstrap-box.sh -o bootstrap-box.sh
#   DOMAIN=peliarch.ca ACME_EMAIL=you@real-address.tld bash bootstrap-box.sh
#
# Optional environment:
#   REPO_URL      default https://github.com/4laric/Archipelago.git
#   REPO_REF      default main (a branch, tag or SHA -- the box is pinned to whatever this resolves to)
#   ER_REF        default: the `stable` row of er-archipelago release/CHANNELS.tsv
#   BB_REF        default: the `stable` row of bb-archipelago release/CHANNELS.tsv ("none" = skip Bloodborne)
#   RESTORE_FROM  a backup directory made by backup.sh; restored BEFORE the stack starts
#
# WHY THIS EXISTS. MIGRATION.md is the record of moving a working box; it assumes an old box to copy
# from. On 2026-09-21 both boxes were unreachable and the runbook's first step, "freeze OLDBOX",
# had nothing to act on. This is the same procedure with the copy step made optional.
#
# IDEMPOTENT: re-running pulls the repo, keeps an existing .env (it never overwrites one), and
# `compose up -d --build` is a no-op when nothing changed. It does NOT touch DNS. Point the A
# records at the box AFTER smoke.sh is green; Caddy cannot get a certificate before that.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/4laric/Archipelago.git}"
REPO_REF="${REPO_REF:-main}"
APP_DIR="${APP_DIR:-/root/Archipelago}"
COMPOSE_DIR="${APP_DIR}/deploy/docker"
ER_RAW="https://raw.githubusercontent.com/4laric/er-archipelago/main"
BB_RAW="https://raw.githubusercontent.com/4laric/bb-archipelago/main"

say() { printf '==> %s\n' "$*"; }
die() { printf 'bootstrap-box: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root (the stack lives in /root/Archipelago, as on the box it replaces)"

stable_of() {  # raw-base -> the stable tag in that repo's ledger
  curl -fsSL "$1/release/CHANNELS.tsv" | awk -F'\t' '!/^#/ && $1=="stable" { t=$2 } END { print t }'
}

# ---- 1. packages -----------------------------------------------------------------------------
say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 ufw git curl unzip rsync ca-certificates
systemctl enable --now docker

# ---- 2. code ---------------------------------------------------------------------------------
say "code: ${REPO_URL} @ ${REPO_REF}"
if [ -d "${APP_DIR}/.git" ]; then
  git -C "$APP_DIR" fetch -q --tags origin
else
  git clone -q "$REPO_URL" "$APP_DIR"
fi
git -C "$APP_DIR" checkout -q "$REPO_REF"
# A branch name should follow its remote; a tag or SHA leaves this a harmless no-op.
git -C "$APP_DIR" pull -q --ff-only origin "$REPO_REF" 2>/dev/null || true

# ---- 3. .env (never overwritten) ---------------------------------------------------------------
cd "$COMPOSE_DIR"
if [ ! -f .env ]; then
  [ -n "${DOMAIN:-}" ] || die "first run needs DOMAIN=... (e.g. peliarch.ca)"
  [ -n "${ACME_EMAIL:-}" ] || die "first run needs ACME_EMAIL=... a REAL address (example.com made Let's Encrypt refuse and fall back to ZeroSSL)"
  case "$ACME_EMAIL" in *example.com*|*example.org*) die "ACME_EMAIL is a placeholder: ${ACME_EMAIL}";; esac
  ER_REF="${ER_REF:-$(stable_of "$ER_RAW")}"
  [ -n "$ER_REF" ] || die "could not resolve ER_REF from the er-archipelago ledger; pass ER_REF=vX.Y.Z"
  BB_REF="${BB_REF:-$(stable_of "$BB_RAW")}"
  [ "$BB_REF" != "none" ] || BB_REF=""
  say ".env from .env.example (ER_REF=${ER_REF}, BB_REF=${BB_REF:-<off>})"
  cp .env.example .env
  chmod 600 .env
  # Only lines that START with the key: the example's comments quote these names too.
  setkv() { sed -i "s|^$1=.*|$1=$2|" .env; }
  setkv DOMAIN "$DOMAIN"
  setkv PUBLIC_HOST "$DOMAIN"
  setkv ACME_EMAIL "$ACME_EMAIL"
  setkv ER_REF "$ER_REF"
  setkv BB_REF "$BB_REF"
else
  say ".env already present -- keeping it"
fi
# shellcheck disable=SC1091
set -a; . ./.env; set +a
case "${ACME_EMAIL:-}" in ""|*example.com*) die "ACME_EMAIL in .env is empty or a placeholder";; esac

# ---- 4. firewall: the room range comes from .env, so it cannot drift from compose ---------------
say "firewall (ssh, 80/443, rooms ${PORT_START:-38400}-${PORT_END:-38599})"
ufw allow OpenSSH >/dev/null
ufw allow 80,443/tcp >/dev/null
ufw allow "${PORT_START:-38400}:${PORT_END:-38599}/tcp" >/dev/null
ufw --force enable >/dev/null

# ---- 5. static trees + their promotion scripts --------------------------------------------------
say "static pages"
mkdir -p "${ER_HOST_STATIC_DIR:-/srv/er}" "${BB_HOST_STATIC_DIR:-/srv/bb}"
curl -fsSL "${ER_RAW}/tools/deploy_wizard.sh" -o /root/deploy_wizard.sh
chmod +x /root/deploy_wizard.sh
# 🛑 env -u: .env exports ER_REPO/BB_REPO as full git URLs for the Docker build, while the deploy
# scripts read the same names as `owner/repo` -- inherited, they fetch raw.githubusercontent.com/https://...
env -u ER_REPO -u BB_REPO ER_STATIC_DIR="${ER_HOST_STATIC_DIR:-/srv/er}" /root/deploy_wizard.sh --landing
if [ -n "${BB_REF:-}" ]; then
  curl -fsSL "${BB_RAW}/tools/deploy_site.sh" -o /root/deploy_site.sh
  chmod +x /root/deploy_site.sh
  env -u ER_REPO -u BB_REPO BB_STATIC_DIR="${BB_HOST_STATIC_DIR:-/srv/bb}" /root/deploy_site.sh
fi

# ---- 6. optional restore, BEFORE the stack starts so rooms come up with their saves -------------
if [ -n "${RESTORE_FROM:-}" ]; then
  say "restore from ${RESTORE_FROM}"
  "${COMPOSE_DIR}/backup.sh" restore "$RESTORE_FROM"
fi

# ---- 7. up -------------------------------------------------------------------------------------
say "docker compose up -d --build (first build pulls every world; expect several minutes)"
docker compose up -d --build

# ---- 8. nightly backup -------------------------------------------------------------------------
say "nightly backup cron (04:10)"
cat > /etc/cron.d/peliarch-backup <<CRON
10 4 * * * root ${COMPOSE_DIR}/backup.sh >>/var/log/peliarch-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/peliarch-backup

IP="$(curl -fsS -4 https://ifconfig.me 2>/dev/null || echo '<this box>')"
cat <<NEXT

Stack is up. Before DNS moves:

  ${COMPOSE_DIR}/smoke.sh ${IP} ${DOMAIN}          # add SMOKE_INSECURE=1 until DNS points here

Then, at Porkbun, set the A records for ${DOMAIN} and www to ${IP}.
Caddy orders the certificate once the name resolves here:  docker compose logs -f caddy

Backups land in \${BACKUP_DIR:-/root/backups}. Set BACKUP_REMOTE in .env to push them OFF this box
(a Hetzner Storage Box works over rsync/ssh) -- a backup on the same disk did not survive the
outage this script was written after.
NEXT
