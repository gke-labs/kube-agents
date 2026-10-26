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

"""gitops-drift-unmanaged-object-triage's two report checks on the conclusion.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines`` (lowercased, emphasis and backticks dropped). The
object in that case is one git never declared, so no option may be a
revert, while "add it to git with the change undone" is the sound option and
must pass in the wordings below. A title cannot tell a wrong "Undo ..." from a
right one, so the pattern leaves both to the judge.
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
    for title in ("Roll back the label", "Roll-back the label", "Revert the label"):
        assert not _offers_no_revert(f"- **Proposed fix ({title}):** Remove it in GitOps."), title


def _does_not_offer_to_apply_leaving(report: str) -> bool:
    text = _normalize_lines(report)
    patterns = _check("triage-does-not-offer-to-apply-leaving-it")["forbidden_patterns"]
    return not any(re.search(p, text) for p in patterns)


# Observed on the d8836c11a build, verbatim but for the option bodies.
OFFERS_TO_APPLY_LEAVING = (
    "- **Option A (Codify namespace without privileged label):** Add it to GitOps.\n"
    "- **Option C (Leave unmanaged):** Leave namespace eval-drift-unmanaged unmanaged.\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request with the recommended fix, or name one "
    "directly with **'apply Option A'** / **'apply Option B'** / **'apply Option C'**."
)
NAMES_ONLY_PULL_REQUESTS = (
    "- **Option A (Codify namespace in GitOps with change undone):** Add a manifest.\n"
    "- **Option B (Codify namespace in GitOps as live):** Add a manifest.\n"
    "- **Option C (Leave unmanaged):** Leave it with no GitOps manifest.\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request with the recommended fix, or name one "
    "directly with **'apply Option A'** / **'apply Option B'**."
)


def test_offering_to_apply_the_leave_option_is_caught():
    assert not _does_not_offer_to_apply_leaving(OFFERS_TO_APPLY_LEAVING)
    keep = OFFERS_TO_APPLY_LEAVING.replace("Leave unmanaged", "Keep it unmanaged")
    assert not _does_not_offer_to_apply_leaving(keep)


def test_naming_only_the_pull_request_options_passes():
    assert _does_not_offer_to_apply_leaving(NAMES_ONLY_PULL_REQUESTS)
    assert _does_not_offer_to_apply_leaving(RED)


def test_the_red_does_not_say_git_lacks_the_object_and_the_green_does():
    assert not _says_git_does_not_declare(RED)
    assert _says_git_does_not_declare(
        "contains no GitOps management markers, confirming git declares nothing for this object."
    )
