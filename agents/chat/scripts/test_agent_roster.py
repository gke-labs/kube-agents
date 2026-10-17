"""Tests for the shared specialist roster.

This logic used to live in ``router_server.py`` and be reachable only through
the ``list_agents`` MCP tool. It moved here when the roster started being
injected into every turn as well, so both consumers describe the same fleet in
the same words; the tests moved with it. ``test_router_server.py`` now only
checks that the tool still delegates here.
"""

import json
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

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


class TestModelIdentity(unittest.TestCase):
    """Backing model and provider resolution for #3295."""

    def setUp(self):
        agent_roster._cached_gateway_model = ""
        agent_roster._cached_gateway_model_expires_at = 0.0
        agent_roster._next_gateway_probe_at = 0.0

    def tearDown(self):
        agent_roster._cached_gateway_model = ""
        agent_roster._cached_gateway_model_expires_at = 0.0
        agent_roster._next_gateway_probe_at = 0.0

    def test_renders_backing_model_from_env(self):
        with mock.patch.dict(os.environ, {"LITELLM_BACKING_MODEL": "vertex_ai/gemini-3.8-flash"}, clear=True):
            out = agent_roster.render_model_identity()
        self.assertIsNotNone(out)
        self.assertIn("Backing model: gemini-3.8-flash", out)
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", out)
        self.assertNotIn("model-default", out)

    def test_renders_from_provider_and_model_name_env(self):
        with mock.patch.dict(
            os.environ,
            {"MODEL_PROVIDER": "anthropic", "MODEL_NAME": "claude-opus-5"},
            clear=True,
        ):
            out = agent_roster.render_model_identity()
        self.assertIsNotNone(out)
        self.assertIn("Backing model: claude-opus-5", out)
        self.assertIn("Provider: Anthropic (anthropic)", out)

    def test_renders_from_model_name_alone_without_provider(self):
        with mock.patch.dict(os.environ, {"MODEL_NAME": "gemini-3.8-flash"}, clear=True):
            out = agent_roster.render_model_identity()
        self.assertIsNotNone(out)
        self.assertIn("Backing model: gemini-3.8-flash", out)
        self.assertNotIn("Provider:", out)

    def test_filters_out_placeholder_router_aliases(self):
        for alias in ("model-default", "hermes-agent"):
            with mock.patch.dict(os.environ, {"LITELLM_BACKING_MODEL": f"custom/{alias}"}, clear=True):
                self.assertIsNone(agent_roster.render_model_identity())
            with mock.patch.dict(os.environ, {"MODEL_NAME": alias}, clear=True):
                self.assertIsNone(agent_roster.render_model_identity())
            with mock.patch.dict(os.environ, {"LITELLM_BACKING_MODEL": alias}, clear=True):
                self.assertIsNone(agent_roster.render_model_identity())

    def test_resolves_from_litellm_model_info_endpoint(self):
        payload = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "vertex_ai/gemini-3.8-flash"},
                }
            ]
        }
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        with mock.patch.dict(os.environ, {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=fake_resp) as mocked_open:
                first = agent_roster.render_model_identity()
                second = agent_roster.render_model_identity()
        self.assertEqual(mocked_open.call_count, 1)
        self.assertEqual(first, second)
        self.assertIn("Backing model: gemini-3.8-flash", first)
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", first)

    def test_returns_none_when_unset_or_alias_only(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(agent_roster.render_model_identity())
        with mock.patch.dict(os.environ, {"LITELLM_BACKING_MODEL": "custom/model-default"}, clear=True):
            self.assertIsNone(agent_roster.render_model_identity())

    def test_falls_back_to_gateway_when_env_holds_placeholder_alias(self):
        payload = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "vertex_ai/gemini-3.8-flash"},
                }
            ]
        }
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "LITELLM_BACKING_MODEL": "custom/model-default",
                "INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1",
            },
            clear=True,
        ):
            with mock.patch("urllib.request.urlopen", return_value=fake_resp) as mocked_open:
                out = agent_roster.render_model_identity()
        self.assertEqual(mocked_open.call_count, 1)
        self.assertIn("Backing model: gemini-3.8-flash", out)

    def test_reprobes_after_ttl_and_picks_up_switched_model(self):
        payload1 = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "vertex_ai/gemini-3.8-flash"},
                }
            ]
        }
        payload2 = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "anthropic/claude-opus-5"},
                }
            ]
        }
        resp1 = mock.MagicMock()
        resp1.__enter__.return_value.read.return_value = json.dumps(payload1).encode("utf-8")
        resp2 = mock.MagicMock()
        resp2.__enter__.return_value.read.return_value = json.dumps(payload2).encode("utf-8")

        current_time = 1000.0
        with mock.patch.dict(os.environ, {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1"}, clear=True):
            with mock.patch("time.monotonic", side_effect=lambda: current_time):
                with mock.patch("urllib.request.urlopen", side_effect=[resp1, resp2]) as mocked_open:
                    # Initial probe
                    out1 = agent_roster.render_model_identity()
                    self.assertIn("gemini-3.8-flash", out1)
                    self.assertEqual(mocked_open.call_count, 1)

                    # Within TTL: returns cached
                    current_time += 100.0
                    out2 = agent_roster.render_model_identity()
                    self.assertIn("gemini-3.8-flash", out2)
                    self.assertEqual(mocked_open.call_count, 1)

                    # Past TTL: reprobes and updates
                    current_time += 250.0  # total +350s > 300s TTL
                    out3 = agent_roster.render_model_identity()
                    self.assertIn("claude-opus-5", out3)
                    self.assertIn("Anthropic (anthropic)", out3)
                    self.assertEqual(mocked_open.call_count, 2)

    def test_reprobe_failure_retains_cached_model_and_enters_cooldown(self):
        payload1 = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "vertex_ai/gemini-3.8-flash"},
                }
            ]
        }
        resp1 = mock.MagicMock()
        resp1.__enter__.return_value.read.return_value = json.dumps(payload1).encode("utf-8")

        current_time = 1000.0
        with mock.patch.dict(os.environ, {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1"}, clear=True):
            with mock.patch("time.monotonic", side_effect=lambda: current_time):
                with mock.patch("urllib.request.urlopen", side_effect=[resp1, OSError("probe failed")]) as mocked_open:
                    # Initial probe succeeds
                    out1 = agent_roster.render_model_identity()
                    self.assertIn("gemini-3.8-flash", out1)
                    self.assertEqual(mocked_open.call_count, 1)

                    # TTL expires, re-probe fails with OSError
                    current_time += 350.0
                    out2 = agent_roster.render_model_identity()
                    self.assertIn("gemini-3.8-flash", out2)  # retains last good model
                    self.assertEqual(mocked_open.call_count, 2)

                    # In failure cooldown: does not retry urlopen
                    current_time += 10.0  # within 30s cooldown
                    out3 = agent_roster.render_model_identity()
                    self.assertIn("gemini-3.8-flash", out3)
                    self.assertEqual(mocked_open.call_count, 2)

    def test_degrades_and_respects_cooldown_when_gateway_fails_or_returns_non_object(self):
        with mock.patch.dict(os.environ, {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1"}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=OSError("unreachable")) as mocked_open:
                self.assertIsNone(agent_roster.render_model_identity())
                self.assertIsNone(agent_roster.render_model_identity())
        self.assertEqual(mocked_open.call_count, 1)

        agent_roster._next_gateway_probe_at = 0.0
        bad_resp = mock.MagicMock()
        bad_resp.__enter__.return_value.read.return_value = b"[]"
        with mock.patch.dict(os.environ, {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=bad_resp) as mocked_bad:
                self.assertIsNone(agent_roster.render_model_identity())
                self.assertIsNone(agent_roster.render_model_identity())
        self.assertEqual(mocked_bad.call_count, 1)


if __name__ == "__main__":
    unittest.main()
