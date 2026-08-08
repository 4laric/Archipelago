# SPEC — Customer-facing web GUI for Peliarch

**Status:** spec-with-options (intent undecided — paths laid out for later decision)
**Owner:** Alaric
**Depends on:** `HOSTING.md` (orchestrator + ingress), `federation.py` (supervision seed), `peliarch/` (Large-tier backend after the multidata loader), `SPEC_pricing.md`.
**Goal:** the website players and room-owners actually touch — upload a room, get a connect address, watch it run. `HOSTING.md` already specs the backend orchestrator; **this spec is the front end and the API contract it talks to**, written so the same UI serves whichever of the three intents you pick.

---

## 1. The one hard constraint that shapes the whole UI

From `HOSTING.md §3`: **in Archipelago a room's identity *is* its `host:port`.** The client connects to a bare `wss://host:PORT`, the server immediately sends `RoomInfo` — there is no room-id in the handshake. Consequences for the GUI:

- The **web page is path-based** (`/room/<id>` for status/logs/connect-info); the **game connection is port-based** (`wss://host:PORT`). The UI must show both clearly and never conflate them.
- The headline element of a room page is a **copy-able connect address** (`wss://host:PORT`, plus the bare `host` and `port` separately for desktop clients that take them split).
- Rooms hibernate when idle and **lazy-wake on first connect** — the UI shows "Sleeping — connecting will wake it (~few sec)" rather than implying it's down.

---

## 2. Three product intents → one UI, three skins

The same screens serve all three; what differs is auth, payment, and which tiers are exposed. Build the union; gate features by a config flag.

| | **A. Commercial SaaS** | **B. Free community** | **C. Self-host / OSS admin** |
|---|---|---|---|
| Who logs in | paying customers | anyone (light auth) | the box owner only |
| Room creation | tier picker + checkout | tier picker (Large gated) | all tiers, no payment |
| Pages needed | full set + billing | full set, no billing | full set, single-tenant |
| Differentiator surfaced | "host the rooms .gg can't" + SLA | same capability, donation ask | "run your own .gg in one box" |
| Maps to `HOSTING.md` | §0 option C hybrid, multi-node roadmap | option C, single node | option A/C single-box image |

**Recommendation regardless of intent:** ship the **self-host single-box image first** (`HOSTING.md §10` calls it "a fast first deliverable and a good wedge"). It exercises the whole UI + orchestrator on one node with no billing, and it's identical to the SaaS UI minus the payment/multi-tenant layer. You can flip on auth tiers and billing later without rebuilding screens.

---

## 3. Screens (MVP)

### 3.1 Landing / dashboard — `/`
- List of the user's rooms (or all rooms on a single-box self-host): name, game(s), tier badge, status pill (Running / Sleeping / Starting / Crashed), live player count, connect address (copy button), last-active.
- Primary CTA: **Host a room** (upload).
- Empty state explains the model: "Upload a generated `.archipelago`, we host it, you get a connect link."

### 3.2 Upload / create room — `POST /rooms`
- Drag-drop `.archipelago` / `.zip` (the **generated** multidata — generation is **out of scope**, per `HOSTING.md §10`; v1 hosts only).
- Client-side: size cap, extension check; server re-validates (size, zip structure, restricted unpickling — `HOSTING.md §5`).
- Fields: room name, optional password (maps to AP `--password`), **tier** (Standard / Large / Federated — see §4), optional idle-hibernate timeout.
- On success → redirect to the room page with the connect address front-and-center.

### 3.3 Room page — `/room/<id>` (the screen that matters most)
- **Connect block:** big copy-able `wss://host:PORT`; separate `host` / `port` for desktop clients; a "Connecting? Here's how" expander (point at the AP client + this address).
- **Status:** state pill, uptime, player count, tier, password-protected indicator. Sleeping rooms show the lazy-wake note.
- **Players:** slot/name/game/status (goal complete?), online/offline — read from the running server's state via the orchestrator health probe.
- **Live log tail:** last N lines from the room process (chat/item-send/join-part). Poll or stream (SSE).
- **Owner controls (auth-gated):** Start / Stop, regenerate password, delete room, edit idle timeout. Auto-start-on-connect is default; manual is for owners (`HOSTING.md §7`).
- **Tier/capacity (Large/Federated):** show the measured capacity number from the harness SLA (`SPEC_go_loadtest.md`) so a big-room host knows it'll hold.

### 3.4 Auth (intent-dependent)
- A/B: account signup/login; room ownership. B can use lightweight/OAuth. C: single admin credential, no signup.
- Owner-only actions enforced server-side, not just hidden in UI.

### 3.5 Billing (intent A only) — deferred behind a flag
- Tier checkout at room create; plan management. Driven by `SPEC_pricing.md` tiers. Not in the first self-host cut.

---

## 4. Tier selector (the differentiator, surfaced in UI)

From `HOSTING.md §4`:

| Tier | Backend | Capacity | When the UI offers it |
|---|---|---|---|
| **Standard** | stock MultiServer | ~100–150 active slots | default; ships day one |
| **Large** | peliarch single room | 1,000+ (benchmarks) | once the real-multidata loader lands (`SPEC_remaining_go_functions.md` Batch A); gated/paid in A |
| **Federated** | islands + bridge + director | 1,000+ across islands | events; works on stock servers now |

The selector explains the trade in plain language ("Standard fits almost every room. Large is for one giant cross-routed room. Federated is for events split across islands."). Gate Large/Federated eligibility by **measured** room size where possible (the harness is the SLA tool). Until peliarch's loader lands, Large is "federated under the hood" — the UI can still offer the capability, backed by federation.

---

## 5. API contract (front end ↔ orchestrator)

Mirrors `HOSTING.md §7`; the GUI is a thin client over it:

```
POST   /rooms                 # multipart upload .archipelago -> {id, connect:{host,port,wss}, tier}
GET    /rooms                 # list (scoped to owner, or all on self-host)
GET    /room/<id>             # status, players, tier, connect info, log tail ref
POST   /room/<id>/start       # manual start
POST   /room/<id>/stop        # manual stop
GET    /room/<id>/health      # orchestrator probe: CPU/RSS, liveness via a datastorage Get
GET    /room/<id>/logs        # SSE or paged tail
DELETE /room/<id>             # remove room + saved state
POST   /room/<id>/password    # set/clear room password
```
Owner auth on mutating routes + per-tier limits enforced at create time. A thin FastAPI/Flask app over the orchestrator (`HOSTING.md §7`); under option C much of this is WebHostLib's existing surface with the hosting backend swapped.

---

## 6. Tech choices

- **Frontend:** server-rendered (Flask/Jinja or FastAPI + htmx) is plenty — these are CRUD + status pages, not an app. Keeps the self-host image small and dependency-light. A SPA is unjustified for MVP.
- **Live data:** SSE for log tail and status; cheap, no websocket infra needed on the web side.
- **Ingress reminder:** the web app and the game ports are different planes. TLS-terminate `wss` at Caddy/Traefik per `HOSTING.md §3`; the web app just *displays* the port, it doesn't proxy game traffic.
- **Connect-address correctness is a test, not a nicety:** an integration test must confirm the address shown actually accepts an AP client and yields RoomInfo. A wrong address is the one bug that makes the product look broken.

---

## 7. MVP cut vs roadmap

**MVP (self-host single box, Standard tier):** dashboard, upload→host, room page with connect address + status + log tail + start/stop, room password, restricted unpickling + size caps. No billing, single admin auth. This is `HOSTING.md §8` MVP with a face on it.

**Roadmap:** multi-tenant auth (A/B) → Large tier surfaced once the loader lands → Federated tier UI → billing/checkout (A) → multi-node room placement display (`HOSTING.md §6`).

## 8. Open decisions (defer, don't block)
- Intent A/B/C — drives auth + billing presence only; screens are shared. Build C first.
- Generation in scope? Default **no** (host-only) per `HOSTING.md §10`; revisit as a later "generate in-browser" feature.
- Brand: "Peliarch" naming and how loudly to position "the host that runs rooms archipelago.gg can't."

## 9. DoD (MVP)
Self-host image where a user uploads a `.archipelago`, lands on a room page with a working copy-able connect address, sees live status + log tail + player count, can start/stop/password/delete, with restricted unpickling and size caps enforced — and the connect-address integration test passes against a real AP client.
