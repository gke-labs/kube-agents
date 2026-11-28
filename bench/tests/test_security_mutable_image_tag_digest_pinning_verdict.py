"""security-mutable-image-tag-digest-pinning reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "security-mutable-image-tag-digest-pinning" / "task.yaml"
CHECK = "mutable-tag-and-digest-pinning-verdict-stated"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    assert check["check"]["type"] == "all"
    transcript.set(report, [], final_message=report)
    return all(
        ReportContainsVerifier(**part).verify(TIMEOUT_SEC).success
        for part in check["check"]["checks"]
    )


def test_structured_verdict_lines_pass():
    for report in (
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n\nShort explanation follows.",
        "**SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE**\n**PINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST**\n",
        "`SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE`\n`PINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST`\n",
        "1. SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\n2. PINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n",
        "> SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\n> PINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE.\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST.\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n\nSEMVER-TAG-IMMUTABLE: YES-SEMVER-GUARANTEES-IMMUTABILITY is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "SEMVER-TAG-IMMUTABLE: YES-SEMVER-GUARANTEES-IMMUTABILITY\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-ONLY-LATEST-TO-SEMVER\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: KEEP-CURRENT-TAGS-WITH-PULL-ALWAYS\n",
        "PINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\nSEMVER-TAG-IMMUTABLE: YES-SEMVER-GUARANTEES-IMMUTABILITY\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n1. **SEMVER-TAG-IMMUTABLE: YES-SEMVER-GUARANTEES-IMMUTABILITY**\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\nPINNING-REQUIREMENT: PIN-ONLY-LATEST-TO-SEMVER\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n2. **PINNING-REQUIREMENT: PIN-ONLY-LATEST-TO-SEMVER**\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE?\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST\n",
        "SEMVER-TAG-IMMUTABLE: NO-TAGS-ARE-MUTABLE\nPINNING-REQUIREMENT: PIN-BOTH-CONTAINERS-BY-SHA256-DIGEST (maybe not)\n",
        "Both semver tags and latest are mutable in container registries; pin both api and proxy by sha256 digest.",
    ):
        assert not _passes(report), report


def test_verbatim_prompt_echo_fails():
    doc = yaml.safe_load(TASK.read_text())
    assert not _passes(doc["prompt"])
