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

"""Unit tests for eval run cancellation marker detection and scoring guards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kube_agents_bench import cancel


def test_cancel_marker_path_from_explicit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "custom-marker"
    monkeypatch.setenv(cancel.CANCEL_MARKER_ENV, str(target))
    assert cancel.cancel_marker_path() == target


def test_cancel_marker_path_from_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv(cancel.CANCEL_MARKER_ENV, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert cancel.cancel_marker_path() == tmp_path / cancel.DEFAULT_CANCEL_MARKER


def test_cancel_marker_path_from_eval_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv(cancel.CANCEL_MARKER_ENV, raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setenv("EVAL_STATE_DIR", str(tmp_path))
    assert cancel.cancel_marker_path() == tmp_path / cancel.DEFAULT_CANCEL_MARKER


def test_cancel_marker_path_none_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(cancel.CANCEL_MARKER_ENV, raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("EVAL_STATE_DIR", raising=False)
    assert cancel.cancel_marker_path() is None
    assert not cancel.is_cancelled()


def test_is_cancelled_respects_file_existence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    marker = tmp_path / "cancelled"
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv(cancel.CANCEL_MARKER_ENV, raising=False)

    assert not cancel.is_cancelled()
    marker.touch()
    assert cancel.is_cancelled()
    marker.unlink()
    assert not cancel.is_cancelled()


def test_wrap_evaluator_applies_blocks_when_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    marker = tmp_path / "cancelled"
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv(cancel.CANCEL_MARKER_ENV, raising=False)
    cancel.reset_cancel_log_state()

    class FakeEvaluator:
        called = False

        def applies(self, ctx: Any) -> bool:
            self.called = True
            return True

    cancel.wrap_evaluator_applies(FakeEvaluator)

    instance = FakeEvaluator()
    assert instance.applies(None)
    assert instance.called

    marker.touch()
    cancel.reset_cancel_log_state()
    instance.called = False
    assert not instance.applies(None)
    assert not instance.called
    assert "run cancelled: devops-bench judge and verification scores are off" in capsys.readouterr().err

    # Second call does not print log again
    assert not instance.applies(None)
    assert capsys.readouterr().err == ""
