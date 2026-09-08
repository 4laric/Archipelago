#!/usr/bin/env python3
"""Verify a game's deployed stable/beta builders against their authoritative Git refs.

WAS `check_er_channels.py`, ONE GAME HARDCODED FIVE WAYS: the repo, the path a wizard lives at in
that repo, the site root it is served under, the bundle filename in the downloads link, and the
release-tag spelling. With Bloodborne on the same site those five became a row in GAMES below,
and the monitor is `--game er` or `--game bb`. The alternative was a second copy of this file,
which is the drift this check exists to catch, one level up.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request

#: V.R.M with an OPTIONAL fourth "fixpack" segment (er-archipelago `tools/vrmf.py`): `v0.6.0` is
#: the first release on a line and `v0.6.0.1`, `v0.6.0.2` the fixpacks after it.
VRMF = r"v\d+\.\d+\.\d+(?:\.\d+)?"

#: Bloodborne's beta spelling. These are immutable tags like any other -- GitHub's "prerelease"
#: checkbox is a UI flag, not a mutability property, and `release/CHANNELS.tsv` is what promotes
#: one to stable. See `_resolve` in webgui/releases.py.
VRMF_BETA = r"v\d+\.\d+\.\d+(?:\.\d+|-beta\.\d+)?"


class GameChannels:
    """One game's five formerly-hardcoded facts.

    Deliberately NOT a dataclass: webgui/test_er_channels.py loads this file by path with
    `spec_from_file_location` and never registers it in `sys.modules`, and `@dataclass` resolves
    annotations through `sys.modules[cls.__module__]` at class-creation time -- which is None
    under that loader. A plain class has no such dependency.
    """

    def __init__(self, key, name, repo, root, wizard_path, bundle_prefix, tag_pattern,
                 embeds_full_tag=True):
        self.key = key
        self.name = name
        self.repo = repo
        self.root = root                    # site root the pages are served under, e.g. "er"
        self.wizard_path = wizard_path      # path to the builder inside the repo
        self.bundle_prefix = bundle_prefix  # filename prefix in the /downloads release link
        self.tag_pattern = tag_pattern
        #: What the builder embeds as `apworld_version`. ER embeds the whole tag minus `v`,
        #: fixpack included. Bloodborne embeds `archipelago.json`'s `world_version`, which
        #: Archipelago forces to strict X.Y.Z (`tuplize_version` raises on anything else), so a
        #: `v0.1.0.2` or `v0.1.0-beta.5` stable can only ever embed `0.1.0`.
        self.embeds_full_tag = embeds_full_tag

    def embedded_version(self, tag: str) -> str:
        """The `apworld_version` a builder built at `tag` embeds."""
        version = tag.removeprefix("v")
        if self.embeds_full_tag:
            return version
        match = re.match(r"\d+\.\d+\.\d+", version)
        if not match:
            raise ValueError(f"{tag!r} does not start with a V.R.M version")
        return match.group(0)


GAMES = {
    "er": GameChannels(
        key="er", name="ER", repo="4laric/er-archipelago", root="er",
        wizard_path="wizard/wizard.html", bundle_prefix="ER-Archipelago-", tag_pattern=VRMF),
    "bb": GameChannels(
        key="bb", name="BB", repo="4laric/bb-archipelago", root="bb",
        wizard_path="site/wizard.html", bundle_prefix="BloodborneAPLauncher-",
        tag_pattern=VRMF_BETA, embeds_full_tag=False),
}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "peliarch-channel-ci"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def stable_ref(channels: bytes, tag_pattern: str = VRMF) -> str:
    found = ""
    for raw in channels.decode("utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        fields = raw.split("\t")
        if len(fields) >= 2 and fields[0] == "stable":
            found = fields[1]
    if not re.fullmatch(tag_pattern, found):
        raise ValueError(f"stable channel is not an immutable release tag: {found!r}")
    return found


def wizard_version(page: bytes) -> str:
    match = re.search(rb'"apworld_version"\s*:\s*"([^"]+)"', page)
    if not match:
        raise ValueError("wizard has no embedded apworld_version")
    return match.group(1).decode("ascii")


def download_ref(page: bytes, game: GameChannels = None) -> str:
    game = game or GAMES["er"]
    pattern = r"/releases/download/(%s)/%s" % (game.tag_pattern, re.escape(game.bundle_prefix))
    match = re.search(pattern.encode("ascii"), page)
    if not match:
        raise ValueError("downloads page has no stable bundle release link")
    return match.group(1).decode("ascii")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(get, base_url: str, repo: str = None, game: GameChannels = None) -> list[str]:
    game = game or GAMES["er"]
    repo = repo or game.repo
    raw = f"https://raw.githubusercontent.com/{repo}"
    site = base_url.rstrip("/")
    channels = get(f"{raw}/main/release/CHANNELS.tsv")
    stable = stable_ref(channels, game.tag_pattern)
    expected_stable = get(f"{raw}/{stable}/{game.wizard_path}")
    expected_beta = get(f"{raw}/main/{game.wizard_path}")
    live_stable = get(f"{site}/{game.root}/wizard.html")
    live_beta = get(f"{site}/{game.root}/beta/wizard.html")
    downloads = get(f"{site}/downloads")

    errors = []
    if live_stable != expected_stable:
        errors.append(
            f"stable wizard is not {stable}: live {digest(live_stable)}, expected {digest(expected_stable)}")
    if live_beta != expected_beta:
        errors.append(
            f"beta wizard is not main: live {digest(live_beta)}, expected {digest(expected_beta)}")
    if wizard_version(live_stable) != game.embedded_version(stable):
        errors.append(
            f"stable wizard embeds {wizard_version(live_stable)}, channel ledger says {stable} "
            f"(expected {game.embedded_version(stable)})")
    if download_ref(downloads, game) != stable:
        errors.append(
            f"downloads points at {download_ref(downloads, game)}, channel ledger says {stable}")
    if expected_stable != expected_beta and live_stable == live_beta:
        errors.append("stable and beta are byte-identical even though main is ahead of stable")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="er", choices=sorted(GAMES))
    parser.add_argument("--base-url", default="https://peliarch.ca")
    parser.add_argument("--repo", default=None)
    args = parser.parse_args(argv)
    game = GAMES[args.game]
    try:
        errors = verify(fetch, args.base_url, args.repo, game)
    except Exception as exc:  # network/schema failure is a failed monitor, never a green silence
        print(f"{game.name} channel verification failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"{game.name} channels agree: stable builder, beta builder, downloads, and Git refs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
