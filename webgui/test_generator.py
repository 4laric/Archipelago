"""
test_generator.py -- the seed-generation stage, and the limits that make it safe to expose.

Two halves, deliberately:

  * `TestValidateYamls` / `TestGenerateLimits` drive `webgui/generator.py` directly against a FAKE
    Generate.py, so the timeout, the process-group kill, the plando default and the
    no-artifact-on-success path are all tested WITHOUT needing an Archipelago checkout or 3 seconds
    of real fill. A stub is the only way to test "what happens when generation hangs".
  * The POST /generate route was RETIRED at v0.4.0; its 410 contract is asserted in
    test_app.py::TestRetiredRoutes. What follows drives the MODULE, which outlived the route
    monkeypatched, so the route's contract -- status codes, which failure is 422 vs 504, and that a
    generated seed reaches create_room -- is asserted without a fill either.

🛑 WHAT NEITHER HALF PROVES: that a REAL yaml generates a REAL seed. That needs an AP checkout with
apworlds installed, so it is an integration check on the box, not a unit test here. The one thing
this file must never become is a suite that passes because generation was stubbed everywhere and
nobody noticed the real path was broken -- `test_real_generate_smoke` exists for exactly that, and
SKIPS loudly (never passes quietly) when there is no Generate.py to run.

Run:
    python -m pytest webgui/test_generator.py -v
"""
from __future__ import annotations

import io
import os
import sys
import textwrap
import time

import pytest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from webgui import generator


GOOD_YAML = b"name: Tester\ngame: Clique\nClique:\n  hard_mode: false\n"


def _fake_ap_root(tmp_path, body: str) -> str:
    """An AP tree whose entire content is a Generate.py doing exactly what the test needs."""
    root = tmp_path / "ap"
    root.mkdir()
    (root / "Generate.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return str(root)


# ---------------------------------------------------------------------------
# validate_yamls -- the cheap rejections, before a process is spent
# ---------------------------------------------------------------------------

class TestValidateYamls:

    def test_accepts_a_normal_player_file(self):
        generator.validate_yamls([GOOD_YAML])   # must not raise

    def test_rejects_nothing_at_all(self):
        with pytest.raises(ValueError, match="No yaml"):
            generator.validate_yamls([])

    def test_rejects_oversize(self):
        big = b"name: A\ngame: Clique\n" + b"#" * (generator.DEFAULT_YAML_MAX_BYTES + 1)
        with pytest.raises(ValueError, match="too large"):
            generator.validate_yamls([big])

    def test_rejects_too_many_players(self):
        with pytest.raises(ValueError, match="Too many player files"):
            generator.validate_yamls([GOOD_YAML] * (generator.DEFAULT_MAX_PLAYERS + 1))

    def test_rejects_unparseable_yaml(self):
        with pytest.raises(ValueError, match="not valid YAML"):
            generator.validate_yamls([b"name: [unclosed\ngame: Clique\n"])

    def test_rejects_a_yaml_with_no_game(self):
        with pytest.raises(ValueError, match="no `game:` key"):
            generator.validate_yamls([b"name: Tester\n"])

    def test_rejects_a_scalar_document(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            generator.validate_yamls([b"just a string\n"])


# ---------------------------------------------------------------------------
# The limits. Each drives a stub Generate.py into the failure it is meant to bound.
# ---------------------------------------------------------------------------

class TestGenerateLimits:

    def test_happy_path_returns_the_zip_bytes(self, tmp_path):
        root = _fake_ap_root(tmp_path, '''
            import sys, os
            out = sys.argv[sys.argv.index("--outputpath") + 1]
            open(os.path.join(out, "AP_12345.zip"), "wb").write(b"PK\\x03\\x04seed")
            print("Done. Enjoy.")
        ''')
        got = generator.generate([GOOD_YAML], root)
        assert got.data == b"PK\x03\x04seed"
        assert got.filename == "AP_12345.zip"
        assert "Done. Enjoy." in got.stdout_tail

    def test_player_files_are_written_one_per_yaml(self, tmp_path):
        root = _fake_ap_root(tmp_path, '''
            import sys, os
            players = sys.argv[sys.argv.index("--player_files_path") + 1]
            out = sys.argv[sys.argv.index("--outputpath") + 1]
            names = sorted(os.listdir(players))
            open(os.path.join(out, "AP_1.zip"), "wb").write(("|".join(names)).encode())
        ''')
        got = generator.generate([GOOD_YAML, GOOD_YAML, GOOD_YAML], root)
        assert got.data == b"player1.yaml|player2.yaml|player3.yaml"

    def test_plando_is_passed_and_defaults_to_empty(self, tmp_path):
        """The public endpoint must not honour plando unless someone deliberately turns it on."""
        root = _fake_ap_root(tmp_path, '''
            import sys, os
            i = sys.argv.index("--plando")
            out = sys.argv[sys.argv.index("--outputpath") + 1]
            open(os.path.join(out, "AP_1.zip"), "wb").write(("plando=%r" % sys.argv[i + 1]).encode())
        ''')
        assert generator.generate([GOOD_YAML], root).data == b"plando=''"
        assert generator.generate([GOOD_YAML], root, plando="bosses").data == b"plando='bosses'"

    def test_a_hang_is_killed_and_reported_as_a_timeout(self, tmp_path):
        root = _fake_ap_root(tmp_path, '''
            import time
            print("starting", flush=True)
            time.sleep(120)
        ''')
        started = time.time()
        with pytest.raises(generator.GenerationError) as exc:
            generator.generate([GOOD_YAML], root, timeout=2)
        assert exc.value.timed_out is True
        assert time.time() - started < 30, "the timeout did not actually stop the child"

    def test_a_hanging_GRANDCHILD_is_killed_too(self, tmp_path):
        """Generate spawns children. Killing only the parent leaves them holding the box.

        The stub forks a grandchild that outlives its parent and writes a file after a delay; if the
        process-group kill works, that file never appears.
        """
        marker = tmp_path / "grandchild-survived"
        root = _fake_ap_root(tmp_path, f'''
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(6); open({str(marker)!r}, 'w').write('x')"])
            time.sleep(60)
        ''')
        with pytest.raises(generator.GenerationError):
            generator.generate([GOOD_YAML], root, timeout=2)
        time.sleep(8)
        assert not marker.exists(), "a grandchild outlived the process-group kill"

    def test_nonzero_exit_is_a_generation_error_with_the_tail(self, tmp_path):
        root = _fake_ap_root(tmp_path, '''
            import sys
            print("FillError: no room for the last item")
            sys.exit(1)
        ''')
        with pytest.raises(generator.GenerationError) as exc:
            generator.generate([GOOD_YAML], root)
        assert exc.value.timed_out is False
        assert "FillError" in exc.value.detail

    def test_success_with_no_artifact_is_its_own_error(self, tmp_path):
        """Exit 0 and no zip is a real state; it must not surface as an IndexError."""
        root = _fake_ap_root(tmp_path, 'print("Done. Enjoy.")')
        with pytest.raises(generator.GenerationError, match="produced no seed file"):
            generator.generate([GOOD_YAML], root)

    def test_missing_ap_tree_says_so(self, tmp_path):
        with pytest.raises(generator.GenerationError, match="No Generate.py"):
            generator.generate([GOOD_YAML], str(tmp_path / "nope"))

    def test_address_space_cap_is_applied_to_the_child(self, tmp_path):
        """RLIMIT_AS must be set in the CHILD, and must be a limit it cannot raise."""
        root = _fake_ap_root(tmp_path, '''
            import resource, sys, os
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            out = sys.argv[sys.argv.index("--outputpath") + 1]
            open(os.path.join(out, "AP_1.zip"), "wb").write(("%d|%d" % (soft, hard)).encode())
        ''')
        got = generator.generate([GOOD_YAML], root, max_address_space_mb=512)
        soft, hard = (int(x) for x in got.data.split(b"|"))
        assert soft == hard == 512 * 1024 * 1024

    def test_the_workspace_is_removed_afterwards(self, tmp_path):
        root = _fake_ap_root(tmp_path, '''
            import sys, os
            out = sys.argv[sys.argv.index("--outputpath") + 1]
            open(os.path.join(out, "AP_1.zip"), "wb").write(out.encode())
        ''')
        used = generator.generate([GOOD_YAML], root).data.decode()
        assert not os.path.exists(used), "the temp workspace outlived the call"


# ---------------------------------------------------------------------------
# POST /generate -- RETIRED AT v0.4.0
#
# 🛑 THE ROUTE IS GONE; webgui/generator.py IS NOT. Everything above this line drives the
# generator MODULE directly -- validate_yamls, the wall timeout, RLIMIT_AS/CPU, plando off, the
# process-group kill, the temp-workspace cleanup. Those tests stay, and they stay for a reason:
# the module is the part that was hard to get right, and if seed generation ever comes back it
# comes back on top of exactly this, already proven. Deleting them with the route would throw
# away the expensive half to tidy up the cheap half.
#
# What used to live below was `TestGenerateRoute`, which drove POST /generate through the Flask
# test client. The route now answers 410 Gone. Its contract -- 410, and a body carrying a
# sentence the OLD wizard can print, because old wizards in browsers and in every previous
# release zip still POST here -- is asserted in test_app.py::TestRetiredRoutes, next to the
# retired /rooms route it shares its reasoning with.
# ---------------------------------------------------------------------------
