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
"""The recorded-weeks stack's report templates are the collector's own rendering.

`bench/tf/prebuilt/upgrade-retro-recorded-weeks/render.py` renders the templates
from `upgrade_retrospective.py`; the checked-in files must equal its output, so a
renderer change regenerates them rather than leaving the two gate cases grading a
report the collector no longer writes.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "upgrade-retro-recorded-weeks"
SCRIPTS = REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
EXPECTED_PLACEHOLDERS = {"{{PROJECT}}"} | {f"{{{{CLUSTER_{s}}}}}" for s in "ABCD"} | {f"{{{{LOCATION_{s}}}}}" for s in "ABCD"}
CASES = ("upgrades-retrospective-log-volume-gate", "upgrades-rollout-family-held-by-canary-gate")


@pytest.fixture(scope="module")
def render():
    spec = importlib.util.spec_from_file_location("render", STACK / "render.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(module)
    return module


def test_the_checked_in_templates_are_the_renderers_output(render):
    for name, text in render.render_all().items():
        assert (STACK / "report" / name).read_text() == text, f"{name} drifted: run python3 {STACK.relative_to(REPO)}/render.py"


def test_the_templates_carry_the_placeholders_and_the_sentinels(render):
    for mode in render.MODES:
        md = (STACK / "report" / f"report-{mode}.md.tmpl").read_text()
        twin = json.loads((STACK / "report" / f"report-{mode}.json.tmpl").read_text())
        assert set(PLACEHOLDER.findall(md)) == EXPECTED_PLACEHOLDERS
        assert twin["generated_at"] == render.GENERATED_AT
        assert md.startswith(f"# Upgrade retrospective {render.TITLE_DATE}")
        assert render.GENERATED_AT in md


def test_the_risk_report_carries_the_gate_the_cases_grade(render):
    sys.path.insert(0, str(SCRIPTS))
    import upgrade_retrospective as ur

    md = (STACK / "report" / "report-risk.md.tmpl").read_text()
    twin = json.loads((STACK / "report" / "report-risk.json.tmpl").read_text())
    assert f"{ur.PART_GATE} {ur.GATE_RISK}: {ur.GATE_REASON_LOG_VOLUME.format(count=1)}" in md
    assert ur.PART_LOG_VOLUME in md and "kube-system/konnectivity-agent" in md
    assert ur.INFO_FAMILIES in md
    review = twin["reviews"][0]
    assert review["gate"]["verdict"] == ur.GATE_RISK
    assert [row["object"] for row in review["gate"]["log_volume"]["rows"] if row["breach"]] == ["kube-system/Container/konnectivity-agent"]
    regular = next(f for f in twin["families"] if f["family"] == "seeded-regular")
    assert (regular["verdict"], regular["canaries"][0]["cluster"]) == (ur.FAMILY_HOLD, "{{PROJECT}}/{{LOCATION_A}}/{{CLUSTER_A}}")
    assert [w["check"] for w in twin["sections"]["warnings"]] == [ur.CHECK_LOG_VOLUME]
    clear = json.loads((STACK / "report" / "report-clear.json.tmpl").read_text())
    assert clear["reviews"][0]["gate"]["verdict"] == ur.GATE_CLEAR
    assert clear["sections"]["warnings"] == []


def test_the_two_cases_use_the_stack_in_risk_mode():
    import yaml

    for case in CASES:
        task = yaml.safe_load((REPO / "bench" / "tasks" / case / "task.yaml").read_text())
        assert task["infrastructure"]["stack"] == "prebuilt/upgrade-retro-recorded-weeks"
        assert task["infrastructure"]["variables"] == {"gate_mode": "risk"}
        assert task["infrastructure"]["teardown"] is True
