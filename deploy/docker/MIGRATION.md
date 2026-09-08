# Bloodborne go-live and the move to a bigger box

Two jobs that share most of their steps, so they are one runbook:

- **Track A — Bloodborne go-live.** Set `BB_REF`, populate `/srv/bb`, rebuild, verify `/bb/`.
  Rollout step 3 of `docs/SPEC-peliarch-bloodborne.md` in bb-archipelago.
- **Track B — migration.** Move the whole deployment from the current CX23 to a bigger Hetzner
  box (`NEWBOX`), carrying the three named Docker volumes and both `/srv` static trees.

**Do them in one maintenance window, in this order: provision → migrate → go live on NEWBOX →
smoke → DNS.** Track A costs an image rebuild (`BB_REF` is a build arg), and the migration
rebuilds the image on NEWBOX anyway. Doing Bloodborne first on the old box means paying for that
rebuild twice and cutting DNS over to a configuration nobody has smoke-tested. If you only want
Track A today, run steps **G1–G5** on the current box and stop; nothing in them depends on NEWBOX.

> ✅ **This migration was executed on 2026-09-08 and is complete.** Everything below is kept as the
> record of what was done, and as the procedure for the next move. The resulting live state is
> `OPERATIONS.md` → "Live deployment"; the leftover follow-ups are in its "Known follow-ups".
> The only open item from this runbook is **X1**, decommissioning `135.181.100.88` on or after
> **2026-09-15**.

Placeholders used throughout — substitute before pasting:

| Placeholder | Meaning | Today |
|---|---|---|
| `OLDBOX` | current server IPv4 | `135.181.100.88` (CX23, Helsinki, Ubuntu 26.04) |
| `NEWBOX` | target server IPv4 | `46.62.130.40` — **confirmed**; `ubuntu-16gb-hel1-1`, Helsinki, 8 vCPU / 15 GB RAM / 150 GB disk, Ubuntu 26.04 (risk R4 resolved) |
| `DOMAIN` | the site hostname | `peliarch.ca` |
| `BB_TAG` | Bloodborne stable tag | `v0.1.0.2` (being promoted to non-prerelease in parallel; **it must be the `stable` row of bb-archipelago's `release/CHANNELS.tsv` before you start** — see risk R2) |

---

## Checklist

Work top to bottom. Nothing below the DNS line is reversible in seconds, everything above it is.

| # | Step | Track | Owner | Status |
|---|---|---|---|---|
| **P1** | Lower DNS TTL for `DOMAIN` and `www` to 300s — **≥24 h before the window** | B | | ✅ |
| **P2** | Record current `.env`, published port range, `ufw` rules, volume sizes | A+B | | ✅ |
| **P3** | Confirm no active rooms, or announce the window | A+B | | ✅ |
| **P4** | Confirm `BB_TAG` is the `stable` row in bb-archipelago `release/CHANNELS.tsv` | A | | ✅ |
| **N1** | Provision NEWBOX: `archi` user, Docker Engine + compose plugin, `ufw`, (optional) fail2ban | B | | ✅ |
| **N2** | Clone `4laric/Archipelago` at a **pinned commit** into `~/Archipelago` | B | | ✅ |
| **N3** | Copy `.env` across; set `BB_REF=BB_TAG`, `BB_HOST_STATIC_DIR=/srv/bb`, leave `ER_REF` alone | A+B | | ✅ |
| **F1** | **Freeze OLDBOX**: `docker compose stop web` | B | | ✅ |
| **F2** | Transfer `peliarch_data`, `caddy_data`, `caddy_config` with `./migrate-volumes.sh` | B | | ✅ |
| **F3** | Transfer `/srv/er` | B | | ✅ |
| **N4** | Confirm the carried-over cert is in `caddy_data` (this is the cert-ordering answer) | B | | ✅ |
| **N5** | `docker compose build` on NEWBOX with **both** refs | A+B | | ✅ |
| **N6** | Populate `/srv/bb` (bb `deploy_site.sh`) and re-run er `deploy_wizard.sh` for `/srv/er` | A+B | | ✅ |
| **N7** | `docker compose up -d` | A+B | | ✅ |
| **S1** | `./smoke.sh NEWBOX DOMAIN` — every page, pre-DNS | A+B | | ✅ |
| **S2** | Room count and `rooms.json` match OLDBOX | B | | ✅ |
| **S3** | Manual: upload a small `.archipelago`, connect an AP client to `ws://NEWBOX:PORT` | A+B | | ✅ |
| **D1** | **DNS cutover**: A records for apex and `www` → NEWBOX | B | | ✅ |
| **V1** | Cert: `docker compose logs caddy`, `curl -vI https://DOMAIN` from off-box | B | | ✅ |
| **V2** | Set `BB_LIVE=1` Actions variable; run both parity workflows' `live` jobs | A | | ✅ |
| **V3** | Raise TTL back to 3600s | B | | ✅ |
| **V4** | Update the doc references in the appendix (old IP, box size, port range) | A+B | | ✅ |
| **X1** | After N=7 days with no rollback: final backup, then delete OLDBOX | B | | ☐ **due 2026-09-15** |

**Rollback line:** everything up to and including **S3** is undone by doing nothing — OLDBOX still
holds every byte, because F1 only *stopped* `web`. After **D1**, rollback is "point DNS back and
`docker compose start web` on OLDBOX" (see [Rollback](#rollback)).

---

## P — Pre-flight

### P1. Lower the TTL (a day ahead, not in the window)

At the registrar/DNS host for `peliarch.ca`, set the TTL on the apex `A` and the `www` record to
**300**. Do this **at least 24 hours before** the window: lowering a TTL only takes effect after
the *old* TTL expires everywhere, so a TTL dropped an hour before the cutover buys you nothing and
a stale resolver still sends players to a stopped box.

```bash
dig +noall +answer peliarch.ca A www.peliarch.ca A     # confirm TTL 300 the morning of
```

### P2. Record what the box is today

Run on OLDBOX as `archi` and **keep the output** — this is what you compare against afterwards.

```bash
cd ~/Archipelago/deploy/docker
git rev-parse HEAD                                   # the commit NEWBOX will be pinned to
cp .env ~/env-backup-$(date +%F)                     # .env is NOT in git and never will be
docker compose config | grep -A3 '^ *ports:'         # the published range, as resolved
grep -E '^PORT_(START|END)=' .env
sudo ufw status numbered
docker volume ls
docker run --rm -v peliarch_peliarch_data:/data:ro alpine du -sh /data
ls /srv/er /srv/bb 2>&1
```

Two things to notice in that output:

- 🛑 **The volume names are project-prefixed.** `docker-compose.yml` declares `peliarch_data`,
  `caddy_data` and `caddy_config`; Compose creates them as `<project>_<name>`, and the project is
  `${COMPOSE_PROJECT_NAME:-peliarch}` — so on disk they are `peliarch_peliarch_data`,
  `peliarch_caddy_data` and `peliarch_caddy_config`. Copying the unprefixed names would "succeed"
  and give NEWBOX three empty volumes. `migrate-volumes.sh` defaults to the prefixed form and
  verifies each with `docker volume inspect` before it transfers anything.
- 🛑 **`OPERATIONS.md` says the room range is `38400–38463`. It is not** — compose and `.env` have
  said `38400-38599` since the 2026-08-13 alignment. Whatever `.env` says is the truth; the doc is
  stale and step V4 fixes it. Open the *real* range on NEWBOX's firewall or rooms above 38463 will
  be listening, healthy and unreachable.

### P3. No rooms mid-flight

```bash
curl -fsS -H 'Accept: application/json' https://peliarch.ca/rooms | jq '[.[] | select(.alive)] | length'
```

Zero is the easy case. Otherwise announce a window: a running room is a supervised subprocess, its
port is held for the life of the room, and **any** path through this runbook disconnects its
players — the freeze in F1 does, and so does `docker compose up -d --build` for Track A alone. The
room itself survives (state is in `peliarch_data`); the sessions do not.

### P4. The Bloodborne ledger must already say `BB_TAG`

`tools/deploy_site.sh` and `/downloads` both resolve "stable" from bb-archipelago's
`release/CHANNELS.tsv` at `main`, **not** from GitHub's prerelease checkbox and not from `BB_REF`.
If you pin `BB_REF=v0.1.0.2` while the ledger still says `v0.1.0-beta.5`, the build installs one
apworld and the site serves another game's pages against it — the exact drift the pin exists to
prevent, and `smoke.sh`'s ledger check is what catches it.

```bash
curl -fsSL https://raw.githubusercontent.com/4laric/bb-archipelago/main/release/CHANNELS.tsv \
  | awk -F'\t' '!/^#/ && $1=="stable" { t=$2 } END { print t }'      # must print v0.1.0.2
```

---

## N — Provision NEWBOX

> **What was actually done on 2026-09-08, where it differs from the steps below:** the stack runs as
> **`root`**, not as an `archi` user, and the repo lives at **`/root/Archipelago`**. Docker came from
> the Ubuntu packages `docker.io` (29.1.3) and `docker-compose-v2` (2.40.3) rather than Docker's own
> apt repo. `ufw` was opened for `OpenSSH`, `80,443/tcp` and the **full** `38400:38599/tcp` range
> (see risk R5). Substitute `archi@` → `root@` and `~/Archipelago` → `/root/Archipelago` when
> re-reading any command below as history rather than as instructions.

### N1. Base system

As `root` over ssh, on the fresh Ubuntu image:

```bash
adduser --disabled-password --gecos "" archi
usermod -aG sudo archi
echo 'archi ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/archi
install -d -m 700 -o archi -g archi /home/archi/.ssh
cp /root/.ssh/authorized_keys /home/archi/.ssh/
chown archi:archi /home/archi/.ssh/authorized_keys
```

Docker Engine + the compose plugin (the distro's `docker.io` has no `docker compose`):

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker archi
docker compose version        # must print v2.x — if this fails, nothing below works
```

Firewall. **The room range must match `PORT_START..PORT_END` from `.env`** (P2), not the number in
`OPERATIONS.md`:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 38400:38599/tcp     # ← the RESOLVED range from P2
ufw --force enable
ufw status numbered
```

Optional but cheap — `ssh` on a public IPv4 is scanned continuously:

```bash
apt-get install -y fail2ban
systemctl enable --now fail2ban
```

> Docker publishes ports by writing `iptables` DNAT rules in the `DOCKER` chain, which is
> consulted **before** `ufw`'s. So `ufw` is defence for the host's own services, and the published
> room range is reachable whether or not you added the rule — add it anyway, so the intended
> surface is written down somewhere a human reads.
>
> Before widening the range later, read the `docker-proxy` note in `docker-compose.yml`: the cost
> is one process per **published port**, not per room, unless `"userland-proxy": false` is set in
> `/etc/docker/daemon.json`. With 200 ports that is already a few hundred MB resident at idle,
> which is a real line item on a small box.

### N2. Clone at a pinned commit

```bash
sudo -iu archi
git clone https://github.com/4laric/Archipelago.git ~/Archipelago
cd ~/Archipelago
git checkout <the SHA recorded in P2>      # a pin, not `main`
git rev-parse HEAD
sudo mkdir -p /srv/er /srv/bb && sudo chown -R archi:archi /srv/er /srv/bb
```

🛑 `/srv/bb` **must exist before `up`, even empty.** Docker creates a missing bind source as an
empty directory, and an empty `/bb-static` is indistinguishable from "Bloodborne not deployed
here": `/bb/` 404s and nothing else changes. That is a supported state, so it fails silently.

### N3. `.env`

`.env` is **not in git** and must not be — it carries `ACME_EMAIL` and optionally
`DOWNLOADS_GITHUB_TOKEN`. Copy the one from P2, then change exactly three things:

```bash
scp ~/env-backup-$(date +%F) archi@NEWBOX:~/Archipelago/deploy/docker/.env   # from your laptop
```

On NEWBOX, edit `~/Archipelago/deploy/docker/.env`:

```ini
BB_REF=v0.1.0.2              # was blank. Immutable tag or full SHA; validate-ref.sh rejects branches.
BB_HOST_STATIC_DIR=/srv/bb   # confirm it is set and the directory exists
# ER_REF: DO NOT TOUCH. Changing it here turns a migration into an ER upgrade,
#         and then a failure has two candidate causes instead of one.
```

Everything else — `DOMAIN`, `PUBLIC_HOST`, `PORT_START`, `PORT_END`, the whole
`DOWNLOADS_*`/`BB_DOWNLOADS_*` block, `GENERATE_*` — carries over **unchanged**. The full key
inventory is in [Appendix B](#appendix-b--every-env-key). Sanity-check the file parses:

```bash
cd ~/Archipelago/deploy/docker
docker compose config --quiet && echo "compose config OK"
grep -n 'GENERATE_PLANDO' .env    # must be a BARE `GENERATE_PLANDO=` with no inline comment
```

---

## F — Freeze and transfer (the window starts here)

### F1. Stop `web` on OLDBOX

```bash
ssh archi@OLDBOX
cd ~/Archipelago/deploy/docker
docker compose stop web           # NOT `down`, and NOT `down -v`
docker compose ps
```

`stop` and not `down`: the volumes, the built image and the `.env` all stay exactly where they
are, which is what makes rollback a DNS change rather than a restore. From here on nothing writes
to `peliarch_data`, so the tar in F2 is a consistent snapshot. Leave `caddy` running — it keeps
answering on the old IP (and keeps renewing the cert) right up to the cutover.

### F2. Transfer the three volumes

`migrate-volumes.sh` lives beside this file. Copy it to OLDBOX and run it **there**:

```bash
scp deploy/docker/migrate-volumes.sh archi@OLDBOX:~/
ssh archi@OLDBOX
chmod +x ~/migrate-volumes.sh
~/migrate-volumes.sh --dry-run archi@NEWBOX      # names + sizes, writes nothing
~/migrate-volumes.sh archi@NEWBOX                # the real copy, with a per-file checksum compare
```

It streams each of `peliarch_data`, `caddy_data` and `caddy_config` (in their prefixed form)
straight over ssh into a same-named volume on the far side, with no intermediate tarball on either
disk, then compares a sorted per-file `sha256sum` manifest between the two and exits non-zero if
they differ. **`ssh archi@NEWBOX` must work from OLDBOX without a password** — put OLDBOX's public
key in NEWBOX's `~archi/.ssh/authorized_keys` first, and `ssh archi@NEWBOX true` once to accept the
host key, or the pipeline hangs on a prompt with a tar half-written.

If you would rather have a file you can keep, the backup-and-copy form is equivalent:

```bash
# on OLDBOX
for v in peliarch_peliarch_data peliarch_caddy_data peliarch_caddy_config; do
  docker run --rm -v "$v":/data:ro -v "$PWD":/backup alpine:3.20 \
    tar czf "/backup/${v}-$(date +%F).tgz" -C /data .
done
ls -lh ./*.tgz
scp ./*.tgz archi@NEWBOX:~/

# on NEWBOX
for v in peliarch_peliarch_data peliarch_caddy_data peliarch_caddy_config; do
  docker volume create "$v"
  docker run --rm -v "$v":/data -v "$PWD":/backup alpine:3.20 \
    tar xzf "/backup/${v}-"*.tgz -p --numeric-owner -C /data
done
```

Either way, confirm the size on NEWBOX is within a few percent of the size recorded in P2:

```bash
ssh archi@NEWBOX 'docker run --rm -v peliarch_peliarch_data:/data:ro alpine:3.20 du -sh /data'
```

### F3. Transfer `/srv/er`

`/srv/er` is a host directory, not a volume, so `migrate-volumes.sh` does not touch it. It is
fully reproducible by re-running er-archipelago's `deploy_wizard.sh` (step N6), so this copy is
belt-and-braces — worth doing anyway, because it is seconds and it means a GitHub outage during
the window is not an outage for you:

```bash
rsync -a --delete /srv/er/ archi@NEWBOX:/srv/er/
ssh archi@NEWBOX 'ls -R /srv/er | head -30'
```

### N4. The cert — read this before you build

Caddy proves ownership over HTTP-01, which means the challenge has to reach whatever IP the A
record points at. NEWBOX cannot issue a cert for `peliarch.ca` until after the cutover. Three ways
out, in order of preference:

1. **Carry `caddy_data` over — recommended, and F2 already did it.** The volume holds the issued
   certificate and its key. Caddy on NEWBOX loads it, serves the existing cert immediately, and
   renews normally once DNS points here. Nothing extra to do, and `smoke.sh` runs with full TLS
   verification. Confirm it landed:
   ```bash
   ssh archi@NEWBOX 'docker run --rm -v peliarch_caddy_data:/data:ro alpine:3.20 \
     find /data -name "*.crt" -o -name "*.key" | head'
   ```
2. **Pre-issue via DNS-01** with a Caddy DNS-provider build. Correct, and a different Caddy image
   than the pinned `Caddyfile.Dockerfile` one — not worth introducing on cutover day.
3. **Accept a short ACME-pending window** and smoke-test with `SMOKE_INSECURE=1 ./smoke.sh` (which
   adds `curl -k`). Everything except the cert is still proven. Only do this if (1) failed.

### N5. Build

```bash
cd ~/Archipelago/deploy/docker
docker compose build          # ~10-20 min: the ertools and bbtools stages clone and install
docker compose run --rm --entrypoint sh web -c 'cat /app/.er-rev /app/.bb-rev'
```

Both refs are build args, so this is where `BB_REF` actually takes effect — an already-running
container does not pick it up from `.env`. If the build rejects a ref, that is
`deploy/docker/validate-ref.sh` refusing a moving branch name, which is deliberate.

### N6. Populate the static trees

```bash
# Bloodborne — needs only curl and bash; no checkout, no python on the box
curl -fsSL https://raw.githubusercontent.com/4laric/bb-archipelago/main/tools/deploy_site.sh \
  -o ~/deploy_site.sh
chmod +x ~/deploy_site.sh
BB_STATIC_DIR=/srv/bb ~/deploy_site.sh --dry-run     # prints the channel it resolved
BB_STATIC_DIR=/srv/bb ~/deploy_site.sh
ls -l /srv/bb /srv/bb/beta

# Elden Ring — same shape, from the er repo
git clone https://github.com/4laric/er-archipelago.git ~/er-archipelago
ER_STATIC_DIR=/srv/er ~/er-archipelago/tools/deploy_wizard.sh
```

Both scripts read their repo's `release/CHANNELS.tsv` at `main` to decide which tag to fetch, write
each file atomically (`mktemp` + `mv`), and refuse to install a page that does not contain its
sentinel string. `deploy_site.sh`'s first line of output names the channels it resolved — **check
that it says `stable -> v0.1.0.2`** before moving on. `--dry-run` first is free.

### N7. Start

```bash
docker compose up -d
docker compose ps                # web healthy (allow 90s start_period), caddy up, autoheal up
docker compose logs --tail=50 web
```

---

## S — Smoke tests, before DNS

### S1. Every page, by IP, with the real Host header

```bash
# from your laptop, from the repo checkout
./deploy/docker/smoke.sh NEWBOX peliarch.ca
# if you took cert path 3 in N4:
SMOKE_INSECURE=1 ./deploy/docker/smoke.sh NEWBOX peliarch.ca
```

It checks status **and** a distinguishing string for `/`, `/hosting`, `/downloads`, `/er/`,
`/er/checks.html`, `/bb/`, `/bb/wizard.html`, `/bb/checks.html`, and parses `/bb/latest.json` and
compares its `version` to the ledger's stable tag. `--resolve` is what makes it a test of NEWBOX
rather than of whatever DNS currently answers; the real Host header is required because Caddy's
site block is keyed on `{$DOMAIN}` and a wrong Host never reaches the reverse proxy at all.

### S2. The state actually came across

```bash
ssh archi@NEWBOX 'docker run --rm -v peliarch_peliarch_data:/data:ro alpine:3.20 \
  sh -c "wc -c /data/rooms.json; ls /data/server-logs | head"'
curl -s --resolve peliarch.ca:443:NEWBOX -H 'Accept: application/json' \
  https://peliarch.ca/rooms | jq 'length'      # same count as P3 saw on OLDBOX
```

### S3. The chain no static check can prove

A green container and a served page prove neither generation nor hosting works.

1. Upload a small `.archipelago` at `https://peliarch.ca/hosting` (via `--resolve`, or a hosts-file
   entry on your laptop). Confirm a room is created and starts.
2. Point a desktop AP client at `ws://NEWBOX:<that room's port>` — **by IP**, since DNS still says
   OLDBOX — and confirm the handshake. This is the only step that proves the published port range
   and the firewall agree with the allocator.
3. Delete the test room.

---

## D — DNS cutover

### D1. Move the A records

At the DNS host, change **both**:

```
peliarch.ca.       A   NEWBOX
www.peliarch.ca.   A   NEWBOX
```

`www` is easy to forget — it is still listed as an open follow-up in `OPERATIONS.md`, so it may
not exist at all; if it does not, this is the moment to add it. The `birdfuck.ca` /
`www.birdfuck.ca` redirect block in the `Caddyfile` also needs its A records moved, or that
redirect stops working and Caddy cannot renew its cert.

```bash
watch -n10 'dig +short peliarch.ca A; dig +short www.peliarch.ca A'
```

At TTL 300 the world follows within ~5 minutes. Leave OLDBOX's `caddy` running and `web` stopped
until X1: a straggling resolver reaching OLDBOX gets a 502 rather than a connection refused, which
is a nicer failure and a much louder one.

---

## V — After the cutover

### V1. Cert

```bash
ssh archi@NEWBOX 'cd ~/Archipelago/deploy/docker && docker compose logs caddy | tail -40'
curl -vI https://peliarch.ca 2>&1 | grep -E 'subject:|expire|HTTP/'
```

Look for `certificate obtained successfully` or a clean load of the carried-over cert, and **no**
repeated challenge failures. If you took cert path 3, this is where the real cert appears — it can
take a minute or two after the records propagate.

### V2. Parity workflows

```bash
gh variable set BB_LIVE --body 1 -R 4laric/Archipelago
gh workflow run bb-channel-parity.yml -R 4laric/Archipelago
gh workflow run er-channel-parity.yml -R 4laric/Archipelago
gh run watch -R 4laric/Archipelago
```

`BB_LIVE` gates the `live` job of `bb-channel-parity.yml` precisely because it fails against a 404
until `/bb/` serves. Setting it is the formal "Bloodborne is live" switch, and it must not be set
before S1 passes.

### V3. Restore the TTL

Put the apex and `www` TTL back to 3600 once you are confident. Leaving it at 300 is not harmful,
just chattier — but restore it, or the next migration's P1 will look like it is already done.

### V4. Fix the docs

See [Appendix A](#appendix-a--what-to-update-after-cutover).

---

## Rollback

Before **D1**: do nothing. DNS still points at OLDBOX; `docker compose start web` there and you are
exactly where you began. NEWBOX can be re-attempted or destroyed.

After **D1**:

```bash
# 1. DNS back to OLDBOX (both records)
# 2. on OLDBOX:
ssh archi@OLDBOX 'cd ~/Archipelago/deploy/docker && docker compose start web && docker compose ps'
```

🛑 **The window between F1 and the rollback is a state fork.** Any room created or advanced on
NEWBOX after the cutover exists only in NEWBOX's `peliarch_data`; OLDBOX's copy is frozen at F1.
Rolling back therefore *loses* post-cutover play. That is the honest cost and it is why S1–S3 come
before D1 — and why, after a rollback, the recovery is to migrate NEWBOX's volumes *back* with the
same script rather than to pretend the fork did not happen.

## Decommission

**Not before N=7 days** of NEWBOX running clean. Then:

```bash
ssh archi@OLDBOX
cd ~/Archipelago/deploy/docker
for v in peliarch_peliarch_data peliarch_caddy_data peliarch_caddy_config; do
  docker run --rm -v "$v":/data:ro -v "$PWD":/backup alpine:3.20 \
    tar czf "/backup/final-${v}-$(date +%F).tgz" -C /data .
done
# copy the tarballs OFF the box, verify they open, THEN delete the server in the Hetzner console.
```

Deleting the server releases the IPv4. If `135.181.100.88` is written down anywhere outside this
repo — a Discord pin, a bookmark, a monitoring check — that is the moment it starts pointing at a
stranger's box.

---

## Appendix A — what to update after cutover ✅ done

Every place in this repo that named the old box, its IP, or a stale port range. Done as one commit
after V1 passed, on 2026-09-08.

| File | Line / section | What was wrong | What it says now | Status |
|---|---|---|---|---|
| `OPERATIONS.md` | "Live deployment" → **Box** | Hetzner **CX23**, Helsinki, ~$7/mo | `ubuntu-16gb-hel1-1`, `46.62.130.40`, Helsinki, 8 vCPU / 15 GB RAM / 150 GB disk, Ubuntu 26.04 — plus the `root` / `/root/Archipelago` paths, the Docker package versions, the `ufw` rules, the `/srv/er` + `/srv/bb` trees and the two deploy scripts | ✅ |
| `OPERATIONS.md` | "Live deployment" → **Room ports** | duplicated, one copy saying `38400–38463` | a single entry, `38400–38599`, matching `.env`, Compose and `ufw` | ✅ |
| `OPERATIONS.md` | "Known follow-ups" → **`www` DNS** | listed as outstanding | deleted — D1 moved both A records at Porkbun and `www.peliarch.ca` resolves | ✅ |
| `OPERATIONS.md` | "Known follow-ups" → **Donation URL** | may still be the placeholder | checked in the migrated `.env`; still a placeholder, so the item stays | ✅ (kept) |
| `OPERATIONS.md` | new "Migration 2026-09-08" section | did not exist | what moved, the volume sizes and room count, the DNS move, the ER redeploy, and the old box's decommission date | ✅ |
| `OPERATIONS.md` | "Known follow-ups" | — | two new items found during the move: the placeholder `ACME_EMAIL` and the `birdfuck.ca` Caddy block | ✅ |
| `DEPLOY.md` | title line 1 | "Deploying Peliarch on a Hetzner **CX23**" | **left as-is on purpose** — the guide is still a CX23 walkthrough and the CX23 is still the right starting size; the Cost recap now points at what is actually live | ✅ (deliberate) |
| `DEPLOY.md` | placeholder table, `BOXIP` row | example `65.21.x.x` | confirmed an example, not mistakable for the real IP; unchanged | ✅ |
| `DEPLOY.md` | §5 firewall | opened `40000:40063/tcp`, a range nothing uses | `38400:38599/tcp`, matching `PORT_START..PORT_END` in `.env`, with a note saying they must match | ✅ |
| `DEPLOY.md` | "Cost recap" | "CX23 Helsinki ≈ $7.09/mo" | keeps the CX23 figure for the guide, then records that live is a 16 GB box and says to read the plan's price off the Hetzner console | ✅ |
| `deploy/docker/README.md` | "Scaling notes" | "Bigger box first… nothing changes except widening `PORT_START..PORT_END`" | same advice, now noting it has actually been done, with the real range and links to this runbook and `OPERATIONS.md` | ✅ |

No source file hardcodes `135.181.100.88`; the IP lives only in DNS, in ssh config and in this
runbook. Confirm again before the decommission:

```bash
grep -rn '135\.181\.100\.88' . || echo "no source reference to the old IP"
```

## Appendix B — every `.env` key

`.env` is never committed. This is the inventory to check the copied file against, from
`.env.example`. **Only the three marked ✏ change during this migration.**

| Key | Carries over | Note |
|---|---|---|
| `DOMAIN` | as-is | `peliarch.ca`. Caddy's site block is keyed on it. |
| `PUBLIC_HOST` | as-is | what players see in connect addresses |
| `ACME_EMAIL` | as-is | Let's Encrypt contact; a reason `.env` stays out of git |
| `DONATION_URL` | as-is | |
| `CONTACT_DISCORD` | as-is | rendered as copyable text, not a link |
| `CONTACT_GITHUB` | as-is | |
| `DOWNLOADS_REPO` | as-is | ER release source |
| `DOWNLOADS_CHANNELS_URL` | as-is | |
| `DOWNLOADS_CHANNELS_RAW_URL` | as-is | selects stable rather than newest |
| `NEXUS_URL` | as-is | |
| `GAME_GITHUB_URL` | as-is | |
| `DOWNLOADS_TTL_SECONDS` | as-is | 900 |
| `DOWNLOADS_TIMEOUT_SECONDS` | as-is | 4 |
| `DOWNLOADS_GITHUB_TOKEN` | as-is | normally **absent**; keep it commented out, never blank |
| `PORT_START` | as-is | must equal the `ufw` range and the published range |
| `PORT_END` | as-is | inclusive; caps rooms that *exist*, not concurrent rooms |
| `COMPOSE_PROJECT_NAME` | as-is | ⚠ **also the volume-name prefix** — changing it orphans every migrated volume |
| `UPLOAD_MAX_BYTES` | as-is | 64 MB |
| `LOG_TAIL_LINES` | as-is | |
| `ROOM_NEVER_CONNECTED_RETENTION` | as-is | 86400 |
| `ROOM_USED_RETENTION` | as-is | 2592000 |
| `ROOM_LOG_ARCHIVE_DIR` | as-is | inside `peliarch_data`, so F2 carries the archive |
| `ER_HOST_STATIC_DIR` | as-is | `/srv/er` — the directory must exist before `up` |
| `ER_REPO` | as-is | |
| `ER_REF` | **as-is — do not touch** | changing it makes this an ER upgrade too |
| `BB_HOST_STATIC_DIR` | ✏ confirm `/srv/bb` | directory must exist before `up`, even empty |
| `BB_REPO` | as-is | |
| `BB_REF` | ✏ **blank → `v0.1.0.2`** | the go-live switch; build arg, so it needs `build` |
| `BB_DOWNLOADS_REPO` | as-is | |
| `BB_DOWNLOADS_CHANNELS_URL` | as-is | |
| `BB_DOWNLOADS_CHANNELS_RAW_URL` | as-is | the ledger `smoke.sh` checks against |
| `BB_GAME_GITHUB_URL` | as-is | |
| `GENERATE_ENABLED` | as-is | |
| `GENERATE_TIMEOUT` | as-is | 180 wall seconds |
| `GENERATE_MAX_AS_MB` | as-is | 2048 |
| `GENERATE_PLANDO` | as-is | 🛑 must be a bare `GENERATE_PLANDO=`; an inline comment becomes the value |
| `GENERATE_RATE_EVENTS` | ✏ consider | one bucket shared by **both** games — see risk R3 |
| `GENERATE_RATE_WINDOW` | as-is | |

## Appendix C — ordering risks

- **R1 — cert before DNS.** Caddy cannot issue for `peliarch.ca` from an IP the A record does not
  name. Carrying `caddy_data` (F2/N4) sidesteps it entirely; the alternatives are DNS-01 or a short
  `-k` window. Do not discover this at S1.
- **R2 — the ledger leads the pin.** `BB_REF` pins the apworld in the image; `CHANNELS.tsv` at
  `main` decides which pages `deploy_site.sh` installs. Promote the ledger to `v0.1.0.2` **first**
  (P4), then build. The reverse order ships a wizard from one release against a world from another,
  and both halves work — which is what makes it dangerous.
- **R3 — one rate-limit bucket, two games.** The Caddy `/generate` limiter is keyed by client IP,
  not by game, and the port range caps rooms that *exist* at 200. Bloodborne roughly doubles the
  creation rate against an unchanged budget. Nothing to change on cutover day, but if
  `_pick_free_port` starts refusing, that is why, and deleting finished rooms is the first move.
- **R4 — NEWBOX is a placeholder.** ✅ **Resolved 2026-09-08.** `46.62.130.40` was confirmed in the
  Hetzner console and is the box the site now runs on: `ubuntu-16gb-hel1-1`, Helsinki,
  **8 vCPU / 15 GB RAM / 150 GB disk**, Ubuntu 26.04. RAM is what caps concurrent rooms
  (~160–200 MB each), so 15 GB is what makes the full `38400–38599` range worth opening — which it
  now is. The original warning stands for the *next* migration: confirm the IP and the size before
  P1, because a migration to the wrong IP is discovered at D1, the worst possible moment.
- **R5 — the stale port range in `OPERATIONS.md`.** ✅ **Resolved 2026-09-08** — `OPERATIONS.md` now
  says `38400–38599`, and `ufw` on NEWBOX opens `38400:38599/tcp` (OLDBOX only ever opened
  `38400:38463`). The hazard as originally written: `38400–38463` there versus `38400-38599` in
  compose. Open the wrong range on NEWBOX and rooms above 38463 look perfectly healthy from the
  inside — listening, `alive: true` — and are unreachable from the internet.
- **R6 — rooms mid-flight.** Any path here disconnects live sessions; the rooms survive, the
  sessions do not. Check P3 and announce a window rather than finding out from a player.
- **R7 — the project prefix on volume names.** Copying `peliarch_data` instead of
  `peliarch_peliarch_data` gives NEWBOX three empty volumes and a site that boots perfectly with no
  rooms and no cert. `migrate-volumes.sh` refuses to run on a name `docker volume inspect` does not
  know, which turns a silent success into a loud failure.
