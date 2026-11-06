"""Telling the runs in a build apart: cards from stored text, runs from results.json, the counting rule."""

import json
import os
import shutil
import tempfile
import unittest

import run_attribution as RA

BUILD = "1790000000000000000"
SETTINGS = "s" * 64
FRONT_PROMPT = "Which namespaces exist in the cluster?"


def blob_name(index):
    return f"{index:064x}"


class World:
    """A keep directory (store/), a verdicts directory (case-*.json), results.json per run, and lines."""

    def __init__(self, root):
        self.root = root
        self.verdicts = os.path.join(root, "verdicts")
        os.makedirs(os.path.join(root, "store", "requests"))
        os.makedirs(os.path.join(root, "store", "blobs"))
        os.makedirs(self.verdicts)
        self.blobs = {}
        self.lines = []
        self.cases = {}

    def blob(self, message):
        key = json.dumps(message, sort_keys=True)
        if key not in self.blobs:
            name = blob_name(len(self.blobs) + 1)
            self.blobs[key] = name
            with open(os.path.join(self.root, "store", "blobs", name + ".json"), "w") as handle:
                json.dump(message, handle)
        return self.blobs[key]

    def request(self, messages, fingerprint, stored=True, recorded=False):
        request_id = f"{len(self.lines) + 1:032x}"
        if stored:
            with open(os.path.join(self.root, "store", "requests", request_id + ".json"), "w") as handle:
                json.dump({"messages": [self.blob(m) for m in messages]}, handle)
        hashes = [f"{abs(hash(json.dumps(m, sort_keys=True))) % (1 << 60):016x}" for m in messages]
        entry = {"event": "replay_shadow_request", "request_id": request_id, "fingerprint": fingerprint,
                 "job": {"build": BUILD}, "diag_settings_hash": SETTINGS, "diag_message_hashes": hashes,
                 "recorded": recorded, "prompt_tokens": 100}
        self.lines.append(entry)
        return entry

    def run(self, case, rep, outcome, cards, results=True):
        run_dir = os.path.join(self.root, "runs", f"{case}-{rep}")
        os.makedirs(run_dir)
        if results:
            trajectory = [{"name": "inject.task", "args": {"taskId": "task-" + "0" * 15 + str(rep)}}]
            trajectory += [{"name": "terminal", "task": card} for card in cards]
            with open(os.path.join(run_dir, "results.json"), "w") as handle:
                json.dump([{"trajectory": trajectory}], handle)
        self.cases.setdefault(case, []).append({"index": rep, "outcome": outcome, "run_dir": run_dir})
        with open(os.path.join(self.verdicts, f"case-{case}.json"), "w") as handle:
            json.dump({"case": case, "reps": self.cases[case]}, handle)

    def attribution(self, final=True, lost=0):
        logs = RA.LogSet(lines=[RA.Line.from_entry(e) for e in self.lines], lost=lost)
        return RA.Attribution(logs, RA.Store(self.root), RA.load_runs(self.verdicts), final)

    def decide(self, entry, final=True, lost=0):
        attribution = self.attribution(final, lost)
        return attribution.decide(RA.Line.from_entry(entry))


def system():
    return {"role": "system", "content": "Card `t_3ba2166a` closed on 2026-08-09 with an ASCII report."}


def worker(card, *more):
    return [system(), {"role": "user", "content": f"work kanban task {card}"}, *more]


def front(*more):
    return [system(), {"role": "user", "content": FRONT_PROMPT}, *more]


def created(card, call_id="call_1"):
    return [
        {"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "kanban_create", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"ok": True, "task_id": card})},
    ]


class CardsTest(unittest.TestCase):
    def test_a_worker_owns_the_card_it_was_started_on(self):
        self.assertEqual(RA.owned_cards(worker("t_11111111")), {"t_11111111"})

    def test_a_conversation_owns_the_cards_its_kanban_create_calls_returned(self):
        self.assertEqual(RA.owned_cards(front(*created("t_22222222"))), {"t_22222222"})

    def test_cards_named_anywhere_else_are_not_evidence(self):
        listing = {"role": "tool", "tool_call_id": "call_9",
                   "content": json.dumps({"tasks": [{"id": "t_33333333"}], "task_id": "t_44444444"})}
        later = {"role": "user", "content": "work kanban task t_55555555"}
        messages = front({"role": "assistant", "tool_calls": [{"id": "call_9", "function": {"name": "kanban_list"}}]},
                         listing, later)
        self.assertEqual(RA.owned_cards(messages), set(), "a system prompt, a listing, a later user turn")

    def test_a_run_owns_its_trajectory_tasks_and_inject_task(self):
        record = [{"trajectory": [
            {"name": "inject.task", "args": {"taskId": "task-0770b8e639ccae1a"}},
            {"name": "kanban_show", "args": {"task_id": "t_99999999"}, "result": "t_88888888", "task": "t_11111111"},
            {"name": "terminal", "task": "not-a-card"},
        ]}]
        self.assertEqual(RA.run_cards(record), {"task-0770b8e639ccae1a", "t_11111111"})


class DecisionTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.world = World(root)

    def test_a_request_belongs_to_the_run_that_worked_its_card(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.run("case-a", 2, "fail", ["t_22222222"])
        passing = self.world.request(worker("t_11111111"), "f1")
        failing = self.world.request(worker("t_22222222"), "f2")
        self.assertEqual(self.world.decide(passing).__dict__, {"decision": "pass", "runs": ("case-a#1",)})
        self.assertEqual(self.world.decide(failing).__dict__, {"decision": "fail", "runs": ("case-a#2",)})

    def test_a_card_in_a_passing_and_a_failing_run_is_a_fail(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.run("case-a", 2, "fail", ["t_11111111"])
        entry = self.world.request(worker("t_11111111"), "f1")
        self.assertEqual(self.world.decide(entry).decision, "fail")

    def test_a_card_no_graded_run_worked_waits_then_is_unattributed(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        entry = self.world.request(worker("t_99999999"), "f1")
        self.assertEqual(self.world.decide(entry, final=False).decision, "pending")
        self.assertEqual(self.world.decide(entry, final=True).decision, "unattributed")

    def test_a_request_without_stored_text_is_never_attributed(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        entry = self.world.request(worker("t_11111111"), "f1", stored=False)
        self.assertEqual(self.world.decide(entry, final=False).decision, "pending")
        self.assertEqual(self.world.decide(entry, final=True).decision, "unattributed")

    def test_a_run_without_results_owns_nothing(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"], results=False)
        entry = self.world.request(worker("t_11111111"), "f1")
        self.assertEqual(self.world.decide(entry).decision, "unattributed")

    def _openings(self, outcomes, continue_all=True):
        """Each run's front door opens with the same request, then creates its own card."""
        openings = []
        for rep, outcome in enumerate(outcomes, start=1):
            card = f"t_{rep:08x}"
            self.world.run("case-a", rep, outcome, [card])
            openings.append(self.world.request(front(), "opening"))
            if continue_all or rep == 1:
                self.world.request(front(*created(card)), f"next-{rep}")
        return openings

    def test_an_opening_with_no_card_is_decided_by_the_runs_that_continued_it(self):
        openings = self._openings(["pass", "pass", "pass"])
        self.assertEqual(self.world.decide(openings[0]).__dict__,
                         {"decision": "pass", "runs": ("case-a#1", "case-a#2", "case-a#3")})

    def test_an_opening_one_failing_run_continued_is_a_fail(self):
        openings = self._openings(["pass", "fail", "pass"])
        self.assertEqual(self.world.decide(openings[0]).decision, "fail")

    def test_an_opening_issued_more_often_than_runs_continued_it_is_ambiguous(self):
        # Run 2 and 3 issued the opening and never continued it (their front
        # door answered and stopped): either could have written the saved answer.
        openings = self._openings(["pass", "fail", "fail"], continue_all=False)
        self.assertEqual(self.world.decide(openings[0]).decision, "ambiguous")

    def test_an_opening_waits_for_every_case_and_a_lost_line_refuses_it(self):
        openings = self._openings(["pass", "pass"])
        self.assertEqual(self.world.decide(openings[0], final=False).decision, "pending")
        self.assertEqual(self.world.decide(openings[0], lost=1).decision, "ambiguous")

    def test_a_continuation_naming_two_runs_counts_for_neither(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.world.run("case-a", 2, "pass", ["t_00000002"])
        opening = self.world.request(front(), "opening")
        self.world.request(front(*created("t_00000001", "c1"), *created("t_00000002", "c2")), "both")
        self.assertEqual(self.world.decide(opening).decision, "ambiguous")

    def test_load_runs_skips_a_case_file_still_being_written(self):
        with open(os.path.join(self.world.verdicts, "case-half.json"), "w") as handle:
            handle.write('{"case": "half", "reps": [')
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.assertEqual([r.key for r in RA.load_runs(self.world.verdicts)], ["case-a#1"])


if __name__ == "__main__":
    unittest.main()
