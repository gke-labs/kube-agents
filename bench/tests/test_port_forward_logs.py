"""Each port-forward's kubectl stderr survives the run under ARTIFACTS.

Every eval unit owns its own tunnel on its own port, and until now every one of
those logs went to a temp directory the process deleted on the way out -- so a
tunnel that died mid-run left nothing behind to say why. These cover where the
logs land, that the exit cleanup does not take Prow's copy with it, and that the
fallback still holds when ARTIFACTS is unusable or absent.
"""

from __future__ import annotations

import shutil

import pytest

from kube_agents_bench import harness


@pytest.fixture(autouse=True)
def _reset_pf_log_dir():
    """The directory is memoised in a module global, so each test starts clean,
    and a temp directory a test made is removed rather than left behind."""
    harness._PF_LOG_DIR = None
    harness._PF_LOG_DIR_IS_TEMP = True
    yield
    if harness._PF_LOG_DIR is not None and harness._PF_LOG_DIR_IS_TEMP:
        shutil.rmtree(harness._PF_LOG_DIR, ignore_errors=True)
    harness._PF_LOG_DIR = None
    harness._PF_LOG_DIR_IS_TEMP = True


def test_prow_gets_the_logs_under_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    assert harness._pf_log_dir() == tmp_path / "port-forward"
    assert (tmp_path / "port-forward").is_dir()
    assert harness._PF_LOG_DIR_IS_TEMP is False


def test_a_local_run_still_uses_a_temp_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("ARTIFACTS", raising=False)
    directory = harness._pf_log_dir()
    assert directory.is_dir()
    assert tmp_path not in directory.parents
    assert harness._PF_LOG_DIR_IS_TEMP is True


def test_an_unusable_artifacts_falls_back_rather_than_failing(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setenv("ARTIFACTS", str(blocked))
    directory = harness._pf_log_dir()
    assert directory.is_dir()
    assert harness._PF_LOG_DIR_IS_TEMP is True


def test_the_exit_cleanup_does_not_delete_prows_copy(monkeypatch, tmp_path):
    # The bug this guards: the atexit handler rmtree'd whatever _PF_LOG_DIR
    # pointed at, which under ARTIFACTS is the directory Prow is about to
    # upload -- so the logs would go seconds before they were collected.
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    log = harness._pf_log_path(28642)
    log.write_text("Handling connection for 28642\n")
    harness._cleanup_port_forwards()
    assert log.read_text() == "Handling connection for 28642\n"
    assert harness._PF_LOG_DIR is None  # re-derived on the next call


def test_a_temp_directory_is_still_cleaned_up(monkeypatch):
    monkeypatch.delenv("ARTIFACTS", raising=False)
    directory = harness._pf_log_dir()
    harness._cleanup_port_forwards()
    assert not directory.exists()


def test_each_port_gets_its_own_log(monkeypatch, tmp_path):
    # Units run concurrently on AGENT_LOCAL_PORT 28642+seq; one shared file
    # would interleave their stderr and make neither readable.
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    assert harness._pf_log_path(28642).name == "pf-28642.log"
    assert harness._pf_log_path(28643).name == "pf-28643.log"


def test_a_respawn_keeps_the_stderr_of_the_tunnel_that_died(monkeypatch, tmp_path):
    # The first version opened the log with "wb", so the respawn after a dead
    # tunnel erased the only line that said why it died. Each spawn appends,
    # behind a marker, so both tunnels' stderr survive.
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(harness, "_PF_PROCESSES", {})
    port = 28699
    spawned = []

    class FakeTunnel:
        def __init__(self, cmd, stdout, stderr):
            stderr.write(f"error: lost connection {len(spawned)}\n".encode())
            spawned.append(self)
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        kill = terminate

    monkeypatch.setattr(harness.subprocess, "Popen", FakeTunnel)
    # The port is open while a tunnel is alive, closed otherwise.
    monkeypatch.setattr(
        harness,
        "_port_open",
        lambda port, host="127.0.0.1": any(t.returncode is None for t in spawned),
    )

    harness._ensure_port_forward(port)
    spawned[0].returncode = 1  # the tunnel dies
    harness._ensure_port_forward(port)

    text = harness._pf_log_path(port).read_text()
    assert text.count(harness._PF_SPAWN_MARKER) == 2
    assert "error: lost connection 0\n" in text
    assert "error: lost connection 1\n" in text
    assert text.index("lost connection 0") < text.index("lost connection 1")
