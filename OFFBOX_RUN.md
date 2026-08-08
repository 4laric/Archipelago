# Off-box generator run — two-machine setup

**Why:** in the single-box runs the load generator and the server share a CPU, and the
loop-health probe lives *inside the generator process* — so at the knee we can't fully
tell whether a multi-second stall is the server or the generator starving (see the
caveat in `FINDINGS.md`). Putting the generator on a second machine removes the
contention and makes the latency numbers quotable.

**Goal of this run:** confirm the 100→250 wall is the *server's* limit (not the
generator's), and get clean absolute latencies. We also have to sample the server's
CPU/RSS *on the server box* now, because the harness's `--server-pid` can't reach
across machines — that's what `sample_server.py` is for.

## Roles

- **Server box** — your AP checkout (the powerful machine; this is what we're measuring).
  Hosts MultiServer and runs `sample_server.py`.
- **Generator box** — any second machine on the same LAN. Runs `ap_loadtest.py`. Only
  needs Python + `websockets` + `psutil` + a copy of `ap_loadtest.py`; **no AP checkout,
  no multidata** (the harness only needs slot count/prefix/digits, which are
  deterministic).

Use a **wired LAN** if you can. Wi-Fi adds variable latency that muddies the probe RTT
(which now includes the network round-trip).

## One-time prep

**Server box**

1. Generate the multidata once (fast path, no spoiler):
   ```powershell
   python gen_yamls.py --count 1000 --game ChecksFinder --out .\loadtest_players
   python Generate.py --player_files_path .\loadtest_players --outputpath .\loadtest_out --spoiler 0
   ```
2. Find this box's LAN IP (`ipconfig` → IPv4, e.g. `192.168.1.50`).
3. Allow the port through the firewall (run once, elevated):
   ```powershell
   New-NetFirewallRule -DisplayName "AP loadtest" -Direction Inbound -Protocol TCP -LocalPort 38281 -Action Allow
   ```

**Generator box**

```powershell
py -3.13 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install websockets psutil
# copy ap_loadtest.py over from the server box (that one file is all you need)
```

## Per-rung procedure (repeat for 100, 250, 500, 1000)

Do these in order; the server must be listening before the sampler and generator start.

**1. Server box — host the room (fresh each rung).** Delete any prior save so the room
starts clean, then host bound to all interfaces:
```powershell
Remove-Item .\loadtest_out\*.apsave -ErrorAction SilentlyContinue
python MultiServer.py .\loadtest_out\<the AP_*.zip> --host 0.0.0.0 --port 38281
```
(Add `--disable_save` if you're doing a save-off comparison.)

**2. Server box — start the CPU/RSS sampler** (new terminal, while the server runs):
```powershell
python sample_server.py --port 38281 --interval 0.5 --out server_<N>.csv
```
It auto-resolves the real listening PID. Ctrl+C it when the rung finishes.

**3. Generator box — drive the swarm** at the server's IP:
```powershell
python ap_loadtest.py --host 192.168.1.50 --port 38281 `
  --slots <N> --slot-prefix P --slot-digits 4 `
  --game ChecksFinder --version 0,6,6 `
  --ramp-seconds 30 --soak-seconds 180 --check-rate 1 `
  --tracker-fraction 0.3 --reconnect-fraction 0.25 `
  --out results_offbox_<N>.json
```
Omit `--server-pid` — it can't see the remote process; CPU comes from the sampler in
step 2 instead.

**4. Tear down:** Ctrl+C the sampler, stop the server, repeat for the next rung.

Then pull `results_offbox_*.json` back to the server box and run
`python sweep_compare.py results_offbox_100.json ...` for the table.

## What to look for

- **Does 250 still crater?** If routing p50 and the probe tail are still in the seconds
  with the generator off-box, the wall is genuinely the **server** — banks the FINDINGS
  conclusion. If 250 comes back healthy, a chunk of the earlier cliff was the co-located
  generator, and the real knee is higher.
- **Server CPU at the knee (from `server_<N>.csv`).** If CPU is still low (~10–20%) while
  latency is high, it confirms I/O / head-of-line blocking rather than compute — the
  serialized-broadcast story. If it's pinned near 100% of one core, it's the single-loop
  CPU ceiling.
- **Generator headroom.** Watch the generator box's own CPU (Task Manager). If *it*
  saturates a core at 500–1000, split clients across two generator boxes (run
  `ap_loadtest.py` on each with a disjoint slot range via `--slots` against the same
  server) so you're measuring the server, not the load tool.
- **Probe baseline.** On a wired LAN the healthy probe p50 should be a few ms (network
  RTT). If the *baseline* is tens of ms, that's the network, not the server — subtract it
  mentally when reading the stall tail.

## Notes

- Smaller rungs reuse the same 1000-slot multidata (they connect P0001..P00NN, a subset).
- Keep every parameter identical to the single-box runs (check-rate, soak, fractions) so
  the two-machine numbers are directly comparable to the FINDINGS table.
- The same harness validates any reimplementation later: point `--host` at the new
  server and rerun this procedure unchanged.
