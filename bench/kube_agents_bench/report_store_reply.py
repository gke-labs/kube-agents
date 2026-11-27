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

"""The ``report_store_reply`` check: does the reply give each audit's latest report from the store.

The prompt is a request a user sends for fleet-audit summaries that never reached the chat: "Show
me the latest report of each of these fleet audits from the report store: <ids>." This grades the
reply against the fleet-audit report store, audit by audit. Each audit the check names must be
mentioned in ``final_message``, and the reply must carry the ledger URL of one of its envelopes
written before the turn (the newest, per repository store) or during it, unless the store had no
ledger to give.

The store is read after the reply with the install's own reader: ``report_status.py`` from the
agent image (root-owned, so the model cannot have edited the grader's reader), run where the
audits' shell keeps the store. Which store that is, the operator renders on the sandbox's shell
container as ``FLEET_AUDIT_REPORT_STORE`` (``onboarding.report_store``). With it unset, empty or
``file``, the store is on the sandbox's own volume and is read in the shell sandbox. With it
``broker`` (#6110), the shell writes through the credential broker, whose files are under the same
root on the gateway pod's data volume, so the store is read in the agent container. Any other value,
or a pod spec that cannot be read, is an error: a guess would grade the run against a store it did
not write. The sandbox is not optional on this operator (``ShellSandboxSpec``), so there is nowhere
else to read: a pod that cannot be read is an error, never a fallback, and a store root absent
there is a store nothing has written to yet, which the pass reason and ``raw`` say.

Which envelopes count is decided by when each file was written, not by its ``finished_at``:
``finish`` stamps a run before it publishes and writes the store after, so a run stamped before
the turn can land during it. Per audit and repository store, the accepted set is the newest
envelope written before the turn started, plus every envelope written during it. Then:

- a URL of any accepted envelope of the audit, anywhere in the reply, passes;
- otherwise the audit fails when one of its repository stores needs a URL: its newest envelope
  before the turn has a ledger, and no envelope without one was written since. A clean or held
  run with no open ledger, or a store with nothing before the turn (the stream never ran, or its
  first run was in flight), needs none.

Do not credit a URL to an audit by its position in the reply: a header that names the audits
before their URLs, or display names in place of ids, defeat it. Which audit a URL is given to, and
whether the reply says "no record" for a stream the store has nothing for, are the judge's to
grade. A URL from any one repository store passes the audit; a reply need not list every
repository's ledger. Other URLs (a closed ledger the worker looked up, a pull request) are allowed.

Its own module rather than a section of ``verifiers.py``, registered through the same
``devops_bench.verifiers`` entry-point group.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus
from pydantic import Field

from kube_agents_bench import onboarding, transcript
from kube_agents_bench.verifiers import (
    _NO_RUN_CLOCK_REASON,
    _NO_TRANSCRIPT_REASON,
    REPORT_STORE_ROOT,
    AuditId,
    _OnboardingPollVerifier,
)
from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

# The install's copy of the store's reader in the agent image (deploy/docker/Dockerfile copies
# agents/platform/skills to /opt/platform-template/skills and chowns it to root).
READER_PATH = "/opt/platform-template/skills/fleet-audit/scripts/report_status.py"
# The interpreter the driver runs under: the sandbox's python3 on PATH, or in the agent
# container its own interpreter, where a bare `python3` on PATH is not guaranteed.
SANDBOX_PYTHON = "python3"
AGENT_PYTHON = f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; "$PY"'
# How the broker store's read place is named in reasons and ``raw``.
AGENT_CONTAINER_PLACE = "the agent container"
READER_READ = "__REPORT_STATUS_SOURCE__"
STORE_READ = "__REPORT_STORE_REPLY__"
# A reply cites a stored ledger URL when the URL appears in it with no digit right after, so
# /issues/12 is not found inside /issues/120. Nothing else about the URL's shape is read here: the
# store holds whatever the forge handed back (GitHub's /issues/<n>, GitLab's /-/work_items/<n>), and
# a reply may add a trailing slash or a fragment.
CITATION_SUFFIX = r"(?!\d)"
# A backslash before punctuation is a Markdown escape (`work\_items`, `compliance\-audit`), which
# chat models write freely; the reply is read with those escapes removed.
MARKDOWN_ESCAPE_RE = re.compile(r"\\([^A-Za-z0-9\s])")

# Runs report_status.py's helpers under a module name that is not __main__, so its CLI does not
# run, then prints, per audit and repository, the envelopes the check needs: every file's write
# time, and the parsed key set of the newest written before `since` and of each written after.
# argv: source, root, since (epoch seconds), audit ids.
_DRIVER = """
import json, os, sys, types
source, root, since = sys.argv[1], sys.argv[2], float(sys.argv[3])
audits = sys.argv[4:]
rs = types.ModuleType("report_status")
exec(compile(source, "report_status.py", "exec"), rs.__dict__)
SENTINEL = %(sentinel)r
KEYS = ("issue_url", "issue_number", "finished_at", "status", "repo")
out = {"root_exists": os.path.isdir(root), "audits": {}}
for audit in audits:
    entry = {"repos": {}, "error": None}
    out["audits"][audit] = entry
    try:
        dirs, unreadable = rs.scan_repo_dirs(root, audit)
    except OSError as exc:
        entry["error"] = rs.os_reason(exc)
        continue
    if unreadable:
        entry["error"] = "; ".join(unreadable)
    for repo in (d for d in dirs if d == d.lower()):
        try:
            base = rs.store_path(root, audit, repo)
            files = [os.path.join(base, rs.LATEST_NAME)]
            files += [os.path.join(base, rs.RUNS_DIR, name) for name in rs.list_runs(root, audit, repo)]
            written = []
            for path in files:
                try:
                    written.append((os.stat(path).st_mtime, path))
                except FileNotFoundError:
                    pass
            written.sort()
            before = [w for w in written if w[0] < since]
            chosen = before[-1:] + [w for w in written if w[0] >= since]
            envelopes = []
            for mtime, path in chosen:
                envelope = rs.read_json(path)
                if not isinstance(envelope, dict):
                    raise ValueError("%%s is not a JSON object" %% path)
                row = {key: envelope.get(key) for key in KEYS}
                row["has_issue_url"] = "issue_url" in envelope
                row["written"] = mtime
                row["before"] = mtime < since
                envelopes.append(row)
            entry["repos"][repo] = envelopes
        except (OSError, ValueError) as exc:
            entry["error"] = entry["error"] or "%%s: %%s" %% (repo, exc)
print(SENTINEL)
print(json.dumps(out))
""" % {"sentinel": STORE_READ}

# The runner and the pod are two clocks; LedgerIssueContainsVerifier's tolerance.
DEFAULT_SKEW_SEC = 120.0
# How many URLs a failing reason lists.
REASON_URLS_SHOWN = 3


def _after_sentinel(reply: str, sentinel: str) -> str | None:
    """Everything after the line that is exactly ``sentinel``, or None when it is absent."""
    head, found, tail = reply.partition(sentinel + "\n")
    return tail if found else None


def read_reader_source(timeout: float) -> str | None:
    """``report_status.py`` from the agent image, or None when the agent container gave none."""
    script = f"echo {READER_READ}; cat {shlex.quote(READER_PATH)}"
    source = _after_sentinel(onboarding.agent_shell(script, timeout), READER_READ)
    return source if source and source.strip() else None


def store_command(source: str, root: str, since: float, audits: list[str], python: str = SANDBOX_PYTHON) -> str:
    """The ``sh -c`` line that runs the driver over ``source`` against ``root`` under ``python``."""
    args = [source, root, repr(since), *audits]
    return python + " -I -c " + " ".join(shlex.quote(a) for a in [_DRIVER, *args])


def read_store(
    shell, source: str, since: float, audits: list[str], timeout: float, python: str = SANDBOX_PYTHON
) -> dict[str, Any] | None:
    """The driver's document, or None when the pod gave none."""
    command = store_command(source, REPORT_STORE_ROOT, since, audits, python)
    tail = _after_sentinel(shell(command, timeout), STORE_READ)
    if tail is None:
        return None
    try:
        document = json.loads(tail.strip().splitlines()[0])
    except (IndexError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) and isinstance(document.get("audits"), dict) else None


def _mention_re(audit: str) -> re.Pattern[str]:
    """The audit's id, case-insensitive, with any run of `-`, `_` or spaces between its words.

    The boundary is a letter, digit or hyphen, not the word class: `_` is a Markdown emphasis mark, so
    `_compliance-audit_` is a mention and `security-patch-orchestrator-v2` is not.
    """
    words = audit.split("-")
    return re.compile(
        r"(?<![A-Za-z0-9-])" + r"[-_\s]+".join(map(re.escape, words)) + r"(?![A-Za-z0-9-])", re.IGNORECASE
    )


def unescaped(reply: str) -> str:
    """``reply`` with its Markdown backslash escapes removed."""
    return MARKDOWN_ESCAPE_RE.sub(r"\1", reply)


def cites(reply: str, url: str) -> bool:
    """True when ``reply`` carries ``url`` (case-insensitive, a trailing slash on either side ignored)."""
    return re.search(re.escape(url.rstrip("/")) + CITATION_SUFFIX, reply, re.IGNORECASE) is not None


def mentioned_audits(reply: str, audits: list[str]) -> set[str]:
    """The audits ``reply`` mentions by id, in any case and with any separators between its words."""
    return {audit for audit in audits if _mention_re(audit).search(reply)}


@VERIFIERS.register("report_store_reply")
class ReportStoreReplyVerifier(_OnboardingPollVerifier):
    """Checks that the reply gives each audit's latest report from the fleet-audit report store.

    ``audits``: the audits the request names, each graded as the module docstring says. A reply
    that does not mention an audit fails it. A run with no start time, a report store switch that
    cannot be read or is neither ``file`` nor ``broker``, a store pod that cannot be read, a reader
    that cannot be fetched, or an envelope with no ``issue_url`` key at all (a store format this
    check does not know) is ``status="error"``.
    """

    type: Literal["report_store_reply"]
    audits: list[AuditId] = Field(min_length=1)
    max_clock_skew_sec: float = Field(default=DEFAULT_SKEW_SEC, ge=0)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        snap = transcript.get()
        if snap is None:
            return "error", _NO_TRANSCRIPT_REASON, None
        if not snap.started_at:
            return "error", _NO_RUN_CLOCK_REASON, None
        source = read_reader_source(read_timeout)
        if source is None:
            return "error", f"could not read {READER_PATH} from the agent container", None
        since = snap.started_at - self.max_clock_skew_sec
        audits = list(dict.fromkeys(self.audits))
        try:
            kind = onboarding.report_store(read_timeout)
        except RuntimeError as exc:
            return "error", str(exc), None
        if kind == onboarding.REPORT_STORE_BROKER:
            place = where = AGENT_CONTAINER_PLACE
            store = read_store(onboarding.agent_shell, source, since, audits, read_timeout, AGENT_PYTHON)
        else:
            where = onboarding.sandbox_pod()
            place = f"the shell sandbox pod {where}"
            store = read_store(onboarding.sandbox_shell, source, since, audits, read_timeout)
        if store is None:
            return "error", f"could not read the report store in {place}", None
        accepted: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for audit in audits:
            entry = store["audits"].get(audit) or {}
            if entry.get("error"):
                return "error", f"{audit}: the store could not be read: {entry['error']}", {"store": store}
            repos = entry.get("repos") or {}
            if any(not row.get("has_issue_url") for rows in repos.values() for row in rows):
                return "error", f"{audit}: an envelope has no issue_url key", {"store": store}
            accepted[audit] = repos
        urls = {
            audit: {str(row["issue_url"]) for rows in repos.values() for row in rows if row.get("issue_url")}
            for audit, repos in accepted.items()
        }
        reply = unescaped(snap.final_message or "")
        mentioned = mentioned_audits(reply, audits)
        failures = []
        for audit in audits:
            if audit not in mentioned:
                failures.append(f"{audit}: the reply does not mention it")
                continue
            if any(cites(reply, url) for url in urls[audit]):
                continue
            # A repository whose newest envelope before the turn has a ledger, and that wrote no
            # envelope without one since, needs that ledger: a null one in another repository's
            # store does not stand in for it.
            needed = {
                str(row["issue_url"])
                for rows in accepted[audit].values()
                if any(row["before"] and row.get("issue_url") for row in rows)
                and not any(row.get("issue_url") is None for row in rows)
                for row in rows
                if row.get("issue_url")
            }
            if needed:
                expected = sorted(needed)[:REASON_URLS_SHOWN]
                failures.append(f"{audit}: no ledger URL of its latest report; expected one of {expected}")
        root_exists = bool(store.get("root_exists"))
        raw = {"accepted": accepted, "pod": where, "root_exists": root_exists}
        if failures:
            return "fail", "; ".join(failures), raw
        reason = f"each of the {len(audits)} audits has its latest report from the store in {where}"
        if not root_exists:
            reason += " (the store root is absent there: nothing stored yet, so no link was required)"
        return "pass", reason, raw
