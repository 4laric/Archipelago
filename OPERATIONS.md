# Peliarch — Operations & Launch Notes

Written down on launch day. This is the "how it's deployed and how to run it" doc.

## Live deployment

- **Site:** https://peliarch.ca and https://www.peliarch.ca (HTTPS via Caddy; the current
  certificate is **ZeroSSL**-issued and valid to **2026-11-27** — see the `ACME_EMAIL` follow-up
  below for why it is not Let's Encrypt)
- **Box:** Hetzner, Helsinki, hostname `ubuntu-16gb-hel1-1`, **46.62.130.40** —
  8 vCPU / 15 GB RAM / 150 GB disk, Ubuntu 26.04. (Hetzner's 16 GB shared-vCPU tier; confirm the
  exact plan row and its price in the console before quoting a number.)
- **User and paths:** the stack runs as **`root`**, not `archi`. Repo cloned at
  **`/root/Archipelago`**; compose lives in `/root/Archipelago/deploy/docker/`.
- **Docker:** `docker.io` **29.1.3** and `docker-compose-v2` **2.40.3**, both from the Ubuntu
  packages (not Docker's own apt repo).
- **Stack:** Docker Compose at `/root/Archipelago/deploy/docker/`
  - `web` container = Flask GUI + per-room MultiServer processes (+ bundled `peliarch` Go binary for Large tier)
  - `caddy` container = TLS termination / reverse proxy for the website
- **Room ports:** **`38400–38599`**, **port-per-room**, plain `ws://` (no TLS on game ports yet).
  This is the full range configured in `deploy/docker/.env` and published by Compose, and `ufw`
  now opens all of it. (The old box only opened `38400:38463`.)
- **Firewall:** `ufw` allows `OpenSSH`, `80,443/tcp`, `38400:38599/tcp`.
- **Host static trees:** `/srv/er` (Elden Ring pages) and `/srv/bb` (Bloodborne pages), bind-mounted
  into `web`. Promotion scripts fetched to the box: **`/root/deploy_site.sh`** (from bb-archipelago,
  populates `/srv/bb`) and **`/root/deploy_wizard.sh`** (from er-archipelago, populates `/srv/er`).
- **Pinned refs in `.env`:** `BB_REF=v0.1.0.2`, `BB_HOST_STATIC_DIR=/srv/bb`, `ER_REF=v0.6.0.5`.
- **Bloodborne:** live at `/bb/`. The `BB_LIVE=1` Actions variable is set and the
  bb-channel-parity `live` job is green.
- **State proven end-to-end:** upload `.archipelago` → room hosts → client connects at `ws://peliarch.ca:38400`

## Migration 2026-09-08

The deployment moved from the old CX23 to the box above on **2026-09-08**, in the same maintenance
window that brought Bloodborne live. The runbook that was followed is
[`deploy/docker/MIGRATION.md`](deploy/docker/MIGRATION.md); it stays in the repo as the record of
what was done and as the procedure for the next move.

What moved and how it went:

- All three Docker volumes were streamed old → new: `peliarch_peliarch_data` (6.1 MB, **17 rooms**),
  `peliarch_caddy_data` (which is what carried the existing peliarch.ca certificate across, so no
  cert had to be ordered before DNS moved) and `peliarch_caddy_config`.
- The old box's `web` container was **stopped** (frozen) before the final data re-sync, so nothing
  advanced on the old side after the copy.
- DNS `A` records for `peliarch.ca` and `www` were moved at **Porkbun**.
- Verified from outside the box: every page returns 200 and the certificate is served. `/bb/` is live.
- The ER pages had to be **redeployed after the move**, because er-archipelago promoted `stable` to
  `v0.6.0.5` the same day; `/root/deploy_wizard.sh` was re-run against the new stable.
- The old box's checkout carried exactly one local edit — the Dockerfile `CMD` `--threads 64` — and
  that change is already on `main`, so nothing was lost by abandoning it.

**Old box: `135.181.100.88` (`ubuntu-4gb-hel1-1`, CX23).** Its `web` container is stopped and its
`caddy` is still running. **Decommission it on or after 2026-09-15** (7 clean days), following the
[Decommission](deploy/docker/MIGRATION.md#decommission) step — final volume backups off the box
first. Deleting the server releases that IPv4.

## Fixes applied during the deploy (all now in the repo too)

1. **`requirements-host.txt`** — slim hosting deps instead of the full `requirements.txt`, which pulls the desktop-client stack (Kivy/KivyMD via git) that a server doesn't need and that broke the build. Includes `bsdiff4`, `requests`, `setuptools` so the ROM-based world modules import cleanly at server start.
2. **`ws://` scheme** — the GUI now headlines `ws://host:PORT` (game ports have no TLS yet); `wss://` is shown as the "after TLS setup" option. (Was advertising `wss://`, which caused `400 Bad Request` on connect.)
3. **`Room.to_dict` crash** — was `asdict(self)` which deep-copies every field, including the live `subprocess.Popen` (holds a thread lock → "cannot pickle '_thread.lock'"). Now builds the dict from scalar fields, skipping `_proc`. This was the room-CRASHED-on-start bug.

## ⚠️ Reconcile the box with the repo

During the deploy, a few fixes were applied **directly on the box** (sed/heredoc). The **same fixes are now in your canonical repo**. Get them in sync so future rebuilds are clean and nothing's only-on-the-box:

```bash
# 1. on your laptop: commit + push the repo
git add -A && git commit -m "deploy fixes: requirements-host, ws scheme, to_dict, large-tier" && git push

# 2. on the box: pull the repo version (drops the manual box edits in favor of the committed ones)
cd /root/Archipelago && git stash && git pull && cd deploy/docker && docker compose up -d --build
```

## Day-2 operations (on the box, as `root`, in `/root/Archipelago/deploy/docker/`)

```bash
docker compose ps                  # status
docker compose logs -f web         # GUI + orchestrator logs
docker compose logs -f caddy       # TLS / cert logs
docker compose up -d --build       # apply code changes / rebuild
docker compose restart             # restart without rebuild
docker compose down                # stop (keeps data)
```

**Data** (uploads, room saves, `rooms.json`) lives in the `peliarch_data` Docker volume. Back it up:

```bash
docker run --rm -v peliarch_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/peliarch-$(date +%F).tgz -C /data .
```

## Known follow-ups (none blocking — it works today)

- **wss:// on room ports** — needed for browser clients / archipelago.gg interop. Desktop AP clients work on `ws://` now. See `DEPLOY.md §9`.
- **`ACME_EMAIL` is still the placeholder** — `deploy/docker/.env` carries `ACME_EMAIL=you@example.com`.
  Let's Encrypt rejects that address, so Caddy fell back to **ZeroSSL**, which issued fine and is what
  is serving today (valid to 2026-11-27). It works, but set it to a real address so expiry warnings
  reach a human and so the LE path is available again: edit `.env` and
  `docker compose up -d` (Caddy re-reads it; the existing cert is untouched).
- **`birdfuck.ca` in the `Caddyfile`** — `deploy/docker/Caddyfile` still has a site block for
  `birdfuck.ca` / `www.birdfuck.ca`, whose DNS points at `207.207.210.36`, not this box. Caddy
  therefore cannot complete an ACME challenge for it and logs a certificate failure on **every**
  retry. Nothing else is affected. Fix it either by pointing that DNS at `46.62.130.40` or by
  deleting the block; leaving it is a permanent source of scary-looking log noise.
- **Decommission the old box on/after 2026-09-15** — `135.181.100.88`, still running `caddy` with a
  stopped `web`. See the Migration note above.
- **Donation URL** — set `DONATION_URL` in `deploy/docker/.env` to your real tip jar (currently the placeholder).
- **Niche game deps** — add `pyevermizer` (Secret of Evermore) and `zilliandomizer` (Zillion) to `requirements-host.txt` only if you want to host those two games. Everything else loads.
- **Nightly backup cron** — `DEPLOY.md §10`.
- **Large tier** — the `peliarch` Go backend is built into the image and selectable in the upload form; exercise it with a Large-tier upload when you want 1,000-slot rooms.

## Harmless log noise to ignore

- `connection rejected (400 Bad Request)` on a room port = internet port-scanners / browsers poking the open port. Real AP clients handshake fine.
- `_speedups not available … pure python LocationStore` = optional Cython speedup absent; functionally fine.
- `Could not load world …` for the 3 niche games above = expected until their deps are added; doesn't affect other games.
