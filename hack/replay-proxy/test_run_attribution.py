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

    def request(self, messages, fingerprint, stored=True, recorded=False, answer=None, prior=None):
        request_id = f"{len(self.lines) + 1:032x}"
        if stored:
            with open(os.path.join(self.root, "store", "requests", request_id + ".json"), "w") as handle:
                json.dump({"messages": [self.blob(m) for m in messages]}, handle)
        hashes = [f"{abs(hash(json.dumps(m, sort_keys=True))) % (1 << 60):016x}" for m in messages]
        entry = {"event": "replay_shadow_request", "request_id": request_id, "fingerprint": fingerprint,
                 "job": {"build": BUILD}, "diag_settings_hash": SETTINGS, "diag_message_hashes": hashes,
                 "recorded": recorded, "prompt_tokens": 100}
        if answer:
            entry["answer_variant"] = ["ctx-" + fingerprint, answer]
        if prior:
            entry["prior_answer"] = list(prior)
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


class NameShapeTest(unittest.TestCase):
    def test_a_card_or_store_name_with_a_trailing_newline_is_refused(self):
        for pattern, good in ((RA.CARD_RE, "t_11111111"), (RA.SAFE_STORE_NAME, blob_name(1))):
            with self.subTest(pattern=pattern.pattern):
                self.assertTrue(pattern.match(good))
                self.assertIsNone(pattern.match(good + "\n"))
        self.assertEqual(RA.owned_cards(front(*created("t_22222222\n"))), set())


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

    def test_a_run_owns_its_worker_trajectory_tasks_only(self):
        record = [{"trajectory": [
            {"name": "inject.task", "args": {"taskId": "task-0770b8e639ccae1a"}},
            {"name": "kanban_show", "args": {"task_id": "t_99999999"}, "result": "t_88888888", "task": "t_11111111"},
            {"name": "terminal", "task": "not-a-card"},
        ]}]
        self.assertEqual(RA.run_cards(record), {"t_11111111"})


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

    def _rep(self, rep, outcome, answer, continued=True, opening_logged=True, recorded=False):
        """One repetition's front door: the shared opening, answered `answer`, then its own card."""
        card = f"t_{rep:08x}"
        self.world.run("case-a", rep, outcome, [card])
        opening = None
        if opening_logged:
            opening = self.world.request(front(), "opening", recorded=recorded, answer=answer)
        if continued:
            self.world.request(front({"role": "assistant", "content": answer}, *created(card)), f"next-{rep}",
                               prior=("ctx-opening", answer))
        return opening

    def test_an_opening_is_published_when_a_passing_run_went_on_from_its_answer(self):
        opening = self._rep(1, "pass", "A", recorded=True)
        self._rep(2, "fail", "B")
        self.assertEqual(self.world.decide(opening).__dict__, {"decision": "pass", "runs": ("case-a#1",)})
        self.assertEqual(self.world.decide(opening, final=False).decision, "pass", "positive evidence holds early")

    def test_a_failing_runs_opening_is_not_published_when_the_passing_runs_line_was_lost(self):
        # The review's case: the failing repetition recorded the opening; the
        # passing one's opening line never reached a segment. Its continuation
        # went on from another answer, so it is no evidence for this one.
        opening = self._rep(1, "fail", "A", recorded=True)
        self._rep(2, "pass", "B", opening_logged=False)
        self.assertEqual(self.world.decide(opening).decision, "fail")
        self.assertEqual(self.world.decide(opening, final=False).decision, "pending")

    def test_the_same_answer_given_to_a_passing_run_publishes_it(self):
        opening = self._rep(1, "fail", "A", recorded=True)
        self._rep(2, "pass", "A")
        self.assertEqual(self.world.decide(opening).__dict__, {"decision": "pass", "runs": ("case-a#2",)})

    def test_an_opening_with_no_continuation_logged_is_never_published(self):
        # Lost lines of every kind -- a spent budget, a replaced pod, a cut last
        # pass -- leave the opening without evidence, never with false evidence.
        opening = self._rep(1, "pass", "A", recorded=True, continued=False)
        self._rep(2, "pass", "B", opening_logged=False, continued=False)
        self.assertEqual(self.world.decide(opening, final=False).decision, "pending")
        self.assertEqual(self.world.decide(opening).decision, "ambiguous")
        self.assertEqual(self.world.decide(opening, lost=5).decision, "ambiguous")

    def _board_chain(self, outcome, log_middle=True):
        """A front door that reads its board twice before it creates its card: R1, R2, R3 card-less, then R4."""
        card = "t_00000001"
        self.world.run("case-a", 1, outcome, [card])
        listing = {"role": "tool", "tool_call_id": "c9", "content": "[]"}
        r1 = self.world.request(front(), "r1", recorded=True, answer="A")
        steps = [("r2", [{"role": "assistant", "content": "A"}, listing], ("ctx-r1", "A"), "B"),
                 ("r3", [{"role": "assistant", "content": "A"}, listing, {"role": "assistant", "content": "B"}, listing],
                  ("ctx-r2", "B"), "C")]
        for fingerprint, more, prior, answer in steps:
            if log_middle or fingerprint != "r2":
                self.world.request(front(*more), fingerprint, prior=prior, answer=answer)
        last = steps[-1][1] + [{"role": "assistant", "content": "C"}, *created(card)]
        self.world.request(front(*last), "r4", prior=("ctx-r3", "C"))
        return r1

    def test_an_opening_is_proven_through_card_less_continuations(self):
        opening = self._board_chain("pass")
        self.assertEqual(self.world.decide(opening).__dict__, {"decision": "pass", "runs": ("case-a#1",)})

    def test_a_chain_proven_to_a_failing_run_stays_a_fail(self):
        self.assertEqual(self.world.decide(self._board_chain("fail")).decision, "fail")

    def test_a_chain_broken_by_a_lost_line_stays_unproven(self):
        opening = self._board_chain("pass", log_middle=False)
        self.assertEqual(self.world.decide(opening).decision, "ambiguous")
        self.assertEqual(self.world.decide(opening, final=False).decision, "pending")

    def _shared_middle(self, outcomes):
        """Repetitions that open and read their board alike, each then creating its own card."""
        listing = {"role": "tool", "tool_call_id": "c9", "content": "[]"}
        opening = self.world.request(front(), "r1", recorded=True, answer="A")
        middle = [{"role": "assistant", "content": "A"}, listing]
        for rep, outcome in enumerate(outcomes, start=1):
            card = f"t_{rep:08x}"
            self.world.run("case-a", rep, outcome, [card])
            self.world.request(front(*middle), "r2", prior=("ctx-r1", "A"), answer="B")
            self.world.request(front(*middle, {"role": "assistant", "content": "B"}, *created(card, f"x{rep}")),
                               f"r3-{rep}", prior=("ctx-r2", "B"))
        return opening

    def test_repetitions_sharing_a_card_less_step_each_prove_the_opening(self):
        # Every run that went on from the shared middle answer sent the middle
        # request after the opening's answer, so each is proven for the opening.
        opening = self._shared_middle(["fail", "pass", "pass"])
        self.assertEqual(self.world.decide(opening).__dict__, {"decision": "pass", "runs": ("case-a#2", "case-a#3")})

    def test_repetitions_sharing_a_card_less_step_that_all_failed_stay_a_fail(self):
        self.assertEqual(self.world.decide(self._shared_middle(["fail", "fail"])).decision, "fail")

    def test_a_line_without_an_answer_variant_is_never_published(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        opening = self.world.request(front(), "opening", recorded=True)
        self.world.request(front(*created("t_00000001")), "next", prior=("ctx-opening", "A"))
        self.assertEqual(self.world.decide(opening).decision, "ambiguous")

    def test_a_continuation_naming_two_runs_is_no_evidence(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.world.run("case-a", 2, "pass", ["t_00000002"])
        opening = self.world.request(front(), "opening", recorded=True, answer="A")
        self.world.request(front(*created("t_00000001", "c1"), *created("t_00000002", "c2")), "both",
                           prior=("ctx-opening", "A"))
        self.assertEqual(self.world.decide(opening).decision, "ambiguous")

    def test_the_same_answer_after_another_request_is_no_evidence(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        opening = self.world.request(front(), "opening", recorded=True, answer="A")
        other = [system(), {"role": "user", "content": "another prompt"}, *created("t_00000001")]
        self.world.request(other, "elsewhere", prior=("ctx-opening", "A"))
        self.assertEqual(self.world.decide(opening).decision, "ambiguous")

    def _first_writer(self, writer_card, answers, outcomes):
        """An answer saved by `writer_card`'s request, then the same request from runs' workers, unsaved."""
        for rep, (answer, outcome) in enumerate(zip(answers, outcomes), start=1):
            card = f"t_{rep:08x}"
            self.world.run("case-a", rep, outcome, [card])
            self.world.request(worker(card), "opening", answer=answer)
        return self.world.request(worker(writer_card), "opening", recorded=True, answer="A")

    def test_an_answer_saved_by_background_traffic_is_proven_by_a_passing_run_that_gave_it(self):
        # The cache keeps the first answer to a fingerprint, so a run's worker
        # opening that background traffic sent first is saved under the
        # background card; the run's own line says it got exactly that answer.
        saved = self._first_writer("t_99999999", ["A"], ["pass"])
        attribution = self.world.attribution()
        line = RA.Line.from_entry(saved)
        self.assertEqual(attribution.decide(line).decision, "unattributed", "the saving line itself is no run's")
        self.assertEqual(attribution.decide_answer(line).__dict__, {"decision": "pass", "runs": ("case-a#1",)})

    def test_a_passing_run_that_gave_another_answer_is_no_evidence(self):
        saved = self._first_writer("t_99999999", ["B"], ["pass"])
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "unattributed")

    def test_the_same_answer_given_to_a_failing_run_too_is_not_published(self):
        saved = self._first_writer("t_99999999", ["A", "A"], ["pass", "fail"])
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "unattributed")

    def test_a_failing_runs_saved_answer_stays_a_fail_when_a_passing_run_gave_it_too(self):
        saved = self._first_writer("t_00000002", ["A"], ["pass"])
        self.world.run("case-a", 2, "fail", ["t_00000002"])
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "fail")

    def test_a_failing_runs_saving_line_beside_an_unworked_card_is_not_published(self):
        # The saving line is a failing run's worker that also created a card no
        # run worked (a run cut short), so it decides unattributed with that run.
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.world.request(worker("t_00000001"), "opening", answer="A")
        self.world.run("case-a", 2, "fail", ["t_00000002"])
        saved = self.world.request(worker("t_00000002", *created("t_77777777")), "opening", recorded=True, answer="A")
        attribution = self.world.attribution()
        line = RA.Line.from_entry(saved)
        self.assertEqual(attribution.decide(line).__dict__, {"decision": "unattributed", "runs": ("case-a#2",)})
        self.assertEqual(attribution.decide_answer(line).decision, "unattributed")

    def test_a_failing_run_beside_an_unworked_card_that_got_the_answer_blocks_it(self):
        saved = self._first_writer("t_99999999", ["A"], ["pass"])
        self.world.run("case-a", 2, "fail", ["t_00000002"])
        self.world.request(worker("t_00000002", *created("t_88888888")), "opening", answer="A")
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "unattributed")

    def test_a_failing_run_hidden_behind_a_card_less_lines_pass_blocks_it(self):
        # A card-less line that got the answer decides pass on its passing run
        # and drops the failing one that went on from it too; the guard sees both.
        saved = self.world.request(worker("t_99999999"), "r1", recorded=True, answer="A")
        self.world.request(front(), "r1", answer="A")
        for rep, outcome in ((1, "pass"), (2, "fail")):
            card = f"t_{rep:08x}"
            self.world.run("case-a", rep, outcome, [card])
            self.world.request(front({"role": "assistant", "content": "A"}, *created(card, f"x{rep}")),
                               f"r2-{rep}", prior=("ctx-r1", "A"))
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "unattributed")

    def test_a_passing_run_in_another_build_is_no_evidence(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        other = self.world.request(worker("t_00000001"), "opening", answer="A")
        other["job"] = {"build": "1790000000000000001"}
        saved = self.world.request(worker("t_99999999"), "opening", recorded=True, answer="A")
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "unattributed")

    def test_an_answer_proven_by_another_line_waits_for_the_last_call(self):
        saved = self._first_writer("t_99999999", ["A"], ["pass"])
        decision = self.world.attribution(final=False).decide_answer(RA.Line.from_entry(saved))
        self.assertEqual(decision.decision, "pending", "the saving line's card may still be a running case's")

    def test_a_saved_answer_without_an_answer_variant_takes_no_evidence_from_other_lines(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.world.request(worker("t_00000001"), "opening")
        saved = self.world.request(worker("t_99999999"), "opening", recorded=True)
        self.assertEqual(self.world.attribution().decide_answer(RA.Line.from_entry(saved)).decision, "unattributed")

    def test_load_runs_reads_no_relative_run_directory(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        path = os.path.join(self.world.verdicts, "case-case-a.json")
        with open(path) as handle:
            case = json.load(handle)
        case["reps"][0]["run_dir"] = os.path.relpath(case["reps"][0]["run_dir"])
        with open(path, "w") as handle:
            json.dump(case, handle)
        self.assertEqual(RA.load_runs(self.world.verdicts)[0].ids, frozenset())

    def test_load_runs_skips_a_case_file_still_being_written(self):
        with open(os.path.join(self.world.verdicts, "case-half.json"), "w") as handle:
            handle.write('{"case": "half", "reps": [')
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.assertEqual([r.key for r in RA.load_runs(self.world.verdicts)], ["case-a#1"])


if __name__ == "__main__":
    unittest.main()
