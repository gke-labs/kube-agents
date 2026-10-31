"""Unit tests for the platform self-copy refusal installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import kanban_self_copy as ksc

# The #3164 shape: eval-next 2110536180175998976, pdb-remediation-pr rep 2.
PARENT = "t_cd092e7d"
CLUSTER = "cluster-kube-agents-evals-4-seeded-a-us-central1-a"


def card(assignee="platform", tid=PARENT):
    return SimpleNamespace(id=tid, assignee=assignee)


class TestTheRefusal(unittest.TestCase):
    def test_a_platform_workers_concurrent_platform_card_is_refused(self):
        text = ksc.refuse_self_copy(card(), "platform", [])
        self.assertIn("kanban_create refused", text)
        self.assertIn("#3164", text)

    def test_the_refusal_does_not_offer_parents_as_a_way_round(self):
        """Told to queue it after its own card, a model does the job twice in sequence."""
        text = ksc.refuse_self_copy(card(), "platform", [])
        self.assertNotIn("parents", text)
        self.assertIn("Do this work on your own card", text)

    def test_an_assignee_with_stray_whitespace_is_still_refused(self):
        self.assertIsNotNone(ksc.refuse_self_copy(card(), " platform ", []))

    def test_a_title_cased_assignee_is_still_refused(self):
        """create_task lower-cases it, so "Platform" lands as `platform`."""
        self.assertIsNotNone(ksc.refuse_self_copy(card(), "Platform", []))

    def test_parents_listing_other_cards_only_is_still_refused(self):
        self.assertIsNotNone(ksc.refuse_self_copy(card(), "platform", ["t_other"]))


class TestWhatIsAllowed(unittest.TestCase):
    def test_a_follow_up_queued_after_the_creator_is_allowed(self):
        """Platform SOUL section 0: parents=[<your own card id>] queues follow-up work."""
        self.assertIsNone(ksc.refuse_self_copy(card(), "platform", [PARENT]))

    def test_a_card_for_a_cluster_agent_is_allowed(self):
        self.assertIsNone(ksc.refuse_self_copy(card(), CLUSTER, []))

    def test_a_card_filed_outside_any_worker_is_allowed(self):
        """A person, the chat session, a cron: kanban_create has no self_task."""
        self.assertIsNone(ksc.refuse_self_copy(None, "platform", []))

    def test_a_cluster_agents_card_for_its_own_profile_is_allowed(self):
        """A Cluster Agent may split its investigation into its own cards."""
        self.assertIsNone(ksc.refuse_self_copy(card(assignee=CLUSTER), CLUSTER, []))

    def test_a_card_with_no_id_is_left_alone(self):
        self.assertIsNone(ksc.refuse_self_copy(card(tid=None), "platform", []))


class TestTheApplier(unittest.TestCase):
    SOURCE = (
        "def _handle_create(args, **kw):\n"
        "    assignee = args.get('assignee')\n"
        "    parents = args.get('parents') or []\n"
        "    with _board(None) as (kb, conn):\n"
        "        self_tid = None\n"
        "        self_task = kb.get_task(conn, self_tid) if self_tid else None\n"
        "        new_tid = kb.create_task(conn)\n"
    )

    def setUp(self):
        import apply_kanban_self_copy as applier

        self.applier = applier
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / applier.RELATIVE
        self.target.parent.mkdir(parents=True)
        self.target.write_text(self.SOURCE)

    def test_the_gate_lands_after_self_task_and_before_the_write(self):
        import ast

        self.applier.apply(self.root)
        out = self.target.read_text()
        ast.parse(out)
        gate = out.index("_kanban_refuse_self_copy(self_task, assignee, parents)")
        self.assertLess(out.index("self_task = kb.get_task"), gate)
        self.assertLess(gate, out.index("kb.create_task"))
        self.assertIn("from tools.kanban_self_copy import", out)

    def test_a_second_run_refuses(self):
        self.applier.apply(self.root)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.root)

    def test_a_missing_anchor_fails_the_build(self):
        self.target.write_text(self.SOURCE.replace("self_task = ", "own = "))
        with self.assertRaises(SystemExit):
            self.applier.apply(self.root)


if __name__ == "__main__":
    unittest.main()
