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

"""The triage delivery contract, held together across the files that own it.

``_triage_task_body`` (agents/platform/scripts/session_kv_server.py) writes the
report template a Cluster Agent fills in. It permits two shapes, and the
difference between them is the whole subject of this module: with two or more
remediations the options are lettered ``Option A``, ``Option B``, ...; with
exactly one the template forbids the letter outright and labels that bullet
``Proposed fix`` instead. The call to action follows either way — the
single-option shape is those two bullets and nothing else — which is why
``To authorize:`` is the line both shapes carry.

``_drift_task_body``, in the same module, writes the card for an out-of-band
change to a live object and reaches the same contract by its own route: same
three ``##`` sections, same two shapes, same authorize bullet, different prose
around all of it. It is a second source of the same literals rather than a
caller of the first, so every join below is per template, and a case is pinned
to whichever template writes the card it grades. Pinning them all to one would
pass for as long as the two agreed on the phrase in question and stop asserting
anything on the day they did not — which is the day the join was supposed to
fire.

Two gates read the result, and neither is written in the same language as the
template:

* ``actionable_report`` (deploy/docker/patches/kanban_notifier.py) decides in
  production whether a completed card earns an ``incidents`` row — whether a
  reply saying ``apply`` will find a report to act on. Three regexes.
* The delivery objective of each case in ``CONTRACTS`` decides whether that
  eval case passes. Phrase lists in a task.yaml.

So one decision lives in three files, joined by string literals. **The
template↔notifier half of that join is already held**, by
``test_the_gate_recognises_the_shape_the_template_asks_the_agent_for`` in
agents/platform/scripts/test_triage_reply_roundtrip.py, which calls the real
``_triage_task_body`` and drives ``actionable_report`` on both shapes; it runs
on every pull request. For ``_drift_task_body`` there is no such test, so the
notifier assertions below are that template's only hold on the notifier rather
than a cross-check. The half nobody held is task.yaml↔template — a reword
there leaves the eval check asserting a string nothing writes, and a check no
report can satisfy reds the case rather than the reword, so the diagnosis lands
a long way from the edit. That is what this module is for.

The notifier assertions below are kept anyway, and they are the smaller half of
this file's value: the roundtrip test transcribes its exemplars by hand, while
these are cut from the template's own text, so a reword moves them with it. Read
them as the cross-check that the two gates have not diverged on the shapes the
template emits, not as first coverage of a join that had none.

Living in bench/tests/ rather than scripts/test_integration_contracts.py is
forced by ``verifiers``, which imports devops_bench — the bench environment is
the only one that has it. (pydantic, the other heavy import, is in the root test
environment already; Makefile PYTHON_TEST_IMPORTS names it.) Only the two tests
that instantiate ``ReportContainsVerifier`` actually need that; the phrase-join
test needs nothing beyond ``ast`` and ``yaml``, and is here to sit with them.

What is NOT asserted here is that the two gates are equivalent. They are not:
``actionable_report`` searches for the option or authorize bullet strictly after
the heading, requires that heading to be a real markdown heading, and is
case-sensitive on the option letter, while the eval check is a normalized
substring match that can do none of the three. The task.yaml comment enumerates
all three and says why each is an acceptable relaxation. The claim here is
narrower and is the one that matters — **on the shapes the template actually
produces, both gates say yes** — plus negatives, so a gate that accepts
everything fails here rather than passing quietly.

The template is read as TEXT rather than imported: ``session_kv_server`` pulls in
fastapi, ``agent_common_server`` and mcp, none of which the bench environment
installs. deploy/docker/patches/test_kanban_notifier.py reads
``verify_kanban_notifier.py`` the same way and for the same kind of reason. The
read goes through ``ast`` rather than a regex over the source, so f-string
quoting and escapes are Python's problem and not ours.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier, _normalize

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The template's home. Two functions in it write card bodies that carry this
#: contract, and a case is pinned to whichever one writes the card it grades --
#: pinning a drift case to the event template would assert a join that does not
#: exist, and would keep passing right up until the two templates diverge on the
#: phrase in question, which is the day the assertion was supposed to fire.
TEMPLATE_MODULE = REPO_ROOT / "agents" / "platform" / "scripts" / "session_kv_server.py"
TRIAGE_TEMPLATE = "_triage_task_body"
DRIFT_TEMPLATE = "_drift_task_body"

#: The cases whose delivery objective this module pins, each with the template
#: that writes its card. Named, not globbed: a glob over the directory would
#: cover exactly these files today and silently cover nothing on the day one is
#: renamed, and it could not supply the second half of the pair anyway.
#: `incident-triage-oom-event-probe` (#1023) and
#: `autoops-crashloop-config-triage` (#1103) carry the incumbent's check
#: verbatim; a further case adopting the delivery contract is added here rather
#: than left to a pattern.
TASKS = REPO_ROOT / "bench" / "tasks"
CONTRACTS = [
    (TASKS / "autoops-warning-event-triage" / "task.yaml", TRIAGE_TEMPLATE),
    (TASKS / "incident-triage-oom-event-probe" / "task.yaml", TRIAGE_TEMPLATE),
    (TASKS / "autoops-crashloop-config-triage" / "task.yaml", TRIAGE_TEMPLATE),
    (TASKS / "gitops-drift-out-of-band-triage" / "task.yaml", DRIFT_TEMPLATE),
]
CHECK_NAME = "triage-delivers-an-actionable-report"

#: The distinct templates the cases above reach, for the assertions that are
#: about a template rather than about a case. Sorted so the parametrize ids are
#: stable between runs.
TEMPLATES = sorted({template for _, template in CONTRACTS})

#: The notifier is a Dockerfile-applied patch that lands flat at
#: ``/opt/hermes/gateway/`` in the image and has no package in the checkout, so
#: it is loaded by path below — and its directory goes on ``sys.path`` too,
#: because it imports siblings (``kanban_handoff_clip``) the way it will find
#: them at runtime, side by side in one directory.
NOTIFIER_DIR = REPO_ROOT / "deploy" / "docker" / "patches"
NOTIFIER_PATH = NOTIFIER_DIR / "kanban_notifier.py"

#: Where each shape starts and stops inside the template text. Each of these is
#: unique in it, and an anchor that stops matching means the template was
#: reworded — which is a red worth having rather than a lenient fallback.
SINGLE_OPTION_ANCHOR = "- **Proposed fix ("
SINGLE_OPTION_BULLETS = 2
WHAT_TO_DO_PHRASE = "What to do"
WHAT_TO_DO_HEADING = f"## {WHAT_TO_DO_PHRASE}"

#: Where the lettered section stops, per template. The two differ because what
#: follows the section differs: the event template prints a console-links line
#: and the drift template goes straight to the horizontal rule, there being no
#: console view of "someone edited this object". Both anchors are the first
#: thing after the section rather than the last thing in it, so a bullet added
#: to the section travels into the exemplar instead of being cut off by it.
LETTERED_END_ANCHORS = {
    TRIAGE_TEMPLATE: "\U0001f517",  # the console-links line that follows the section
    DRIFT_TEMPLATE: "\n\n---",  # the rule before the "Who acts on this" footer
}

#: A report the front door might deliver instead of the card's own: true about
#: the incident, actionable by nobody. Both gates must reject it, and the eval
#: check rejecting it is the coverage a case carrying no delivery objective
#: gives up.
SUMMARY_ONLY_REPORT = (
    "The eval-incident-workload deployment is OOMKilled: it allocates 96MiB "
    "against a 64Mi limit."
)

#: A `What to do` section with nothing under it to act on. The negative that
#: separates "has the heading" from "offers something".
EMPTY_SECTION_REPORT = "## What's wrong\n\nIt is OOMKilled.\n\n## What to do\n\n- Look into it.\n"

#: A call to action with no section to hang it on. The negative that separates
#: "offers something" from "has the heading" — the other direction from
#: EMPTY_SECTION_REPORT, and the one that reds if the case ever stops requiring
#: the heading phrase while keeping its any_of alternatives.
NO_HEADING_REPORT = (
    "It is OOMKilled. Raise the limit to 128Mi.\n\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request.\n"
)

#: append, not insert(0, ...). deploy/docker/patches/ holds some eighty flat
#: modules, twenty-odd of them named ``test_*.py``, and putting that in FRONT of
#: the path would shadow same-named modules for the rest of the pytest session.
#: The tail is enough to resolve the notifier's siblings.
sys.path.append(str(NOTIFIER_DIR))


def _load(name: str, path: Path):
    """Import a module by file path, the way the runtime loads it.

    agents/platform/scripts/test_triage_reply_roundtrip.py loads this same
    module this same way, with the same path append beside it.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


actionable_report = _load("kanban_notifier", NOTIFIER_PATH).actionable_report


def _literal_parts(node: ast.AST) -> str:
    """The string literals of an expression, in source order.

    Both templates return one expression built entirely out of string literals
    and interpolations. The interpolations are dropped rather than rendered —
    every one of them is a record detail (namespace, object name, project id,
    principal), and none carries any part of the report template.

    The recursion exists because the two return expressions are not the same
    shape. ``_triage_task_body`` returns a single implicitly-concatenated
    f-string, which is one ``JoinedStr``; ``_drift_task_body`` splices a
    conditional clause into the middle of its body, which makes its return an
    ``Add`` chain with an ``IfExp`` in it. Both branches of that conditional are
    walked, because template text on the road not taken is still template text
    someone can reword.
    """
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_parts(node.left) + _literal_parts(node.right)
    if isinstance(node, ast.IfExp):
        return _literal_parts(node.body) + _literal_parts(node.orelse)
    return ""


def _template_text(template: str) -> str:
    """The literal half of ``template``'s returned expression."""
    tree = ast.parse(TEMPLATE_MODULE.read_text())
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == template
        ),
        None,
    )
    assert fn is not None, f"{template} is gone from {TEMPLATE_MODULE}"
    ret = next((n for n in ast.walk(fn) if isinstance(n, ast.Return)), None)
    assert ret is not None, f"{template} no longer returns anything"
    text = _literal_parts(ret.value)
    assert WHAT_TO_DO_HEADING in text, (
        f"{template}'s return yielded no {WHAT_TO_DO_HEADING!r}. Either the "
        "template was reworded, or its return is a shape _literal_parts does "
        "not walk and this extraction needs a branch for it — the second reads "
        "as an empty template, which would pass every phrase assertion below "
        "vacuously if it were not for this check."
    )
    return text


def _slice_unique(text: str, start: str, stop: str | None) -> str:
    found = text.count(start)
    assert found == 1, (
        f"{start!r} appears {found} times in the template, expected exactly one. "
        "Zero means it was reworded and both gates now read a string nothing "
        "writes; more than one means this anchor no longer picks out a shape."
    )
    begin = text.index(start)
    return text[begin:] if stop is None else text[begin : text.index(stop, begin)]


def _report_shapes(template: str) -> dict[str, str]:
    """One exemplar report per shape ``template`` permits.

    Composed from the template's own lines rather than transcribed, so a reword
    moves these with it. The single-option shape gets the heading prepended
    because the template supplies its bullets as a replacement for the ones
    under an existing ``## What to do`` — "these two bullets and nothing else".
    """
    text = _template_text(template)
    single = "\n".join(
        _slice_unique(text, SINGLE_OPTION_ANCHOR, None).splitlines()[:SINGLE_OPTION_BULLETS]
    )
    return {
        "lettered": _slice_unique(text, WHAT_TO_DO_HEADING, LETTERED_END_ANCHORS[template]),
        "single-option": f"{WHAT_TO_DO_HEADING}\n\n{single}\n",
    }


def _delivery_check(case_path: Path) -> dict:
    document = yaml.safe_load(case_path.read_text())
    entry = next(
        (e for e in document["verification_spec"] if e.get("name") == CHECK_NAME), None
    )
    assert entry is not None, (
        f"{case_path.parent.name} has no {CHECK_NAME!r} objective. If it was "
        "renamed, rename CHECK_NAME with it; if it was deleted, remove the case "
        "from CONTRACTS rather than leaving this module asserting nothing."
    )
    return entry["check"]


@pytest.fixture(params=CONTRACTS, ids=lambda c: c[0].parent.name)
def contract(request) -> tuple[Path, str]:
    """Every case-level test below runs once per case that carries the contract.

    The pair, not the path: a case is only pinned to a template, so a test that
    took the path alone would have to guess which one, and guessing wrong is the
    failure this parameter exists to make impossible.
    """
    return request.param


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def _eval_check_accepts(report: str, case_path: Path) -> bool:
    """The case's own objective, run through the real verifier.

    Constructed from the task.yaml rather than restated, which is what makes
    this a contract test: edit the case and these assertions re-evaluate against
    the edit instead of against a copy of what it used to say.
    """
    transcript.set(report, [])
    return ReportContainsVerifier(**_delivery_check(case_path)).verify(5.0).success


@pytest.mark.parametrize("shape", ["lettered", "single-option"])
def test_the_eval_check_accepts_both_template_shapes(shape, contract):
    """The regression #1101 was filed for.

    The objective used to require "Option A", a token the single-option shape is
    forbidden to contain, so a correct one-remediation triage failed delivery on
    a rule the template had already decided the other way. #1057 lost three
    graded repetitions to it.
    """
    case_path, template = contract
    assert _eval_check_accepts(_report_shapes(template)[shape], case_path)


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("shape", ["lettered", "single-option"])
def test_the_notifier_gate_accepts_both_template_shapes(shape, template):
    """The other side of the same bar, and the reason the eval check reads it.

    ``actionable_report`` is what decides whether a reply of ``apply`` finds
    anything; a shape it rejects is delivered to the user and then unactionable.

    Per template rather than per case: the notifier does not know which case a
    report came from, and two cases sharing a template would otherwise assert
    the same thing twice.
    """
    assert actionable_report(_report_shapes(template)[shape])


@pytest.mark.parametrize(
    "report", [SUMMARY_ONLY_REPORT, EMPTY_SECTION_REPORT, NO_HEADING_REPORT]
)
def test_both_gates_reject_a_report_with_nothing_to_act_on(report, contract):
    """Neither gate may be satisfiable by everything.

    Without this the module would pass just as happily against a check that
    asserted nothing, which is the failure it was written to prevent. The three
    exemplars fail in three different ways — no section, a section with nothing
    under it, and a call to action with no section — so a relaxation on either
    side of the contract trips at least one of them.
    """
    case_path, _template = contract
    assert not _eval_check_accepts(report, case_path)
    assert not actionable_report(report)


def test_every_phrase_the_case_requires_is_still_in_the_template(contract):
    """The join itself, stated directly.

    The tests above would catch a template reword through the shapes it
    produces. This catches the narrower case where a required phrase survives in
    an exemplar but has left the template that is supposed to be its source —
    the point at which the case is asserting a string nothing writes any more.
    """
    # Normalized on both sides, the way the verifier compares them. A raw
    # substring test would be STRICTER than the check it stands in for -- a
    # template emitting `**To authorize**:` still satisfies the eval check but
    # would fail a literal `in` -- and a false red on a case that works is the
    # one failure mode this module must not introduce.
    case_path, template = contract
    text = _normalize(_template_text(template))
    check = _delivery_check(case_path)
    required = check.get("required_phrases", [])
    assert WHAT_TO_DO_PHRASE in required, (
        f"{CHECK_NAME} no longer requires {WHAT_TO_DO_PHRASE!r}. `actionable_report` "
        "hard-requires that heading and returns False without it, so a case that "
        "stops asking for it passes reports the production gate would refuse. "
        "Without this assertion an empty required_phrases satisfies every check "
        "below vacuously."
    )
    missing = [p for p in required if _normalize(p) not in text]
    assert not missing, f"{CHECK_NAME} requires phrases the template no longer writes: {missing}"
    alternatives = check.get("any_of_phrases", [])
    assert alternatives, (
        f"{CHECK_NAME} has no any_of_phrases. The two template shapes differ, "
        "so a delivery check written only from required_phrases asserts one of "
        "them and reds the other."
    )
    # EVERY alternative, not just one of them. `any_of_phrases` is a disjunction
    # when the verifier grades a report -- either token satisfies it -- but each
    # listed token still has to be one the template actually writes. Accepting
    # "at least one present" lets a rename of `Option A` pass unnoticed, because
    # `To authorize:` survives it and carries the list on its own; the lettered
    # alternative is then dead text asserting a string nothing emits, which is
    # precisely the drift this test is here to name.
    dead = [p for p in alternatives if _normalize(p) not in text]
    assert not dead, (
        f"{CHECK_NAME} offers alternatives the template no longer writes: {dead}. "
        "The check still passes on the surviving alternative, so nothing else in "
        "the suite reds -- which is why this assertion is per-phrase."
    )
