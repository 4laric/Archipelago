"""
test_generator.py -- the seed-generation stage, and the limits that make it safe to expose.

Two halves, deliberately:

  * `TestValidateYamls` / `TestGenerateLimits` drive `webgui/generator.py` directly against a FAKE
    Generate.py, so the timeout, the process-group kill, the plando default and the
    no-artifact-on-success path are all tested WITHOUT needing an Archipelago checkout or 3 seconds
    of real fill. A stub is the only way to test "what happens when generation hangs".
  * `TestGenerateRoute` drives POST /generate through the Flask test client with generation
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
from webgui.app import create_app


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
# POST /generate -- the route's contract
# ---------------------------------------------------------------------------

class _Room:
    def __init__(self):
        self.id, self.name, self.status, self.tier = "abc12345", "seed", "RUNNING", "Standard"
        self.port, self.password, self.created_at = 38400, None, 0
        self.last_active_at, self.crash_count, self.idle_timeout = 0, 0, 1800

    def connect_info(self, host):
        return {"host": host, "port": self.port}


class _Mgr:
    public_host = "peliarch.test"

    def __init__(self):
        self.created = []

    def create_room(self, **kw):
        self.created.append(kw)
        return _Room()

    def start_room(self, room_id):
        return _Room()

    def list_rooms(self):
        return []


@pytest.fixture
def client_and_mgr():
    mgr = _Mgr()
    app = create_app(manager=mgr)
    app.config["TESTING"] = True
    return app.test_client(), mgr


JSON = {"Accept": "application/json"}


class TestGenerateRoute:

    def test_a_yaml_becomes_a_running_room(self, client_and_mgr, monkeypatch):
        client, mgr = client_and_mgr
        monkeypatch.setattr(generator, "generate",
                            lambda *a, **k: generator.GeneratedSeed(b"PK\x03\x04", "AP_9.zip", ""))
        r = client.post("/generate", data={"yaml": GOOD_YAML.decode(), "name": "wizard seed"},
                        headers=JSON)
        assert r.status_code == 201, r.data
        assert r.get_json()["seed_file"] == "AP_9.zip"
        assert mgr.created and mgr.created[0]["file_data"] == b"PK\x03\x04"
        assert mgr.created[0]["name"] == "wizard seed"

    def test_uploaded_player_files_work_too(self, client_and_mgr, monkeypatch):
        client, _ = client_and_mgr
        seen = {}

        def _fake(yamls, root, **kw):
            seen["n"] = len(yamls)
            return generator.GeneratedSeed(b"PK\x03\x04", "AP_9.zip", "")
        monkeypatch.setattr(generator, "generate", _fake)
        data = {"file": [(io.BytesIO(GOOD_YAML), "a.yaml"), (io.BytesIO(GOOD_YAML), "b.yaml")]}
        r = client.post("/generate", data=data, headers=JSON,
                        content_type="multipart/form-data")
        assert r.status_code == 201, r.data
        assert seen["n"] == 2

    def test_no_yaml_is_a_400(self, client_and_mgr):
        client, _ = client_and_mgr
        r = client.post("/generate", data={}, headers=JSON)
        assert r.status_code == 400
        assert "No yaml" in r.get_json()["error"]

    def test_an_invalid_yaml_is_rejected_before_generating(self, client_and_mgr, monkeypatch):
        client, _ = client_and_mgr

        def _boom(*a, **k):
            raise AssertionError("generate must not run on a yaml that failed validation")
        monkeypatch.setattr(generator, "generate", _boom)
        r = client.post("/generate", data={"yaml": "name: Tester\n"}, headers=JSON)
        assert r.status_code == 400
        assert "game:" in r.get_json()["error"]

    def test_a_failed_fill_is_422_not_500(self, client_and_mgr, monkeypatch):
        """The user's options did not generate. That is their problem to fix, not a server error."""
        client, _ = client_and_mgr

        def _fail(*a, **k):
            raise generator.GenerationError("Generation failed.", detail="FillError: ...")
        monkeypatch.setattr(generator, "generate", _fail)
        r = client.post("/generate", data={"yaml": GOOD_YAML.decode()}, headers=JSON)
        assert r.status_code == 422
        assert "FillError" in r.get_json()["detail"]

    def test_a_timeout_is_504(self, client_and_mgr, monkeypatch):
        client, _ = client_and_mgr

        def _slow(*a, **k):
            raise generator.GenerationError("too long", timed_out=True)
        monkeypatch.setattr(generator, "generate", _slow)
        r = client.post("/generate", data={"yaml": GOOD_YAML.decode()}, headers=JSON)
        assert r.status_code == 504

    def test_generation_can_be_switched_off(self, monkeypatch):
        """A box under load, or one without an AP tree, must be able to refuse cleanly."""
        import webgui.app as appmod
        monkeypatch.setattr(appmod, "GENERATE_ENABLED", False)
        app = appmod.create_app(manager=_Mgr())
        app.config["TESTING"] = True
        r = app.test_client().post("/generate", data={"yaml": GOOD_YAML.decode()}, headers=JSON)
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# The one test that uses the real thing
# ---------------------------------------------------------------------------

def test_real_generate_smoke():
    """Generate a real seed with the real Generate.py, if this tree can.

    SKIPS rather than passes when it cannot -- the stubs above would otherwise let a completely
    broken generation path sit behind a green suite.
    """
    root = os.environ.get("AP_ROOT", REPO_DIR)
    if not os.path.isfile(os.path.join(root, "Generate.py")):
        pytest.skip("no Generate.py at AP_ROOT -- real generation is NOT covered on this box")
    try:
        import worlds  # noqa: F401
    except Exception as e:
        pytest.skip(f"AP tree is not importable here ({e}) -- real generation is NOT covered")
    try:
        got = generator.generate([GOOD_YAML], root, seed=1, timeout=300)
    except generator.GenerationError as e:
        # An AP checkout with optional world dependencies missing (zilliandomizer, pymem, ...) fails
        # at world-import time, before anything this module owns is exercised. That is a fact about
        # the BOX, not a regression, and it must SKIP saying so rather than go red and train people
        # to ignore this test -- or pass quietly and pretend the path is covered.
        missing = [ln for ln in e.detail.splitlines() if "ModuleNotFoundError" in ln]
        if missing:
            pytest.skip("AP tree is missing an optional world dependency (%s) -- real generation is "
                        "NOT covered on this box" % missing[-1].strip())
        raise
    assert got.data[:2] == b"PK", "output is not a zip"
    assert len(got.data) > 1024
