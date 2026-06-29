# AP room-hosting platform — architecture spec

**Product:** a website that hosts Archipelago rooms the way archipelago.gg does —
**drop in a `.archipelago`/`.zip`, it hosts it** and hands players a connect address —
*plus* a tier that can host the big rooms nobody else can (the mega-room / event case
the rest of this repo's work unlocks).

This is fundamentally an **orchestration + ops** product, not a server rewrite. The
per-room server (stock Python MultiServer) is fine for the ~99% of rooms that are small;
the platform is the upload → host → supervise → persist → route loop around it, with a
scaling tier bolted on for the rare giant room.

---

## 0. Build vs reuse (decide this first)

archipelago.gg's hosting stack is **open source**: `WebHostLib` (Flask app: upload,
generate, room pages, auto-hosting, timeouts) + `customserver.WebHostContext` (MultiServer
subclass with DB-backed save persistence and room-timeout logic). So three paths:

| Option | Get | Cost |
|---|---|---|
| **A. Thin custom orchestrator** | Full control; your scaling tiers are native; small surface you understand end-to-end (you've already prototyped it in `federation.py`). | Rebuild upload/DB/room-page/auth from scratch. |
| **B. Fork WebHostLib** | Parity with archipelago.gg fast; battle-tested room lifecycle + DB persistence. | Inherit their Flask/Postgres stack and assumptions; their per-room hosting backend caps where your differentiator lives; you fork a moving target. |
| **C. Hybrid (recommended)** | Reuse WebHost's **web/upload/DB/room-page** layer; **replace the room-hosting backend** (`WebHostContext` launch path) with your own orchestrator so you own scheduling + can route big rooms to archipela-go/federation. | Some integration glue at the seam between their web layer and your backend. |

**Recommendation: C.** The web/upload/persistence surface is undifferentiated plumbing —
reuse it. The room-hosting backend is exactly where your value (mega-rooms, islands,
multi-node) lives — own it. If you'd rather not adopt AP's Flask/Postgres at all, fall
back to **A** and accept rebuilding the boring surface. Don't pick **B** — forking the
whole thing buys parity but locks you into the ceiling you're trying to beat.

Everything below is written to hold under A or C (the orchestrator, lifecycle, ingress,
and tiers are the same; only the web/DB layer's origin differs).

---

## 1. Room model & lifecycle

A **room** is one uploaded multidata + its running state. Persistent record (DB):

`id, owner, multidata_ref, save_ref, status, node, port, tier, password,
created_at, last_active_at, limits{max_players, cpu, mem}`

Lifecycle states and transitions:

```
UPLOADED ──start/first-connect──► STARTING ──ready──► RUNNING
   ▲                                                   │
   │                                  idle > T minutes │
   │                                                   ▼
 (rehost on connect) ◄──────────── HIBERNATED ◄──save─┘
                                                       │ crash
                                          RUNNING ◄─restart─ CRASHED
```

- **STARTING:** allocate a port, launch the server on the multidata (+ existing save if
  any), wait for the port to listen, mark RUNNING.
- **RUNNING:** supervised process; health-checked; connection count tracked.
- **HIBERNATED:** no connections for `T` minutes → trigger a final save, kill the process,
  free the port. Cheap to keep thousands of idle rooms as just rows + a save blob (this is
  archipelago.gg's "auto-hosting/timeout" behavior).
- **Rehost:** a connection attempt to a hibernated room re-runs STARTING from the saved
  state. (Requires the ingress to know a room exists even when no process is up — see §3.)
- **CRASHED → restart** from the latest save, with a backoff + a crash counter that
  eventually parks the room and alerts the owner.

You have most of this already: `federation.py` launches/supervises N MultiServers with
`free_port`, `resolve_server_pid`, `wait_for_port`, `stop_server`. The orchestrator is
that, generalized to dynamic rooms with a DB and the lifecycle above.

---

## 2. The orchestrator (the actual product)

Responsibilities:

- **Provisioning:** allocate a free port (per node), launch the right server binary for
  the room's tier, attach `--password` for gated rooms, point it at the multidata + save.
- **Supervision:** watch the process; restart on crash from save; enforce per-room limits
  (CPU/mem/connection cap) via cgroups or a container runtime.
- **Lifecycle:** idle detection → hibernate (final save + stop); rehost on demand.
- **Persistence:** write/read the `.apsave` to durable storage (local disk for single-node
  MVP; object store/DB for multi-node — mirrors `WebHostContext`'s DB save override).
- **Sampling/health:** reuse `sample_server.py`'s resolve-the-listening-PID logic for
  per-room CPU/RSS and a liveness probe (the harness's loop-health `Get` ping doubles as a
  synthetic health check).
- **Capacity accounting:** track per-node running rooms + resource use for scheduling (§6).

---

## 3. Networking & ingress (the one hard protocol constraint)

**In AP a room's identity *is* its `host:port`.** The client connects to a bare ws/wss
endpoint and the server immediately sends RoomInfo — there is no room-id in the handshake.
So you **cannot** path-multiplex many rooms behind a single port with stock clients. A
hosting platform is therefore a **port allocator + reverse proxy**, not a path router:

- **Port-per-room.** Each RUNNING room binds a port on its node. The room page shows the
  player-facing `wss://host:PORT`.
- **TLS/`wss` at the edge.** Browser clients (and archipelago.gg) use `wss`; desktop
  clients do `ws` or `wss`. Terminate TLS at a proxy (Caddy/Traefik with auto-cert) that
  maps each room port to its backend. A wildcard cert + per-port listeners is the simplest
  compatible setup.
- **Hibernated rooms need a wake path.** Because identity is the port, either (a) keep the
  proxy listener up for a hibernated room and have it call the orchestrator to rehost on
  the first TCP connect (lazy wake), or (b) keep rooms warm and only hibernate the
  *process*, not the port reservation. Lazy-wake is the archipelago.gg-style behavior and
  the better default.
- **The web page is path-based** (`yoursite/room/<id>` for status/logs/connect-info); only
  the *game connection* is port-based. Don't conflate the two.

(If you ever control the client or add a subprotocol you could multiplex — out of scope
for stock-client compatibility, but note it for archipela-go, which *could* expose a
room-id handshake later.)

---

## 4. Scaling tiers (where the prior work is the moat)

The platform offers tiers, selected per room by expected size:

| Tier | Backend | Capacity (from FINDINGS) | Use |
|---|---|---|---|
| **Standard** | stock MultiServer | comfortable to ~100–150 active slots | the 99% of rooms |
| **Large** | archipela-go (single big room) | 1,000+ in benchmarks, server ~idle | one big room, full cross-routing |
| **Federated** | islands + bridge (+ director) | 1,000+ across islands, stock servers | events, friend/sub-island social play |

Standard ships day one on stock servers. **Large** and **Federated** are the
differentiator: archipelago.gg itself can't host these, so "the host that runs the rooms
the official one can't" is real positioning. The **harness is the SLA tool** — load-test
each tier, publish capacity numbers, gate tier eligibility by measured room size.

Note: archipela-go must grow the real-multidata loader before it can back **Large** for
real rooms (it's synthetic today — see `PROTOCOL_SURFACE.md`). Federated works on stock
servers *now*.

---

## 5. Multi-tenancy, security, abuse

You're running **arbitrary user-uploaded multidata** as a server process, publicly
reachable. Treat each room as untrusted:

- **Untrusted multidata.** AP already uses *restricted unpickling* (`restricted_loads`) to
  load multidata/saves — keep that; never plain-`pickle.load` an upload. Validate the zip
  (size cap, structure) before hosting.
- **Process isolation.** One container/cgroup per room with CPU/mem/file-descriptor limits
  and a connection cap, so one room can't starve the node or the load-generator-style
  abuse can't take neighbours down.
- **Resource ceilings per tier.** Standard rooms get small limits; Large/Federated get more
  but are gated (and probably paid). A connection-rate limiter at the proxy blunts socket
  floods (the same thing our harness does on purpose).
- **Auth & ownership.** Room owners authenticate; gated rooms use the AP `--password`
  mechanism (the same lever the federation director uses for sub-only islands).
- **Lifecycle hygiene.** Idle hibernation + a hard max lifetime keeps abandoned rooms from
  accumulating; crash-parking prevents restart loops.

---

## 6. Multi-node scheduling (roadmap, not MVP)

Single-node MVP: everything local, `.apsave` on disk. To scale horizontally:

- **State in durable storage:** multidata in object storage, saves in object storage/DB
  (exactly `WebHostContext`'s DB-save model), room records in the DB.
- **Scheduler:** place a STARTING room on a node with spare capacity (bin-pack by the
  per-room limits — same greedy first-fit stance as the federation director and AP fill:
  feasible, not optimal). Rooms are stateless-on-disk, so any node can rehost any room from
  its save.
- **Stickiness:** a running room stays on its node (its port is there); the ingress/router
  maps `room → node:port`. On rehost, re-place and update the mapping.

This is the point where you'd reach for a container orchestrator (Nomad/k8s) rather than
hand-rolled supervision.

---

## 7. Web/API surface (MVP)

- `POST /rooms` — upload `.archipelago`, create room (returns id + connect info).
- `GET /rooms` / `GET /room/<id>` — list/status, player count, tier, connect `wss://…`,
  log tail.
- `POST /room/<id>/start` · `/stop` — manual lifecycle (auto start-on-connect is the
  default; manual is for owners).
- `GET /room/<id>/health` — orchestrator probe (CPU/RSS, liveness via a datastorage `Get`).
- Owner auth; per-tier limits enforced at create time.

A thin FastAPI/Flask app over the orchestrator. Under option **C**, much of this is
WebHostLib's existing surface with the hosting backend swapped.

---

## 8. MVP cut vs roadmap

**MVP (single node, standard tier):**
1. Upload `.archipelago` → store + room record.
2. Orchestrator: port-per-room stock MultiServer, start-on-connect, idle-hibernate +
   lazy-wake, crash-restart from `.apsave`, disk persistence.
3. `wss` via Caddy/Traefik; room page with connect address + status + log tail.
4. Per-room resource limits + restricted unpickling + size caps.

**Roadmap:** Large tier (archipela-go + real-multidata loader) → Federated tier (islands +
bridge + director, incl. friend/sub-island placement) → multi-node scheduling → billing/tiers.

---

## 9. How existing assets map

| Asset | Role in the platform |
|---|---|
| `federation.py` (launch/supervise N servers) | seed of the **orchestrator** |
| `sample_server.py` (resolve-listening-PID, CPU/RSS) | per-room **health/metrics** |
| `ap_loadtest.py` + `sweep_compare.py` | **capacity SLA** / tier qualification |
| `archipela-go/` | **Large** tier backend (after real-multidata loader) |
| `FEDERATION.md` + `federation*.py` + director spec | **Federated** tier + friend/sub islands |
| `PROTOCOL_SURFACE.md` | what a tier backend must implement to be drop-in |

---

## 10. Open decisions

- **Build-vs-reuse (§0):** confirm A (thin) vs C (hybrid). Drives whether the web/DB layer
  is forked from WebHostLib or built fresh.
- **Generation in scope?** "Drop in your zip" implies **hosting only** (generation done
  elsewhere). Keeping generation out of v1 is a big simplification vs full archipelago.gg.
- **Self-host vs SaaS:** a self-hostable single-box image is a fast first deliverable and a
  good wedge even before multi-node SaaS.
- **Where archipela-go earns the Large tier:** the real-multidata loader is the gating
  piece; until then Large is "federated under the hood."

---

## 11. Infrastructure / where to host (beginner orientation)

*(Pricing as of June 2026 — it moves; re-check before committing.)*

### Three kinds of host (a spectrum)

1. **VPS — "rent a Linux box with a public IP, you run everything"**
   (Hetzner, DigitalOcean, Linode/Akamai, Vultr). Cheapest, most control, most ops work
   on you. **Best fit here** because port-per-room (§3) needs a machine where *you* own
   the whole port range.
2. **PaaS — "push a container, they handle networking/TLS/scaling-ish"**
   (Fly.io, Render, Railway). Less ops, more cost, networking model less flexible for many
   dynamic ports.
3. **Hyperscaler — "every managed service, scales forever, priciest + most complex"**
   (AWS, GCP, Azure). For when you're big; their load balancers fight port-per-room and
   their egress (bandwidth) billing is the expensive trap.

### Cost comparison (≈4 vCPU / 8 GB MVP box)

| Provider | Compute / mo | Bandwidth | Port-per-room fit |
|---|---|---|---|
| **Hetzner CX32** (4 vCPU/8 GB, EU) | ~€6.80 (~$7.50) | **20 TB included** | excellent — full port control |
| **Fly.io** (~shared-cpu-4x) | ~$30–40 | metered $0.02/GB (NA/EU) + $2/mo IPv4 + $0.15/GB volumes | awkward — fixed ports per app |
| **AWS** (t3.large + proxy) | ~$60 | $0.09/GB after 100 GB free (1 TB ≈ $90/mo!) | poor — ALB/NLB want fixed ports |

**Why the gap matters for *this* workload:** room traffic is small JSON over WebSockets,
but it scales with active player-hours. On Hetzner that lands inside the included 20 TB
(effectively free). On AWS the same traffic is billed at $0.09/GB and the egress line item
can dwarf the entire Hetzner bill. Compute is also ~5–10× cheaper on a VPS than equivalent
managed instances.

### Recommendation

- **MVP: a single Hetzner VPS** (CX32, scale to CX42 for headroom / big rooms). It maps
  directly onto §3: you control the full port range, bandwidth is generous, cost is ~$8–18/mo.
  Front it with **Caddy or Traefik** (automatic Let's Encrypt → `wss`), put saves + multidata
  in **S3-compatible object storage** (Hetzner Object Storage or Backblaze B2), and run
  **Postgres** on the box to start.
- **Graduate to managed orchestration** (AWS/GCP ECS/EKS) only when multi-node scheduling
  (§6) is the real bottleneck — not before. That's when you're paying for *placement at
  scale*, which is the one thing hyperscalers do that a single VPS can't.
- **Fly.io** is the middle option ("less ops, pay more"), but its fixed-ports-per-app model
  is a slightly awkward fit for dynamic port-per-room — note it, don't default to it.

### Caveats for a newcomer

- **Audience latency:** Hetzner's cheapest, highest-bandwidth servers are in the EU. They
  have US locations, but those cost more and include far less transfer. If your players/
  streamers are US-based, weigh latency vs cost (or run one box per region later).
- **A VPS means you own ops:** OS updates, firewall, TLS renewal (Caddy automates most),
  backups of the DB + save storage. That's more to learn — and the right place to learn it
  before taking on managed complexity.
- **Security baseline (§5 still applies):** firewall everything except the room port range +
  the web port, run each room under a resource limit, keep restricted unpickling, cap upload
  sizes. One box hosting many untrusted rooms still needs per-room isolation.
