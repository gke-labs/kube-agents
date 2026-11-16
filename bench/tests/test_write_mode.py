"""The write-mode directive's prompt, and the broker read."""

from __future__ import annotations

import http.server
import json
import os
import shlex
import subprocess
import sys
import threading

import pytest
from kube_agents_bench import write_mode

DIRECTIVE = write_mode.DIRECTIVE
ASK = "Open a pull request that scales checkout-gateway to 4 replicas."


def test_an_ordinary_ask_is_not_a_write_mode_ask():
    assert write_mode.parse(ASK) is None
    assert write_mode.parse(f"{ASK}\n{DIRECTIVE} set=none") is None


def test_the_fields_parse():
    assert write_mode.parse(f"{DIRECTIVE} set=none\n{ASK}") == write_mode.WriteMode(ASK, "none")
    assert write_mode.parse(f"{DIRECTIVE} set=propose from=none\n{ASK}") == write_mode.WriteMode(ASK, "propose", "none")


@pytest.mark.parametrize(
    "first",
    [
        f"{DIRECTIVE}",
        f"{DIRECTIVE} from=none",
        f"{DIRECTIVE} set=explain",
        f"{DIRECTIVE} set=none set=propose",
        f"{DIRECTIVE} set=none from=none",
        f"{DIRECTIVE} set=none extra",
        f"{DIRECTIVE}set=none",
    ],
)
def test_a_bad_directive_is_refused(first):
    assert write_mode.is_write_mode_ask(f"{first}\n{ASK}")
    with pytest.raises(ValueError):
        write_mode.parse(f"{first}\n{ASK}")


def test_a_directive_without_an_ask_is_refused():
    with pytest.raises(ValueError):
        write_mode.parse(f"{DIRECTIVE} set=none\n\n")


def test_the_module_imports_without_devops_bench():
    code = (
        "import sys; sys.modules['devops_bench'] = None; sys.modules['devops_bench.agents'] = None; "
        "import kube_agents_bench.write_mode; print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


def _run_read(tmp_path, answer: str) -> str:
    """Run the in-pod read under this interpreter against a stand-in vcs.py that prints ``answer``."""
    vcs = tmp_path / "vcs.py"
    vcs.write_text(f"import sys\nassert sys.argv[1:] == ['capabilities', 'org/repo'], sys.argv\nprint({answer!r})\n")
    command = write_mode.read_command("org/repo").replace(shlex.quote(write_mode.VCS_SCRIPT), shlex.quote(str(vcs)))
    command = command.replace("python3 -c", f"{shlex.quote(sys.executable)} -c")
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=60, check=False).stdout


def test_the_read_is_the_brokers_capabilities(tmp_path):
    for mode in write_mode.MODES:
        out = _run_read(tmp_path, f'{{"forge": "github", "writeMode": "{mode}"}}')
        assert write_mode.read_mode(lambda cmd, timeout, out=out: out, 1.0, "org/repo") == mode


@pytest.mark.parametrize(
    "answer",
    [
        '{"forge": "github"}',
        '{"error": "CREDENTIAL_PROXY_URL is not set"}',
        '{"writeMode": "explain"}',
        "[]",
        "Traceback",
    ],
)
def test_a_read_without_a_mode_is_unavailable(tmp_path, answer):
    out = _run_read(tmp_path, answer)
    with pytest.raises(write_mode.ModeUnavailable):
        write_mode.read_mode(lambda cmd, timeout: out, 1.0, "org/repo")


def test_a_read_that_never_ran_or_has_no_repository_is_unavailable(monkeypatch):
    with pytest.raises(write_mode.ModeUnavailable):
        write_mode.read_mode(lambda cmd, timeout: "", 1.0, "org/repo")
    monkeypatch.delenv(write_mode.REPOSITORY_ENV, raising=False)
    with pytest.raises(write_mode.ModeUnavailable):
        write_mode.read_mode(lambda cmd, timeout: "", 1.0)


def test_a_vcs_script_that_crashes_says_why(tmp_path):
    vcs = tmp_path / "vcs.py"
    vcs.write_text("import sys\nsys.stderr.write('ImportError: no module named vcs_client\\n')\nsys.exit(1)\n")
    command = write_mode.read_command("org/repo").replace(shlex.quote(write_mode.VCS_SCRIPT), shlex.quote(str(vcs)))
    command = command.replace("python3 -c", f"{shlex.quote(sys.executable)} -c")
    out = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=60, check=False).stdout
    with pytest.raises(write_mode.ModeUnavailable, match="vcs_client"):
        write_mode.read_mode(lambda cmd, timeout: out, 1.0, "org/repo")


def test_the_watched_pods_are_the_agents_and_the_brokers():
    listing = "\n".join(
        [
            "ka-gateway-7d9-abc 0 1",
            "ka-credential-proxy-5f-xyz 2",
            "ka-credential-proxy-exec-tier-9-q 0",
            "ka-shell-0 0",
            "other-gateway-1 0",
        ]
    )
    assert write_mode.watched_pods(listing, "ka") == {"ka-gateway-7d9-abc": "0 1", "ka-credential-proxy-5f-xyz": "2"}
    assert write_mode.watched_pods(None, "ka") is None


PODS = {"ka-gateway-1": "0"}


@pytest.mark.parametrize(
    "initial, readings, restored, pods, error, status",
    [
        ("propose", [{"set": "none", "read": "none"}], "propose", (PODS, PODS), "", "completed"),
        ("none", [{"set": "none", "read": "none"}], "none", (PODS, PODS), "already at none", "error"),
        ("propose", [{"set": "none", "read": "propose"}], "propose", (PODS, PODS), "did not read back", "error"),
        ("propose", [{"set": "none", "read": "none"}], "propose", (PODS, {"ka-gateway-2": "0"}), "restarted", "error"),
        ("propose", [{"set": "none", "read": "none"}], "propose", (PODS, None), "could not be listed", "error"),
        ("propose", [{"set": "none", "read": "none"}], "none", (PODS, PODS), "not put back", "error"),
    ],
)
def test_the_entry_is_completed_only_on_a_real_change_read_back_and_put_back(initial, readings, restored, pods, error, status):
    got = write_mode.entry(write_mode.WriteMode(ASK, "none"), initial, readings, restored, pods)
    assert got["name"] == write_mode.SET_ENTRY
    assert got["status"] == status
    assert error in (got["result"]["error"] or "")


def test_a_from_mode_needs_both_sets():
    asked = write_mode.WriteMode(ASK, "propose", "none")
    got = write_mode.entry(asked, "propose", [{"set": "none", "read": "none"}], "propose", (PODS, PODS))
    assert got["status"] == "error"
    assert "1 of 2" in got["result"]["error"]


@pytest.mark.parametrize("initial, restored", [("explain", "explain"), ("propose", "explain")])
def test_the_entry_refuses_a_dial_level_where_a_broker_mode_belongs(initial, restored):
    got = write_mode.entry(write_mode.WriteMode(ASK, "none"), initial, [{"set": "none", "read": "none"}], restored, (PODS, PODS))
    assert got["status"] == "error"
    assert "not broker modes" in got["result"]["error"]


class _SessionKV:
    """A stand-in for session-kv's board routes, recording each dial.level it is sent."""

    def __init__(self, dial, answer):
        self.dial, self.answer, self.posts, self.tokens = dial, answer, [], []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.tokens.append(self.headers.get("Authorization"))
                assert self.path == "/v1/board", self.path
                self._reply({"clusters": [], "dial": stub.dial})

            def do_POST(self):
                assert self.path == "/v1/board/actions", self.path
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.posts.append(body)
                if stub.answer.get("ok"):
                    stub.dial["level"] = body["level"]
                self._reply(stub.answer)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def shell(self, command, timeout):
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        command = command.replace(shlex.quote(write_mode.SESSION_KV_URL), shlex.quote(url))
        command = command.replace("python3 -c", f"{shlex.quote(sys.executable)} -c")
        env = {**os.environ, "SESSION_KV_API_KEY": "k"}
        return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=timeout, env=env, check=False).stdout


@pytest.fixture
def session_kv():
    stubs = []

    def make(dial=None, answer=None):
        stub = _SessionKV(
            {"enabled": True, "level": "propose", "admins": ["U01", "U02"]} if dial is None else dial,
            {"action": "dial.level", "ok": True, "message": "Set.", "reported": True} if answer is None else answer,
        )
        stubs.append(stub)
        return stub

    yield make
    for stub in stubs:
        stub.server.shutdown()


def test_the_set_is_an_admins_dial_level_tap_and_restore_puts_the_level_back(session_kv):
    stub = session_kv()
    assert write_mode.set_mode(stub.shell, "none", 30) == "propose"
    write_mode.restore(stub.shell, "propose", 30)
    assert stub.posts == [
        {"action": "dial.level", "level": "explain", "user": "U01"},
        {"action": "dial.level", "level": "propose", "user": "U01"},
    ]
    assert stub.tokens[0] == "Bearer k"


@pytest.mark.parametrize(
    "dial, answer, match, previous",
    [
        ({"enabled": False}, None, "dial is off", None),
        ({"enabled": True, "level": "propose", "admins": []}, None, "no admin", None),
        (None, {"action": "dial.level", "ok": False, "message": "Only an install admin can change this."}, "refused", "propose"),
        (None, {"action": "dial.level", "ok": True, "message": "Saved.", "reported": False}, "did not confirm", "propose"),
    ],
)
def test_a_set_that_does_not_take_is_unavailable_and_carries_the_level_found(session_kv, dial, answer, match, previous):
    stub = session_kv(dial, answer)
    with pytest.raises(write_mode.ModeUnavailable, match=match) as raised:
        write_mode.set_mode(stub.shell, "none", 30)
    assert raised.value.previous == previous
    if dial is not None:
        assert stub.posts == []


def test_a_from_ask_puts_back_the_level_found_before_the_first_set(session_kv):
    stub = session_kv()
    first = write_mode.set_mode(stub.shell, "none", 30)
    assert write_mode.set_mode(stub.shell, "propose", 30) == "explain"
    assert write_mode.set_mode(stub.shell, "none", 30, keep=first) == "propose"
    stub.answer = {"action": "dial.level", "ok": False, "message": "No."}
    with pytest.raises(write_mode.ModeUnavailable) as raised:
        write_mode.set_mode(stub.shell, "propose", 30, keep=first)
    assert raised.value.previous == "propose"


def test_a_set_that_never_ran_is_unavailable():
    with pytest.raises(write_mode.ModeUnavailable, match="never ran"):
        write_mode.set_mode(lambda cmd, timeout: "", "none", 1.0)
