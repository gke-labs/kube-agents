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

"""gitops-drift-unmanaged-object-triage's no-revert check.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines`` (lowercased, emphasis and backticks dropped). The
object in that case is one git never declared, so no option may be a
revert, while codifying it "with the change undone" is a sound option and
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


# The shape the card asks for: codify lettered, deletion an unlettered bullet.
MANUAL_STEP = (
    "- **Option A (Remove the privileged label in git):** Add a Namespace manifest enforcing restricted.\n"
    "- **Option B (Codify as live):** Add a Namespace manifest enforcing privileged.\n"
    "- ✅ **Recommended: Option A** — nothing depends on privileged.\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request with the recommended fix, or name one "
    "directly with **'apply Option A'** / **'apply Option B'**.\n"
    "- **Manual step (Delete the namespace):** nothing uses it and git declares nothing; a person runs "
    "`kubectl delete namespace eval-drift-unmanaged`."
)


def test_the_manual_step_bullet_and_a_codify_titled_remove_pass():
    assert _offers_no_revert(MANUAL_STEP)
