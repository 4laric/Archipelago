# Peliarch — one-command self-host (Docker Compose)

Clone-to-running for an operator who just wants to run their own instance. One container
hosts the web GUI + the per-room MultiServer processes (+ the `archipela-go` Large-tier
binary, bundled and ready); a second runs Caddy for automatic HTTPS on the website.

## Quick start

```bash
git clone <your-repo> Archipelago
cd Archipelago/deploy/docker
cp .env.example .env
nano .env                      # set DOMAIN, PUBLIC_HOST, ACME_EMAIL, DONATION_URL
docker compose up -d --build
```

Point a DNS **A record** for your `DOMAIN` at the box's IP first (Caddy needs it to issue a
cert). Then open `https://DOMAIN` — upload a `.archipelago`, get a connect address, done.

**Local test, no domain:** set `DOMAIN=:80` and `PUBLIC_HOST=<box-ip>` in `.env`, then
`docker compose up -d --build` and hit `http://<box-ip>`.

## How it's wired

- **web** container: the Flask GUI (gunicorn, **single worker** — the orchestrator keeps
  room state + the port allocator in memory and supervises room subprocesses, so it can't
  be replicated). Rooms bind ports in `PORT_START..PORT_END`, which Compose maps straight
  through to the host (port-per-room — a room's identity *is* its `host:port`).
- **caddy** container: terminates TLS for the website only, reverse-proxies to `web:8080`.
- **persistence**: the `peliarch_data` named volume holds uploads, room saves, logs, and
  `rooms.json`. Survives `docker compose down`; removed only by `docker compose down -v`.

### Room retention and server logs

The room reaper gives generated-but-abandoned rooms a shorter lifetime than rooms players actually
used. By default, a room with no observed client connection is deleted after 24 hours; a previously
used room is deleted after 30 days without activity. Running rooms are never retention-deleted.
Configure the windows in `.env` with `ROOM_NEVER_CONNECTED_RETENTION` and
`ROOM_USED_RETENTION` (seconds); `0` disables that cleanup class.

Every room's real server stdout/stderr already goes to `server.log`. Before any automatic or manual
room deletion, that file is copied unchanged to `/data/server-logs/YYYY-MM/<room-id>.server.log`,
outside the disposable room directory. `ROOM_LOG_ARCHIVE_DIR` can move the archive, but its default
is in the persistent `peliarch_data` volume and is included by the backup command below.

## Common ops

```bash
docker compose logs -f web        # tail the GUI/orchestrator
docker compose ps                 # status
docker compose pull && docker compose up -d --build   # update
docker compose down               # stop (keeps data)
```

Backup the state volume:
```bash
docker run --rm -v peliarch_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/peliarch-$(date +%F).tgz -C /data .
```

## Scaling notes (for later)

This is single-node by design — the right shape until one box genuinely isn't enough
(`HOSTING.md §6`). When you outgrow it:

- **Bigger box first.** Resize the VPS (more RAM = more concurrent rooms); nothing in the
  compose changes except widening `PORT_START..PORT_END`.
- **Then multi-node.** Move `peliarch_data` to object storage / a shared DB, run the
  orchestrator as a scheduler placing rooms across worker nodes, and put the room→node:port
  map behind the ingress. That's where you'd graduate from Compose to Nomad/k8s — but not
  before you've measured that you need it.

## wss on room ports

Desktop AP clients connect over `ws://PUBLIC_HOST:PORT` (what the GUI advertises by default)
— fine to launch with. Browser clients / archipelago.gg need `wss://`. To add it: give the
room subprocesses a cert (share Caddy's, or a certbot DNS-challenge wildcard) via the
orchestrator's MultiServer launch (`--cert`/`--cert_key`), and flip the GUI's advertised
scheme. See `DEPLOY.md §9` (repo root) for the two approaches.

## Elden Ring: the wizard, and generating seeds on the box

`docker compose up -d` now also gives you the ER options wizard at `https://YOURDOMAIN/er/` and
seed generation at `POST /generate`. Nothing extra to install, and nothing to copy onto the box.

### Why it is a build pin and not a mount

The apworld and the wizard live in a **different repository**. There were three ways to get them
here and only one of them is reproducible:

| | why not |
|---|---|
| vendor them into this repo | forks them; they drift the moment either side moves |
| `rsync` them onto the box | the manual step this whole directory exists to delete |
| **clone at a pin during the build** | an immutable `ER_REF` always builds the same image |

So `ER_REF` in `.env` is the room-generation reproducibility boundary. It pins the installed
apworld. Static pages are promoted separately from the host directory described below.

`ER_REF` is required and the build rejects moving branch names. Use a `vX.Y.Z` release tag or a full
40-character commit SHA. This is enforced because `ER_REF=main` once rebuilt the public stable page
from development while `/downloads` remained on v0.4.6: both pages worked, and together they lied.
The resolved commit is baked into the image at `/app/.er-rev`, so a running container can always
answer *which* ER build it is:

```bash
docker compose exec web cat /app/.er-rev
```

### Deploying stable and beta pages

er-archipelago's `tools/deploy_wizard.sh` writes stable pages, `latest.json`, and the moving beta
channel to one host directory. Compose mounts that complete directory read-only at `/er-static`.
This keeps room generation pinned while letting a gated static promotion become live as soon as the
script exits, without an image rebuild.

On the host, deploy the pages before or after recreating the web container:

```bash
cd ~/er-archipelago
ER_STATIC_DIR=/srv/er tools/deploy_wizard.sh
cd ~/Archipelago/deploy/docker
docker compose up -d --force-recreate web
curl -fsS https://YOURDOMAIN/er/latest.json >/dev/null
curl -fsS https://YOURDOMAIN/er/beta/wizard.html >/dev/null
```

Set `ER_HOST_STATIC_DIR` if the deploy root is not `/srv/er`. Populate it before starting the
container: the read-only bind mount deliberately replaces the image's fallback pages. It survives
container recreation and contains both stable files and the `beta/` subdirectory.

The world is installed by `tools/gf_test.py --install-only`, which is the same entry point CI and
the dev box use. That matters more than it looks: the apworld needs several files installed
*beside* the package (`region_map.csv`, the `.tsv` tables, `region_groups.py`, the shipping yaml),
and hand-copying the package alone produces a world that imports and then fails oddly at generation
time. One installer, one definition, no drift.

### What to set

```ini
ER_REF=v0.4.6              # immutable tag or full 40-character commit SHA; never main
GENERATE_ENABLED=1
GENERATE_TIMEOUT=180
GENERATE_MAX_AS_MB=2048
# empty = plando off. Keep this on its own line: an inline comment after an EMPTY
# value ends up as the value, and generation then dies with
# KeyError: '# empty = plando off is not a recognized name for a plando module'.
GENERATE_PLANDO=
```

### 🛑 Before you expose /generate publicly

Every other endpoint here takes a multidata and validates it structurally without ever unpickling
it. `/generate` hands attacker-controlled text to a program that will follow it — an AP yaml
carries weights, triggers and plando that steer allocation and runtime. `webgui/generator.py`
bounds that with a wall timeout (killed by **process group**, because Generate spawns children), an
`RLIMIT_AS` the child cannot raise, `RLIMIT_CPU`, plando off, and a size cap before a process is
even spent.

**That makes it survivable, not safe.**

**Rate limiting is now in place** (as of the ratelimit PR): Caddy is built from
`deploy/docker/Caddyfile.Dockerfile` with `github.com/mholt/caddy-ratelimit` compiled in, and
`/generate` is capped per client IP — `GENERATE_RATE_EVENTS` per `GENERATE_RATE_WINDOW`, default
3/min, with IPv6 clients bucketed by `/64`. This lives in Caddy rather than Flask on purpose: there
is one gunicorn worker, so an in-app limiter competes for the very thread it is protecting.

> ⚠️ That replaces the official `caddy:2` image with a locally compiled binary carrying a
> **third-party module** — its own README says it is "not an official repository of the Caddy Web
> Server organization" — on the box that terminates TLS. A deliberate trade against an unlimited
> `/generate`, with both the Caddy version and the module version **pinned** for exactly that
> reason. A floating `@latest` on a component in this position would be the real mistake.

**Still not done:** generation runs as the web container's user, sharing the container with the
orchestrator that supervises live rooms. The process limits are the only thing between a hostile
yaml and those rooms. A separate generation container with its own CPU/memory reservation is the
remaining fix.

### Verifying a deploy without guessing

```bash
docker compose exec web cat /app/.er-rev                     # which ER build is baked in
curl -sf https://YOURDOMAIN/er/wizard.html | head -c 40      # the wizard is served
python .github/scripts/check_er_channels.py --base-url https://YOURDOMAIN
curl -s -H 'Accept: application/json' -X POST \
     --data-urlencode 'yaml@my-seed.yaml' \
     https://YOURDOMAIN/generate | jq .                      # a real seed -> a real room
```

The third one is the only check that proves the whole chain — wizard yaml, generation, room
creation, auto-start — actually works. A green container and a served page prove neither.
