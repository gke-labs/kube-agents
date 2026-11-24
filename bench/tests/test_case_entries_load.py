"""`make bench-case-check` loads each check entry with the harness's own parser (#6375).

The harness refuses an entry whose check has a field that its verifier does not
have, records it in `verification_parse_errors`, and goes on with the other
entries. So the case file's own check has to refuse that entry first. The rule
needs the parser, which only this job installs; these tests are its gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ENTRY = "names-the-thing"
CASE_ID = "made-up-case"
# A case for a path outside bench/tasks, as scripts/test_task_registration.py builds one.
CASE = {"id": CASE_ID, "name": "A case", "domain": "security", "fixtures": ["rbac-overgrant"]}


@pytest.fixture(scope="module")
def validator():
    spec = importlib.util.spec_from_file_location(
        "validate_bench_cases_for_entry_load_tests", REPO / "scripts" / "validate_bench_cases.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case_file(tmp_path: Path, check: dict | str) -> Path:
    """The case with one entry. `check` as text goes into the file as it is, for a value that
    `yaml.safe_dump` would quote."""
    path = tmp_path / CASE_ID / "task.yaml"
    path.parent.mkdir(exist_ok=True)
    text = yaml.safe_dump({**CASE, "verification_spec": [{"name": ENTRY, "role": "objective", "check": check}]})
    if isinstance(check, str):
        text = yaml.safe_dump(CASE) + f"verification_spec:\n  - name: {ENTRY}\n    role: objective\n    check: {check}\n"
    path.write_text(text, encoding="utf-8")
    return path


def _from_the_parser(validator, problems: list[str]) -> list[str]:
    marks = (validator.UNLOADABLE_ENTRY, validator.PARSER_RAISED, validator.HARNESS_CANNOT_READ)
    return [p for p in problems if any(mark in p for mark in marks)]


def test_the_parser_is_importable_in_this_job(validator):
    """Without it, or with another checkout's, the rule does not run, and every test below would
    pass on nothing."""
    assert not isinstance(validator.spec_parser(), str)


def test_a_field_that_the_verifier_does_not_have_is_rejected(validator, tmp_path):
    check = {"type": "report_contains", "required_phrases": ["x"], "no_such_field": ["y"]}
    problems = validator.validate_paths([_case_file(tmp_path, check)])[CASE_ID]
    (problem,) = _from_the_parser(validator, problems)
    assert problem.startswith(f"check {ENTRY!r}: {validator.UNLOADABLE_ENTRY}: ")
    assert "no_such_field" in problem


def test_the_same_entry_without_the_field_passes(validator, tmp_path):
    check = {"type": "report_contains", "required_phrases": ["x"]}
    problems = validator.validate_paths([_case_file(tmp_path, check)])[CASE_ID]
    assert _from_the_parser(validator, problems) == []


def test_a_missing_required_field_is_rejected(validator, tmp_path):
    """`ledger_issue_contains` needs `audit`; no rule of the validator's own knows that."""
    check = {"type": "ledger_issue_contains", "required_phrases": ["x"]}
    problems = validator.validate_paths([_case_file(tmp_path, check)])[CASE_ID]
    (problem,) = _from_the_parser(validator, problems)
    assert f"check {ENTRY!r}" in problem


ROLE_CHECK = {"type": "fleet_resource_property", "kind": "deployment", "resource_name": "x", "namespace": "y", "path": "spec.replicas", "op": "eq", "value": 1}


@pytest.mark.parametrize(
    "check",
    [
        # A rule inside the validator's entry loop faults it.
        {"type": "no_such_type"},
        # Rules outside that loop fault these two; the parser refuses both values as well.
        {**ROLE_CHECK, "fixture_role": "{{FLEET_CLUSTER_NAME:rbac-overgrant}}"},
        {**ROLE_CHECK, "fixture_role": "Rbac-Overgrant"},
    ],
)
def test_a_defect_that_another_rule_reports_is_not_reported_a_second_time(validator, tmp_path, check):
    problems = validator.validate_paths([_case_file(tmp_path, check)])[CASE_ID]
    assert problems
    assert _from_the_parser(validator, problems) == []


def test_a_value_that_yaml_1_1_reads_as_a_boolean_is_not_refused(validator, tmp_path):
    """PyYAML reads the bare `no` as False; the harness's reader, and so a run, reads the string."""
    problems = validator.validate_paths([_case_file(tmp_path, '{type: report_contains, required_phrases: [no, "x"]}')])[CASE_ID]
    assert _from_the_parser(validator, problems) == []


def test_a_parser_that_raises_is_a_problem_of_its_entry(validator, tmp_path, capsys):
    """A pattern that does not compile raises inside the parser. The command must name the case
    and the entry, not stop with a traceback that hides every other case."""
    check = {"type": "report_contains", "required_phrases": ["x"], "forbidden_patterns": ["kubectl apply ("]}
    path = _case_file(tmp_path, check)
    (problem,) = _from_the_parser(validator, validator.validate_paths([path])[CASE_ID])
    assert problem.startswith(f"check {ENTRY!r}: ")
    assert validator.main([str(path)]) == 1
    assert f"{CASE_ID}:" in capsys.readouterr().out


def test_the_command_fails_and_names_the_case_and_the_entry(validator, tmp_path, capsys):
    """The repetition of #6375: `ledger_issue_contains` has no `required_patterns`."""
    check = {"type": "ledger_issue_contains", "audit": "fleet-wide-cost-analysis", "required_phrases": ["x"], "required_patterns": ["x"]}
    assert validator.main([str(_case_file(tmp_path, check))]) == 1
    out, err = capsys.readouterr()
    # The parser is here, so the command does not say that the rule did not run.
    assert err == ""
    assert f"{CASE_ID}:" in out
    assert f"check {ENTRY!r}" in out
    assert "required_patterns" in out


def test_no_case_of_the_tree_has_an_entry_that_does_not_load(validator):
    found = {name: _from_the_parser(validator, problems) for name, problems in validator.validate_all().items()}
    assert {name: problems for name, problems in found.items() if problems} == {}
