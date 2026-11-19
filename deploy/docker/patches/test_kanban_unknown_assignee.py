"""Unit tests for the unknown-assignee refusal installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import kanban_unknown_assignee as kua

# The #3786 shape: the name the model composed, and the profile it meant.
COMPOSED = "seeded-a-cluster-agent"
CLUSTER = "cluster-devops-bench-complex-evals-seeded-a-us-central1-a"
UNFINISHED = "cluster-devops-bench-complex-evals-seeded-b-us-central1-a"


REPO = Path(__file__).resolve().parents[3]


def profile(root: Path, name: str, *, registered: bool = True, user_md: bool = True, legacy: bool = False) -> Path:
    home = root / name
    home.mkdir(parents=True)
    if registered:
        (home / kua.PROFILE_MARKER).write_text("name: x\n", encoding="utf-8")
    if legacy:
        (home / kua.LEGACY_PROFILE_MARKER).write_text("model: x\n", encoding="utf-8")
    if user_md:
        (home / kua.USER_MD_NAME).write_text("user\n", encoding="utf-8")
    return home


class ProfilesCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "profiles"
        # The Platform Agent and a specialist carry no USER.md; a Cluster Agent does.
        profile(self.root, "platform", user_md=False)
        profile(self.root, "tse", user_md=False)
        profile(self.root, CLUSTER)
        # Registered, but the scaffold stopped before USER.md.
        profile(self.root, UNFINISHED, user_md=False)
        # A folder Hermes never registered: a mount point's leftover.
        profile(self.root, "cluster-leftover", registered=False, user_md=False)
        profile(self.root, ".deleted", registered=False, user_md=False)

    def refuse(self, assignee):
        return kua.refuse_unknown_assignee(assignee, self.root)


class TestTheRefusal(ProfilesCase):
    def test_a_made_up_name_is_refused_and_the_refusal_lists_the_profiles_that_can_take_a_card(self):
        text = self.refuse(COMPOSED)
        self.assertIn("kanban_create refused", text)
        self.assertIn(f"`{COMPOSED}` is not a profile on this install", text)
        self.assertIn("#3786", text)
        self.assertIn(f"`platform`, `tse`, `{CLUSTER}`", text)

    def test_the_list_leaves_out_what_cannot_take_a_card(self):
        text = self.refuse(COMPOSED)
        for name in (UNFINISHED, "cluster-leftover", ".deleted", kua.ROOT_PROFILE):
            self.assertNotIn(f"`{name}`", text)

    def test_a_name_in_another_case_is_the_profile_it_lands_as(self):
        """create_task lower-cases the assignee, so the card would go to the real profile."""
        self.assertIsNone(self.refuse("Platform"))
        self.assertIsNone(self.refuse(CLUSTER.upper()))
        self.assertIsNone(self.refuse(f"  {CLUSTER}  "))

    def test_a_made_up_name_in_another_case_is_still_refused(self):
        self.assertIn(f"`{COMPOSED}`", self.refuse(COMPOSED.upper()))

    def test_a_cluster_profile_whose_scaffold_did_not_finish_is_refused_as_such(self):
        text = self.refuse(UNFINISHED)
        self.assertIn("kanban_create refused", text)
        self.assertIn("whose scaffold did not finish", text)
        # Its worker does start: the text must not say that no worker picks the card up.
        self.assertIn("would stop at its preflight", text)
        self.assertNotIn("No worker picks up", text)
        self.assertNotIn("is not a profile on this install", text)

    def test_hermes_saying_that_an_unfinished_cluster_profile_exists_does_not_admit_it(self):
        """It is registered, so Hermes says yes; its worker still cannot serve a card."""
        self.assertIsNotNone(kua.refuse_unknown_assignee(UNFINISHED, self.root, lambda name: True))

    def test_a_hidden_folder_is_not_a_profile_even_with_the_marker(self):
        profile(self.root, ".archived")
        text = self.refuse(".archived")
        self.assertIn("is not a profile on this install", text)
        self.assertNotIn("`.archived`,", text)

    def test_on_a_fleet_longer_than_the_list_the_nearest_names_are_shown_first(self):
        for i in range(50):
            profile(self.root, f"cluster-p-fleet-{i:02d}-us-central1-a")
        text = self.refuse("fleet-37-cluster-agent")
        self.assertIn("`cluster-p-fleet-37-us-central1-a`", text)
        self.assertIn("that are not shown", text)
        # Nearest first: it comes before a profile that shares fewer words.
        self.assertLess(text.index("`cluster-p-fleet-37-us-central1-a`"), text.index("`cluster-p-fleet-00-us-central1-a`"))

    def test_a_folder_hermes_never_registered_is_not_a_profile(self):
        self.assertIn("is not a profile on this install", self.refuse("cluster-leftover"))

    def test_a_long_list_is_cut_and_counted(self):
        for i in range(kua.LISTED_LIMIT + 5):
            profile(self.root, f"cluster-p-fleet-{i:02d}-us-central1-a")
        text = self.refuse(COMPOSED)
        self.assertIn("and 8 more", text)
        self.assertEqual(text.count("`cluster-"), kua.LISTED_LIMIT - 2)
        # The specialists stay in view however long the fleet is.
        self.assertIn("`platform`, `tse`, ", text)


class TestWhatItLeavesAlone(ProfilesCase):
    def test_every_profile_that_can_take_a_card_is_accepted(self):
        for name in ("platform", "tse", CLUSTER):
            self.assertIsNone(self.refuse(name), name)

    def test_a_profile_made_before_hermes_wrote_its_marker_is_accepted_and_listed(self):
        """`config.yaml` and no `profile.yaml`: the entrypoint and profile_plugins read it as a real profile."""
        profile(self.root, "legacy-specialist", registered=False, user_md=False, legacy=True)
        self.assertIsNone(self.refuse("legacy-specialist"))
        self.assertIn("`legacy-specialist`", self.refuse(COMPOSED))

    def test_a_profile_that_hermes_says_exists_is_accepted(self):
        """The dispatcher starts a worker for whatever Hermes says exists."""
        asked = []

        def exists(name):
            asked.append(name)
            return True

        self.assertIsNone(kua.refuse_unknown_assignee("Somewhere-Else", self.root, exists))
        self.assertEqual(asked, ["somewhere-else"])
        self.assertIsNotNone(kua.refuse_unknown_assignee("somewhere-else", self.root, lambda name: False))

    def test_only_a_name_that_starts_as_a_cluster_agents_needs_the_finished_scaffold(self):
        profile(self.root, "my-cluster-helper", user_md=False)
        self.assertIsNone(self.refuse("my-cluster-helper"))

    def test_the_gateways_own_profile_is_accepted(self):
        self.assertIsNone(self.refuse(kua.ROOT_PROFILE))
        self.assertIsNone(self.refuse("Default"))

    def test_a_card_with_no_assignee_is_not_this_refusals_case(self):
        for assignee in (None, "", "   "):
            self.assertIsNone(self.refuse(assignee))

    def test_a_profiles_folder_that_is_absent_refuses_nothing(self):
        self.assertIsNone(kua.refuse_unknown_assignee(COMPOSED, self.root / "absent"))

    def test_a_profiles_folder_that_cannot_be_listed_refuses_nothing(self):
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assertIsNone(self.refuse(COMPOSED))

    def test_a_profile_that_cannot_be_read_is_not_known_to_be_wrong(self):
        real = Path.is_file

        def is_file(path):
            if path.parent.name == CLUSTER:
                raise PermissionError("denied")
            return real(path)

        with mock.patch.object(Path, "is_file", is_file):
            self.assertIsNone(self.refuse(CLUSTER))


class TestWhereTheProfilesAre(ProfilesCase):
    def test_the_gateway_sees_them_under_its_home(self):
        self.assertEqual(kua.profiles_dir_for(self.root.parent), self.root)

    def test_a_worker_sees_them_beside_its_own_home(self):
        self.assertEqual(kua.profiles_dir_for(self.root / "platform"), self.root)

    def test_the_handler_finds_them_from_the_process_home(self):
        """Outside Hermes the home is the variable; a worker's home is its profile's folder."""
        with mock.patch.dict(os.environ, {kua.HOME_ENV: str(self.root / "platform")}):
            self.assertIn("kanban_create refused", kua.refuse_unknown_assignee(COMPOSED))
            self.assertIsNone(kua.refuse_unknown_assignee(CLUSTER))

    def test_no_home_refuses_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(kua.refuse_unknown_assignee(COMPOSED))

    def test_a_data_root_inside_a_folder_named_profiles_is_still_the_root(self):
        root = self.root.parent / "outer" / kua.PROFILES_DIR / "data"
        profile(root / kua.PROFILES_DIR, "platform", user_md=False)
        self.assertEqual(kua.profiles_dir_for(root), root / kua.PROFILES_DIR)

    def test_inside_hermes_the_home_comes_from_hermes(self):
        """`hermes_constants.get_hermes_home` wins over the variable when it is importable."""
        fake = types.ModuleType("hermes_constants")
        fake.get_hermes_home = lambda: self.root / "platform"
        with mock.patch.dict(sys.modules, {"hermes_constants": fake}):
            with mock.patch.dict(os.environ, {kua.HOME_ENV: "/nowhere"}):
                self.assertIn("kanban_create refused", kua.refuse_unknown_assignee(COMPOSED))
                self.assertIsNone(kua.refuse_unknown_assignee(CLUSTER))


class TestTheConstantsAreTheirSources(unittest.TestCase):
    """The module is installed alone into the image, so it copies three names; these hold the copies."""

    def assert_constant(self, relative, name, value):
        source = (REPO / relative).read_text(encoding="utf-8")
        match = re.search(rf'^{name} = "([^"]+)"$', source, re.MULTILINE)
        self.assertIsNotNone(match, f"{relative} no longer declares {name}")
        self.assertEqual(match.group(1), value)

    def test_the_profile_marker(self):
        self.assert_constant("agents/platform/scripts/profile_scaffold.py", "PROFILE_MARKER", kua.PROFILE_MARKER)

    def test_the_file_a_cluster_agent_scaffold_writes_last(self):
        self.assert_constant("agents/platform/scripts/cluster_agent_profile.py", "USER_MD_NAME", kua.USER_MD_NAME)

    def test_the_cluster_agent_name_prefix(self):
        self.assert_constant("agents/chat/scripts/agent_roster.py", "CLUSTER_PROFILE_PREFIX", kua.CLUSTER_PROFILE_PREFIX)


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
    GATE = "_kanban_refuse_unknown_assignee(assignee)"
    SELF_COPY_GATE = "_kanban_refuse_self_copy(self_task, assignee, parents)"

    def setUp(self):
        import apply_kanban_self_copy as self_copy
        import apply_kanban_unknown_assignee as applier

        self.applier = applier
        self.self_copy = self_copy
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / applier.RELATIVE
        self.target.parent.mkdir(parents=True)
        self.target.write_text(self.SOURCE)

    def assert_gated(self, gate):
        import ast

        out = self.target.read_text()
        ast.parse(out)
        at = out.index(gate)
        self.assertLess(out.index("self_task = kb.get_task"), at)
        self.assertLess(at, out.index("kb.create_task"))

    def test_the_gate_lands_after_self_task_and_before_the_write(self):
        self.applier.apply(self.root)
        self.assert_gated(self.GATE)
        self.assertIn("from tools.kanban_unknown_assignee import", self.target.read_text())

    def test_it_applies_with_the_self_copy_gate_in_either_order(self):
        """Both appliers anchor on the same upstream line; neither may hide it from the other."""
        for first, second in ((self.self_copy, self.applier), (self.applier, self.self_copy)):
            self.target.write_text(self.SOURCE)
            first.apply(self.root)
            second.apply(self.root)
            self.assert_gated(self.GATE)
            self.assert_gated(self.SELF_COPY_GATE)

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
