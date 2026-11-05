# Copyright 2026 Google LLC
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

"""The probe check of ``obtainability-design-quota-vs-capacity``, run as shipped.

``the-quota-and-advice-probes-actually-ran`` grades that the worker ran the
regional quota read and the capacity advice probe. The nightly answers typed
both behind a ``printenv`` or a ``cd``, or inside a ``for`` loop over the
provisioning models, and the card log's rendering of such a command
(``summarize_shell_command`` in hermes ``agent/display.py``) carries the
first command plus `` + N commands`` and at most one probe. The check reads
each tagged ``terminal`` or ``execute_code`` call as typed
(``after_result_pattern: "(?!)"``; the fixtures here are terminal calls), with the
card log's rows beside it. Each fixture here is one worker run: its rendered
rows and its typed calls together, with the verdict the run earns.
``RECORDED`` holds three runs in the nightly's shapes, each row rendered by
the image's renderer (hermes ``agent/display.py``, ``summarize_shell_command``);
``ONE_LINE`` and ``NOT_THE_PROBES`` are hand-written.
"""

from __future__ import annotations

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

from conftest import TASKS

CASE = TASKS / "obtainability-design-quota-vs-capacity" / "task.yaml"
CHECK = "the-quota-and-advice-probes-actually-ran"
AS_TYPED = "after_result_pattern"
CARD = "t_1"

REGIONS_BEHIND_PRINTENV = (
    "printenv GKE_PROJECT_ID + 2 commands",
    'printenv GKE_PROJECT_ID; gcloud compute regions describe us-central1 --project "$GKE_PROJECT_ID"'
    ' --format="json(quotas)" 2>&1 | python3 -c "\nimport json,sys\nd=json.load(sys.stdin)\n'
    "for q in d['quotas']:\n  if 'A100' in q['metric']:\n    print(q)\n\"; echo ---; "
    'gcloud compute reservations list --project "$GKE_PROJECT_ID" --format=json 2>&1',
)
REGIONS_PIPED = (
    'gcloud compute regions describe us-central1 --project kube-agents-evals-16 --format="json(quotas)"'
    ' | python3 -c " import json,sys d=json.load(sys.stdin) print(d) "',
    'gcloud compute regions describe us-central1 --project kube-agents-evals-16 --format="json(quotas)"'
    ' | python3 -c "\nimport json,sys\nd=json.load(sys.stdin)\nprint(d)\n"',
)
REGIONS_BEHIND_CD = (
    'gcloud compute regions describe us-central1 --project "$GKE_PROJECT_ID" --format=json',
    'cd /opt/data/kanban/workspaces/t_1; gcloud compute regions describe us-central1 --project "$GKE_PROJECT_ID"'
    " --format=json > region.json 2>&1; echo rc=$?",
)
ADVICE_IN_A_LOOP = (
    "for m in SPOT FLEX_START + 7 commands",
    'for m in SPOT FLEX_START; do echo "== $m region"; extra=""; [ $m = FLEX_START ] && extra="--max-run-duration=24h";'
    " gcloud beta compute advice capacity --project kube-agents-evals-16 --region=us-central1 --machine-type=a2-highgpu-8g"
    " --accelerator=type=nvidia-tesla-a100,count=8 --provisioning-model=$m $extra --format=json 2>&1 | head -c 1500; echo; done",
)
ADVICE_IN_A_LOOP_BEHIND_CD = (
    "for m in SPOT FLEX_START + 9 commands",
    'cd /opt/data/kanban/workspaces/t_1; for m in SPOT FLEX_START; do for t in a2-highgpu-8g a2-ultragpu-8g; do echo "== $m $t";'
    ' extra=""; [ $m = FLEX_START ] && extra="--max-run-duration=7d"; gcloud beta compute advice capacity'
    " --project kube-agents-evals-30 --region=us-central1 --machine-type=$t --provisioning-model=$m $extra --format=json 2>&1"
    " | head -c 800; echo; done; done",
)
ADVICE_ALONE = (
    "gcloud beta compute advice capacity --project p --region=us-central1 --machine-type=a2-highgpu-8g --provisioning-model=SPOT --format=json",
    "gcloud beta compute advice capacity --project p --region=us-central1 --machine-type=a2-highgpu-8g --provisioning-model=SPOT --format=json",
)
PROJECT_INFO_BEHIND_PRINTENV = (
    "printenv GKE_PROJECT_ID + 1 command",
    'printenv GKE_PROJECT_ID; gcloud compute project-info describe --project "$GKE_PROJECT_ID" --format=json 2>&1 | head -c 2000',
)
MACHINE_TYPES_IN_A_LOOP = (
    "for z in a b c f + 3 commands",
    'for z in a b c f; do echo "=== describe a2-highgpu-8g us-central1-$z"; gcloud compute machine-types describe a2-highgpu-8g'
    ' --zone us-central1-$z --project "$GKE_PROJECT_ID" --format="value(name,guestCpus,accelerators)" 2>&1; done',
)
ADVICE_WITH_A_SUFFIX = (
    "gcloud beta compute advice capacity-planning --project p",
    "gcloud beta compute advice capacity-planning --project p",
)

# (run, verdict): the nightly shapes that were graded as not having run the
# probes; one run with both probes on one line each; the runs that did not
# run them.
RECORDED = [
    ([REGIONS_BEHIND_PRINTENV, ADVICE_IN_A_LOOP], "pass"),
    ([REGIONS_BEHIND_CD, ADVICE_IN_A_LOOP_BEHIND_CD], "pass"),
    ([REGIONS_PIPED, ADVICE_IN_A_LOOP], "pass"),
]
ONE_LINE = [
    ([REGIONS_PIPED, ADVICE_ALONE], "pass"),
]
NOT_THE_PROBES = [
    ([PROJECT_INFO_BEHIND_PRINTENV, MACHINE_TYPES_IN_A_LOOP], "fail"),
    ([REGIONS_PIPED, MACHINE_TYPES_IN_A_LOOP], "fail"),
    ([PROJECT_INFO_BEHIND_PRINTENV, ADVICE_IN_A_LOOP], "fail"),
    ([REGIONS_PIPED, ADVICE_WITH_A_SUFFIX], "fail"),
]
# The chained shapes: a probe is missing from their rendered rows. In the
# first run both are (`printenv ... + 2 commands`, `for m ... + 7 commands`);
# in the second the regions row is whole (the renderer drops the leading
# `cd` and the boundary `echo rc=$?`, as the image's renderer wrote it for
# this row) and the advice loop is collapsed.
CHAINED = [
    [REGIONS_BEHIND_PRINTENV, ADVICE_IN_A_LOOP],
    [REGIONS_BEHIND_CD, ADVICE_IN_A_LOOP_BEHIND_CD],
]


def _check() -> dict:
    spec = yaml.safe_load(CASE.read_text(encoding="utf-8"))
    entries = [e for e in spec["verification_spec"] if e.get("name") == CHECK]
    assert len(entries) == 1, f"{CASE} declares {CHECK} {len(entries)} times"
    return entries[0]["check"]


def _call(command: str, at: float) -> dict:
    """One tagged terminal call of the worker card, as worker_trajectory records it."""
    return {
        "name": "terminal",
        "args": {"command": command},
        "result": "",
        "status": "completed",
        "agent": "platform",
        "task": CARD,
        "session": "s_1",
        "at": at,
    }


def _status(check: dict, run: list[tuple[str, str]], output: str = "ok", result: str = "") -> str:
    """The verdict on one card whose log holds the rendered rows and whose tagged calls read the typed text."""
    for rendered, _ in run:
        assert "\n" not in rendered, rendered
    calls = [_call(typed, 100.0 + i) for i, (_, typed) in enumerate(run)]
    for call in calls:
        call["result"] = result
    transcript.set(output, calls, worker_commands=[{"task": CARD, "command": rendered} for rendered, _ in run])
    try:
        result = WorkerCommandsVerifier(**check).verify(5.0)
    finally:
        transcript.clear()
    assert result.status != "error", result.reason
    return result.status


def test_the_check_reads_the_calls_as_typed():
    assert _check()[AS_TYPED] == "(?!)", "the switch opens no window and reads the calls as typed"


@pytest.mark.parametrize("run, verdict", RECORDED + ONE_LINE + NOT_THE_PROBES)
def test_each_run_earns_the_verdict_the_case_states(run, verdict):
    assert _status(_check(), run) == verdict, [rendered for rendered, _ in run]


@pytest.mark.parametrize("run", CHAINED)
def test_the_default_read_also_reaches_a_chained_probe(run):
    # A probe is missing from the rendered rows. Without the switch the
    # required check still reads the logged card's typed call (#4620).
    without = {k: v for k, v in _check().items() if k != AS_TYPED}
    assert _status(without, run) == "pass", "the required check reads the logged card's typed call"


BOTH_PROBES_IN_PROSE = (
    "Ran `gcloud compute regions describe us-central1` for the quota and "
    "`gcloud beta compute advice capacity` for Spot and Flex-Start across two zones."
)


def test_the_reply_and_a_tool_result_cannot_stand_in_for_the_probes():
    """Only a command the worker typed counts: the same sentence in the final
    reply, or in a call's result, satisfies nothing."""
    unrelated = [("gcloud config get-value project", "gcloud config get-value project")]
    assert _status(_check(), unrelated, output=BOTH_PROBES_IN_PROSE) == "fail"
    assert _status(_check(), unrelated, result=BOTH_PROBES_IN_PROSE) == "fail"
