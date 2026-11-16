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

"""gitops-drift-out-of-band-triage's button-title check.

The card fixes the titles of the revert and codify options, because each
title becomes a chat button. The check passes only when an option's title,
not its prose, carries one of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from kube_agents_bench.verifiers import pattern_surface

TASK = Path(__file__).resolve().parent.parent / "tasks" / "gitops-drift-out-of-band-triage" / "task.yaml"


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


def _titles_read_as_buttons(report: str) -> bool:
    check = _check("triage-titles-the-two-ways-out-as-the-buttons-read")
    text = pattern_surface(report)
    return any(re.search(p, text) for p in check["any_of_patterns"])


# The red on main, verbatim (2026-11-11, mn-a51d0cd7).
RED = (
    "- **Option A (Revert to git):** Remove the out-of-band change by letting the GitOps controller "
    "reconcile the deployment back to its declared replica count.\n"
    "- **Option B (Codify in git):** Update the git manifest to declare `replicas: 3` so that the current "
    "live capacity becomes the new baseline."
)

# A green on the branch, verbatim (2026-11-11, dr-4d5804ef).
GREEN = (
    "- **Option A (Put Git's version back):** Revert the deployment back to the GitOps declared replica "
    "count, removing the manual change.\n"
    "- **Option B (Keep it · update Git):** Update the GitOps manifests to declare 3 replicas for this "
    "deployment."
)


def test_the_red_fails():
    assert not _titles_read_as_buttons(RED)


def test_the_green_passes():
    assert _titles_read_as_buttons(GREEN)


def test_the_words_in_an_options_prose_do_not_pass():
    report = (
        "- **Option A (Revert replica count):** Put Git's version back by reconciling.\n"
        "- **Option B (Accept 3 replicas):** Open a PR to update Git."
    )
    assert not _titles_read_as_buttons(report)


def test_a_lone_proposed_fix_with_either_title_passes():
    for title in ("Put Git’s version back", "Put Gits version back", "Keep it - update Git", "Keep it, update Git"):
        assert _titles_read_as_buttons(f"- **Proposed fix ({title}):** Open a GitOps Pull Request."), title
