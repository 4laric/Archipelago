# Peliarch — Operations & Launch Notes

Written down on launch day. This is the "how it's deployed and how to run it" doc.

## Live deployment

- **Site:** https://peliarch.ca (HTTPS, auto-cert via Caddy — cert issued ✅)
- **Box:** Hetzner **CX23**, Helsinki, Ubuntu 26.04, ~$7/mo, IPv4 + 20 TB transfer
- **Stack:** Docker Compose at `~/Archipelago/deploy/docker/`
  - `web` container = Flask GUI + per-room MultiServer processes (+ bundled `peliarch` Go binary for Large tier)
  - `caddy` container = TLS termination / reverse proxy for the website
- **Room ports:** `38400–38463`, **port-per-room**, plain `ws://` (no TLS on game ports yet)
- **State proven end-to-end:** upload `.archipelago` → room hosts → client connects at `ws://peliarch.ca:38400`

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
cd ~/Archipelago && git stash && git pull && cd deploy/docker && docker compose up -d --build
```

## Day-2 operations (on the box, in `~/Archipelago/deploy/docker/`)

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
- **`www` DNS** — add an A record `www` → box IPv4 (or CNAME `www` → `peliarch.ca`) so `www.peliarch.ca` resolves.
- **Donation URL** — set `DONATION_URL` in `deploy/docker/.env` to your real tip jar (currently the placeholder).
- **Niche game deps** — add `pyevermizer` (Secret of Evermore) and `zilliandomizer` (Zillion) to `requirements-host.txt` only if you want to host those two games. Everything else loads.
- **Nightly backup cron** — `DEPLOY.md §10`.
- **Large tier** — the `peliarch` Go backend is built into the image and selectable in the upload form; exercise it with a Large-tier upload when you want 1,000-slot rooms.

## Harmless log noise to ignore

- `connection rejected (400 Bad Request)` on a room port = internet port-scanners / browsers poking the open port. Real AP clients handshake fine.
- `_speedups not available … pure python LocationStore` = optional Cython speedup absent; functionally fine.
- `Could not load world …` for the 3 niche games above = expected until their deps are added; doesn't affect other games.
