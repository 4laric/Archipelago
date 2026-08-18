from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_er_channels.py"
SPEC = spec_from_file_location("check_er_channels", SCRIPT)
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
