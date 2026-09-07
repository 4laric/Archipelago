"""
games.py -- the table that turned this site from "Elden Ring for Archipelago" into a host for more
than one game.

WHY A TABLE AND NOT A SECOND SITE. Every surface here -- the landing page, the yaml builder, the
check browser, the bug-report form, /downloads, /hosting -- was written once for Elden Ring and
then hardcoded to it in five separate places: a static directory, a downloads repo, a channel
ledger URL, two asset-name matchers, and a tab strip. Adding Bloodborne by copying those five
would have made every one of them a pair that drifts, which is the exact failure
`SPEC-publishing-pipeline.md` was written about upstream. So they are ONE row per game here, and
the routes, the templates and the release resolver read the row.

WHAT IS DELIBERATELY NOT IN THIS TABLE. The static directory itself. It stays as
`ER_STATIC_DIR` / `BB_STATIC_DIR` module globals in `app.py`, read at REQUEST time, because a
deploy can land mid-process and because that is the attribute the whole test suite monkeypatches.
A frozen dataclass field would capture the value at import and quietly answer with yesterday's
truth. The row names the env var (`static_dir_env`); `app.py` resolves it per request and wraps
the row in a per-request view (`app.GameView`) that knows `available` and `has(filename)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Tab:
    """One extra, game-specific tab in the strip.

    `artifact` is the filename that must exist in the game's static dir for the tab to render. A
    tab that 404s is worse than a tab that is not there -- the ER questline DAG is optional at
    older refs and this is how it stays optional.
    """

    label: str
    path: str
    key: str
    artifact: str


@dataclass(frozen=True)
class Game:
    """One game the site serves. See the module docstring for what is NOT here."""

    key: str                    #: URL root and static-dir key: "er" | "bb"
    name: str                   #: "Elden Ring" | "Bloodborne"
    short: str                  #: switcher label: "ER" | "BB"
    static_dir_env: str         #: name of the app.py module global holding the directory
    #: File served for a bare `/<key>/`. ER's front door into the tooling has always been the
    #: builder and moving it would break every link ever posted; Bloodborne starts at its landing.
    default_file: str
    #: Path under the game root for the Builder tab. Empty for ER, because `/er/` IS the builder.
    builder_path: str
    downloads_repo: str
    channels_url: str
    channels_raw_url: str
    #: Asset matchers, in the order the downloads page lists them. A predicate rather than a name
    #: because the ER bundle carries its version in its filename.
    wanted: tuple[tuple[str, Callable[[str], bool]], ...]
    github_url: str
    nexus_url: Optional[str]
    extra_tabs: tuple[Tab, ...]
    #: Whether this game publishes a rolling `dev` prerelease. Bloodborne does not, and an empty
    #: "Development build" card is a promise the project has not made.
    has_dev_channel: bool

    @property
    def root(self) -> str:
        return f"/{self.key}/"

    @property
    def builder_url(self) -> str:
        return self.root + self.builder_path

    @property
    def releases_index(self) -> str:
        return self.github_url.rstrip("/") + "/releases"


_ER_REPO = os.environ.get("DOWNLOADS_REPO", "4laric/er-archipelago")
_BB_REPO = os.environ.get("BB_DOWNLOADS_REPO", "4laric/bb-archipelago")

ER = Game(
    key="er",
    name="Elden Ring",
    short="ER",
    static_dir_env="ER_STATIC_DIR",
    default_file="wizard.html",
    builder_path="",
    downloads_repo=_ER_REPO,
    channels_url=os.environ.get(
        "DOWNLOADS_CHANNELS_URL",
        f"https://github.com/{_ER_REPO}/blob/main/release/CHANNELS.tsv"),
    channels_raw_url=os.environ.get(
        "DOWNLOADS_CHANNELS_RAW_URL",
        f"https://raw.githubusercontent.com/{_ER_REPO}/main/release/CHANNELS.tsv"),
    wanted=(
        ("bundle", lambda n: n.startswith("ER-Archipelago-") and n.endswith(".zip")),
        ("apworld", lambda n: n in ("eldenring.apworld", "eldenring-dev.apworld")),
    ),
    github_url=os.environ.get("GAME_GITHUB_URL", "https://github.com/4laric/er-archipelago"),
    nexus_url=os.environ.get("NEXUS_URL", "https://www.nexusmods.com/eldenring/mods/10334"),
    extra_tabs=(Tab(label="Questlines", path="questlines.html", key="questlines",
                    artifact="questlines.html"),),
    has_dev_channel=True,
)

#: Bloodborne runs under shadPS4. There is no Nexus page and there is no questline DAG, so both
#: are absent rather than empty -- see SPEC-peliarch-bloodborne.md §1 "Not in scope".
BB = Game(
    key="bb",
    name="Bloodborne",
    short="BB",
    static_dir_env="BB_STATIC_DIR",
    default_file="landing.html",
    builder_path="wizard.html",
    downloads_repo=_BB_REPO,
    channels_url=os.environ.get(
        "BB_DOWNLOADS_CHANNELS_URL",
        f"https://github.com/{_BB_REPO}/blob/main/release/CHANNELS.tsv"),
    channels_raw_url=os.environ.get(
        "BB_DOWNLOADS_CHANNELS_RAW_URL",
        f"https://raw.githubusercontent.com/{_BB_REPO}/main/release/CHANNELS.tsv"),
    #: Exact names, not prefixes: bb-archipelago ships one zip and one apworld per release and
    #: neither carries its version in the filename.
    wanted=(
        ("bundle", lambda n: n == "BloodborneAPLauncher-win-x64.zip"),
        ("apworld", lambda n: n == "bloodborne.apworld"),
    ),
    github_url=os.environ.get("BB_GAME_GITHUB_URL", "https://github.com/4laric/bb-archipelago"),
    nexus_url=None,
    extra_tabs=(),
    has_dev_channel=False,
)

#: Insertion order is display order: the switcher, the downloads page and the hosting cards all
#: iterate this.
GAMES: dict[str, Game] = {g.key: g for g in (ER, BB)}

#: The game a shared page (/downloads, /hosting, /room/<id>) belongs to when it belongs to none.
#: Elden Ring, so an ER visitor's tab strip is what it has always been.
DEFAULT_GAME = "er"
