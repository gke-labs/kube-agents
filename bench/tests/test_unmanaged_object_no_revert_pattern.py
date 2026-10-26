"""gitops-drift-unmanaged-object-triage's two report checks on the conclusion.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines`` (lowercased, emphasis and backticks dropped). The
object in that case is one git never declared, so no option may be a
revert, while "add it to git with the change undone" -- worded however the
agent likes -- is the sound option and must pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "gitops-drift-unmanaged-object-triage" / "task.yaml"


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


def _offers_no_revert(report: str) -> bool:
    text = _normalize_lines(report)
    return not any(re.search(p, text) for p in _check("triage-offers-no-revert")["forbidden_patterns"])


def _says_git_does_not_declare(report: str) -> bool:
    text = _normalize_lines(report)
    return any(re.search(p, text) for p in _check("triage-says-git-does-not-declare-the-object")["any_of_patterns"])


# The red on main, verbatim (2026-10-26, control image of bee8a9822).
RED = (
    "- **Option A (Revert out-of-band Pod Security label):** Remove label "
    "`pod-security.kubernetes.io/enforce=privileged` from namespace `eval-drift-unmanaged` in GitOps.\n"
    "- **Option B (Codify privileged Pod Security label):** Add `pod-security.kubernetes.io/enforce: privileged` "
    "to namespace `eval-drift-unmanaged` manifest in GitOps."
)

# The first green on the fix, verbatim, and the sound option worded the ways
# the card's own phrasing invites.
SOUND = (
    "- **Option A (Add to GitOps with Baseline PSS):** Add manifest for `namespaces/eval-drift-unmanaged`.\n"
    "- **Option B (Add to GitOps as Live):** Add manifest retaining the live privileged label.",
    "- **Option A (Restore `restricted` enforcement in git):** Add a Namespace manifest enforcing restricted.",
    "- **Option B (Undo the change by codifying the namespace):** Add it to git at restricted.",
    "- **Proposed fix (Add to git with the change undone):** Add the namespace at restricted.",
)


def test_the_red_offers_a_revert():
    assert not _offers_no_revert(RED)


def test_the_sound_option_is_not_taken_for_a_revert():
    for report in SOUND:
        assert _offers_no_revert(report), report


def test_a_single_option_revert_is_caught():
    assert not _offers_no_revert("- **Proposed fix (Roll back the label):** Remove it in GitOps.")


def test_the_red_does_not_say_git_lacks_the_object_and_the_green_does():
    assert not _says_git_does_not_declare(RED)
    assert _says_git_does_not_declare(
        "contains no GitOps management markers, confirming git declares nothing for this object."
    )
