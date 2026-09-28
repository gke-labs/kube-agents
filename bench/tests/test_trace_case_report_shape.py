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

"""The report checks of ``observability-trace-latency-brokered``, run as shipped.

The case exists to catch a helper that exits 1 before it reads a trace. Its
prompt asks for "each trace's ID ... the single slowest span in it with that
span's share", so a worker whose helper failed writes those words back, and a
report check built from them alone passes on the failure. This module runs
the case's own objective report checks against such a report and against the
helper's breakdown, so a check that a failure report satisfies fails here
rather than recording a green on the regressed nightly.
"""

from __future__ import annotations

import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

from conftest import TASKS

CASE = TASKS / "observability-trace-latency-brokered" / "task.yaml"

# The prompt's own vocabulary, every word of it, around a helper that failed.
FAILURE_REPORT = (
    "## No traces analyzed\n\n"
    "I could not obtain any trace ID, total duration or slowest-span % share "
    "for the three most recent traces: the analyzer exited 1 at its auth step, "
    "so there is no bottleneck or longest span to report. Breakdown of spans: none."
)

# One row of the helper's breakdown as a worker relays it.
BREAKDOWN_REPORT = (
    "## Three traces\n\n"
    "- **`0384e171d360c91c96df3124562dcc59`** — `2.590s`, 1 span. "
    "Slowest: `api.model-default` `2.590s` (`100.0%`)."
)


def _objective_report_checks() -> list[ReportContainsVerifier]:
    spec = yaml.safe_load(CASE.read_text(encoding="utf-8"))
    checks = [
        ReportContainsVerifier(**entry["check"])
        for entry in spec["verification_spec"]
        if entry.get("role") == "objective" and entry["check"].get("type") == "report_contains"
    ]
    assert checks, f"{CASE} has no objective report_contains check"
    return checks


def test_a_failure_report_in_the_prompts_words_fails_an_objective_check():
    transcript.set(FAILURE_REPORT, [])
    verdicts = {c.verify(5.0).status for c in _objective_report_checks()}
    assert "fail" in verdicts, "every objective report check passed a report of the helper's failure"


def test_the_helpers_breakdown_passes_every_objective_check():
    transcript.set(BREAKDOWN_REPORT, [])
    for check in _objective_report_checks():
        res = check.verify(5.0)
        assert res.status == "pass", res.reason
