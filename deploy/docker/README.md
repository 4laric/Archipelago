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
