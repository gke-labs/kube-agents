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


class InjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        (self.data_dir / "scripts").mkdir()
        shutil.copy(ROSTER_SOURCE, self.data_dir / "scripts" / "agent_roster.py")
        self.profiles = self.data_dir / "profiles"
        self.profiles.mkdir()
        self._env = mock.patch.dict(
            "os.environ",
            {
                "HERMES_HOME": str(self.data_dir),
                "LITELLM_BACKING_MODEL": "",
                "MODEL_PROVIDER": "",
                "MODEL_NAME": "",
                "LITELLM_MODEL_INFO_URL": "",
                "INFERENCE_GATEWAY_SERVICE_HOST": "",
            },
        )
        self._env.start()
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

    def test_a_populated_fleet_keeps_the_footer(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        self.assertIn("verbatim as the `assignee`", plugin.handle_pre_llm_call()["context"])

    def test_injects_runtime_model_identity_when_configured(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        with mock.patch.dict(os.environ, {"LITELLM_BACKING_MODEL": "vertex_ai/gemini-3.8-flash"}):
            context = plugin.handle_pre_llm_call()["context"]
        self.assertIn("[RUNTIME MODEL IDENTITY]", context)
        self.assertIn("Backing model: gemini-3.8-flash", context)
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", context)
        self.assertIn("[SPECIALIST AGENTS AVAILABLE NOW]", context)

    def test_injects_model_identity_alone_when_roster_is_unreadable(self):
        # Even if rendering the specialist roster raises or fails, the runtime
        # model identity must still be injected into context.
        self._profile("default")
        plugin.handle_pre_llm_call()  # prime module
        with mock.patch.object(plugin._roster_module, "render", return_value=None):
            with mock.patch.dict(os.environ, {"LITELLM_BACKING_MODEL": "vertex_ai/gemini-3.8-flash"}):
                result = plugin.handle_pre_llm_call()
        self.assertIsNotNone(result)
        context = result["context"]
        self.assertIn("[RUNTIME MODEL IDENTITY]", context)
        self.assertIn("Backing model: gemini-3.8-flash", context)
        self.assertNotIn("[SPECIALIST AGENTS AVAILABLE NOW]", context)

    def test_model_identity_render_failure_does_not_break_roster(self):
        self._profile("default")
        self._profile("platform", "Fleet work.")
        plugin.handle_pre_llm_call()  # prime module
        with mock.patch.object(plugin._roster_module, "render_model_identity", side_effect=RuntimeError("probe exploded")):
            result = plugin.handle_pre_llm_call()
        self.assertIsNotNone(result)
        context = result["context"]
        self.assertIn("- platform: Fleet work.", context)
        self.assertNotIn("[RUNTIME MODEL IDENTITY]", context)

    def test_returns_none_when_both_roster_and_model_identity_fail(self):
        plugin.handle_pre_llm_call()  # prime module
        with mock.patch.object(plugin._roster_module, "render", side_effect=OSError("PVC gone")):
            self.assertIsNone(plugin.handle_pre_llm_call())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits this test relies on")
    def test_an_unreadable_profiles_dir_injects_nothing_when_no_model_identity(self):
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
