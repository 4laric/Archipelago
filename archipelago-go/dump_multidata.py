#!/usr/bin/env python3
"""
dump_multidata.py — export an Archipelago multidata to a Go-friendly JSON bundle.

peliarch consumes this bundle instead of importing `worlds` or unpickling the
multidata itself: everything the runtime server needs (routing table, slot identity,
auth gates, datapackage, options) is static and exportable once at generation time.
See PROTOCOL_SURFACE.md ("What the server needs at boot") and
specs/SPEC_remaining_go_functions.md Batch A.

Usage:
    python dump_multidata.py <room.archipelago | room.zip> -o room.apgo.json

Accepts a raw .archipelago, a generation-output .zip (finds the .archipelago inside),
or any file whose bytes are an AP multidata (format byte + zlib + restricted pickle).

JSON shapes (all dict keys are strings because JSON has no int keys; Go parses them):
    locations:        {slot: {loc_id: [item_id, target_player, flags]}}
    slot_info:        {slot: {name, game, type, group_members}}
    connect_names:    {name: [team, slot]}
    min_client_versions: {slot: [major, minor, build]}
    precollected_items:  {slot: [item_id, ...]}     # start inventory
    slot_data:        {slot: {...}}
    datapackage:      {game: {item_name_to_id, location_name_to_id,
                              item_name_groups, location_name_groups, checksum}}
    datapackage_checksums: {game: checksum}
"""
import argparse
import io
import json
import os
import sys
import zipfile
import zlib

# import from the AP checkout one directory up (archipelago-go/ lives inside the repo)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Utils import restricted_loads  # noqa: E402


def read_multidata_bytes(path: str) -> bytes:
    """Return the raw multidata bytes from a .archipelago, or from inside a .zip."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".archipelago")]
            if not names:
                raise SystemExit(f"no .archipelago found inside {path}: {z.namelist()}")
            return z.read(names[0])
    with open(path, "rb") as f:
        return f.read()


def decompress(data: bytes) -> dict:
    """Mirror MultiServer.MultiServer.decompress: strip the format byte, inflate, unpickle."""
    fmt = data[0]
    if fmt > 3:
        raise SystemExit(f"incompatible multidata format version {fmt}")
    return restricted_loads(zlib.decompress(data[1:]))


def export(obj: dict) -> dict:
    slot_info_raw = obj["slot_info"]

    slot_info = {}
    games = {}
    for slot, si in slot_info_raw.items():
        slot_info[str(slot)] = {
            "name": si.name,
            "game": si.game,
            "type": int(si.type),
            "group_members": list(si.group_members),
        }
        games[str(slot)] = si.game

    # routing table: slot -> {loc -> [item, target_player, flags]}
    locations = {}
    n_locs = 0
    for slot, table in obj["locations"].items():
        out = {}
        for loc, target in table.items():
            item_id, target_player, flags = target[0], target[1], target[2]
            out[str(loc)] = [item_id, target_player, flags]
            n_locs += 1
        locations[str(slot)] = out

    connect_names = {name: [team, slot] for name, (team, slot) in obj["connect_names"].items()}

    clients_ver = obj.get("minimum_versions", {}).get("clients", {})
    min_client_versions = {str(slot): list(v) for slot, v in clients_ver.items()}

    precollected = {str(slot): list(items) for slot, items in obj.get("precollected_items", {}).items()}

    slot_data = {str(slot): data for slot, data in obj.get("slot_data", {}).items()}

    datapackage = {}
    checksums = {}
    for game, dp in obj.get("datapackage", {}).items():
        datapackage[game] = {
            "item_name_to_id": dp.get("item_name_to_id", {}),
            "location_name_to_id": dp.get("location_name_to_id", {}),
            "item_name_groups": dp.get("item_name_groups", {}),
            "location_name_groups": dp.get("location_name_groups", {}),
            "checksum": dp.get("checksum"),
        }
        if dp.get("checksum"):
            checksums[game] = dp["checksum"]

    so = obj.get("server_options", {}) or {}
    server_options = {
        "location_check_points": so.get("location_check_points", 1),
        "hint_cost": so.get("hint_cost", 10),
        "release_mode": so.get("release_mode", "goal"),
        "collect_mode": so.get("collect_mode", "goal"),
        "remaining_mode": so.get("remaining_mode", "goal"),
    }
    # room connection password (gates Connect); CLI --password on the Go side overrides.
    password = so.get("server_password") or so.get("password") or None

    bundle = {
        "seed_name": obj.get("seed_name"),
        "server_version": list(obj.get("minimum_versions", {}).get("server", (0, 5, 0))),
        "generator_version": list(obj.get("version", (0, 0, 0))),
        "password": password,
        "games": games,
        "slot_info": slot_info,
        "connect_names": connect_names,
        "min_client_versions": min_client_versions,
        "locations": locations,
        "slot_data": slot_data,
        "precollected_items": precollected,
        "datapackage": datapackage,
        "datapackage_checksums": checksums,
        "server_options": server_options,
        "_meta": {
            "n_slots": len(slot_info),
            "n_locations": n_locs,
            "games": sorted(set(games.values())),
        },
    }
    return bundle


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help=".archipelago, generation .zip, or raw multidata")
    ap.add_argument("-o", "--out", default=None, help="output JSON (default: <input>.apgo.json)")
    ap.add_argument("--indent", type=int, default=None, help="pretty-print with this indent (default: compact)")
    args = ap.parse_args()

    raw = read_multidata_bytes(args.input)
    obj = decompress(raw)
    bundle = export(obj)

    out = args.out or (os.path.splitext(args.input)[0] + ".apgo.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=args.indent, ensure_ascii=False)

    m = bundle["_meta"]
    print(f"wrote {out}")
    print(f"  slots: {m['n_slots']}  locations: {m['n_locations']}  games: {', '.join(m['games'])}")
    print(f"  size: {os.path.getsize(out):,} bytes")


if __name__ == "__main__":
    main()
