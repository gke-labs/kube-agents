"""The write-mode directive's prompt, and the broker read."""

from __future__ import annotations

import shlex
import subprocess
import sys

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
