"""The ``post_merge_outcome`` verifier: merge the agent's fix, play the reconciler, read the outcome.

The post-merge check (design gke-labs/kube-agents#5054) runs in the agent pod
on a five-minute tick. Once a fix pull request the install opened merges, it
waits for the fix to be applied, watches the workload through a health window,
re-runs the audit's check and stores one outcome in Session KV's
``post_merge_records`` table. Under ``KUBEAGENTS_POST_MERGE_CHECK=report`` it
also comments once on the pull request, ending the comment with a hidden marker.

Nobody merges an agent's pull request inside a run, and the eval GitOps
repositories have no reconciler, so this check does both, then drives the
ticks itself rather than waiting on the five-minute cron. On its first poll it:

1. finds the one pull request the run's reply names, under ``owner``;
2. merges it with the harness's write token (:data:`WRITE_TOKEN_ENV`), never
   the agent's;
3. applies every manifest the pull request adds to the cluster carrying
   ``apply_fixture_role``, as a reconciler would. No reconciler names the merge
   commit, so the check's apply step sees the object the fix adds instead.
   Reconciler matching is covered by ``post_merge_check``'s unit tests;
4. runs ``post_merge_check.py`` (the cron job's own entry, no arguments) in
   the agent container every :data:`TICK_INTERVAL_SEC` until the record has an
   outcome or :data:`DRIVE_DEADLINE_SEC` passes. The fix's own health window
   (five minutes for ``no-pdb``) is what bounds this, so one ``_check`` call
   outlasts devops-bench's 120 s polling budget: that budget spaces polls, it
   does not interrupt one.

It also requires the ``post-merge-check`` cron job in the platform profile's
``jobs.json``, enabled, so the schedule the install ships is still graded.

Every poll after that reads the record by the pull request's URL from the agent
pod's SQLite file, read-only, through ``kubectl exec``. Until the record has an
outcome and has been reported the poll is a fail that is polled again. Then
the check passes when that outcome is ``outcome`` and the report matches
``report``, the install's ``KUBEAGENTS_POST_MERGE_CHECK``:

- ``silent`` (the CI installs' setting, hack/ci-deploy.sh): ``reported_at``
  carries ``post_merge_report``'s ``silent:`` stamp and no comment on the pull
  request carries the marker;
- ``report``: exactly one comment carries both ``comment_phrase`` and the marker.

A different outcome or report is a fail. A pod, record or forge that cannot be read is ``status="error"``.
Setting up (no reply URL, no write token, a merge GitHub refuses, a manifest that
does not apply) is an error too, because none of them says anything about the
agent.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus
from pydantic import Field, PrivateAttr, model_validator

from kube_agents_bench import fleet, forges, onboarding, transcript
from kube_agents_bench.verifiers import (
    _NO_TRANSCRIPT_REASON,
    _added_file_text,
    _forge_get_json,
    _OnboardingPollVerifier,
)
from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

logger = logging.getLogger(__name__)

__all__ = ["COMMENT_MARKER", "WRITE_TOKEN_ENV", "PostMergeOutcomeVerifier", "read_record"]

#: The environment variable holding the harness's GitHub token for the merge:
#: ``pull_requests: write`` and ``contents: write``, narrowed to the eval
#: GitOps repository. Never the agent's credential.
WRITE_TOKEN_ENV = "EVAL_POST_MERGE_WRITE_TOKEN"
#: The hidden line ``post_merge_report.py`` ends every comment with.
COMMENT_MARKER = "<!-- kube-agents:post-merge-check -->"
#: ``post_merge_report.SILENT_PREFIX``: what ``reported_at`` starts with under ``silent``.
SILENT_STAMP = "silent:"
RECORD_READ = "__POST_MERGE_RECORD_READ__"
OUTCOMES = ("verified_fixed", "still_broken", "not_applied")
RECORD_FIELDS = ("url", "merged_at", "applied_at", "health_result", "outcome", "outcome_at", "outcome_detail", "reported_at")
#: Where the agent container keeps Session KV's database unless it says otherwise.
SESSION_KV_DB_DEFAULT = "/var/lib/kube-agents/session/session_kv.db"
#: The agent container's script directory and the platform profile's cron file.
SCRIPTS_DIR = "/opt/data/scripts"
JOBS_FILE = "/opt/data/profiles/platform/cron/jobs.json"
CRON_JOB_ID = "post-merge-check"
#: The scheduler's per-job flock for it (deploy/docker/patches/cron_tick_lock_scope.py).
JOB_LOCK = "/opt/data/profiles/platform/cron/.job-post-merge-check.lock"
TICK_READ = "__POST_MERGE_TICK__"
#: Between ticks: under the 60 s the check waits after an apply, so the health
#: window gets its two readings soon after it closes.
TICK_INTERVAL_SEC = 45.0
#: The no-pdb window is five minutes; this leaves room for notice, apply and report.
DRIVE_DEADLINE_SEC = 600.0
TICK_TIMEOUT_SEC = 180.0
MANIFEST_SUFFIXES = (".yaml", ".yml")
MAX_FILES_PAGES = 5
FILES_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 5
COMMENT_PAGE_SIZE = 100
HTTP_TIMEOUT_SEC = 30.0
APPLY_TIMEOUT_SEC = 120.0

_READ_SCRIPT = r"""
import json, os, sqlite3, sys
url, fields, default, sentinel = sys.argv[1:5]
path = os.environ.get("SESSION_KV_DB_PATH") or default
out = {"record": None, "error": None}
try:
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT %s FROM post_merge_records WHERE url = ?" % fields, (url,)).fetchone()
        out["record"] = dict(row) if row is not None else None
    finally:
        conn.close()
except sqlite3.Error as exc:
    out["error"] = "post_merge_records in %s: %s" % (path, exc)
print(sentinel)
print(json.dumps(out))
"""


def read_command(url: str) -> str:
    """The ``sh -c`` line that prints one record of ``post_merge_records`` in the agent container."""
    args = " ".join(shlex.quote(a) for a in (url, ", ".join(RECORD_FIELDS), SESSION_KV_DB_DEFAULT, RECORD_READ))
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_READ_SCRIPT)} {args}'
    )


def read_record(shell, url: str, timeout: float) -> dict[str, Any] | None:
    """``{"record": row-or-None, "error": None}``, ``{"record": None, "error": why}``, or None if the pod gave no answer."""
    lines = shell(read_command(url), timeout).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == RECORD_READ), None)
    if start is None:
        return None
    try:
        parsed = json.loads("\n".join(lines[start + 1 :]))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or ("record" not in parsed and "error" not in parsed):
        return None
    return parsed


_TICK_SCRIPT = (
    "import fcntl, subprocess, sys; "
    "lock = open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX); "
    "done = subprocess.run([sys.executable, 'post_merge_check.py'], cwd=sys.argv[2], capture_output=True, text=True); "
    "print((done.stdout + done.stderr)[-2000:]); print(sys.argv[3], done.returncode)"
)


def tick_command() -> str:
    """One run of the ``post-merge-check`` cron job's script under the job's own lock, then a sentinel and its exit code.

    The scheduler claims ``.job-post-merge-check.lock`` (cron_tick_lock_scope.py,
    ``JobLocks``) without waiting and skips the run when it is held; this waits
    for it. So the cron and the harness never run a tick at the same time, and
    neither can report a record the other is reporting.
    """
    args = " ".join(shlex.quote(a) for a in (JOB_LOCK, SCRIPTS_DIR, TICK_READ))
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_TICK_SCRIPT)} {args}'
    )


_CRON_JOB_SCRIPT = (
    "import json, sys; jobs = json.load(open(sys.argv[1])); "
    "jobs = jobs.get('jobs', []) if isinstance(jobs, dict) else jobs; "
    "print(sys.argv[3]); "
    "print(json.dumps([j for j in jobs if isinstance(j, dict) and j.get('id') == sys.argv[2]]))"
)


def cron_job_command() -> str:
    """Prints the ``post-merge-check`` job from the platform profile's jobs.json as JSON, after a sentinel."""
    args = " ".join(shlex.quote(a) for a in (JOBS_FILE, CRON_JOB_ID, RECORD_READ))
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_CRON_JOB_SCRIPT)} {args}'
    )


def cron_job_enabled(shell, timeout: float) -> bool | None:
    """Whether the platform profile schedules ``post-merge-check``, enabled; None if unreadable."""
    lines = shell(cron_job_command(), timeout).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == RECORD_READ), None)
    if start is None:
        return None
    try:
        jobs = json.loads("\n".join(lines[start + 1 :]))
    except json.JSONDecodeError:
        return None
    return any(job.get("enabled", True) for job in jobs) if isinstance(jobs, list) else None


def _github_write(method: str, url: str, token: str, body: dict) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "null")
        except (ValueError, OSError):
            payload = None
        return exc.code, payload
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"message": str(exc)}


def _apply(manifest: str, kubeconfig: str) -> tuple[bool, str]:
    try:
        done = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"],
            input=manifest, capture_output=True, text=True, timeout=APPLY_TIMEOUT_SEC, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stderr or done.stdout).strip()[-300:]


@VERIFIERS.register("post_merge_outcome")
class PostMergeOutcomeVerifier(_OnboardingPollVerifier):
    """See the module docstring."""

    type: Literal["post_merge_outcome"]
    # The organisation the pull request must sit under, as pull_request_opened's.
    owner: str = ""
    outcome: Literal["verified_fixed", "still_broken", "not_applied"]
    # The install's switch value the report is graded against; see the docstring.
    report: Literal["silent", "report"] = "silent"
    # Under `report`, text the one comment must carry beside the marker.
    comment_phrase: str = ""
    # The fixture role whose cluster plays the reconciler's target.
    apply_fixture_role: str = Field(min_length=1)

    @model_validator(mode="after")
    def _phrase_for_report(self):
        if self.report == "report" and not self.comment_phrase:
            raise ValueError("comment_phrase is required when report is 'report'")
        return self

    _merged: tuple[str, str, int] | None = PrivateAttr(default=None)
    _ticks: list[str] = PrivateAttr(default_factory=list)
    # What the merge put on the default branch and the apply put on the
    # cluster, for _clean_up: the pool's fixture must read as unbudgeted again.
    _added: list[tuple[str, str]] = PrivateAttr(default_factory=list)
    _repo: tuple[str, str] | None = PrivateAttr(default=None)
    _kubeconfig: str = PrivateAttr(default="")
    _touched: list[tuple[str, str]] = PrivateAttr(default_factory=list)
    _base: str = PrivateAttr(default="")

    def verify(self, timeout_sec: float):
        try:
            return super().verify(timeout_sec)
        finally:
            self._clean_up()

    def _clean_up(self) -> None:
        """Best effort, every step on its own: delete what was applied, then put the default branch back.

        Without it the next audit on this pool project finds checkout-gateway
        budgeted, declared and live, and every no-pdb case after it goes red.
        Each file the pull request touched goes back to its content at the
        pull request's base: an added file is deleted, a modified or removed
        one is written back. A step that fails is logged and the rest still run.
        """
        import os

        for _, text in self._added if self._kubeconfig else []:
            try:
                subprocess.run(
                    ["kubectl", "--kubeconfig", self._kubeconfig, "delete", "--ignore-not-found", "-f", "-"],
                    input=text, capture_output=True, text=True, timeout=APPLY_TIMEOUT_SEC, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("post_merge_outcome: could not delete an applied manifest: %s", exc)
        token = (os.environ.get(WRITE_TOKEN_ENV) or "").strip()
        if not token or self._repo is None:
            return
        owner, repo = self._repo
        for path, file_status in self._touched:
            try:
                why = self._restore(owner, repo, path, file_status, token)
            except Exception as exc:  # noqa: BLE001 -- one file must not stop the others
                why = repr(exc)
            if why:
                logger.warning("post_merge_outcome: %s/%s %s not restored: %s", owner, repo, path, why)

    def _restore(self, owner: str, repo: str, path: str, file_status: str, token: str) -> str:
        """Put ``path`` on the default branch back to its content at the base; "" when done."""
        url = f"{forges.github_api_root()}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
        status, now = _forge_get_json(url, token, HTTP_TIMEOUT_SEC)
        current = now.get("sha") if status == 200 and isinstance(now, dict) else None
        message = f"eval: restore {path} after grading"
        if file_status in ("added", "renamed", "copied"):
            if not current:
                return ""
            done, answer = _github_write("DELETE", url, token, {"message": message, "sha": current})
            return "" if done == 200 else f"DELETE answered {done}"
        if not self._base:
            return "the pull request's base commit is unknown"
        status, before = _forge_get_json(f"{url}?ref={self._base}", token, HTTP_TIMEOUT_SEC)
        if status != 200 or not isinstance(before, dict) or not before.get("content"):
            return f"its content at {self._base} could not be read ({status})"
        body = {"message": message, "content": str(before["content"]).replace("\n", "")}
        if current:
            body["sha"] = current
        done, answer = _github_write("PUT", url, token, body)
        return "" if done in (200, 201) else f"PUT answered {done}"

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        scheduled = cron_job_enabled(onboarding.agent_shell, read_timeout)
        if scheduled is None:
            return "error", f"could not read {JOBS_FILE} in the agent container", None
        if not scheduled:
            return "fail", f"the platform profile does not schedule an enabled {CRON_JOB_ID!r} job", None
        if self._merged is None:
            status, why = self._merge_and_apply(read_timeout)
            if status != "pass":
                return status, why, None
            self._drive(read_timeout)
        owner, repo, number = self._merged
        url = f"https://{forges.github_host()}/{owner}/{repo}/pull/{number}"
        answer = read_record(onboarding.agent_shell, url, read_timeout)
        if answer is None:
            return "error", "the agent pod gave no answer to the post_merge_records read", None
        if answer.get("error"):
            return "error", str(answer["error"]), None
        record = answer.get("record")
        if record is None:
            return "fail", f"no post_merge_records row for {url}: the install did not record the fix it opened", None
        if not record.get("outcome"):
            return "fail", f"{url} has no outcome yet (merged {record.get('merged_at')}, applied {record.get('applied_at')})", record
        if record["outcome"] != self.outcome:
            return "fail", f"{url} ended {record['outcome']!r} ({record.get('outcome_detail')}), not {self.outcome!r}", record
        if not record.get("reported_at"):
            return "fail", f"{url} ended {self.outcome!r} but has not been reported yet", record
        count, why = self._marked_comments(owner, repo, number, read_timeout)
        if why:
            return "error", why, record
        if self.report == "silent":
            if not str(record["reported_at"]).startswith(SILENT_STAMP):
                return "fail", f"{url} was reported {record['reported_at']!r}, not under silent", record
            if count:
                return "fail", f"{url} carries {count} post-merge comments under silent; none is allowed", record
            return "pass", f"{url} ended {self.outcome!r}, reported silently with no comment", record
        if count != 1:
            return "fail", f"{url} carries {count} post-merge comments saying {self.comment_phrase!r}; exactly one is required", record
        return "pass", f"{url} ended {self.outcome!r} with one {self.comment_phrase!r} comment", record

    def _drive(self, read_timeout: float) -> None:
        """Ticks until the record has an outcome or the deadline passes; each tick's tail goes in ``_ticks``."""
        owner, repo, number = self._merged
        url = f"https://{forges.github_host()}/{owner}/{repo}/pull/{number}"
        deadline = time.monotonic() + DRIVE_DEADLINE_SEC
        while True:
            out = onboarding.agent_shell(tick_command(), TICK_TIMEOUT_SEC)
            self._ticks.append(out[-400:])
            answer = read_record(onboarding.agent_shell, url, read_timeout) or {}
            if (answer.get("record") or {}).get("reported_at") or time.monotonic() + TICK_INTERVAL_SEC > deadline:
                return
            time.sleep(TICK_INTERVAL_SEC)

    def _merge_and_apply(self, timeout: float) -> tuple[VerificationStatus, str]:
        snap = transcript.get()
        if snap is None:
            return "error", _NO_TRANSCRIPT_REASON
        import os

        write_token = (os.environ.get(WRITE_TOKEN_ENV) or "").strip()
        read_token = forges.read_token(forges.FORGE_GITHUB)
        if not write_token or not read_token:
            return "error", f"{WRITE_TOKEN_ENV} and the grading token are both needed to merge and read the fix"
        refs = [
            (*ref.split("/", 1), number)
            for ref, number in forges.proposal_refs(snap.final_message, forges.FORGE_GITHUB)
            if not self.owner or ref.split("/", 1)[0] == self.owner
        ]
        if len(refs) != 1:
            return "error", f"the reply names {len(refs)} pull requests under {self.owner or 'any owner'}; one fix is needed to merge"
        owner, repo, number = refs[0]
        api = f"{forges.github_api_root()}/repos/{owner}/{repo}/pulls/{number}"
        status, payload = _github_write("PUT", f"{api}/merge", write_token, {"merge_method": "squash"})
        if status not in (200, 405):
            return "error", f"GitHub answered {status} to merging {owner}/{repo}#{number}: {(payload or {}).get('message')}"
        if status == 405:
            status, pull = _forge_get_json(api, read_token, timeout)
            if status != 200 or not (isinstance(pull, dict) and pull.get("merged")):
                return "error", f"{owner}/{repo}#{number} is not mergeable: {(payload or {}).get('message')}"
        self._repo = (owner, repo)
        status, pull = _forge_get_json(api, read_token, timeout)
        self._base = str(((pull or {}).get("base") or {}).get("sha") or "") if status == 200 and isinstance(pull, dict) else ""
        for page in range(1, MAX_FILES_PAGES + 1):
            status, files = _forge_get_json(f"{api}/files?per_page={FILES_PAGE_SIZE}&page={page}", read_token, timeout)
            if status != 200 or not isinstance(files, list):
                return "error", f"GitHub answered {status} for the files of {owner}/{repo}#{number}"
            for entry in files:
                path = str(entry.get("filename") or "") if isinstance(entry, dict) else ""
                if path:
                    self._touched.append((path, str(entry.get("status") or "")))
                    if entry.get("previous_filename"):
                        self._touched.append((str(entry["previous_filename"]), "removed"))
                if path.endswith(MANIFEST_SUFFIXES):
                    text = _added_file_text(entry)
                    if text:
                        self._added.append((path, text))
            if len(files) < FILES_PAGE_SIZE:
                break
        if not self._added:
            return "error", f"{owner}/{repo}#{number} adds no manifest to apply"
        try:
            self._kubeconfig = fleet.kubeconfig_for_role(self.apply_fixture_role)
        except fleet.FleetRoleUnresolved as exc:
            return "error", str(exc)
        for _, manifest in self._added:
            ok, why = _apply(manifest, self._kubeconfig)
            if not ok:
                return "error", f"applying {owner}/{repo}#{number}'s manifest failed: {why}"
        self._merged = (owner, repo, number)
        return "pass", ""

    def _marked_comments(self, owner: str, repo: str, number: int, timeout: float) -> tuple[int, str]:
        token = forges.read_token(forges.FORGE_GITHUB) or ""
        base = f"{forges.github_api_root()}/repos/{owner}/{repo}/issues/{number}/comments"
        count = 0
        for page in range(1, MAX_COMMENT_PAGES + 1):
            status, comments = _forge_get_json(f"{base}?per_page={COMMENT_PAGE_SIZE}&page={page}", token, timeout)
            if status != 200 or not isinstance(comments, list):
                return 0, f"GitHub answered {status} for the comments on {owner}/{repo}#{number}"
            count += sum(
                1
                for c in comments
                if isinstance(c, dict) and COMMENT_MARKER in str(c.get("body") or "")
                and (self.report == "silent" or self.comment_phrase in str(c.get("body") or ""))
            )
            if len(comments) < COMMENT_PAGE_SIZE:
                break
        return count, ""
