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

"""The triage delivery contract, held together across the three files that own it.

``_triage_task_body`` (agents/platform/scripts/session_kv_server.py) writes the
report template a Cluster Agent fills in. It permits two shapes, and the
difference between them is the whole subject of this module: with two or more
remediations the options are lettered ``Option A``, ``Option B``, ...; with
exactly one the template forbids the letter outright and the report ends on a
``Proposed fix`` bullet instead.

Two gates read the result, and neither is written in the same language as the
template:

* ``actionable_report`` (deploy/docker/patches/kanban_notifier.py) decides in
  production whether a completed card earns an ``incidents`` row — whether a
  reply saying ``apply`` will find a report to act on. Three regexes.
* ``autoops-warning-event-triage``'s delivery objective decides whether the eval
  case passes. Phrase lists in a task.yaml.

So one decision lives in three files, joined by string literals, and nothing
executes all three together. A reword of the template silently un-gates both
readers: the notifier stops writing the row (the #802 failure, reintroduced with
nothing red) and the eval check stops asserting delivery while still reporting
green. That is the drift this module exists to catch, and it is the same
silent-green shape scripts/test_integration_contracts.py was written for — it
cannot live there because ``verifiers`` needs pydantic and devops-bench, which
only the bench environment has.

What is NOT asserted here is that the two gates are equivalent. They are not:
``actionable_report`` searches for the option or authorize bullet strictly after
the heading, and is case-sensitive on the option letter, while the eval check is
a normalized substring match that can do neither. The claim is narrower and is
the one that matters — **on the shapes the template actually produces, both
gates say yes** — plus a negative on each, so a gate that accepts everything
fails here rather than passing quietly.

The template is read as TEXT rather than imported: ``session_kv_server`` pulls in
fastapi, ``agent_common_server`` and mcp, none of which the bench environment
installs. deploy/docker/patches/test_kanban_notifier.py reads
``verify_kanban_notifier.py`` the same way and for the same kind of reason. The
read goes through ``ast`` rather than a regex over the source, so f-string
quoting and escapes are Python's problem and not ours.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The case whose delivery objective this module pins. One case, deliberately:
#: the sibling AutoOps cases carry no delivery objective (see #1103), and a glob
#: over the directory would silently cover nothing on the day this one is
#: renamed.
CASE_PATH = REPO_ROOT / "bench" / "tasks" / "autoops-warning-event-triage" / "task.yaml"
CHECK_NAME = "triage-delivers-an-actionable-report"

#: The template's home, and the function inside it that returns the card body.
TEMPLATE_MODULE = REPO_ROOT / "agents" / "platform" / "scripts" / "session_kv_server.py"
TEMPLATE_FUNCTION = "_triage_task_body"

#: ``kanban_notifier`` is stdlib-only at module level, so a path append is the
#: whole import ceremony.
NOTIFIER_DIR = REPO_ROOT / "deploy" / "docker" / "patches"

#: Where each shape starts and stops inside the template text. Each of these is
#: unique in it, and an anchor that stops matching means the template was
#: reworded — which is a red worth having rather than a lenient fallback.
SINGLE_OPTION_ANCHOR = "- **Proposed fix ("
SINGLE_OPTION_BULLETS = 2
WHAT_TO_DO_HEADING = "## What to do"
LETTERED_END_ANCHOR = "\U0001f517"  # the console-links line that follows the section

#: A report the front door might deliver instead of the card's own: true about
#: the incident, actionable by nobody. Both gates must reject it, and the eval
#: check rejecting it is the coverage #1103's sibling case chose to give up.
SUMMARY_ONLY_REPORT = (
    "The eval-incident-workload deployment is OOMKilled: it allocates 96MiB "
    "against a 64Mi limit."
)

#: A `What to do` section with nothing under it to act on. The negative that
#: separates "has the heading" from "offers something".
EMPTY_SECTION_REPORT = "## What's wrong\n\nIt is OOMKilled.\n\n## What to do\n\n- Look into it.\n"

sys.path.insert(0, str(NOTIFIER_DIR))

from kanban_notifier import actionable_report  # noqa: E402


def _template_text() -> str:
    """The literal half of ``_triage_task_body``'s returned f-string.

    The interpolations are dropped rather than rendered — every one of them is
    an event detail (namespace, object name, project id), and none carries any
    part of the report template.
    """
    tree = ast.parse(TEMPLATE_MODULE.read_text())
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == TEMPLATE_FUNCTION
        ),
        None,
    )
    assert fn is not None, f"{TEMPLATE_FUNCTION} is gone from {TEMPLATE_MODULE}"
    ret = next((n for n in ast.walk(fn) if isinstance(n, ast.Return)), None)
    assert ret is not None and isinstance(ret.value, ast.JoinedStr), (
        f"{TEMPLATE_FUNCTION} no longer returns one f-string; this extraction "
        "reads the literal parts of a JoinedStr and needs rewriting"
    )
    return "".join(v.value for v in ret.value.values if isinstance(v, ast.Constant))


def _slice_unique(text: str, start: str, stop: str | None) -> str:
    found = text.count(start)
    assert found == 1, (
        f"{start!r} appears {found} times in the template, expected exactly one. "
        "Zero means it was reworded and both gates now read a string nothing "
        "writes; more than one means this anchor no longer picks out a shape."
    )
    begin = text.index(start)
    return text[begin:] if stop is None else text[begin : text.index(stop, begin)]


def _report_shapes() -> dict[str, str]:
    """One exemplar report per shape the template permits.

    Composed from the template's own lines rather than transcribed, so a reword
    moves these with it. The single-option shape gets the heading prepended
    because the template supplies its bullets as a replacement for the ones
    under an existing ``## What to do`` — "these two bullets and nothing else".
    """
    text = _template_text()
    single = "\n".join(
        _slice_unique(text, SINGLE_OPTION_ANCHOR, None).splitlines()[:SINGLE_OPTION_BULLETS]
    )
    return {
        "lettered": _slice_unique(text, WHAT_TO_DO_HEADING, LETTERED_END_ANCHOR),
        "single-option": f"{WHAT_TO_DO_HEADING}\n\n{single}\n",
    }


def _delivery_check() -> dict:
    document = yaml.safe_load(CASE_PATH.read_text())
    entry = next(
        (e for e in document["verification_spec"] if e.get("name") == CHECK_NAME), None
    )
    assert entry is not None, (
        f"{CASE_PATH.parent.name} has no {CHECK_NAME!r} objective. If it was "
        "renamed, rename CHECK_NAME with it; if it was deleted, delete this "
        "module rather than leaving it asserting nothing."
    )
    return entry["check"]


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def _eval_check_accepts(report: str) -> bool:
    """The case's own objective, run through the real verifier.

    Constructed from the task.yaml rather than restated, which is what makes
    this a contract test: edit the case and these assertions re-evaluate against
    the edit instead of against a copy of what it used to say.
    """
    transcript.set(report, [])
    return ReportContainsVerifier(**_delivery_check()).verify(5.0).success


@pytest.mark.parametrize("shape", ["lettered", "single-option"])
def test_the_eval_check_accepts_both_template_shapes(shape):
    """The regression #1101 was filed for.

    The objective used to require "Option A", a token the single-option shape is
    forbidden to contain, so a correct one-remediation triage failed delivery on
    a rule the template had already decided the other way. #1057 lost three
    graded repetitions to it.
    """
    assert _eval_check_accepts(_report_shapes()[shape])


@pytest.mark.parametrize("shape", ["lettered", "single-option"])
def test_the_notifier_gate_accepts_both_template_shapes(shape):
    """The other side of the same bar, and the reason the eval check reads it.

    ``actionable_report`` is what decides whether a reply of ``apply`` finds
    anything; a shape it rejects is delivered to the user and then unactionable.
    """
    assert actionable_report(_report_shapes()[shape])


@pytest.mark.parametrize("report", [SUMMARY_ONLY_REPORT, EMPTY_SECTION_REPORT])
def test_both_gates_reject_a_report_with_nothing_to_act_on(report):
    """Neither gate may be satisfiable by everything.

    Without this the module would pass just as happily against a check that
    asserted nothing, which is the failure it was written to prevent.
    """
    assert not _eval_check_accepts(report)
    assert not actionable_report(report)


def test_every_phrase_the_case_requires_is_still_in_the_template():
    """The join itself, stated directly.

    The tests above would catch a template reword through the shapes it
    produces. This catches the narrower case where a required phrase survives in
    an exemplar but has left the template that is supposed to be its source —
    the point at which the case is asserting a string nothing writes any more.
    """
    text = _template_text()
    check = _delivery_check()
    missing = [p for p in check.get("required_phrases", []) if p not in text]
    assert not missing, f"{CHECK_NAME} requires phrases the template no longer writes: {missing}"
    alternatives = check.get("any_of_phrases", [])
    assert alternatives, (
        f"{CHECK_NAME} has no any_of_phrases. The two template shapes differ, "
        "so a delivery check written only from required_phrases asserts one of "
        "them and reds the other."
    )
    assert [p for p in alternatives if p in text], (
        f"none of {CHECK_NAME}'s alternatives appear in the template: {alternatives}"
    )
