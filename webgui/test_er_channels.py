from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_channels.py"
SPEC = spec_from_file_location("check_channels", SCRIPT)
assert SPEC and SPEC.loader
channels = module_from_spec(SPEC)
SPEC.loader.exec_module(channels)

LEDGER = b"stable\tv0.4.6\t2026-08-17\trelease\nbeta\tmain\t2026-08-18\tdevelopment\n"
STABLE = b'<script>{"apworld_version": "0.4.6"}</script>'
BETA = b'<script>{"apworld_version": "0.4.8"}</script>'
DOWNLOADS = b'/releases/download/v0.4.6/ER-Archipelago-v0.4.6.zip'


def fixture(live_stable=STABLE, live_beta=BETA, downloads=DOWNLOADS):
    values = {
        "https://raw.githubusercontent.com/4laric/er-archipelago/main/release/CHANNELS.tsv": LEDGER,
        "https://raw.githubusercontent.com/4laric/er-archipelago/v0.4.6/wizard/wizard.html": STABLE,
        "https://raw.githubusercontent.com/4laric/er-archipelago/main/wizard/wizard.html": BETA,
        "https://example.test/er/wizard.html": live_stable,
        "https://example.test/er/beta/wizard.html": live_beta,
        "https://example.test/downloads": downloads,
    }
    return values.__getitem__


def test_matching_channels_pass():
    assert channels.verify(fixture(), "https://example.test") == []


def test_main_leaking_into_stable_fails():
    errors = channels.verify(fixture(live_stable=BETA), "https://example.test")
    assert any("stable wizard is not v0.4.6" in error for error in errors)
    assert any("byte-identical" in error for error in errors)


def test_download_pointer_drift_fails():
    bad = b'/releases/download/v0.4.5/ER-Archipelago-v0.4.5.zip'
    errors = channels.verify(fixture(downloads=bad), "https://example.test")
    assert any("downloads points at v0.4.5" in error for error in errors)


# ---------------------------------------------------------------------------
# V.R.M.F. A release tag may carry a fourth "fixpack" segment (er-archipelago tools/vrmf.py):
# v0.6.0 is the first release on a line and v0.6.0.1, v0.6.0.2 the fixpacks after it. Both
# spellings are immutable tags, so both must pass every place a tag is parsed -- and the wizard's
# embedded apworld_version carries the fourth segment too (it is the tag minus the leading `v`).
# ---------------------------------------------------------------------------

FIX_LEDGER = (b"stable\tv0.6.0\t2026-09-06\trelease\n"
              b"stable\tv0.6.0.2\t2026-09-07\tsecond fixpack on the 0.6.0 line\n"
              b"beta\tmain\t2026-09-07\tdevelopment\n")
FIX_STABLE = b'<script>{"apworld_version": "0.6.0.2"}</script>'
FIX_DOWNLOADS = b'/releases/download/v0.6.0.2/ER-Archipelago-v0.6.0.2.zip'


def fixpack_fixture(live_stable=FIX_STABLE, downloads=FIX_DOWNLOADS):
    values = {
        "https://raw.githubusercontent.com/4laric/er-archipelago/main/release/CHANNELS.tsv":
            FIX_LEDGER,
        "https://raw.githubusercontent.com/4laric/er-archipelago/v0.6.0.2/wizard/wizard.html":
            FIX_STABLE,
        "https://raw.githubusercontent.com/4laric/er-archipelago/main/wizard/wizard.html": BETA,
        "https://example.test/er/wizard.html": live_stable,
        "https://example.test/er/beta/wizard.html": BETA,
        "https://example.test/downloads": downloads,
    }
    return values.__getitem__


def test_three_segment_tag_is_still_an_immutable_release():
    assert channels.stable_ref(LEDGER) == "v0.4.6"


def test_four_segment_fixpack_tag_is_an_immutable_release():
    assert channels.stable_ref(FIX_LEDGER) == "v0.6.0.2"


def test_non_release_stable_refs_are_still_rejected():
    for bad in (b"stable\tmain\t2026-09-07\tno\n",
                b"stable\tv0.6\t2026-09-07\tno\n",
                b"stable\tv0.6.0.2.1\t2026-09-07\tno\n",
                b"stable\tv0.6.0.2-rc1\t2026-09-07\tno\n"):
        try:
            channels.stable_ref(bad)
        except ValueError:
            continue
        raise AssertionError("accepted a non-release stable ref: %r" % (bad,))


def test_download_ref_reads_a_fixpack_bundle():
    assert channels.download_ref(FIX_DOWNLOADS) == "v0.6.0.2"


def test_fixpack_channels_agree():
    assert channels.verify(fixpack_fixture(), "https://example.test") == []


def test_fixpack_wizard_version_drift_fails():
    """The wizard must embed the tag minus `v`, fourth segment included."""
    stale = b'<script>{"apworld_version": "0.6.0"}</script>'
    errors = channels.verify(fixpack_fixture(live_stable=stale), "https://example.test")
    assert any("embeds 0.6.0, channel ledger says v0.6.0.2" in error for error in errors)


def test_fixpack_download_pointer_drift_fails():
    bad = b'/releases/download/v0.6.0.1/ER-Archipelago-v0.6.0.1.zip'
    errors = channels.verify(fixpack_fixture(downloads=bad), "https://example.test")
    assert any("downloads points at v0.6.0.1" in error for error in errors)


# ---------------------------------------------------------------------------
# The second game. Same monitor, one row over: a different repo, a different path to the builder
# inside it, a different site root, a different bundle name, and a tag spelling that carries
# `-beta.N`. All five used to be hardcoded, which is why there is a table now and not a copy.
# ---------------------------------------------------------------------------

BB = channels.GAMES["bb"]
BB_LEDGER = b"stable\tv0.1.0-beta.5\t2026-09-05\nbeta\tmain\t2026-09-05\n"
BB_STABLE = b'<script>{"apworld_version": "0.1.0-beta.5"}</script>'
BB_BETA = b'<script>{"apworld_version": "0.1.0-beta.6"}</script>'
BB_DOWNLOADS = b'/releases/download/v0.1.0-beta.5/BloodborneAPLauncher-win-x64.zip'


def bb_fixture(live_stable=BB_STABLE, live_beta=BB_BETA, downloads=BB_DOWNLOADS):
    raw = "https://raw.githubusercontent.com/4laric/bb-archipelago"
    values = {
        f"{raw}/main/release/CHANNELS.tsv": BB_LEDGER,
        f"{raw}/v0.1.0-beta.5/site/wizard.html": BB_STABLE,
        f"{raw}/main/site/wizard.html": BB_BETA,
        "https://example.test/bb/wizard.html": live_stable,
        "https://example.test/bb/beta/wizard.html": live_beta,
        "https://example.test/downloads": downloads,
    }
    return values.__getitem__


def test_bb_matching_channels_pass():
    assert channels.verify(bb_fixture(), "https://example.test", game=BB) == []


def test_bb_beta_tags_are_immutable_release_tags():
    """`v0.1.0-beta.5` is a tag, not a branch. The prerelease FLAG is a GitHub UI checkbox and
    says nothing about whether the ref can move under a deploy."""
    assert channels.stable_ref(BB_LEDGER, BB.tag_pattern) == "v0.1.0-beta.5"


def test_bb_rejects_a_moving_stable_pointer():
    import pytest
    with pytest.raises(ValueError):
        channels.stable_ref(b"stable\tmain\t2026-09-05\n", BB.tag_pattern)


def test_er_still_rejects_a_beta_tag_as_stable():
    """The looser spelling is Bloodborne's, not the site's. ER stable stays V.R.M[.F]."""
    import pytest
    with pytest.raises(ValueError):
        channels.stable_ref(b"stable\tv0.6.0-beta.1\t2026-09-05\n")


def test_bb_download_pointer_drift_fails():
    bad = b'/releases/download/v0.1.0-beta.4/BloodborneAPLauncher-win-x64.zip'
    errors = channels.verify(bb_fixture(downloads=bad), "https://example.test", game=BB)
    assert any("v0.1.0-beta.4" in error for error in errors)


def test_bb_main_leaking_into_stable_fails():
    errors = channels.verify(bb_fixture(live_stable=BB_BETA), "https://example.test", game=BB)
    assert any("stable wizard is not v0.1.0-beta.5" in error for error in errors)
    assert any("byte-identical" in error for error in errors)
