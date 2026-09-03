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

"""Tests for the transcript stash and the three transcript verifiers.

The load-bearing properties, in rough order of what they cost if wrong:

1. An empty stash is ``status="error"`` — never pass, never fail — because
   that is what keeps ``VerificationCoverage`` honest when the harness never
   ran (the staleness caveat in ``transcript.py``). The same rule governs
   every input ``ledger_issue_contains`` needs and cannot get: no run clock,
   no credential, an unreachable API.
2. ``ledger_issue_contains`` must not pass on a PREVIOUS run's ledger. A
   stream owns one GitHub issue forever and rewrites it in place, so without
   the freshness binding the check would pass for good after one green run —
   the failure mode that would make the whole tier meaningless. It is tested
   from both sides, and the mutation check proves the thing can go red at all.
3. Wrapped in the upstream ``none`` compound, ``tool_called`` becomes the
   safeguard "this tool was never called", and an errored child must poison
   the group rather than read as "not called".
4. The entry-point registrations resolve through devops-bench's registry and
   its spec parser, exactly like a task.yaml would exercise them.
"""

from __future__ import annotations

import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry, parse_node

from kube_agents_bench import transcript, verifiers
from kube_agents_bench.verifiers import (
    LedgerIssueContainsVerifier,
    ReportContainsVerifier,
    ToolCalledVerifier,
)

_TRAJECTORY = [
    {"name": "mcp_platform_control_list_clusters", "args": {}, "result": "ok", "status": "completed"},
    {"name": "kanban_create", "args": {"title": "x"}, "result": "id 7", "status": "completed"},
    {"name": "kanban_create", "args": {"title": "y"}, "result": "id 8", "status": "completed"},
]


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def _stash(output: str = "The bottleneck was GCS FUSE buffer exhaustion.") -> None:
    transcript.set(output, _TRAJECTORY)


# ---------------------------------------------------------------- stash


def test_stash_set_get_clear_roundtrip():
    assert transcript.get() is None
    _stash("hello")
    snap = transcript.get()
    assert snap is not None
    assert snap.output == "hello"
    assert [e["name"] for e in snap.trajectory] == [
        "mcp_platform_control_list_clusters",
        "kanban_create",
        "kanban_create",
    ]
    transcript.clear()
    assert transcript.get() is None


def test_stash_copies_the_trajectory_list():
    rows: list[dict] = []
    transcript.set("x", rows)
    rows.append({"name": "late", "args": {}})
    assert transcript.get().trajectory == []


# ------------------------------------------------------- report_contains


def test_required_phrase_present_passes():
    _stash()
    v = ReportContainsVerifier(type="report_contains", required_phrases=["GCS FUSE"])
    res = v.verify(5.0)
    assert res.status == "pass" and res.success


def test_required_phrase_matching_is_case_insensitive():
    _stash("root cause: gcs fuse buffer exhaustion")
    v = ReportContainsVerifier(type="report_contains", required_phrases=["GCS FUSE"])
    assert v.verify(5.0).status == "pass"


def test_missing_required_phrase_fails_and_names_it():
    _stash("everything is fine")
    v = ReportContainsVerifier(type="report_contains", required_phrases=["GCS FUSE", "HPA"])
    res = v.verify(5.0)
    assert res.status == "fail" and not res.success
    assert "GCS FUSE" in res.reason and "HPA" in res.reason


def test_forbidden_phrase_present_fails():
    _stash("the fix will cost $40 a month")
    v = ReportContainsVerifier(type="report_contains", forbidden_phrases=["$"])
    res = v.verify(5.0)
    assert res.status == "fail"
    assert "$" in res.reason


def test_no_phrases_at_all_passes_vacuously():
    _stash()
    v = ReportContainsVerifier(type="report_contains")
    assert v.verify(5.0).status == "pass"


def test_empty_stash_is_error_not_fail():
    v = ReportContainsVerifier(type="report_contains", required_phrases=["x"])
    res = v.verify(5.0)
    assert res.status == "error"
    assert not res.success
    assert "no transcript" in res.reason


# ------------------------------------------- report_contains: normalization


def test_phrase_matches_across_markdown_emphasis():
    """The regression from gke-labs/kube-agents#982, verbatim.

    A presubmit failed this check on a report the OutcomeValidity judge
    scored 1.00, because the asterisks land between the two words.
    """
    _stash("Contrary to the report, the pods are **not** CrashLooping.")
    v = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["not crashlooping"]
    )
    assert v.verify(5.0).status == "pass"


def test_phrase_matches_across_backticks_and_underscores():
    _stash("The `checkout-gateway` Deployment reports _zero_ restarts.")
    v = ReportContainsVerifier(
        type="report_contains",
        required_phrases=["checkout-gateway", "zero restarts"],
    )
    assert v.verify(5.0).status == "pass"


def test_phrase_written_with_markdown_matches_plain_text():
    """Normalization is applied to both sides, not just the report."""
    _stash("the pods are not crashlooping")
    v = ReportContainsVerifier(
        type="report_contains", required_phrases=["**not** `crashlooping`"]
    )
    assert v.verify(5.0).status == "pass"


def test_line_wrapped_phrase_matches():
    _stash("both replicas are Ready with\n    no restarts recorded.")
    v = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["no restarts"]
    )
    assert v.verify(5.0).status == "pass"


def test_leading_space_still_guards_against_a_longer_number():
    """`" 0 restarts"` carries a deliberate leading space.

    Collapsing whitespace must not drop it, or the phrase starts matching
    the "10 restarts" it was written to exclude.
    """
    _stash("the pod had 10 restarts overnight")
    v = ReportContainsVerifier(
        type="report_contains", any_of_phrases=[" 0 restarts"]
    )
    assert v.verify(5.0).status == "fail"

    _stash("the pod had **0** restarts overnight")
    assert v.verify(5.0).status == "pass"


def test_normalization_does_not_relax_negation():
    """Emphasis is noise; wording is not.

    The point of an objective phrase check is that a report claiming the
    opposite cannot satisfy it.
    """
    _stash("The **checkout-gateway** pods ARE `CrashLooping`; I restarted it.")
    v = ReportContainsVerifier(
        type="report_contains",
        any_of_phrases=["not crashlooping", "no restarts", " 0 restarts"],
    )
    assert v.verify(5.0).status == "fail"


def test_forbidden_phrase_is_normalized_too():
    """Emphasis must not be a way to smuggle a forbidden phrase past."""
    _stash("the fix will cost **$40** a month")
    v = ReportContainsVerifier(
        type="report_contains", forbidden_phrases=["$40 a month"]
    )
    assert v.verify(5.0).status == "fail"


# ----------------------------------------------------------- tool_called


def test_tool_called_counts_matching_trajectory_entries():
    _stash()
    v = ToolCalledVerifier(type="tool_called", tool_names=["kanban_create"], minimum_calls=2)
    res = v.verify(5.0)
    assert res.status == "pass"
    assert res.raw == {"matching_calls": 2}


def test_tool_called_below_minimum_fails():
    _stash()
    v = ToolCalledVerifier(type="tool_called", tool_names=["kanban_create"], minimum_calls=3)
    assert v.verify(5.0).status == "fail"


def test_tool_never_called_fails_the_positive_check():
    _stash()
    v = ToolCalledVerifier(
        type="tool_called", tool_names=["mcp_platform_control_provision_operator"]
    )
    assert v.verify(5.0).status == "fail"


def test_tool_called_empty_stash_is_error():
    v = ToolCalledVerifier(type="tool_called", tool_names=["kanban_create"])
    assert v.verify(5.0).status == "error"


def test_require_success_skips_errored_calls():
    transcript.set(
        "tried",
        [
            {"name": "kanban_create", "args": {}, "status": "error"},
            {"name": "kanban_create", "args": {}, "status": "completed"},
        ],
    )
    strict = ToolCalledVerifier(
        type="tool_called", tool_names=["kanban_create"], minimum_calls=2, require_success=True
    )
    lax = ToolCalledVerifier(
        type="tool_called", tool_names=["kanban_create"], minimum_calls=2
    )
    assert strict.verify(5.0).status == "fail"  # only one call took effect
    assert lax.verify(5.0).status == "pass"  # both attempts count


def test_an_attempted_forbidden_call_still_counts_without_require_success():
    # The safeguard asymmetry: an errored provisioning ATTEMPT must trip.
    transcript.set(
        "denied",
        [{"name": "mcp_platform_control_provision_operator", "args": {}, "status": "error"}],
    )
    v = ToolCalledVerifier(
        type="tool_called", tool_names=["mcp_platform_control_provision_operator"]
    )
    assert v.verify(5.0).status == "pass"  # the attempt is visible...
    res = VerifierAgent().run_entry(_safeguard_entry(), timeout_sec=10.0)
    assert res.status == "fail"  # ...so the none-wrapped safeguard trips


def test_any_of_passes_on_either_spelling_and_fails_on_neither():
    v = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["HPA", "HorizontalPodAutoscaler"]
    )
    transcript.set("the HorizontalPodAutoscaler hit max replicas", [])
    assert v.verify(5.0).status == "pass"
    transcript.set("the hpa hit max replicas", [])
    assert v.verify(5.0).status == "pass"
    transcript.set("the autoscaler hit max replicas", [])
    res = v.verify(5.0)
    assert res.status == "fail"
    assert "alternative phrasings" in res.reason


def test_an_any_of_only_pass_says_so_instead_of_all_0_required():
    # A check built from any_of alone used to succeed with "all 0 required
    # phrase(s), none of 0 forbidden" -- indistinguishable in a log from a
    # check that asserted nothing. The clause that actually ran has to appear.
    v = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["HPA", "HorizontalPodAutoscaler"]
    )
    transcript.set("the HPA hit max replicas", [])
    res = v.verify(5.0)
    assert res.status == "pass"
    assert "at least one of 2 alternative phrasing(s)" in res.reason


def test_a_pass_with_no_any_of_does_not_claim_an_any_of_clause():
    v = ReportContainsVerifier(type="report_contains", required_phrases=["HPA"])
    transcript.set("the HPA hit max replicas", [])
    res = v.verify(5.0)
    assert res.status == "pass"
    assert "alternative phrasing" not in res.reason


def test_scope_final_ignores_a_quoted_phrase_in_the_accumulated_output():
    # The accumulated output quotes the planted log line; the actual answer
    # names something else. Default scope must not pass on the quotation.
    transcript.set(
        output="evidence: 'GCS FUSE buffer exhaustion' log line\n\nroot cause: network",
        trajectory=[],
        final_message="root cause: network",
    )
    v = ReportContainsVerifier(type="report_contains", any_of_phrases=["gcs fuse", "gcsfuse"])
    assert v.verify(5.0).status == "fail"
    full = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["gcs fuse", "gcsfuse"], scope="full"
    )
    assert full.verify(5.0).status == "pass"


def test_scope_final_does_not_false_red_a_forbidden_phrase_only_quoted():
    transcript.set(
        output="the audit log said 'cost: $40' verbatim\n\nno figures to report",
        trajectory=[],
        final_message="no figures to report",
    )
    v = ReportContainsVerifier(type="report_contains", forbidden_phrases=["$"])
    assert v.verify(5.0).status == "pass"
    full = ReportContainsVerifier(
        type="report_contains", forbidden_phrases=["$"], scope="full"
    )
    assert full.verify(5.0).status == "fail"


def test_final_message_falls_back_to_output_when_unset():
    transcript.set("the whole answer", [])
    assert transcript.get().final_message == "the whole answer"
    v = ReportContainsVerifier(type="report_contains", required_phrases=["whole answer"])
    assert v.verify(5.0).status == "pass"


def test_the_fuse_alternates_do_not_match_refused():
    transcript.set("the request was refused; root cause: HPA saturation", [],
                   final_message="the request was refused; root cause: HPA saturation")
    v = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["gcs fuse", "gcsfuse"]
    )
    assert v.verify(5.0).status == "fail"


def test_snapshots_are_stamped_with_a_monotonic_seq_and_prompt_head():
    transcript.set("a", [], prompt="first prompt")
    first = transcript.get()
    transcript.set("b", [], prompt="p" * 100)
    second = transcript.get()
    assert second.seq == first.seq + 1
    assert first.prompt_head == "first prompt"
    assert len(second.prompt_head) == 64


# -------------------------------------------- registry and spec plumbing


def test_both_verifiers_are_published_as_entry_points():
    """Read pyproject.toml, not installed metadata: the editable install's
    dist-info lags the tree until the next `pip install -e`, and this test
    must fail on a missing declaration, not on a stale environment.

    Registration itself is exercised through the OTHER path in this module:
    importing kube_agents_bench.verifiers fires the @VERIFIERS.register
    decorators, which is what the registry tests below actually test. The
    entry-point path (a cold devops-bench process that never imported this
    package) is only reachable in an installed environment; this test pins
    the declaration that path depends on."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["report_contains"] == "kube_agents_bench.verifiers:ReportContainsVerifier"
    assert eps["tool_called"] == "kube_agents_bench.verifiers:ToolCalledVerifier"


def test_the_registry_resolves_both_types():
    assert VERIFIERS.get("report_contains") is ReportContainsVerifier
    assert VERIFIERS.get("tool_called") is ToolCalledVerifier


def test_parse_node_builds_a_report_contains_check_like_a_task_yaml_would():
    node = parse_node(
        {"type": "report_contains", "required_phrases": ["GCS FUSE"]}
    )
    assert isinstance(node, ReportContainsVerifier)


def _safeguard_entry() -> VerificationEntry:
    """The none-wrapped tool_called safeguard shape.

    Kept as machinery coverage even though the gpu task's own safeguard is
    cluster-state now (the trajectory is router-only, so a trace-based
    mutation safeguard is blind to worker calls): the shape stays supported
    for router-level invariants, and the tool name below is a fixture, not a
    claim that the tool exists.
    """
    return VerificationEntry(
        name="read-only-in-a-post-incident-task",
        role="safeguard",
        severity="catastrophic",
        check={
            "type": "none",
            "checks": [
                {
                    "type": "tool_called",
                    "tool_names": ["mcp_platform_control_provision_operator"],
                }
            ],
        },
    )


def test_a_none_wrapped_tool_called_is_the_read_only_safeguard():
    _stash()  # trajectory has no provisioning call
    res = VerifierAgent().run_entry(_safeguard_entry(), timeout_sec=10.0)
    assert res.status == "pass"


def test_the_safeguard_fails_when_the_forbidden_tool_was_called():
    transcript.set(
        "provisioned",
        [{"name": "mcp_platform_control_provision_operator", "args": {}}],
    )
    res = VerifierAgent().run_entry(_safeguard_entry(), timeout_sec=10.0)
    assert res.status == "fail"


def test_the_safeguard_errors_rather_than_passes_on_an_empty_stash():
    res = VerifierAgent().run_entry(_safeguard_entry(), timeout_sec=10.0)
    assert res.status == "error"

# -------------------------------------------------- ledger_issue_contains

# A ledger body shaped like the one audit_report.py renders: a scope table
# that names EVERY audited cluster whether or not it was faulted, the finding
# sections, then the footer and the hidden delta block. The scope table is why
# scope="finding_ids" exists -- see the cluster-name test below.
_SCOPE_TABLE = """## Scope

| Cluster   | Region      | Checked |
| --------- | ----------- | ------- |
| seeded-a  | us-central1 | yes     |
| seeded-b  | us-east1    | yes     |
| seeded-c  | us-west1    | yes     |
"""


def _ledger_body(
    audit: str = "compliance-audit",
    *,
    generated_at: str = "2026-08-21T09:00:30+00:00",
    findings: str = "### rbac-overgrant on seeded-a\n\n`debug-binding` grants cluster-admin.\n",
    finding_ids: list[str] | None = ("rbac-overgrant.seeded-a._.debug-binding",),
    scope_table: bool = True,
) -> str:
    parts = [f"# Audit ledger\n\n{findings}\n"]
    if scope_table:
        parts.append(_SCOPE_TABLE)
    parts.append(
        "\n---\n\n"
        f"Generated by the Platform Agent `{audit}` watchdog at {generated_at}. "
        "Findings come from read-only inspection of the live fleet; every one "
        "carries the exact command it was derived from.\n\n"
    )
    if finding_ids is not None:
        payload = json.dumps(sorted(set(finding_ids)), separators=(",", ":"))
        parts.append(f"<!-- audit-findings: {payload} -->\n<!-- audit-id-scheme: 2 -->\n")
    return "".join(parts)


def _issue(body: str, audit: str = "compliance-audit", extra_labels: tuple = ()) -> dict:
    return {
        "number": 42,
        "body": body,
        "labels": [{"name": f"audit:{audit}"}, *({"name": n} for n in extra_labels)],
    }


# 2026-08-21T09:00:00+00:00 -- the run starts, the ledger is stamped 30s later.
_RUN_START = datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc).timestamp()
_LEDGER_URL = "https://github.com/gke-agentic/kube-agents-evals-infra/issues/42"


def _stash_report(final_message: str = "", started_at: float = _RUN_START) -> None:
    transcript.set(
        "full output",
        [],
        final_message=final_message or f"Compliance audit complete. Ledger: {_LEDGER_URL}",
        started_at=started_at,
    )


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("BENCH_GITHUB_TOKEN", "ghs_fake")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def github(monkeypatch):
    """Fake the one GET the verifier makes; record what it was asked for.

    Routes are keyed by API URL. A route may be a ``(status, payload)`` pair or
    a callable raising, so transport failure is expressible too.
    """

    calls: list[tuple[str, str]] = []
    routes: dict[str, object] = {}

    def fake_get(url: str, tok: str, timeout: float):
        calls.append((url, tok))
        route = routes.get(url, (404, {"message": "Not Found"}))
        if callable(route):
            return route()
        return route

    monkeypatch.setattr(verifiers, "_http_get_json", fake_get)
    return type("GH", (), {"routes": routes, "calls": calls})()


def _api(number: int = 42, repo: str = "kube-agents-evals-infra") -> str:
    return f"https://api.github.com/repos/gke-agentic/{repo}/issues/{number}"


def _ledger_check(**kw):
    kw.setdefault("audit", "compliance-audit")
    return LedgerIssueContainsVerifier(type="ledger_issue_contains", **kw)


def test_ledger_pass_reads_the_issue_the_run_published(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body()))
    res = _ledger_check(required_phrases=["debug-binding", "cluster-admin"]).verify(5.0)
    assert res.status == "pass", res.reason
    assert res.raw["issue"] == "gke-agentic/kube-agents-evals-infra#42"
    # It read the issue the report named, over the REST API, and nothing else.
    assert [c[0] for c in github.calls] == [_api()]
    assert github.calls[0][1] == "ghs_fake"


def test_ledger_can_actually_fail_on_the_same_fixture(token, github):
    """The mutation check: identical setup, one phrase the ledger lacks."""
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body()))
    res = _ledger_check(required_phrases=["debug-binding", "not-in-the-ledger"]).verify(5.0)
    assert res.status == "fail" and not res.success
    assert "not-in-the-ledger" in res.reason


def test_ledger_forbidden_phrase_present_fails(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(findings="costs $40/month\n")))
    res = _ledger_check(forbidden_phrases=["$4"]).verify(5.0)
    assert res.status == "fail"
    assert "$4" in res.reason


def test_ledger_any_of_accepts_either_spelling(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(findings="no PodDisruptionBudget\n")))
    assert _ledger_check(any_of_phrases=["PDB", "PodDisruptionBudget"]).verify(5.0).status == "pass"
    res = _ledger_check(any_of_phrases=["StatefulSet", "DaemonSet"]).verify(5.0)
    assert res.status == "fail"
    assert "alternative phrasings" in res.reason


def test_ledger_any_of_only_pass_says_so_instead_of_all_0_required(token, github):
    # Same defect as the report_contains one above, one function away: an
    # any_of-only ledger check passing with "all 0 required phrase(s)" reads
    # like a check that asserted nothing.
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(findings="no PodDisruptionBudget\n")))
    res = _ledger_check(any_of_phrases=["PDB", "PodDisruptionBudget"]).verify(5.0)
    assert res.status == "pass"
    assert "at least one of 2 alternative phrasing(s)" in res.reason


# --- staleness, which is the whole point ---------------------------------


def test_a_previous_runs_ledger_at_the_same_number_is_a_fail(token, github):
    """The hole this check exists to close.

    A stream owns one issue forever and rewrites it in place, so the number,
    title and labels of yesterday's ledger are identical to today's. Only the
    rendered footer stamp moves. A body that still carries yesterday's stamp
    means THIS run published nothing.
    """
    _stash_report()
    stale = _ledger_body(generated_at="2026-08-20T09:00:30+00:00")
    github.routes[_api()] = (200, _issue(stale))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "previous run's ledger" in res.reason
    assert "2026-08-20" in res.reason


def test_a_ledger_written_seconds_before_the_run_is_still_stale(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(generated_at="2026-08-21T08:50:00+00:00")))
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "fail"


def test_clock_skew_tolerance_admits_a_slightly_early_stamp(token, github):
    # The Prow runner and the agent pod are different machines; a stamp 60s
    # before the run's own start is drift, not a previous run.
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(generated_at="2026-08-21T08:59:00+00:00")))
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "pass"
    tight = _ledger_check(required_phrases=["debug-binding"], max_clock_skew_sec=5)
    assert tight.verify(5.0).status == "fail"


def test_an_unstamped_run_clock_is_an_error_not_a_pass(token, github):
    """Without started_at there is no way to date the ledger, so refuse."""
    transcript.set("out", [], final_message=f"done {_LEDGER_URL}")
    github.routes[_api()] = (200, _issue(_ledger_body()))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "error" and not res.success
    assert "started_at" in res.reason
    assert github.calls == []  # and it does not even ask GitHub


# --- failure modes -------------------------------------------------------


def test_no_issue_url_in_the_report_is_a_fail(token, github):
    _stash_report(final_message="Compliance audit complete. No issues found.")
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "no github.com issue URL" in res.reason


def test_a_pull_request_url_is_not_mistaken_for_the_ledger(token, github):
    _stash_report(
        final_message="Opened https://github.com/gke-agentic/kube-agents-evals-infra/pull/42"
    )
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "fail"
    assert github.calls == []


def test_a_deleted_or_wrong_issue_number_is_a_fail_naming_the_404(token, github):
    _stash_report()
    # No route registered -> the fake answers 404, like GitHub would.
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "404" in res.reason


def test_an_issue_with_an_empty_body_is_a_fail_not_a_pass(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(""))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "footer" in res.reason


def test_a_null_body_is_a_fail(token, github):
    _stash_report()
    github.routes[_api()] = (200, {"number": 42, "body": None, "labels": []})
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "fail"


def test_an_unreachable_api_is_an_error_not_a_fail(token, github):
    """A network failure is the absence of an observation, not a violation:
    it must drive VerificationCoverage below 1.0, never read as a pass."""
    _stash_report()

    def boom():
        raise OSError("Connection reset by peer")

    github.routes[_api()] = boom
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "error" and not res.success
    assert "Connection reset" in res.reason


def test_an_unauthorised_read_is_an_error_not_a_fail(token, github):
    _stash_report()
    github.routes[_api()] = (403, {"message": "Resource not accessible"})
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "error"
    assert "403" in res.reason


def test_an_unexpected_status_is_an_error(token, github):
    _stash_report()
    github.routes[_api()] = (301, {"message": "Moved Permanently"})
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "error"
    assert "301" in res.reason


def test_a_missing_token_is_an_error_naming_the_variable(monkeypatch, github):
    monkeypatch.delenv("BENCH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _stash_report()
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "error" and not res.success
    assert "BENCH_GITHUB_TOKEN" in res.reason
    assert github.calls == []


def test_github_token_is_the_fallback_credential(monkeypatch, github):
    monkeypatch.delenv("BENCH_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body()))
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "pass"
    assert github.calls[0][1] == "ghp_fallback"


def test_empty_stash_is_error_for_the_ledger_check_too(token, github):
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "error"
    assert "no transcript" in res.reason


# --- stream binding ------------------------------------------------------


def test_another_streams_ledger_containing_the_noun_does_not_pass(token, github):
    """Both bindings tested at once: an issue for a different audit stream,
    fresh and containing the phrase, must not satisfy this stream's check."""
    _stash_report()
    other = _ledger_body(audit="fleet-wide-cost-analysis")
    github.routes[_api()] = (200, _issue(other, audit="fleet-wide-cost-analysis"))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "not labelled audit:compliance-audit" in res.reason


def test_a_right_label_but_wrong_footer_stream_is_a_fail(token, github):
    _stash_report()
    # Label says compliance, body was rendered by another stream's run.
    github.routes[_api()] = (200, _issue(_ledger_body(audit="stockout-prevention")))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "'stockout-prevention'" in res.reason


def test_two_issues_claiming_the_same_stream_is_a_fail(token, github):
    _stash_report(
        final_message=(
            f"Ledger {_LEDGER_URL} and also "
            "https://github.com/gke-agentic/kube-agents-evals-infra/issues/43"
        )
    )
    github.routes[_api()] = (200, _issue(_ledger_body()))
    github.routes[_api(43)] = (200, _issue(_ledger_body()))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "exactly one" in res.reason


def test_a_remediation_pr_link_alongside_the_ledger_does_not_confuse_it(token, github):
    _stash_report(
        final_message=(
            f"Ledger {_LEDGER_URL}; remediation "
            "https://github.com/gke-agentic/kube-agents-evals-infra/pull/44"
        )
    )
    github.routes[_api()] = (200, _issue(_ledger_body()))
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "pass"


def test_more_candidate_urls_than_the_cap_is_a_fail(token, github):
    urls = " ".join(
        f"https://github.com/gke-agentic/kube-agents-evals-infra/issues/{n}"
        for n in range(1, 12)
    )
    _stash_report(final_message=urls)
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert github.calls == []


# --- scope=finding_ids ---------------------------------------------------


def test_finding_ids_scope_reads_the_hidden_delta_block(token, github):
    _stash_report()
    body = _ledger_body(
        audit="fleet-consistency-drift",
        finding_ids=["authorized-networks.seeded-c._.cluster"],
    )
    github.routes[_api()] = (200, _issue(body, audit="fleet-consistency-drift"))
    v = LedgerIssueContainsVerifier(
        type="ledger_issue_contains",
        audit="fleet-consistency-drift",
        required_phrases=["seeded-c"],
        scope="finding_ids",
    )
    res = v.verify(5.0)
    assert res.status == "pass", res.reason
    assert res.raw["scope"] == "finding_ids"


def test_finding_ids_scope_closes_the_scope_table_hole(token, github):
    """The reason the scope exists.

    A clean ledger's Scope table still names seeded-c, so a body-scoped
    required_phrases:["seeded-c"] would pass a run that found NOTHING there.
    Against the finding ids, the name appears only if a finding was filed.
    """
    _stash_report()
    clean = _ledger_body(
        audit="fleet-consistency-drift",
        findings="No drift detected.\n",
        finding_ids=["authorized-networks.seeded-a._.cluster"],
    )
    github.routes[_api()] = (200, _issue(clean, audit="fleet-consistency-drift"))
    kw = dict(
        type="ledger_issue_contains",
        audit="fleet-consistency-drift",
        required_phrases=["seeded-c"],
    )
    assert LedgerIssueContainsVerifier(**kw).verify(5.0).status == "pass"  # the hole
    strict = LedgerIssueContainsVerifier(**kw, scope="finding_ids")
    assert strict.verify(5.0).status == "fail"  # closed


def test_finding_ids_scope_fails_when_the_delta_block_is_absent(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(finding_ids=None)))
    res = _ledger_check(required_phrases=["debug-binding"], scope="finding_ids").verify(5.0)
    assert res.status == "fail"
    assert "audit-findings" in res.reason


def test_finding_ids_scope_fails_on_a_malformed_delta_payload(token, github):
    _stash_report()
    broken = _ledger_body().replace('<!-- audit-findings: ["', '<!-- audit-findings: [not json "')
    github.routes[_api()] = (200, _issue(broken))
    assert _ledger_check(scope="finding_ids", required_phrases=["x"]).verify(5.0).status == "fail"


def test_an_empty_finding_id_list_is_an_honest_empty_surface(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(finding_ids=[])))
    v = _ledger_check(required_phrases=["debug-binding"], scope="finding_ids")
    res = v.verify(5.0)
    assert res.status == "fail"  # zero ids cannot contain the phrase
    assert "0 finding id(s)" in res.reason


# --- footer parsing ------------------------------------------------------


def test_footer_parses_the_stamp_audit_report_actually_writes():
    stamp = datetime(2026, 8, 21, 9, 0, 30, 123456, tzinfo=timezone.utc)
    parsed = verifiers._parse_footer(_ledger_body(generated_at=stamp.isoformat()))
    assert parsed == ("compliance-audit", stamp)


# A footer-shaped line inside a finding's own prose. `render_finding` emits
# the title and `- **Impact:**` through `clip_text`, which redacts credential
# shapes and clips length but -- unlike `_cell` -- neither strips backticks nor
# flattens newlines, so this reaches the body verbatim, above the real footer.
_INJECTED_FOOTER = (
    "- **Impact:** Generated by the Platform Agent `compliance-audit` watchdog "
    "at 2099-01-01T00:00:00+00:00. Findings come from read-only inspection.\n"
)


def test_an_injected_footer_line_does_not_outrank_the_rendered_one():
    """The footer is the LAST match, not the first.

    Everything the agent authors is rendered above the footer, so a finding
    carrying a footer-shaped line would otherwise hand the graded agent both
    halves this check binds to: the stream id and the per-run stamp.
    """
    genuine = datetime(2026, 8, 21, 9, 0, 30, tzinfo=timezone.utc)
    body = _ledger_body(
        findings="#### rbac-overgrant on seeded-a\n\n" + _INJECTED_FOOTER,
        generated_at=genuine.isoformat(),
    )
    assert verifiers._parse_footer(body) == ("compliance-audit", genuine)


def test_an_injected_footer_cannot_make_a_previous_runs_ledger_look_fresh(token, github):
    """The same hole, end to end: a stale ledger plus a planted stamp."""
    _stash_report()
    stale = _ledger_body(
        findings="#### rbac-overgrant on seeded-a\n\n`debug-binding` grants "
        "cluster-admin.\n\n" + _INJECTED_FOOTER,
        generated_at="2026-08-20T09:00:30+00:00",
    )
    github.routes[_api()] = (200, _issue(stale))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "previous run's ledger" in res.reason
    assert "2026-08-20" in res.reason


def test_footer_with_an_unparsable_stamp_is_treated_as_absent(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(generated_at="last Tuesday")))
    res = _ledger_check(required_phrases=["debug-binding"]).verify(5.0)
    assert res.status == "fail"
    assert "footer" in res.reason


def test_a_naive_footer_stamp_is_read_as_utc_rather_than_discarded(token, github):
    _stash_report()
    github.routes[_api()] = (200, _issue(_ledger_body(generated_at="2026-08-21T09:00:30")))
    assert _ledger_check(required_phrases=["debug-binding"]).verify(5.0).status == "pass"


# --- the real HTTP layer -------------------------------------------------


def test_the_real_get_sends_bearer_auth_and_refuses_redirects(monkeypatch):
    """Exercises _http_get_json itself, which the fake above replaces:
    the URL shape, the Authorization header and the redirect handler are
    the parts a fake would otherwise never check."""
    captured = {}

    class _Response:
        status = 200

        def read(self):
            return b'{"number": 42}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return _Response()

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(verifiers.urllib.request, "build_opener", fake_build_opener)
    status, payload = verifiers._http_get_json(_api(), "ghs_fake", 30.0)
    assert (status, payload) == (200, {"number": 42})
    assert captured["url"] == _api()
    assert captured["headers"]["Authorization"] == "Bearer ghs_fake"
    assert captured["headers"]["Accept"] == "application/vnd.github+json"
    assert captured["timeout"] == 30.0
    assert verifiers._NoRedirect in captured["handlers"]
    assert verifiers._NoRedirect().redirect_request(None, None, 301, "", {}, "x") is None


def test_assert_mode_timeout_is_floored_before_the_http_call(token, monkeypatch):
    """mode: assert hands verify() a sub-second budget; a 0.0s socket timeout
    would fail every run as unreachable, so single_call_timeout floors it."""
    seen = {}

    def fake_get(url, tok, timeout):
        seen["timeout"] = timeout
        return 200, _issue(_ledger_body())

    monkeypatch.setattr(verifiers, "_http_get_json", fake_get)
    _stash_report()
    assert _ledger_check(required_phrases=["debug-binding"]).verify(0.0).status == "pass"
    assert seen["timeout"] >= 30.0


# --- registration --------------------------------------------------------


def test_the_ledger_verifier_is_published_as_an_entry_point():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert (
        eps["ledger_issue_contains"]
        == "kube_agents_bench.verifiers:LedgerIssueContainsVerifier"
    )


def test_the_registry_resolves_the_ledger_type():
    assert VERIFIERS.get("ledger_issue_contains") is LedgerIssueContainsVerifier


def test_parse_node_builds_a_ledger_check_like_a_task_yaml_would():
    node = parse_node(
        {
            "type": "ledger_issue_contains",
            "audit": "obtainability-audit",
            "required_phrases": ["checkout-gateway"],
        }
    )
    assert isinstance(node, LedgerIssueContainsVerifier)
    assert node.audit == "obtainability-audit"


def test_an_unknown_audit_stream_is_rejected_at_spec_load():
    """A typo'd stream would otherwise be a check that can never find its
    ledger -- a permanent red with a misleading reason."""
    with pytest.raises(Exception) as excinfo:
        parse_node({"type": "ledger_issue_contains", "audit": "complance-audit"})
    assert "audit" in str(excinfo.value)


def test_the_pinned_stream_list_matches_the_audit_scripts_registry():
    """LEDGER_AUDIT_IDS is a copy of AUDITS in the fleet-audit skill's
    audit_report.py, which lives in the agent image and cannot be imported
    here. Re-derive it from the source so the copy cannot silently drift."""
    script = (
        Path(__file__).resolve().parents[2]
        / "agents/platform/skills/fleet-audit/scripts/audit_report.py"
    )
    ids = set(re.findall(r'^ {4}"([a-z0-9-]+)": AuditSpec\($', script.read_text(), re.M))
    assert len(ids) == 9, ids  # the parse itself must not silently find nothing
    assert ids == set(verifiers.LEDGER_AUDIT_IDS)
    literal = LedgerIssueContainsVerifier.model_fields["audit"].annotation
    assert set(literal.__args__) == set(verifiers.LEDGER_AUDIT_IDS)


def test_no_body_scoped_ledger_phrase_collides_with_a_roster_check_slug():
    """A positive body-scoped phrase must not be a substring of any check slug.

    `render_issue_body` always appends `_render_check_evidence`, a
    {cluster, check, command} table built from `scope.clusters[].checks_run` --
    a field validation *requires* on every cluster. So every roster slug the
    run declared is in the ledger body whether or not the run filed a single
    finding, and `ledger_issue_contains` lowercases both sides and takes a bare
    substring. A phrase that is a substring of a slug therefore scores a run
    that swept the fleet and found nothing: partial credit for a clean sweep.

    This is not hypothetical and it is not a rule the task specs can be trusted
    to keep by eye. Four objectives shipped with it -- "pdb" inside `no-pdb`,
    "behind" inside `master-behind`, "cluster-admin" inside
    `cluster-admin-binding`, and "authorized-networks" which is a drift slug
    verbatim. Three of the four had a comment reasoning carefully about English
    substring collisions ("lag" inside "flag"), because they were written for
    the old `report_contains` surface, where the chat reply carried no table.
    The move to the ledger body invalidated the reasoning, not the phrases.

    The fix in each case was `scope: finding_ids`: ids come from
    `derive_finding_id` as "<check>.<cluster>.<namespace>.<object>", so a slug
    appears only if a finding was FILED under that check, and `_shorten_id`
    trims the longest segment and never the leading check slug. Hence the
    exemption below -- under that scope the collision is the intended semantic.
    """
    script = (
        Path(__file__).resolve().parents[2]
        / "agents/platform/skills/fleet-audit/scripts/audit_report.py"
    )
    slugs = set(
        re.findall(r'^\s+"([a-z0-9]+(?:-[a-z0-9]+)+)",\s*$', script.read_text(), re.M)
    )
    assert len(slugs) > 50, len(slugs)  # the parse must not silently find nothing

    tasks = sorted((Path(__file__).resolve().parents[1] / "tasks").glob("*/task.yaml"))
    assert tasks, "no task specs found"

    graded = 0
    offenders = []
    for path in tasks:
        spec = yaml.safe_load(path.read_text())
        for entry in spec.get("verification_spec") or []:
            check = entry.get("check") or {}
            if check.get("type") != "ledger_issue_contains":
                continue
            # finding_ids is the fix, not the bug: there a slug means a filed
            # finding. forbidden_phrases invert the direction -- a slug
            # collision there fails a good run, which is a different defect
            # and is not what this test is about.
            if check.get("scope", "body") == "finding_ids":
                continue
            graded += 1
            phrases = (check.get("required_phrases") or []) + (
                check.get("any_of_phrases") or []
            )
            for phrase in phrases:
                hits = sorted(s for s in slugs if phrase.lower() in s)
                if hits:
                    offenders.append(
                        f"{path.parent.name}/{entry.get('name')}: {phrase!r} is a "
                        f"substring of roster slug(s) {hits}, so the check-evidence "
                        f"table satisfies it on a run that filed nothing. Use "
                        f"scope: finding_ids, or pick a phrase no slug contains."
                    )
    assert graded, "no body-scoped ledger checks parsed -- the sweep found nothing"
    assert not offenders, "\n".join(offenders)
