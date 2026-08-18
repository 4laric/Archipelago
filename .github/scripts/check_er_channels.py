#!/usr/bin/env python3
"""Verify the deployed ER stable/beta builders against their authoritative Git refs."""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request

DEFAULT_REPO = "4laric/er-archipelago"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "peliarch-er-channel-ci"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def stable_ref(channels: bytes) -> str:
    found = ""
    for raw in channels.decode("utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        fields = raw.split("\t")
        if len(fields) >= 2 and fields[0] == "stable":
            found = fields[1]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", found):
        raise ValueError(f"stable channel is not an immutable release tag: {found!r}")
    return found


def wizard_version(page: bytes) -> str:
    match = re.search(rb'"apworld_version"\s*:\s*"([^"]+)"', page)
    if not match:
        raise ValueError("wizard has no embedded apworld_version")
    return match.group(1).decode("ascii")


def download_ref(page: bytes) -> str:
    match = re.search(rb"/releases/download/(v\d+\.\d+\.\d+)/ER-Archipelago-", page)
    if not match:
        raise ValueError("downloads page has no stable bundle release link")
    return match.group(1).decode("ascii")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(get, base_url: str, repo: str = DEFAULT_REPO) -> list[str]:
    raw = f"https://raw.githubusercontent.com/{repo}"
    channels = get(f"{raw}/main/release/CHANNELS.tsv")
    stable = stable_ref(channels)
    expected_stable = get(f"{raw}/{stable}/wizard/wizard.html")
    expected_beta = get(f"{raw}/main/wizard/wizard.html")
    live_stable = get(f"{base_url.rstrip('/')}/er/wizard.html")
    live_beta = get(f"{base_url.rstrip('/')}/er/beta/wizard.html")
    downloads = get(f"{base_url.rstrip('/')}/downloads")

    errors = []
    if live_stable != expected_stable:
        errors.append(
            f"stable wizard is not {stable}: live {digest(live_stable)}, expected {digest(expected_stable)}")
    if live_beta != expected_beta:
        errors.append(
            f"beta wizard is not main: live {digest(live_beta)}, expected {digest(expected_beta)}")
    if wizard_version(live_stable) != stable.removeprefix("v"):
        errors.append(
            f"stable wizard embeds {wizard_version(live_stable)}, channel ledger says {stable}")
    if download_ref(downloads) != stable:
        errors.append(f"downloads points at {download_ref(downloads)}, channel ledger says {stable}")
    if expected_stable != expected_beta and live_stable == live_beta:
        errors.append("stable and beta are byte-identical even though main is ahead of stable")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://peliarch.ca")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    try:
        errors = verify(fetch, args.base_url, args.repo)
    except Exception as exc:  # network/schema failure is a failed monitor, never a green silence
        print(f"ER channel verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("ER channels agree: stable builder, beta builder, downloads, and Git refs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
