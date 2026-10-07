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

from pathlib import Path

import pytest

from kube_agents_bench import audit_streams

BOTH = ["compliance-audit", "stockout-prevention"]
LAYOUTS = {
    "inline": "audit_streams: [compliance-audit, stockout-prevention] # two\nowner: x\n",
    "wrapped": "audit_streams:\n  [\n    compliance-audit,\n    stockout-prevention,\n  ]\nowner: x\n",
    "block": "audit_streams:\n  - compliance-audit # first\n  - stockout-prevention\nowner: x\n",
    "indentless": "audit_streams:\n- compliance-audit\n- stockout-prevention\nowner: x\n",
    "commented": "audit_streams:\n  - compliance-audit\n# the daily one\n\n  - stockout-prevention\nowner: x\n",
    "anchored": "streams: &s [compliance-audit, stockout-prevention]\naudit_streams: *s\n",
}


def _task(tmp_path: Path, case: str, body: str) -> Path:
    path = tmp_path / case / "task.yaml"
    path.parent.mkdir()
    path.write_text("id: x\n" + body, encoding="utf-8")
    return path


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_every_yaml_layout_reads_the_same_streams(tmp_path, layout):
    assert audit_streams.declared_streams(_task(tmp_path, layout, LAYOUTS[layout])) == BOTH


def test_one_line_per_task_in_order_and_none_for_an_undeclared_case(tmp_path, capsys):
    declared = _task(tmp_path, "oobe", LAYOUTS["block"])
    plain = _task(tmp_path, "plain", "owner: x\n")
    assert audit_streams.main([str(declared), str(plain)]) == 0
    assert capsys.readouterr().out.splitlines() == ["oobe compliance-audit stockout-prevention", "plain"]


@pytest.mark.parametrize("value", ["compliance-audit", "[1]", "['a b']", "['../x']", "{a: b}"])
def test_a_value_the_runner_cannot_lock_on_fails_the_read(tmp_path, capsys, value):
    task = _task(tmp_path, "bad", f"audit_streams: {value}\n")
    assert audit_streams.main([str(task)]) == 1
    assert str(task) in capsys.readouterr().err

