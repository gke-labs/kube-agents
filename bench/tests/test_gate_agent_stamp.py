"""`bench-gate case` stamps the case record with the gateway's model setup.

The stamp (`agent`: provider, model, reasoning effort) comes from the
environment the deploy used (`MODEL_PROVIDER`, `EVAL_REASONING_EFFORT`) and
from the run's own manifest for the model, never from a new flag: a pull
request is graded with main's bench. gke-labs/kube-agents#3332.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FIXTURE_RUNS, GREEN_RUNS
from kube_agents_bench.gate import (
    AGENT_PROVIDER_ENV,
    AGENT_STAMP_KEY,
    REASONING_EFFORT_ENV,
    agent_stamp,
    main,
)

#: What the fixture run's manifest.json names; the stamp must repeat it.
FIXTURE_MODEL = "gemini-3.1-pro-preview"
PROVIDER = "openai_compatible"
JUDGE = "gemini-3.1-pro-preview"


def _record(model: str | None) -> SimpleNamespace:
    return SimpleNamespace(agent_model=model)


class TestAgentStamp:
    def test_provider_model_and_effort(self) -> None:
        env = {AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: "low"}
        assert agent_stamp([_record("m")], env) == {"provider": PROVIDER, "model": "m", "reasoning_effort": "low"}

    def test_an_empty_effort_leaves_the_field_out(self) -> None:
        for effort in ("", "  "):
            env = {AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: effort}
            assert agent_stamp([_record("m")], env) == {"provider": PROVIDER, "model": "m"}
        assert agent_stamp([_record("m")], {AGENT_PROVIDER_ENV: PROVIDER}) == {"provider": PROVIDER, "model": "m"}

    def test_the_model_is_the_first_record_that_names_one(self) -> None:
        env = {AGENT_PROVIDER_ENV: PROVIDER}
        assert agent_stamp([None, _record(None), _record("second"), _record("third")], env)["model"] == "second"

    def test_no_provider_or_no_model_means_no_stamp(self) -> None:
        assert agent_stamp([_record("m")], {}) is None
        assert agent_stamp([_record("m")], {AGENT_PROVIDER_ENV: " "}) is None
        assert agent_stamp([None, _record(None)], {AGENT_PROVIDER_ENV: PROVIDER}) is None
        assert agent_stamp([], {AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: "high"}) is None

    @pytest.mark.parametrize("effort", ["xhigh", "max", "Low", "low medium", "default"])
    def test_an_unknown_effort_is_refused(self, effort: str) -> None:
        with pytest.raises(ValueError, match=REASONING_EFFORT_ENV):
            agent_stamp([_record("m")], {AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: effort})


@pytest.fixture
def case_run(tmp_path: Path, monkeypatch, kanban_task: Path):
    """Runs `bench-gate case` over one green fixture repetition and returns the document."""
    shipped = tmp_path / "baselines"
    shipped.mkdir()
    (shipped / "VERSIONS.json").write_text(json.dumps({"fleet": 1, "verifiers": 1}), encoding="utf-8")
    for name in ("BOOTSTRAP_ADMITTED", "EVAL_BASELINE_STORE", "PULL_NUMBER", AGENT_PROVIDER_ENV, REASONING_EFFORT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)

    def run(env: dict[str, str]) -> tuple[int, dict | None]:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        out = tmp_path / "case.json"
        out.unlink(missing_ok=True)
        rc = main([
            "case", "--task", str(kanban_task), "--baseline-dir", str(shipped),
            "--result", str(FIXTURE_RUNS / GREEN_RUNS[0]), "--json-out", str(out),
        ])
        return rc, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None)

    return run


class TestCaseDocument:
    def test_the_stamp_is_written_from_the_environment_and_the_manifest(self, case_run) -> None:
        rc, doc = case_run({AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: "high"})
        assert rc == 0
        assert doc[AGENT_STAMP_KEY] == {"provider": PROVIDER, "model": FIXTURE_MODEL, "reasoning_effort": "high"}
        assert doc["version_key"]["setup_id"].startswith("gemini-3-1-pro-preview")

    def test_the_model_default_run_carries_no_effort_field(self, case_run) -> None:
        rc, doc = case_run({AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: ""})
        assert rc == 0
        assert doc[AGENT_STAMP_KEY] == {"provider": PROVIDER, "model": FIXTURE_MODEL}

    def test_without_a_provider_the_document_has_no_stamp(self, case_run) -> None:
        rc, doc = case_run({})
        assert rc == 0
        assert AGENT_STAMP_KEY not in doc

    def test_a_bad_effort_is_exit_2_and_no_document(self, case_run, capsys) -> None:
        rc, doc = case_run({AGENT_PROVIDER_ENV: PROVIDER, REASONING_EFFORT_ENV: "xhigh"})
        assert rc == 2
        assert doc is None
        assert REASONING_EFFORT_ENV in capsys.readouterr().err
