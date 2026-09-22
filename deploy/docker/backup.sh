#!/usr/bin/env bash
# backup.sh -- snapshot the stateful half of Peliarch, and put it somewhere that survives the box.
#
#   ./backup.sh                       # back up (this is what cron runs)
#   ./backup.sh restore DIR           # restore a backup directory into the volumes, then start web
#
# WHAT IS STATEFUL: the `peliarch_data` volume (uploads, room saves, logs, rooms.json), the
# `caddy_data` volume (the ACME account and certificates -- restoring it avoids re-ordering a cert),
# and `.env`, which is NOT in git and is the one file that cannot be rebuilt from the repo.
# Everything else (image, static pages) is reproducible from GitHub.
#
# Settings, from the environment or from .env beside this script:
#   BACKUP_DIR     default /root/backups
#   BACKUP_KEEP    default 14 -- local directories to keep
#   BACKUP_REMOTE  optional rsync target, e.g. u123456@u123456.your-storagebox.de:peliarch
#                  (Hetzner Storage Box: ssh on port 23, so also set BACKUP_RSYNC_SSH="ssh -p 23")
#
# 🛑 A LOCAL-ONLY BACKUP IS NOT A BACKUP. On 2026-09-21 the server itself became unreachable and
# every room went with it. BACKUP_REMOTE is what turns this into something that would have helped;
# the script says so on every run where it is unset.
#
# Rooms are live while this runs, so a save file can be captured mid-write. MultiServer saves are
# pickles it rewrites whole; the worst case is one room rolling back to its previous autosave.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${HERE}/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "${HERE}/.env"; set +a
fi
PROJECT="${COMPOSE_PROJECT_NAME:-peliarch}"
# Compose prefixes volume names with the project (MIGRATION.md P2): the unprefixed name would
# "succeed" and archive an empty volume.
VOLUMES=("${PROJECT}_peliarch_data" "${PROJECT}_caddy_data")
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
KEEP="${BACKUP_KEEP:-14}"

die() { printf 'backup: %s\n' "$*" >&2; exit 1; }

cmd="${1:-backup}"
case "$cmd" in
  backup)
    for v in "${VOLUMES[@]}"; do
      docker volume inspect "$v" >/dev/null 2>&1 || die "volume ${v} does not exist (project name wrong?)"
    done
    stamp="$(date +%F-%H%M)"
    dest="${BACKUP_DIR}/${stamp}"
    mkdir -p "$dest"
    umask 077
    for v in "${VOLUMES[@]}"; do
      docker run --rm -v "${v}:/data:ro" -v "${dest}:/out" alpine \
        tar czf "/out/${v#"${PROJECT}_"}.tgz" -C /data .
    done
    cp "${HERE}/.env" "${dest}/env" 2>/dev/null || echo "backup: WARNING no .env beside this script"
    # A backup that cannot be read back is worse than none: list every archive once.
    for f in "$dest"/*.tgz; do tar tzf "$f" >/dev/null || die "${f} is not a readable archive"; done
    echo "backup: ${dest} ($(du -sh "$dest" | cut -f1))"

    # keep the newest $KEEP local directories
    # shellcheck disable=SC2012
    ls -1d "${BACKUP_DIR}"/20* 2>/dev/null | sort | head -n "-${KEEP}" | xargs -r rm -rf

    if [ -n "${BACKUP_REMOTE:-}" ]; then
      rsync -a -e "${BACKUP_RSYNC_SSH:-ssh}" "${BACKUP_DIR}/" "${BACKUP_REMOTE}/"
      echo "backup: pushed to ${BACKUP_REMOTE}"
    else
      echo "backup: WARNING BACKUP_REMOTE is unset -- this copy dies with the box" >&2
    fi
    ;;
  restore)
    src="${2:-}"
    [ -d "$src" ] || die "usage: $0 restore BACKUP_DIR"
    for v in "${VOLUMES[@]}"; do
      [ -f "${src}/${v#"${PROJECT}_"}.tgz" ] || die "${src} has no ${v#"${PROJECT}_"}.tgz"
    done
    ( cd "$HERE" && docker compose stop web 2>/dev/null || true )
    for v in "${VOLUMES[@]}"; do
      docker volume create "$v" >/dev/null
      docker run --rm -v "${v}:/data" -v "$(cd "$src" && pwd):/in:ro" alpine \
        sh -c "rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /in/${v#"${PROJECT}_"}.tgz -C /data"
    done
    [ -f "${HERE}/.env" ] || { cp "${src}/env" "${HERE}/.env" && chmod 600 "${HERE}/.env"; }
    echo "restore: ${src} restored. Start the stack with: cd ${HERE} && docker compose up -d"
    ;;
  *) die "usage: $0 [backup | restore DIR]" ;;
esac
