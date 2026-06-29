#!/usr/bin/env python3
"""
gen_yamls.py — emit N minimal Clique player YAMLs for a large-room load test.

Clique is the minimal AP world (fast to generate, no ROMs), so it isolates the
SERVER from generation cost. Slot names are deterministic — <prefix><zero-padded i>
— so ap_loadtest.py can connect to them without parsing the multidata.

Note the item economy: stock Clique is light on cross-slot routing. This battery
stresses connection scaling, datastorage fan-out, save stalls, and reconnect — the
suspected bottlenecks. To later stress heavy cross-slot item traffic, regenerate with
a higher-location game and/or item_links and point the harness at that multidata; the
wire protocol the harness speaks is identical.

Usage:
  python gen_yamls.py --count 1000 --out ./loadtest_players
Then generate the multidata with stock Archipelago:
  python Generate.py --player_files_path ./loadtest_players --outputpath ./loadtest_out
"""
import argparse, os

TEMPLATE = """\
name: {name}
description: loadtest slot
game: {game}
{game}:
  progression_balancing: 0
  accessibility: minimal
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--game", default="ChecksFinder",
                    help="any ROM-free world that routes items cross-slot (ChecksFinder is a good default)")
    ap.add_argument("--prefix", default="P")
    ap.add_argument("--digits", type=int, default=4)
    ap.add_argument("--out", default="./loadtest_players")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for i in range(1, args.count + 1):
        name = f"{args.prefix}{i:0{args.digits}d}"
        with open(os.path.join(args.out, f"{name}.yaml"), "w") as f:
            f.write(TEMPLATE.format(name=name, game=args.game))
    print(f"wrote {args.count} {args.game} YAMLs to {args.out}")
    print("slot names: " + ", ".join(
        f"{args.prefix}{i:0{args.digits}d}" for i in (1, 2, args.count)).replace(
        f"{args.prefix}{2:0{args.digits}d}", f"{args.prefix}{2:0{args.digits}d} ... "))
    print("\nNext:")
    print(f"  python Generate.py --player_files_path {args.out} --outputpath ./loadtest_out")
    print("  # then host the resulting .archipelago / output folder with MultiServer.py")
    print(f"  # and run: python ap_loadtest.py --slots {args.count} "
          f"--slot-prefix {args.prefix} --slot-digits {args.digits}")


if __name__ == "__main__":
    main()
