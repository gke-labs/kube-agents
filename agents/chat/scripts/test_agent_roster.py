"""Tests for the shared specialist roster.

This logic used to live in ``router_server.py`` and be reachable only through
the ``list_agents`` MCP tool. It moved here when the roster started being
injected into every turn as well, so both consumers describe the same fleet in
the same words; the tests moved with it. ``test_router_server.py`` now only
checks that the tool still delegates here.
"""

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

# Add the directory containing agent_roster.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

import agent_roster  # noqa: E402


class TestDiscovery(unittest.TestCase):
    """The roster enumerates every profile except the front door itself."""

    def _with_profiles(self, tmp, names):
        base = Path(tmp) / "profiles"
        for name in names:
            (base / name).mkdir(parents=True)
        agent_roster.PROFILES_BASE = base
        return base

    def test_excludes_default_and_lists_specialists(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["default", "platform", "cluster-a"])
            # A CAPABILITIES.md is the preferred description source.
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            # SOUL.md is the fallback when no CAPABILITIES.md exists.
            (base / "cluster-a" / "SOUL.md").write_text("# Title\n\nRead-only cluster diagnostics.\n")

            out = agent_roster.render()
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- cluster-a: Read-only cluster diagnostics.", out)
            self.assertNotIn("default", out)

    def test_empty_when_no_specialists(self):
        with TemporaryDirectory() as tmp:
            self._with_profiles(tmp, ["default"])
            self.assertIn("No specialist agents", agent_roster.render())

    def test_an_explicit_base_overrides_the_module_default(self):
        # The injecting plugin passes its own resolved path rather than relying
        # on the module picking up the right HERMES_HOME at import time.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "elsewhere"
            (base / "platform").mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet work.")
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("- platform: Fleet work.", agent_roster.render(base))


class TestSharedRoleGrouping(unittest.TestCase):
    """Agents with an identical description are stated once, not repeated per agent.

    Every Cluster Agent is scaffolded from the same template, so a fleet of N
    clusters otherwise repeats one CAPABILITIES.md verbatim N times — the bulk of
    what the front door reads on every single delegation.
    """

    # Sized like the real Cluster Agent CAPABILITIES.md (~885 bytes), because the
    # win depends on it: the grouped form costs a fixed ~76-char preamble, so for a
    # short description and a small fleet it is actually *longer* than one line per
    # agent. Break-even is roughly (N-1) x len(desc) > 76.
    FLEET = ("Read-only diagnostics for one GKE cluster. "
             + "Scoped to a single cluster; no write paths. " * 19).strip()

    def _fleet(self, tmp):
        base = Path(tmp) / "profiles"
        for name in ("default", "platform", "cluster-a", "cluster-b", "cluster-c"):
            (base / name).mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
        for name in ("cluster-a", "cluster-b", "cluster-c"):
            (base / name / "CAPABILITIES.md").write_text(self.FLEET)
        agent_roster.PROFILES_BASE = base

    def test_shared_description_stated_once(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            out = agent_roster.render()

            # The expensive part — the repeated blob — appears exactly once...
            self.assertEqual(out.count(self.FLEET), 1)
            # ...while every cluster is still individually addressable as an assignee.
            for name in ("cluster-a", "cluster-b", "cluster-c"):
                self.assertIn(f"  - {name}", out)

    def test_unique_specialist_kept_inline_and_ordered_first(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            out = agent_roster.render()

            # A one-off specialist stays on a single `- name: desc` line.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            # Distinct specialists sort ahead of shared-role fleets: the front door
            # routes to a named specialist far more often than to a given cluster.
            self.assertLess(out.index("- platform:"), out.index("share one role"))

    def test_grouping_shrinks_the_roster(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            grouped = agent_roster.render()

        # Compare against what the un-grouped one-line-per-agent form would cost.
        ungrouped = "\n".join(
            ["- platform: Fleet + GitOps write path."]
            + [f"- {n}: {self.FLEET}" for n in ("cluster-a", "cluster-b", "cluster-c")]
        )
        self.assertLess(len(grouped), len(ungrouped))

    def test_undescribed_agents_are_not_a_shared_role(self):
        # Two profiles nothing is known about have no role in common. Grouping on
        # the placeholder would file them under "pick the one whose cluster you
        # need" — a claim about interchangeability the roster has no basis for.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "mystery-a", "mystery-b"):
                (base / name).mkdir(parents=True)
            agent_roster.PROFILES_BASE = base

            out = agent_roster.render()

            self.assertIn("- mystery-a: (no description provided)", out)
            self.assertIn("- mystery-b: (no description provided)", out)
            self.assertNotIn("share one role", out)


@unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits these tests rely on")
class TestDiscoveryDegradesOnIOError(unittest.TestCase):
    """Discovery must degrade, never raise.

    It backs both the Chat Agent's routing tool and the block injected into
    every turn, so a raise here is a front door that cannot route at all.
    pathlib swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so `is_dir()`/`is_file()`/
    `iterdir()` on the shared PVC raise PermissionError (EACCES) for real.
    """

    @contextmanager
    def _locked(self, path):
        """Make `path` unreadable, restoring the mode so TemporaryDirectory can clean up."""
        original = path.stat().st_mode
        path.chmod(0o000)
        try:
            yield
        finally:
            path.chmod(original)

    def test_unreadable_profile_costs_only_that_agent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "platform", "broken"):
                (base / name).mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            agent_roster.PROFILES_BASE = base

            with self._locked(base / "broken"):
                out = agent_roster.render()

            # The healthy specialist still routes; the unreadable one is listed
            # without a description rather than taking down the whole roster.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- broken: (no description provided)", out)

    def test_unreadable_profiles_base_is_unknown_not_empty(self):
        # "I could not read the fleet" must never be rendered as "there is no
        # fleet": the front door would stop routing and tell the user there is
        # nobody to route to. render() returns None so the injecting plugin says
        # nothing and the model falls back to `list_agents`.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            (base / "platform").mkdir(parents=True)
            agent_roster.PROFILES_BASE = base

            with self._locked(base):
                self.assertIsNone(agent_roster.render())

    def test_missing_profiles_base_returns_empty_roster(self):
        # A directory that is absent, unlike one that cannot be read, is a fact:
        # pathlib's is_dir() swallows ENOENT, so this is a genuinely empty fleet.
        with TemporaryDirectory() as tmp:
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("No specialist agents", agent_roster.render())


if __name__ == "__main__":
    unittest.main()
