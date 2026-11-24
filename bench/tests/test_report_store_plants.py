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

"""The unchanged-finding-wording and unchanged-manifest-digest stacks on both report stores.

FLEET_AUDIT_REPORT_STORE on the sandbox's shell container picks the store: unset,
empty or `file` is the store on the plant's own container's volume, as before;
`broker` (#6110) is the credential broker's, whose files are on the gateway pod's
data volume, so the teardown moves to the gateway's `platform-agent` container;
anything else stops the stack. The stacks' apply and destroy scripts run here
against a `kubectl` stand-in with one gateway pod and one sandbox pod, and the
plant scripts' store branches run against stand-ins for `audit_report`.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
PREBUILT = REPO / "bench" / "tf" / "prebuilt"
WORDING = PREBUILT / "unchanged-finding-wording"
DIGEST = PREBUILT / "unchanged-manifest-digest"
HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
INTERPOLATION_RE = re.compile(r"\$\{(?:var|local|self\.triggers)\.([a-z_0-9]+)\}")
APPLY, DESTROY = 0, 1
GATEWAY_POD = "platform-agent-gw-0"
SANDBOX_POD = "platform-agent-shell-0"
MARK = "MARK"
SCRIPTS = "/scripts"
B64 = base64.b64encode(b"script").decode()
# What Terraform would put in each `${var.*}`, `${local.*}` and `${self.triggers.*}`
# place, by name; a trigger carries the value of the variable or local it copies.
VALUES = {
    "agent_namespace": "ns",
    "namespace": "ns",
    "pod_selector": "app=agent",
    "store_containers": "shell platform-agent",
    "sandbox_container": "shell",
    "gateway_container": "platform-agent",
    "scripts_dir": SCRIPTS,
    "mark": MARK,
    "script_b64": B64,
    "plant_b64": B64,
    "arm_b64": B64,
    "scope_b64": B64,
    "python": "python3",
    "home": "/opt/data",
    "audit_job": "fleet-consistency-drift",
    "agent_deployment": "gw",
    "deployment": "gw",
    "agent_container": "platform-agent",
    "container": "platform-agent",
    "pod_wait": "5",
    "own_wait": "1",
    "run_wait": "5",
    "project_id": "example-project",
    "host_project": "example-project",
    "host_cluster_name": "host",
    "host_cluster": "host",
    "host_cluster_location": "us-east4",
    "host_location": "us-east4",
}
SWITCH_ENTRY = '.spec.containers[?(@.name=="shell")].env[?(@.name=="FLEET_AUDIT_REPORT_STORE")]'
SWITCH_JSONPATH = (
    f'jsonpath={{{SWITCH_ENTRY}.value}}{{"\\n"}}{{{SWITCH_ENTRY}.valueFrom}}'
    '{.spec.containers[?(@.name=="shell")].envFrom}'
)
INDIRECT = ['\n{"configMapKeyRef":{"key":"store"}}', 'broker\n[{"configMapRef":{"name":"env"}}]']
# `kubectl get pods` lists the gateway pod and the sandbox pod; `get pod` answers
# each one's containers, or the sandbox shell container's switch ($STUB_STORE,
# or a failure when $STUB_FAIL is `get`). `kubectl exec` logs one line, `<pod>
# <container> <in-pod words>`, to $STUB_LOG and fails the one whose words
# include a word of $STUB_FAIL, and prints $STUB_STATE for plant.py's `state`. `gcloud`
# succeeds.
KUBECTL_STUB = f"""#!/bin/sh
if [ "$1" = get ] && [ "$2" = pods ]; then echo "{GATEWAY_POD} {SANDBOX_POD}"; exit 0; fi
if [ "$1" = get ]; then
  case "$*" in
    *FLEET_AUDIT_REPORT_STORE*)
      printf '%s\\n' "$*" >> "$STUB_GETS"
      [ "$STUB_FAIL" = get ] && exit 1
      printf '%s' "$STUB_STORE"
      ;;
    *{SANDBOX_POD}*) echo shell ;;
    *) echo platform-agent ;;
  esac
  exit 0
fi
pod="$5"
container="$7"
in_pod=""
seen=0
for word in "$@"; do
  if [ "$seen" = 1 ]; then in_pod="$in_pod $word"; fi
  if [ "$word" = "--" ]; then seen=1; fi
done
cat >/dev/null
echo "$pod $container$in_pod" >> "$STUB_LOG"
for step in $STUB_FAIL; do
  case "$in_pod " in *" $step "*) exit 1 ;; esac
done
case "$in_pod " in *" state "*) printf '%s\\n' "$STUB_STATE" ;; esac
exit 0
"""


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wording = _load(WORDING / "plant.py", "unchanged_finding_wording_plant")
digest = _load(DIGEST / "plant.py", "unchanged_manifest_digest_plant")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Runs one of a stack's scripts; returns (status, exec lines as word lists, stderr)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("kubectl", KUBECTL_STUB), ("gcloud", "#!/bin/sh\nexit 0\n")):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    log, gets = tmp_path / "exec.log", tmp_path / "gets.log"

    def run(stack_dir: Path, block: int, store: str = "", fail: str = "no-step-fails", state: str = ""):
        script = HEREDOC_RE.findall((stack_dir / "main.tf").read_text(encoding="utf-8"))[block]
        script = INTERPOLATION_RE.sub(lambda m: VALUES[m.group(1)], script)
        assert "${" not in script.replace("$${", ""), "a Terraform interpolation is left unrendered"
        log.unlink(missing_ok=True)
        gets.unlink(missing_ok=True)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_GETS": str(gets),
            "STUB_STORE": store,
            "STUB_FAIL": fail,
            "STUB_STATE": state,
        }
        done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        run.gets = gets.read_text(encoding="utf-8").splitlines() if gets.exists() else []
        return done.returncode, [line.split() for line in lines], done.stderr

    return run


def _plant_steps(commands):
    """`<pod> <container> <step>` of each plant.py exec (`env PYTHONDONTWRITEBYTECODE=1 python3 - <step>`)."""
    return [(words[0], words[1], words[6]) for words in commands if words[2:5] == ["env", "PYTHONDONTWRITEBYTECODE=1", "python3"]]


# --- unchanged-finding-wording: main.tf ----------------------------------------


@pytest.mark.parametrize("value", ["", "file"])
def test_wording_file_store_plants_and_tears_down_in_the_sandbox(stack, value):
    status, commands, _ = stack(WORDING, APPLY, store=value)
    assert status == 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant")]
    assert stack.gets and SWITCH_JSONPATH in stack.gets[0]
    status, commands, _ = stack(WORDING, APPLY, store=value, fail="plant")
    assert status != 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant"), (SANDBOX_POD, "shell", "teardown")]
    status, commands, _ = stack(WORDING, DESTROY, store=value)
    assert status == 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "teardown")]


def test_wording_broker_store_plants_in_the_sandbox_and_tears_down_in_the_gateway(stack):
    status, commands, _ = stack(WORDING, APPLY, store="broker")
    assert status == 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant")]
    status, commands, _ = stack(WORDING, APPLY, store="broker", fail="plant")
    assert status != 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant"), (GATEWAY_POD, "platform-agent", "teardown")]
    status, commands, _ = stack(WORDING, DESTROY, store="broker")
    assert status == 0
    assert _plant_steps(commands) == [(GATEWAY_POD, "platform-agent", "teardown")]


@pytest.mark.parametrize("block", [APPLY, DESTROY])
def test_wording_unknown_switch_fails_before_any_exec(stack, block):
    status, commands, err = stack(WORDING, block, store="Broker")
    assert status != 0
    assert commands == []
    assert "FLEET_AUDIT_REPORT_STORE='Broker'" in err


@pytest.mark.parametrize("stack_dir", [WORDING, DIGEST], ids=["wording", "digest"])
@pytest.mark.parametrize("block", [APPLY, DESTROY])
@pytest.mark.parametrize("printed", INDIRECT, ids=["valueFrom", "envFrom"])
def test_a_switch_set_indirectly_fails_before_the_plant_or_its_teardown(stack, stack_dir, block, printed):
    status, commands, err = stack(stack_dir, block, store=printed)
    assert status != 0
    assert _plant_steps(commands) == []
    assert "set indirectly (valueFrom or envFrom)" in err


def test_wording_unreadable_pod_spec_fails_before_the_plant(stack):
    status, commands, _ = stack(WORDING, APPLY, fail="get")
    assert status != 0
    assert commands == []


# --- unchanged-manifest-digest: main.tf ----------------------------------------


@pytest.mark.parametrize("value", ["", "file"])
def test_digest_file_store_plants_and_tears_down_in_the_sandbox(stack, value):
    status, commands, _ = stack(DIGEST, APPLY, store=value)
    assert status == 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant")]
    status, commands, _ = stack(DIGEST, APPLY, store=value, fail="fire")
    assert status != 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant"), (SANDBOX_POD, "shell", "teardown")]
    status, commands, _ = stack(DIGEST, DESTROY, store=value)
    assert status == 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "teardown")]


def test_digest_broker_store_tears_the_store_down_in_the_gateway_and_the_scratch_in_the_sandbox(stack):
    status, commands, _ = stack(DIGEST, APPLY, store="broker")
    assert status == 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant")]
    halves = [
        (SANDBOX_POD, "shell", "state"),
        ("deployment/gw", "platform-agent", "teardown-store"),
        (SANDBOX_POD, "shell", "teardown-scratch"),
    ]
    status, commands, _ = stack(DIGEST, APPLY, store="broker", fail="fire")
    assert status != 0
    assert _plant_steps(commands) == [(SANDBOX_POD, "shell", "plant"), *halves]
    status, commands, _ = stack(DIGEST, DESTROY, store="broker")
    assert status == 0
    assert _plant_steps(commands) == halves


@pytest.mark.parametrize("block", [APPLY, DESTROY])
def test_digest_broker_teardown_passes_the_plants_record_to_teardown_store(stack, block):
    record = '{"repo":"acme/fleet","finished_at":"2026-11-02T00:00:00+00:00"}'
    fail = "fire" if block == APPLY else "no-step-fails"
    _, commands, err = stack(DIGEST, block, store="broker", fail=fail, state=record)
    store_half = next(words for words in commands if words[6:7] == ["teardown-store"])
    assert store_half[7:] == [SCRIPTS, MARK, record]
    assert "WARNING" not in err


@pytest.mark.parametrize("block", [APPLY, DESTROY])
def test_digest_broker_teardown_warns_and_passes_an_empty_record_when_it_cannot_read_one(stack, block):
    fail = "fire state" if block == APPLY else "state"
    _, commands, err = stack(DIGEST, block, store="broker", fail=fail)
    store_half = next(words for words in commands if words[6:7] == ["teardown-store"])
    assert store_half[7:] == [SCRIPTS, MARK, "{}"]
    assert "WARNING: could not read the plant's record from the scratch" in err


@pytest.mark.parametrize("block", [APPLY, DESTROY])
def test_digest_unknown_switch_fails_before_the_plant_or_its_teardown(stack, block):
    status, commands, err = stack(DIGEST, block, store="s3")
    assert status != 0
    assert _plant_steps(commands) == []
    assert "FLEET_AUDIT_REPORT_STORE='s3'" in err


# --- the plant scripts' switch --------------------------------------------------


@pytest.mark.parametrize("module", [wording, digest], ids=["wording", "digest"])
@pytest.mark.parametrize(("value", "kind"), [(None, "file"), ("", "file"), ("file", "file"), ("broker", "broker")])
def test_the_plant_reads_the_switch_from_its_env(monkeypatch, module, value, kind):
    if value is None:
        monkeypatch.delenv("FLEET_AUDIT_REPORT_STORE", raising=False)
    else:
        monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", value)
    assert module.report_store() == kind


@pytest.mark.parametrize("module", [wording, digest], ids=["wording", "digest"])
def test_any_other_switch_value_stops_the_plant_naming_it(monkeypatch, module):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "Broker")
    with pytest.raises(SystemExit, match=re.escape("FLEET_AUDIT_REPORT_STORE='Broker'")):
        module.report_store()


# --- unchanged-finding-wording: plant.py ----------------------------------------


class BrokerDir:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return "broker acme/fleet"

    def read(self, name):
        if self.text is None:
            raise FileNotFoundError(name)
        return self.text


def _wording_audit_report(tmp_path, broker_text=None):
    store = tmp_path / "reports" / wording.AUDIT / "acme" / "fleet"

    def write_report(audit, envelope, now):
        store.mkdir(parents=True, exist_ok=True)
        (store / "latest.json").write_text(json.dumps(envelope), encoding="utf-8")

    return SimpleNamespace(
        _candidates=lambda manifest: [({"name": "c"}, {"check": "x", "object": "o", "severity": "minor"})],
        adopt_collector_evidence=lambda findings, manifest: None,
        published_id=lambda finding: "id-1",
        resolve_repo=lambda audit_id: "acme/fleet",
        report_envelope=lambda *args, **kwargs: {"findings": args[2]["findings"]},
        write_report=write_report,
        reports_dir_for=lambda audit, repo: store,
        report_store_dir=lambda audit, repo: BrokerDir(broker_text),
        REPORT_LATEST_NAME="latest.json",
        REPORT_RUNS_DIR="runs",
    ), store


@pytest.fixture
def collector(monkeypatch):
    done = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
    monkeypatch.setattr(wording.subprocess, "run", lambda *args, **kwargs: done)


def test_wording_file_plant_reads_back_the_local_file(tmp_path, monkeypatch, collector):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "file")
    audit_report, store = _wording_audit_report(tmp_path)
    assert wording.plant(audit_report, MARK, SCRIPTS) == 0
    assert MARK in (store / "latest.json").read_text(encoding="utf-8")


def test_wording_broker_plant_reads_back_through_the_broker(tmp_path, monkeypatch, collector):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "broker")
    audit_report, _ = _wording_audit_report(tmp_path, broker_text=f'{{"title": "{MARK}"}}')
    assert wording.plant(audit_report, MARK, SCRIPTS) == 0


def test_wording_broker_plant_that_the_broker_does_not_hold_fails_and_leaves_teardown_to_main_tf(
    tmp_path, monkeypatch, collector, capsys
):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "broker")
    audit_report, _ = _wording_audit_report(tmp_path, broker_text=None)
    monkeypatch.setattr(wording, "teardown", lambda *args: pytest.fail("tore down where the store is not"))
    assert wording.plant(audit_report, MARK, SCRIPTS) == 1
    assert "broker acme/fleet does not hold the planted run" in capsys.readouterr().err


def test_wording_unknown_switch_plants_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "s3")
    monkeypatch.setattr(wording.subprocess, "run", lambda *args, **kwargs: pytest.fail("the collector ran"))
    audit_report, _ = _wording_audit_report(tmp_path)
    with pytest.raises(SystemExit):
        wording.plant(audit_report, MARK, SCRIPTS)


def test_wording_teardown_removes_a_marked_envelope_and_its_writer_record(tmp_path):
    audit_report, store = _wording_audit_report(tmp_path)
    (store / "runs").mkdir(parents=True)
    for name, text in (("latest.json", MARK), ("runs/1.json", MARK), ("runs/0.json", "older")):
        (store / name).write_text(text, encoding="utf-8")
    for name in ("latest.writer", "runs/1.writer", "runs/0.writer"):
        (store / name).write_text("{}", encoding="utf-8")
    assert wording.teardown(audit_report, MARK) == 0
    left = sorted(str(p.relative_to(store)) for p in store.rglob("*") if p.is_file())
    assert left == ["runs/0.json", "runs/0.writer"]


# --- unchanged-manifest-digest: plant.py ----------------------------------------


def _digest_audit_report(tmp_path, broker_text=None):
    root = tmp_path / "reports"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return SimpleNamespace(
        REPORTS_DIR=str(root),
        SCRATCH_DIR=str(scratch),
        REPORT_LATEST_NAME="latest.json",
        REPORT_RUNS_DIR="runs",
        NO_FORGE_REPO="no-forge/none",
        resolve_repo_or_no_forge=lambda audit: "acme/fleet",
        reports_dir_for=lambda audit, repo: root / audit / repo,
        report_store_dir=lambda audit, repo: BrokerDir(broker_text),
        is_diverted=lambda envelope: False,
        manifest_path_for=lambda audit: str(scratch / f"{audit}.manifest.json"),
    )


def test_digest_stored_envelope_reads_the_store_the_switch_names(tmp_path):
    audit_report = _digest_audit_report(tmp_path, broker_text='{"scheduled": true, "via": "broker"}')
    local = Path(audit_report.reports_dir_for(digest.AUDIT, "acme/fleet"))
    local.mkdir(parents=True)
    (local / "latest.json").write_text('{"scheduled": true, "via": "file"}', encoding="utf-8")
    assert digest.stored_envelope(audit_report)[0]["via"] == "file"
    assert digest.stored_envelope(audit_report, digest.REPORT_STORE_FILE)[0]["via"] == "file"
    assert digest.stored_envelope(audit_report, digest.REPORT_STORE_BROKER)[0]["via"] == "broker"


@pytest.mark.parametrize(("store", "expected"), [("file", ["teardown"]), ("broker", ["teardown-scratch"])])
def test_digest_failed_plant_undoes_here_only_what_is_here(monkeypatch, store, expected):
    calls = []
    monkeypatch.setattr(digest, "teardown", lambda audit_report, mark: calls.append("teardown"))
    monkeypatch.setattr(digest, "teardown_scratch", lambda audit_report: calls.append("teardown-scratch"))
    digest.undo_here(object(), MARK, store)
    assert calls == expected


def test_digest_unknown_switch_stops_the_plant_before_start(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "Broker")
    monkeypatch.setattr(digest, "run_cli", lambda *args: pytest.fail("start ran"))
    with pytest.raises(SystemExit):
        digest.plant(_digest_audit_report(tmp_path), MARK, SCRIPTS, [])


def _planted_store(tmp_path):
    audit_report = _digest_audit_report(tmp_path)
    repo_dir = Path(audit_report.reports_dir_for(digest.AUDIT, "acme/fleet"))
    (repo_dir / "runs").mkdir(parents=True)
    (repo_dir / "runs" / "0.json").write_text('{"finished_at": "2026-11-01T00:00:00+00:00"}', encoding="utf-8")
    (repo_dir / "runs" / "0.writer").write_text("writer-0", encoding="utf-8")
    for name in ("runs/1.json", "latest.json"):
        (repo_dir / name).write_text(f'{{"finished_at": "2026-11-02T00:00:00+00:00", "t": "{MARK}"}}', encoding="utf-8")
    for name in ("runs/1.writer", "latest.writer"):
        (repo_dir / name).write_text("writer-1", encoding="utf-8")
    scratch = [digest.digest_record_path(audit_report), digest.planted_manifest_path(audit_report)]
    for path in scratch:
        path.write_text("{}", encoding="utf-8")
    state = {"repo": "acme/fleet", "finished_at": "2026-11-02T00:00:00+00:00"}
    digest.plant_state_path(audit_report).write_text(json.dumps(state), encoding="utf-8")
    return audit_report, repo_dir, state


def test_digest_file_teardown_undoes_store_and_scratch_in_one_place(tmp_path, capsys):
    audit_report, repo_dir, _ = _planted_store(tmp_path)
    assert digest.teardown(audit_report, MARK) == 0
    assert (repo_dir / "latest.json").read_text(encoding="utf-8") == (repo_dir / "runs" / "0.json").read_text(
        encoding="utf-8"
    )
    assert not (repo_dir / "runs" / "1.json").exists()
    assert list(Path(audit_report.SCRATCH_DIR).iterdir()) == []
    assert "and 3 scratch file(s); store fleet: restored." in capsys.readouterr().out


def test_digest_broker_teardown_halves(tmp_path, monkeypatch, capsys):
    audit_report, repo_dir, state = _planted_store(tmp_path)
    monkeypatch.setattr(digest, "become_data_owner", lambda: "running as a test")
    monkeypatch.setattr(digest, "load", lambda scripts: audit_report)
    assert digest.main(["plant.py", "state", SCRIPTS, MARK]) == 0
    assert json.loads(capsys.readouterr().out) == state
    assert digest.main(["plant.py", "teardown-store", SCRIPTS, MARK, json.dumps(state)]) == 0
    # The store half: the marked envelopes and their writer records go, and latest
    # comes back as the newest run left, with that run's writer record.
    assert not (repo_dir / "runs" / "1.json").exists() and not (repo_dir / "runs" / "1.writer").exists()
    assert (repo_dir / "latest.writer").read_text(encoding="utf-8") == "writer-0"
    assert len(list(Path(audit_report.SCRATCH_DIR).iterdir())) == 3
    assert digest.teardown_scratch(audit_report) == 0
    assert list(Path(audit_report.SCRATCH_DIR).iterdir()) == []
    out = capsys.readouterr().out
    assert "store fleet: restored." in out and "removed 3 scratch file(s)." in out


def _closing_store(tmp_path):
    """An older run, the planted run (no mark: `finish` kept the old wording), the run closed from
    it, and a later run, none of which carries the mark, plus one marked ring entry."""
    audit_report = _digest_audit_report(tmp_path)
    repo_dir = Path(audit_report.reports_dir_for(digest.AUDIT, "acme/fleet"))
    (repo_dir / "runs").mkdir(parents=True)
    stamps = {"0": "2026-11-01", "1": "2026-11-02", "2": "2026-11-03", "3": "2026-11-04"}
    for name, day in stamps.items():
        (repo_dir / "runs" / f"{name}.json").write_text(f'{{"finished_at": "{day}T00:00:00+00:00"}}', encoding="utf-8")
    (repo_dir / "runs" / "m.json").write_text(f'{{"finished_at": "2026-10-01T00:00:00+00:00", "t": "{MARK}"}}', encoding="utf-8")
    (repo_dir / "latest.json").write_text('{"finished_at": "2026-11-04T00:00:00+00:00"}', encoding="utf-8")
    return audit_report, repo_dir


def _left(repo_dir):
    return sorted(p.name for p in (repo_dir / "runs").glob("*.json"))


def test_digest_teardown_store_removes_the_planted_run_and_the_one_closed_from_it(tmp_path, capsys):
    audit_report, repo_dir = _closing_store(tmp_path)
    state = {"repo": "acme/fleet", "finished_at": "2026-11-02T00:00:00+00:00"}
    assert digest.teardown_store(audit_report, MARK, state) == 0
    assert _left(repo_dir) == ["0.json", "3.json"]
    assert "WARNING" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ({"repo": "acme/other", "finished_at": "2026-11-02T00:00:00+00:00"}, "not the stream's repository"),
        ({"repo": "acme/fleet", "finished_at": "2026-11-02T12:00:00+00:00"}, "no envelope of acme/fleet finished at"),
        ({"repo": "acme/fleet"}, "no repository or no finished_at"),
        ({}, "no repository or no finished_at"),
    ],
    ids=["another-repo", "no-such-envelope", "no-time", "empty"],
)
def test_digest_teardown_store_with_a_record_that_does_not_validate_removes_only_the_marked(
    tmp_path, capsys, state, why
):
    audit_report, repo_dir = _closing_store(tmp_path)
    assert digest.teardown_store(audit_report, MARK, state) == 0
    assert _left(repo_dir) == ["0.json", "1.json", "2.json", "3.json"]
    err = capsys.readouterr().err
    assert "WARNING: the plant's record is not one to delete by" in err and why in err


def test_digest_teardown_store_with_an_unresolvable_stream_removes_only_the_marked(tmp_path, capsys):
    audit_report, repo_dir = _closing_store(tmp_path)

    def refuse(audit):
        raise RuntimeError("several repositories and no gitops role")

    audit_report.resolve_repo_or_no_forge = refuse
    state = {"repo": "acme/fleet", "finished_at": "2026-11-02T00:00:00+00:00"}
    assert digest.teardown_store(audit_report, MARK, state) == 0
    assert _left(repo_dir) == ["0.json", "1.json", "2.json", "3.json"]
    assert "does not resolve here" in capsys.readouterr().err
