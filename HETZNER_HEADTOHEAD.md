# Run the head-to-head against a Hetzner box (off-box, clean numbers)

Goal: put the **server on a Hetzner box** and drive it from **your laptop**, so the only
thing between harness and server is the real network. That's the clean off-box measurement
we kept saying we needed — and the first real step toward hosting on Hetzner.

You'll run Python MultiServer and peliarch on the box (both at once, on two ports), then
drive each in turn from your laptop and read the side-by-side.

---

## 0. One-time: make a box (~10 min, you're new — this is the whole flow)

1. Sign up at hetzner.com → **Cloud** → **New Project**.
2. **Add Server**: location near you, image **Ubuntu 24.04**, type **CX32** (4 vCPU/8 GB;
   bump to **CX42** for the bigger ceiling runs later). Add your **SSH key** (Hetzner shows
   how; on Windows, `ssh-keygen` in PowerShell makes one, paste `~/.ssh/id_ed25519.pub`).
3. Create it. Note the **public IPv4** (e.g. `203.0.113.10`). Billing is hourly — you can
   delete it when done and stop paying.

## 1. Copy your Archipelago folder to the box

From your laptop (PowerShell), in the folder's parent:

```powershell
# excludes the big stuff you don't need on the box
scp -r -O .\Archipelago root@203.0.113.10:~/Archipelago
```

(If `scp` is slow or chokes on `.git`, use WinSCP, or zip the folder first. You need:
`MultiServer.py` + the AP checkout, `archipelago-go/`, `ap_loadtest.py`,
`sample_server.py`, and a multidata in `loadtest_out/`.)

## 2. Set up the box

```bash
ssh root@203.0.113.10
bash ~/Archipelago/hetzner_setup.sh
```

This installs Python + Go, builds `peliarch`, makes a venv with the server deps, and
tells you whether it found a room in `loadtest_out/` (if not, it prints the generate
command — run it once on the box).

## 3. Open the firewall + start both servers

```bash
sudo ufw allow OpenSSH && sudo ufw allow 38281 && sudo ufw allow 38291 && sudo ufw --force enable
bash ~/Archipelago/start_servers.sh
```

`start_servers.sh` launches MultiServer on **:38281** and peliarch on **:38291** (Go's
`--locs-per-slot` defaulted to 25 to match the ChecksFinder room). They run at the same
time; since you drive them one at a time, the idle one sits at ~0 CPU and doesn't interfere.

## 4. (optional) Sample the box's server CPU

In a **second** SSH session, during each phase, capture the *server-side* CPU/RSS (the clean
signal now that the generator is elsewhere):

```bash
cd ~/Archipelago && . .venv/bin/activate
python3 sample_server.py --port 38281 --out py_cpu.csv   # during the Python phase
python3 sample_server.py --port 38291 --out go_cpu.csv   # during the Go phase
```

Ctrl+C each when its phase ends, then `scp` the CSVs back to your laptop to merge into the
table.

## 5. Drive it from your LAPTOP

In your local Archipelago folder (venv active):

```powershell
python headtohead_remote.py --host 203.0.113.10 --slots 250 --soak-seconds 180
# with box CPU merged in:
python headtohead_remote.py --host 203.0.113.10 --slots 250 `
  --py-cpu-csv .\py_cpu.csv --go-cpu-csv .\go_cpu.csv
```

It runs the harness against the Python port, then the Go port, identical params, and prints
the side-by-side into `h2h_remote_results/`.

## 6. Read it right

- The probe/routing **floors now include your laptop↔box network RTT** (tens of ms, not
  sub-ms). That's real — it's what players feel. Read the **wall behavior** (Python collapse
  vs Go staying flat), not the absolute floor.
- This is the version of the result you can quote: server isolated on its own box, generator
  isolated on yours, real network between.

## 7. Teardown

Stop servers (`pkill -f MultiServer; pkill -f peliarch`) and, to stop billing, **delete
the server** in the Hetzner console (or keep it — it's the start of your hosting box).

---

## Next: "how high can peliarch actually go?"

The head-to-head proves Go beats Python at 250. To find Go's *ceiling*, the catch is that
**your single laptop becomes the bottleneck before Go does** — `ap_loadtest.py` is itself a
single asyncio process, so one generator can't out-muscle a multi-core Go server. To push
Go to its real limit you need to **out-number it with generators**:

- **Server:** one beefy box (CX42/CX52, or a dedicated CPU plan) running only peliarch.
- **Generators:** several cheap boxes (or several processes), each running `ap_loadtest.py`
  against the **same** Go port with a **disjoint slot range**, so together they simulate
  4k / 8k / 16k connections the server can't tell apart.
- **Ramp:** push slots and `--check-rate` up rung by rung; watch the box's `sample_server.py`
  CPU. Go can exceed 100% (multi-core) — the ceiling is when it pegs **all** cores or latency
  finally climbs. That number is peliarch's real capacity per box, and it sets your
  per-room pricing for the Large tier.

This is the `OFFBOX_RUN.md` idea taken to its conclusion, generators-first. The tooling to
fan N generators at one server and aggregate the result is a small extension of the harness —
ask and it's a script.
