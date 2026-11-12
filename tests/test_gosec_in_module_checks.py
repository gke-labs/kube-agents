"""gosec scans the a2a and hack/ghes-proxy modules in the check that tests them.

The Go modules in this tree are k8s-operator/, k8s-api-proxy/, a2a/ and
hack/ghes-proxy/. Until this test's change, the first two were scanned and the
other two never had been; a finding in either could sit on `main` with nothing
to show it. The scan now runs as a step of each module's own test workflow,
under the gate its test step runs under (a2a-test.yml filters in the step,
ghes-proxy-test.yml at the trigger), built from k8s-operator/go.mod's pin, the
one gosec this repository declares, so that go.mod and go.sum are inputs of
both workflows: a pin bump runs the scan where its new findings would land.
`make gosec` runs the same scan over every module locally. Held here: the step
leaving either workflow, a gate or `continue-on-error` of its own (a red that
fails nothing), a pin input missing from the trigger, a second pin, a Go
module `make gosec` does not reach, and an operator `-exclude=` list that
differs between the Makefile and the workflow that scans the operator.

tests/test_gosec_in_required_checks.py holds the operator and proxy halves of
the same rule from a sibling change (gke-labs/kube-agents#5382). Its
`test_gosec_runs_in_no_other_workflow` asserts no gosec step outside its two
workflows, which the two steps held here fail, and its gate test asserts every
gosec step has an `if:`, which the ghes-proxy step has not, since its trigger
gates it. Whichever of the two lands second widens that module map to these
two workflows, allows a trigger-gated row, and folds this file into that one.
"""

import re
import unittest

import yaml

from tests.test_workflow_pins_and_fork_guards import REPO_ROOT, WORKFLOWS_DIR, _load, _workflow_files

#: The scanner, as a `run:` command or as the action upstream documents.
_GOSEC_MARKERS = ("github.com/securego/gosec", "securego/gosec@")
#: The one module that pins gosec; every scan builds from it (`go -C <here>`).
_PIN_MODULE = "k8s-operator"
#: gosec's module path, as a `require` line in a go.mod names it.
_GOSEC_MODULE_PATH = "github.com/securego/gosec"
#: The files a pin bump changes, which must therefore trigger each scan.
_PIN_INPUTS = (f"{_PIN_MODULE}/go.mod", f"{_PIN_MODULE}/go.sum")
#: Go module -> (its test workflow, the step that runs its tests, where the
#: workflow gates: the `paths-filter` step's output key, or None when the
#: trigger's `paths:` lists gate it and the steps carry no `if:`).
_MODULE_CHECKS = {
    "a2a": ("a2a-test.yml", "Run Tests", "a2a"),
    "hack/ghes-proxy": ("ghes-proxy-test.yml", "Test", None),
}
_CONTINUE_ON_ERROR = "continue-on-error"
#: Directories a go.mod under them is not a module of this tree.
_FOREIGN_MODULE_DIRS = ("node_modules", ".claude", "testdata")
_MAKEFILE = REPO_ROOT / "Makefile"
#: The Makefile lines the gosec target's module list is read from.
_MAKE_TARGET = "gosec:"
_MAKE_UNEXCLUDED = re.compile(r"^GOSEC_UNEXCLUDED_MODULES\s*:=\s*(.*)$", re.MULTILINE)
_MAKE_PIN_MODULE = re.compile(r"^GOSEC_PIN_MODULE\s*:=\s*(\S+)\s*$", re.MULTILINE)
_MAKE_OPERATOR_EXCLUDE = re.compile(r"^GOSEC_OPERATOR_EXCLUDE\s*:=\s*(\S+)\s*$", re.MULTILINE)
#: A gosec invocation's exclusion list, wherever a workflow runs one.
_EXCLUDE_FLAG = re.compile(r"-exclude=(\S+)")


def _steps(doc: dict) -> list[dict]:
    jobs = (doc or {}).get("jobs") or {}
    return [step for job in jobs.values() for step in ((job or {}).get("steps") or [])]


def _is_gosec(step: dict) -> bool:
    text = f"{step.get('run', '')} {step.get('uses', '')}"
    return any(marker in text for marker in _GOSEC_MARKERS)


def _trigger_paths(doc: dict) -> dict[str, list[str]]:
    on = doc.get("on") or doc.get(True) or {}
    out = {}
    for event, spec in (on or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("paths"), list):
            out[event] = spec["paths"]
    return out


def _filter_paths(steps: list[dict], key: str) -> list[str]:
    for step in steps:
        if "paths-filter" in str(step.get("uses", "")):
            filters = yaml.safe_load((step.get("with") or {}).get("filters") or "") or {}
            return filters.get(key) or []
    return []


def _go_modules() -> set[str]:
    found = set()
    for path in REPO_ROOT.rglob("go.mod"):
        rel = path.relative_to(REPO_ROOT).parent
        if any(part in _FOREIGN_MODULE_DIRS for part in rel.parts):
            continue
        found.add(rel.as_posix())
    return found


class GosecInModuleChecksTest(unittest.TestCase):
    def test_each_module_runs_gosec_under_its_tests_gate(self) -> None:
        for module, (name, test_step, _key) in _MODULE_CHECKS.items():
            with self.subTest(module=module, workflow=name):
                _text, doc = _load(WORKFLOWS_DIR / name)
                steps = _steps(doc)
                gosec = [s for s in steps if _is_gosec(s)]
                self.assertEqual(1, len(gosec), f"{name}: expected one gosec step, found {len(gosec)}")
                tests = [s for s in steps if s.get("name") == test_step]
                self.assertEqual(1, len(tests), f"{name}: expected one step named {test_step!r}")
                self.assertEqual(
                    tests[0].get("if"),
                    gosec[0].get("if"),
                    f"{name}: the gosec step's `if:` differs from the {test_step!r} step's",
                )
                self.assertNotIn(
                    _CONTINUE_ON_ERROR,
                    gosec[0],
                    f"{name}: a gosec step with {_CONTINUE_ON_ERROR} is a red that fails nothing",
                )
                self.assertRegex(
                    str(gosec[0].get("run", "")),
                    rf"go -C (\.\./)*{re.escape(_PIN_MODULE)} build ",
                    f"{name}: the scanner is not built from {_PIN_MODULE}/go.mod's pin",
                )

    def test_a_pin_bump_runs_each_scan(self) -> None:
        for module, (name, _test_step, key) in _MODULE_CHECKS.items():
            with self.subTest(module=module, workflow=name):
                _text, doc = _load(WORKFLOWS_DIR / name)
                lists = dict(_trigger_paths(doc))
                self.assertTrue(lists, f"{name}: no trigger carries a `paths:` list")
                if key is not None:
                    lists["paths-filter"] = _filter_paths(_steps(doc), key)
                for where, paths in lists.items():
                    for pin_input in _PIN_INPUTS:
                        self.assertIn(
                            pin_input,
                            paths,
                            f"{name}: {where} does not list {pin_input}; a gosec bump there "
                            "would not run this scan, and the next pull request would inherit "
                            "its findings",
                        )

    def test_make_gosec_reaches_every_go_module(self) -> None:
        text = _MAKEFILE.read_text(encoding="utf-8")
        self.assertIn(_MAKE_TARGET, text, "Makefile: no gosec target")
        pin = _MAKE_PIN_MODULE.search(text)
        self.assertIsNotNone(pin, "Makefile: GOSEC_PIN_MODULE is not declared")
        unexcluded = _MAKE_UNEXCLUDED.search(text)
        self.assertIsNotNone(unexcluded, "Makefile: GOSEC_UNEXCLUDED_MODULES is not declared")
        covered = set(unexcluded.group(1).split()) | {pin.group(1)}
        self.assertEqual(_PIN_MODULE, pin.group(1))
        self.assertEqual(
            _go_modules(),
            covered,
            "Makefile: `make gosec` and the Go modules in the tree differ",
        )
        for module in _go_modules() - {_PIN_MODULE}:
            with self.subTest(module=module):
                self.assertNotIn(
                    _GOSEC_MODULE_PATH,
                    (REPO_ROOT / module / "go.mod").read_text(encoding="utf-8"),
                    f"{module}/go.mod pins gosec too; {_PIN_MODULE}/go.mod is the one pin every scan builds from",
                )

    def test_make_gosec_excludes_what_the_operator_scan_excludes(self) -> None:
        text = _MAKEFILE.read_text(encoding="utf-8")
        make_exclude = _MAKE_OPERATOR_EXCLUDE.search(text)
        self.assertIsNotNone(make_exclude, "Makefile: GOSEC_OPERATOR_EXCLUDE is not declared")
        excludes = {}
        for path in _workflow_files(WORKFLOWS_DIR):
            _text, doc = _load(path)
            for step in _steps(doc):
                if _is_gosec(step):
                    for found in _EXCLUDE_FLAG.findall(str(step.get("run", ""))):
                        excludes[f"{path.name}: {step.get('name')}"] = found
        self.assertTrue(excludes, "no workflow runs gosec with an -exclude= list; the Makefile's has nothing to match")
        for where, found in excludes.items():
            with self.subTest(step=where):
                self.assertEqual(
                    make_exclude.group(1),
                    found,
                    f"{where}: -exclude={found} differs from the Makefile's GOSEC_OPERATOR_EXCLUDE; "
                    "`make gosec` would pass or fail differently from the check it stands in for",
                )


if __name__ == "__main__":
    unittest.main()
