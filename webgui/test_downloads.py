"""
test_downloads.py -- the Downloads page and the release resolver behind it.

The load-bearing behaviour under test is NOT "does it render a link". It is that the page never
presents assets from two different tags as one download, and never renders a download URL it cannot
vouch for. Both of those are `release/DISTRIBUTION.md` invariants: a mismatched apworld/client pair
connects and then misbehaves quietly, which is the failure mode packaging exists to prevent.

Every test here drives the resolver on a FIXTURE payload. Nothing hits the network -- a test whose
verdict depends on GitHub being up is testing GitHub.
"""

from __future__ import annotations

import os
import sys
import urllib.error

import pytest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from webgui import games, releases
from webgui import app as app_module
from webgui.app import create_app
from webgui.test_app import MockManager


# ---------------------------------------------------------------------------
# Fixtures: payloads shaped like the real GitHub Releases API
# ---------------------------------------------------------------------------

def _asset(name, size=1000):
    return {
        "name": name,
        "size": size,
        "browser_download_url": f"https://github.com/4laric/er-archipelago/releases/download/X/{name}",
    }


def _release(tag, published, assets, draft=False, prerelease=False):
    return {
        "tag_name": tag,
        "published_at": published,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/4laric/er-archipelago/releases/tag/{tag}",
        "assets": assets,
    }


#: Both assets on the newest tag -- the healthy shape DISTRIBUTION.md describes.
COMPLETE = [
    _release("v0.3.10", "2026-08-10T03:00:16Z",
             [_asset("ER-Archipelago-v0.3.10.zip", 123792609),
              _asset("eldenring.apworld", 1369262),
              _asset("er-options-wizard.html", 154649)]),
    _release("v0.3.9", "2026-08-09T00:33:38Z",
             [_asset("ER-Archipelago-v0.3.9.zip"), _asset("eldenring.apworld")]),
]

#: The REAL shape on 2026-08-12: v0.3.11 shipped the bundle and no bare apworld.
#: This is the fixture that matters -- it is the state the page had to be designed around.
APWORLD_MISSING = [
    _release("v0.3.11", "2026-08-12T00:33:30Z",
             [_asset("ER-Archipelago-v0.3.11.zip", 123937863)]),
    _release("v0.3.10", "2026-08-10T03:00:16Z",
             [_asset("ER-Archipelago-v0.3.10.zip"), _asset("eldenring.apworld", 1369262)]),
]

DEV = _release(
    "dev", "2026-08-17T12:00:00Z",
    [_asset("ER-Archipelago-dev.zip", 120000000), _asset("eldenring-dev.apworld", 1400000)],
    prerelease=True,
)
CHANNELS = "stable\tv0.3.10\t2026-08-10\n" "beta\tmain\t2026-08-11\n"


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.setattr(releases, "_fetch_channels", lambda timeout, url=None: CHANNELS)
    releases.reset_cache()
    yield
    releases.reset_cache()


@pytest.fixture
def stub(monkeypatch):
    """Point the resolver at a fixture payload instead of the network."""
    def _install(payload):
        def fake(repo, timeout):
            return payload
        monkeypatch.setattr(releases, "_fetch_raw", fake)
    return _install


@pytest.fixture
def client(stub):
    def _build(payload=COMPLETE):
        stub(payload)
        app = create_app(manager=MockManager())
        app.config["TESTING"] = True
        return app.test_client()
    return _build


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

class TestResolve:

    def test_picks_the_newest_published_release(self, stub):
        stub(COMPLETE)
        rel = releases.get_releases()
        assert rel.ok
        assert rel.tag == "v0.3.10"

    def test_stable_follows_the_ledger_not_a_newer_unpromoted_tag(self, stub, monkeypatch):
        newer = _release("v0.3.11", "2026-08-11T03:00:16Z",
                         [_asset("ER-Archipelago-v0.3.11.zip"), _asset("eldenring.apworld")])
        stub([newer] + COMPLETE)
        monkeypatch.setattr(releases, "_fetch_channels", lambda timeout, url=None: CHANNELS)
        assert releases.get_releases().tag == "v0.3.10"

    def test_both_assets_come_from_one_tag(self, stub):
        """The invariant. Not 'both are present' -- both are present FROM THE SAME RELEASE."""
        stub(COMPLETE)
        rel = releases.get_releases()
        bundle, apworld = rel.asset("bundle"), rel.asset("apworld")
        assert bundle.available and apworld.available
        assert "v0.3.10" in bundle.url
        # The bundle names its version; the apworld does not, so the tag in its URL is the check.
        assert "/download/" in apworld.url

    def test_a_missing_asset_is_not_backfilled_from_an_older_tag(self, stub, monkeypatch):
        """v0.3.11 has no apworld. The resolved asset must be UNAVAILABLE, not silently v0.3.10.

        Backfilling is the tempting behaviour and it is the bug: it hands a visitor a v0.3.11
        bundle and a v0.3.10 apworld under one 'latest release' heading.
        """
        stub(APWORLD_MISSING)
        monkeypatch.setattr(releases, "_fetch_channels",
                            lambda timeout, url=None: "stable\tv0.3.11\t2026-08-12\nbeta\tmain\t2026-08-12\n")
        rel = releases.get_releases()
        assert rel.tag == "v0.3.11"
        apworld = rel.asset("apworld")
        assert not apworld.available
        assert apworld.url is None
        # It still knows where the last one was -- for a LABELLED link, not a silent substitution.
        assert apworld.last_seen_tag == "v0.3.10"
        assert apworld.last_seen_url is not None

    def test_drafts_and_prereleases_are_ignored(self, stub):
        stub([_release("v0.4.0-rc1", "2026-08-13T00:00:00Z", [_asset("ER-Archipelago-v0.4.0.zip")],
                       prerelease=True),
              _release("v0.3.99", "2026-08-13T00:00:00Z", [], draft=True)] + COMPLETE)
        assert releases.get_releases().tag == "v0.3.10"

    def test_dev_is_a_named_prerelease_channel_not_the_latest_stable(self, stub):
        stub([DEV] + COMPLETE)
        assert releases.get_releases().tag == "v0.3.10"
        dev = releases.get_dev_release()
        assert dev.ok and dev.tag == "dev"
        assert dev.asset("bundle").filename == "ER-Archipelago-dev.zip"
        assert dev.asset("apworld").filename == "eldenring-dev.apworld"

    def test_an_arbitrary_prerelease_is_not_the_dev_channel(self, stub):
        stub([_release("v0.4.0-rc1", "2026-08-17T12:00:00Z",
                       [_asset("ER-Archipelago-v0.4.0-rc1.zip")], prerelease=True)])
        assert not releases.get_dev_release().ok

    def test_ordering_is_by_published_at_not_payload_order(self, stub):
        stub(list(reversed(COMPLETE)))
        assert releases.get_releases().tag == "v0.3.10"

    def test_wizard_html_is_not_offered_as_a_download(self, stub):
        """v0.3.10 attaches er-options-wizard.html. It is deploy input, not a player download."""
        stub(COMPLETE)
        rel = releases.get_releases()
        assert set(rel.assets) == {"bundle", "apworld"}

    def test_size_is_human_readable(self, stub):
        stub(COMPLETE)
        assert releases.get_releases().asset("bundle").size_human == "118 MB"
        assert releases.get_releases().asset("apworld").size_human == "1.3 MB"


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

class TestDegradation:

    def test_network_failure_on_a_cold_cache_degrades(self, monkeypatch):
        def boom(repo, timeout):
            raise urllib.error.URLError("no route to host")
        monkeypatch.setattr(releases, "_fetch_raw", boom)
        rel = releases.get_releases()
        assert rel.ok is False
        assert rel.tag is None

    def test_malformed_payload_degrades_rather_than_raising(self, stub):
        stub({"message": "API rate limit exceeded"})
        assert releases.get_releases().ok is False

    def test_empty_release_list_degrades(self, stub):
        stub([])
        assert releases.get_releases().ok is False

    def test_a_stale_answer_beats_a_failed_fetch(self, stub, monkeypatch):
        """Once we have an answer, a later outage must not blank the page."""
        stub(COMPLETE)
        assert releases.get_releases().tag == "v0.3.10"

        def boom(repo, timeout):
            raise urllib.error.URLError("github is down")
        monkeypatch.setattr(releases, "_fetch_raw", boom)
        rel = releases.get_releases(ttl=0)      # cache expired, fetch fails
        assert rel.ok and rel.tag == "v0.3.10"


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class TestCache:

    def test_a_second_call_inside_the_ttl_does_not_refetch(self, monkeypatch):
        calls = []

        def counted(repo, timeout):
            calls.append(1)
            return COMPLETE
        monkeypatch.setattr(releases, "_fetch_raw", counted)

        releases.get_releases()
        releases.get_releases()
        releases.get_releases()
        assert len(calls) == 1

    def test_an_expired_ttl_refetches(self, monkeypatch):
        calls = []

        def counted(repo, timeout):
            calls.append(1)
            return COMPLETE
        monkeypatch.setattr(releases, "_fetch_raw", counted)

        releases.get_releases()
        releases.get_releases(ttl=0)
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

class TestDownloadsPage:

    def test_renders(self, client):
        assert client().get("/downloads").status_code == 200

    def test_shows_the_tag_and_both_download_urls(self, client):
        html = client().get("/downloads").data.decode()
        assert "v0.3.10" in html
        assert "ER-Archipelago-v0.3.10.zip" in html
        assert "eldenring.apworld" in html

    def test_carries_the_pairing_warning(self, client):
        """The whole point of the section chosen over a bare link list."""
        html = client().get("/downloads").data.decode()
        assert "hash-matched pair" in html
        assert "VERSION MISMATCH" in html

    def test_links_nexus_and_github(self, client):
        html = client().get("/downloads").data.decode()
        assert releases.NEXUS_URL in html
        assert releases.GAME_GITHUB_URL in html

    def test_development_channel_is_separate_and_warns_about_the_icon(self, client):
        html = client([DEV] + COMPLETE).get("/downloads").data.decode()
        assert "Development build" in html
        assert "ER-Archipelago-dev.zip" in html
        assert "eldenring-dev.apworld" in html
        assert "Telescope icon" in html
        assert releases.CHANNELS_URL in html

    def test_a_missing_apworld_is_labelled_with_its_older_tag(self, client, monkeypatch):
        """The older-tag link must never appear without the older tag written on it."""
        monkeypatch.setattr(releases, "_fetch_channels",
                            lambda timeout, url=None: "stable\tv0.3.11\t2026-08-12\nbeta\tmain\t2026-08-12\n")
        html = client(APWORLD_MISSING).get("/downloads").data.decode()
        assert "did not publish a bare apworld" in html
        # The offered fallback is present AND named.
        assert "Download eldenring.apworld from v0.3.10" in html

    def test_degraded_page_offers_no_download_url(self, monkeypatch):
        def boom(repo, timeout):
            raise urllib.error.URLError("down")
        monkeypatch.setattr(releases, "_fetch_raw", boom)
        app = create_app(manager=MockManager())
        app.config["TESTING"] = True
        html = app.test_client().get("/downloads").data.decode()
        assert "releases" in html
        # A dead download button is worse than no button.
        assert "/releases/download/" not in html

    def test_nav_and_dashboard_both_reach_the_page(self, client):
        c = client()
        assert 'href="/downloads"' in c.get("/downloads").data.decode()   # header nav
        assert 'href="/downloads"' in c.get("/").data.decode()            # dashboard teaser


# ---------------------------------------------------------------------------
# V.R.M.F fixpack tags. The ledger may promote `stable` to a FOUR-segment tag (`v0.6.0.2`), and
# the bundle then carries that spelling in its filename. The resolver matches the ledger pointer
# exactly, so the only thing that can break here is a tag-shaped assumption elsewhere.
# ---------------------------------------------------------------------------

FIXPACK = [
    _release("v0.6.0.2", "2026-09-07T09:00:00Z",
             [_asset("ER-Archipelago-v0.6.0.2.zip", 123900000),
              _asset("eldenring.apworld", 1400000)]),
    _release("v0.6.0.1", "2026-09-07T01:00:00Z",
             [_asset("ER-Archipelago-v0.6.0.1.zip"), _asset("eldenring.apworld")]),
    _release("v0.6.0", "2026-09-06T00:00:00Z",
             [_asset("ER-Archipelago-v0.6.0.zip"), _asset("eldenring.apworld")]),
    _release("v0.5.7", "2026-09-02T00:00:00Z",
             [_asset("ER-Archipelago-v0.5.7.zip"), _asset("eldenring.apworld")]),
]

FIXPACK_CHANNELS = "stable\tv0.6.0\t2026-09-06\nstable\tv0.6.0.2\t2026-09-07\nbeta\tmain\t2026-09-07\n"


class TestFixpackTags:

    def test_stable_resolves_a_four_segment_tag(self, stub, monkeypatch):
        stub(FIXPACK)
        monkeypatch.setattr(releases, "_fetch_channels", lambda timeout, url=None: FIXPACK_CHANNELS)
        rel = releases.get_releases()
        assert rel.ok and rel.tag == "v0.6.0.2"
        assert rel.asset("bundle").filename == "ER-Archipelago-v0.6.0.2.zip"
        assert rel.asset("apworld").available

    def test_a_fixpack_line_orders_after_its_base_release(self, stub, monkeypatch):
        """v0.6.0.2 > v0.6.0.1 > v0.6.0 > v0.5.7. Resolution is by published_at, so a lexical
        comparison of the tags never happens -- pin that, because '0.6.0.2' < '0.6.0' is false
        lexically but 'v0.6.0.10' < 'v0.6.0.2' is true, and a sort on the string would be wrong."""
        stub(list(reversed(FIXPACK)))
        monkeypatch.setattr(releases, "_fetch_channels", lambda timeout, url=None: FIXPACK_CHANNELS)
        assert releases.get_releases().tag == "v0.6.0.2"

        def key(tag):
            return tuple(int(part) for part in tag.lstrip("v").split("."))

        ordered = sorted(["v0.6.0", "v0.5.7", "v0.6.0.10", "v0.6.0.2"], key=key)
        assert ordered == ["v0.5.7", "v0.6.0", "v0.6.0.2", "v0.6.0.10"]

    def test_older_base_release_is_still_a_labelled_fallback(self, stub, monkeypatch):
        """A fixpack that shipped only the bundle still names where the apworld last was."""
        no_apworld = [_release("v0.6.0.2", "2026-09-07T09:00:00Z",
                               [_asset("ER-Archipelago-v0.6.0.2.zip")])] + FIXPACK[1:]
        stub(no_apworld)
        monkeypatch.setattr(releases, "_fetch_channels", lambda timeout, url=None: FIXPACK_CHANNELS)
        rel = releases.get_releases()
        assert rel.tag == "v0.6.0.2"
        assert not rel.asset("apworld").available
        assert rel.asset("apworld").last_seen_tag == "v0.6.0.1"


# ---------------------------------------------------------------------------
# The second game. Bloodborne publishes every build as a GitHub PRERELEASE, has no rolling `dev`
# release, and shares this module's resolver with Elden Ring -- three ways the single-game code
# could have hidden it, all three pinned here.
# ---------------------------------------------------------------------------

def _bb_asset(name, size=1000):
    return {
        "name": name,
        "size": size,
        "browser_download_url":
            "https://github.com/4laric/bb-archipelago/releases/download/X/" + name,
    }


#: The real shape on 2026-09-07: three beta tags, every one of them flagged prerelease.
BB_RELEASES = [
    _release("v0.1.0-beta.5", "2026-09-05T10:00:00Z",
             [_bb_asset("BloodborneAPLauncher-win-x64.zip", 48000000),
              _bb_asset("bloodborne.apworld", 900000)], prerelease=True),
    _release("v0.1.0-beta.4", "2026-08-30T10:00:00Z",
             [_bb_asset("BloodborneAPLauncher-win-x64.zip"),
              _bb_asset("bloodborne.apworld")], prerelease=True),
]

BB_CHANNELS = "stable\tv0.1.0-beta.5\t2026-09-05\nbeta\tmain\t2026-09-05\n"


@pytest.fixture
def bb_stub(monkeypatch):
    """Both ledgers and both release lists, chosen by the URL and the repo the caller asks for."""
    def _channels(timeout, url=None):
        return BB_CHANNELS if url and "bb-archipelago" in url else CHANNELS
    def _raw(repo, timeout):
        return BB_RELEASES if "bb-archipelago" in repo else COMPLETE
    monkeypatch.setattr(releases, "_fetch_channels", _channels)
    monkeypatch.setattr(releases, "_fetch_raw", _raw)


class TestBloodborneDownloads:

    def test_stable_pointer_accepts_a_prerelease_tag(self, bb_stub):
        """🛑 THE LEDGER IS THE POINTER; GITHUB'S CHECKBOX IS NOT.

        The resolver used to drop every prerelease before looking for the ledger's tag. Every
        Bloodborne release is a prerelease, so under the old rule the Bloodborne section could
        never render a build that exists -- the page would have been permanently degraded against
        a repo that was publishing normally. Promotion is a reviewed commit to an append-only
        file; that is a stronger claim than a checkbox in the GitHub UI.
        """
        rel = releases.get_releases(games.BB)
        assert rel.ok
        assert rel.tag == "v0.1.0-beta.5"
        assert rel.asset("bundle").filename == "BloodborneAPLauncher-win-x64.zip"
        assert rel.asset("apworld").filename == "bloodborne.apworld"

    def test_elden_ring_still_ignores_unpromoted_prereleases(self, bb_stub):
        """The other half of the change: the flag stopped being a FILTER, not a fact."""
        assert releases.get_releases(games.ER).tag == "v0.3.10"

    def test_dev_block_hidden_without_a_dev_release(self, bb_stub, tmp_path, monkeypatch):
        """Bloodborne ships no rolling `dev` build, so there is no card -- not an empty one.

        `dev.ok == False` means "today's development build failed", which the card says out loud.
        "This game has no development channel" is a different statement and the honest rendering
        of it is silence.
        """
        bb = tmp_path / "bb"
        bb.mkdir()
        monkeypatch.setattr(app_module, "BB_STATIC_DIR", str(bb))
        app = create_app(manager=MockManager())
        app.config["TESTING"] = True
        html = app.test_client().get("/downloads/bb").data.decode()

        assert "v0.1.0-beta.5" in html
        assert "BloodborneAPLauncher-win-x64.zip" in html
        assert "Development build" not in html
        assert "No development bundle has completed successfully yet" not in html
        assert "There is no rolling development build for" in html

        # ...and the game that DOES have one keeps it.
        assert "Development build" in app.test_client().get("/downloads/er").data.decode()

    def test_the_combined_page_carries_both_games(self, bb_stub, tmp_path, monkeypatch):
        bb = tmp_path / "bb"
        bb.mkdir()
        monkeypatch.setattr(app_module, "BB_STATIC_DIR", str(bb))
        html = create_app(manager=MockManager()).test_client().get("/downloads").data.decode()
        assert 'data-testid="downloads-er"' in html
        assert 'data-testid="downloads-bb"' in html
        # No Nexus row for a game that has no Nexus page.
        assert "nexusmods.com/bloodborne" not in html

    def test_an_undeployed_second_game_is_not_fetched_or_rendered(self, monkeypatch):
        """Until rollout step 3 there is no BB_STATIC_DIR, and /downloads must not grow two
        four-second GitHub calls in its request path for a section it will not render."""
        asked = []

        def _raw(repo, timeout):
            asked.append(repo)
            return COMPLETE
        monkeypatch.setattr(releases, "_fetch_raw", _raw)
        monkeypatch.setattr(app_module, "BB_STATIC_DIR", "")
        html = create_app(manager=MockManager()).test_client().get("/downloads").data.decode()
        assert 'data-testid="downloads-bb"' not in html
        assert not any("bb-archipelago" in r for r in asked)


class TestPerGameReleaseCache:
    """One cache keyed by (game, channel). Two single-slot globals would have crossed the games.

    🛑 THE FAILURE THIS PREVENTS IS NOT "a stale version number". It is serving one game's release
    under the other game's heading -- the same class of defect as the mismatched apworld/client
    pair this whole module exists to prevent, one level up.
    """

    def test_er_failure_returns_er_stale_not_bb(self, bb_stub, monkeypatch):
        assert releases.get_releases(games.ER).tag == "v0.3.10"
        assert releases.get_releases(games.BB).tag == "v0.1.0-beta.5"

        def boom(repo, timeout):
            if "bb-archipelago" in repo:
                return BB_RELEASES
            raise urllib.error.URLError("er github is down")
        monkeypatch.setattr(releases, "_fetch_raw", boom)

        er = releases.get_releases(games.ER, ttl=0)
        assert er.ok and er.tag == "v0.3.10", "an ER outage must serve ER's own stale answer"
        assert releases.get_releases(games.BB, ttl=0).tag == "v0.1.0-beta.5"

    def test_a_cold_er_cache_does_not_borrow_bbs_answer(self, bb_stub, monkeypatch):
        assert releases.get_releases(games.BB).tag == "v0.1.0-beta.5"

        def boom(repo, timeout):
            raise urllib.error.URLError("down")
        monkeypatch.setattr(releases, "_fetch_raw", boom)
        assert releases.get_releases(games.ER).ok is False

    def test_reset_cache_clears_every_game(self, bb_stub):
        releases.get_releases(games.ER)
        releases.get_releases(games.BB)
        releases.reset_cache()
        assert releases._cache == {}
