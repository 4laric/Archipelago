"""
releases.py -- what the Downloads page knows about the published Elden Ring release.

WHY THIS IS A FETCH AND NOT A CONSTANT. The two things a player downloads --
`ER-Archipelago-v<ver>.zip` and the bare `eldenring.apworld` -- live on GitHub Releases, which
`release/DISTRIBUTION.md` names as the single source of truth. Peliarch is a THIRD publishing
surface for the same project (the tag, the wizard at /er/, and now this page), and the
er-archipelago spec `SPEC-publishing-pipeline.md` recorded on 2026-08-08 that all three were
serving different builds because nothing pinned any of them to each other. A hardcoded version
here would be a fourth thing to forget to bump, so this reads the release rather than restating it.

THE RULE THIS MODULE EXISTS TO NOT BREAK. The apworld and the client `.dll` are a HASH-MATCHED
PAIR. A mismatched pair does not fail at the door -- it connects and then behaves subtly wrong. So
the page must never present two assets from different tags as if they were one download. That is
why this module resolves ONE release and reports what that release actually carries, instead of
per-asset "newest that has it" resolution: the latter quietly manufactures exactly the cross-tag
pair the whole distribution design is built to prevent.

When an asset is missing from the resolved release, `Asset.url` is None and the caller renders the
gap. `last_seen_tag` says where it was last published, so the gap can be described honestly rather
than papered over -- and a link to an older tag is only ever rendered with that older tag written
on it. (This is not hypothetical: v0.3.11, the newest tag on 2026-08-12, shipped the bundle and no
bare apworld at all.)

Degradation is total and silent-to-the-user: any failure -- no network, HTTP error, rate limit,
malformed JSON -- yields `Releases.ok == False` and the page falls back to a plain link to the
releases index, which is always right and never stale. A downloads page that renders a dead
download URL is worse than one that renders no URL.

No new dependency: stdlib urllib, because `deploy/docker/requirements-host.txt` is hand-curated and
adding `requests` to it for one GET is a drift risk out of proportion to the call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from webgui import games

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

#: 🛑 THESE FIVE ARE ELDEN RING'S ROW, RE-EXPORTED, NOT THE SITE'S CONFIGURATION ANY MORE.
#: They used to BE the configuration: one repo, one ledger, one Nexus page, one asset pair. With a
#: second game they moved to `webgui/games.py`, where every game has its own. They stay here under
#: their old names because `.env.example`, the deploy README and the existing tests all speak them.
DOWNLOADS_REPO   = games.ER.downloads_repo
CHANNELS_URL     = games.ER.channels_url
CHANNELS_RAW_URL = games.ER.channels_raw_url
NEXUS_URL        = games.ER.nexus_url
GAME_GITHUB_URL  = games.ER.github_url

#: Seconds a fetched release is reused. The measured tag cadence on er-archipelago is a 0.82-day
#: MEDIAN GAP, so anything under an hour is already far finer than the thing it tracks; 15 minutes
#: keeps an unauthenticated box at 4 calls/hour against GitHub's 60/hour anonymous budget.
DOWNLOADS_TTL_SECONDS = int(os.environ.get("DOWNLOADS_TTL_SECONDS", "900"))

#: Seconds to wait on the API before giving up and rendering the degraded page. Deliberately short:
#: this call sits in the request path of a page whose fallback is perfectly usable, so a slow
#: GitHub must cost a fraction of a second, not a page load.
DOWNLOADS_TIMEOUT_SECONDS = float(os.environ.get("DOWNLOADS_TIMEOUT_SECONDS", "4"))

#: Optional. Raises the anonymous 60/hour rate limit to 5000/hour. Not required, and the box does
#: not need one -- a public repo's releases are readable without auth.
DOWNLOADS_GITHUB_TOKEN = os.environ.get("DOWNLOADS_GITHUB_TOKEN", "")

#: How many releases back to look when answering "where was this asset last published". Only used
#: for the honest-gap note; it never promotes an older tag into a download button on its own.
_HISTORY_DEPTH = 10

#: Elden Ring's asset matchers, re-exported from its row for the same reason as the block above.
#: `_resolve` takes the matchers as an argument now; nothing in this module reads this constant.
_WANTED = games.ER.wanted


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Asset:
    """One downloadable file on the resolved release.

    `url is None` means the resolved release did not carry it. That is a state the page renders,
    not an error -- a tag can legitimately ship one asset and not the other, and saying so is the
    whole point of this module.
    """

    kind: str
    filename: Optional[str] = None
    url: Optional[str] = None
    size_bytes: int = 0
    #: Tag where this asset was last published, when the resolved release lacks it. Rendered only
    #: alongside that tag's name, never as an unlabelled "download".
    last_seen_tag: Optional[str] = None
    last_seen_url: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.url)

    @property
    def size_human(self) -> str:
        if not self.size_bytes:
            return ""
        mb = self.size_bytes / (1024 * 1024)
        return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


@dataclass(frozen=True)
class Releases:
    """The resolved release, or a marker that we could not resolve one.

    `ok is False` is not exceptional and carries no detail for the page: the fallback (link the
    releases index) does not vary by cause, and a downloads page is not a status console.
    """

    ok: bool = False
    tag: Optional[str] = None
    published_at: Optional[str] = None
    html_url: Optional[str] = None
    assets: dict = field(default_factory=dict)

    def asset(self, kind: str) -> Asset:
        return self.assets.get(kind, Asset(kind=kind))


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()

#: ONE dict keyed by `(game_key, channel)`, not two globals. With two games the old pair of
#: single-slot caches would have made every game's answer the previous caller's answer: a
#: Bloodborne fetch would evict Elden Ring's, and worse, an ER outage would have served the cached
#: BB release under an ER heading. `TestPerGameReleaseCache` pins that they cannot cross.
#: Each value is `{"at": float, "value": Releases|None}`.
_cache: dict = {}


def _slot(game_key: str, channel: str) -> dict:
    return _cache.setdefault((game_key, channel), {"at": 0.0, "value": None})


def _api_url(repo: str) -> str:
    return "https://api.github.com/repos/{}/releases?per_page={}".format(repo, _HISTORY_DEPTH)


def _fetch_raw(repo: str, timeout: float) -> list:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "peliarch-downloads",
    }
    if DOWNLOADS_GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + DOWNLOADS_GITHUB_TOKEN
    req = urllib.request.Request(_api_url(repo), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_channels(timeout: float, url: str = None) -> str:
    req = urllib.request.Request(url or CHANNELS_RAW_URL,
                                 headers={"User-Agent": "peliarch-downloads"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _current_channels(text: str) -> dict[str, str]:
    """Last append-only row wins, matching er-archipelago's check_channels.py."""
    current = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            current[parts[0].strip()] = parts[1].strip()
    return current


def _resolve(payload: list, stable_tag: str = None, wanted=None) -> Releases:
    """Turn the API payload into one release plus what it carries.

    THE RESOLVED RELEASE IS THE ONE THE LEDGER POINTS AT -- chosen once, for all assets. Per-asset
    resolution is the tempting alternative and it is the bug: it would happily hand a v0.3.11
    bundle and a v0.3.10 apworld to the same visitor under one heading.

    THE PRERELEASE FLAG IS NOT THE POINTER; THE LEDGER IS. This used to drop every prerelease
    before looking for the ledger's tag, and that was fine while the only game published stable
    tags. Bloodborne publishes `v0.1.0-beta.N` and marks all of them prerelease, so the old filter
    resolved NOTHING for it -- a downloads section that could never render a build that exists. A
    tag NAMED by `release/CHANNELS.tsv` is accepted whatever GitHub's checkbox says; promotion is
    a reviewed commit to an append-only file, which is a stronger claim than the flag. Drafts stay
    excluded: a draft is not published at all and its assets are not fetchable.

    Elden Ring is unaffected, because its `stable` rows have never named a prerelease -- and the
    asset-history walk below only looks at releases with the SAME prerelease flag as the selected
    one, so an ER `-rc1` still cannot become the "last seen" tag on an ER gap note.
    """
    if not isinstance(payload, list):
        return Releases(ok=False)

    wanted = games.ER.wanted if wanted is None else wanted
    non_draft = [r for r in payload if isinstance(r, dict) and not r.get("draft")]
    # The API returns newest-first by creation, but sorting on published_at makes that an assertion
    # rather than a hope -- a re-published tag reorders the former and not the latter.
    non_draft.sort(key=lambda r: r.get("published_at") or "", reverse=True)

    if stable_tag:
        selected = next((r for r in non_draft if r.get("tag_name") == stable_tag), None)
        if selected is None:
            return Releases(ok=False)
        # A promoted stable release may trail newer version tags. Asset history must look backward
        # from stable, never forward into an unpromoted build.
        flag = bool(selected.get("prerelease"))
        published = [selected] + [
            r for r in non_draft
            if bool(r.get("prerelease")) == flag
            and (r.get("published_at") or "") < (selected.get("published_at") or "")
        ]
    else:
        # No pointer to follow: fall back to the newest full release. A prerelease nobody promoted
        # cannot stand in for one.
        published = [r for r in non_draft if not r.get("prerelease")]
        if not published:
            return Releases(ok=False)
    newest = published[0]

    def _named(release, match):
        for a in release.get("assets") or []:
            name = a.get("name") or ""
            if match(name):
                return a
        return None

    assets = {}
    for kind, match in wanted:
        hit = _named(newest, match)
        if hit:
            assets[kind] = Asset(
                kind=kind,
                filename=hit.get("name"),
                url=hit.get("browser_download_url"),
                size_bytes=int(hit.get("size") or 0),
            )
            continue

        # Missing here. Look back only far enough to say WHERE it last appeared, so the page can
        # describe the gap with a tag on it instead of pretending the asset does not exist.
        prior_tag = prior_url = None
        for older in published[1:]:
            hit = _named(older, match)
            if hit:
                prior_tag = older.get("tag_name")
                prior_url = hit.get("browser_download_url")
                break
        assets[kind] = Asset(kind=kind, last_seen_tag=prior_tag, last_seen_url=prior_url)

    return Releases(
        ok=True,
        tag=newest.get("tag_name"),
        published_at=newest.get("published_at"),
        html_url=newest.get("html_url"),
        assets=assets,
    )


def _resolve_dev(payload: list, wanted=None) -> Releases:
    """Resolve only the moving `dev` prerelease; never mistake another prerelease for the channel."""
    if not isinstance(payload, list):
        return Releases(ok=False)
    candidates = [r for r in payload if isinstance(r, dict) and not r.get("draft")
                  and r.get("prerelease") and r.get("tag_name") == "dev"]
    if not candidates:
        return Releases(ok=False)
    return _resolve_one(candidates[0], wanted)


def _resolve_one(release: dict, wanted=None) -> Releases:
    """One already-selected release, with no cross-release asset fallback."""
    wanted = games.ER.wanted if wanted is None else wanted
    assets = {}
    for kind, match in wanted:
        hit = next((a for a in release.get("assets") or [] if match(a.get("name") or "")), None)
        assets[kind] = Asset(
            kind=kind,
            filename=hit.get("name") if hit else None,
            url=hit.get("browser_download_url") if hit else None,
            size_bytes=int(hit.get("size") or 0) if hit else 0,
        )
    return Releases(
        ok=True,
        tag=release.get("tag_name"),
        published_at=release.get("published_at"),
        html_url=release.get("html_url"),
        assets=assets,
    )


def _game(game):
    """Accept a Game, a game key, or None (Elden Ring -- every caller that predates the table)."""
    if game is None:
        return games.GAMES[games.DEFAULT_GAME]
    if isinstance(game, str):
        return games.GAMES[game]
    return game


def get_releases(game=None, repo: str = None, ttl: int = None, timeout: float = None,
                 force: bool = False) -> Releases:
    """Resolved stable release for one game. Never raises.

    A stale cached value beats a failed fetch: if GitHub is down and we already have an answer, the
    page keeps working with a slightly old version number, which is a far better failure than the
    bare fallback. Only a cold cache plus a failed fetch degrades -- and the cache is keyed by
    game, so "we already have an answer" can never quietly mean "for the other game".
    """
    game = _game(game)
    repo = repo or game.downloads_repo
    ttl = DOWNLOADS_TTL_SECONDS if ttl is None else ttl
    timeout = DOWNLOADS_TIMEOUT_SECONDS if timeout is None else timeout

    with _lock:
        slot = _slot(game.key, "stable")
        cached = slot["value"]
        fresh = cached is not None and (time.time() - slot["at"]) < ttl
        if fresh and not force:
            return cached

    try:
        channels = _current_channels(_fetch_channels(timeout, game.channels_raw_url))
        stable_tag = channels.get("stable")
        if not stable_tag:
            raise ValueError("channel ledger has no stable pointer")
        resolved = _resolve(_fetch_raw(repo, timeout), stable_tag=stable_tag, wanted=game.wanted)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TypeError, KeyError) as exc:
        logger.warning("downloads: %s release fetch failed (%s: %s)",
                       game.key, type(exc).__name__, exc)
        with _lock:
            # Serve the stale answer if we have one; only a cold cache degrades.
            return _slot(game.key, "stable")["value"] or Releases(ok=False)

    if not resolved.ok:
        with _lock:
            return _slot(game.key, "stable")["value"] or resolved

    with _lock:
        slot = _slot(game.key, "stable")
        slot["at"] = time.time()
        slot["value"] = resolved
    return resolved


def get_dev_release(game=None, repo: str = None, ttl: int = None, timeout: float = None,
                    force: bool = False) -> Releases:
    """The moving development prerelease. Never raises and never falls back to a stable asset.

    A game with no rolling `dev` release (Bloodborne) never reaches here: `app.downloads` passes
    `dev=None` for it and the template omits the card. An empty "Development build" section is a
    promise the project has not made.
    """
    game = _game(game)
    repo = repo or game.downloads_repo
    ttl = DOWNLOADS_TTL_SECONDS if ttl is None else ttl
    timeout = DOWNLOADS_TIMEOUT_SECONDS if timeout is None else timeout
    with _lock:
        slot = _slot(game.key, "dev")
        cached = slot["value"]
        if cached is not None and (time.time() - slot["at"]) < ttl and not force:
            return cached
    try:
        channels = _current_channels(_fetch_channels(timeout, game.channels_raw_url))
        if channels.get("beta") != "main":
            return Releases(ok=False)
        resolved = _resolve_dev(_fetch_raw(repo, timeout), wanted=game.wanted)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TypeError, KeyError) as exc:
        logger.warning("downloads: %s dev release fetch failed (%s: %s)",
                       game.key, type(exc).__name__, exc)
        with _lock:
            return _slot(game.key, "dev")["value"] or Releases(ok=False)
    with _lock:
        slot = _slot(game.key, "dev")
        slot["at"] = time.time()
        slot["value"] = resolved
    return resolved


def reset_cache() -> None:
    """Drop every cached release, for every game and channel. For tests, and an admin poke."""
    with _lock:
        _cache.clear()
