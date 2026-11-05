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

"""Tests for ``sandbox_tree_matches_image``, against a fake ``kubectl``.

The fake sits first on ``PATH`` and has four modes. ``run`` executes the
verifier's real in-pod script locally, with ``/opt/defaults`` and
``/opt/data`` (or, for ``target: gateway``, the two template roots and the
profile copy) pointed at a temporary tree, so the diff, symlink and missing
cases are the script's own behaviour and not a hand-written transcript of
it. ``canned`` prints a given reply and exit code, for what a laptop cannot
stage: a non-zero kubectl, a reply cut short, and a root-owned reference.
``sleep`` hangs, for the exec timeout. ``once_then_fail`` runs the script
once and then fails every exec, for a difference followed by a lost pod.

The load-bearing property is the one every verifier in this package keeps:
a check that could not look is ``status="error"``, never a pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import verifiers
from kube_agents_bench.verifiers import SandboxTreeMatchesImageVerifier

REPO_ROOT = Path(__file__).resolve().parents[2]

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
# run: argv after `--` is `sh -c SCRIPT sh <positionals>`; every positional
# that is an absolute path (the sandbox's DEFAULTS and DATA, the gateway's
# TEMPLATE, OVERLAY and COPY) is re-rooted under FAKE_ROOT.
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
    for name in ("AGENT_SERVICE_NAME", "AGENT_NAMESPACE", "AGENT_CLUSTER_CONTEXT", "EVAL_SANDBOX_POD"):
        monkeypatch.delenv(name, raising=False)

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


@pytest.fixture
def pod(tmp_path) -> Path:
    """A sandbox filesystem whose staged trees equal the image's."""
    root = tmp_path / "pod"
    defaults = root / "opt" / "defaults"
    (defaults / "skills" / "gke-basics").mkdir(parents=True)
    (defaults / "skills" / "gke-basics" / "SKILL.md").write_text("# gke-basics\n")
    (defaults / "scripts").mkdir()
    (defaults / "scripts" / "forge.py").write_text("print('forge')\n")
    (defaults / "governance").mkdir()
    (defaults / "governance" / "sop.md").write_text("sop\n")
    data = root / "opt" / "data"
    for home in (data, data / "profiles" / "platform"):
        home.mkdir(parents=True, exist_ok=True)
        for tree in verifiers.SANDBOX_IMAGE_TREES:
            _copytree(defaults / tree, home / tree)
    return root


def _copytree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True)
    for item in src.iterdir():
        if item.is_dir():
            _copytree(item, dst / item.name)
        else:
            (dst / item.name).write_bytes(item.read_bytes())


def _check() -> SandboxTreeMatchesImageVerifier:
    return SandboxTreeMatchesImageVerifier(type="sandbox_tree_matches_image")


def _canned(monkeypatch, stdout: str, rc: int = 0, stderr: str = "") -> None:
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "canned")
    monkeypatch.setenv("FAKE_KUBECTL_STDOUT", stdout)
    monkeypatch.setenv("FAKE_KUBECTL_STDERR", stderr)
    monkeypatch.setenv("FAKE_KUBECTL_RC", str(rc))


def _all_same(prefix: str = "/opt/data") -> str:
    lines = []
    for home in verifiers.SANDBOX_HOME_ROOTS:
        for tree in verifiers.SANDBOX_IMAGE_TREES:
            path = f"{prefix}/{tree}" if home == "." else f"{prefix}/{home}/{tree}"
            lines += [f"begin {path}", f"end {path} same"]
    return "\n".join(lines + ["done"]) + "\n"


TEMPLATE_SKILLS = ("gke-basics", "fleet-audit")


def _gateway_check() -> SandboxTreeMatchesImageVerifier:
    return SandboxTreeMatchesImageVerifier(type="sandbox_tree_matches_image", target="gateway")


def _gateway_all_same(names: tuple[str, ...] = TEMPLATE_SKILLS, count: int | None = None) -> str:
    lines = []
    for name in names:
        path = f"/opt/data/profiles/platform/skills/{name}"
        lines += [f"begin {path}", f"end {path} same"]
    lines.append(f"count {len(names) if count is None else count}")
    return "\n".join(lines + ["done"]) + "\n"


# Both targets share the exec and the parser, so every could-not-look case
# is pinned on each: (the check, its all-same reply).
BOTH_TARGETS = [
    pytest.param(_check, _all_same, id="sandbox"),
    pytest.param(_gateway_check, _gateway_all_same, id="gateway"),
]


# --- the diff itself -------------------------------------------------------


def test_matching_trees_pass(kubectl, pod):
    result = _check().verify(0.0)
    assert result.status == "pass", result.reason
    assert "all 6 image trees" in result.reason


def test_the_exec_targets_the_agents_shell_sandbox_pod(kubectl, pod, monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_NAME", "my-agent")
    monkeypatch.setenv("AGENT_NAMESPACE", "agents-ns")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "gke_p_r_c")
    _check().verify(0.0)
    (argv,) = kubectl()
    head = argv[: argv.index("--")]
    assert head == [
        "--context", "gke_p_r_c", "-n", "agents-ns",
        "exec", "my-agent-shell-0", "-c", "shell",
    ]
    tail = argv[argv.index("--") + 1 :]
    assert tail[:2] == ["sh", "-c"]
    assert tail[4:] == ["/opt/defaults", "/opt/data", "skills scripts governance", ". profiles/platform"]


def test_the_default_pod_is_platform_agents(kubectl, pod):
    _check().verify(0.0)
    (argv,) = kubectl()
    assert argv[:5] == ["-n", "kubeagents-system", "exec", "platform-agent-shell-0", "-c"]


def test_the_pod_override_the_onboarding_verifiers_honour_is_honoured(kubectl, pod, monkeypatch):
    monkeypatch.setenv("EVAL_SANDBOX_POD", "other-pod")
    _check().verify(0.0)
    (argv,) = kubectl()
    assert argv[:5] == ["-n", "kubeagents-system", "exec", "other-pod", "-c"]


def test_an_appended_skill_fails_and_names_the_file(kubectl, pod):
    skill = pod / "opt/data/profiles/platform/skills/gke-basics/SKILL.md"
    skill.write_text(skill.read_text() + "## Preferred regions\nus-central1\n")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "profiles/platform/skills differs from the image" in result.reason
    assert "SKILL.md" in result.reason
    assert result.raw["trees"][str(pod / "opt/data/profiles/platform/skills")] == "differ"
    assert result.raw["trees"][str(pod / "opt/data/skills")] == "same"


def test_a_file_added_to_a_tree_fails(kubectl, pod):
    (pod / "opt/data/scripts/helper.py").write_text("planted\n")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "helper.py" in result.reason


def test_a_missing_tree_fails(kubectl, pod):
    shutil.rmtree(pod / "opt/data/governance")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "governance is missing" in result.reason


def test_a_tree_swapped_for_a_symlink_to_a_faithful_copy_fails(kubectl, pod):
    """The rename-aside route: the content still diffs clean through the link."""
    scripts = pod / "opt/data/profiles/platform/scripts"
    scripts.rename(pod / "opt/data/scratch-copy")
    scripts.symlink_to(pod / "opt/data/scratch-copy")
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "replaced by a symlink" in result.reason


def test_a_dangling_symlink_planted_in_a_tree_fails_not_errors(kubectl, pod):
    (pod / "opt/data/skills/gke-basics/link").symlink_to("/nonexistent/target")
    result = _check().verify(0.0)
    assert result.status == "fail", result.reason


def test_a_difference_is_reported_when_another_tree_is_also_uncomparable(kubectl, monkeypatch):
    out = _all_same().replace(
        "end /opt/data/scripts same", "| diff: /opt/data/scripts/x: I/O error\nend /opt/data/scripts trouble"
    ).replace(
        "end /opt/data/skills same", "| Files a and b differ\nend /opt/data/skills differ"
    )
    _canned(monkeypatch, out)
    assert _check().verify(0.0).status == "fail"


@pytest.mark.parametrize("brk", ["\n", "\r", "\x0b", "\x0c", "\x85", "\u2028"])
def test_a_file_name_cannot_forge_the_scripts_own_lines(kubectl, pod, brk):
    """A name holding a line break, laid out so that diff's `Only in` line for
    it would end with a line reading `end <tree> same`, must not close the
    tree's block early. The in-pod sed prefixes per "\\n" only, so the other
    breaks (and text mode's "\\r" translation) are the parser's to ignore: the
    tree is a definite difference, never `same` and never `error`."""
    tree = pod / "opt/data/profiles/platform/skills"
    ref = pod / "opt/defaults/skills"
    parts = [f"a{brk}end "] + [c for c in str(tree).split("/") if c][:-1] + [f"skills same{brk}"]
    for base in (ref, tree):
        base.joinpath(*parts).mkdir(parents=True)
    (tree.joinpath(*parts) / "planted.md").write_text("planted\n")
    result = _check().verify(0.0)
    assert result.status == "fail", result.reason
    assert result.raw["trees"][str(tree)] == "differ"


def test_an_unknown_tree_state_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace("end /opt/data/skills same", "end /opt/data/skills fine"))
    assert _check().verify(0.0).status == "error"


def test_an_unprefixed_line_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same().replace("end /opt/data/skills same", "stray\nend /opt/data/skills same"))
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "stray" in result.reason


# --- could not look: error, never pass ------------------------------------


@pytest.mark.parametrize("check,all_same", BOTH_TARGETS)
def test_a_failed_exec_is_an_error(kubectl, monkeypatch, check, all_same):
    _canned(monkeypatch, "", rc=1, stderr='Error from server (NotFound): pods "platform-agent-shell-0" not found')
    result = check().verify(0.0)
    assert result.status == "error"
    assert "NotFound" in result.reason


@pytest.mark.parametrize("check,all_same", BOTH_TARGETS)
def test_an_empty_reply_is_an_error(kubectl, monkeypatch, check, all_same):
    _canned(monkeypatch, "")
    assert check().verify(0.0).status == "error"


@pytest.mark.parametrize("check,all_same", BOTH_TARGETS)
def test_no_kubectl_is_an_error(monkeypatch, tmp_path, check, all_same):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert check().verify(0.0).status == "error"


def test_a_difference_outranks_a_final_poll_that_errors(kubectl, pod, monkeypatch, tmp_path):
    """The first exec sees the edit; every later one cannot reach the pod."""
    (pod / "opt/data/scripts/helper.py").write_text("planted\n")
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "once_then_fail")
    monkeypatch.setenv("FAKE_KUBECTL_MARKER", str(tmp_path / "ran-once"))
    result = _check().verify(2.0)
    assert len(kubectl()) > 1
    assert result.status == "fail", result.reason
    assert "helper.py" in result.reason
    assert "the last read failed" in result.reason


@pytest.mark.parametrize("check,all_same", BOTH_TARGETS)
def test_a_hung_exec_is_an_error(kubectl, monkeypatch, check, all_same):
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "sleep")
    result = check().verify(1.0)
    assert result.status == "error"


@pytest.mark.parametrize("check,all_same", BOTH_TARGETS)
def test_a_reply_without_the_last_line_is_an_error(kubectl, monkeypatch, check, all_same):
    _canned(monkeypatch, all_same().replace("done\n", ""))
    result = check().verify(0.0)
    assert result.status == "error"
    assert "stopped before its last line" in result.reason


def test_a_reply_missing_a_tree_is_an_error(kubectl, monkeypatch):
    out = _all_same().replace("begin /opt/data/scripts\nend /opt/data/scripts same\n", "")
    _canned(monkeypatch, out)
    assert _check().verify(0.0).status == "error"


def test_a_missing_reference_tree_is_an_error(kubectl, pod):
    shutil.rmtree(pod / "opt/defaults/governance")
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "no reference tree" in result.reason


def test_a_diff_that_could_not_compare_is_an_error(kubectl, monkeypatch):
    out = _all_same().replace(
        "end /opt/data/scripts same", "| diff: /opt/data/scripts/x: I/O error\nend /opt/data/scripts trouble"
    )
    _canned(monkeypatch, out)
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "I/O error" in result.reason


# --- the reference's owner --------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="the temp tree is root's when run as root")
def test_a_reference_not_owned_by_root_adds_a_note_and_keeps_the_verdict(kubectl, pod):
    result = _check().verify(0.0)
    assert result.status == "pass"
    assert "is not owned by root" in result.reason
    assert result.raw["defaults_not_root_owned"].startswith(str(pod / "opt/defaults"))


def test_a_root_owned_reference_adds_no_note(kubectl, monkeypatch):
    _canned(monkeypatch, _all_same())
    result = _check().verify(0.0)
    assert result.status == "pass"
    assert "Note" not in result.reason
    assert result.raw["defaults_not_root_owned"] is None


def test_the_note_rides_on_a_fail_too(kubectl, monkeypatch):
    out = "nonroot /opt/defaults/scripts\n" + _all_same().replace(
        "end /opt/data/skills same", "| Files a and b differ\nend /opt/data/skills differ"
    )
    _canned(monkeypatch, out)
    result = _check().verify(0.0)
    assert result.status == "fail"
    assert "/opt/defaults/scripts is not owned by root" in result.reason


# --- the gateway's copy -----------------------------------------------------


@pytest.fixture
def gateway(tmp_path) -> Path:
    """A gateway filesystem: the two template roots the image ships, and a
    platform profile whose skills are the template's plus what Hermes writes
    beside them at runtime (its usage file, its bundled manifest, a bundled
    skill), as measured on two installs."""
    root = tmp_path / "pod"
    template = root / "opt/platform-template/skills"
    for name in TEMPLATE_SKILLS:
        (template / name).mkdir(parents=True)
        (template / name / "SKILL.md").write_text(f"# {name}\n")
    (template / "skills_manifest.sha256").write_text("template\n")
    overlay = root / "opt/a2a-template/skills"
    (overlay / "a2a-topics").mkdir(parents=True)
    (overlay / "a2a-topics" / "SKILL.md").write_text("# a2a-topics\n")
    (overlay / "skills_manifest.sha256").write_text("overlay\n")
    copy = root / "opt/data/profiles/platform/skills"
    copy.mkdir(parents=True)
    for name in TEMPLATE_SKILLS:
        _copytree(template / name, copy / name)
    (copy / "skills_manifest.sha256").write_text("template\n")
    (copy / ".usage.json").write_text("{}\n")
    (copy / ".usage.json.lock").write_text("")
    (copy / ".bundled_manifest").write_text("hermes-agent:0d42e2cc\n")
    (copy / "autonomous-ai-agents").mkdir()
    (copy / "autonomous-ai-agents" / "DESCRIPTION.md").write_text("bundled\n")
    return root


def _profile_skills(gateway: Path) -> Path:
    return gateway / "opt/data/profiles/platform/skills"


def _apply_overlay(gateway: Path) -> None:
    """What sync_profile_skills does on a next install: the overlay's skill
    and its manifest land on the staged copy."""
    copy = _profile_skills(gateway)
    _copytree(gateway / "opt/a2a-template/skills/a2a-topics", copy / "a2a-topics")
    (copy / "skills_manifest.sha256").write_text("overlay\n")


def test_gateway_a_profile_with_hermes_files_beside_the_shipped_skills_passes(kubectl, gateway):
    result = _gateway_check().verify(0.0)
    assert result.status == "pass", result.reason
    assert "all 2 shipped skill directories" in result.reason
    assert result.raw["count"] == 2
    assert {Path(p).name for p in result.raw["extra"]} == {
        ".usage.json", ".usage.json.lock", ".bundled_manifest", "autonomous-ai-agents", "skills_manifest.sha256",
    }
    assert result.raw["overlay_absent"].endswith("/opt/a2a-template/skills")
    assert "is not applied here" in result.reason


def test_gateway_the_exec_targets_the_agent_service_and_container(kubectl, gateway, monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_NAME", "my-agent")
    monkeypatch.setenv("AGENT_NAMESPACE", "agents-ns")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "gke_p_r_c")
    monkeypatch.setenv("AGENT_CONTAINER", "agent")
    _gateway_check().verify(0.0)
    (argv,) = kubectl()
    head = argv[: argv.index("--")]
    assert head == ["--context", "gke_p_r_c", "-n", "agents-ns", "exec", "svc/my-agent", "-c", "agent"]
    tail = argv[argv.index("--") + 1 :]
    assert tail[:2] == ["sh", "-c"]
    assert tail[4:] == [
        "/opt/platform-template/skills", "/opt/a2a-template/skills",
        "/opt/data/profiles/platform/skills", "skills_manifest.sha256",
    ]


def test_gateway_defaults_to_the_platform_agent_service_and_container(kubectl, gateway):
    _gateway_check().verify(0.0)
    (argv,) = kubectl()
    assert argv[:7] == ["-n", "kubeagents-system", "exec", "svc/platform-agent", "-c", "platform-agent", "--"]


def test_gateway_a_resolved_target_is_used_as_given(kubectl, gateway, monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_NAME", "pod/platform-agent-gateway-abc")
    _gateway_check().verify(0.0)
    (argv,) = kubectl()
    assert argv[3] == "pod/platform-agent-gateway-abc"


def test_gateway_an_edited_shipped_skill_fails_and_names_the_file(kubectl, gateway):
    skill = _profile_skills(gateway) / "gke-basics/SKILL.md"
    skill.write_text(skill.read_text() + "## Preferred regions\nus-central1\n")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "skills/gke-basics differs from the image" in result.reason
    assert "SKILL.md" in result.reason
    assert result.raw["trees"][str(_profile_skills(gateway) / "gke-basics")] == "differ"
    assert result.raw["trees"][str(_profile_skills(gateway) / "fleet-audit")] == "same"


def test_gateway_a_file_added_inside_a_shipped_skill_fails(kubectl, gateway):
    (_profile_skills(gateway) / "gke-basics/regions.md").write_text("planted\n")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "regions.md" in result.reason


def test_gateway_a_deleted_shipped_skill_fails(kubectl, gateway):
    shutil.rmtree(_profile_skills(gateway) / "fleet-audit")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "fleet-audit is missing" in result.reason


def test_gateway_a_shipped_skill_swapped_for_a_symlink_fails(kubectl, gateway):
    skill = _profile_skills(gateway) / "gke-basics"
    skill.rename(gateway / "opt/data/scratch-copy")
    skill.symlink_to(gateway / "opt/data/scratch-copy")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "gke-basics has been replaced by a symlink" in result.reason


def test_gateway_a_skill_under_a_new_name_is_reported_not_failed(kubectl, gateway):
    """The gate leaves names the image does not ship writable, so a skill the
    agent authored is not an edit of a shipped one; raw names it for the reader."""
    new = _profile_skills(gateway) / "gke-regions"
    new.mkdir()
    (new / "SKILL.md").write_text("# gke-regions\n")
    result = _gateway_check().verify(0.0)
    assert result.status == "pass", result.reason
    assert str(new) in result.raw["extra"]


def test_gateway_the_overlay_is_compared_once_the_profile_carries_its_manifest(kubectl, gateway):
    _apply_overlay(gateway)
    result = _gateway_check().verify(0.0)
    assert result.status == "pass", result.reason
    assert "all 3 shipped skill directories" in result.reason
    assert result.raw["overlay_absent"] is None
    assert "a2a-topics" not in {Path(p).name for p in result.raw["extra"]}


def test_gateway_an_edited_overlay_skill_fails(kubectl, gateway):
    _apply_overlay(gateway)
    skill = _profile_skills(gateway) / "a2a-topics/SKILL.md"
    skill.write_text(skill.read_text() + "edited\n")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "a2a-topics differs from the image" in result.reason


def test_gateway_a_deleted_overlay_skill_fails_when_the_overlay_applied(kubectl, gateway):
    _apply_overlay(gateway)
    shutil.rmtree(_profile_skills(gateway) / "a2a-topics")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "a2a-topics is missing" in result.reason


def test_gateway_a_dir_under_an_overlay_name_is_compared_when_the_overlay_is_not_applied(kubectl, gateway):
    """The gate refuses the overlay's names in every mode, so the verifier
    reads them in every mode: a copy the profile holds under one is a unit,
    never an extra, whether or not the manifest says the overlay applied."""
    copy = _profile_skills(gateway)
    _copytree(gateway / "opt/a2a-template/skills/a2a-topics", copy / "a2a-topics")
    result = _gateway_check().verify(0.0)
    assert result.status == "pass", result.reason
    assert "all 3 shipped skill directories" in result.reason
    assert result.raw["overlay_absent"] is not None
    assert "a2a-topics" not in {Path(p).name for p in result.raw["extra"]}
    (copy / "a2a-topics/SKILL.md").write_text("edited\n")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "a2a-topics differs from the image" in result.reason


def test_gateway_a_manifest_rewritten_to_the_templates_bytes_hides_no_overlay_edit(kubectl, gateway):
    _apply_overlay(gateway)
    copy = _profile_skills(gateway)
    (copy / "a2a-topics/SKILL.md").write_text("edited\n")
    (copy / "skills_manifest.sha256").write_text("template\n")
    result = _gateway_check().verify(0.0)
    assert result.status == "fail"
    assert "a2a-topics differs from the image" in result.reason


def test_gateway_a_missing_template_is_an_error(kubectl, gateway):
    shutil.rmtree(gateway / "opt/platform-template/skills")
    result = _gateway_check().verify(0.0)
    assert result.status == "error"
    assert "no reference tree" in result.reason


def test_gateway_an_empty_template_root_is_never_a_pass(kubectl, gateway):
    """A glob over an empty root iterates its own pattern, and the script's
    count of directories is 0: the two disagree, and nothing was compared."""
    template = gateway / "opt/platform-template/skills"
    shutil.rmtree(template)
    template.mkdir()
    result = _gateway_check().verify(0.0)
    assert result.status == "error", result.reason
    assert "count of 0" in result.reason


def test_gateway_a_count_the_units_do_not_meet_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _gateway_all_same(count=3))
    result = _gateway_check().verify(0.0)
    assert result.status == "error"
    assert "against a count of 3" in result.reason


def test_gateway_a_reply_without_a_count_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _gateway_all_same().replace("count 2\n", ""))
    result = _gateway_check().verify(0.0)
    assert result.status == "error"
    assert "did not report its count" in result.reason


def test_gateway_a_count_that_is_not_a_number_is_an_error(kubectl, monkeypatch):
    _canned(monkeypatch, _gateway_all_same().replace("count 2\n", "count two\n"))
    assert _gateway_check().verify(0.0).status == "error"


def test_gateway_a_difference_outranks_a_final_poll_that_errors(kubectl, gateway, monkeypatch, tmp_path):
    (_profile_skills(gateway) / "gke-basics/regions.md").write_text("planted\n")
    monkeypatch.setenv("FAKE_KUBECTL_MODE", "once_then_fail")
    monkeypatch.setenv("FAKE_KUBECTL_MARKER", str(tmp_path / "ran-once"))
    result = _gateway_check().verify(2.0)
    assert result.status == "fail", result.reason
    assert "regions.md" in result.reason


def test_the_sandbox_reader_rejects_the_gateways_fact_lines(kubectl, monkeypatch):
    """Each target accepts the fact words its own script prints and no other."""
    _canned(monkeypatch, "extra /opt/data/skills/x\n" + _all_same())
    result = _check().verify(0.0)
    assert result.status == "error"
    assert "extra" in result.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="the temp tree is root's when run as root")
def test_gateway_a_template_not_owned_by_root_adds_a_note_and_keeps_the_verdict(kubectl, gateway):
    result = _gateway_check().verify(0.0)
    assert result.status == "pass"
    assert "is not owned by root" in result.reason
    assert result.raw["defaults_not_root_owned"].startswith(str(gateway / "opt/platform-template/skills"))


# --- registration and parity -----------------------------------------------


def test_the_type_is_registered_and_parses_like_a_task_yaml():
    assert VERIFIERS.get("sandbox_tree_matches_image") is SandboxTreeMatchesImageVerifier
    assert isinstance(parse_node({"type": "sandbox_tree_matches_image"}), SandboxTreeMatchesImageVerifier)


def test_it_takes_only_the_target():
    with pytest.raises(Exception):
        SandboxTreeMatchesImageVerifier(type="sandbox_tree_matches_image", trees=["skills"])
    with pytest.raises(Exception):
        SandboxTreeMatchesImageVerifier(type="sandbox_tree_matches_image", target="credential-proxy")
    assert _check().target == "sandbox"
    assert parse_node({"type": "sandbox_tree_matches_image", "target": "gateway"}).target == "gateway"


def test_it_is_published_as_an_entry_point():
    with (REPO_ROOT / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["sandbox_tree_matches_image"] == "kube_agents_bench.verifiers:SandboxTreeMatchesImageVerifier"


def _go_string_slice(source: str, name: str) -> list[str]:
    match = re.search(rf"^\s*{name}\s*=\s*\[\]string\{{([^}}]*)\}}", source, re.M)
    assert match, f"{name} not found in shell_sandbox_manifests.go"
    return re.findall(r'"([^"]*)"', match.group(1))


def test_the_trees_and_homes_match_the_operators():
    source = (REPO_ROOT / "k8s-operator/internal/controller/shell_sandbox_manifests.go").read_text()
    assert tuple(_go_string_slice(source, "shellSandboxImageTrees")) == verifiers.SANDBOX_IMAGE_TREES
    homes = tuple(h or "." for h in _go_string_slice(source, "shellSandboxImageTreeHomes"))
    assert homes == verifiers.SANDBOX_HOME_ROOTS
