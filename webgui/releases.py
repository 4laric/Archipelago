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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

#: `owner/repo` the release assets come from. The world repo, not this one -- peliarch hosts rooms,
#: er-archipelago publishes the game.
DOWNLOADS_REPO = os.environ.get("DOWNLOADS_REPO", "4laric/er-archipelago")

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

#: Where the project is published outside GitHub. The Nexus page is a LINK-ONLY storefront by
#: policy (`DISTRIBUTION.md`: "No mirrors"), so it is a destination here, never a download target.
NEXUS_URL = os.environ.get("NEXUS_URL", "https://www.nexusmods.com/eldenring/mods/10334")

#: The world repo's human-facing home. Distinct from CONTACT_GITHUB in app.py, which points at
#: peliarch's own source -- a visitor looking for the game and a visitor looking for the host are
#: two different people and they are not owed the same link.
GAME_GITHUB_URL = os.environ.get("GAME_GITHUB_URL", "https://github.com/4laric/er-archipelago")

#: How many releases back to look when answering "where was this asset last published". Only used
#: for the honest-gap note; it never promotes an older tag into a download button on its own.
_HISTORY_DEPTH = 10

#: The two published assets, in the order `DISTRIBUTION.md` lists them.
#: `match` is a predicate on the asset filename because the bundle carries its version in its name.
_WANTED = (
    ("bundle", lambda n: n.startswith("ER-Archipelago-") and n.endswith(".zip")),
    ("apworld", lambda n: n == "eldenring.apworld"),
)


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
_cache: dict = {"at": 0.0, "value": None}


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


def _resolve(payload: list) -> Releases:
    """Turn the API payload into one release plus what it carries.

    THE RESOLVED RELEASE IS THE NEWEST NON-DRAFT, NON-PRERELEASE ONE -- chosen once, for all assets.
    Per-asset resolution is the tempting alternative and it is the bug: it would happily hand a
    v0.3.11 bundle and a v0.3.10 apworld to the same visitor under one heading.
    """
    if not isinstance(payload, list):
        return Releases(ok=False)

    published = [
        r for r in payload
        if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
    ]
    if not published:
        return Releases(ok=False)

    # The API returns newest-first by creation, but sorting on published_at makes that an assertion
    # rather than a hope -- a re-published tag reorders the former and not the latter.
    published.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    newest = published[0]

    def _named(release, match):
        for a in release.get("assets") or []:
            name = a.get("name") or ""
            if match(name):
                return a
        return None

    assets = {}
    for kind, match in _WANTED:
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


def get_releases(repo: str = None, ttl: int = None, timeout: float = None,
                 force: bool = False) -> Releases:
    """Resolved release for the downloads page. Never raises.

    A stale cached value beats a failed fetch: if GitHub is down and we already have an answer, the
    page keeps working with a slightly old version number, which is a far better failure than the
    bare fallback. Only a cold cache plus a failed fetch degrades.
    """
    repo = repo or DOWNLOADS_REPO
    ttl = DOWNLOADS_TTL_SECONDS if ttl is None else ttl
    timeout = DOWNLOADS_TIMEOUT_SECONDS if timeout is None else timeout

    with _lock:
        cached = _cache["value"]
        fresh = cached is not None and (time.time() - _cache["at"]) < ttl
        if fresh and not force:
            return cached

    try:
        resolved = _resolve(_fetch_raw(repo, timeout))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TypeError, KeyError) as exc:
        logger.warning("downloads: release fetch failed (%s: %s)", type(exc).__name__, exc)
        with _lock:
            # Serve the stale answer if we have one; only a cold cache degrades.
            return _cache["value"] or Releases(ok=False)

    if not resolved.ok:
        with _lock:
            return _cache["value"] or resolved

    with _lock:
        _cache["at"] = time.time()
        _cache["value"] = resolved
    return resolved


def reset_cache() -> None:
    """Drop the cached release. For tests, and for a future admin poke."""
    with _lock:
        _cache["at"] = 0.0
        _cache["value"] = None
