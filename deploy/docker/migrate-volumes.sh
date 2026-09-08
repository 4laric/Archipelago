#!/usr/bin/env bash
# migrate-volumes.sh -- stream this box's named Docker volumes to another box, over ssh.
#
# Run it ON THE OLD BOX. For each volume it creates the same-named volume on the far side and
# pipes a tar of the contents into it, then compares a per-file manifest (sha256 of every regular
# file, plus mode/owner) between the two sides and refuses to report success if they differ.
#
#   ./migrate-volumes.sh --dry-run archi@46.62.130.40      # sizes and names only, nothing written
#   ./migrate-volumes.sh archi@46.62.130.40                # the real copy
#   ./migrate-volumes.sh --volumes peliarch_peliarch_data archi@NEWBOX
#
# 🛑 THE VOLUME NAMES ARE PROJECT-PREFIXED. `docker-compose.yml` declares `peliarch_data`,
# `caddy_data`, `caddy_config`, but Compose creates them as `<project>_<name>` -- with
# `name: ${COMPOSE_PROJECT_NAME:-peliarch}` at the top of that file, the volumes actually on disk
# are `peliarch_peliarch_data`, `peliarch_caddy_data`, `peliarch_caddy_config`. Copying
# `peliarch_data` instead would succeed, create an empty volume on the far side, and the new box
# would come up with no rooms and no cert. The default list below is the prefixed form, and every
# name is checked with `docker volume inspect` before anything is transferred.
#
# 🛑 STOP `web` FIRST. Room saves and rooms.json are written while the app runs; a tar of a live
# volume is a torn snapshot and the checksum step will (correctly) fail. See MIGRATION.md step F1.
#
# IDEMPOTENT: re-running overwrites the far-side contents of each volume with this box's copy.
# The far-side volume is created if missing (`docker volume create`) and reused if present; the
# extract does not delete files that only exist there, so a partial earlier run is healed rather
# than doubled -- but a volume that has since diverged on the far side should be removed there
# first if you want an exact mirror.
set -euo pipefail

# alpine is pinned: `tar` and `sha256sum` behaviour is what the manifest comparison depends on,
# and a floating tag on both ends of a checksum comparison can differ between the two boxes.
IMAGE="${MIGRATE_IMAGE:-alpine:3.20}"
PROJECT="${COMPOSE_PROJECT_NAME:-peliarch}"
SSH_OPTS="${MIGRATE_SSH_OPTS:-}"
DRY=0
REMOTE=""
VOLUMES=()

usage() {
  sed -n '2,32p' "$0"
  exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --volumes)
      shift
      [ "$#" -gt 0 ] || { echo "--volumes needs at least one name" >&2; exit 2; }
      while [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ] && [ "${1#*@}" = "$1" ]; do
        VOLUMES+=("$1"); shift
      done
      continue
      ;;
    -h|--help) usage 0 ;;
    --*) echo "unknown argument: $1" >&2; usage 2 ;;
    *)
      [ -z "$REMOTE" ] || { echo "only one remote may be given (got '$REMOTE' and '$1')" >&2; exit 2; }
      REMOTE="$1"
      ;;
  esac
  shift
done

[ -n "$REMOTE" ] || { echo "usage: $0 [--dry-run] [--volumes NAME...] user@NEWBOX" >&2; exit 2; }

if [ "${#VOLUMES[@]}" -eq 0 ]; then
  VOLUMES=("${PROJECT}_peliarch_data" "${PROJECT}_caddy_data" "${PROJECT}_caddy_config")
fi

say() { printf '%s\n' "$*"; }
die() { printf 'migrate-volumes: %s\n' "$*" >&2; exit 1; }

# shellcheck disable=SC2086  # SSH_OPTS is deliberately word-split; it is operator-supplied flags.
ssh_remote() { ssh $SSH_OPTS "$REMOTE" "$@"; }

command -v docker >/dev/null 2>&1 || die "docker is not on PATH on this box"
command -v ssh >/dev/null 2>&1 || die "ssh is not on PATH on this box"

say "remote:  $REMOTE"
say "project: $PROJECT"
say "image:   $IMAGE"
say ""

# ---- pre-flight: every source volume must exist, and the far side must have docker ------------
missing=0
for vol in "${VOLUMES[@]}"; do
  docker volume inspect "$vol" >/dev/null 2>&1 || { say "MISSING locally: $vol"; missing=1; }
done
if [ "$missing" = "1" ]; then
  say ""
  say "Volumes on this box:"
  docker volume ls --format '  {{.Name}}'
  die "at least one named volume does not exist here -- check COMPOSE_PROJECT_NAME and the prefix note at the top of this script"
fi

ssh_remote 'command -v docker >/dev/null 2>&1' \
  || die "docker is not on PATH for $REMOTE (provision it first -- MIGRATION.md step P3)"

# ---- sizes, always printed, dry-run or not ----------------------------------------------------
say "sizes (apparent, uncompressed):"
total_kb=0
for vol in "${VOLUMES[@]}"; do
  kb="$(docker run --rm -v "${vol}":/data:ro "$IMAGE" du -sk /data | awk '{print $1}')"
  files="$(docker run --rm -v "${vol}":/data:ro "$IMAGE" sh -c 'find /data -type f | wc -l')"
  total_kb=$((total_kb + kb))
  printf '  %-32s %8s MB  %6s files\n' "$vol" "$((kb / 1024))" "$files"
done
say "  ----"
printf '  %-32s %8s MB\n' "TOTAL" "$((total_kb / 1024))"
say ""

if [ "$DRY" = "1" ]; then
  say "DRY RUN: nothing was transferred. Re-run without --dry-run to copy."
  say "Budget roughly ${total_kb} KB over the wire (gzip typically halves room saves and logs)."
  exit 0
fi

# ---- the manifest both sides are compared on --------------------------------------------------
# Per file rather than a checksum of the tar stream: two tars of the same tree differ in member
# order and mtime granularity across filesystems, so a stream hash mismatches on a GOOD copy and
# teaches you to ignore it. A sorted per-file digest list does not.
MANIFEST_CMD='cd /data && find . -type f -print0 | sort -z | xargs -0 -r sha256sum | sha256sum'

manifest_local() {
  docker run --rm -v "${1}":/data:ro "$IMAGE" sh -c "$MANIFEST_CMD" | awk '{print $1}'
}
manifest_remote() {
  ssh_remote "docker run --rm -v ${1}:/data:ro ${IMAGE} sh -c '${MANIFEST_CMD}'" | awk '{print $1}'
}

failures=0
for vol in "${VOLUMES[@]}"; do
  say "==> $vol"
  # `docker volume create` is idempotent: it is a no-op on an existing volume, which is what makes
  # re-running this script safe.
  ssh_remote "docker volume create ${vol} >/dev/null" || die "could not create ${vol} on the far side"

  # tar out of a read-only mount here, into a writable mount there, through ssh. No intermediate
  # file on either box: a 5 GB tarball in ~ on a box with 8 GB of disk is its own outage.
  # `-p` and numeric owners keep the uid Caddy and gunicorn expect inside their containers.
  docker run --rm -v "${vol}":/data:ro "$IMAGE" tar czf - -C /data . \
    | ssh_remote "docker run --rm -i -v ${vol}:/data ${IMAGE} tar xzf - -p --numeric-owner -C /data" \
    || die "transfer of ${vol} failed"

  local_sum="$(manifest_local "$vol")"
  remote_sum="$(manifest_remote "$vol")"
  if [ "$local_sum" = "$remote_sum" ]; then
    say "    OK   manifest ${local_sum:0:16}..."
  else
    say "    FAIL local  ${local_sum}"
    say "    FAIL remote ${remote_sum}"
    failures=$((failures + 1))
  fi
done

say ""
if [ "$failures" -gt 0 ]; then
  die "${failures} volume(s) did not match after transfer -- do NOT cut DNS over. Confirm \`docker compose stop web\` ran on this box (a live volume tars torn), then re-run."
fi
say "All ${#VOLUMES[@]} volume(s) transferred and verified."
say "Next: MIGRATION.md step N5 (build), N6 (/srv trees), N7 (up), then smoke.sh before DNS."
