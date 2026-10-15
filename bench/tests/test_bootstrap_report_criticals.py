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

"""The scored-report read and the ``bootstrap_report_criticals`` verifier.

The items are what ``inventory_findings.py extract`` writes from the raw report
the bootstrap-ranking stack plants, and the scores are the vectors the
prioritization SOP's rubric gives those findings. The read command runs under
``sh`` here with the repository's scripts as the sandbox's, so the severities
come from the same scorer the sandbox runs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import onboarding
from kube_agents_bench.verifiers import BootstrapReportCriticalsVerifier

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "bench" / "tf" / "prebuilt" / "bootstrap-ranking" / "inventory-raw.txt"
SCRIPTS = REPO / "agents" / "platform" / "scripts"
FINDINGS = SCRIPTS / "inventory_findings.py"
TASK = REPO / "bench" / "tasks" / "bootstrap-inventory-ranking-delivery" / "task.yaml"

# The rubric the SOP's tables give each planted finding (Step 3). The four
# criticals: a project-editor key reachable from a pod (the SOP's own worked
# example, 288), cluster-admin held by a ServiceAccount whose token a running
# pod mounts (B=8 for what it grants, L=6 for a long-lived broad credential
# reachable from a pod, 288), and two serving Deployments with no ready
# replica (L=10 with B=5, critical by the floor whatever they score).
VECTORS = {
    "invoice-worker": {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0},
    "release-bot": {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 0.9},
    "quote-api": {"B": 5, "L": 10, "detect": 1, "recover": 2, "C": 1.0},
    "pricing-engine": {"B": 5, "L": 10, "detect": 1, "recover": 2, "C": 1.0},
    "gift-cards": {"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 0.9},
    "orders-db": {"B": 3, "L": 2, "detect": 2, "recover": 3, "C": 0.9},
    "prod-central": {"B": 1, "L": 1, "detect": 2, "recover": 1, "C": 1.0},
}
CRITICALS = ("invoice-worker", "release-bot", "quote-api", "pricing-engine")
# A rubric that scores minor, for a test that demotes a planted critical.
MINOR = {"B": 1, "L": 1, "detect": 1, "recover": 1, "C": 1.0}


def _objective() -> dict[str, Any]:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    return next(e for e in spec if e["name"] == "report-lists-two-criticals")["check"]


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _score(rubric: dict[str, Any]) -> dict[str, Any]:
    return {
        "rubric": rubric,
        "recommendation": {"action": "fix it", "rationale": "it is broken", "risk": "an outage"},
        "remediation": {"kind": "manual", "note": "by hand"},
        "verification": {"kind": "manual", "still_failing_when": "it is still broken"},
        "actionable": True,
    }


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sandbox's data volume, with the planted batch extracted and scored, read locally."""
    for name, attr in (
        ("INVENTORY.items.json", "ITEMS_FILE"),
        ("INVENTORY.scores.json", "SCORES_FILE"),
        ("INVENTORY.md", "REPORT_FILE"),
        ("INVENTORY.delivered.md", "DELIVERED_FILE"),
    ):
        monkeypatch.setattr(onboarding, attr, str(tmp_path / name))
    monkeypatch.setattr(onboarding, "PARSER_DIR", str(SCRIPTS))
    monkeypatch.setattr(onboarding, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    subprocess.run(
        [sys.executable, str(FINDINGS), "extract", "--raw", str(RAW), "--out", str(tmp_path / "INVENTORY.items.json")],
        check=True,
        capture_output=True,
    )
    _write_scores(tmp_path)
    return tmp_path


def _items(sandbox: Path) -> list[dict[str, Any]]:
    return json.loads((sandbox / "INVENTORY.items.json").read_text())["items"]


def _write_scores(sandbox: Path, **overrides: dict[str, Any]) -> None:
    scores = {i["id"]: _score(overrides.get(i["object"], VECTORS[i["object"]])) for i in _items(sandbox)}
    (sandbox / "INVENTORY.scores.json").write_text(json.dumps({"scores": scores}))


def _report(*listed: str, rollup: str = "") -> str:
    lines = ["# GKE Environment Scan", "", "One cluster, six workloads.", ""]
    for n, name in enumerate(listed, 1):
        lines += [f"{n}. **Critical: `{name}` in `prod-central` is broken**", "   Left alone it stays broken; fix it.", ""]
    if rollup:
        lines += [rollup, ""]
    lines.append("Ask for the full inventory any time.")
    return "\n".join(lines) + "\n"


def _verify(timeout: float = 0.0, limit: int = 2):
    return BootstrapReportCriticalsVerifier(type="bootstrap_report_criticals", limit=limit).verify(timeout)


# --- the case and its fixture ---------------------------------------------


def _scorer():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import inventory_findings
    finally:
        sys.path.remove(str(SCRIPTS))
    return inventory_findings


def test_the_planted_report_carries_four_criticals_on_four_checks(sandbox: Path) -> None:
    scorer = _scorer()
    scores = json.loads((sandbox / "INVENTORY.scores.json").read_text())["scores"]
    rows = [scorer.fq.validate_finding(p) for p in scorer.build_payloads(_items(sandbox), scores)]
    critical = [(r["check_slug"], r["object"]) for r in rows if r["severity"] == "critical"]
    assert sorted(o for _, o in critical) == sorted(CRITICALS)
    assert len({c for c, _ in critical}) == len(CRITICALS)


def test_the_case_grades_the_default_limit() -> None:
    assert _objective()["limit"] == _scorer().fq.DEFAULT_FIRST_REPORT_CRITICALS


def test_the_reader_spells_the_scores_file_as_the_script_does() -> None:
    assert f'DEFAULT_SCORES_PATH = "{onboarding.SCORES_FILE}"' in FINDINGS.read_text()


# --- the verdict ----------------------------------------------------------


def test_a_report_that_lists_what_select_chose_passes(sandbox: Path) -> None:
    out = subprocess.run(
        [
            sys.executable, str(FINDINGS), "select",
            "--items", str(sandbox / "INVENTORY.items.json"),
            "--scores", str(sandbox / "INVENTORY.scores.json"),
            "--limits", str(sandbox / "no-limits.json"),
            "--out", str(sandbox / "INVENTORY.shown.json"),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    chosen = re.findall(r"^\s+f\d{3}\s+critical\s+\S+/(\S+)", out, re.M)
    assert len(chosen) == 2
    (sandbox / "INVENTORY.delivered.md").write_text(
        _report(*chosen, rollup="Also found: 5 more items, 2 of them critical, tracked in the findings queue.")
    )
    result = _verify()
    assert result.status == "pass", result.reason
    assert "name 2 of the 4 item(s) the worker scored critical" in result.reason


def test_a_report_that_lists_every_critical_fails(sandbox: Path) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(_report(*CRITICALS, "gift-cards"))
    result = _verify()
    assert result.status == "fail"
    assert "5 numbered item(s) name 4 of the 4 item(s) the worker scored critical; expected exactly 2" in result.reason


def test_two_criticals_padded_with_non_criticals_fail(sandbox: Path) -> None:
    # The shape the old SOP produced, with only the critical cap applied.
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "release-bot", "gift-cards", "orders-db"))
    result = _verify()
    assert result.status == "fail"
    assert "name 2 of the 4 item(s) the worker scored critical; they also name 2 non-critical finding(s)" in result.reason
    assert result.raw["padded"] == ["gift-cards", "orders-db"]


def test_a_critical_item_naming_its_cluster_is_not_padding(sandbox: Path) -> None:
    # Every item says `in prod-central`, the object of the cluster-level Managed Prometheus finding.
    report = _report("invoice-worker", "release-bot")
    assert report.count("`prod-central`") == 2
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    result = _verify()
    assert result.status == "pass", result.reason
    assert result.raw["padded"] == []


def test_a_deferred_critical_named_outside_the_list_does_not_count(sandbox: Path) -> None:
    rollup = "Also found: `quote-api` and `pricing-engine` are down too, and 3 more items."
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "release-bot", rollup=rollup))
    assert _verify().status == "pass"


def test_a_name_inside_a_longer_name_does_not_count(sandbox: Path) -> None:
    report = _report("invoice-worker", "release-bot").replace(
        "Left alone it stays broken", "Unlike quote-api-v2 and xpricing-engine, left alone it stays broken", 1
    )
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    assert _verify().status == "pass"


def test_a_name_on_an_indented_line_of_an_item_counts(sandbox: Path) -> None:
    report = _report("invoice-worker", "release-bot").replace(
        "   Left alone it stays broken", "   It also takes `quote-api` down. Left alone it stays broken", 1
    )
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    result = _verify()
    assert result.status == "fail"
    assert "name 3 of the 4" in result.reason


def test_too_few_listed_fails(sandbox: Path) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "gift-cards"))
    result = _verify()
    assert result.status == "fail"
    assert "name 1 of the 4" in result.reason


def test_the_undelivered_report_is_read_when_there_is_no_delivered_one(sandbox: Path) -> None:
    (sandbox / "INVENTORY.md").write_text(_report(*CRITICALS))
    result = _verify()
    assert result.status == "fail"
    assert result.raw["report"] == "written"


def test_no_report_fails(sandbox: Path) -> None:
    result = _verify()
    assert result.status == "fail"
    assert "the worker wrote no report" in result.reason


def test_two_or_fewer_scored_critical_is_an_error_not_a_pass(sandbox: Path) -> None:
    _write_scores(sandbox, **{"quote-api": MINOR, "pricing-engine": MINOR})
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "release-bot"))
    result = _verify()
    assert result.status == "error"
    assert "scored 2 item(s) critical, not more than the limit of 2" in result.reason


def test_rows_on_one_check_are_one_critical_item(sandbox: Path) -> None:
    items = json.loads((sandbox / "INVENTORY.items.json").read_text())
    for item in items["items"]:
        if item["object"] == "pricing-engine":
            item["check"] = "crashloop-backoff"
    (sandbox / "INVENTORY.items.json").write_text(json.dumps(items))
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "quote-api", "pricing-engine"))
    result = _verify()
    assert result.status == "pass", result.reason
    assert "crashloop-backoff (pricing-engine, quote-api)" in result.reason
    assert "name 2 of the 3" in result.reason


def test_rows_are_gathered_by_the_scorers_item_key(sandbox: Path) -> None:
    # Two spellings of one check are one line to `select`, so one item here.
    items = json.loads((sandbox / "INVENTORY.items.json").read_text())
    for item in items["items"]:
        if item["object"] == "pricing-engine":
            item["check"] = "Crashloop_Backoff"
    (sandbox / "INVENTORY.items.json").write_text(json.dumps(items))
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "quote-api", "pricing-engine"))
    result = _verify()
    assert result.status == "pass", result.reason
    assert "crashloop-backoff (pricing-engine, quote-api)" in result.reason
    assert "name 2 of the 3" in result.reason


def test_a_provider_managed_observation_is_not_an_item(sandbox: Path) -> None:
    scores = json.loads((sandbox / "INVENTORY.scores.json").read_text())
    for item in _items(sandbox):
        if item["object"] in ("quote-api", "pricing-engine"):
            scores["scores"][item["id"]].update(provider_managed=True, actionable=False)
    (sandbox / "INVENTORY.scores.json").write_text(json.dumps(scores))
    assert _verify().status == "error"


def test_a_batch_that_cannot_be_scored_is_an_error(sandbox: Path) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(_report("invoice-worker", "release-bot"))
    scores = json.loads((sandbox / "INVENTORY.scores.json").read_text())
    scores["scores"].pop(next(iter(scores["scores"])))
    (sandbox / "INVENTORY.scores.json").write_text(json.dumps(scores))
    result = _verify()
    assert result.status == "error"
    assert "unscored: f001" in result.reason
    (sandbox / "INVENTORY.scores.json").unlink()
    result = _verify()
    assert result.status == "error"
    assert "FileNotFoundError" in result.reason


def test_a_scorer_the_sandbox_cannot_import_is_an_error(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "PARSER_DIR", str(sandbox / "nowhere"))
    monkeypatch.setattr(onboarding, "PARSER_MODULE", "no_such_module")
    result = _verify()
    assert result.status == "error"
    assert "cannot import no_such_module" in result.reason


def test_an_unreadable_sandbox_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "sandbox_shell", lambda s, t: "")
    assert _verify().status == "error"


def test_a_limit_of_zero_passes_a_report_that_lists_no_critical(sandbox: Path) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(
        _report(rollup="Also found: 7 more items, 4 of them critical, tracked in the findings queue.")
    )
    assert _verify(limit=0).status == "pass"


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_report_criticals"] == "kube_agents_bench.verifiers:BootstrapReportCriticalsVerifier"


def test_parse_node_builds_the_case_objective() -> None:
    node = parse_node(_objective())
    assert isinstance(node, BootstrapReportCriticalsVerifier)
    assert node.limit == 2
    assert VERIFIERS.get("bootstrap_report_criticals") is BootstrapReportCriticalsVerifier


@pytest.mark.parametrize("spec", [{}, {"limit": -1}, {"limit": 2, "extra": 1}])
def test_a_missing_or_malformed_limit_is_rejected_at_load(spec: dict[str, Any]) -> None:
    with pytest.raises(Exception):
        parse_node({"type": "bootstrap_report_criticals", **spec})
