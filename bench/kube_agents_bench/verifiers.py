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

Three of them answer the half of a task's exact checks that cluster state
cannot: did the *report* name the thing we planted, did the agent *call* the
tools it claims to have used, and — for the fleet audits, whose SOPs
deliberately keep the chat reply to one line — does the *ledger issue the run
published* carry the finding. All three read the per-run stash in
:mod:`kube_agents_bench.transcript`, and all three fail closed: an empty
stash is ``status="error"`` — the check could not be evaluated — never a pass
or a fail, so ``VerificationCoverage`` drops below 1.0 and the gate catches
it.

The fourth, ``fleet_resource_property``, does read cluster state, and exists
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
import urllib.request
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from devops_bench.k8s import get_resource
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)
from devops_bench.verification.verifiers import ResourcePropertyVerifier

from kube_agents_bench import transcript
from kube_agents_bench.fleet import (
    ROLE_PATTERN,
    FleetRoleUnresolved,
    confirmed_subjects,
    kubeconfig_for_role,
)

__all__ = [
    "FleetResourcePropertyVerifier",
    "LedgerIssueContainsVerifier",
    "ReportContainsVerifier",
    "ToolCalledVerifier",
]

_NO_TRANSCRIPT_REASON = (
    "no transcript stashed for this run: the harness did not complete an "
    "agent execution (kube_agents_bench.transcript is empty), so this check "
    "could not be evaluated"
)

# Emphasis and code markers, dropped before matching. The agent answers in
# Markdown, and a phrase spanning an emphasised word cannot match the raw
# text: "not crashlooping" against a report that reads "the pods are **not**
# CrashLooping" fails on the asterisks alone. That failed a real presubmit
# (gke-labs/kube-agents#982) on a report the OutcomeValidity judge scored
# 1.00, and enumerating markdown shapes in every task.yaml would only encode
# one run's formatting.
_MARKDOWN_NOISE = str.maketrans("", "", "*_`")


def _normalize(text: str) -> str:
    """Lowercase, strip Markdown emphasis, collapse runs of whitespace.

    Applied to BOTH sides, so a phrase may be written with or without the
    markers and match either way.

    Boundary whitespace survives. ``" 0 restarts"`` carries a leading space
    on purpose -- it is what stops the phrase matching "10 restarts" -- and
    ``" ".join(str.split())`` would drop it and hand back the false positive
    the space was added to prevent.
    """
    stripped = text.translate(_MARKDOWN_NOISE)
    collapsed = " ".join(stripped.split())
    if stripped[:1].isspace():
        collapsed = " " + collapsed
    if stripped[-1:].isspace():
        collapsed += " "
    return collapsed.lower()


@VERIFIERS.register("report_contains")
class ReportContainsVerifier(BaseVerifier):
    """Exact phrase checks against the agent's answer.

    Substring matching, deliberately: the task author chose the phrase (a
    planted defect's name, a required noun), so an exact match is fair.
    Anything fuzzier belongs to the judge, not to a blocking check.

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
    scope: Literal["final", "full"] = "final"

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
        text = _normalize(
            snap.final_message if self.scope == "final" else snap.output
        )
        missing = [p for p in self.required_phrases if _normalize(p) not in text]
        present = [p for p in self.forbidden_phrases if _normalize(p) in text]
        any_of_miss = bool(self.any_of_phrases) and not any(
            _normalize(p) in text for p in self.any_of_phrases
        )
        if missing or present or any_of_miss:
            parts = []
            if missing:
                parts.append(f"required phrases absent from the report: {missing}")
            if present:
                parts.append(f"forbidden phrases present in the report: {present}")
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
        if self.any_of_phrases:
            satisfied.append(
                f"at least one of {len(self.any_of_phrases)} alternative phrasing(s)"
            )
        return VerificationResult(
            success=True,
            elapsed_time=time.monotonic() - start,
            reason="report contains " + ", ".join(satisfied),
        )


@VERIFIERS.register("tool_called")
class ToolCalledVerifier(BaseVerifier):
    """Count trajectory entries whose tool name is in ``tool_names``.

    THE TRAJECTORY IS THE ROUTER'S, NOT THE FLEET'S. By this harness's
    design, ``result.trajectory`` holds only the delegating turn's calls:
    poll-turn calls are the harness's own bookkeeping and are kept out
    (``_fold_status_turn``), and a delegated worker's calls never reach it
    at all. This verifier can therefore assert what the ROUTER did
    (``kanban_create`` is the router's own call) and nothing about what a
    worker did on a cluster — a mutation safeguard built on it would be
    blind to the very calls it fears. Use a cluster-state check
    (``resource_property``) for those.

    Passes when at least ``minimum_calls`` matching calls were made. Wrapped
    in a ``none`` compound, it is the safeguard shape "this tool was never
    called", within the router-only limits above. Names match the harness's
    canonical trajectory entries (``ToolCall.to_dict()["name"]``), e.g.
    ``kanban_create``.
    """

    type: Literal["tool_called"]
    tool_names: list[str] = Field(min_length=1)
    minimum_calls: int = Field(default=1, ge=1)
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
        wanted = set(self.tool_names)
        calls = [
            entry
            for entry in snap.trajectory
            if isinstance(entry, dict)
            and entry.get("name") in wanted
            and not (self.require_success and entry.get("status") == "error")
        ]
        count = len(calls)
        ok = count >= self.minimum_calls
        return VerificationResult(
            success=ok,
            elapsed_time=time.monotonic() - start,
            reason=(
                f"{count} call(s) to {sorted(wanted)} in the trajectory"
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

# github.com only, and issues only: `/pull/<n>` is a remediation pull request,
# which every audit report also links and which is not the ledger.
_ISSUE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)/issues/(\d+)",
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

# Bound on issue URLs fetched from one report. An audit reply names its ledger
# once; anything past a handful is a report to look at by hand, not a set of
# candidates to shotgun the API with.
_MAX_LEDGER_CANDIDATES = 8

_NO_RUN_CLOCK_REASON = (
    "the run's transcript carries no start time (TranscriptSnapshot.started_at "
    "is unset), so this check cannot tell this run's ledger from a previous "
    "run's and refuses to grade it"
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
    ``fixed + findings + withheld + evidence + footer``, so every byte the
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
    ``matches[-1]``.
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


def _finding_ids(body: str) -> list[str] | None:
    """This run's finding ids from the hidden delta block, or None when absent."""
    matches = _DELTA_RE.findall(body)
    if not matches:
        return None
    try:
        ids = json.loads(matches[-1])
    except (ValueError, TypeError):
        return None
    if not isinstance(ids, list):
        return None
    return [i for i in ids if isinstance(i, str)]


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
    delegated worker whose tool calls never reach ``snap.trajectory``. What
    does cross back is ``finish``'s ``issue_url``, which the SOP requires
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
    delta block, which ``audit_report.py`` derives as
    ``<check>.<cluster>.<namespace>.<object>``. Use it whenever the phrase is a
    CLUSTER name: the body's scope table names every audited cluster on every
    run, so ``required_phrases: ["seeded-c"]`` against ``body`` would pass on a
    ledger that enumerated the fleet and found nothing. Against the ids it
    passes only when a finding was actually FILED against that cluster.
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
                    "body": body,
                    "generated_at": footer[1],
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
            return done(
                False,
                f"{ledger['slug']} was generated at {generated_at.isoformat()}, "
                f"{age:.0f}s BEFORE this run started ({started.isoformat()}): it "
                "is a previous run's ledger, so this run published nothing",
                raw={"generated_at": generated_at.isoformat()},
            )

        if self.scope == "finding_ids":
            ids = _finding_ids(ledger["body"])
            if ids is None:
                return done(
                    False,
                    f"{ledger['slug']} carries no readable "
                    "<!-- audit-findings: [...] --> delta block, so the findings "
                    "this run filed cannot be read off it",
                )
            text = "\n".join(ids).lower()
            surface = f"the {len(ids)} finding id(s) on {ledger['slug']}"
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
    gate confirmed only the NAMESPACE, which four of the seven roles do not
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
