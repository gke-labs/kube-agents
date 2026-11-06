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
is what the teardown removes by, so a clean plant would stay in the store.

teardown: removes the digest record, the planted manifest, and each stored
envelope of the stream that holds <mark>.

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
    run = subprocess.run(
        [sys.executable, str(collector), *collector_args, *COLLECTOR_FLAGS],
        capture_output=True, text=True, check=False, timeout=COLLECTOR_TIMEOUT_S,
    )
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


def plant(audit_report, mark: str, scripts: str, collector_args: list[str]) -> int:
    code, line = run_cli(audit_report, ["start", "--audit", AUDIT])
    if code != 0:
        print(f"start exited {code}; a {AUDIT} run may be in flight. Nothing planted.", file=sys.stderr)
        return 1
    manifest, why = collect(scripts, collector_args)
    if manifest is None:
        print(why, file=sys.stderr)
        return 1
    document = filled_document(audit_report, manifest, mark)
    if not document["findings"]:
        print(f"{COLLECTOR} gave no candidate; nothing to plant.", file=sys.stderr)
        return 1
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
        print(f"finish exited {code}; the previous run was not planted.", file=sys.stderr)
        teardown(audit_report, mark)
        return 1
    try:
        payload = json.loads(line.strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = {}
    repo = str(payload.get("repo") or "")
    stored_marked = any(
        mark in path.read_text(encoding="utf-8")
        for path in _store_files(audit_report)
    )
    if not stored_marked:
        teardown(audit_report, mark)
        print("the report store does not hold the planted run.", file=sys.stderr)
        return 1
    recorded = digest_record_path(audit_report).is_file()
    print(
        f"planted a scheduled {AUDIT} run: {len(document['findings'])} finding(s), status {payload.get('status')}, "
        f"silent_ok {payload.get('silent_ok')}{', repo ' + repo if repo else ''}; "
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


def teardown(audit_report, mark: str) -> int:
    removed = 0
    for path in _store_files(audit_report):
        try:
            if mark in path.read_text(encoding="utf-8"):
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    extra = 0
    for path in (digest_record_path(audit_report), planted_manifest_path(audit_report)):
        try:
            path.unlink()
            extra += 1
        except FileNotFoundError:
            continue
    print(f"removed {removed} stored envelope(s) that hold the planted run and {extra} scratch file(s).")
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
