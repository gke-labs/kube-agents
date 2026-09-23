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
