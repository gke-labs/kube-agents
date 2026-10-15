"""Tests for chat_cases, the reader of the shared kube-agents.chat/v1 cases."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_cases as cc


def _case():
    return {
        "name": "a-case",
        "source": "agents/platform/scripts/test_chat_cases.py::_case",
        "input": {"markdown": "x"},
        "expected": [{"carrier": "result", "chat": {"schema": cc.SCHEMA, "headline": "x"}}],
    }


class SharedCasesTest(unittest.TestCase):
    def test_every_shared_case_has_the_documented_shape(self):
        cases = cc.load()
        self.assertGreater(len(cases), 0)
        self.assertEqual([c["name"] for c in cases], sorted(c["name"] for c in cases))

    def test_the_cases_cover_bnaylors_fixture_list(self):
        # The #2149 source map's section 5, less slack_ux_answer's tests (not on main yet).
        sources = {c["source"].split(cc.SOURCE_SEPARATOR)[0] for c in cc.load()}
        for path in (
            "agents/platform/scripts/test_slack_status.py",
            "agents/platform/scripts/test_slack_presenter.py",
            "agents/platform/scripts/test_slack_moments.py",
            "deploy/docker/patches/test_slack_ux_incident.py",
            "deploy/docker/patches/test_slack_ux_failure.py",
            "deploy/docker/patches/test_slack_ux_moments.py",
            "deploy/docker/patches/test_kanban_progress_lines.py",
            "deploy/docker/patches/test_slack_ux_reactions.py",
        ):
            self.assertIn(path, sources)

    def test_the_gaps_are_encoded(self):
        cases = {c["name"]: c for c in cc.load()}
        # G1: after is capped at 5 though the input has more lines.
        after = cases["report-after-capped-at-five"]
        self.assertEqual(len(after["expected"][0]["chat"]["after"]), 5)
        self.assertGreater(len(after["input"]["after_rows"].splitlines()), 5)
        # G2: every incident reply starts with its button's text.
        for name in ("incident-lettered-options", "incident-single-fix"):
            for choice in cases[name]["expected"][0]["chat"]["choices"]:
                self.assertTrue(choice["reply"].startswith(choice["text"] + " — apply"), choice)


class CheckCaseTest(unittest.TestCase):
    def check(self, case, stem="a-case"):
        cc.check_case(case, stem)

    def refused(self, edit, message):
        case = _case()
        edit(case)
        with self.assertRaisesRegex(cc.CaseError, message):
            self.check(case)

    def test_a_good_case(self):
        self.check(_case())

    def test_the_name_is_the_files(self):
        with self.assertRaisesRegex(cc.CaseError, "not the file's name"):
            self.check(_case(), "another")

    def test_refusals(self):
        self.refused(lambda c: c.pop("input"), "missing input")
        self.refused(lambda c: c.update(extra=1), "unknown fields")
        self.refused(lambda c: c.update(source="nowhere.py"), "is not <path>::<fixture>")
        self.refused(lambda c: c.update(source="nowhere.py::x"), "does not exist")
        self.refused(lambda c: c["expected"].append(c["expected"][0]), "exactly one output")
        self.refused(lambda c: c.update(expected=[None]), "no chat message at all")
        self.refused(lambda c: c["expected"][0].update(carrier="dm"), "is not a carrier")
        self.refused(lambda c: c["expected"][0].update(update="plan"), "only a notify")
        self.refused(lambda c: c["expected"][0]["chat"].update(schema="kube-agents.chat/v2"), "is not")
        self.refused(lambda c: c["expected"][0]["chat"].update(headlne="x"), "unknown fields")
        self.refused(lambda c: c["expected"][0]["chat"].update(rows=[{"severity": "x"}]), "missing text")
        self.refused(lambda c: c["expected"][0]["chat"].update(after=[1]), "not a string")
        self.refused(lambda c: c["expected"][0]["chat"].update(choices=[{"text": "a", "primary": "yes"}]), "not a bool")
        self.refused(
            lambda c: c["expected"][0]["chat"].update(plan={"items": [{"id": "a", "status": "pending", "stepCount": True}]}),
            "not a int",
        )
        self.refused(lambda c: c["expected"][0]["chat"].update(mark={"emoji": "x"}), "unknown fields")

    def test_events_have_one_expected_entry_each(self):
        case = _case()
        case["input"] = {"events": [{"ask": "a"}, {"terminal": "completed"}]}
        case["expected"] = [case["expected"][0], None]
        self.check(case)
        short = copy.deepcopy(case)
        short["expected"].pop()
        with self.assertRaisesRegex(cc.CaseError, "2 events but 1"):
            self.check(short)

    def test_load_refuses_an_empty_directory(self):
        with tempfile.TemporaryDirectory() as empty, self.assertRaisesRegex(cc.CaseError, "no cases"):
            cc.load(Path(empty))

    def test_load_checks_each_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = _case()
            Path(tmp, "a-case.json").write_text(json.dumps(case))
            self.assertEqual(cc.load(Path(tmp)), [case])
            Path(tmp, "b-case.json").write_text(json.dumps(case))
            with self.assertRaisesRegex(cc.CaseError, "not the file's name"):
                cc.load(Path(tmp))

    def test_load_sorts_by_name_where_a_name_prefixes_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a-case-twice", "a-case"):
                Path(tmp, f"{name}.json").write_text(json.dumps({**_case(), "name": name}))
            self.assertEqual([c["name"] for c in cc.load(Path(tmp))], ["a-case", "a-case-twice"])


if __name__ == "__main__":
    unittest.main()
