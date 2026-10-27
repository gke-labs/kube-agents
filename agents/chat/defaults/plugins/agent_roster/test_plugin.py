"""Unit tests for the agent_roster plugin's pre_llm_call injection.

Run: python3 -m unittest agents/chat/defaults/plugins/agent_roster/test_plugin.py

The plugin loads agent_roster.py by path, so these tests build a real scripts/
directory containing the real module — the repository copy — rather than a
stub. That is the point: the injected block and the `list_agents` tool must
render the same fleet the same way, and a stub here would let them drift
silently.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import plugin  # noqa: E402

# .../agents/chat/defaults/plugins/agent_roster/test_plugin.py -> .../agents/chat
CHAT_AGENT_DIR = Path(__file__).resolve().parents[3]
ROSTER_SOURCE = CHAT_AGENT_DIR / "scripts" / "agent_roster.py"
# The image copies agents/platform/scripts beside agent_roster.py
# (deploy/docker/Dockerfile), and the roster reads the scope through them.
SCOPE_READER_SOURCES = [
    CHAT_AGENT_DIR.parent / "platform" / "scripts" / name
    for name in ("fleet_scope_targets.py", "fleet_scope_args.py")
]


class InjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        (self.data_dir / "scripts").mkdir()
        shutil.copy(ROSTER_SOURCE, self.data_dir / "scripts" / "agent_roster.py")
        for source in SCOPE_READER_SOURCES:
            shutil.copy(source, self.data_dir / "scripts" / source.name)
        self.profiles = self.data_dir / "profiles"
        self.profiles.mkdir()
        # The roster reads the host cluster env and the scope render; drop them
        # so a developer shell cannot leak into the block.
        self._env = mock.patch.dict("os.environ", {"HERMES_HOME": str(self.data_dir)})
        self._env.start()
        for name in ("GKE_CLUSTER_NAME", "GKE_PROJECT_ID", "GCP_PROJECT_ID", "GKE_LOCATION",
                     "KUBEAGENTS_SCOPE_FILE", "PLATFORM_AGENT_HOME"):
            os.environ.pop(name, None)
        # The module handle is cached for the process; a stale one from another
        # test would silently read the wrong tree.
        plugin._roster_module = None

    def tearDown(self):
        self._env.stop()
        plugin._roster_module = None
        self._tmp.cleanup()

    def _profile(self, name, capabilities=None):
        (self.profiles / name).mkdir()
        if capabilities is not None:
            (self.profiles / name / "CAPABILITIES.md").write_text(capabilities)

    def test_injects_the_roster(self):
        self._profile("default")
        self._profile("platform", "Fleet + GitOps write path.")
        self._profile("cluster-a", "Read-only diagnostics for cluster a.")

        result = plugin.handle_pre_llm_call()

        self.assertIsNotNone(result)
        context = result["context"]
        self.assertIn("- platform: Fleet + GitOps write path.", context)
        self.assertIn("- cluster-a: Read-only diagnostics for cluster a.", context)
        # The front door is not a delegation target.
        self.assertNotIn("- default:", context)
        # Named so the model can tell injected context from the user's words.
        self.assertIn("[SPECIALIST AGENTS AVAILABLE NOW]", context)
        # And told what to do with it, or the block is just trivia.
        self.assertIn("assignee", context)

    def test_says_so_when_there_are_no_specialists(self):
        # Silence would read as "the roster is unavailable"; the truthful
        # answer is that there is nobody to route to, which is what the front
        # door needs in order to say so rather than invent a target.
        self._profile("default")

        result = plugin.handle_pre_llm_call()

        self.assertIsNotNone(result)
        self.assertIn("No specialist agents", result["context"])

    def test_the_roster_is_re_read_every_turn(self):
        # A cluster agent scaffolded a minute ago has to appear on the next
        # message — that window is exactly when the user asks about it.
        self._profile("default")
        self._profile("platform", "Fleet work.")
        self.assertNotIn("cluster-new", plugin.handle_pre_llm_call()["context"])

        self._profile("cluster-new", "Diagnostics for the new cluster.")
        self.assertIn("cluster-new", plugin.handle_pre_llm_call()["context"])

    def test_missing_script_is_silent_not_fatal(self):
        # This hook runs ahead of every user turn on the front door: a raise
        # here is a Chat Agent that cannot answer at all.
        (self.data_dir / "scripts" / "agent_roster.py").unlink()
        with mock.patch.object(plugin, "_FALLBACK_SCRIPTS_DIR", self.data_dir / "nope"):
            self.assertIsNone(plugin.handle_pre_llm_call())

    def test_falls_back_to_the_image_defaults_directory(self):
        fallback = self.data_dir / "image-defaults"
        fallback.mkdir()
        shutil.copy(ROSTER_SOURCE, fallback / "agent_roster.py")
        (self.data_dir / "scripts" / "agent_roster.py").unlink()
        self._profile("default")
        self._profile("platform", "Fleet work.")

        with mock.patch.object(plugin, "_FALLBACK_SCRIPTS_DIR", fallback):
            result = plugin.handle_pre_llm_call()

        self.assertIn("- platform: Fleet work.", result["context"])

    def test_a_render_failure_is_silent_not_fatal(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        # Prime the cached module, then make rendering blow up.
        plugin.handle_pre_llm_call()
        with mock.patch.object(plugin._roster_module, "render", side_effect=OSError("PVC gone")):
            self.assertIsNone(plugin.handle_pre_llm_call())

    def test_a_missing_profiles_dir_still_reports_an_empty_fleet(self):
        # agent_roster degrades internally; assert the plugin does not undo that.
        # An absent directory is a knowable fact — there is nobody to route to.
        shutil.rmtree(self.profiles)
        self.assertIn("No specialist agents", plugin.handle_pre_llm_call()["context"])

    def test_an_empty_fleet_is_stated_without_the_pick_a_name_footer(self):
        # The footer points at "the names above". With no names above it, the
        # instruction can only be satisfied by inventing one.
        shutil.rmtree(self.profiles)
        context = plugin.handle_pre_llm_call()["context"]
        self.assertIn("No specialist agents", context)
        self.assertNotIn("verbatim as the `assignee`", context)

    def test_an_empty_fleet_with_a_host_section_still_drops_the_footer(self):
        # render() appends the host section after the empty-fleet sentence; the
        # footer must still stay off, since no assignee exists to copy.
        shutil.rmtree(self.profiles)
        env = {"GKE_CLUSTER_NAME": "h", "GKE_PROJECT_ID": "p", "GKE_LOCATION": "us-central1"}
        with mock.patch.dict(os.environ, env, clear=False):
            context = plugin.handle_pre_llm_call()["context"]
        self.assertIn("No specialist agents", context)
        self.assertIn("- cluster=h, project=p, location=us-central1", context)
        self.assertNotIn("verbatim as the `assignee`", context)
        self.assertNotIn("Host Cluster `assignee`", context)

    def test_a_populated_fleet_keeps_the_footer(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        context = plugin.handle_pre_llm_call()["context"]
        self.assertIn("verbatim as the `assignee`", context)
        # No host env, no host section, so no sentence pointing at one.
        self.assertNotIn("Host Cluster `assignee`", context)

    def test_host_heading_in_profile_description_does_not_trigger_host_footer(self):
        # Keying the host footer on free-text substring search would attach it
        # whenever a specialist's CAPABILITIES.md quoted the heading, even on a
        # kind install with no host section rendered.
        self._profile("default")
        self._profile(
            "platform",
            'Explains Host Cluster Identity ("this cluster" / "this host cluster"): see docs.',
        )
        context = plugin.handle_pre_llm_call()["context"]
        self.assertNotIn("Host Cluster `assignee`", context)

    def test_injects_host_and_fleet_scope_identity(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        self._profile(
            "cluster-kube-agents-demo-platform-agent-host-us-central1",
            "Read-only diagnostics for one GKE cluster.",
        )
        (
            self.profiles
            / "cluster-kube-agents-demo-platform-agent-host-us-central1"
            / "config.yaml"
        ).write_text(
            "cluster_identity:\n"
            "  cluster: platform-agent-host\n"
            "  location: us-central1\n"
            "  project: kube-agents-demo\n"
        )
        scope_file = self.data_dir / "scope.json"
        (self.data_dir / "fleet_scope.json").write_text(
            '{"boundary": true, "present": true, "declared": {"projects": ["gca-gke-2025"]}, '
            '"projects": [{"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", '
            '"via": ["management"]}, {"id": "gca-gke-2025", "state": "in-scope", '
            '"outcome": "ok", "via": ["projects"]}]}\n'
        )
        scope_file.write_text('{"present": true, "projects": ["gca-gke-2025"]}\n')
        env = {
            "GKE_CLUSTER_NAME": "platform-agent-host",
            "GKE_PROJECT_ID": "kube-agents-demo",
            "GKE_LOCATION": "us-central1",
            "KUBEAGENTS_SCOPE_FILE": str(scope_file),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            context = plugin.handle_pre_llm_call()["context"]

        self.assertIn('Host Cluster Identity ("this cluster" / "this host cluster"):', context)
        self.assertIn(
            "assignee=cluster-kube-agents-demo-platform-agent-host-us-central1",
            context,
        )
        self.assertIn("- GCP projects in scope (2): kube-agents-demo, gca-gke-2025", context)
        self.assertIn("Host Cluster `assignee`", context)

    def test_reloads_roster_module_when_script_mtime_changes(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        first = plugin.handle_pre_llm_call()["context"]
        self.assertIn("- platform: Fleet work.", first)

        script_path = self.data_dir / "scripts" / "agent_roster.py"
        script_path.write_text(
            'EMPTY_ROSTER = "EMPTY"\n'
            'def render(base=None):\n'
            '    return "- custom-reloaded: Reloaded from updated script."\n'
        )
        stat = script_path.stat()
        os.utime(script_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        second = plugin.handle_pre_llm_call()["context"]
        self.assertIn("- custom-reloaded: Reloaded from updated script.", second)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits this test relies on")
    def test_an_unreadable_profiles_dir_injects_nothing(self):
        # Not the same as an empty one. Announcing "no specialist agents are
        # available" on an I/O fault would stop the front door routing at all;
        # injecting nothing leaves it exactly where it was before this plugin —
        # able to reach for `list_agents`.
        self._profile("platform", "Fleet work.")
        self.profiles.chmod(0o000)
        try:
            self.assertIsNone(plugin.handle_pre_llm_call())
        finally:
            self.profiles.chmod(0o755)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits this test relies on")
    def test_an_unreadable_scripts_dir_is_silent_not_fatal(self):
        # pathlib swallows ENOENT but not EACCES, so even the is_file() probe for
        # the roster module can raise on the shared PVC. This hook runs ahead of
        # every user turn: a raise here is a Chat Agent that cannot answer at all.
        scripts = self.data_dir / "scripts"
        scripts.chmod(0o000)
        try:
            with mock.patch.object(plugin, "_FALLBACK_SCRIPTS_DIR", scripts):
                self.assertIsNone(plugin.handle_pre_llm_call())
        finally:
            scripts.chmod(0o755)


class RegistrationTest(unittest.TestCase):
    def test_registers_the_pre_llm_call_hook(self):
        ctx = mock.MagicMock()
        plugin.register(ctx)
        ctx.register_hook.assert_called_once_with("pre_llm_call", plugin.handle_pre_llm_call)


if __name__ == "__main__":
    unittest.main()
