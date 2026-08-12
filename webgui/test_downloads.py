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

from webgui import releases
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


@pytest.fixture(autouse=True)
def _clean_cache():
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

    def test_both_assets_come_from_one_tag(self, stub):
        """The invariant. Not 'both are present' -- both are present FROM THE SAME RELEASE."""
        stub(COMPLETE)
        rel = releases.get_releases()
        bundle, apworld = rel.asset("bundle"), rel.asset("apworld")
        assert bundle.available and apworld.available
        assert "v0.3.10" in bundle.url
        # The bundle names its version; the apworld does not, so the tag in its URL is the check.
        assert "/download/" in apworld.url

    def test_a_missing_asset_is_not_backfilled_from_an_older_tag(self, stub):
        """v0.3.11 has no apworld. The resolved asset must be UNAVAILABLE, not silently v0.3.10.

        Backfilling is the tempting behaviour and it is the bug: it hands a visitor a v0.3.11
        bundle and a v0.3.10 apworld under one 'latest release' heading.
        """
        stub(APWORLD_MISSING)
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

    def test_a_missing_apworld_is_labelled_with_its_older_tag(self, client):
        """The older-tag link must never appear without the older tag written on it."""
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
