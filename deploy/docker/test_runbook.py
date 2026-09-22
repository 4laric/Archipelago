"""test_runbook.py -- keep MIGRATION.md and smoke.sh honest against the files they describe.

A runbook is prose, and prose rots silently: a volume renamed in `docker-compose.yml`, a key added
to `.env.example`, a third game added to `webgui/games.py` all leave the runbook *looking* right.
The failure mode is not a broken doc, it is a migration that copies two volumes out of three and
brings up a site with no cert -- discovered at the DNS cutover, which is the one moment there is
no time to read anything.

So the assertions here are all of the same shape: the runbook must NAME everything the machine
knows about. Naming is a low bar deliberately -- this cannot check that the steps are correct, only
that nothing has appeared that nobody wrote down.

Run:
    python -m pytest deploy/docker/test_runbook.py -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

MIGRATION = os.path.join(HERE, "MIGRATION.md")
SMOKE = os.path.join(HERE, "smoke.sh")
MIGRATE = os.path.join(HERE, "migrate-volumes.sh")
BOOTSTRAP = os.path.join(HERE, "bootstrap-box.sh")
BACKUP = os.path.join(HERE, "backup.sh")
COMPOSE = os.path.join(HERE, "docker-compose.yml")
ENV_EXAMPLE = os.path.join(HERE, ".env.example")

if REPO not in sys.path:
    sys.path.insert(0, REPO)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# What the machine knows
# ---------------------------------------------------------------------------

def compose_volumes() -> list[str]:
    """The named volumes declared by docker-compose.yml, unprefixed.

    Parsed by hand rather than with PyYAML: this test must run in the same bare `pip install
    pytest` job as the rest of deploy/docker's CI (see bb-channel-parity.yml), and adding a
    dependency to assert a doc mentions three strings is a poor trade. The top-level `volumes:`
    block is the last one in the file and its entries are two-space-indented keys.
    """
    text = read(COMPOSE)
    match = re.search(r"^volumes:\n((?:[ \t]+\S.*\n|\n)+)", text, re.MULTILINE)
    assert match, "docker-compose.yml has no top-level volumes: block"
    return re.findall(r"^  ([A-Za-z0-9_.-]+):", match.group(1), re.MULTILINE)


def env_keys() -> list[str]:
    """Every key in .env.example, including the deliberately commented-out ones.

    `DOWNLOADS_GITHUB_TOKEN` ships commented out on purpose (an empty value would take the inline
    comment as its value). It is still a key an operator can carry across, so it counts.
    """
    return sorted(set(re.findall(r"^#?([A-Z][A-Z0-9_]+)=", read(ENV_EXAMPLE), re.MULTILINE)))


def game_paths() -> list[str]:
    """Every site path the game table exposes, from webgui/games.py itself."""
    from webgui.games import GAMES

    paths = ["/", "/downloads", "/hosting"]
    for game in GAMES.values():
        paths.append(game.root)              # /er/ , /bb/
        paths.append(game.builder_url)       # /er/  , /bb/wizard.html
        paths.append(game.root + "checks.html")
    return sorted(set(paths))


# ---------------------------------------------------------------------------
# MIGRATION.md
# ---------------------------------------------------------------------------

class TestMigrationDoc:
    def test_exists_and_is_not_a_stub(self):
        assert os.path.isfile(MIGRATION)
        assert len(read(MIGRATION)) > 4000, "MIGRATION.md is too short to be a runbook"

    def test_names_every_compose_volume(self):
        text = read(MIGRATION)
        volumes = compose_volumes()
        assert volumes, "parsed no volumes out of docker-compose.yml -- the parser broke"
        missing = [v for v in volumes if v not in text]
        assert not missing, (
            f"MIGRATION.md never names these volumes: {missing}. A volume the runbook does not "
            f"name is a volume the migration does not carry."
        )

    def test_names_every_env_key(self):
        text = read(MIGRATION)
        missing = [k for k in env_keys() if k not in text]
        assert not missing, (
            f"MIGRATION.md never names these .env keys: {missing}. Appendix B is the inventory "
            f"the copied .env is checked against; a key missing from it is a key nobody checks."
        )

    def test_has_a_checklist_with_owner_and_status(self):
        text = read(MIGRATION)
        assert "| Owner | Status |" in text, "the checklist table lost its owner/status columns"

    def test_covers_the_irreversible_moments(self):
        text = read(MIGRATION).lower()
        for phrase in ("rollback", "decommission", "dns", "ttl", "docker compose stop web"):
            assert phrase in text, f"MIGRATION.md never mentions {phrase!r}"

    def test_points_at_both_scripts(self):
        text = read(MIGRATION)
        assert "migrate-volumes.sh" in text
        assert "smoke.sh" in text


# ---------------------------------------------------------------------------
# smoke.sh
# ---------------------------------------------------------------------------

class TestSmokeScript:
    def test_covers_every_path_the_game_table_exposes(self):
        text = read(SMOKE)
        missing = []
        for path in game_paths():
            # `/` is checked as a quoted argument; a bare slash would match anything.
            needle = '"/"' if path == "/" else f'"{path}"'
            if needle not in text:
                missing.append(path)
        assert not missing, (
            f"smoke.sh does not check these paths: {missing}. They come from webgui/games.py -- "
            f"a game added to the table and not to the smoke test goes live untested."
        )

    def test_checks_latest_json_against_the_ledger(self):
        text = read(SMOKE)
        assert "/bb/latest.json" in text
        assert "CHANNELS.tsv" in text, "the ledger comparison is what catches a stale latest.json"

    def test_uses_resolve_so_it_tests_the_new_box(self):
        text = read(SMOKE)
        assert "--resolve" in text, (
            "without --resolve this hits whatever DNS says, which is the OLD box -- the entire "
            "point is to test NEWBOX before the cutover"
        )

    def test_fails_loudly(self):
        text = read(SMOKE)
        assert "set -euo pipefail" in text
        assert "exit 1" in text, "a smoke test that always exits 0 is a green light, not a test"

    def test_usage_error_without_an_ip(self):
        result = subprocess.run(["bash", SMOKE], capture_output=True, text=True)
        assert result.returncode == 2, "smoke.sh with no NEWBOXIP must exit 2, not run"
        assert "usage" in result.stderr.lower()


# ---------------------------------------------------------------------------
# migrate-volumes.sh
# ---------------------------------------------------------------------------

class TestMigrateScript:
    def test_defaults_to_the_project_prefixed_names(self):
        text = read(MIGRATE)
        for vol in compose_volumes():
            assert f"${{PROJECT}}_{vol}" in text, (
                f"{vol} is not in the default transfer list, or is listed without the "
                f"COMPOSE_PROJECT_NAME prefix Compose actually creates it with"
            )

    def test_has_a_dry_run_and_a_checksum_step(self):
        text = read(MIGRATE)
        assert "--dry-run" in text
        assert "sha256sum" in text, "a copy with no verification is a hope"

    def test_fails_loudly(self):
        assert "set -euo pipefail" in read(MIGRATE)

    def test_usage_error_without_a_remote(self):
        result = subprocess.run(["bash", MIGRATE], capture_output=True, text=True)
        assert result.returncode == 2
        assert "usage" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Both scripts, as shell
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", [SMOKE, MIGRATE, BOOTSTRAP, BACKUP])
def test_bash_syntax(script):
    result = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n {os.path.basename(script)}:\n{result.stderr}"


@pytest.mark.parametrize("script", [SMOKE, MIGRATE, BOOTSTRAP, BACKUP])
def test_shellcheck(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed here -- CI installs it; bash -n still ran")
    result = subprocess.run(["shellcheck", "-S", "warning", script],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("script", [SMOKE, MIGRATE, BOOTSTRAP, BACKUP])
def test_executable_bit(script):
    """Git tracks the mode; a runbook that says `./smoke.sh` needs it to be one."""
    result = subprocess.run(["git", "ls-files", "-s", "--", script],
                            capture_output=True, text=True, cwd=REPO)
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("not a git checkout, or the file is not tracked yet")
    mode = result.stdout.split()[0]
    assert mode == "100755", f"{os.path.basename(script)} is tracked as {mode}, want 100755"


# ---------------------------------------------------------------------------
# Fresh-box path: bootstrap-box.sh and backup.sh
# ---------------------------------------------------------------------------

class TestFreshBoxPath:
    def test_bootstrap_opens_the_range_from_env_not_a_literal(self):
        """The room range is one fact, written in .env; a second copy in a script is how the old
        box ended up opening 38400:38463 while compose published 38400-38599."""
        text = open(BOOTSTRAP, encoding="utf-8").read()
        assert "PORT_START" in text and "PORT_END" in text
        assert "38463" not in text

    def test_bootstrap_refuses_a_placeholder_acme_email(self):
        text = open(BOOTSTRAP, encoding="utf-8").read()
        assert "example.com" in text and "ACME_EMAIL" in text

    def test_bootstrap_never_overwrites_an_existing_env(self):
        text = open(BOOTSTRAP, encoding="utf-8").read()
        assert "if [ ! -f .env ]" in text

    def test_backup_archives_every_stateful_volume(self):
        text = open(BACKUP, encoding="utf-8").read()
        with open(os.path.join(HERE, "docker-compose.yml"), encoding="utf-8") as fh:
            compose = fh.read()
        # caddy_config is derived from the Caddyfile and is deliberately not backed up.
        for vol in ("peliarch_data", "caddy_data"):
            assert vol in compose and vol in text

    def test_backup_uses_the_project_prefixed_volume_names(self):
        text = open(BACKUP, encoding="utf-8").read()
        assert "${PROJECT}_peliarch_data" in text

    def test_caddyfile_has_no_site_block_for_a_domain_this_box_does_not_serve(self):
        """birdfuck.ca pointed elsewhere, so Caddy failed its ACME challenge on every retry."""
        with open(os.path.join(HERE, "Caddyfile"), encoding="utf-8") as fh:
            assert "birdfuck" not in fh.read()

    def test_env_example_does_not_ship_a_placeholder_acme_email(self):
        with open(os.path.join(HERE, ".env.example"), encoding="utf-8") as fh:
            line = next(l for l in fh if l.startswith("ACME_EMAIL="))
        assert "example.com" not in line
