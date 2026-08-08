#!/usr/bin/env python3
"""
run_loadtest.py - one-command orchestrator for the AP single-room scaling sweep.

Runs the whole pipeline against THIS Archipelago checkout:
  gen_yamls -> Generate.py (once, at the largest rung) -> for each slot rung:
  start a FRESH MultiServer -> drive ap_loadtest.py -> kill server -> next rung
  -> sweep_compare over all results.

Why a fresh server per rung: a clean sweep needs each rung to start from a clean
room. Re-driving low-numbered slots against one long-lived server would hit
already-checked locations (fewer missing_locations -> contaminated throughput).
So we restart MultiServer and clear its save between rungs. One multidata is
generated at the largest rung; smaller rungs just connect a subset (P0001..P00NN),
which all exist in that multidata.

Every run is stamped with a run-id (default: timestamp). All outputs carry it:
  results_<run_id>_<n>.json  and  run_<run_id>.log
so reruns never collide and you never read a stale same-named file.

Tailored defaults for this checkout (auto-detected, override on the CLI):
  game    = ChecksFinder  (Clique is NOT installed here; ChecksFinder is ROM-free
                           and routes items cross-slot, so routing latency is real)
  version = matches Utils.__version__ so you never hit ConnectionRefused/InvalidVersion
  spoiler = 0             (skip the slow playthrough/spoiler calc; not needed here)

--no-save passes MultiServer's --disable_save: no auto-saver thread, _save never
fires. Use it to isolate how much of a stall is the GIL-holding pickle/zlib save
vs the single-loop routing/broadcast cost.

Examples
  python run_loadtest.py --rungs 50,100 --soak-seconds 30 --check-rate 1
  python run_loadtest.py --rungs 100,250,500,1000 --soak-seconds 180 --check-rate 1
  python run_loadtest.py --rungs 100,250 --no-save --run-id nosave   # save-isolation
  python run_loadtest.py --rungs 100,250 --dry-run
"""
import argparse, glob, os, re, shutil, socket, subprocess, sys, time

try:
    import psutil
except ImportError:
    psutil = None

# Stop AP's ModuleUpdate from pip-installing world requirements on every
# MultiServer/Generate launch (it pins protobuf==6.31.1 for SC2 and clobbers
# your global). Set before any subprocess so they all inherit it. Imported by
# every launcher (headtohead/go_sweep/federation*), so this one line covers all.
os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
_LOGF = None  # set in main(); log() tees here so the run is captured with its run-id


def log(msg):
    line = f"[runner] {msg}"
    print(line, flush=True)
    if _LOGF:
        _LOGF.write(line + "\n")
        _LOGF.flush()


def detect_version():
    """Read Utils.__version__ -> 'major,minor,build' so the client matches the server."""
    try:
        with open(os.path.join(HERE, "Utils.py")) as f:
            txt = f.read()
        m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', txt)
        if m:
            return ",".join(m.groups())
    except Exception:
        pass
    return "0,6,1"


def free_port(preferred):
    """Return preferred if free, else an OS-assigned open port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def wait_for_port(host, port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _rss(p):
    try:
        return p.memory_info().rss
    except Exception:
        return 0


def resolve_server_pid(launch_pid, port, timeout=10):
    """Find the PID actually LISTENING on `port`.

    On Windows `python MultiServer.py` (via ModuleUpdate / re-exec) can leave the
    launched PID as a thin parent while the real server runs as a child, so trusting
    Popen's pid gives bogus 0%%-CPU / ~12 MB-RSS samples. Resolve by port first, then
    fall back to the heaviest child of the launch process, then to launch_pid."""
    if psutil is None:
        return launch_pid
    end = time.time() + timeout
    while time.time() < end:
        # 1) whoever is LISTENing on the port = the real server
        try:
            for c in psutil.net_connections(kind="inet"):
                if (c.laddr and c.laddr.port == port and c.pid
                        and c.status == psutil.CONN_LISTEN):
                    return c.pid
        except Exception:
            pass
        # 2) heaviest descendant of the launched process
        try:
            kids = psutil.Process(launch_pid).children(recursive=True)
            if kids:
                return max(kids, key=_rss).pid
        except Exception:
            pass
        time.sleep(0.5)
    return launch_pid


def newest_multidata(out_dir):
    cands = []
    for pat in ("*.archipelago", "*.zip"):
        cands += glob.glob(os.path.join(out_dir, pat))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def stop_server(srv):
    """Cross-platform stop. terminate() = SIGTERM on POSIX, TerminateProcess on
    Windows (Windows can't deliver SIGINT to a child via send_signal)."""
    if srv is None:
        return
    srv.terminate()
    try:
        srv.wait(timeout=15)
    except subprocess.TimeoutExpired:
        srv.kill()
    time.sleep(1)


def run(cmd, dry, **kw):
    log("$ " + " ".join(str(c) for c in cmd))
    if dry:
        return 0
    return subprocess.call(cmd, **kw)


def main():
    global _LOGF
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rungs", default="100,250,500,1000",
                    help="comma-separated slot counts to sweep")
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default=None, help="major,minor,build (default: this checkout)")
    ap.add_argument("--port", type=int, default=38281)
    ap.add_argument("--check-rate", type=float, default=1.0)
    ap.add_argument("--soak-seconds", type=float, default=180.0)
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--tracker-fraction", type=float, default=0.3)
    ap.add_argument("--reconnect-fraction", type=float, default=0.25)
    ap.add_argument("--players-dir", default=os.path.join(HERE, "loadtest_players"))
    ap.add_argument("--out-dir", default=os.path.join(HERE, "loadtest_out"))
    ap.add_argument("--results-dir", default=os.path.join(HERE, "loadtest_results"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--spoiler", type=int, default=0,
                    help="Generate.py spoiler level; 0 (default) skips the slow "
                         "playthrough/spoiler calc, which the load test doesn't need")
    ap.add_argument("--no-save", action="store_true",
                    help="pass MultiServer --disable_save (no auto-saver thread / no "
                         "pickle+zlib save) to isolate the save stall from routing cost")
    ap.add_argument("--run-id", default=None,
                    help="stamp for output filenames (default: UTC timestamp)")
    ap.add_argument("--skip-generate", action="store_true",
                    help="reuse an existing multidata in --out-dir")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    version = args.version or detect_version()
    rungs = sorted(int(x) for x in args.rungs.split(","))
    max_slots = rungs[-1]
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    os.makedirs(args.results_dir, exist_ok=True)
    py = args.python

    if not args.dry_run:
        _LOGF = open(os.path.join(args.results_dir, f"run_{run_id}.log"), "w")

    log(f"run_id={run_id}")
    log(f"checkout={HERE}")
    log(f"game={args.game}  version={version}  spoiler={args.spoiler}  "
        f"no_save={args.no_save}  rungs={rungs}  max_slots={max_slots}")
    if psutil is None:
        log("WARNING: psutil not installed -> no server CPU/RSS sampling. "
            "pip install psutil")

    # ---- 1. YAMLs (generate at the largest rung; smaller rungs are subsets) ----
    if not args.skip_generate:
        if os.path.isdir(args.players_dir) and not args.dry_run:
            shutil.rmtree(args.players_dir)
        run([py, os.path.join(HERE, "gen_yamls.py"), "--count", str(max_slots),
             "--game", args.game, "--prefix", "P", "--digits", "4",
             "--out", args.players_dir], args.dry_run)

        # ---- 2. Generate the multidata once (--spoiler 0 skips the slow playthrough) ----
        if os.path.isdir(args.out_dir) and not args.dry_run:
            shutil.rmtree(args.out_dir)
        rc = run([py, os.path.join(HERE, "Generate.py"),
                  "--player_files_path", args.players_dir,
                  "--outputpath", args.out_dir,
                  "--spoiler", str(args.spoiler)], args.dry_run)
        if rc != 0 and not args.dry_run:
            sys.exit(f"Generate.py failed (rc={rc}). Fix generation before load testing.")

    multidata = newest_multidata(args.out_dir) if not args.dry_run else "<multidata>"
    if not multidata:
        sys.exit(f"No multidata found in {args.out_dir}. Run without --skip-generate.")
    log(f"multidata={multidata}")

    # ---- 3. Per-rung: fresh server -> drive harness -> kill ----
    results = []
    for n in rungs:
        port = free_port(args.port) if not args.dry_run else args.port
        # clear any saved room state so each rung starts clean
        if not args.dry_run:
            for sv in glob.glob(multidata + "_*") + glob.glob(os.path.join(args.out_dir, "*.apsave")):
                try:
                    os.remove(sv)
                except OSError:
                    pass

        log(f"=== rung {n} slots on port {port} ===")
        srv_cmd = [py, os.path.join(HERE, "MultiServer.py"), multidata,
                   "--port", str(port), "--host", "0.0.0.0"]
        if args.no_save:
            srv_cmd.append("--disable_save")
        log("$ " + " ".join(str(c) for c in srv_cmd))
        srv = None
        server_pid = None
        if not args.dry_run:
            srv = subprocess.Popen(srv_cmd, cwd=HERE)
            if not wait_for_port("127.0.0.1", port, timeout=90):
                stop_server(srv)
                sys.exit(f"MultiServer didn't open port {port} in time.")
            time.sleep(2)  # let it finish loading slots
            server_pid = resolve_server_pid(srv.pid, port)
            log(f"server launch pid={srv.pid}  sampling pid={server_pid}"
                + ("" if server_pid == srv.pid else "  (resolved real listener)"))

        result_path = os.path.join(args.results_dir, f"results_{run_id}_{n}.json")
        drive = [py, os.path.join(HERE, "ap_loadtest.py"),
                 "--host", "localhost", "--port", str(port),
                 "--slots", str(n), "--slot-prefix", "P", "--slot-digits", "4",
                 "--game", args.game, "--version", version,
                 "--ramp-seconds", str(args.ramp_seconds),
                 "--soak-seconds", str(args.soak_seconds),
                 "--check-rate", str(args.check_rate),
                 "--tracker-fraction", str(args.tracker_fraction),
                 "--reconnect-fraction", str(args.reconnect_fraction),
                 "--out", result_path]
        if server_pid is not None:
            drive += ["--server-pid", str(server_pid)]
        run(drive, args.dry_run)
        results.append(result_path)
        stop_server(srv)

    # ---- 4. Decision table ----
    log("=== sweep comparison ===")
    run([py, os.path.join(HERE, "sweep_compare.py")] + results, args.dry_run)
    if not args.dry_run:
        log(f"done. results stamped run_id={run_id} in {args.results_dir}")
        if _LOGF:
            _LOGF.close()


if __name__ == "__main__":
    main()
