#!/usr/bin/env bash
# smoke.sh NEWBOXIP [DOMAIN] -- prove the new box serves the whole site BEFORE DNS points at it.
#
#   ./smoke.sh 46.62.130.40                 # DOMAIN defaults to peliarch.ca
#   ./smoke.sh 46.62.130.40 peliarch.ca
#   SMOKE_INSECURE=1 ./smoke.sh 46.62.130.40    # accept a self-signed / ACME-pending cert
#
# Every request goes to the NEW box by IP but carries the REAL hostname, via `curl --resolve`.
# That matters for more than TLS SNI: Caddy's site block is keyed on {$DOMAIN}, so a request with
# the wrong Host header does not reach the reverse_proxy at all and you would be testing Caddy's
# fallback instead of the site.
#
# 🛑 CERT ORDERING. Until the A record moves, Caddy on the new box cannot answer an HTTP-01
# challenge for peliarch.ca, so it will serve either the cert carried over in the `caddy_data`
# volume (the recommended path -- MIGRATION.md step N4) or an internal self-signed one. If you
# took the self-signed path, run with SMOKE_INSECURE=1; a green run under -k proves everything
# except the cert, which the post-cutover check in MIGRATION.md step V2 covers.
#
# Checks HTTP status AND a distinguishing string per page, because a 200 proves the web server is
# up and proves nothing about what it served: a mis-set BB_HOST_STATIC_DIR gives an empty
# /bb-static, which is the supported "not deployed" state and answers 404 -- while a stale bind of
# /srv/er over /bb-static would answer 200 with the WRONG GAME'S landing page.
set -euo pipefail

IP="${1:-}"
DOMAIN="${2:-${SMOKE_DOMAIN:-peliarch.ca}}"
[ -n "$IP" ] || { echo "usage: $0 NEWBOXIP [DOMAIN]" >&2; exit 2; }

CURL_OPTS=(--silent --show-error --location --max-time 20
           --resolve "${DOMAIN}:443:${IP}" --resolve "${DOMAIN}:80:${IP}")
if [ "${SMOKE_INSECURE:-0}" = "1" ]; then CURL_OPTS+=(--insecure); fi

BASE="https://${DOMAIN}"
PASS=0
FAIL=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# check PATH EXPECTED_STATUS NEEDLE LABEL
check() {
  local path="$1" want="$2" needle="$3" label="$4" body status
  body="${TMP}/body"
  status="$(curl "${CURL_OPTS[@]}" -o "$body" -w '%{http_code}' "${BASE}${path}")" || status="000"
  if [ "$status" != "$want" ]; then
    printf '  FAIL %-26s %-14s HTTP %s (wanted %s)\n' "$path" "$label" "$status" "$want"
    FAIL=$((FAIL + 1))
    return 0
  fi
  if [ -n "$needle" ] && ! grep -Fq "$needle" "$body"; then
    printf '  FAIL %-26s %-14s HTTP %s but does not contain %s\n' "$path" "$label" "$status" "$needle"
    FAIL=$((FAIL + 1))
    return 0
  fi
  printf '  ok   %-26s %-14s HTTP %s\n' "$path" "$label" "$status"
  PASS=$((PASS + 1))
}

echo "smoke: ${BASE} --resolve-> ${IP}"
if [ "${SMOKE_INSECURE:-0}" = "1" ]; then
  echo "       (TLS verification DISABLED -- cert is not being tested)"
fi
echo ""

# ---- shared surfaces --------------------------------------------------------------------------
echo "site:"
# `/` is er-archipelago's landing.html served out of ER_STATIC_DIR, NOT a template: on a box
# where /srv/er was never populated the route answers 503 with a "not deployed" stub, so a
# needle from the real page is what separates "migrated" from "empty box that boots".
check "/"           200 "Elden Ring"  "landing"
check "/hosting"    200 "Your Rooms"  "hosting"
check "/downloads"  200 "Downloads"   "downloads"

# ---- per game: root, builder, check browser ----------------------------------------------------
# The rows here mirror webgui/games.py: ER's builder IS `/er/` (empty builder_path) and its
# default_file is the wizard; Bloodborne's `/bb/` is a landing page and its builder is
# /bb/wizard.html. deploy/docker/test_runbook.py asserts this list against that table so a third
# game cannot be added to the site and forgotten here.
echo ""
echo "Elden Ring:"
check "/er/"              200 "er-options-metadata" "root+builder"
check "/er/checks.html"   200 "id=\"mapslot\""     "checks"

echo ""
echo "Bloodborne:"
check "/bb/"              200 "Bloodborne"          "landing"
check "/bb/wizard.html"   200 "bb-options-metadata" "builder"
check "/bb/checks.html"   200 "bb-checks"           "checks"

# ---- /bb/latest.json must agree with the ledger ------------------------------------------------
# The deploy script already refuses to install a latest.json that disagrees with CHANNELS.tsv, but
# that check ran on whatever box last deployed. This one asks the two live sources -- the served
# file and the ledger on GitHub -- the same question, which is the only form that catches "the
# right file was installed on the wrong box".
echo ""
echo "channel:"
LEDGER_URL="${SMOKE_CHANNELS_RAW_URL:-https://raw.githubusercontent.com/4laric/bb-archipelago/main/release/CHANNELS.tsv}"
stable_tag="$(curl -fsSL --max-time 20 "$LEDGER_URL" \
  | awk -F'\t' '!/^#/ && $1=="stable" { t=$2 } END { print t }')" || stable_tag=""
if [ -z "$stable_tag" ]; then
  echo "  FAIL ledger                    could not read the stable row from ${LEDGER_URL}"
  FAIL=$((FAIL + 1))
else
  want_ver="${stable_tag#v}"
  lj="${TMP}/latest.json"
  st="$(curl "${CURL_OPTS[@]}" -o "$lj" -w '%{http_code}' "${BASE}/bb/latest.json")" || st="000"
  if [ "$st" != "200" ]; then
    printf '  FAIL %-26s %-14s HTTP %s (wanted 200)\n' "/bb/latest.json" "verdict" "$st"
    FAIL=$((FAIL + 1))
  elif ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$lj" 2>/dev/null; then
    echo "  FAIL /bb/latest.json           is not valid JSON"
    FAIL=$((FAIL + 1))
  else
    got_ver="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' "$lj")"
    if [ "$got_ver" = "$want_ver" ]; then
      printf '  ok   %-26s %-14s version %s == stable %s\n' "/bb/latest.json" "verdict" "$got_ver" "$stable_tag"
      PASS=$((PASS + 1))
    else
      printf '  FAIL %-26s version %s but the ledger says stable is %s\n' "/bb/latest.json" "$got_ver" "$stable_tag"
      FAIL=$((FAIL + 1))
    fi
  fi
fi

echo ""
echo "${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "DO NOT CUT DNS OVER. A 404 on /bb/* usually means BB_HOST_STATIC_DIR is empty or unset --"
  echo "run bb-archipelago's tools/deploy_site.sh and recreate web (MIGRATION.md step N6)."
  exit 1
fi
echo ""
echo "Static surfaces are good. smoke.sh does NOT cover the two things only a human can:"
echo "  1. upload a small .archipelago at ${BASE} and confirm a room is created"
echo "  2. connect an AP client to ws://${IP}:<that room's port> and see it handshake"
echo "Do both before the DNS cutover (MIGRATION.md step S3)."
