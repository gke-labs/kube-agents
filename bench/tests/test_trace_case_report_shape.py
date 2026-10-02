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
report check built from them alone passes on the failure; and a 32-hex id is
not the helper's alone, since an OTel ``trace_id`` in an exporter log line
or a session id has the same shape, so a worker that quotes one beside a
percentage has the shapes without the breakdown. This module runs the case's
own objective report checks against both failure reports and against the
helper's breakdown in each shape the recorded green runs relayed it, so a
check that a failure report satisfies, or one that a real breakdown fails,
fails here rather than on the nightly.
"""

from __future__ import annotations

import pytest
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

# The same failure, by a worker that quoted an exporter log line and its
# session id: two 32-hex values it never got from the helper, with a
# percentage and the prompt's "slowest" elsewhere in the report.
STRAY_ID_FAILURE_REPORT = (
    "## No traces analyzed\n\n"
    "The analyzer exited 1 at its auth step, so I have 0% of the three requested "
    "traces and no slowest span to report.\n\n"
    "The agent pod's exporter log (`kubectl logs ... | grep -iE otel`) shows the "
    "export path itself is healthy:\n\n"
    "```\n"
    "2026-09-28T16:50:01Z otel exporter: exported span "
    "trace_id=0384e171d360c91c96df3124562dcc59 span_id=8f2c1a9e0b3d4c5f\n"
    "```\n\n"
    "Session `t_405326f2` (id `9c1e4f0a2b3d4e5f6a7b8c9d0e1f2a3b`)."
)

# One row of the helper's breakdown as a worker relays it.
BREAKDOWN_REPORT = (
    "## Three traces\n\n"
    "- **`0384e171d360c91c96df3124562dcc59`** — `2.590s`, 1 span. "
    "Slowest: `api.model-default` `2.590s` (`100.0%`)."
)

# The helper's own output pasted whole: the id sits on its "Trace ID:" line,
# the duration on the next and the share three lines down.
PASTED_BREAKDOWN_REPORT = (
    "The helper printed:\n\n"
    "```\n"
    "======================================================================\n"
    "Trace ID: 0384e171d360c91c96df3124562dcc59\n"
    "Total Duration: 2.590 seconds | Total Spans: 1\n"
    "Breakdown of spans:\n"
    "  - api.model-default : 2.590s (100.0%)\n"
    "```\n"
)

# The id on a heading or a bold line of its own, a blank line or none, then
# a table or a list: ordinary Markdown for a per-trace section, which puts
# the share two to four lines below the id. The table headers that say
# neither total nor duration are the ones only the data row's own shape (a
# duration in seconds and a share on one line) can satisfy.
HEADING_BREAKDOWN_REPORTS = [
    "### Trace 0384e171d360c91c96df3124562dcc59\n\n"
    "| Total duration | Spans | Slowest span | Share |\n|---|---|---|---|\n"
    "| 2.590s | 1 | `api.model-default` | 100.0% |",
    "**Trace 0384e171d360c91c96df3124562dcc59**\n\n"
    "- Total duration: 2.590s (1 span)\n"
    "- Slowest span: `api.model-default`, 2.590s, 100.0% of the trace",
    "### Trace 0384e171d360c91c96df3124562dcc59\n\n"
    "| Latency | Spans | Slowest span | Share |\n|---|---|---|---|\n"
    "| 2.590s | 1 | `api.model-default` | 100.0% |",
    "#### 0384e171d360c91c96df3124562dcc59\n"
    "| Time | Spans | Slowest | % of trace |\n|---|---|---|---|\n"
    "| 2.59 s | 1 | api.model-default | 100% |",
    "**Trace `0384e171d360c91c96df3124562dcc59`**\n\n"
    "| Seconds | Spans | Slowest span | Share |\n|:--|:--|:--|:--|\n"
    "| 2.590 seconds | 1 | `api.model-default` (2590 ms) | 100.0% |",
]

# A failure that happens to carry a seconds-and-share line but no id the
# helper gave it: the id pattern is what reds it.
TIMED_FAILURE_REPORT = (
    "## No traces analyzed\n\n"
    "The analyzer exited 1 after 0.4s (0% of the three requested traces came back), "
    "so there is no slowest span to report."
)

# One row in each shape the three recorded green runs wrote: a Markdown
# table row, a bullet naming the total, and a console link around the id.
RECORDED_ROW_REPORTS = [
    "| Trace ID | Total | Slowest span | Share |\n|---|---|---|---|\n"
    "| `0384e171d360c91c96df3124562dcc59` | `2.590s` | `api.model-default` `2.590s` | `100.0%` |",
    "- 123089d0ac11f2cf16dc389fc8ad0553 — total 60.917s — slowest span "
    "api.model-default at 18.419s (30.2%). Started 16:54:29Z, 17 spans.",
    "- [`00e1bb32…`](https://console.cloud.google.com/traces/detail/"
    "00e1bb328361e5a5b0cb311f3ba11f06?project=p) — total `10.605s`, 5 spans; "
    "slowest `api.model-default` at `5.654s` (`53.3%`)",
]


def _objective_report_checks() -> list[ReportContainsVerifier]:
    spec = yaml.safe_load(CASE.read_text(encoding="utf-8"))
    checks = [
        ReportContainsVerifier(**entry["check"])
        for entry in spec["verification_spec"]
        if entry.get("role") == "objective" and entry["check"].get("type") == "report_contains"
    ]
    assert checks, f"{CASE} has no objective report_contains check"
    return checks


@pytest.mark.parametrize("report", [FAILURE_REPORT, STRAY_ID_FAILURE_REPORT, TIMED_FAILURE_REPORT])
def test_a_failure_report_in_the_prompts_words_fails_an_objective_check(report):
    transcript.set(report, [])
    verdicts = {c.verify(5.0).status for c in _objective_report_checks()}
    assert "fail" in verdicts, "every objective report check passed a report of the helper's failure"


def test_a_stray_id_beside_a_percentage_fails_the_breakdown_check():
    # The id check in particular, not the slowest-span phrase: the stray-id
    # report says "slowest" and carries "0%", so only the row shape reds it.
    transcript.set(STRAY_ID_FAILURE_REPORT, [])
    (id_check,) = [c for c in _objective_report_checks() if len(c.required_patterns) == 3]
    res = id_check.verify(5.0)
    assert res.status == "fail", res.reason


@pytest.mark.parametrize(
    "report", [BREAKDOWN_REPORT, PASTED_BREAKDOWN_REPORT, *HEADING_BREAKDOWN_REPORTS, *RECORDED_ROW_REPORTS]
)
def test_the_helpers_breakdown_passes_every_objective_check(report):
    transcript.set(report, [])
    for check in _objective_report_checks():
        res = check.verify(5.0)
        assert res.status == "pass", res.reason
