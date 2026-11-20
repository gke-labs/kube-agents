# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for ``gateway_plugins_as_shipped``, against a fake ``kubectl``.

The fake sits first on ``PATH`` and has four modes, as in
test_sandbox_tree_verifier.py. ``run`` executes the verifier's real in-pod
script locally with ``/opt/defaults/plugins`` and ``/opt/data/plugins``
re-rooted under a temporary tree, so the missing, differing, symlink and
pinned cases are the script's own behaviour. ``canned`` prints a given reply
and exit code, for what a laptop cannot stage: a non-zero kubectl, a reply
cut short, and a copy owned by another user. ``sleep`` hangs, for the exec
timeout. ``once_then_fail`` runs the script once and then fails every exec.

The state the check was written for (gke-labs/kube-agents#6279) is the
``missing`` one: a root-owned ``/opt/data/plugins`` the entrypoint could not
copy into, read as every shipped plugin absent from the volume.

The load-bearing property is the one every verifier in this package keeps:
a check that could not look is ``status="error"``, never a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import verifiers
from kube_agents_bench.verifiers import GatewayPluginsAsShippedVerifier

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ("agent_roster", "bootstrap_onboarding", "common", "tool_call_audit")
PINNED = verifiers.GATEWAY_PLUGINS_PINNED

_FAKE_KUBECTL = f"""#!{sys.executable}
import json, os, subprocess, sys, time
argv = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
mode = os.environ["FAKE_KUBECTL_MODE"]
if mode == "sleep":
    time.sleep(10)
if mode == "once_then_fail":
    marker = os.environ["FAKE_KUBECTL_MARKER"]
    if os.path.exists(marker):
        sys.stderr.write("Error from server: pods not found")
        sys.exit(1)
    open(marker, "w").close()
if mode == "canned":
    sys.stdout.write(os.environ.get("FAKE_KUBECTL_STDOUT", ""))
    sys.stderr.write(os.environ.get("FAKE_KUBECTL_STDERR", ""))
    sys.exit(int(os.environ.get("FAKE_KUBECTL_RC", "0")))
# run: argv after `--` is `sh -c SCRIPT sh IMG COPY PINNED`; the two absolute
# paths are re-rooted under FAKE_ROOT.
cmd = argv[argv.index("--") + 1 :]
root = os.environ["FAKE_ROOT"]
cmd = [root + a if i >= 4 and a.startswith("/") else a for i, a in enumerate(cmd)]
sys.exit(subprocess.run(cmd).returncode)
"""


@pytest.fixture
def kubectl(tmp_path, monkeypatch):
    """A fake kubectl on PATH; returns a helper that reads its argv log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kubectl"
    fake.write_text(_FAKE_KUBECTL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "kubectl.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(log))
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "run")
    monkeypatch.setenv("FAKE_ROOT", str(tmp_path / "pod"))
    for name in ("AGENT_SERVICE_NAME", "AGENT_NAMESPACE", "AGENT_CLUSTER_CONTEXT", "AGENT_CONTAINER"):
        monkeypatch.delenv(name, raising=False)

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


def _copytree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True)
    for item in src.iterdir():
        if item.is_dir():
            _copytree(item, dst / item.name)
        else:
            (dst / item.name).write_bytes(item.read_bytes())


@pytest.fixture
def pod(tmp_path) -> Path:
    """An agent filesystem whose volume copy of the plugins equals the
    image's, plus the pinned plugin with the state its step 4 writes."""
    root = tmp_path / "pod"
    image = root / "opt/defaults/plugins"
    for name in SHIPPED:
        (image / name).mkdir(parents=True)
        (image / name / "plugin.py").write_text(f"# {name}\n")
        (image / name / "__init__.py").write_text("")
    for name in PINNED:
        (image / name).mkdir(parents=True)
        (image / name / "config.yaml").write_text("backends: []\n")
    # A file at the top of the plugins directory: not a plugin, not counted.
    (image / "README.md").write_text("plugins\n")
    copy = root / "opt/data/plugins"
    for name in SHIPPED:
        _copytree(image / name, copy / name)
    for name in PINNED:
        _copytree(image / name, copy / name)
        (copy / name / "config.yaml").write_text("backends: [otlp]\n")  # step 4's endpoint
    # The operator-mounted default-profile plugin beside them, root-owned on a
    # real install; the image does not ship it, so it is not read.
    (copy / "pubsubplatform").mkdir()
    return root


def _image(pod: Path) -> Path:
    return pod / "opt/defaults/plugins"


def _copy(pod: Path) -> Path:
    return pod / "opt/data/plugins"


def _check() -> GatewayPluginsAsShippedVerifier:
    return GatewayPluginsAsShippedVerifier(type="gateway_plugins_as_shipped")


def _canned(monkeypatch, stdout: str, rc: int = 0, stderr: str = "") -> None:
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "canned")
    monkeypatch.setenv("FAKE_KUBECTL_STDOUT", stdout)
    monkeypatch.setenv("FAKE_KUBECTL_STDERR", stderr)
    monkeypatch.setenv("FAKE_KUBECTL_RC", str(rc))


UID = 10000


def _all_same(names: tuple[str, ...] = SHIPPED + PINNED, uid: int = UID, count: int | None = None) -> str:
    lines = [f"uid {uid}"]
    for name in names:
        path = f"/opt/data/plugins/{name}"
        state = "present" if name in PINNED else "same"
        lines += [f"begin {path}", f"owner {uid}", f"end {path} {state}"]
    lines.append(f"count {len(names) if count is None else count}")
    return "\n".join(lines + ["done"]) + "\n"


# --- the read itself --------------------------------------------------------


def test_a_volume_with_every_shipped_plugin_passes(kubectl, pod):
    result = _check().verify(0.0)
    assert result.status == "pass", result.reason
    assert f"all {len(SHIPPED) + len(PINNED)} plugin directories" in result.reason
    assert result.raw["count"] == len(SHIPPED) + len(PINNED)
    assert result.raw["uid"] == os.geteuid()
    states = {Path(p).name: s for p, s in result.raw["plugins"].items()}
    assert states == {**{n: "same" for n in SHIPPED}, **{n: "present" for n in PINNED}}
    assert "pubsubplatform" not in states


def test_the_exec_targets_the_agent_service_and_container(kubectl, pod, monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_NAME", "my-agent")
    monkeypatch.setenv("AGENT_NAMESPACE", "agents-ns")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "gke_p_r_c")
    monkeypatch.setenv("AGENT_CONTAINER", "agent")
    _check().verify(0.0)
    (argv,) = kubectl()
    head = argv[: argv.index("--")]
    assert head == ["--context", "gke_p_r_c", "-n", "agents-ns", "exec", "svc/my-agent", "-c", "agent"]
    tail = argv[argv.index("--") + 1 :]
    assert tail[:2] == ["sh", "-c"]
    assert tail[4:] == ["/opt/defaults/plugins", "/opt/data/plugins", "hermes_otel"]


def test_defaults_to_the_platform_agent_service_and_container(kubectl, pod):
    _check().verify(0.0)
    (argv,) = kubectl()
    assert argv[:6] == ["-n", "kubeagents-system", "exec", "svc/platform-agent", "-c", "platform-agent"]


def test_a_resolved_target_is_used_as_given(kubectl, pod, monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_NAME", "pod/platform-agent-gateway-abc")
    _check().verify(0.0)
    (argv,) = kubectl()
    assert argv[3] == "pod/platform-agent-gateway-abc"


def test_the_6279_state_every_plugin_missing_fails_and_names_each(kubectl, pod):
    """The entrypoint could not copy into a root-owned parent: the volume's
    plugins directory holds only the operator's mount."""
    for name in SHIPPED + PINNED:
        shutil.rmtree(_copy(pod) / name)
    result = _check().verify(0.0)
    assert result.status == "fail", result.reason
    for name in SHIPPED + PINNED:
        assert f"/opt/data/plugins/{name} is missing" in result.reason
    assert set(result.raw["plugins"].values()) == {"missing"}


def test_one_missing_plugin_fails(kubectl, pod):
    shutil.rmtree(_copy(pod) / "agent_roster")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "agent_roster is missing" in result.reason
    assert "bootstrap_onboarding" not in result.reason


def test_a_stale_plugin_fails_and_names_the_file(kubectl, pod):
    """A previous image's copy kept because the refresh failed."""
    (_copy(pod) / "agent_roster" / "plugin.py").write_text("# old\n")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "agent_roster differs from the image" in result.reason
    assert "plugin.py" in result.reason


def test_a_file_the_image_does_not_ship_inside_a_plugin_is_reported_not_failed(kubectl, pod):
    """The entrypoint copies without pruning, so a file an earlier image shipped
    stays on the volume after an image change; the plugins are still loaded."""
    (_copy(pod) / "common" / "left_over.py").write_text("from image A\n")
    (_copy(pod) / "common" / "sub").mkdir()
    (_copy(pod) / "common" / "sub" / "deeper.py").write_text("from image A\n")
    result = _check().verify(0.0)
    assert result.status == "pass", result.reason
    # `diff -rq` names a directory only the copy has once, not its contents.
    assert "2 entries beside them" in result.reason
    assert sorted(e.split(": ", 1)[1] for e in result.raw["extra"]) == ["left_over.py", "sub"]
    assert result.raw["plugins"][str(_copy(pod) / "common")] == "same"


def test_an_extra_inside_a_directory_both_sides_have_is_reported_not_failed(kubectl, pod):
    """`diff -rq` names this one as `Only in <copy>/<sub>: <name>`: the copy's
    path followed by a slash, the other separator the filter accepts."""
    (_image(pod) / "common" / "sub").mkdir()
    (_image(pod) / "common" / "sub" / "shipped.py").write_text("shipped\n")
    (_copy(pod) / "common" / "sub").mkdir()
    (_copy(pod) / "common" / "sub" / "shipped.py").write_text("shipped\n")
    (_copy(pod) / "common" / "sub" / "left_over.py").write_text("from image A\n")
    result = _check().verify(0.0)
    assert result.status == "pass", result.reason
    assert "1 entry beside them" in result.reason
    assert [e.split(": ", 1) for e in result.raw["extra"]] == [[f"Only in {_copy(pod) / 'common' / 'sub'}", "left_over.py"]]


def test_a_file_the_image_ships_but_the_copy_lacks_fails(kubectl, pod):
    (_copy(pod) / "common" / "__init__.py").unlink()
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "common differs from the image" in result.reason
    assert "__init__.py" in result.reason
    assert result.raw["extra"] == []


def test_an_extra_beside_a_stale_file_is_still_a_difference(kubectl, pod):
    (_copy(pod) / "common" / "left_over.py").write_text("from image A\n")
    (_copy(pod) / "common" / "plugin.py").write_text("# old\n")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "plugin.py" in result.reason
    assert result.raw["extra"] == []


def test_an_only_in_line_naming_the_image_side_is_a_difference(kubectl, monkeypatch):
    """The copy's path is a prefix of nothing on the image side, so a canned
    `Only in /opt/defaults/plugins/common: x` keeps the unit a difference."""
    out = _all_same().replace(
        "end /opt/data/plugins/common same", "| Only in /opt/defaults/plugins/common: x.py\nend /opt/data/plugins/common differ"
    )
    _canned(monkeypatch, out)
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "x.py" in result.reason


def test_an_only_in_line_for_a_sibling_directory_is_a_difference(kubectl, monkeypatch):
    """`/opt/data/plugins/common_old` is not under `/opt/data/plugins/common`."""
    out = _all_same().replace(
        "end /opt/data/plugins/common same", "| Only in /opt/data/plugins/common_old: x.py\nend /opt/data/plugins/common differ"
    )
    _canned(monkeypatch, out)
    assert _check().verify(0.0).status == "fail"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file, so diff cannot be made to exit 2")
def test_a_real_diff_that_cannot_read_a_file_is_an_error(kubectl, pod):
    """`diff -rq` exits 2 on a file it cannot open; the script reports `trouble`
    and the verdict is an error, never a pass or a same."""
    unreadable = _copy(pod) / "common" / "plugin.py"
    unreadable.chmod(0)
    try:
        result = _check().verify(0.0)
    finally:
        unreadable.chmod(0o644)
    assert result.status == "error", result.reason
    assert result.raw["plugins"][str(_copy(pod) / "common")] == "trouble"
    assert "could not compare" in result.reason


def test_a_plugin_swapped_for_a_symlink_fails(kubectl, pod):
    src = _copy(pod) / "tool_call_audit"
    src.rename(_copy(pod) / "aside")
    src.symlink_to(_copy(pod) / "aside")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "tool_call_audit has been replaced by a symlink" in result.reason


def test_the_pinned_plugins_own_state_is_not_a_difference(kubectl, pod):
    """hermes_otel's config.yaml differs from the image by design (step 4)."""
    result = _check().verify(0.0)
    assert result.status == "pass", result.reason


def test_a_missing_pinned_plugin_fails(kubectl, pod):
    shutil.rmtree(_copy(pod) / "hermes_otel")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "hermes_otel is missing" in result.reason


def test_a_copy_another_user_owns_fails(kubectl, monkeypatch):
    out = _all_same().replace("begin /opt/data/plugins/common\nowner 10000", "begin /opt/data/plugins/common\nowner 0")
    _canned(monkeypatch, out)
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "/opt/data/plugins/common is owned by uid 0, not the agent's 10000" in result.reason


def test_a_difference_outranks_an_owner_the_script_could_not_read(kubectl, monkeypatch):
    out = _all_same().replace("owner 10000\nend /opt/data/plugins/common same", "end /opt/data/plugins/common same")
    out = out.replace("end /opt/data/plugins/agent_roster same", "| Files a and b differ\nend /opt/data/plugins/agent_roster differ")
    _canned(monkeypatch, out)
    assert _check().verify(0.0).status == "fail"


def test_an_unknown_state_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace("end /opt/data/plugins/common same", "end /opt/data/plugins/common fine"))
    assert _check().verify(0.0).status == "error"


def test_an_unprefixed_line_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace("end /opt/data/plugins/common same", "stray\nend /opt/data/plugins/common same"))
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "stray" in result.reason


# --- could not look: error, never pass ------------------------------------


def test_a_failed_exec_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, "", rc=1, stderr='Error from server (NotFound): services "platform-agent" not found')
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "NotFound" in result.reason


def test_an_empty_reply_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, "")
    assert _check().verify(0.0).status == "error"


def test_no_kubectl_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _check().verify(0.0).status == "error"


def test_a_hung_exec_is_an_error(kubectl, monkeypatch):
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "sleep")
    assert _check().verify(1.0).status == "error"


def test_a_reply_without_the_last_line_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace("done\n", ""))
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "stopped before its last line" in result.reason


def test_an_image_with_no_plugins_directory_is_an_error(kubectl, pod):
    shutil.rmtree(_image(pod))
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "no plugins directory" in result.reason


def test_an_empty_image_plugins_directory_is_never_a_pass(kubectl, pod):
    for entry in _image(pod).iterdir():
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "count of 0" in result.reason


def test_a_count_the_units_do_not_meet_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same(count=9))
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "against a count of 9" in result.reason


def test_a_reply_without_a_count_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace(f"count {len(SHIPPED) + len(PINNED)}\n", ""))
    assert _check().verify(0.0).status == "error"


def test_a_reply_without_the_uid_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace(f"uid {UID}\n", ""))
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "the user it ran as" in result.reason


def test_a_diff_that_could_not_compare_is_an_error(kubectl, monkeypatch):
    out = _all_same().replace(
        "end /opt/data/plugins/common same", "| diff: /opt/data/plugins/common/x: I/O error\nend /opt/data/plugins/common trouble"
    )
    _canned(monkeypatch, out)
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "I/O error" in result.reason


def test_an_owner_the_script_could_not_read_is_an_error(kubectl, monkeypatch):
    out = _all_same().replace("owner 10000\nend /opt/data/plugins/common same", "end /opt/data/plugins/common same")
    _canned(monkeypatch, out)
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "owner unread" in result.reason


def test_a_difference_outranks_a_final_poll_that_errors(kubectl, pod, monkeypatch, tmp_path):
    shutil.rmtree(_copy(pod) / "agent_roster")
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "once_then_fail")
    monkeypatch.setenv("FAKE_KUBECTL_MARKER", str(tmp_path / "ran-once"))
    result = _check().verify(2.0)
    assert len(kubectl()) > 1
    assert result.status == "fail", result.reason
    assert "agent_roster is missing" in result.reason
    assert "the last read failed" in result.reason


# --- registration -----------------------------------------------------------


def test_the_type_is_registered_and_parses_like_a_task_yaml():
    assert VERIFIERS.get("gateway_plugins_as_shipped") is GatewayPluginsAsShippedVerifier
    assert isinstance(parse_node({"type": "gateway_plugins_as_shipped"}), GatewayPluginsAsShippedVerifier)


def test_it_takes_no_field():
    with pytest.raises(Exception):
        GatewayPluginsAsShippedVerifier(type="gateway_plugins_as_shipped", target="gateway")


def test_it_is_published_as_an_entry_point():
    with (REPO_ROOT / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["gateway_plugins_as_shipped"] == "kube_agents_bench.verifiers:GatewayPluginsAsShippedVerifier"


def test_the_paths_and_pinned_names_match_the_entrypoints():
    source = (REPO_ROOT / "deploy/shared/docker-entrypoint.sh").read_text()
    assert f'readonly DEFAULTS_PLUGINS_DIR="{verifiers.GATEWAY_PLUGINS_IMAGE}"' in source
    assert f'readonly PINNED_EXTERNAL_PLUGINS="{" ".join(verifiers.GATEWAY_PLUGINS_PINNED)}"' in source


def test_the_in_pod_diff_runs_under_the_c_locale():
    """The parser reads diff's "Only in <dir>: <name>" grammar, which gettext
    localises; the script pins it."""
    assert 'LC_ALL=C diff -rq --no-dereference "$s" "$d"' in verifiers._GATEWAY_PLUGINS_SCRIPT


def test_the_shipped_plugins_are_the_chat_defaults():
    shipped = sorted(p.name for p in (REPO_ROOT / "agents/chat/defaults/plugins").iterdir() if p.is_dir())
    assert shipped, "the image ships the chat profile's plugins from agents/chat/defaults/plugins"
    assert not set(shipped) & set(verifiers.GATEWAY_PLUGINS_PINNED)
