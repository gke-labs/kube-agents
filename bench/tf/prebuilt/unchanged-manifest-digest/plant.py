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

"""Plant and remove the previous scheduled run of bench/tasks/fleet-audit-unchanged-manifest-skips-synthesis.

main.tf runs this in the container that runs `audit_report.py finish`, as
`python3 - <mode> <scripts dir> <mark> [<collector_args>...]`. That container
holds the report store and the scratch directory on its own volume.

plant: runs the previous scheduled run of the fleet-consistency-drift stream
the way a worker does, with no model: `start`, the drift collector with the
install's scope arguments and `--config-connector` (the SOP's command), `draft`
from its manifest with each finding's title, recommendation and note starting
with <mark>, and `finish --scheduled --manifest-file`. `finish` stores the run's
envelope and, on an image that ships the precheck, records the manifest's
digest; on an image that does not, nothing reads the digest and the case goes
red for that reason. Exits 1 when the collector gives no candidate: the mark
is what the teardown removes by, so a clean plant would stay in the store. A
plant that fails after its `start` releases the stream's in-flight note, so
the next apply and the install's own run are not refused for two hours.

teardown: removes the digest record, the planted manifest, each stored
envelope of the stream that holds <mark>, and every envelope of the planted
repository stored at or after the planted run's `finished_at`: the scheduled
run the stack fires closes from the planted document, and where `finish`
carried the previous run's wording, neither envelope shows the mark.

Run as root (a `kubectl exec`), it first becomes the owner of the scratch
directory, so what it leaves behind -- the GitOps clone `finish` makes, the
store, the record -- is the agent's to write on its own next run.
"""

import contextlib
import io
import json
import os
import pwd
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIT = "fleet-consistency-drift"
COLLECTOR = "fleet_drift.py"
# The SOP's own flag on the collector command.
COLLECTOR_FLAGS = ("--config-connector",)
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
DATA_ROOT = "/opt/data"
MANUAL_KIND = "manual"
# The manifest the plant's run passed to `finish`, beside the path the SOP's
# command writes to, so the agent's own collector output is never mistaken for it.
PLANTED_MANIFEST_SUFFIX = ".planted"
# Where the plant records the envelope its `finish` stored (its repository and
# `finished_at`), so the teardown removes that envelope and nothing else. The
# mark alone cannot name it: `finish` keeps the previous run's wording for a
# finding whose evidence and severity did not change, so on a stream that
# already carries the findings the stored document shows no mark.
PLANT_STATE_NAME = ".bench-unchanged-manifest-digest-plant.json"
DIGEST_RECORD_TEMPLATE = "last_scheduled_digest_{audit_id}.json"
MIN_ARGC = 4
ROOT_UID = 0
STDERR_TAIL_CHARS = 500
COLLECTOR_TIMEOUT_S = 900


def ensure_proxy_path(proxy_bin: str = CREDENTIAL_PROXY_BIN) -> None:
    """Put the shell sandbox's credential-proxy shims on PATH.

    `kubectl exec` does not start a login shell, so `/etc/profile.d` does not
    add `/opt/credential-proxy/bin` to PATH before this runs.
    """
    if not Path(proxy_bin).is_dir():
        return
    current = os.environ.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if proxy_bin not in entries:
        os.environ["PATH"] = os.pathsep.join([proxy_bin, *entries])


def become_data_owner(data_root: str = DATA_ROOT) -> str:
    """Drop root to the owner of the data volume, so nothing planted is root-owned.

    The agent's own runs write the scratch directory, the store and the GitOps
    clone as one uid; a root-owned clone would refuse its next fetch. Returns a
    line saying what happened. Not root, or a volume root owns: nothing to do.
    """
    if os.geteuid() != ROOT_UID:
        return f"running as uid {os.geteuid()}"
    root = Path(data_root)
    for candidate in (root / "scratch", root):
        try:
            owner = candidate.stat()
        except OSError:
            continue
        if owner.st_uid == ROOT_UID:
            return f"{candidate} is root-owned; staying root"
        try:
            home = pwd.getpwuid(owner.st_uid).pw_dir
        except KeyError:
            home = None
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
        if home:
            os.environ["HOME"] = home
        return f"became uid {owner.st_uid} (the owner of {candidate})"
    return f"{data_root} is not there; staying root"


def load(scripts: str):
    ensure_proxy_path()
    sys.path.insert(0, scripts)
    sys.path.insert(0, str(Path(scripts).parents[2] / "scripts"))
    import audit_report  # noqa: PLC0415 - the module is in the pod, not here

    return audit_report


def run_cli(audit_report, argv: list[str]) -> tuple[int, str]:
    """`audit_report.main(argv)` with its JSON line captured; stderr flows through."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = audit_report.main(argv)
    return code, out.getvalue()


def collect(scripts: str, collector_args: list[str]) -> tuple[dict | None, str]:
    collector = Path(scripts) / COLLECTOR
    try:
        run = subprocess.run(
            [sys.executable, str(collector), *collector_args, *COLLECTOR_FLAGS],
            capture_output=True, text=True, check=False, timeout=COLLECTOR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f"{COLLECTOR} did not finish within {COLLECTOR_TIMEOUT_S}s"
    try:
        manifest = json.loads(run.stdout)
    except ValueError:
        return None, f"{COLLECTOR} gave no manifest (exit {run.returncode}): {run.stderr[-STDERR_TAIL_CHARS:]}"
    if not isinstance(manifest, dict):
        return None, f"{COLLECTOR} gave no manifest object"
    if manifest.get("error") and not manifest.get("clusters"):
        return None, f"{COLLECTOR} enumerated nothing: {manifest.get('error')}"
    return manifest, ""


def filled_document(audit_report, manifest: dict, mark: str) -> dict:
    """`draft`'s document with the judgement a worker writes, each field starting with <mark>."""
    document = audit_report.draft_findings(manifest, AUDIT)
    for finding in document["findings"]:
        finding["title"] = f"{mark} {finding['check']} {finding['object']}"
        if not finding.get("impact"):
            finding["impact"] = f"{mark} impact."
        finding["recommendation"] = {
            "action": f"{mark} action.",
            "rationale": f"{mark} rationale.",
            "risk": f"{mark} risk.",
        }
        finding["remediation"] = {"kind": MANUAL_KIND, "note": f"{mark} note."}
    return document


def planted_manifest_path(audit_report) -> Path:
    return Path(audit_report.manifest_path_for(AUDIT) + PLANTED_MANIFEST_SUFFIX)


def digest_record_path(audit_report) -> Path:
    return Path(audit_report.SCRATCH_DIR) / DIGEST_RECORD_TEMPLATE.format(audit_id=AUDIT)


def plant_state_path(audit_report) -> Path:
    return Path(audit_report.SCRATCH_DIR) / PLANT_STATE_NAME


def stored_envelope(audit_report) -> tuple[dict | None, str]:
    """The newest envelope of the ledger this install publishes the stream to, and its repository."""
    repo = audit_report.resolve_repo_or_no_forge(AUDIT)
    latest = audit_report.reports_dir_for(AUDIT, repo) / audit_report.REPORT_LATEST_NAME
    try:
        envelope = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, repo
    return (envelope if isinstance(envelope, dict) else None), repo


def abandon(audit_report, why: str) -> int:
    """A plant that fails after its `start`: say why, release the note `start` left, exit 1.

    `start` claimed the stream's in-flight note for two hours. The run it
    opened is over here, and a note left behind refuses the next apply at its
    own `start` and the install's real tick at its.
    """
    print(why, file=sys.stderr)
    audit_report.release_in_flight(AUDIT)
    return 1


def plant(audit_report, mark: str, scripts: str, collector_args: list[str]) -> int:
    started = datetime.now(timezone.utc)
    code, line = run_cli(audit_report, ["start", "--audit", AUDIT])
    if code != 0:
        print(f"start exited {code}; a {AUDIT} run may be in flight. Nothing planted.", file=sys.stderr)
        return 1
    manifest, why = collect(scripts, collector_args)
    if manifest is None:
        return abandon(audit_report, why)
    document = filled_document(audit_report, manifest, mark)
    if not document["findings"]:
        return abandon(audit_report, f"{COLLECTOR} gave no candidate; nothing to plant.")
    manifest_path = planted_manifest_path(audit_report)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    findings_path = Path(audit_report.findings_path_for(AUDIT))
    findings_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    code, line = run_cli(
        audit_report,
        [
            "finish",
            "--audit", AUDIT,
            "--findings-file", str(findings_path),
            "--manifest-file", str(manifest_path),
            "--scheduled",
        ],
    )
    if code != 0:
        # An exit 2 (a rejected document, a lost broker) keeps the note for
        # the re-run a worker would make; this run makes none.
        teardown(audit_report, mark)
        return abandon(audit_report, f"finish exited {code}; the previous run was not planted.")
    try:
        payload = json.loads(line.strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = {}
    envelope, repo = stored_envelope(audit_report)
    finished_at = str((envelope or {}).get("finished_at") or "")
    try:
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        finished = None
    if envelope is None or not envelope.get("scheduled") or finished is None or finished < started:
        teardown(audit_report, mark)
        return abandon(
            audit_report, f"the report store of {repo} does not hold this scheduled run (newest: {finished_at or 'none'})."
        )
    plant_state_path(audit_report).write_text(json.dumps({"repo": repo, "finished_at": finished_at}), encoding="utf-8")
    recorded = digest_record_path(audit_report).is_file()
    print(
        f"planted a scheduled {AUDIT} run: {len(document['findings'])} finding(s), status {payload.get('status')}, "
        f"silent_ok {payload.get('silent_ok')}, repo {repo}, finished_at {finished_at}; "
        f"manifest digest {'recorded' if recorded else 'not recorded (this image ships no precheck)'}."
    )
    return 0


def _store_files(audit_report) -> list[Path]:
    """Every envelope file the stream's store holds, under every repository directory."""
    try:
        root = Path(audit_report.REPORTS_DIR) / AUDIT
        return sorted(p for p in root.rglob("*.json") if p.is_file())
    except OSError:
        return []


def _parsed(stamp: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None


def _stored_since(path: Path, repo_dir: Path | None, since: datetime | None, text: str) -> bool:
    """Whether `path` is an envelope of the planted repository stored at or after the planted run.

    The scheduled run the stack fires closes from the planted document and
    stores its own envelope after it; where `finish` carried the previous
    run's wording forward, neither shows the mark, so the time is the join.
    """
    if repo_dir is None or since is None or not path.is_relative_to(repo_dir):
        return False
    try:
        finished = _parsed(json.loads(text).get("finished_at"))
    except (ValueError, AttributeError):
        return False
    return finished is not None and finished >= since


def teardown(audit_report, mark: str) -> int:
    """Remove the planted run and what it led to: any envelope with the mark, every envelope of the
    planted repository stored at or after the planted run, and the scratch files."""
    try:
        state = json.loads(plant_state_path(audit_report).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    since = _parsed(state.get("finished_at"))
    repo_dir = None
    if state.get("repo") and since is not None:
        try:
            repo_dir = Path(audit_report.reports_dir_for(AUDIT, str(state["repo"]))).resolve()
        except (ValueError, OSError):
            repo_dir = None
    removed = 0
    for path in _store_files(audit_report):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        if mark in text or _stored_since(path.resolve(), repo_dir, since, text):
            path.unlink()
            removed += 1
    extra = 0
    for path in (digest_record_path(audit_report), planted_manifest_path(audit_report), plant_state_path(audit_report)):
        try:
            path.unlink()
            extra += 1
        except FileNotFoundError:
            continue
    print(f"removed {removed} stored envelope(s) of the planted run and the runs closed from it, and {extra} scratch file(s).")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < MIN_ARGC:
        print("usage: python3 - plant|teardown <scripts dir> <mark> [collector args...]", file=sys.stderr)
        return 2
    mode, scripts, mark = argv[1:4]
    collector_args = [arg for arg in argv[4:] if arg]
    print(become_data_owner(), file=sys.stderr)
    audit_report = load(scripts)
    if mode == "plant":
        return plant(audit_report, mark, scripts, collector_args)
    if mode == "teardown":
        return teardown(audit_report, mark)
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
