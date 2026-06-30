# SPEC — Full test load for peliarch

**Status:** implementation-ready
**Owner:** Alaric
**Depends on:** `peliarch/` binary, `ap_loadtest.py`, `sweep_compare.py`, `sample_server.py`, `headtohead_remote.py`, `go_ceiling.py`
**Goal:** a repeatable, documented load-test battery that (a) exercises **every** command peliarch implements at scale, (b) proves the head-to-head win vs stock MultiServer at the knee, and (c) drives the generators-first ceiling hunt until Go's *own* compute limit is found — the number that sets Large-tier per-room pricing.

---

## 1. What's already proven vs what's missing

From `FINDINGS.md`:

- Off-box ceiling on a Hetzner CX22 (2 vCPU / 4 GB, €4/mo): **4,000 concurrent connections, zero errors, routing p95 ~145 ms, 26% mean CPU.** Throughput tracked input 1:1 the whole way.
- The 8,000 rung shortfall was **the client**, not the server: only 5,985 of 8,000 connections established (one laptop + home NAT topped out). Server CPU never saturated, RAM peaked 2.79 GB of 4 GB.

So the architectural verdict is in. **What this spec adds is the missing rigor:**

1. **Coverage gap.** The harness exercises Connect, LocationChecks→ReceivedItems, LocationScouts, Get/SetNotify/Set. It does **not** exercise Bounce/DeathLink, Sync, StatusUpdate, GetDataPackage, hints, or admin commands. As those land (`SPEC_remaining_go_functions.md`, `SPEC_bounce_deathlink.md`) the load battery must grow to cover them — otherwise "full load" is only loading the four hot commands.
2. **Generator gap.** One laptop maxes ~6k connections. Go's true ceiling is unknown because we never out-numbered it. We need a **generators-first** fan harness (multiple boxes/processes, disjoint slot ranges, same port).
3. **Real-multidata gap.** Every run to date is synthetic (`mock_server.py` parity). Synthetic is correct for an *architecture* comparison, but the Large tier needs a run against a real `.archipelago` once the multidata loader lands. Keep both: synthetic for clean A/B, real for SLA sign-off.

---

## 2. Test matrix

| Run | Server | Generator(s) | Slots / conns | Purpose | Pass gate |
|---|---|---|---|---|---|
| **CI smoke** | Go, synthetic | 1 process | 50 | catch regressions on every commit | RoomInfo→Connect→Connected→Get all answer; 0 errors; routing p95 < 50 ms (localhost) |
| **Sweep** | Go, synthetic | 1 process | 100 / 250 / 500 / 1000 | curve shape vs `FINDINGS.md` Python baseline | flat curve through 1000; items == checks; 0 dropped conns |
| **Head-to-head** | Python :38281 + Go :38291 | 1 laptop, off-box | 250 (the knee) | the quotable A/B | Python collapses (routing p50 multi-second), Go stays flat (< 1 s over WAN) |
| **Ceiling (generators-first)** | Go alone, beefy box | N boxes/procs, disjoint ranges | 4k → 8k → 16k → … | find Go's real compute limit | ramp until **all** cores peg or routing climbs; record the conn count at that point |
| **Coverage** | Go, synthetic | 1 process, all-commands profile | 250 | every implemented command under load, not just the hot four | each command's tail bounded; no command starves another |
| **SLA sign-off** | Go, **real multidata** | generators-first | the tier's target size | qualify Large tier for a real room | meets the published Large-tier capacity number with margin |

---

## 3. Procedures

### 3.1 Build
```sh
cd archipelago-go
go vet ./...
go mod tidy
go build -o peliarch .
```
`go vet` is a gate, not a suggestion — the skeleton was historically unbuilt in the dev sandbox.

### 3.2 CI smoke (runs on every commit)
```sh
./peliarch --host 127.0.0.1 --port 38291 --locs-per-slot 25 &
SRV=$!
python ap_loadtest.py --host 127.0.0.1 --port 38291 --slots 50 \
  --ramp-seconds 5 --soak-seconds 30 --check-rate 1 --tracker-fraction 0.3 \
  --out ci_smoke.json
kill $SRV
python - <<'PY'
import json,sys
r=json.load(open("ci_smoke.json"))
# gate: zero errors, items delivered ~= checks, probe tail sane
assert r["errors"]==0, r
PY
```
Wire this into `.github/` as a job. Keep `--locs-per-slot` matched to whatever room the comparison baseline used (25 for the ChecksFinder room).

### 3.3 Sweep + compare
```sh
./peliarch --host 0.0.0.0 --port 38291 --locs-per-slot 25 &
for n in 100 250 500 1000; do
  python ap_loadtest.py --host localhost --port 38291 --slots $n \
    --check-rate 1 --soak-seconds 180 --tracker-fraction 0.3 --out go_$n.json
done
python sweep_compare.py go_100.json go_250.json go_500.json go_1000.json
```
Read the curve **shape** against the Python table in `FINDINGS.md §The sweep`. Win condition: where Python walls at 100–250, Go holds flat and keeps climbing because it uses all cores.

### 3.4 Head-to-head, off-box (the quotable result)
Follow `HETZNER_HEADTOHEAD.md` verbatim: provision CX32, copy repo, `hetzner_setup.sh`, open firewall, `start_servers.sh` (Python :38281, Go :38291), then from the laptop:
```sh
python headtohead_remote.py --host <box-ip> --slots 250 --soak-seconds 180 \
  --py-cpu-csv py_cpu.csv --go-cpu-csv go_cpu.csv
```
Sample server-side CPU with `sample_server.py --port <p>` in a second SSH session per phase. Floors now include real laptop↔box RTT — read wall behavior, not absolute floor.

### 3.5 Ceiling, generators-first (the missing run)
The single-laptop limit is the only thing standing between us and Go's real number. To out-number the server:

- **Server:** one beefy box (CX42/CX52 or a dedicated-vCPU plan) running **only** peliarch.
- **Generators:** several cheap boxes (or N processes), each running `ap_loadtest.py` against the **same** Go port with a **disjoint** `--slot-prefix` / slot range so the server can't tell them apart. `go_ceiling.py` already fans 16 local generator processes at one port; the generators-first extension is the same idea across boxes.
- **Ramp:** push slots and `--check-rate` rung by rung; watch `sample_server.py` CPU on the box. Go can exceed 100% (multi-core). **Ceiling = when all cores peg or routing latency finally climbs off the floor.** Record the connection count there — that is peliarch's capacity per box and the input to Large-tier pricing.
- **Knobs that move the ceiling:** the `send` channel buffer in `main.go` (currently 8192 — the RAM driver; shrink to push the RAM ceiling up), box RAM, box core count.

**Deliverable to build:** a small `fan_generators.py` (or shell) that launches K `ap_loadtest.py` invocations with disjoint slot ranges against one host:port and aggregates their JSON into one rung row. Noted as a "small extension of the harness" in `HETZNER_HEADTOHEAD.md`; this spec makes it a required artifact for the ceiling run.

### 3.6 Coverage run (grows with the server)
Once `SPEC_remaining_go_functions.md` and `SPEC_bounce_deathlink.md` land, extend `ap_loadtest.py` with a profile that also issues, per soak second: a Bounce/DeathLink broadcast from a fraction of slots, periodic StatusUpdate, occasional Sync, a GetDataPackage at connect, and hint creation. Gate: each command's latency tail stays bounded and no command class starves another under 250-slot load.

---

## 4. Metrics & artifacts

Every run emits JSON (`--out`). Capture, per run:

- probe p50/p95/p99 (loop-health `Get` ping), routing p50/p95 (check→ReceivedItems pairing), fanout p99 (Set→SetReply), items delivered vs checks sent, connections established vs requested, errors.
- server-side CPU mean/peak and RSS peak (`sample_server.py` CSV), merged into the result table.

Store under `go_results/`, `h2h_remote_results/`, `go_ceiling_results/` (dirs already exist). Each ceiling rung appends a row matching the `FINDINGS.md` cloud-ceiling table format.

## 5. Pass/fail gates (SLA)

- **Correctness:** items delivered ≥ 99.5% of checks sent at every rung below the ceiling; 0 InvalidPacket; established ≥ 99% of requested (until the generator, not the server, is the cap).
- **Latency:** routing p95 at the floor (network RTT) through the rated capacity; no multi-second tail below the ceiling.
- **Resource:** server never pegs all cores below rated capacity; RSS within box RAM with headroom.
- **Regression:** CI smoke must pass on every commit; sweep curve must not regress vs the last stamped run.

## 6. Definition of done

1. CI smoke wired into `.github/` and green.
2. Sweep + head-to-head re-run on current binary, tables stamped into `FINDINGS.md`.
3. `fan_generators.py` built; one generators-first ceiling run completed that **pegs the box** (finds the real ceiling, not the laptop's).
4. Coverage profile lands alongside each new command batch.
5. One SLA sign-off run against a real multidata once the loader exists, producing the published Large-tier capacity number.
