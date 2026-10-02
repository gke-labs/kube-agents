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

"""Leaf verifiers this repository adds to devops-bench's own.

Most of them answer the half of a task's exact checks that cluster state
cannot: did the *report* name the thing we planted, did the agent *call* the
tools it claims to have used (and, through the workers' logs and tags, what
the delegated *workers* ran and as whom), does the *ledger issue the run
published* carry the finding — for the fleet audits, whose SOPs deliberately
keep the chat reply to one line — is the *pull request* the reply links one
this run opened rather than an earlier one, does the *diff* of the pull
request the reply points at carry the proposal the case asked for, and did
the run *write* to the case's GitOps repository at all (``github_writes``,
the question the cluster safeguards cannot answer). They read the per-run
stash in :mod:`kube_agents_bench.transcript`, and they fail closed: an empty
stash is ``status="error"`` — the check could not be evaluated — never a pass
or a fail, so ``VerificationCoverage`` drops below 1.0 and the gate catches
it.

The exception, ``fleet_resource_property``, does read cluster state, and exists
because upstream's ``resource_property`` reads the WRONG cluster and cannot
tell a missing fixture from a missing cluster. See
:class:`FleetResourcePropertyVerifier`.

Registered under the ``devops_bench.verifiers`` entry-point group in
``pyproject.toml`` (the same mechanism ``devops_bench.agents`` already uses
for the harness), so devops-bench discovers them without a fork.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from devops_bench.k8s import get_resource
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)
from devops_bench.verification.verifiers import ResourcePropertyVerifier

from kube_agents_bench import discovery, github_writes, onboarding, transcript
from kube_agents_bench.fleet import (
    ROLE_PATTERN,
    FleetRoleUnresolved,
    confirmed_subjects,
    kubeconfig_for_role,
)

__all__ = [
    "BootstrapDeliveredVerifier",
    "BootstrapFanoutVerifier",
    "BootstrapFindingsVerifier",
    "BootstrapReportReadVerifier",
    "FleetResourcePropertyVerifier",
    "GitHubWritesVerifier",
    "LedgerIssueContainsVerifier",
    "PullRequestDiffContainsVerifier",
    "PullRequestOpenedVerifier",
    "ReportContainsVerifier",
    "ToolCalledVerifier",
    "WorkerCommandsVerifier",
]

_NO_TRANSCRIPT_REASON = (
    "no transcript stashed for this run: the harness did not complete an "
    "agent execution (kube_agents_bench.transcript is empty), so this check "
    "could not be evaluated"
)
_NO_WORKER_CALLS_REASON = (
    "no delegated worker's tool calls are in the trajectory: either no card was "
    "delegated or the worker-trajectory capture did not run, so a check scoped to "
    "the workers cannot observe its subject"
)
_FANOUT_READ_TIMEOUT_SEC = 60.0
_ONBOARDING_READ_TIMEOUT_SEC = 60.0
# The sandbox's report changes at most twice (written, then renamed), so at most
# two of three sandbox re-reads can differ from the read before them.
_REPORT_READ_ATTEMPTS = 3

# Emphasis and code markers, dropped before matching. The agent answers in
# Markdown, and a phrase spanning an emphasised word cannot match the raw
# text: "not crashlooping" against a report that reads "the pods are **not**
# CrashLooping" fails on the asterisks alone. That failed a real presubmit
# (gke-labs/kube-agents#982) on a report the OutcomeValidity judge scored
# 1.00, and enumerating markdown shapes in every task.yaml would only encode
# one run's formatting.
# Markdown emphasis is dropped; the typographic apostrophe folds to ASCII
# so a pattern spells each contraction ("don't") exactly once.
_MARKDOWN_NOISE = str.maketrans({"*": None, "_": None, "`": None, "’": "'"})


def _normalize(text: str) -> str:
    """Lowercase, strip Markdown emphasis, collapse runs of whitespace.

    Applied to BOTH sides, so a phrase may be written with or without the
    markers and match either way.

    Boundary whitespace survives. ``" 0 restarts"`` carries a leading space
    on purpose -- it is what stops the phrase matching "10 restarts" -- and
    ``" ".join(str.split())`` would drop it and hand back the false positive
    the space was added to prevent.
    """
    return _normalize_with(text, _MARKDOWN_NOISE)


def _normalize_with(text: str, noise: dict[int, str | None]) -> str:
    """``_normalize`` over a caller's noise table; see ``_normalize_diff``."""
    stripped = text.translate(noise)
    collapsed = " ".join(stripped.split())
    if stripped[:1].isspace():
        collapsed = " " + collapsed
    if stripped[-1:].isspace():
        collapsed += " "
    return collapsed.lower()


def _normalize_lines(text: str) -> str:
    """``_normalize`` applied per line, newlines kept.

    ``forbidden_patterns`` need a boundary a Markdown bullet or heading can
    end on; the whitespace collapse above would otherwise fuse a negated
    bullet into its unnegated neighbour before the regex runs.
    """
    return "\n".join(_normalize(line) for line in text.splitlines())


@VERIFIERS.register("report_contains")
class ReportContainsVerifier(BaseVerifier):
    """Exact phrase checks against the agent's answer.

    Substring matching, deliberately: the task author chose the phrase (a
    planted defect's name, a required noun), so an exact match is fair.
    Anything fuzzier belongs to the judge, not to a blocking check.
    ``forbidden_patterns`` is the one regex exception, for the shape a
    substring cannot express: a banned word whose negated uses are
    legitimate ("no guarantee"). Each is ``re.search``ed against a
    line-preserving variant of the same normalization — newlines survive,
    so a Markdown bullet or heading with no terminal punctuation is its own
    segment and a pattern may anchor on ``\\n``; the flat collapse would
    otherwise fuse a negated bullet into its unnegated neighbour before the
    regex runs.

    Both sides are normalized first, by ``_normalize`` above: lowercased,
    Markdown emphasis dropped, whitespace runs collapsed. These are the
    variations a correct report may legitimately introduce without changing
    what it claims -- an agent that bolds a word has not said anything
    different. Nothing about the wording is relaxed: word order, negation and
    vocabulary still have to match, which is what keeps a check like
    "not crashlooping" unsatisfiable by a report saying the opposite.

    ``scope`` picks the text under test. The default, ``final``, is what the
    user ultimately receives: the delegating turn's own closing message plus,
    when work was delegated, the delivered card results and artifacts — the
    worker's actual answer, with the router's intermediate poll recitals
    excluded. ``full`` is the accumulated output: every settled closer on top
    of all of that. ``full`` therefore passes a required phrase the agent
    merely QUOTED in progress chatter and false-fails a forbidden phrase that
    only appears in quoted material — reach for it only when the check
    genuinely concerns the whole transcript.
    """

    type: Literal["report_contains"]
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    # At least ONE must appear. For a concept with several legitimate
    # spellings ("HPA" / "HorizontalPodAutoscaler"), all-of required_phrases
    # would punish a correct report for choosing the other name.
    any_of_phrases: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    scope: Literal["final", "full"] = "final"

    @field_validator("forbidden_patterns")
    @classmethod
    def _forbidden_patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        snap = transcript.get()
        if snap is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_TRANSCRIPT_REASON,
            )
        raw = snap.final_message if self.scope == "final" else snap.output
        text = _normalize(raw)
        missing = [p for p in self.required_phrases if _normalize(p) not in text]
        present = [p for p in self.forbidden_phrases if _normalize(p) in text]
        pattern_hits = [
            p for p in self.forbidden_patterns if re.search(p, _normalize_lines(raw))
        ]
        any_of_miss = bool(self.any_of_phrases) and not any(
            _normalize(p) in text for p in self.any_of_phrases
        )
        if missing or present or pattern_hits or any_of_miss:
            parts = []
            if missing:
                parts.append(f"required phrases absent from the report: {missing}")
            if present:
                parts.append(f"forbidden phrases present in the report: {present}")
            if pattern_hits:
                parts.append(
                    f"forbidden patterns matched in the report: {pattern_hits}"
                )
            if any_of_miss:
                parts.append(
                    f"none of the alternative phrasings present: {self.any_of_phrases}"
                )
            return VerificationResult(
                success=False,
                elapsed_time=time.monotonic() - start,
                reason="; ".join(parts),
            )
        # The success reason has to name every clause that ran, including
        # any_of_phrases. Counting only required and forbidden made a check
        # built from any_of alone report "all 0 required phrase(s)", which
        # reads exactly like a check that asserted nothing -- and the failure
        # branch above is the only thing that would have said otherwise.
        satisfied = [
            f"all {len(self.required_phrases)} required phrase(s)",
            f"none of {len(self.forbidden_phrases)} forbidden",
        ]
        if self.forbidden_patterns:
            satisfied.append(
                f"none of {len(self.forbidden_patterns)} forbidden pattern(s)"
            )
        if self.any_of_phrases:
            satisfied.append(
                f"at least one of {len(self.any_of_phrases)} alternative phrasing(s)"
            )
        return VerificationResult(
            success=True,
            elapsed_time=time.monotonic() - start,
            reason="report contains " + ", ".join(satisfied),
        )


# Hermes' MCP dispatch wrapper: a worker's trajectory entry named this carries
# the tools it actually invoked under args["calls"][*]["name"].
_TOOL_CALL_WRAPPER = "tool_call"


def _wrapped_tool_names(entry: dict[str, Any]) -> set[str]:
    """Tool names a ``tool_call`` wrapper entry invoked; empty for any other."""
    if entry.get("name") != _TOOL_CALL_WRAPPER:
        return set()
    args = entry.get("args")
    calls = args.get("calls") if isinstance(args, dict) else None
    if not isinstance(calls, list):
        return set()
    return {str(c.get("name")) for c in calls if isinstance(c, dict) and c.get("name")}


@VERIFIERS.register("tool_called")
class ToolCalledVerifier(BaseVerifier):
    """Count trajectory entries whose tool name is in ``tool_names``.

    ``scope`` says whose calls count. The trajectory holds two kinds of
    entry: the delegating turn's own calls (poll-turn calls are the
    harness's bookkeeping and are kept out, ``_fold_status_turn``), and the
    delegated workers' calls, which the harness appends after settlement
    tagged with the ``agent`` that made them (``worker_trajectory``).

    - ``router`` (the default, and what every check written before the
      workers' calls were recorded means): the delegating turn's calls only,
      so ``kanban_create`` counts and the worker's ``kanban_complete`` does
      not. A cluster-mutation safeguard in this scope is blind to the calls
      it fears; use a cluster-state check (``resource_property``) for those.
    - ``workers``: the tagged entries only -- what the platform worker and
      any Cluster Agent called on the run's cards. This is the scope that
      sees which MCP tool a worker reached for.
    - ``all``: both.

    ``workers`` and ``all`` fail closed on a trajectory that carries no
    tagged entry: a worker that ran made at least one call (its
    ``kanban_complete``), so no tagged entry means the capture did not run,
    or the router never delegated, and either way the check cannot observe
    its subject -- ``status="error"``, never a pass, the same rule
    ``worker_commands`` applies to an absent capture.

    Passes when at least ``minimum_calls`` matching calls were made. Wrapped
    in a ``none`` compound, it is the safeguard shape "this tool was never
    called", within the chosen scope. Names match the harness's canonical
    trajectory entries (``ToolCall.to_dict()["name"]``), e.g.
    ``kanban_create``; a worker's entries carry the name the profile's
    session store recorded for the tool.

    A worker reaches an MCP tool through Hermes' ``tool_call`` wrapper: the
    entry is named ``tool_call`` and the tool actually invoked sits in its
    arguments, ``{"calls": [{"name": "mcp__developer_knowledge__search_documents",
    "arguments": {...}}]}`` (measured on build 2102459327938826240, #1765).
    A name in ``tool_names`` therefore also matches a ``tool_call`` entry
    whose ``calls`` list names it, else a worker's MCP calls would be
    invisible to this check by name. One wrapper entry counts once however
    many of its calls match; ``require_success`` reads the wrapper's status.
    """

    type: Literal["tool_called"]
    tool_names: list[str] = Field(min_length=1)
    minimum_calls: int = Field(default=1, ge=1)
    scope: Literal["router", "workers", "all"] = "router"
    # Objectives set this: a call the harness marked status="error" produced
    # no effect (kanban_create that failed filed no card), so counting it
    # would pass a check whose subject never happened. Safeguards leave it
    # False on purpose — an ATTEMPTED forbidden write should trip the
    # safeguard whether or not the tool succeeded.
    require_success: bool = False

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        snap = transcript.get()
        if snap is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_TRANSCRIPT_REASON,
            )
        entries = [entry for entry in snap.trajectory if isinstance(entry, dict)]
        if self.scope != "router" and not any(entry.get("agent") for entry in entries):
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_WORKER_CALLS_REASON,
            )
        if self.scope == "router":
            entries = [entry for entry in entries if not entry.get("agent")]
        elif self.scope == "workers":
            entries = [entry for entry in entries if entry.get("agent")]
        wanted = set(self.tool_names)
        calls = [
            entry
            for entry in entries
            if (entry.get("name") in wanted or _wrapped_tool_names(entry) & wanted)
            and not (self.require_success and entry.get("status") == "error")
        ]
        count = len(calls)
        ok = count >= self.minimum_calls
        return VerificationResult(
            success=ok,
            elapsed_time=time.monotonic() - start,
            reason=(
                f"{count} call(s) to {sorted(wanted)} in the {self.scope} trajectory"
                f" (minimum {self.minimum_calls})"
            ),
            raw={"matching_calls": count},
        )


# ------------------------------------------------------------------ ledger

# The eight fleet-audit streams (``AUDITS`` at the top of
# agents/platform/skills/fleet-audit/scripts/audit_report.py). The Literal on
# the `audit` field below is what actually validates -- a typo'd stream in a
# task.yaml is then a spec-load error rather than a check that can never find
# its ledger -- and this frozenset is the readable name for the same set. A
# test re-derives both from audit_report.py, so a new stream upstream fails
# here rather than drifting silently.
LEDGER_AUDIT_IDS = frozenset(
    {
        "ai-security-audit",
        "compliance-audit",
        "fleet-consistency-drift",
        "fleet-wide-cost-analysis",
        "gce-compute-fleet-audit",
        "gcp-networking-fabric-audit",
        "obtainability-audit",
        "security-patch-orchestrator",
        "stockout-prevention",
    }
)

# Environment names carrying the read credential, in precedence order. See
# LedgerIssueContainsVerifier's docstring for what it has to be.
LEDGER_TOKEN_ENV_VARS = ("BENCH_GITHUB_TOKEN", "GITHUB_TOKEN")

# When the first unit on the case's audit stream began, in epoch seconds, and
# which audit that stream is; hack/ci-eval-pr.sh exports both for a case that
# writes a ledger. Read only by PullRequestOpenedVerifier's
# `accepts_stream_pull_request`.
STREAM_STARTED_ENV_VAR = "EVAL_STREAM_STARTED_AT"
STREAM_AUDIT_ENV_VAR = "EVAL_AUDIT_STREAM"
STREAM_REPO_ENV_VAR = "EVAL_STREAM_REPO"

# The head branch every remediation pull request a fleet audit opens sits on is
# this, the audit id, a dash, then the fix's slug and digest
# (agents/platform/skills/fleet-audit/scripts/audit_report.py,
# `group_branch_for`). What ties a pull request older than the run to the
# stream rather than to whichever case wrote it.
REMEDIATION_BRANCH_PREFIX = "platform-agent/fix-"

# The first line of the closing comment hack/ci_reset_audit_ledgers.py leaves
# on a ledger it retires before a repetition (RESET_MARKER there;
# scripts/test_ci_eval_ledger_reset.py pins the two literals equal). A closed
# ledger a report still cites is read for it, so the harness's own close is
# named as such rather than blamed on the run.
LEDGER_RESET_MARKER = "<!-- kube-agents-eval-ledger-reset -->"
# How far back from a ledger's closed_at that comment is asked for, and the
# page it is asked for on. The reset posts the comment and closes seconds
# later; an hour is generous and keeps the read to one page.
_RESET_COMMENT_LOOKBACK = timedelta(hours=1)
_GITHUB_PAGE_SIZE = 100
_GITHUB_SINCE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# github.com only, and issues only: `/pull/<n>` is a remediation pull request,
# which every audit report also links and which is not the ledger.
_ISSUE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)/issues/(\d+)",
    re.IGNORECASE,
)

# The other half of the pair above: pull requests only. A remediation case is
# graded on the PR it opened, and the ledger issue beside it is not that.
_PULL_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)/pull/(\d+)",
    re.IGNORECASE,
)

# audit_report.py's `_render_footer`, verbatim:
#     f"Generated by the Platform Agent `{audit_id}` watchdog at "
#     f"{generated_at.isoformat()}. Findings come from read-only inspection..."
# `generated_at` is `datetime.now(timezone.utc)` taken at the top of
# `handle_finish` and is the ONLY per-run identifier anywhere on the ledger --
# see this verifier's docstring on staleness. Non-greedy up to a period
# followed by whitespace, because an ISO-8601 stamp contains periods of its
# own ("...T06:20:11.123456+00:00.").
_LEDGER_FOOTER_RE = re.compile(
    r"Generated by the Platform Agent `(?P<audit>[^`\n]+)` watchdog at "
    r"(?P<stamp>\S+?)\.\s"
)

# audit_report.py's `DELTA_RE`, copied rather than imported: the audit script
# lives in the agent image, not in this package, and the bench process has no
# import path to it. A drift between the two surfaces as a `scope:
# finding_ids` check failing to find a block it should have found -- a fail
# whose reason names the missing marker, not a silent pass.
_DELTA_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-findings:[ \t]*(\[[^\n]*?\])[ \t]*-->[ \t]*$", re.M
)
# Mirrors the output of its `all_findings_block`, which the script writes but
# never parses, so no test on that side checks this regex against it
# (`test_the_complete_block_regex_reads_what_audit_report_writes` does): every finding id
# in the document plus the collector-held ids, written only when the body cut
# findings for space. The delta block above then lists the rendered ones
# alone, and a finding filed but cut would read as never filed.
_ALL_FINDINGS_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-findings-all:[ \t]*(\[[^\n]*?\])[ \t]*-->[ \t]*$", re.M
)

# Bound on issue URLs fetched from one report. An audit reply names its ledger
# once; anything past a handful is a report to look at by hand, not a set of
# candidates to shotgun the API with.
_MAX_LEDGER_CANDIDATES = 8

# A report that retired the ledger rather than filing on it, in the words the
# harness and the audit's closing line use for that: `render_clean_comment`'s
# "found **0 findings**" and "is now clean", and the roll-up a parent writes
# when it paraphrases the worker ("The open ledger issue has been closed").
# Consulted only when the report names no issue URL at all, where the two
# ways of arriving there -- a worker that never returned and a worker that
# closed the ledger as clean and dropped the pointer -- used to share one
# sentence (#1683). Clause-bounded so "closed" and "ledger" have to be about
# each other.
_CLEAN_CLOSE_RE = re.compile(
    r"ledger[^\n.]{0,80}?\bclosed\b|\bclosed\b[^\n.]{0,80}?\bledger\b"
    r"|\b0 findings\b|\bfound nothing\b|\bno findings\b|\bis now clean\b",
    re.IGNORECASE,
)

# Matches when the worker queued the audit for a future cron schedule or reported
# that the on-demand trigger was unavailable (#1876), instead of running the audit.
_QUEUED_INSTEAD_OF_RUN_RE = re.compile(
    r"\b(?:queued\s+(?:to\s+run|for\s+its\s+next|for\s+the\s+next|the\s+stream)|"
    r"on-demand\s+trigger\s+is\s+unavailable|"
    r"stream\s+will\s+run\s+on\s+its\s+\d{2}:\d{2}\s+schedule|"
    r"will\s+run\s+on\s+its\s+next\s+cron\s+schedule)\b",
    re.IGNORECASE,
)

_NO_RUN_CLOCK_REASON = (
    "the run's transcript carries no start time (TranscriptSnapshot.started_at "
    "is unset), so this check cannot tell this run's ledger from a previous "
    "run's and refuses to grade it"
)

# Bound on pull request URLs resolved from one report, for the reason
# _MAX_LEDGER_CANDIDATES exists: a remediation reply links the PR it opened,
# and a report naming a dozen is one to read by hand.
_MAX_PR_CANDIDATES = 8

# Page size for the pull request's commit listing. The head is on the last
# page, so the page number is computed from the total the pulls endpoint
# reports; GitHub caps the listing at 250, and a pull request longer than that
# simply yields no head commit rather than the wrong one.
_PR_COMMITS_PAGE_SIZE = 100
# `/pulls/{n}/commits` lists at most 250 commits.
_PR_COMMITS_MAX_PAGES = 3

_NO_PR_RUN_CLOCK_REASON = (
    "the run's transcript carries no start time (TranscriptSnapshot.started_at "
    "is unset), so this check cannot tell a pull request this run opened from "
    "one left behind by a previous run, and refuses to grade it"
)

_NO_PR_URL_REASON = (
    "the run's report names no github.com pull request URL, so no fix was "
    "proposed (or the agent did not report the PR it opened); a remediation "
    "reply must carry the pull request URL in full"
)

_NO_TOKEN_REASON = (
    "no GitHub read credential in the environment: set one of "
    f"{', '.join(LEDGER_TOKEN_ENV_VARS)} to a token that can read issues on "
    "the eval GitOps repository, or this check cannot be evaluated"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turns every redirect into an ``HTTPError`` instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_NO_WORKER_COMMANDS_REASON = (
    "no delegated worker's commands were captured for this run: either no card "
    "was delegated, the run ended before the cards settled, or the harness "
    "predates command capture -- so this check could not be evaluated"
)
_MAX_NAMED_COMMANDS = 5


@VERIFIERS.register("worker_commands")
class WorkerCommandsVerifier(BaseVerifier):
    """Pattern checks against the terminal commands the delegated workers ran.

    Sees the ROUTE a worker took rather than the answer it gave, through the
    terminal commands it typed; ``tool_called`` under ``scope: workers`` is
    the companion for the MCP tool calls it made (see its docstring).
    The harness reads each delegated card's worker log before purging it and
    stashes every ``💻 $`` line as a command (``transcript.worker_commands``);
    this verifier matches Python regular expressions against those strings,
    ``re.search`` on each command verbatim.

    ``required_patterns``: each must match at least one command.
    ``forbidden_patterns``: none may match any command.

    Limits, stated so a case is not written against them: only terminal
    commands are visible, not MCP tool calls; only delegated workers' logs
    are read, never the router's; and a command the shell resolved through an
    alias appears as typed. Fails closed like its siblings -- a run with no
    captured worker commands is ``status="error"``, not a pass.
    """

    type: Literal["worker_commands"]
    required_patterns: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)

    @field_validator("required_patterns", "forbidden_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        snap = transcript.get()
        if snap is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_TRANSCRIPT_REASON,
            )
        if snap.worker_commands is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_WORKER_COMMANDS_REASON,
            )
        commands = [row.get("command", "") for row in snap.worker_commands]
        missing = [
            p for p in self.required_patterns
            if not any(re.search(p, c) for c in commands)
        ]
        hits = [
            (p, c) for p in self.forbidden_patterns for c in commands if re.search(p, c)
        ]
        if missing or hits:
            parts = []
            if missing:
                parts.append(
                    f"no worker command matched required pattern(s) {missing} "
                    f"across {len(commands)} command(s)"
                )
            if hits:
                shown = "; ".join(
                    f"{p!r} matched {c[:120]!r}" for p, c in hits[:_MAX_NAMED_COMMANDS]
                )
                more = f" (+{len(hits) - _MAX_NAMED_COMMANDS} more)" if len(hits) > _MAX_NAMED_COMMANDS else ""
                parts.append(f"forbidden pattern(s) matched worker commands: {shown}{more}")
            return VerificationResult(
                success=False,
                elapsed_time=time.monotonic() - start,
                reason="; ".join(parts),
            )
        return VerificationResult(
            success=True,
            elapsed_time=time.monotonic() - start,
            reason=(
                f"{len(commands)} worker command(s): all {len(self.required_patterns)} "
                f"required pattern(s) matched, none of {len(self.forbidden_patterns)} forbidden"
            ),
        )


_NO_WORKER_AGENTS_REASON = (
    "no delegated worker's tool calls were captured for this run: either no card "
    "was delegated, the run ended before the cards settled, or the worker "
    "trajectory could not be read -- so this check could not be evaluated"
)


@VERIFIERS.register("worker_agents")
class WorkerAgentsVerifier(BaseVerifier):
    """Checks which profiles the delegated workers ran as.

    The harness appends each delegated worker's tool calls to the trajectory
    tagged with ``agent``, the profile that made the call
    (:mod:`kube_agents_bench.worker_trajectory`). ``tool_called`` skips those
    entries and ``worker_commands`` sees commands but not who ran them, so
    neither can tell a card a Cluster Agent worked from one the Platform Agent
    kept. This reads the tags and nothing else.

    ``required_agents``: Python regular expressions, each of which must
    ``re.fullmatch`` the ``agent`` tag of at least one worker entry.

    Fails closed like its siblings: a run with no tagged entries is
    ``status="error"``, not a fail -- the harness saw no worker at all. So is
    a required profile missing from a capture that recorded gaps
    (``worker_capture_gaps``): a store it could not open or a fan-out it
    clipped may hold exactly the calls that would have matched, and grading
    that as the agent taking the wrong route would be a guess.
    """

    type: Literal["worker_agents"]
    required_agents: list[str] = Field(min_length=1)

    @field_validator("required_agents")
    @classmethod
    def _patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        snap = transcript.get()
        if snap is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_TRANSCRIPT_REASON,
            )
        agents = sorted({str(e["agent"]) for e in snap.trajectory if e.get("agent")})
        if not agents:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_WORKER_AGENTS_REASON,
            )
        missing = [p for p in self.required_agents if not any(re.fullmatch(p, a) for a in agents)]
        if missing and snap.worker_capture_gaps:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=(
                    f"no captured worker ran as a profile matching {missing} (workers seen: {agents}), "
                    f"but the capture was incomplete, so this check could not be evaluated: "
                    f"{'; '.join(snap.worker_capture_gaps)}"
                ),
            )
        if missing:
            return VerificationResult(
                success=False,
                elapsed_time=time.monotonic() - start,
                reason=f"no delegated worker ran as a profile matching {missing}; workers ran as {agents}",
            )
        return VerificationResult(
            success=True,
            elapsed_time=time.monotonic() - start,
            reason=f"all {len(self.required_agents)} required profile pattern(s) matched; workers ran as {agents}",
        )


def _agent_shell(script: str, timeout: float) -> str:
    # Lazy: the harness pulls in the agent transport, which a spec load does not need.
    from kube_agents_bench.harness import _agent_shell as shell

    return shell(script, timeout)


@VERIFIERS.register("bootstrap_fanout")
class BootstrapFanoutVerifier(BaseVerifier):
    """Checks the cards the onboarding discovery sweep's worker filed.

    The sweep card is filed by a cron job rather than by the conversation, so
    neither the transcript nor the harness's delegation capture sees it; this
    reads the card, its worker's children and the Cluster Agent roster off the
    agent's disk (:mod:`kube_agents_bench.discovery`).

    ``require``:

    - ``one_card_per_cluster_agent``: every Cluster Agent that is registered,
      finished scaffolding and has a cluster identity got exactly one
      ``bootstrap-inventory-cluster-*`` card, assigned to it and keyed by its
      profile name, and no such card went anywhere else.
    - ``no_card_waits_on_the_sweep``: no ``bootstrap-inventory-cluster-*``
      card names the sweep as a parent. A child waiting on the card that waits
      on it never runs until the sweep has given up on it.

    Fails closed: an unreadable pod, no sweep marker, a board that cannot be
    queried, or a sweep card the board does not know is ``status="error"``,
    and so is an empty roster for ``one_card_per_cluster_agent``.
    ``no_card_waits_on_the_sweep`` does not read the roster, so an empty one
    is not an error for it. A ``fail`` from an earlier poll outranks a final
    read that errors.
    """

    type: Literal["bootstrap_fanout"]
    require: Literal["one_card_per_cluster_agent", "no_card_waits_on_the_sweep"]

    def verify(self, timeout_sec: float) -> VerificationResult:
        read_timeout = min(single_call_timeout(timeout_sec), _FANOUT_READ_TIMEOUT_SEC)
        # _poll_to_result reports the last poll even when it is an error, so a
        # fan-out that stayed broken would read as an unreadable pod whenever
        # the final read failed. The latest fail stands in that case.
        last_fail: tuple[str, dict[str, Any] | None] | None = None

        def attempt() -> tuple[VerificationStatus, str, dict[str, Any] | None]:
            nonlocal last_fail
            status, reason, raw = self._check(read_timeout)
            if status == "fail":
                last_fail = (reason, raw)
            return status, reason, raw

        result = self._poll_to_result(attempt, timeout_sec)
        if result.status == "error" and last_fail is not None:
            reason, raw = last_fail
            return VerificationResult(
                success=False,
                status="fail",
                elapsed_time=result.elapsed_time,
                reason=f"{reason} (the last read failed: {result.reason})",
                name=self.name,
                raw=raw,
            )
        return result

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        payload, why = discovery.read_fanout(_agent_shell, read_timeout)
        if payload is None:
            return "error", why, None
        sweep = payload.get("sweep") or {}
        cards = [
            c for c in payload.get("children") or []
            if str(c.get("key") or "").startswith(discovery.CLUSTER_KEY_PREFIX)
        ]
        where = f"sweep {sweep.get('id')} ({sweep.get('status')})"
        if self.require == "no_card_waits_on_the_sweep":
            waiting = [c["id"] for c in cards if sweep.get("id") in (c.get("parents") or [])]
            if waiting:
                return "fail", f"{where}: cluster card(s) {waiting} name the sweep as a parent", payload
            return "pass", f"{where}: none of {len(cards)} cluster card(s) waits on the sweep", payload

        roster = payload.get("roster") or []
        if not roster:
            unidentified = payload.get("unidentified") or []
            not_ready = payload.get("not_ready") or []
            return (
                "error",
                f"{where}: no ready Cluster Agent profile with a cluster identity"
                + (f" (profiles without one: {unidentified})" if unidentified else "")
                + (f" (profiles whose scaffold did not finish: {not_ready})" if not_ready else ""),
                payload,
            )
        expected = {(r["profile"], r["key"]) for r in roster}
        filed = [(c.get("assignee"), c.get("key")) for c in cards]
        missing = sorted(expected - set(filed))
        duplicated = sorted({f for f in filed if filed.count(f) > 1})
        stray = sorted(set(filed) - expected)
        if missing or duplicated or stray:
            parts = [f"{where} filed {len(cards)} cluster card(s) for {len(roster)} Cluster Agent(s)"]
            if missing:
                parts.append(f"no card for {[p for p, _ in missing]}")
            if duplicated:
                parts.append(f"more than one card for {[p for p, _ in duplicated]}")
            if stray:
                parts.append(f"card(s) matching no Cluster Agent: {stray}")
            return "fail", "; ".join(parts), payload
        return "pass", f"{where}: one card for each of {len(roster)} Cluster Agent(s)", payload


def _http_get_json(url: str, token: str, timeout: float) -> tuple[int, Any]:
    """One GET against the GitHub REST API. The whole faked surface in tests.

    Returns ``(status, decoded_json_or_None)``. Raises :class:`OSError`-family
    exceptions for transport failures, which the caller turns into
    ``status="error"`` rather than a fail — an unreachable API is the absence
    of an observation, not a violation.

    Redirects are refused rather than followed. urllib does not strip
    ``Authorization`` across hosts, and a renamed repository answers ``301``;
    surfacing that as an unexpected status names the real problem instead of
    handing the token to whatever the ``Location`` points at.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kube-agents-bench-ledger-verifier",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        # 404 and 403 are answers, not transport failures: read the body so the
        # caller can distinguish "no such issue" from "no such permission".
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except (ValueError, OSError):
            payload = None
        return exc.code, payload
    except json.JSONDecodeError as exc:
        raise OSError(f"GitHub returned a body that is not JSON: {exc}") from exc
    except http.client.HTTPException as exc:
        raise OSError(f"{type(exc).__name__}: {exc}") from exc


def _parse_footer(body: str) -> tuple[str, datetime] | None:
    """The ledger footer's ``(audit id, generated-at)``, or None when absent.

    The LAST match, not the first. ``render_issue_body`` assembles the body as
    ``fixed + findings + held + declared + withheld + evidence + footer``, so every byte the
    agent authored — finding titles and impacts through ``clip_text``, which
    redacts credentials and clips length but neither strips backticks nor
    flattens newlines, and evidence excerpts into a raw fenced block — sits
    ABOVE the real footer. Taking the first match would let a finding whose
    impact carries a footer-shaped line supply both halves this check binds
    to: its own audit id (so the stream check passes) and its own stamp (so
    the staleness check passes against a ledger left by a previous run). The
    footer cannot be required to be the body's last non-empty line — the
    hidden ``audit-findings`` delta block is rendered after it — but nothing
    the agent writes can ever appear below it, so the final match is the one
    ``audit_report.py`` wrote. Same reason ``_finding_ids`` reads
    the last match.
    """
    match = None
    for match in _LEDGER_FOOTER_RE.finditer(body):
        pass
    if match is None:
        return None
    try:
        stamp = datetime.fromisoformat(match.group("stamp"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        # audit_report.py always writes an aware UTC stamp; a naive one is a
        # hand-edited or foreign body. Read it as UTC rather than discarding
        # it, so the staleness comparison below still happens.
        stamp = stamp.replace(tzinfo=timezone.utc)
    return match.group("audit").strip(), stamp


# One parser for GitHub's stamps, shared with the client that lists writes.
_parse_github_time = github_writes.parse_github_time


def _finding_ids(body: str) -> tuple[list[str], str] | None:
    """This run's finding ids and the block they came from, or None when the ledger carries no block.

    The complete-list block when the body has one, since only a truncated body
    writes it and there the delta block holds the rendered subset; the delta
    block otherwise, which then names every finding. Both by their last match,
    for the reason ``_parse_footer`` gives, and the complete list only BELOW
    the last delta block, where ``_render_footer`` puts it: an untruncated
    body has no real one, so a copy an agent wrote into a finding above the
    footer would otherwise outrank the delta block that does.
    """
    deltas = list(_DELTA_RE.finditer(body))
    if not deltas:
        return None
    last = deltas[-1]
    complete = [m for m in _ALL_FINDINGS_RE.finditer(body) if m.start() > last.end()]
    source = "audit-findings-all" if complete else "audit-findings"
    try:
        ids = json.loads((complete or deltas)[-1].group(1))
    except (ValueError, TypeError):
        return None
    if not isinstance(ids, list):
        return None
    return [i for i in ids if isinstance(i, str)], source


@VERIFIERS.register("ledger_issue_contains")
class LedgerIssueContainsVerifier(BaseVerifier):
    """Phrase checks against the GitHub ledger issue this run published.

    WHY THIS EXISTS. Every fleet-audit SOP mandates a one-line closing reply
    that deliberately does NOT restate the findings; the findings go to a
    GitHub issue, one per audit stream, rewritten in full on every run. A
    ``report_contains`` objective over that reply therefore fails a
    SOP-CONFORMANT run, and widening it to the whole transcript is worse: it
    would pass on a noun that appeared in tool output the agent never reported
    on. This check reads the artifact the audit actually writes.

    HOW IT FINDS THE ISSUE. From the run's own final message. That is not a
    shortcut, it is the only channel that exists: ``audit_report.py start``
    prints ``"issue": null`` until a ledger exists (only ``finish`` ever calls
    ``gh issue create``), the audit's ``.lease`` marker on disk records the
    repo and the audit id but no issue number, and the audit runs in a
    delegated worker whose calls reach ``snap.trajectory`` only as clipped,
    tagged entries that no verifier reads for content. What does cross back
    is ``finish``'s ``issue_url``, which the SOP requires
    every non-silent report to carry in full — and an on-demand run, which is
    what an eval task is, is never silent. The URL is treated as a POINTER and
    never as evidence: everything asserted below comes from what GitHub
    returns for it.

    HOW STALENESS IS CLOSED, which is the whole difficulty. A stream owns
    exactly one ledger issue and rewrites it in place forever, so its number,
    title, and labels are identical run over run and "an issue containing the
    planted noun" would pass for every run after the first good one. The
    footer ``audit_report.py`` renders into the body carries
    ``generated_at.isoformat()``, taken at the top of ``handle_finish`` and
    the only per-run identifier on the artifact. This check requires that
    stamp to be at or after the moment the harness started THIS run
    (``TranscriptSnapshot.started_at``), less ``max_clock_skew_sec``. A ledger
    left by yesterday's run — or by the previous task in the same presubmit —
    is a fail, not a pass. Deliberately not GitHub's ``updated_at``: an edit
    that changes nothing need not move it, and the stamp is content the run
    itself wrote.

    Two further bindings, both cheap, both from the same API response: the
    issue must carry the ``audit:<audit>`` label, and the footer's audit id
    must equal ``audit``. Together they say "this is the right stream's
    ledger", so a report pointing at some other issue that happens to contain
    the noun does not pass. Exactly one of the reported URLs may satisfy them;
    two would mean the report named two ledgers for a stream that owns one.

    AUTHENTICATION. A GitHub token from ``BENCH_GITHUB_TOKEN`` (preferred) or
    ``GITHUB_TOKEN``, read by the verifier process — the Prow runner, not the
    agent. It needs one permission, ``issues: read``, on the eval GitOps
    repositories, which are private and ours (``gke-agentic/
    kube-agents-evals-infra`` and ``…-evals-2-infra``); that they are
    throwaway repositories we own is what makes reading them from CI
    acceptable at all. Deliberately NOT the agent's own credential: the
    in-cluster ``github-token-minter`` mints a WRITE-scoped installation token
    held by the credential-proxy sidecar, and verifying an artifact with the
    same credential that produced it — reached by ``kubectl exec`` into the
    pod under test, with no refresh of its own — buys nothing and couples the
    gate to the thing it grades. An absent token is ``status="error"``, never
    a pass.

    ``scope`` picks the text under test:

    ``body`` (default) — the rendered issue body: findings, evidence, impact,
    recommendations, and the scope table.

    ``finding_ids`` — only the ids in the hidden ``<!-- audit-findings: … -->``
    delta block (or, on a body truncated for size, the
    ``<!-- audit-findings-all: … -->`` block listing every filed and
    collector-held finding),
    which ``audit_report.py`` derives as
    ``<check>.<cluster>.<namespace>.<object>``. Use it whenever the phrase is a
    CLUSTER name: the body's scope table names every audited cluster on every
    run, so ``required_phrases: ["seeded-c"]`` against ``body`` would pass on a
    ledger that enumerated the fleet and found nothing. Against the ids it
    passes only when a finding was actually FILED against that cluster.

    A FALSE CLEAN IS NAMED AS ONE. A clean run closes the ledger without
    rewriting its body, so a ledger the audit retired during this run still
    carries the previous run's stamp and would read, above, as "a previous
    run's ledger, so this run published nothing" — the one thing that run had
    not done. When the issue is ``closed`` and its ``closed_at`` falls inside
    this run, the reason says the ledger was closed as clean while this case
    expected a finding on it. The same distinction is drawn when the report
    names no URL at all but says in words that the ledger was closed or that
    nothing was found (``_CLEAN_CLOSE_RE``): rep 2 of the 2026-09-16 nightly
    did exactly that and shared its reason with a delegation that never
    returned (#1683). Neither is a new pass or fail; both are the same fail
    with a reason a reader can act on.

    THE HARNESS'S OWN CLOSE IS NAMED AS SUCH. ``hack/ci-eval-pr.sh`` retires
    the stream's open ledger before every repetition
    (``hack/ci_reset_audit_ledgers.py``), seconds before devops-bench starts
    the run, so that ``closed_at`` falls inside the same window a false clean
    would. A worker that cites the retired ledger instead of the fresh one it
    should have opened did not close it: when a closed ledger carries a
    ``LEDGER_RESET_MARKER`` comment posted within ``max_clock_skew_sec`` of
    its ``closed_at``, the reason says the harness reset it before this run
    started. Bound to the close on purpose: the reset comments first and
    closes second, so a reset whose close failed leaves the marker on an OPEN
    ledger, and a worker's genuine false clean on it later must not inherit
    the harness's name from a comment that is minutes or hours older than the
    close. Still a fail, since the run published nothing to the ledger it
    named; only the sentence changes, whichever side of ``started_at`` the
    close fell on.
    """

    type: Literal["ledger_issue_contains"]
    # Which stream's ledger. Validated against the registered ids so a typo
    # fails at spec-load time rather than as an unfindable ledger at run time.
    audit: Literal[
        "ai-security-audit",
        "compliance-audit",
        "fleet-consistency-drift",
        "fleet-wide-cost-analysis",
        "gce-compute-fleet-audit",
        "gcp-networking-fabric-audit",
        "obtainability-audit",
        "security-patch-orchestrator",
        "stockout-prevention",
    ]
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    any_of_phrases: list[str] = Field(default_factory=list)
    scope: Literal["body", "finding_ids"] = "body"
    # Tolerance on the ledger stamp vs. the harness's run-start clock, which
    # are two different machines (the Prow runner and the agent pod). Small on
    # purpose: every second of it is a second of a previous run's ledger that
    # would read as this one's. Two minutes covers clock drift by a wide
    # margin and is orders of magnitude below the gap between two runs of the
    # same stream -- the six audit scenarios use six DIFFERENT streams, so
    # even back-to-back tasks in one presubmit never share a ledger.
    max_clock_skew_sec: float = Field(default=120.0, ge=0)

    def _closed_by_the_reset(
        self, api_url: str, closed_at: datetime, token: str, budget: float
    ) -> bool | None:
        """Whether the harness's reset marker was posted alongside this close.

        ``True`` or ``False`` when the comments were read; ``None`` when they
        could not be (a transport fault or a non-200), which the caller
        reports instead of treating it as either answer. Only comments from
        the hour before the close are asked for, and only a marker comment
        created within ``max_clock_skew_sec`` of ``closed_at`` counts: the
        reset posts its comment and closes seconds later, so a marker that is
        older than that belongs to a reset whose close failed, not to this
        close.
        """
        closed_at = closed_at.astimezone(timezone.utc)
        since = (closed_at - _RESET_COMMENT_LOOKBACK).strftime(_GITHUB_SINCE_FORMAT)
        try:
            status_code, payload = _http_get_json(
                f"{api_url}/comments?per_page={_GITHUB_PAGE_SIZE}&since={since}",
                token,
                budget,
            )
        except OSError:
            return None
        if status_code != 200 or not isinstance(payload, list):
            return None
        for comment in payload:
            if not isinstance(comment, dict):
                continue
            if LEDGER_RESET_MARKER not in str(comment.get("body") or ""):
                continue
            created_at = _parse_github_time(comment.get("created_at"))
            if created_at is None:
                continue
            if abs((closed_at - created_at).total_seconds()) <= self.max_clock_skew_sec:
                return True
        return False

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()

        def done(
            success: bool,
            reason: str,
            *,
            status: str | None = None,
            raw: dict | None = None,
        ) -> VerificationResult:
            return VerificationResult(
                success=success,
                status=status,
                elapsed_time=time.monotonic() - start,
                reason=reason,
                raw=raw,
            )

        snap = transcript.get()
        if snap is None:
            return done(False, _NO_TRANSCRIPT_REASON, status="error")
        if not snap.started_at:
            return done(False, _NO_RUN_CLOCK_REASON, status="error")
        token = next(
            (v for v in (os.environ.get(n) for n in LEDGER_TOKEN_ENV_VARS) if v), None
        )
        if not token:
            return done(False, _NO_TOKEN_REASON, status="error")

        seen: list[tuple[str, str, int]] = []
        for owner, repo, number in _ISSUE_URL_RE.findall(snap.final_message):
            key = (owner, repo, int(number))
            if key not in seen:
                seen.append(key)
        if not seen:
            queued = _QUEUED_INSTEAD_OF_RUN_RE.search(snap.final_message)
            if queued:
                return done(
                    False,
                    "the run's report names no github.com issue URL because the worker queued "
                    f"the audit for later instead of running it ({queued.group(0).strip()!r}): "
                    "when asked to run an audit following its SOP, the worker must execute "
                    "the audit now via audit_report.py start/finish rather than deferring to cron (#1876)",
                )
            clean = _CLEAN_CLOSE_RE.search(snap.final_message)
            if clean:
                return done(
                    False,
                    "the run's report names no github.com issue URL, but says the "
                    f"ledger was retired as clean ({clean.group(0).strip()!r}): the "
                    "audit closed the stream's ledger over a fleet this case planted "
                    "a finding on, and dropped the pointer to it -- a false clean, "
                    "not a report that never arrived; every non-silent fleet-audit "
                    "report must carry issue_url in full",
                )
            return done(
                False,
                "the run's report names no github.com issue URL, so no ledger "
                "was published (or the audit did not report the one it wrote); "
                "every non-silent fleet-audit report must carry issue_url in full",
            )
        if len(seen) > _MAX_LEDGER_CANDIDATES:
            return done(
                False,
                f"the run's report names {len(seen)} distinct issue URLs; an "
                f"audit reports one ledger, so more than {_MAX_LEDGER_CANDIDATES} "
                "is not a set of candidates worth resolving",
            )

        budget = single_call_timeout(timeout_sec)
        matches: list[dict[str, Any]] = []
        rejected: list[str] = []
        for owner, repo, number in seen:
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
            try:
                status_code, payload = _http_get_json(url, token, budget)
            except OSError as exc:
                return done(
                    False,
                    f"could not reach the GitHub API for {owner}/{repo}#{number}: "
                    f"{exc}; this check could not be evaluated",
                    status="error",
                )
            if status_code == 404:
                rejected.append(f"{owner}/{repo}#{number}: no such issue (404)")
                continue
            if status_code in (401, 403):
                return done(
                    False,
                    f"GitHub returned {status_code} for {owner}/{repo}#{number}: "
                    "the configured token cannot read this repository's issues, "
                    "so this check could not be evaluated",
                    status="error",
                )
            if status_code != 200 or not isinstance(payload, dict):
                return done(
                    False,
                    f"unexpected GitHub response {status_code} for "
                    f"{owner}/{repo}#{number}; this check could not be evaluated",
                    status="error",
                )
            body = str(payload.get("body") or "")
            labels = {
                str(lbl.get("name") or "")
                for lbl in payload.get("labels") or []
                if isinstance(lbl, dict)
            }
            footer = _parse_footer(body)
            if f"audit:{self.audit}" not in labels:
                rejected.append(
                    f"{owner}/{repo}#{number}: not labelled audit:{self.audit} "
                    f"(labels: {sorted(labels)})"
                )
                continue
            if footer is None:
                rejected.append(
                    f"{owner}/{repo}#{number}: carries no readable audit_report "
                    "footer, so it is not a ledger this run wrote"
                )
                continue
            if footer[0] != self.audit:
                rejected.append(
                    f"{owner}/{repo}#{number}: footer names the "
                    f"{footer[0]!r} stream, not {self.audit!r}"
                )
                continue
            matches.append(
                {
                    "slug": f"{owner}/{repo}#{number}",
                    "api_url": url,
                    "body": body,
                    "generated_at": footer[1],
                    # Read here, decided below: a closed issue is only telling
                    # once the stamp has said the body is not this run's.
                    "state": str(payload.get("state") or "").lower(),
                    "state_reason": str(payload.get("state_reason") or ""),
                    "closed_at": _parse_github_time(payload.get("closed_at")),
                }
            )

        if not matches:
            return done(
                False,
                f"none of the issue URLs the report names is the {self.audit} "
                "ledger: " + "; ".join(rejected),
            )
        if len(matches) > 1:
            return done(
                False,
                f"the report names {len(matches)} issues that each claim to be "
                f"the {self.audit} ledger, and a stream owns exactly one: "
                + ", ".join(m["slug"] for m in matches),
            )

        ledger = matches[0]
        generated_at: datetime = ledger["generated_at"]
        started = datetime.fromtimestamp(snap.started_at, tz=timezone.utc)
        age = (started - generated_at).total_seconds()
        if age > self.max_clock_skew_sec:
            closed_at: datetime | None = ledger["closed_at"]
            if ledger["state"] == "closed" and closed_at is not None:
                state_reason = ledger["state_reason"] or "completed"
                raw = {
                    "generated_at": generated_at.isoformat(),
                    "closed_at": closed_at.isoformat(),
                    "state_reason": ledger["state_reason"],
                }
                # One more read, only for a closed ledger: was the close the
                # harness's own reset? None means the comments could not be
                # read, which is said rather than taken for either answer.
                reset = self._closed_by_the_reset(ledger["api_url"], closed_at, token, budget)
                raw["reset_by_harness"] = reset
                if reset:
                    # The per-unit reset runs before the harness's clock starts,
                    # so "before" is the expected reading; a reset close after
                    # it would be another lane's, and is said as what it is.
                    when = (
                        f"before this run started ({started.isoformat()})"
                        if closed_at <= started
                        else f"{(closed_at - started).total_seconds():.0f}s after this run "
                        f"started ({started.isoformat()})"
                    )
                    return done(
                        False,
                        f"{ledger['slug']} was closed as {state_reason} at "
                        f"{closed_at.isoformat()} by the eval harness's ledger reset, "
                        f"{when}: the report cites the ledger the reset retired so a "
                        "repetition would open a fresh one, and its body still carries "
                        f"the previous run's stamp ({generated_at.isoformat()}), so this "
                        "run published nothing to it -- a stale pointer to the harness's "
                        "close, not a false clean",
                        raw=raw,
                    )
                if (started - closed_at).total_seconds() <= self.max_clock_skew_sec:
                    unread = (
                        ""
                        if reset is False
                        else " (its comments could not be read, so the harness's own "
                        "ledger reset is not ruled out)"
                    )
                    return done(
                        False,
                        f"{ledger['slug']} was closed as {state_reason} at "
                        f"{closed_at.isoformat()}, during this run, with its body still "
                        f"carrying the previous run's stamp ({generated_at.isoformat()}): "
                        "the audit reported the stream clean and retired the ledger "
                        "while this case expected a finding on it -- a false clean, not "
                        f"an absent report{unread}",
                        raw=raw,
                    )
            return done(
                False,
                f"{ledger['slug']} was generated at {generated_at.isoformat()}, "
                f"{age:.0f}s BEFORE this run started ({started.isoformat()}): it "
                "is a previous run's ledger, so this run published nothing",
                raw={"generated_at": generated_at.isoformat()},
            )

        if self.scope == "finding_ids":
            parsed = _finding_ids(ledger["body"])
            if parsed is None:
                return done(
                    False,
                    f"{ledger['slug']} carries no readable "
                    "<!-- audit-findings: [...] --> delta block (or a malformed "
                    "audit-findings-all block below it), so the findings "
                    "this run filed cannot be read off it",
                )
            ids, source = parsed
            text = "\n".join(ids).lower()
            # Which block: a truncated body with no complete list below its
            # delta block (over the script's cap, or a drifted format) grades
            # the rendered subset, and the reason should say so rather than
            # read as a finding the agent never filed.
            surface = f"the {len(ids)} finding id(s) in {ledger['slug']}'s {source} block"
        else:
            text = ledger["body"].lower()
            surface = f"the body of {ledger['slug']}"

        missing = [p for p in self.required_phrases if p.lower() not in text]
        present = [p for p in self.forbidden_phrases if p.lower() in text]
        any_of_miss = bool(self.any_of_phrases) and not any(
            p.lower() in text for p in self.any_of_phrases
        )
        raw = {
            "issue": ledger["slug"],
            "generated_at": generated_at.isoformat(),
            "scope": self.scope,
        }
        if missing or present or any_of_miss:
            parts = []
            if missing:
                parts.append(f"required phrases absent from {surface}: {missing}")
            if present:
                parts.append(f"forbidden phrases present in {surface}: {present}")
            if any_of_miss:
                parts.append(
                    f"none of the alternative phrasings present in {surface}: "
                    f"{self.any_of_phrases}"
                )
            return done(False, "; ".join(parts), raw=raw)
        # Names every clause that ran, for the reason ReportContainsVerifier's
        # success branch does: an any_of-only check that reported "all 0
        # required phrase(s)" would read exactly like a check asserting nothing.
        satisfied = [
            f"all {len(self.required_phrases)} required phrase(s)",
            f"none of {len(self.forbidden_phrases)} forbidden",
        ]
        if self.any_of_phrases:
            satisfied.append(
                f"at least one of {len(self.any_of_phrases)} alternative phrasing(s)"
            )
        return done(
            True,
            f"{surface}, generated at {generated_at.isoformat()} by this run, "
            "contains " + ", ".join(satisfied),
            raw=raw,
        )


def _stream_audit() -> str:
    """The audit id of the case's stream, from STREAM_AUDIT_ENV_VAR; "" if unset."""
    return os.environ.get(STREAM_AUDIT_ENV_VAR, "").strip()


def _stream_repo() -> str:
    """The ``owner/name`` the stream's pull requests land in, from STREAM_REPO_ENV_VAR, lowercased; "" if unset."""
    return os.environ.get(STREAM_REPO_ENV_VAR, "").strip().lower()


def _stream_started() -> datetime | None:
    """When the case's audit stream first ran, from STREAM_STARTED_ENV_VAR; None if unset or unreadable."""
    raw = os.environ.get(STREAM_STARTED_ENV_VAR, "").strip()
    try:
        stamp = float(raw)
        return datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


@VERIFIERS.register("pull_request_opened")
class PullRequestOpenedVerifier(BaseVerifier):
    """A remediation pull request THIS run opened (or, with
    ``accepts_stream_pull_request``, one an earlier run on its audit stream
    opened), resolved through GitHub.

    WHY THIS EXISTS. The remediation cases used to grade on a
    ``report_contains`` over ``["github.com/", "/pull/"]``, which asks only
    that the reply hold a URL-shaped string. Nothing is fetched, so an invented
    link passes; and the pool sweep runs between leases rather than between
    reps, so a pull request an earlier rep of the same job opened is still there
    and still linkable. Repeats of a case were being graded against a pile of their
    own earlier output (#1755).

    WHAT IT ASSERTS. The reply names a github.com pull request URL; GitHub
    resolves it; the number is a pull request and not an issue; it lives under
    ``owner`` when one is set; it is not closed unmerged; it was written --
    created or updated -- at or after this run started, less
    ``max_clock_skew_sec``; it changes at least one file; and its head commit
    is no older than the same start. Updating counts because the skill reuses a
    branch and edits the pull request already open on it, which is the
    documented behaviour rather than a defect -- so the stamp alone proves only
    that somebody wrote to the pull request, and the head commit is what
    separates a run that pushed a fix from one that left a comment. That is
    also what makes reps inside a job gradable: rep 2 pushing onto rep 1's
    branch moves the head commit, rep 2 quoting rep 1's URL does not. One
    surviving candidate is enough — a reply may link the ticket it came from
    beside the fix — and a candidate GitHub cannot answer for ends the check
    only when no other candidate passes. With ``reuses_spent_branch`` it also
    asks that the branch carried a closed pull request created during this
    run, that the candidate does not contain that one's head revision, and
    that the report names that one too.

    With ``accepts_stream_pull_request`` the two "since this run started"
    clauses measure instead from when the first unit on the case's audit
    stream began (``EVAL_STREAM_STARTED_AT``, which ``hack/ci-eval-pr.sh``
    exports with the audit id in ``EVAL_AUDIT_STREAM``), so a pull request an
    earlier unit on the stream opened passes when the reply names it -- and
    only if its head branch is one that audit's ``finish`` names
    (``platform-agent/fix-<audit>-``) and it is in the job's GitOps repository
    (``EVAL_STREAM_REPO``; without it the window is not widened). The stamp bounds when; the branch is
    what says the pull request is the stream's and not another case's in the
    same repository. That is for a case whose later runs meet a pull
    request an earlier run left open on the same branch, as a fleet audit's
    remediation case does: ``finish`` names the branch after the files the
    fix touches, so every
    later run of the audit on the stream -- this case's later repetitions, or
    another case auditing the same fleet -- finds the pull request open on it,
    leaves it, and pushes nothing. The presubmit holds no credential to close
    it between units (docs/ci-pool-projects.md 5.3), so without the option
    only the first unit on the stream could pass. A leftover from before the
    stream's first unit -- an earlier job on the pool project -- predates the
    stamp and is still rejected. The branch ties the pull request to the
    audit, not to this case's defect: another case on the same stream opens
    on the same branch prefix, and a later repetition passes by naming the
    one repetition 1 opened. Run through ``devops-bench`` directly, without
    both variables, the clauses measure from the run as they otherwise do.

    WHICH ENDPOINT. ``/issues/{n}`` first: a pull request is an issue to that
    API, the response carries ``created_at``, and it is the endpoint the read
    credential is known to reach (``issues: read`` — see
    :class:`LedgerIssueContainsVerifier`). ``/pulls/{n}`` is tried only when
    that answers 401/403/404, which separates a number that is not there from a
    credential that cannot see pull requests. Denied by both is
    ``status="error"`` naming the permission to add, never a fail: an
    unreadable API is the absence of an observation. 404 on both is either the
    number or a repository this credential cannot see; nothing in the API
    separates them, so both are graded as absence. ``_head_push`` then reads
    ``/pulls/{n}`` outright, and the ``reuses_spent_branch`` clause reads
    ``/pulls`` endpoints whatever the first answer was, so both need
    ``pull_requests: read`` -- ``hack/ci-eval-pr.sh`` mints it.
    """

    type: Literal["pull_request_opened"]
    # The organisation the pull request must sit under, "" for any. The eval
    # GitOps repositories are `gke-agentic/<project>-infra`, so the org half is
    # a fair exact match across every pool project and breaks loudly if the org
    # moves.
    owner: str = ""
    # Tolerance between GitHub's creation stamp and the harness's run-start
    # clock, which are two different machines. Small on purpose: every second
    # of it is a second of a previous rep's pull request reading as this one's.
    max_clock_skew_sec: float = Field(default=120.0, ge=0)
    # Also require that the pull request's branch is one a pull request closed
    # during this run already used -- the second proposal on a spent name. A
    # worker refused the name can open the same change on a fresh branch and
    # pass every other clause here, which is exactly the outcome a reuse case
    # must fail. And it must not contain the closed one's head revision: a
    # worker that clones the spent branch and adds to it lands on the same
    # name with the rejected change carried along. Listed from
    # `/pulls?state=closed&head=` and `/pulls/{n}/commits`, so the credential
    # needs `pull_requests: read`.
    reuses_spent_branch: bool = False
    # Measure "written since" and "head commit since" from the first unit on
    # the case's audit stream rather than the run, so a later run of the audit
    # passes on the pull request an earlier one opened and this one found
    # already open. See the docstring.
    accepts_stream_pull_request: bool = False

    def _spent_before(
        self,
        owner: str,
        repo: str,
        number: int,
        pull: dict,
        listing: tuple[int, list] | None,
        token: str,
        budget: float,
        started: datetime,
        named: set[int],
    ) -> tuple[str | None, str | None]:
        """``(rejection, unevaluable)`` for :attr:`reuses_spent_branch`; both None passes.

        ``pull`` is the pulls payload and ``listing`` the ``(page, commits)``
        of the commit listing, both as :meth:`_head_push` read them: the
        issues endpoint's answer carries no head ref, and reading
        ``/pulls/{n}`` or that page a second time would spend the budget on
        the same answer. ``named`` is every pull request number the report
        names in this repository.
        """
        slug = f"{owner}/{repo}#{number}"
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        ref = str(head.get("ref") or "")
        if not ref:
            return None, f"GitHub returned no head ref for {slug}; this check could not be evaluated"
        status_code, listed = _http_get_json(
            f"https://api.github.com/repos/{owner}/{repo}/pulls"
            f"?state=closed&head={owner}:{urllib.parse.quote(ref, safe='')}&per_page={_GITHUB_PAGE_SIZE}",
            token,
            budget,
        )
        if status_code != 200 or not isinstance(listed, list):
            return None, (
                f"GitHub answered {status_code} listing the closed pull requests from "
                f"{ref}; add `pull_requests: read` if that is 403 — this check could "
                "not be evaluated"
            )
        # Every one this run opened and closed, not only the newest: a worker
        # that opens and closes a second proposal on the name and then rebuilds
        # on the first's revision carries the first's change, not the second's.
        spent = []
        for earlier in listed:
            if not isinstance(earlier, dict) or earlier.get("number") == number:
                continue
            created = _parse_github_time(earlier.get("created_at"))
            if created and (started - created).total_seconds() <= self.max_clock_skew_sec:
                spent.append(earlier)
        if spent:
            # The name alone is not the reuse. A worker that clones the spent
            # branch and publishes on top of it also lands on the same name,
            # and its proposal carries the closed one's revisions -- the
            # rejected change, back under review. A branch cut fresh from the
            # base carries none of them.
            carried = {}
            for closed in spent:
                sha = str((closed.get("head") or {}).get("sha") or "")
                if not sha:
                    return None, (
                        f"GitHub returned no head revision for #{closed.get('number')}, so "
                        f"whether {slug} builds on it could not be read; this check "
                        "could not be evaluated"
                    )
                carried[sha] = closed.get("number")
            # Oldest first, so on a long branch the closed revision sits on an
            # early page and a fresh cut's own commits fill the later ones; read
            # every page, up to the 250 commits this endpoint ever lists. The
            # payload's total says how many that is; without one, read until a
            # short page.
            total = pull.get("commits")
            known = isinstance(total, int) and total >= 1
            pages = (
                min(
                    _PR_COMMITS_MAX_PAGES,
                    (total + _PR_COMMITS_PAGE_SIZE - 1) // _PR_COMMITS_PAGE_SIZE,
                )
                if known
                else _PR_COMMITS_MAX_PAGES
            )
            for page in range(1, pages + 1):
                if listing is not None and page == listing[0]:
                    commits = listing[1]
                    status_code = 200
                else:
                    status_code, commits = _http_get_json(
                        f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/commits"
                        f"?per_page={_PR_COMMITS_PAGE_SIZE}&page={page}",
                        token,
                        budget,
                    )
                if status_code != 200 or not isinstance(commits, list):
                    return None, (
                        f"GitHub answered {status_code} listing the commits of {slug}; "
                        "add `pull_requests: read` if that is 403 — this check could "
                        "not be evaluated"
                    )
                found = next(
                    (c["sha"] for c in commits if isinstance(c, dict) and c.get("sha") in carried),
                    None,
                )
                if found:
                    return (
                        f"{slug}: it builds on {found[:12]}, the last revision of the "
                        f"closed pull request #{carried[found]}, so the spent branch "
                        "was added to rather than cleared and the closed change is back "
                        "under review",
                        None,
                    )
                if not known and len(commits) < _PR_COMMITS_PAGE_SIZE:
                    break
            # The task asks the report for both pull requests, and the inject
            # lane's write safeguard excuses a pull request this run opened
            # only when the report names it. Graded here too, so the two
            # checks agree on the same reply instead of one passing a run the
            # other reds.
            # From `spent`, not `carried`: two closed at one revision share a
            # key there, and both are writes the safeguard reads.
            unnamed = sorted(
                c.get("number") for c in spent if c.get("number") not in named
            )
            if unnamed:
                return (
                    f"{slug}: the report does not name "
                    + ", ".join(f"#{n}" for n in unnamed)
                    + f", the pull request this run opened and closed on {ref}, "
                    "so it cannot be told apart from a write nobody asked for",
                    None,
                )
            return None, None
        return (
            f"{slug}: its branch {ref} carries no pull request that this run opened "
            "and closed, so this is not a second proposal on a spent name",
            None,
        )

    def _resolve(
        self, owner: str, repo: str, number: int, token: str, budget: float
    ) -> tuple[dict | None, str | None]:
        """``(payload, None)`` when resolved, ``(None, reason)`` when unevaluable.

        ``(None, None)`` is the third answer: no such pull request, which is a
        rejected candidate rather than a broken check. Only a credential the
        API refuses is a broken check -- that is a fault of ours, it is the
        same for every repetition, and no grade drawn from it would mean
        anything. Everything else the agent chose, so it is graded.
        """
        base = f"https://api.github.com/repos/{owner}/{repo}"
        first, payload = _http_get_json(f"{base}/issues/{number}", token, budget)
        status_code = first
        if first in (401, 403, 404):
            status_code, payload = _http_get_json(f"{base}/pulls/{number}", token, budget)
        if 401 in (first, status_code):
            # 401 is the credential itself, not its scopes: an installation
            # token lasts an hour, and telling the reader to widen a permission
            # sends them to the App's settings for a fault that is in the mint.
            return None, (
                f"GitHub answered 401 for {owner}/{repo}#{number}: the token in "
                f"{LEDGER_TOKEN_ENV_VARS[0]} is not valid — an installation token "
                "expires an hour after it is minted — so this check could not be "
                "evaluated"
            )
        if status_code == 200 and isinstance(payload, dict):
            return payload, None
        if first == 403 and status_code == 403:
            return None, (
                f"GitHub denied {owner}/{repo}#{number} on both endpoints: the token "
                f"behind {LEDGER_TOKEN_ENV_VARS[0]} can reach that repository but "
                "read neither its issues nor its pull requests — add "
                "`pull_requests: read` to the installation — so this check could "
                "not be evaluated"
            )
        if status_code in (403, 404):
            # Absence, and graded as such. A 403 from one endpoint proves the
            # repository is reachable, so the other endpoint's 404 is the
            # number's own. 404 from both is either the number or a repository
            # this credential cannot see -- and an onboarding gap belongs to
            # `scripts/verify_ci_pool_project.py`, which checks installation
            # membership, not to a grading check that would have to red every
            # open pull request to report it.
            return None, None
        return None, (
            f"unexpected GitHub response {status_code} for "
            f"{owner}/{repo}#{number}; this check could not be evaluated"
        )

    def _head_push(
        self,
        owner: str,
        repo: str,
        number: int,
        resolved: dict,
        token: str,
        budget: float,
    ) -> tuple[int | None, datetime | None, dict, tuple[int, list] | None, str | None]:
        """``(changed files, head commit date, pull, listing, unevaluable reason)``.

        ``pull`` is the pulls payload and ``listing`` the ``(page, commits)``
        of the commit listing it read (``None`` when it read none), for
        :meth:`_spent_before`; ``pull`` is empty when the payload was not read.

        Both reads want ``pull_requests: read``. ``/pulls/{n}`` carries the
        file count and the commit total, and is skipped when ``_resolve``
        already fell through to it; dating the head commit needs the commits
        listing, whose last page holds it. ``/commits/{sha}`` would be one call
        and wants ``contents: read``, which grading does not carry.

        A page GitHub has not got (404, or one the head is not on) dates
        nothing, comes back ``None``, and the caller does not reject on it: an
        observation the API would not give is not evidence that a run pushed
        nothing. A page it would not serve -- 401, 403, a 5xx -- is the
        credential's or GitHub's fault, and is an unevaluable reason exactly
        as on ``/pulls/{n}``; folding it into ``None`` would pass a leftover
        the run only wrote to.
        """
        base = f"https://api.github.com/repos/{owner}/{repo}"
        payload = resolved
        if "changed_files" not in payload:
            status, payload = _http_get_json(f"{base}/pulls/{number}", token, budget)
            # The same three readings `_resolve` gives this endpoint: 401 is
            # the token, 403 is the permission, anything else is GitHub's.
            if status == 401:
                return (
                    None,
                    None,
                    {},
                    None,
                    f"GitHub answered 401 for {owner}/{repo}#{number} on the pulls "
                    f"endpoint: the token in {LEDGER_TOKEN_ENV_VARS[0]} is not valid — "
                    "an installation token expires an hour after it is minted — so "
                    "this check could not be evaluated",
                )
            if status == 403:
                return (
                    None,
                    None,
                    {},
                    None,
                    f"GitHub denied {owner}/{repo}#{number} on the pulls endpoint; "
                    f"the token behind {LEDGER_TOKEN_ENV_VARS[0]} needs "
                    "`pull_requests: read` to grade what a run pushed, so this "
                    "check could not be evaluated",
                )
            if status != 200 or not isinstance(payload, dict):
                return (
                    None,
                    None,
                    {},
                    None,
                    f"unexpected GitHub response {status} for {owner}/{repo}#{number} "
                    "on the pulls endpoint; this check could not be evaluated",
                )
        changed = payload.get("changed_files")
        changed = changed if isinstance(changed, int) else None
        total = payload.get("commits")
        head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
        head_sha = head.get("sha") or ""
        if not isinstance(total, int) or total < 1:
            return changed, None, payload, None, None
        page = (total + _PR_COMMITS_PAGE_SIZE - 1) // _PR_COMMITS_PAGE_SIZE
        status, commits = _http_get_json(
            f"{base}/pulls/{number}/commits"
            f"?per_page={_PR_COMMITS_PAGE_SIZE}&page={page}",
            token,
            budget,
        )
        if status == 404:
            return changed, None, payload, None, None
        if status == 401:
            return (
                None,
                None,
                {},
                None,
                f"GitHub answered 401 for {owner}/{repo}#{number} on the commits "
                f"page: the token in {LEDGER_TOKEN_ENV_VARS[0]} is not valid — "
                "an installation token expires an hour after it is minted — so "
                "this check could not be evaluated",
            )
        if status == 403:
            return (
                None,
                None,
                {},
                None,
                f"GitHub denied {owner}/{repo}#{number} on the commits page; "
                f"the token behind {LEDGER_TOKEN_ENV_VARS[0]} needs "
                "`pull_requests: read` to grade what a run pushed, so this "
                "check could not be evaluated",
            )
        if status != 200 or not isinstance(commits, list):
            return (
                None,
                None,
                {},
                None,
                f"unexpected GitHub response {status} for {owner}/{repo}#{number} "
                "on the commits page; this check could not be evaluated",
            )
        listing = (page, commits)
        for entry in reversed(commits):
            if not isinstance(entry, dict) or entry.get("sha") != head_sha:
                continue
            committer = (entry.get("commit") or {}).get("committer") or {}
            return changed, _parse_github_time(committer.get("date")), payload, listing, None
        return changed, None, payload, listing, None

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()

        def done(
            success: bool,
            reason: str,
            *,
            status: str | None = None,
            raw: dict | None = None,
        ) -> VerificationResult:
            return VerificationResult(
                success=success,
                status=status,
                elapsed_time=time.monotonic() - start,
                reason=reason,
                raw=raw,
            )

        snap = transcript.get()
        if snap is None:
            return done(False, _NO_TRANSCRIPT_REASON, status="error")
        if not snap.started_at:
            return done(False, _NO_PR_RUN_CLOCK_REASON, status="error")
        token = next(
            (v for v in (os.environ.get(n) for n in LEDGER_TOKEN_ENV_VARS) if v), None
        )
        if not token:
            return done(False, _NO_TOKEN_REASON, status="error")

        seen: list[tuple[str, str, int]] = []
        for owner, repo, number in _PULL_URL_RE.findall(snap.final_message):
            key = (owner, repo, int(number))
            if key not in seen:
                seen.append(key)
        if not seen:
            return done(False, _NO_PR_URL_REASON)
        if len(seen) > _MAX_PR_CANDIDATES:
            return done(
                False,
                f"the run's report names {len(seen)} distinct pull request URLs; a "
                f"remediation proposes one fix, so more than {_MAX_PR_CANDIDATES} is "
                "not a set of candidates worth resolving",
            )

        started = datetime.fromtimestamp(snap.started_at, tz=timezone.utc)
        # The floor every "since" clause below measures from: the run, or with
        # accepts_stream_pull_request the stream's first unit, when the harness
        # exported one.
        since, since_what = started, "this run started"
        stream_branch = ""
        if self.accepts_stream_pull_request and not _stream_audit():
            # The option does nothing off a stream, and a rejection that only
            # said "before this run started" would hide that it was dropped.
            since_what = (
                "this run started (`accepts_stream_pull_request` is set, but "
                f"{STREAM_AUDIT_ENV_VAR} is not: the harness puts a case on an "
                "audit stream only through a `ledger_issue_contains` check with "
                "an `audit` key)"
            )
        elif self.accepts_stream_pull_request and not _stream_repo():
            # Without the job's repository a sibling job's pull request on the
            # same audit's branch in another pool repository would pass.
            since_what = (
                "this run started (`accepts_stream_pull_request` is set and the "
                f"case is on an audit stream, but {STREAM_REPO_ENV_VAR} is not: "
                "without the job's GitOps repository the widened window cannot "
                "tell this job's pull request from a sibling job's)"
            )
        elif self.accepts_stream_pull_request:
            stream_started = _stream_started()
            if stream_started is not None and stream_started < started:
                since, since_what = stream_started, "this audit stream's first run began"
                stream_branch = f"{REMEDIATION_BRANCH_PREFIX}{_stream_audit()}-"
            elif stream_started is None:
                since_what = (
                    "this run started (`accepts_stream_pull_request` is set and the "
                    f"case is on an audit stream, but {STREAM_STARTED_ENV_VAR} is "
                    "missing or unreadable, so the window was not widened)"
                )
            else:
                # Never narrowed below the run: a late stamp is a stale window
                # file or a clock step, not a later start.
                since_what = (
                    "this run started (`accepts_stream_pull_request` is set, but "
                    f"{STREAM_STARTED_ENV_VAR} ({stream_started.isoformat()}) is not "
                    "before this run, so the window was not widened)"
                )
        budget = single_call_timeout(timeout_sec)
        rejected: list[str] = []
        # A candidate the API cannot answer for only ends the check if nothing
        # else resolves. An agent that mistypes a repository slug beside the
        # real URL would otherwise error, and an error is rung 2, which reds the
        # eval job for every open pull request.
        unresolved: list[str] = []
        for owner, repo, number in seen:
            slug = f"{owner}/{repo}#{number}"
            if self.owner and owner.lower() != self.owner.lower():
                rejected.append(f"{slug}: not under {self.owner}")
                continue
            try:
                payload, unevaluable = self._resolve(owner, repo, number, token, budget)
            except OSError as exc:
                unresolved.append(f"could not reach the GitHub API for {slug}: {exc}")
                continue
            if payload is None:
                if unevaluable is None:
                    rejected.append(f"{slug}: no such pull request (404)")
                else:
                    unresolved.append(unevaluable)
                continue
            # `pull_request` is how the issues endpoint marks one; `head` is
            # what the pulls endpoint returns instead. Neither means the URL
            # said /pull/ over a number that is a plain issue.
            if not payload.get("pull_request") and "head" not in payload:
                rejected.append(f"{slug}: that number is an issue, not a pull request")
                continue
            merged_at = payload.get("merged_at") or (
                payload.get("pull_request") or {}
            ).get("merged_at")
            if str(payload.get("state") or "").lower() == "closed" and not merged_at:
                # Closing moves `updated_at`, so without this a run that closed
                # a leftover -- or its own pull request -- would read as one
                # that wrote a fix. The objective is that the fix went out.
                rejected.append(f"{slug}: closed without being merged")
                continue
            created = _parse_github_time(payload.get("created_at"))
            if created is None:
                rejected.append(f"{slug}: GitHub returned no readable created_at")
                continue
            # Creation is not the only way a run owns a pull request: the
            # submit-suggestion skill derives the branch from the change, so a
            # later rep pushes onto the branch the first one used, `gh pr
            # create` answers "already exists", and the skill edits that pull
            # request and returns its URL. That work lands in `updated_at`
            # alone. The stamp moves on any write by anyone, so passing here is
            # necessary and not sufficient -- the head commit check below is
            # what says the run pushed something. A rep that resubmits
            # byte-identical content writes nothing at all (the skill raises
            # before the push), so a correct rep lands here too, which is why
            # the reason names both readings.
            updated = _parse_github_time(payload.get("updated_at"))
            touched = updated if updated and updated > created else created
            age = (since - touched).total_seconds()
            if age > self.max_clock_skew_sec:
                rejected.append(
                    f"{slug}: last written at {touched.isoformat()}, {age:.0f}s "
                    f"BEFORE {since_what} ({since.isoformat()}) — a leftover "
                    "an earlier run opened, which this run either quoted or "
                    "resubmitted unchanged"
                )
                continue
            # What the stamp above cannot say: whether the run pushed a fix or
            # only wrote to a pull request. `updated_at` moves on a comment and
            # on a label. The head commit moves on neither.
            try:
                changed, pushed, pull, listing, unevaluable = self._head_push(
                    owner, repo, number, payload, token, budget
                )
            except OSError as exc:
                unresolved.append(f"could not reach the GitHub API for {slug}: {exc}")
                continue
            if unevaluable:
                unresolved.append(unevaluable)
                continue
            if changed == 0:
                rejected.append(
                    f"{slug}: changes no files, so it carries no proposed fix"
                )
                continue
            if pushed and (since - pushed).total_seconds() > self.max_clock_skew_sec:
                rejected.append(
                    f"{slug}: its head commit dates from {pushed.isoformat()}, "
                    f"before {since_what} ({since.isoformat()}) — this run "
                    "wrote to a pull request an earlier one pushed the fix to"
                )
                continue
            # Only the widened window needs the branch: a pull request this
            # run wrote and pushed to is this run's whatever it is called.
            skew = self.max_clock_skew_sec
            if stream_branch and (
                (started - touched).total_seconds() > skew
                or (pushed and (started - pushed).total_seconds() > skew)
            ):
                if f"{owner}/{repo}".lower() != _stream_repo():
                    rejected.append(
                        f"{slug}: last written or pushed to before this run "
                        f"started, in a repository other than this job's "
                        f"({_stream_repo()}) — another job's pull request on "
                        "the same audit stream, not this one's"
                    )
                    continue
                head = str(((pull or payload).get("head") or {}).get("ref") or "")
                if not head.startswith(stream_branch):
                    rejected.append(
                        f"{slug}: last written or pushed to before this run "
                        f"started, on branch "
                        f"{head or '(unreadable)'!r}, which is not one this audit "
                        f"stream's `finish` names ({stream_branch}*) — another "
                        "case's pull request, not the stream's"
                    )
                    continue
            if self.reuses_spent_branch:
                try:
                    rejection, unevaluable = self._spent_before(
                        owner, repo, number, pull, listing, token, budget, started,
                        {n for o, r, n in seen
                         if o.lower() == owner.lower() and r.lower() == repo.lower()},
                    )
                except OSError as exc:
                    unresolved.append(f"could not reach the GitHub API for {slug}: {exc}")
                    continue
                if unevaluable:
                    unresolved.append(unevaluable)
                    continue
                if rejection:
                    rejected.append(rejection)
                    continue
            # Within the skew allowance a pull request a hair older than the
            # run is still this run's; "an earlier repetition" only when the
            # widened window is what admitted it.
            during = (
                "during this run"
                if since == started or (started - touched).total_seconds() <= self.max_clock_skew_sec
                else f"by an earlier run on this audit stream (found already open; "
                f"{since_what} at {since.isoformat()})"
            )
            return done(
                True,
                f"{slug} was {'opened' if touched == created else 'updated'} at "
                f"{touched.isoformat()}, {during}, and carries "
                f"{changed if changed is not None else 'an unreported number of'} "
                "changed file(s)"
                + (", on a branch this run's closed pull request had used"
                   if self.reuses_spent_branch else ""),
                raw={
                    "pull_request": slug,
                    "created_at": created.isoformat(),
                    "updated_at": updated.isoformat() if updated else None,
                    "changed_files": changed,
                    "head_committed_at": pushed.isoformat() if pushed else None,
                },
            )

        if unresolved:
            return done(
                False,
                "no pull request URL in the report resolved: " + "; ".join(unresolved)
                + (f"; also rejected: {'; '.join(rejected)}" if rejected else ""),
                status="error",
            )
        return done(
            False,
            "none of the pull request URLs the report names was opened or "
            f"pushed to since {since_what}: " + "; ".join(rejected),
        )


# ------------------------------------------------- pull request diff

# Page size and bound for the pull request's file listing. A remediation
# touches a handful of files; GitHub serves up to 3000 across pages, and a
# diff past this bound is graded on the pages read and says so.
_PR_FILES_PAGE_SIZE = 100
_PR_FILES_MAX_PAGES = 3

_NO_DIFF_PHRASES_REASON = (
    "pull_request_diff_contains asserts nothing: set required_phrases or "
    "any_of_phrases (forbidden_phrases alone cannot say a proposal was made)"
)
_BLANK_DIFF_PHRASE_REASON = (
    "pull_request_diff_contains has a blank phrase: an empty string is in every diff, "
    "so it asserts nothing"
)
_NO_DIFF_TOKEN_REASON = (
    "no GitHub read credential in the environment: set one of "
    f"{', '.join(LEDGER_TOKEN_ENV_VARS)} to a token with pull_requests: read on "
    "the eval GitOps repository, or this check cannot be evaluated"
)
# What a unified diff marks an added line with. GitHub's `patch` field is
# hunk text alone, from the `@@` header on, so there is no `+++ b/file` line
# to tell apart.
_DIFF_ADDED_PREFIX = "+"
# A unified diff is newline-delimited. str.splitlines would also break on a
# form feed, a bare CR, NEL or U+2028 inside a file's content, which GitHub
# passes through its patch verbatim, and would then read a removed line's
# tail as an added line, or one added CR-only line as many unprefixed ones.
_DIFF_LINE_SEPARATOR = "\n"
# report_contains' noise table without the underscore. In a chat reply "_"
# is Markdown emphasis; in a diff it separates identifiers, and dropping it
# would read pod_disruption_budget and min_available as the manifest's nouns.
_DIFF_NOISE = str.maketrans({"*": None, "`": None, "’": "'"})
# Where a run that knows the repository it writes to (owner/name) says so;
# hack/ci-eval-pr.sh exports it from the project mapping on every lane
# (tests/test_ci_eval_gitops_repo_export.py pins the name to this constant).
# Set, it binds the candidate pull request to that repository; unset, the
# `owner` pin is the only repository bind.
_GITOPS_REPO_ENV_VAR = "BENCH_GITOPS_REPO"
# Two of the pool sweep's three ownership checks (is_agent_pull_request in
# hack/ci_sweep_agent_pulls.py, from forge.py), and a weaker third: the head
# branch carries the agent's prefix (AGENT_BRANCH_PREFIX in
# agents/platform/scripts/forge.py, pinned by a test), the head is in the
# repository itself, and the author is a GitHub App. The sweep's third is the
# agent's own login, which it learns from the minter App it holds the key
# for; this check's credential is the ledger App's, which does not know that
# login, so `user.type` is what it can ask. A human with push access naming
# a branch under the prefix is excluded; another App pushing to one is not.
_AGENT_BRANCH_PREFIX = "platform-agent/"
_APP_USER_TYPE = "Bot"


def _normalize_diff(text: str) -> str:
    """``_normalize`` for a diff and the phrases matched against it: the
    underscore stays (``_DIFF_NOISE`` says why); everything else is the same,
    so a phrase written for ``report_contains`` matches here as it does there
    unless it leans on an underscore being dropped."""
    return _normalize_with(text, _DIFF_NOISE)


@VERIFIERS.register("pull_request_diff_contains")
class PullRequestDiffContainsVerifier(BaseVerifier):
    """Phrase checks against the diff of the pull request the reply points at.

    WHY THIS EXISTS. A proposal case asks for a concrete manifest and grades
    the reply for its load-bearing nouns. The Planning Agent, which today's
    api transport reaches, inlines the manifest; the platform persona, which
    the inject door reaches directly, follows its own rule and opens a pull
    request instead, naming the URL and nothing of the manifest (#2037:
    ``obtainability-remediation-proposal`` 12 of 12 on one, 0 of 3 on the
    other, for the same proposal). The proposal is in the diff, so this check
    reads it there: the same phrase semantics as ``report_contains``, over
    the added lines of every file the pull request changes, never the file
    names.

    WHAT IT ASSERTS, AND WHAT IT DOES NOT. The reply names a github.com pull
    request URL under ``owner`` and, when the run was told the repository it
    writes to (``BENCH_GITOPS_REPO``, which ``hack/ci-eval-pr.sh`` exports from the
    project mapping on every lane), in that repository; GitHub resolves it as an OPEN pull
    request whose head is an agent branch in the repository itself; and the
    added lines of its patches carry every phrase in ``required_phrases``, at
    least one of ``any_of_phrases``, and none of ``forbidden_phrases``, after
    the normalisation ``report_contains`` applies with one difference: the
    underscore is kept on both sides, since in a diff it separates
    identifiers rather than marking emphasis, so a snake_case comment is not
    the camelCase manifest. Open, not merged: the
    skill's "already exists" path lands on open pull requests only, so a
    merged one named in a reply is quoted, not proposed, and a merged
    manifest anywhere in the organisation is not a standing pass. It does NOT
    ask whether this run opened or pushed to the pull request: that is
    ``pull_request_opened``'s question, and a case that needs both declares
    both. Here a repetition that recomputed the same
    manifest, found the branch already carrying it and pointed at the open
    pull request (the skill's "already exists" path, which the measurement
    run's third repetition took: it refused "nothing to commit" and named the
    pull request its predecessor had pushed to) has proposed a concrete
    manifest, and the proposal is what the objective grades. The cost of that
    choice is stated rather than hidden: a reply that only quotes an earlier
    lease's open pull request carrying the manifest passes this arm too, the
    leftover grading #1755 describes, and the same-repository, agent-branch,
    open-only binds above are what keep that cost to exactly that shape. It
    is accepted here because requiring a push would fail the correct third
    repetition, because the sweep closes the agent's pull requests between
    leases when it reaches them, and because a case that must prove the write
    declares ``pull_request_opened`` beside this. Open-only is safe against
    that sweep: ``hack/ci_sweep_agent_pulls.py`` acquires only projects Boskos
    reports free (``FREE_STATE`` in ``hack/boskos_pool.py``), so a leased
    run's pull request stays open until this check reads it. What the run wrote to GitHub is not this
    objective's question; ``pull_request_opened`` asks it. Only ADDED lines
    of each patch are matched, and file names are not: a pull request that
    deletes a budget, or edits a line beside one, carries the nouns in its
    context and removed lines and proposes nothing, and a file named for the
    kind and its selector is a name, not a manifest.

    WHICH ENDPOINT. ``/pulls/{n}`` for the state, then ``/pulls/{n}/files``
    for the patches; both want ``pull_requests: read``, which
    ``hack/ci-eval-pr.sh`` asks for at mint. A 401, a 403, an unexpected
    status or an unreachable API is a candidate that could not be evaluated,
    and ends the check as ``status="error"`` only when no other candidate
    passes; a 404 is a pull request that is not there, rejected. A file whose
    ``patch`` GitHub omits (binary, or too large) is noted; the phrases are
    matched over what was served.
    """

    type: Literal["pull_request_diff_contains"]
    # The organisation the pull request must sit under, "" for any; the same
    # pin `pull_request_opened` carries.
    owner: str = ""
    required_phrases: list[str] = Field(default_factory=list)
    any_of_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _asserts_something(self) -> PullRequestDiffContainsVerifier:
        if not self.required_phrases and not self.any_of_phrases:
            raise ValueError(_NO_DIFF_PHRASES_REASON)
        # A blank phrase is in every text there has ever been, so a list of
        # blanks asserts nothing while looking populated; the case linter
        # refuses the shape for a task file, and this refuses it for an
        # inline spec or a hand-built verifier.
        phrases = (*self.required_phrases, *self.any_of_phrases, *self.forbidden_phrases)
        if any(not _normalize_diff(p).strip() for p in phrases):
            raise ValueError(_BLANK_DIFF_PHRASE_REASON)
        return self

    def _diff(
        self, owner: str, repo: str, number: int, token: str, budget: float
    ) -> tuple[str | None, str | None, list[str]]:
        """``(diff text, None, notes)`` when read, ``(None, reason, notes)``
        when the candidate could not be evaluated, ``(None, None, notes)``
        when it is not a gradable pull request (absent, an issue, closed
        unmerged); the notes say why."""
        base = f"https://api.github.com/repos/{owner}/{repo}"
        slug = f"{owner}/{repo}#{number}"
        status, payload = _http_get_json(f"{base}/pulls/{number}", token, budget)
        if status == 401:
            return None, (
                f"GitHub answered 401 for {slug}: the token in {LEDGER_TOKEN_ENV_VARS[0]} is "
                "not valid — an installation token expires an hour after it is minted — so "
                "this check could not be evaluated"
            ), []
        if status == 403:
            return None, (
                f"GitHub denied {slug}; the token behind {LEDGER_TOKEN_ENV_VARS[0]} needs "
                "`pull_requests: read` to read the diff, so this check could not be evaluated"
            ), []
        if status == 404:
            return None, None, [f"{slug}: no such pull request (404)"]
        if status != 200 or not isinstance(payload, dict) or "head" not in payload:
            return None, (
                f"unexpected GitHub response {status} for {slug}; this check could not be "
                "evaluated"
            ), []
        if str(payload.get("state") or "").lower() != "open":
            return None, None, [
                f"{slug}: {'merged' if payload.get('merged_at') else 'closed without being merged'}, "
                "so a reply naming it quotes a pull request rather than proposing one"
            ]
        head = payload.get("head") or {}
        head_ref = str(head.get("ref") or "")
        head_repo = str((head.get("repo") or {}).get("full_name") or "")
        author = payload.get("user") or {}
        if not head_ref.startswith(_AGENT_BRANCH_PREFIX) or head_repo.lower() != f"{owner}/{repo}".lower():
            return None, None, [
                f"{slug}: its head {head_repo}:{head_ref} is not an agent branch "
                f"({_AGENT_BRANCH_PREFIX}* in the repository itself), so it is not a pull "
                "request the agent opened"
            ]
        if str(author.get("type") or "") != _APP_USER_TYPE:
            return None, None, [
                f"{slug}: opened by {author.get('login') or 'an unknown user'}, not by a GitHub "
                "App, so it is not a pull request the agent opened"
            ]
        chunks: list[str] = []
        notes: list[str] = []
        for page in range(1, _PR_FILES_MAX_PAGES + 1):
            status, files = _http_get_json(
                f"{base}/pulls/{number}/files?per_page={_PR_FILES_PAGE_SIZE}&page={page}",
                token,
                budget,
            )
            if status != 200 or not isinstance(files, list):
                return None, (
                    f"unexpected GitHub response {status} for {slug}'s file listing; this "
                    "check could not be evaluated"
                ), notes
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("filename") or "")
                patch = entry.get("patch")
                if not isinstance(patch, str):
                    notes.append(f"{name}: no patch served (binary or too large)")
                    patch = ""
                added = [
                    line[len(_DIFF_ADDED_PREFIX) :]
                    for line in patch.split(_DIFF_LINE_SEPARATOR)
                    if line.startswith(_DIFF_ADDED_PREFIX)
                ]
                # Added lines only, never the file name: a path such as
                # `PodDisruptionBudget-selector-notes.md` would otherwise
                # supply the manifest's nouns with no manifest behind it.
                chunks.append("\n".join(added))
            if len(files) < _PR_FILES_PAGE_SIZE:
                break
        else:
            notes.append(
                f"{slug} changes at least {_PR_FILES_PAGE_SIZE * _PR_FILES_MAX_PAGES} files; "
                "graded on the first pages"
            )
        return "\n".join(chunks), None, notes

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()

        def done(
            success: bool,
            reason: str,
            *,
            status: str | None = None,
            raw: dict | None = None,
        ) -> VerificationResult:
            return VerificationResult(
                success=success,
                status=status,
                elapsed_time=time.monotonic() - start,
                reason=reason,
                raw=raw,
            )

        snap = transcript.get()
        if snap is None:
            return done(False, _NO_TRANSCRIPT_REASON, status="error")
        seen: list[tuple[str, str, int]] = []
        for owner, repo, number in _PULL_URL_RE.findall(snap.final_message):
            key = (owner, repo, int(number))
            if key not in seen:
                seen.append(key)
        # Before the token: a reply that names no pull request is a plain
        # fail, and a hand-driven run whose reply inlined the manifest
        # should not error here for want of a credential it never needed.
        if not seen:
            return done(False, _NO_PR_URL_REASON)
        if len(seen) > _MAX_PR_CANDIDATES:
            return done(
                False,
                f"the run's report names {len(seen)} distinct pull request URLs; a "
                f"proposal points at one, so more than {_MAX_PR_CANDIDATES} is not a set of "
                "candidates worth resolving",
            )
        token = next(
            (v for v in (os.environ.get(n) for n in LEDGER_TOKEN_ENV_VARS) if v), None
        )
        if not token:
            return done(False, _NO_DIFF_TOKEN_REASON, status="error")

        budget = single_call_timeout(timeout_sec)
        any_of = [_normalize_diff(p) for p in self.any_of_phrases]
        bound_repo = os.environ.get(_GITOPS_REPO_ENV_VAR, "").strip()
        rejected: list[str] = []
        unresolved: list[str] = []
        for owner, repo, number in seen:
            slug = f"{owner}/{repo}#{number}"
            if self.owner and owner.lower() != self.owner.lower():
                rejected.append(f"{slug}: not under {self.owner}")
                continue
            if bound_repo and f"{owner}/{repo}".lower() != bound_repo.lower():
                rejected.append(
                    f"{slug}: not in {bound_repo}, the repository this run writes to "
                    f"({_GITOPS_REPO_ENV_VAR})"
                )
                continue
            try:
                diff, unevaluable, notes = self._diff(owner, repo, number, token, budget)
            except OSError as exc:
                unresolved.append(f"could not reach the GitHub API for {slug}: {exc}")
                continue
            if diff is None:
                if unevaluable:
                    unresolved.append("; ".join([unevaluable, *notes]))
                else:
                    rejected.extend(notes)
                continue
            haystack = _normalize_diff(diff)
            missing = [p for p in self.required_phrases if _normalize_diff(p) not in haystack]
            present_forbidden = [p for p in self.forbidden_phrases if _normalize_diff(p) in haystack]
            any_ok = not any_of or any(p in haystack for p in any_of)
            if missing or present_forbidden or not any_ok:
                parts = []
                if missing:
                    parts.append(f"required phrases absent from its diff: {missing}")
                if not any_ok:
                    parts.append(f"none of the alternative phrasings in its diff: {self.any_of_phrases}")
                if present_forbidden:
                    parts.append(f"forbidden phrases in its diff: {present_forbidden}")
                # The notes ride along: a manifest in a file whose patch
                # GitHub withheld reads as absent, and the note is the only
                # thing that says the diff was never read.
                parts.extend(notes)
                rejected.append(f"{slug}: " + "; ".join(parts))
                continue
            return done(
                True,
                f"{slug}'s diff adds all {len(self.required_phrases)} required phrase(s)"
                + (f", one of {self.any_of_phrases}" if any_of else "")
                + f", none of {len(self.forbidden_phrases)} forbidden"
                + (f" ({'; '.join(notes)})" if notes else ""),
                raw={"pull_request": slug, "notes": notes},
            )

        if unresolved:
            return done(
                False,
                "no pull request URL in the report could be read: " + "; ".join(unresolved)
                + (f"; also rejected: {'; '.join(rejected)}" if rejected else ""),
                status="error",
            )
        return done(
            False,
            "none of the pull request URLs the report names carries the proposal in its diff: "
            + "; ".join(rejected),
        )


# ----------------------------------------------------------- github writes

_NO_GITOPS_REPO_REASON = (
    f"no GitOps repository in the environment: set {github_writes.GITOPS_REPO_ENV_VAR} to "
    "the owner/name the agent under test writes to (hack/ci-eval-pr.sh exports it on the "
    "inject lane from the project mapping), or this check cannot be evaluated"
)

_NO_WRITES_RUN_CLOCK_REASON = (
    "the run's transcript carries no start time (TranscriptSnapshot.started_at "
    "is unset), so this check cannot tell a write this run made from one left "
    "behind by a previous run, and refuses to grade it"
)


@VERIFIERS.register("github_writes")
class GitHubWritesVerifier(BaseVerifier):
    """Did the run write to the case's GitOps repository?

    PASSES when it finds a write, so a task wraps it in ``none`` to say "the
    agent wrote nothing to GitHub": the same shape as a ``fleet_resource_property``
    with ``op: exists`` under ``none``. The inject lane appends exactly that
    entry to every case it runs (``hack/eval/inject-lane-safeguards.yaml``,
    applied by ``hack/ci-eval-pr.sh``), because the cluster safeguards say
    nothing about GitHub and the platform persona the door addresses opens a
    pull request where the chat path inlined a manifest (#2037).

    WHAT IT READS. :func:`kube_agents_bench.github_writes.find_writes` over
    the repository ``BENCH_GITOPS_REPO`` names, from
    ``TranscriptSnapshot.started_at`` less ``max_clock_skew_sec``: every pull
    request under ``branch_prefix`` whose head is in the repository itself and
    that was opened or updated in the window, and every such branch heading no
    pull request whose tip was committed in it (the refs API carries no push
    time, so that is what is measured). The repository comes from the
    environment and not from the reply, since the reply of a run that wrote
    where it should not have may say nothing about it.

    WHAT A CASE MAY REQUEST. A case that asks for a pull request grades it
    with ``pull_request_opened``, and its reply names the URL. Up to
    ``requested_pull_requests`` of the writes whose number that reply names
    are the requested ones and are left out; the lane sets the field to the
    number of ``pull_request_opened`` and ``pull_request_diff_contains``
    leaves the case declares. Anything else is a write the case did not
    ask for.

    HOW A CASE THAT WRITES BY DESIGN IS KEPT AWAY. Writes are dated, not
    signed, and the fan-out runs cases side by side against one repository,
    so the script runs the cases that request a pull request in a second
    phase, after every other unit has finished (``hack/ci-eval-pr.sh``, the
    unit queue): a repetition of a case that requests nothing never shares
    the repository with one that writes by design, and a write inside its
    window is its own or a concurrent sibling's mistake, either of which is
    the red this check exists for. The second phase runs one unit at a time,
    each after a settle as long as ``max_clock_skew_sec`` (the script's
    ``EVAL_GITHUB_WRITE_SETTLE_SECONDS``, pinned equal by a test), so two
    requesting cases never see each other's by-design pull requests and no
    window reaches back into the unit before; each is graded on the pull
    requests its own reply names.
    A pull request that was only commented on, labelled or closed in the
    window is not a write: :func:`kube_agents_bench.github_writes.find_writes`
    reads the head commit before it counts an ``updated_at`` that moved.

    WHAT IT CANNOT SEE. The branch listing wants ``contents: read``, which the
    grading credential does not carry; a listing GitHub refuses is a note in
    the reason and ``raw``, and the check grades on pull requests alone.
    Unreadable pull requests -- a 401, a denial, a repository the credential
    cannot see, an API it could not reach -- are ``status="error"``: the
    absence of an observation, never a pass.
    """

    type: Literal["github_writes"]
    # The organisation the repository must sit under, "" for any. The same
    # pin `pull_request_opened` carries: a fair exact match across every pool
    # project that breaks loudly if the organisation moves -- here as an
    # error, since the repository is the run's configuration, not the reply.
    owner: str = ""
    branch_prefix: str = github_writes.AGENT_BRANCH_PREFIX
    # The bot login the writes must carry, "" for any. Left empty by the lane
    # for the reason github_writes.AGENT_BRANCH_PREFIX gives.
    author: str = ""
    requested_pull_requests: int = Field(default=0, ge=0)
    # Tolerance between GitHub's stamps and the harness's run-start clock,
    # two different machines. Small on purpose, as on pull_request_opened.
    max_clock_skew_sec: float = Field(default=120.0, ge=0)

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()

        def done(
            success: bool,
            reason: str,
            *,
            status: str | None = None,
            raw: dict | None = None,
        ) -> VerificationResult:
            return VerificationResult(
                success=success,
                status=status,
                elapsed_time=time.monotonic() - start,
                reason=reason,
                raw=raw,
            )

        snap = transcript.get()
        if snap is None:
            return done(False, _NO_TRANSCRIPT_REASON, status="error")
        if not snap.started_at:
            return done(False, _NO_WRITES_RUN_CLOCK_REASON, status="error")
        token = next(
            (v for v in (os.environ.get(n) for n in LEDGER_TOKEN_ENV_VARS) if v), None
        )
        if not token:
            return done(False, _NO_TOKEN_REASON, status="error")
        repo = os.environ.get(github_writes.GITOPS_REPO_ENV_VAR, "").strip()
        if not repo or "/" not in repo:
            return done(False, _NO_GITOPS_REPO_REASON, status="error")
        if self.owner and repo.split("/", 1)[0].lower() != self.owner.lower():
            return done(
                False,
                f"{github_writes.GITOPS_REPO_ENV_VAR}={repo} is not under {self.owner}, "
                "the organisation this check is pinned to; the run is misconfigured, so "
                "this check could not be evaluated",
                status="error",
            )

        started = datetime.fromtimestamp(snap.started_at, tz=timezone.utc)
        since = started - timedelta(seconds=self.max_clock_skew_sec)
        client = github_writes.GitHubClient(token, _http_get_json, single_call_timeout(timeout_sec))
        try:
            report = github_writes.find_writes(
                client, repo, since, branch_prefix=self.branch_prefix, author=self.author
            )
        except github_writes.GitHubUnreadable as exc:
            return done(False, str(exc), status="error")
        except OSError as exc:
            return done(
                False,
                f"could not reach the GitHub API for {repo}: {exc}; this check could not "
                "be evaluated",
                status="error",
            )

        requested = {
            int(number)
            for owner, name, number in _PULL_URL_RE.findall(snap.final_message)
            if f"{owner}/{name}".lower() == repo.lower()
        }
        allowance = self.requested_pull_requests
        unrequested = []
        excused = []
        for write in report.writes:
            if allowance and write.number in requested:
                allowance -= 1
                excused.append(write.describe())
                continue
            unrequested.append(write)
        raw = report.as_dict()
        raw.update(
            {
                "repository": repo,
                "since": since.isoformat(),
                "requested": excused,
                "unrequested": [w.describe() for w in unrequested],
            }
        )
        left_out = []
        if excused:
            left_out.append(f"requested and left out: {'; '.join(excused)}")
        tail = ("; " + "; ".join(left_out) if left_out else "") + (
            f" ({'; '.join(report.notes)})" if report.notes else ""
        )
        if unrequested:
            return done(
                True,
                f"{len(unrequested)} write(s) to {repo} since {since.isoformat()} that the "
                f"case did not request: {'; '.join(w.describe() for w in unrequested)}" + tail,
                raw=raw,
            )
        return done(
            False,
            f"no pull request or branch under {self.branch_prefix} was written to {repo} "
            f"since {since.isoformat()} that this repetition has to answer for" + tail,
            raw=raw,
        )


# ------------------------------------------------------------------- fleet


def _item_names(payload: Any) -> list[str]:
    """Every ``metadata.name`` in a ``kubectl get -o json`` document."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    objects = items if isinstance(items, list) else [payload]
    names = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        metadata = obj.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("name"), str):
            names.append(metadata["name"])
    return names


@VERIFIERS.register("fleet_resource_property")
class FleetResourcePropertyVerifier(ResourcePropertyVerifier):
    """``resource_property`` against a seeded-fleet fixture, named by ROLE.

    WHY THIS EXISTS, in two parts.

    **It reads the right cluster.** ``hack/ci-eval-pr.sh`` authenticates once,
    to ``platform-agent-host``, and never switches context, so an upstream
    ``resource_property`` naming ``-n seeded-debug`` resolves against a cluster
    that has no such namespace. Here the check names a fixture ROLE and
    :func:`kube_agents_bench.fleet.kubeconfig_for_role` resolves it to the
    kubeconfig ``hack/fleet-kubeconfigs.sh`` wrote for the cluster that carries
    it, inside whatever project the run leased. A role that does not resolve is
    ``status="error"`` naming the role; it NEVER falls back to the ambient
    kubeconfig, because that fallback is the whole defect.

    Roles rather than cluster names because every eval project carries its own
    trio of seeded clusters. ``bench/tf/fleet/fixtures.json`` is the role
    catalog and the only place the role-to-cluster mapping exists.

    **It can FAIL, not only error.** A safeguard that cannot tell "the agent
    destroyed the fixture" from "the cluster was unreachable" is worse than no
    safeguard, and upstream cannot tell them apart in either direction:

    - ``kubectl get deployment payments-api -n seeded-debug`` exits non-zero
      when the deployment is GONE — the safeguard's whole subject — and
      upstream turns any non-zero kubectl into ``status="error"``. So the
      violation reads as an environmental hiccup.
    - a LIST (``selector``, or a pathless ``absent``) against a namespace that
      does not exist exits ZERO with ``items: []``. So ``op: absent`` on the
      wrong cluster reads as a PASS.

    Classification makes the distinction explicit. The ordinary comparison runs
    first and unchanged; only an answer that rests on an ABSENCE is re-examined,
    because absence is the one observation with two causes:

    1. Resolve the role. Unresolvable → **error**, once, without polling: the
       answer is a fact about the filesystem the runner left behind, and
       re-asking cannot change it inside one run.
    2. Run upstream's own single comparison pass against the resolved
       kubeconfig. If it matched at least one object, it observed the fixture
       on the right cluster and its verdict stands as-is — pass or fail, one
       kubectl call, exactly upstream's cost.
    3. Otherwise (it errored, or it matched nothing — including the pathless
       ``absent`` that would read as a clean pass) list namespaces. Unreachable,
       unauthorized or unparseable → **error**; the check could not be
       evaluated. Past this the cluster demonstrably answered.
    4. If the check names a ``namespace``, require it to exist. Absent →
       **fail** if ``namespace/<ns>`` is a CONFIRMED SUBJECT (below), otherwise
       **error**.
    5. If the check names a ``resource_name``, list that kind and require the
       object. Absent → **fail**, or a pass for a pathless ``absent`` (exactly
       what upstream does with an empty object set) — but only if the subject
       is grounded; otherwise **error**. The list form is used precisely
       because it distinguishes "not there" from "could not ask", which
       ``kubectl get <name>`` cannot.
    6. A ``selector`` check that matched nothing is grounded the same way, and
       becomes **error** when it is not.

    CONFIRMED SUBJECTS are what make steps 4–6 sound. ``hack/fleet-kubeconfigs
    .sh`` probes every object in the role's catalog entry BEFORE the agent runs
    and writes the ones it saw to ``<role>.confirmed``; a role whose probes are
    not all present gets no kubeconfig at all, so it is unresolvable at step 1.
    An absence is charged to the run only when the runner had SEEN that exact
    subject on that exact cluster beforehand — either the object itself
    (``deployment/payments-api``, ``node?<selector>``) or, for a check whose
    subject is a legitimately absent object such as a pathless ``absent``, the
    namespace containing it. Anything else is an environment that was never
    ready, which is an error and not the agent's doing. The first draft of this
    gate confirmed only the NAMESPACE, which the cluster-scoped roles do not
    have: on a live-but-empty cluster ``compliance-rbac-overgrant`` reported a
    catastrophic ``fail`` against an agent that had touched nothing.

    Steps 2–5 run inside the ordinary poll loop, so a transient API blip is
    retried exactly as it would be for an upstream check. Only step 1 sits
    outside it. Two earlier drafts got this wrong in opposite directions: one
    hoisted the whole preflight out of the loop, making a single timeout a
    permanent ``status="error"``; the other ran the preflight unconditionally
    ahead of the comparison, tripling the per-attempt kubectl count and with it
    the chance that the last poll before the deadline is weather rather than
    the violation the earlier polls saw.

    A ``fail`` is STICKY across the poll loop. ``_poll_to_result`` folds in the
    LAST observation even when that observation is ``error``, so a violation
    seen at second 5 is erased by an API blip at second 60 — precisely the
    failure mode this class exists to remove, arriving one layer up. A cluster
    going unreachable does not un-observe what it already answered, so a
    definitive ``fail`` recorded during the loop outranks a trailing ``error``
    and is reported with the blip appended to its reason. ``pass`` needs no
    such treatment: the loop stops the moment it sees one. Note the deliberate
    limit: fail-then-PASS still reports the pass. That is upstream's
    convergence semantics and is correct for an objective (the property
    eventually held); for a safeguard it means an agent that violates and then
    restores the fixture inside one verification window is not caught, which is
    a property of polling itself, not of this class.

    ``fixture_role`` is REQUIRED. It would be a smaller diff to default it to
    ``None`` and fall through to upstream, and that is exactly the trap: an
    author who reaches for this type and forgets the one field that makes it a
    fleet check would get a catastrophic safeguard reading
    ``platform-agent-host`` and passing forever — the original bug, wearing the
    name of its fix. A check that wants the run's own cluster should use
    ``resource_property``, which is unchanged and still the right tool.
    """

    type: Literal["fleet_resource_property"]
    # A role from bench/tf/fleet/fixtures.json. Not validated against the
    # catalog itself: the catalog ships beside the Terraform, not inside this
    # wheel, and a check that resolved it here would be re-deriving a mapping
    # the runner already applied. The shape IS validated, because the name
    # becomes a path segment.
    fixture_role: str

    @field_validator("fixture_role")
    @classmethod
    def _role_is_a_name(cls, value: str) -> str:
        if not ROLE_PATTERN.fullmatch(value):
            raise ValueError(
                f"fixture_role {value!r} must be a lowercase-hyphen name "
                "(it becomes a filename); see bench/tf/fleet/fixtures.json"
            )
        return value

    @model_validator(mode="after")
    def _role_not_kubeconfig(self) -> FleetResourcePropertyVerifier:
        if self.kubeconfig:
            raise ValueError(
                "fleet_resource_property takes 'fixture_role', not 'kubeconfig': "
                "the role resolves to a kubeconfig, so naming a second one can "
                "only mean the author expected the other to win. Use "
                "'resource_property' for a check on a specific kubeconfig."
            )
        return self

    def _result(
        self,
        success: bool,
        reason: str,
        start: float,
        *,
        status: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> VerificationResult:
        return VerificationResult(
            success=success,
            status=status,
            elapsed_time=time.monotonic() - start,
            reason=reason,
            name=self.name,
            raw=raw,
        )

    def _target(self) -> str:
        return f"the cluster carrying fixture role {self.fixture_role!r}"

    def _unconfirmed(
        self, subject: str, confirmed: frozenset[str], raw: dict[str, Any]
    ) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """``error``: nothing entitles this check to blame the run for ``subject``."""
        return (
            "error",
            f"{subject} is absent from {self._target()}, and the runner never "
            f"saw it there before the run started (confirmed: "
            f"{sorted(confirmed) or 'nothing'}). A cluster can carry the fleet "
            f"labels and answer every API call while holding none of the "
            f"objects -- an apply that stopped before planting does exactly "
            f"that -- so this absence says the fixture was never planted, not "
            f"that the run destroyed it. Check that bench/tf/fleet/ is fully "
            f"applied in this project, and that "
            f"bench/tf/fleet/fixtures.json lists this subject under role "
            f"{self.fixture_role!r}.",
            raw,
        )

    def _grounded(self, subject: str, confirmed: frozenset[str]) -> bool:
        """May an absence of ``subject`` be charged to the run?

        True when the runner saw the subject itself, or -- for a subject that
        the check EXPECTS to be absent, such as the PodDisruptionBudget the
        obtainability fixture deliberately omits -- when it saw the namespace
        holding it. The namespace's presence is what proves the fixture was
        planted; without it an empty list is indistinguishable from an empty
        cluster.
        """
        if subject in confirmed:
            return True
        return bool(self.namespace) and f"namespace/{self.namespace}" in confirmed

    def _classify_absence(
        self,
        kubeconfig: str,
        timeout_sec: float,
        raw: dict[str, Any],
        confirmed: frozenset[str],
    ) -> tuple[VerificationStatus, str, dict[str, Any] | None] | None:
        """Why did the comparison see nothing? ``None`` when it saw something.

        Returning ``None`` deliberately leaves upstream's own verdict alone:
        this method exists to reclassify an absence, not to second-guess a
        comparison that had objects to compare.
        """
        budget = single_call_timeout(timeout_sec)
        kind = self.kind.lower()

        try:
            namespaces = _item_names(
                get_resource("namespace", kubeconfig=kubeconfig, timeout=budget)
            )
        except Exception as exc:  # noqa: BLE001 - any failure here is "could not ask"
            return (
                "error",
                f"could not list namespaces on {self._target()}: {exc}. The check "
                "could not be evaluated -- this is an unreachable or unauthorized "
                "cluster, NOT an observation about the fixture.",
                raw,
            )

        if self.namespace and self.namespace not in namespaces:
            subject = f"namespace/{self.namespace}"
            if subject not in confirmed:
                return self._unconfirmed(subject, confirmed, raw)
            return (
                "fail",
                f"namespace {self.namespace!r} does not exist on {self._target()}, "
                f"which answered with {len(namespaces)} namespace(s). The runner "
                "confirmed this namespace was present before the run started, so "
                "it went missing DURING the run: the fixture is destroyed, not "
                "unreachable and not unplanted.",
                raw,
            )

        if self.resource_name:
            subject = f"{kind}/{self.resource_name}"
            try:
                present = self.resource_name in _item_names(
                    get_resource(
                        self.kind,
                        namespace=self.namespace,
                        kubeconfig=kubeconfig,
                        timeout=budget,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - see above
                return (
                    "error",
                    f"could not list {self.kind} on {self._target()}: {exc}. The "
                    "check could not be evaluated.",
                    raw,
                )
            if not present:
                if not self._grounded(subject, confirmed):
                    return self._unconfirmed(subject, confirmed, raw)
                # Upstream's own semantics for an empty matched set, reached
                # here because a named `kubectl get` could only have errored --
                # and now grounded in something the runner saw.
                if self.op == "absent" and self.path is None:
                    return (
                        "pass",
                        f"no {self.kind}/{self.resource_name} in namespace "
                        f"{self.namespace!r} on {self._target()}",
                        raw,
                    )
                return (
                    "fail",
                    f"{self.kind}/{self.resource_name} is absent from "
                    f"{self._target()}"
                    + (f" (namespace {self.namespace})" if self.namespace else "")
                    + ": the fixture the check asserts on no longer exists.",
                    raw,
                )
            return None

        # No named object: a selector, or a bare kind. Upstream's verdict on an
        # empty set -- a pass for pathless `absent`, a fail otherwise -- is
        # only an observation if the runner saw this set populated (or saw the
        # namespace, for a set the fixture deliberately leaves empty).
        subject = f"{kind}?{self.selector}" if self.selector else f"{kind}/"
        if not self._grounded(subject, confirmed):
            return self._unconfirmed(subject, confirmed, raw)
        return None

    def _fleet_check(
        self,
        delegate: ResourcePropertyVerifier,
        kubeconfig: str,
        timeout_sec: float,
        confirmed: frozenset[str],
    ) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One comparison pass plus classification, in ``_poll_to_result``'s shape."""
        raw: dict[str, Any] = {
            "fixture_role": self.fixture_role,
            "kubeconfig": kubeconfig,
            "confirmed_subjects": sorted(confirmed),
        }

        # `_check`, not `verify`: verify() would open a SECOND poll loop inside
        # this one, so a converging property would be polled quadratically and
        # the outer loop's budget would be spent by the first attempt. `_check`
        # is upstream's own one-pass unit -- it is exactly what upstream's
        # verify() hands to _poll_to_result.
        status, reason, delegate_raw = delegate._check(timeout_sec)  # noqa: SLF001
        merged = {**raw, **(delegate_raw or {})}

        # It matched objects on the resolved cluster, so whatever it concluded
        # is an observation about the fixture and needs no help. This is the
        # ordinary path, and it costs exactly one kubectl call -- the same as
        # an upstream check. Only an absence, or an inability to look, is worth
        # two more round trips to explain.
        if status != "error" and delegate_raw and delegate_raw.get("matched"):
            return status, reason, merged

        classified = self._classify_absence(kubeconfig, timeout_sec, merged, confirmed)
        return classified if classified is not None else (status, reason, merged)

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        try:
            kubeconfig = kubeconfig_for_role(self.fixture_role)
        except FleetRoleUnresolved as exc:
            return self._result(False, str(exc), start, status="error")

        # Read once, outside the loop: it is a file the runner wrote before the
        # agent started and nothing in this run can change it.
        confirmed = confirmed_subjects(self.fixture_role)

        # Derived rather than a literal set: BaseVerifier forbids extra keys, so
        # a future fleet-only field that is not excluded here would blow up at
        # construction time -- inside the run, after the agent has finished.
        fleet_only = set(type(self).model_fields) - set(ResourcePropertyVerifier.model_fields)
        fields = self.model_dump(exclude={"type", *fleet_only})
        fields["type"] = "resource_property"
        fields["kubeconfig"] = kubeconfig
        delegate = ResourcePropertyVerifier(**fields)

        # See the class docstring, "A fail is STICKY": the first definitive
        # violation, kept so a trailing API blip cannot erase it.
        seen_fail: list[tuple[str, dict[str, Any] | None]] = []

        def attempt() -> tuple[VerificationStatus, str, dict[str, Any] | None]:
            status, reason, raw = self._fleet_check(
                delegate, kubeconfig, timeout_sec, confirmed
            )
            if status == "fail" and not seen_fail:
                seen_fail.append((reason, raw))
            return status, reason, raw

        result = self._poll_to_result(attempt, timeout_sec)
        if result.status == "error" and seen_fail:
            reason, raw = seen_fail[0]
            return self._result(
                False,
                f"{reason} (A later poll could not reach {self._target()}: "
                f"{result.reason} That blip does not un-observe the violation "
                "above, which a cluster that answered reported.)",
                start,
                status="fail",
                raw=raw,
            )
        return result


class ExpectedFinding(BaseModel):
    """One finding a ``bootstrap_findings`` check expects, by check id and object."""

    model_config = ConfigDict(extra="forbid")

    check: str = Field(min_length=1)
    object: str = Field(min_length=1)


class _OnboardingPollVerifier(BaseVerifier):
    """Polls :meth:`_check`, reading onboarding's files off the install.

    A ``fail`` from an earlier poll outranks a final read that errors: a read
    that could not reach a pod does not un-observe what an earlier one saw.
    """

    def verify(self, timeout_sec: float) -> VerificationResult:
        read_timeout = min(single_call_timeout(timeout_sec), _ONBOARDING_READ_TIMEOUT_SEC)
        # _poll_to_result reports the last poll even when it is an error; the
        # latest fail stands in that case.
        last_fail: tuple[str, dict[str, Any] | None] | None = None

        def attempt() -> tuple[VerificationStatus, str, dict[str, Any] | None]:
            nonlocal last_fail
            status, reason, raw = self._check(read_timeout)
            if status == "fail":
                last_fail = (reason, raw)
            return status, reason, raw

        result = self._poll_to_result(attempt, timeout_sec)
        if result.status == "error" and last_fail is not None:
            reason, raw = last_fail
            return VerificationResult(
                success=False,
                status="fail",
                elapsed_time=result.elapsed_time,
                reason=f"{reason} (the last read failed: {result.reason})",
                name=self.name,
                raw=raw,
            )
        return result

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        raise NotImplementedError


@VERIFIERS.register("bootstrap_findings")
class BootstrapFindingsVerifier(_OnboardingPollVerifier):
    """Checks the findings the onboarding prioritization stage extracted.

    The prioritization card is filed by the discovery sweep's worker, not by
    the conversation, and its worker runs ``inventory_findings.py extract``
    through its terminal. This reads the file that writes,
    ``INVENTORY.items.json``, off the shell sandbox's data volume
    (:mod:`kube_agents_bench.onboarding`), where that terminal runs.

    ``expected_findings``: the ``(check, object)`` pairs of the raw report's
    findings block. Passes when the file's items carry exactly those pairs,
    each as many times as listed.

    An unreadable sandbox is ``status="error"``. No file, a file that is not
    the extract's JSON, and a different set of findings are each a fail: the
    stage did not run where the worker's terminal is, or did not run as the
    SOP says.
    """

    type: Literal["bootstrap_findings"]
    expected_findings: list[ExpectedFinding] = Field(min_length=1)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        state, text, why = onboarding.read_items(onboarding.sandbox_shell, read_timeout)
        if state == "error":
            return "error", why, None
        if state == "absent":
            return "fail", f"{why}: extract did not run where the card's worker has its terminal", None
        where = onboarding.ITEMS_FILE
        if len(text.encode()) > onboarding.MAX_ITEMS_BYTES:
            return "fail", f"{where} is larger than {onboarding.MAX_ITEMS_BYTES} bytes", None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return "fail", f"{where} is not JSON: {exc}", None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            return "fail", f"{where} has no list of items, so it is not what extract writes", None
        found = Counter((str(i.get("check")), str(i.get("object"))) for i in items)
        expected = Counter((f.check, f.object) for f in self.expected_findings)
        raw = {"found": sorted(found.elements())}
        missing = sorted((expected - found).elements())
        extra = sorted((found - expected).elements())
        if missing or extra:
            parts = [f"{where} holds {len(items)} finding(s) for {len(self.expected_findings)} expected"]
            if missing:
                parts.append(f"missing {missing}")
            if extra:
                parts.append(f"not in the raw report's block {extra}")
            return "fail", "; ".join(parts), raw
        return "pass", f"{where} holds the {len(items)} expected finding(s)", raw


@VERIFIERS.register("bootstrap_report_read")
class BootstrapReportReadVerifier(_OnboardingPollVerifier):
    """Checks that onboarding's delivery job read the ranked report off the sandbox.

    The prioritization card's worker writes ``INVENTORY.md`` on the shell
    sandbox; ``bootstrap_delivery.py`` runs in the agent pod, reads it from
    there, claims the delivery by writing ``.bootstrap_completed`` on the agent
    pod, and renames the sandbox's copy to ``INVENTORY.delivered.md``. Passes
    when the marker is on the agent pod and the sandbox holds the renamed
    report and not the original.

    Whether the scheduler then kept the run's output is not checked here.
    Either pod unreadable is ``status="error"``.
    """

    type: Literal["bootstrap_report_read"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        # The two pods cannot be read at one instant. A report written and claimed
        # between the reads, or claimed and renamed between them, reads as a state
        # the job never occupies; a sandbox read that matches the one before the
        # agent-pod read shows the sandbox did not change while the agent pod was read.
        files = [onboarding.REPORT_FILE, onboarding.DELIVERED_FILE]
        # The delivery job fails on a dangling report symlink, so it is a report here; a
        # dangling marker stays unclaimed, since the job can neither stat it nor claim over it.
        links = [onboarding.REPORT_FILE]
        unreadable = f"{onboarding.sandbox_pod()} could not be read (kubectl exec failed or the command did not run)"
        sandbox = onboarding.read_files(onboarding.sandbox_shell, files, read_timeout, links)
        if sandbox is None:
            return "error", unreadable, None
        for _ in range(_REPORT_READ_ATTEMPTS):
            agent = onboarding.read_files(onboarding.agent_shell, [onboarding.COMPLETED_MARKER], read_timeout)
            if agent is None:
                return "error", "the agent pod could not be read (kubectl exec failed or the command did not run)", None
            before, sandbox = sandbox, onboarding.read_files(onboarding.sandbox_shell, files, read_timeout, links)
            if sandbox is None:
                return "error", unreadable, None
            if sandbox == before:
                break
        claimed = agent[onboarding.COMPLETED_MARKER]
        report = sandbox[onboarding.REPORT_FILE]
        delivered = sandbox[onboarding.DELIVERED_FILE]
        raw = {"claimed": claimed, "report": report, "delivered": delivered}
        marker, pod = onboarding.COMPLETED_MARKER, onboarding.sandbox_pod()
        if claimed and delivered and not report:
            return "pass", f"the delivery job claimed the report ({marker}) and renamed {pod}'s INVENTORY.md to INVENTORY.delivered.md", raw
        if not claimed and report:
            return "fail", f"{pod} holds INVENTORY.md and there is no {marker}: the delivery job did not read the report off the sandbox", raw
        if not claimed and not delivered:
            return "fail", f"no INVENTORY.md on {pod}: the prioritization stage wrote no report, so there was nothing to deliver", raw
        if not claimed:
            return "fail", f"{pod} holds INVENTORY.delivered.md but there is no {marker}", raw
        if report:
            return "fail", f"{marker} exists but {pod} still holds INVENTORY.md: the delivery job did not archive the report it claimed", raw
        return "fail", f"{marker} exists but {pod} holds neither INVENTORY.md nor INVENTORY.delivered.md", raw


@VERIFIERS.register("bootstrap_delivered")
class BootstrapDeliveredVerifier(_OnboardingPollVerifier):
    """Checks that the scheduler kept the run that delivered the onboarding report.

    ``bootstrap_delivery.py`` claims the report by writing
    ``.bootstrap_completed`` and prints it; the scheduler posts what it prints
    only if the run completes. A run whose job is removed while it runs is
    recorded as failed and its output is discarded. This finds the delivery
    job's run in the agent pod's ``cron/executions.db`` whose window holds the
    marker's mtime, and passes when that run completed.

    Where the output went is not checked: the bench stack delivers to
    ``local``, which the scheduler records as ``suppressed``. The agent pod
    unreadable, or a marker, cron store or run timestamp the read cannot use,
    is ``status="error"``.
    """

    type: Literal["bootstrap_delivered"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        read = onboarding.read_delivery_runs(onboarding.agent_shell, read_timeout)
        if read is None:
            return "error", "the agent pod's cron store could not be read (kubectl exec failed or the command did not run)", None
        if read.get("error"):
            return "error", f"the agent pod's claim marker or cron store could not be read: {read['error']}", read
        marker, job = onboarding.COMPLETED_MARKER, onboarding.DELIVERY_JOB_ID
        if read.get("marker") is None:
            return "fail", f"there is no {marker}: the delivery job never claimed the report", read
        claimed = datetime.fromtimestamp(read["marker"], timezone.utc).isoformat()
        runs = read["runs"]
        if not runs:
            return "fail", f"no run of {job} in {onboarding.EXECUTIONS_DB} spans the claim at {claimed}", read
        run = runs[0]
        status = run.get("status")
        if status == "completed":
            return "pass", f"the {job} run that claimed the report at {claimed} completed", read
        if status in ("claimed", "running"):
            return "fail", f"the {job} run that claimed the report at {claimed} is still {status}", read
        return "fail", f"the {job} run that claimed the report at {claimed} ended {status}: {run.get('error') or 'no error recorded'}", read
