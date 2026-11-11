"""Tests for automation_rules, the parser for the customer's `.kube-agents/automation.yaml`."""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import automation_rules as ar

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "gitops-repo" / ar.RULES_FILE

GOOD = """\
version: 1
automerge:
  allow:
    - fix: no-pdb
      environments: [dev, staging]
  deny:
    - fix: "*"
      environments: [prod]
"""


def _tree(text: str | None) -> Path:
    root = Path(tempfile.mkdtemp())
    if text is not None:
        (root / ".kube-agents").mkdir()
        (root / ar.RULES_FILE).write_text(text, encoding="utf-8")
    return root


class TestParse(unittest.TestCase):
    def test_good_file(self):
        rules = ar.parse(GOOD)
        self.assertEqual(rules.status, ar.LOADED, rules.error)
        self.assertEqual(rules.allow, (ar.Rule("no-pdb", ("dev", "staging")),))
        self.assertEqual(rules.deny, (ar.Rule("*", ("prod",)),))
        self.assertEqual(rules.warnings, ())

    def test_example_file_parses_and_allows_only_dev(self):
        rules = ar.parse(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(rules.status, ar.LOADED, rules.error)
        self.assertEqual(rules.warnings, ())
        self.assertTrue(ar.decide(rules, "no-pdb", "dev").allowed)
        self.assertFalse(ar.decide(rules, "no-pdb", "prod").allowed)

    def test_version_only_allows_nothing(self):
        rules = ar.parse("version: 1\n")
        self.assertEqual(rules.status, ar.LOADED)
        self.assertFalse(ar.decide(rules, "no-pdb", "dev").allowed)

    def test_refusals(self):
        # Each refuses the whole file, never the one rule.
        cases = {
            "empty": "",
            "not a mapping": "- a\n",
            "no version": "automerge: {}\n",
            "wrong version": "version: 2\n",
            "bool version": "version: true\n",
            "string version": "version: '1'\n",
            "unknown top key": "version: 1\nautomerge: {}\nextra: 1\n",
            "unknown automerge key": "version: 1\nautomerge: {maybe: []}\n",
            "allow not a list": "version: 1\nautomerge: {allow: {fix: no-pdb}}\n",
            "rule missing environments": "version: 1\nautomerge: {allow: [{fix: no-pdb}]}\n",
            "rule unknown key": "version: 1\nautomerge: {allow: [{fix: no-pdb, environments: [dev], when: x}]}\n",
            "empty environments": "version: 1\nautomerge: {allow: [{fix: no-pdb, environments: []}]}\n",
            "environment not a string": "version: 1\nautomerge: {allow: [{fix: no-pdb, environments: [1]}]}\n",
            "uppercase environment": "version: 1\nautomerge: {allow: [{fix: no-pdb, environments: [Dev]}]}\n",
            "wildcard fix in allow": "version: 1\nautomerge: {allow: [{fix: '*', environments: [dev]}]}\n",
            "wildcard env in allow": "version: 1\nautomerge: {allow: [{fix: no-pdb, environments: ['*']}]}\n",
            "bad fix spelling": "version: 1\nautomerge: {allow: [{fix: No_PDB, environments: [dev]}]}\n",
            "not yaml": "version: [1\n",
            "duplicate key": "version: 1\nautomerge: {deny: [{fix: '*', environments: [prod]}]}\nautomerge: {}\n",
            "nested duplicate key": "version: 1\nautomerge: {allow: [], allow: [{fix: no-pdb, environments: [dev]}]}\n",
            "merge key": "version: 1\nautomerge:\n  deny: &d [{fix: '*', environments: [prod]}]\n  allow: [{<<: {fix: no-pdb}, environments: [dev]}]\n",
            "trailing newline in environment": "version: 1\nautomerge:\n  deny: [{fix: '*', environments: [\"prod\\n\"]}]\n",
            "block scalar environment": "version: 1\nautomerge:\n  deny:\n    - fix: '*'\n      environments:\n        - |\n          prod\n",
            "trailing newline in fix": "version: 1\nautomerge: {deny: [{fix: \"no-pdb\\n\", environments: [prod]}]}\n",
            "unhashable key": "version: 1\n? [a]\n: 1\n",
            "unsafe tag": "version: !!python/object/apply:os.system ['true']\n",
            "too large": "version: 1\n#" + "x" * ar.MAX_FILE_BYTES + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                rules = ar.parse(text)
                self.assertEqual(rules.status, ar.INVALID)
                self.assertTrue(rules.error)
                self.assertEqual((rules.allow, rules.deny), ((), ()))
                self.assertFalse(ar.decide(rules, "no-pdb", "dev").allowed)

    def test_a_key_too_large_to_print_refuses_the_file(self):
        huge = ":".join(["59"] * 2600)
        for text in (f"version: 1\n? {huge}\n: x\n", f"version: 1\nautomerge:\n  ? {huge}\n  : x\n"):
            self.assertEqual(ar.parse(text).status, ar.INVALID)

    def test_merge_key_named_in_error(self):
        rules = ar.parse("version: 1\nautomerge: {allow: [{<<: {fix: no-pdb}, environments: [dev]}]}\n")
        self.assertEqual(rules.status, ar.INVALID)
        self.assertIn("merge keys", rules.error)

    def test_bad_deny_rule_does_not_leave_allow_in_force(self):
        text = (
            "version: 1\nautomerge:\n  allow: [{fix: no-pdb, environments: [dev]}]\n"
            "  deny: [{fix: no-pdb, environment: [dev]}]\n"
        )
        rules = ar.parse(text)
        self.assertEqual(rules.status, ar.INVALID)
        self.assertIn("automerge.deny[0]", rules.error)
        self.assertFalse(ar.decide(rules, "no-pdb", "dev").allowed)

    def test_unknown_fix_type_warns_and_never_matches(self):
        rules = ar.parse("version: 1\nautomerge: {allow: [{fix: no-pbd, environments: [dev]}]}\n")
        self.assertEqual(rules.status, ar.LOADED)
        self.assertEqual(len(rules.warnings), 1)
        self.assertIn("no-pbd", rules.warnings[0])
        self.assertFalse(ar.decide(rules, "no-pdb", "dev").allowed)
        # Asked about the unknown type itself, the rule still does not match.
        self.assertFalse(ar.decide(rules, "no-pbd", "dev").allowed)


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.rules = ar.parse(GOOD)

    def test_allowed(self):
        decision = ar.decide(self.rules, "no-pdb", "dev")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed by automerge.allow[0]")

    def test_default_deny(self):
        self.assertFalse(ar.decide(self.rules, "no-pdb", "qa").allowed)
        self.assertFalse(ar.decide(self.rules, "unbound-sa-automount", "dev").allowed)

    def test_deny_beats_allow(self):
        rules = ar.parse(
            "version: 1\nautomerge:\n  allow: [{fix: no-pdb, environments: [dev]}]\n"
            "  deny: [{fix: no-pdb, environments: ['*']}]\n"
        )
        decision = ar.decide(rules, "no-pdb", "dev")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "denied by automerge.deny[0]")

    def test_unknown_environment_denied(self):
        decision = ar.decide(self.rules, "no-pdb", ar.UNKNOWN_ENVIRONMENT)
        self.assertFalse(decision.allowed)
        self.assertIn("no environment label", decision.reason)

    def test_status_reasons(self):
        self.assertIn("off", ar.decide(ar.Rules(ar.SWITCH_OFF), "no-pdb", "dev").reason)
        self.assertIn("no .kube-agents", ar.decide(ar.Rules(ar.ABSENT), "no-pdb", "dev").reason)
        refused = ar.decide(ar.Rules(ar.INVALID, error="boom"), "no-pdb", "dev")
        self.assertFalse(refused.allowed)
        self.assertIn("boom", refused.reason)


class TestLoad(unittest.TestCase):
    def test_absent(self):
        self.assertEqual(ar.load(_tree(None)).status, ar.ABSENT)

    def test_loaded(self):
        self.assertEqual(ar.load(_tree(GOOD)).status, ar.LOADED)

    def test_not_utf8(self):
        root = _tree(None)
        (root / ".kube-agents").mkdir()
        (root / ar.RULES_FILE).write_bytes(b"version: 1\n\xff\n")
        self.assertEqual(ar.load(root).status, ar.INVALID)

    def test_oversized_file_read_is_capped(self):
        rules = ar.load(_tree("version: 1\n#" + "x" * (ar.MAX_FILE_BYTES * 4) + "\n"))
        self.assertEqual(rules.status, ar.INVALID)

    def test_oversized_multibyte_says_too_large(self):
        # A cap that cuts a character in half must still report the size.
        rules = ar.load(_tree("version: 1\n#" + "\u00e9" * ar.MAX_FILE_BYTES + "\n"))
        self.assertEqual(rules.status, ar.INVALID)
        self.assertIn("larger than", rules.error)

    def test_symlinked_file_refused(self):
        root = _tree(None)
        (root / ".kube-agents").mkdir()
        outside = Path(tempfile.mkdtemp()) / "rules.yaml"
        outside.write_text(GOOD, encoding="utf-8")
        os.symlink(outside, root / ar.RULES_FILE)
        rules = ar.load(root)
        self.assertEqual(rules.status, ar.INVALID)
        self.assertIn("symbolic link", rules.error)

    def test_symlinked_directory_refused(self):
        root = Path(tempfile.mkdtemp())
        elsewhere = _tree(GOOD) / ".kube-agents"
        os.symlink(elsewhere, root / ".kube-agents")
        self.assertEqual(ar.load(root).status, ar.INVALID)


class TestReadRules(unittest.TestCase):
    def test_switch_off_never_reads_the_file(self):
        root = _tree(GOOD)
        with mock.patch.object(ar, "load") as load:
            for value in ("off", "", "garbage"):
                with self.subTest(value):
                    self.assertEqual(ar.read_rules(root, automerge=value).status, ar.SWITCH_OFF)
        load.assert_not_called()

    def test_switch_on_or_practice_reads_the_file(self):
        root = _tree(GOOD)
        for value in ("practice", "on"):
            with self.subTest(value):
                self.assertEqual(ar.read_rules(root, automerge=value).status, ar.LOADED)

    def test_default_asks_the_switch_helper(self):
        root = _tree(GOOD)
        for value, status in (("off", ar.SWITCH_OFF), ("practice", ar.LOADED)):
            fake = types.ModuleType("kubeagents_switches")
            fake.switch = mock.Mock(return_value=value)
            with self.subTest(value), mock.patch.dict(sys.modules, {"kubeagents_switches": fake}):
                self.assertEqual(ar.read_rules(root).status, status)
                fake.switch.assert_called_once_with("AUTOMERGE")


class TestRealSwitchHelper(unittest.TestCase):
    def test_reads_the_variable_outside_the_sandbox(self):
        import kubeagents_switches

        root = _tree(GOOD)
        missing = Path(tempfile.mkdtemp()) / "none"
        with mock.patch.object(kubeagents_switches, "SWITCH_DIR", missing):
            with mock.patch.dict(os.environ, {"KUBEAGENTS_AUTOMERGE": "practice"}):
                self.assertEqual(ar.read_rules(root).status, ar.LOADED)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(ar.read_rules(root).status, ar.SWITCH_OFF)


class TestClusterEnvironment(unittest.TestCase):
    def test_keys_match_fleet_drift(self):
        # The page says the keys are the fleet-drift audit's; hold them together.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "fleet-audit" / "scripts"))
        try:
            import fleet_drift
        finally:
            sys.path.pop(0)
        self.assertEqual(ar.ENV_LABEL_KEYS, fleet_drift.ENV_LABEL_KEYS)

    def test_label_order(self):
        self.assertEqual(ar.cluster_environment({"tier": "t", "env": "dev"}), "dev")
        self.assertEqual(ar.cluster_environment({"environment": "prod", "env": "dev"}), "prod")
        self.assertEqual(ar.cluster_environment({"stage": "qa"}), "qa")

    def test_none(self):
        self.assertEqual(ar.cluster_environment(None), ar.UNKNOWN_ENVIRONMENT)
        self.assertEqual(ar.cluster_environment({"team": "x", "env": ""}), ar.UNKNOWN_ENVIRONMENT)


class TestMain(unittest.TestCase):
    def _run(self, *argv, automerge="practice"):
        fake = types.ModuleType("kubeagents_switches")
        fake.switch = lambda name: automerge
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"kubeagents_switches": fake}), redirect_stdout(out):
            code = ar.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_decision(self):
        code, out = self._run(str(_tree(GOOD)), "--fix", "no-pdb", "--environment", "dev")
        self.assertEqual(code, 0)
        self.assertEqual(out["decision"], {"allowed": True, "reason": "allowed by automerge.allow[0]"})

    def test_invalid_exits_nonzero(self):
        code, out = self._run(str(_tree("version: 2\n")))
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], ar.INVALID)

    def test_switch_off(self):
        code, out = self._run(str(_tree(GOOD)), "--fix", "no-pdb", "--environment", "dev", automerge="off")
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], ar.SWITCH_OFF)
        self.assertFalse(out["decision"]["allowed"])


if __name__ == "__main__":
    unittest.main()
