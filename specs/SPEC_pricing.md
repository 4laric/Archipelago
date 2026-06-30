# SPEC — Pricing options for Peliarch hosting

**Status:** DECIDED — donation model (Model B). Buy-me-a-coffee / tip-jar, no paid tiers. Sections 3B and 4 are the active plan; A and C retained for reference.

> **Decision (2026-06-29):** Alaric chose the **donation model** — a "buy me a cup of coffee" tip jar, everything free. No billing system, no paid tiers, no checkout. The web GUI ships a donation link; capacity is whatever the community box(es) can bear. This sidesteps the free-archipelago.gg anchor entirely (we're not competing on price — there is no price) and keeps ops/legal overhead near zero. The Large/Federated *capabilities* still exist as free features, gated only by measured capacity, not payment.

**Owner:** Alaric
**Grounded in:** `HOSTING.md §11` (infra costs, June 2026), `FINDINGS.md` cloud-ceiling (capacity per box), `peliarch` benchmarks.
**Goal:** turn the capacity numbers into a pricing decision. The cost side is solid and known; the **price** side is constrained by one hard fact: the incumbent, archipelago.gg, hosts small rooms **for free**. So pricing strategy isn't "what's it cost to run" — it's "what can we charge for that has no free substitute."

> Prices below are June 2026 and **move** — re-check Hetzner before committing (`HOSTING.md §11`).

---

## 1. The competitive anchor (read this first)

archipelago.gg hosts rooms free. That sets willingness-to-pay for **standard small rooms at ~$0.** You cannot build a business undercutting free. Every viable commercial path therefore prices the things .gg **can't** do:

- **Large tier:** one giant cross-routed room (1,000+ slots) — archipelago.gg falls over near 250; this repo's whole point is that Peliarch doesn't.
- **Federated / event tier:** 1,000+ players across islands for organized events.
- **Guarantees:** measured-capacity SLA, priority/no-hibernate, private/branded hosting.

That's the moat `HOSTING.md §4` describes, restated as the pricing surface.

---

## 2. Cost basis (what it actually costs to run)

From `HOSTING.md §11` and `FINDINGS.md`:

| Resource | Cost | Capacity evidence |
|---|---|---|
| Hetzner **CX22** (2 vCPU/4 GB) | ~€4 / mo | served **4,000** concurrent connections, 0 errors, 26% mean CPU, RAM the cap at 2.79 GB |
| Hetzner **CX32** (4 vCPU/8 GB) | ~€6.80 (~$7.50) / mo, **20 TB** included | the recommended MVP box |
| Bandwidth | effectively free on Hetzner (room traffic = small JSON over WS, fits in 20 TB) | the AWS trap is $0.09/GB egress — avoid |
| Object storage (saves/multidata) | a few €/mo (Hetzner Object Storage / Backblaze B2) | small blobs |

**Derived unit costs:**

- **Idle room ≈ €0.** A hibernated room is a DB row + a save blob (`HOSTING.md §1`). A box holds thousands.
- **Concurrent *small* room (stock MultiServer):** ~230–260 MB RSS each (`FINDINGS.md`, 100-slot = 231 MB). An 8 GB CX32 runs **~25 concurrent** small rooms with headroom → **~€0.25–0.30 / concurrent-room-month** of compute at full packing; less concurrency = even cheaper. Realistically, with hibernation, *registered* rooms vastly outnumber *running* ones, so blended cost per hosted room is well under that.
- **One Large room (peliarch):** under one core; RAM is the driver (2.79 GB held 4,000 conns on the 4 GB box). A single CX32 hosts **one big Large room plus the small-room population.** Shrinking the `send` buffer in `main.go` (currently 8192) pushes the RAM ceiling further.

**Bottom line:** a single ~€7/mo box covers an entire community's small-room hosting *and* a flagship Large room. Marginal cost per room is near zero; the cost that scales is concurrent big rooms and operator time.

---

## 3. Three pricing models

### Model A — Commercial freemium (recommended if monetizing)
Match free on the commodity, charge for the differentiator.

| Plan | Price | What | Margin note |
|---|---|---|---|
| **Standard** | **Free** (or tip jar) | small rooms, hibernation, the .gg experience | loss-leader; near-zero marginal cost |
| **Large room** | **$5–10 / room-instance** (one giant room, lifetime of the room) or **$5–8 / mo** subscription | peliarch single big room, no hibernation, measured capacity | one box (~$7/mo) serves several → strong margin once >1–2 paying rooms |
| **Event / Federated** | **$25–75 / event** (sized by player count + duration) | islands + bridge + director for organized events; setup + priority | events are high-value, low-volume; price on outcome not compute |
| **Private / branded** | **$15–30 / mo** | dedicated capacity, custom subdomain, no shared neighbours | a dedicated box's cost + ops premium |

Rationale: only Large/Event/Private have **no free substitute**, so they carry price. Standard exists to capture the audience and funnel to paid tiers. Keep prices anchored to value (a 1,000-player community event is worth far more than $75 of compute), not to the trivial box cost.

### Model B — Community cost-recovery / donations
Everything free; cover the box with donations/Patreon.

- One CX32 (~$7.50/mo) + object storage ≈ **$10–15/mo all-in** covers a whole community's small rooms and a flagship big room.
- Ask for ~$15–25/mo in recurring donations → break-even with headroom; surplus funds a bigger box for events.
- Optional: "sponsor an event" one-off asks for the Federated/Large runs that cost real concurrency.
- Zero pricing UI; a donate link on the room page. Lowest friction, no billing system, no tax/【payment complexity. Best fit if the motivation is community goodwill, not income.

### Model C — Self-host / OSS (no pricing — infra guidance)
Ship the single-box image; "pricing" = telling users what their own box costs.

- "**$7.50–18/mo on a Hetzner CX32–CX42 hosts your whole group**, including a Large room. Bandwidth is included; saves go to cheap object storage." (`HOSTING.md §11`.)
- Optional later: a **paid hosted convenience tier** for people who don't want to run a box — that's Model A bolted on top of the OSS core (open-core).

---

## 4. Recommendation

If the goal is any revenue: **Model A (freemium), charging only for Large / Event / Private.** It's the only model that respects the free anchor while monetizing the genuine moat, and the margins are strong because marginal compute cost is near zero — the real cost is your ops time, so price events and private hosting on **value and effort**, not on the €7 box.

If the goal is community goodwill: **Model B** — lowest friction, a single donation link covers everything, no billing to build.

Either way, **ship Model C's self-host image first** (`SPEC_web_gui.md §2`, `HOSTING.md §10`): it's the fastest deliverable, validates the whole stack, costs nothing to operate (users bring their own box), and converts cleanly into A (open-core hosted tier) or B (one community box with a donate link) once you've decided.

---

## 5. What gates charging for "Large"

You can't sell the Large tier on peliarch until the **real-multidata loader** lands (`SPEC_remaining_go_functions.md` Batch A) — it's synthetic today. Until then, Large is "federated under the hood" (`HOSTING.md §4`), which *can* still be sold as an event tier on stock federation **now**. The harness (`SPEC_go_loadtest.md`) is the SLA tool: publish the measured per-box capacity number and price/gate tiers against it, so every capacity claim behind a paid plan is measured, not asserted.

## 6. Open decisions
- Pick the model (A / B / C — or C-then-A).
- If A: free Standard, or a token price to deter abuse? (Free + per-room resource caps + restricted unpickling, per `HOSTING.md §5`, is probably enough.)
- Event-tier pricing format: flat per-event vs sized by players×hours. Sized is fairer and matches the cost driver (concurrency), but flat is easier to sell.
- Region premium: Hetzner's cheap/high-bandwidth boxes are EU; US-located capacity costs more and includes less transfer (`HOSTING.md §11` caveat). If you target US streamers, a US box is a real cost line — price it into Private/Event tiers.
