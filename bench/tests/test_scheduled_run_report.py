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

"""The scheduled-run-report plant (#6110).

The stack's in-gateway script (``bench/tf/prebuilt/scheduled-run-report/scheduled_report.py``)
runs here against the real ``audit_report`` and ``report_status`` modules, with
the broker store's root in a temporary directory. The tests show what a case
rests on: the seed leaves a marked run as the stream's ``latest`` under the
broker store's root and nowhere else; it refuses a container that is not the
one holding that root; and teardown removes exactly the plant, puts back a
``latest`` it moved aside, and does so after a seed that failed part way.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "scheduled-run-report"
AUDIT = "compliance-audit"
GITOPS = "acme/fleet"
MARK = "eval-sched-run"
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)

sys.path[:0] = [
    str(REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"),
    str(REPO / "agents" / "platform" / "scripts"),
]
import audit_report  # noqa: E402
import report_status  # noqa: E402

_spec = importlib.util.spec_from_file_location("scheduled_run_report", STACK / "scheduled_report.py")
plant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plant)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The broker store's root, in a temporary directory, as this container's file store."""
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setenv("FLEET_AUDIT_REPORTS_DIR", str(root))
    monkeypatch.delenv(report_status.REPORT_STORE_ENV, raising=False)
    monkeypatch.setattr(plant, "BROKER_STORE_ROOT", str(root))
    return root


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under `root`, with a file's bytes (None for a directory)."""
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))
    }


def stream() -> Path:
    return audit_report.reports_dir_for(AUDIT, GITOPS)


def real_stream():
    """A stream with one real run, as an install that has run the audit holds it."""
    directory = stream()
    (directory / "runs").mkdir(parents=True)
    (directory / "runs" / "20261120T090000.000000Z.json").write_text('{"status": "CLEAN"}')
    (directory / "latest.json").write_text('{"status": "CLEAN"}')


def test_the_seed_leaves_the_marked_run_as_latest_under_the_root(store):
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    latest = report_status.read_json(str(stream() / "latest.json"))
    assert latest["status"] == plant.PLANT_STATUS
    assert latest["scheduled"] is True
    (finding,) = latest["document"]["findings"]
    assert finding["severity"] == "critical"
    assert MARK in finding["cluster"] and MARK in finding["namespace"]
    # The ring entry is the same run, an hour before the turn.
    (ring,) = (stream() / "runs").glob("*.json")
    assert ring.name == (NOW - plant.REPORT_AGE).strftime(audit_report.REPORT_STAMP_FORMAT) + ".json"
    assert MARK in ring.read_text()
    # Nothing outside the store's root.
    assert all(plant.under_root(store / name) for name in snapshot(store))


def test_teardown_removes_exactly_the_plant(store):
    before = snapshot(store)
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(store) != before
    plant.teardown(audit_report, AUDIT, MARK)
    assert snapshot(store) == before


def test_teardown_puts_back_a_latest_the_seed_moved_aside(store):
    real_stream()
    before = snapshot(store)
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert MARK in (stream() / "latest.json").read_text()
    plant.teardown(audit_report, AUDIT, MARK)
    assert snapshot(store) == before


def test_a_seed_that_fails_part_way_leaves_teardown_what_it_needs(store, monkeypatch):
    real_stream()
    before = snapshot(store)

    def boom(*args, **kwargs):
        raise RuntimeError("the report write failed")

    monkeypatch.setattr(audit_report, "write_report", boom)
    with pytest.raises(RuntimeError):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    # The real latest is aside and the note names it: teardown puts it back.
    assert not (stream() / "latest.json").exists()
    plant.teardown(audit_report, AUDIT, MARK)
    assert snapshot(store) == before


def test_a_seed_that_fails_after_the_ring_entry_is_undone(store, monkeypatch):
    real_stream()
    before = snapshot(store)
    real_write = audit_report.write_report

    def ring_then_boom(audit_id, envelope, now):
        real_write(audit_id, envelope, now)
        (stream() / "latest.json").unlink()
        raise RuntimeError("the latest write failed")

    monkeypatch.setattr(audit_report, "write_report", ring_then_boom)
    with pytest.raises(RuntimeError):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    plant.teardown(audit_report, AUDIT, MARK)
    assert snapshot(store) == before


def test_a_seed_that_fails_before_any_write_moves_nothing(store, monkeypatch):
    real_stream()
    before = snapshot(store)

    def boom(*args, **kwargs):
        raise RuntimeError("the document did not build")

    monkeypatch.setattr(plant, "planted_document", boom)
    with pytest.raises(RuntimeError):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(store) == before


def test_a_second_seed_without_teardown_refuses_and_keeps_the_real_latest(store):
    real_stream()
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    with pytest.raises(SystemExit):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    plant.teardown(audit_report, AUDIT, MARK)
    assert json.loads((stream() / "latest.json").read_text()) == {"status": "CLEAN"}


def test_the_seed_refuses_a_ring_with_no_slot_to_spare(store):
    # One slot short of full: the plant plus a real run landing during the
    # case would push a real run out.
    runs = stream() / "runs"
    runs.mkdir(parents=True)
    for day in range(audit_report.REPORT_HISTORY - 1):
        (runs / f"202611{day + 1:02d}T090000.000000Z.json").write_text('{"status": "CLEAN"}')
    before = snapshot(store)
    with pytest.raises(SystemExit):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(store) == before


def test_the_seed_refuses_a_container_that_reads_the_broker_store(store, monkeypatch):
    monkeypatch.setattr(audit_report, "_report_store_client", lambda: object())
    before = snapshot(store)
    with pytest.raises(SystemExit, match="reads the broker report store"):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(store) == before


def test_the_seed_refuses_a_store_root_that_is_not_the_brokers(store, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FLEET_AUDIT_REPORTS_DIR", str(elsewhere))
    with pytest.raises(SystemExit, match="not the broker store's root"):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(elsewhere) == {} and snapshot(store) == {}


def test_teardown_with_no_note_removes_nothing(store):
    real_stream()
    before = snapshot(store)
    plant.teardown(audit_report, AUDIT, MARK)
    assert snapshot(store) == before


def test_teardown_ignores_a_note_naming_paths_outside_the_stream(store, tmp_path):
    real_stream()
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    other = audit_report.reports_dir_for("fleet-waste-audit", GITOPS)
    other.mkdir(parents=True)
    (other / "latest.json").write_text("keep")
    empty = tmp_path / "empty-outside"
    empty.mkdir()
    note = {
        "mark": MARK,
        "ring": str(outside),
        "latest": str(other / "latest.json"),
        "backup": str(outside),
        "created": [str(empty), str(store)],
    }
    (stream() / plant.PLANT_NOTE).write_text(json.dumps(note))
    plant.teardown(audit_report, AUDIT, MARK)
    assert outside.read_text() == "keep"
    assert (other / "latest.json").read_text() == "keep"
    assert empty.is_dir() and store.is_dir()
    assert json.loads((stream() / "latest.json").read_text()) == {"status": "CLEAN"}


@pytest.mark.parametrize("mark", ["", "Eval", "a", "-eval", "eval-", "eval_run", "x" * 33, "../latest"])
def test_a_mark_that_is_no_short_label_is_refused(mark):
    with pytest.raises(SystemExit, match="DNS-1123"):
        plant.main(["scheduled_report.py", plant.SEED_MODE, AUDIT, mark, GITOPS])


def test_the_planted_names_fit_a_label():
    longest = "x" * 32
    for value in plant.names(longest).values():
        assert len(value) <= 63 and re.fullmatch(r"[a-z0-9-]+", value)


# --- main.tf -------------------------------------------------------------------


def main_tf() -> str:
    return (STACK / "main.tf").read_text(encoding="utf-8")


def test_a_failed_seed_in_the_provisioner_runs_teardown():
    text = main_tf()
    seed_at = text.index("- seed ")
    fallback = text[seed_at : text.index("EOT", seed_at)]
    assert "|| {" in fallback and "- teardown " in fallback and 'exit "$status"' in fallback


def test_the_stack_runs_both_modes_with_the_mark():
    text = main_tf()
    assert f'- {plant.SEED_MODE} "${{var.audit_id}}" "${{var.mark}}"' in text
    assert f'- {plant.TEARDOWN_MODE} "${{self.triggers.audit_id}}" "${{self.triggers.mark}}"' in text


def test_the_stack_uses_only_get_and_exec():
    # The same access the unchanged-finding-wording stack's broker teardown
    # uses in the gateway container: no write verb, no Secret.
    verbs = set(re.findall(r"\bkubectl\s+(\w+)", main_tf()))
    assert verbs == {"get", "exec"}, verbs
    assert "secret" not in main_tf().lower()


def test_the_stack_declares_the_mark_with_the_scripts_rule():
    variables = (STACK / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "mark"' in variables
    assert plant.MARK_RE.pattern in variables


def test_the_script_writes_under_the_listeners_root():
    store_module = REPO / "agents" / "platform" / "scripts" / "audit_report_store.py"
    assert f'DEFAULT_REPORTS_DIR = "{plant.BROKER_STORE_ROOT}"' in store_module.read_text(encoding="utf-8")
    assert os.path.normpath(plant.BROKER_STORE_ROOT) == plant.BROKER_STORE_ROOT


def test_the_seed_refuses_a_ring_with_a_run_newer_than_the_plant(store):
    runs = stream() / "runs"
    runs.mkdir(parents=True)
    newer = (NOW - plant.REPORT_AGE / 2).strftime(audit_report.REPORT_STAMP_FORMAT)
    (runs / f"{newer}.json").write_text('{"status": "CLEAN"}')
    before = snapshot(store)
    with pytest.raises(SystemExit, match="would not be the newest"):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(store) == before


def test_teardown_finds_the_plant_without_resolving_the_repository(store, monkeypatch):
    real_stream()
    before = snapshot(store)
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)

    def no_forge(*args, **kwargs):
        raise AssertionError("teardown resolved the repository")

    monkeypatch.setattr(audit_report, "resolve_reachable_repo", no_forge)
    monkeypatch.setattr(plant, "publish_repo", no_forge)
    plant.main(["scheduled_report.py", plant.TEARDOWN_MODE, AUDIT, MARK])
    assert snapshot(store) == before


def test_teardown_leaves_another_marks_plant(store):
    real_stream()
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    planted = snapshot(store)
    assert plant.teardown(audit_report, AUDIT, "another-mark") == []
    assert snapshot(store) == planted


def test_teardown_keeps_a_newer_real_latest_that_replaced_the_plant(store):
    real_stream()
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    # A real run lands during the case and replaces the plant as latest.
    (stream() / "latest.json").write_text('{"status": "OPENED", "real": true}')
    plant.teardown(audit_report, AUDIT, MARK)
    assert json.loads((stream() / "latest.json").read_text()) == {"status": "OPENED", "real": True}
    assert not (stream() / plant.LATEST_BACKUP).exists()
    assert not (stream() / plant.PLANT_NOTE).exists()
    assert [p.name for p in (stream() / "runs").glob("*.json")] == ["20261120T090000.000000Z.json"]


def test_a_seed_that_dies_moving_latest_aside_is_undone(store, monkeypatch):
    real_stream()
    before = snapshot(store)
    real_replace = os.replace

    def replace_then_die(src, dst):
        real_replace(src, dst)
        raise KeyboardInterrupt("killed after the move")

    monkeypatch.setattr(plant.os, "replace", replace_then_die)
    with pytest.raises(KeyboardInterrupt):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    monkeypatch.setattr(plant.os, "replace", real_replace)
    plant.teardown(audit_report, AUDIT, MARK)
    assert snapshot(store) == before


def test_a_note_that_cannot_be_written_leaves_no_directories(store, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("read-only store")

    monkeypatch.setattr(plant, "write_note", boom)
    with pytest.raises(OSError):
        plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    assert snapshot(store) == {}


def test_the_seed_never_records_the_root_for_removal(store):
    plant.seed(audit_report, AUDIT, MARK, GITOPS, NOW)
    note = json.loads((stream() / plant.PLANT_NOTE).read_text())
    assert str(store) not in note["created"]
    assert note["mark"] == MARK and note["latest_sha256"]
    plant.teardown(audit_report, AUDIT, MARK)
    assert store.is_dir() and snapshot(store) == {}


def test_apply_and_destroy_take_one_running_gateway_and_skip_a_terminating_pod():
    text = main_tf()
    apply_at = text.index("- seed ")
    destroy_at = text.index("when        = destroy")
    for block in (text[:apply_at], text[destroy_at:]):
        assert "more than one running agent pod" in block
        assert "{.metadata.deletionTimestamp}" in block
        assert '[ -z "$deleting" ] || continue' in block


def test_teardown_is_called_without_the_repository():
    # Teardown finds its notes by mark, so a forge hiccup at destroy time can't
    # send it to another store directory.
    for line in main_tf().splitlines():
        if "- teardown " in line:
            assert "gitops_repo" not in line, line
