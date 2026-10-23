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

    def test_excludes_dot_prefixed_archive_directories(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["platform", "tse"])
            (base / "platform" / "SOUL.md").write_text("# Platform Agent\n\nManages fleet GitOps.\n")
            (base / "tse" / "SOUL.md").write_text("# GKE Support Agent (TSE)\n\nTriages L1 GKE support incidents.\n")
            archived = base / ".deleted" / "tse-20261021T000000Z"
            archived.mkdir(parents=True)
            (archived / "SOUL.md").write_text("# Archived Profile\n\nShould not appear in roster.\n")

            discovered = agent_roster.discover(base)
            self.assertIsNotNone(discovered)
            self.assertEqual([entry["name"] for entry in discovered], ["platform", "tse"])

    def test_a_cluster_agent_shows_the_cluster_it_targets(self):
        # #3082: the assignee id is not a name to show a person, and the cluster
        # name cannot be read back out of it. The scaffold's cluster_identity
        # block (yaml.safe_dump, sort_keys=False) supplies it.
        with TemporaryDirectory() as tmp:
            name = "cluster-my-proj-ka-today-2-northamerica-northeast1"
            base = self._with_profiles(tmp, ["platform", name])
            (base / name / "CAPABILITIES.md").write_text("Read-only cluster diagnostics.")
            (base / name / "config.yaml").write_text(
                "model: x\ncluster_identity:\n  project: my-proj\n  cluster: ka-today-2\n"
                "  location: northamerica-northeast1\ncontext_file_max_chars: 100000\n"
            )
            out = agent_roster.render()
            self.assertIn(
                f"- {name} (the ka-today-2 cluster agent; project my-proj, location northamerica-northeast1): "
                "Read-only cluster diagnostics.",
                out,
            )

    def test_the_identity_note_rides_each_name_in_a_shared_role(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["cluster-a", "cluster-b"])
            for n, c in (("cluster-a", "alpha"), ("cluster-b", "beta")):
                (base / n / "CAPABILITIES.md").write_text("Shared template.")
                (base / n / "config.yaml").write_text(f"cluster_identity:\n  cluster: {c}\n")
            out = agent_roster.render()
            self.assertIn("  - cluster-a (the alpha cluster agent)\n", out)
            self.assertIn("  - cluster-b (the beta cluster agent)\n", out)

    def test_no_identity_no_note(self):
        # No config.yaml, no block, an empty cluster, or the block ending before
        # a cluster key: the line is as before.
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["p1", "p2", "p3", "p4"])
            (base / "p2" / "config.yaml").write_text("model: x\n")
            (base / "p3" / "config.yaml").write_text("cluster_identity:\n  cluster: ''\n")
            (base / "p4" / "config.yaml").write_text("cluster_identity:\n  project: x\nother: y\n  cluster: nope\n")
            for n in ("p1", "p2", "p3", "p4"):
                (base / n / "CAPABILITIES.md").write_text(f"role {n}")
            out = agent_roster.render()
            for n in ("p1", "p2", "p3", "p4"):
                self.assertIn(f"- {n}: role {n}", out)
            self.assertNotIn("cluster agent", out)

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

    def _render_from_gateway(self, backing, alias="model-default", env=None):
        """Render with the gateway's model info naming ``backing`` for ``alias``."""
        payload = {"data": [{"model_name": alias, "litellm_params": {"model": backing}}]}
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        environ = {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1", **(env or {})}
        with mock.patch.dict(os.environ, environ, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=fake_resp):
                return agent_roster.render_model_identity()

    def test_renders_backing_model_from_gateway(self):
        out = self._render_from_gateway("vertex_ai/gemini-3.8-flash")
        self.assertIsNotNone(out)
        self.assertIn("Backing model: gemini-3.8-flash", out)
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", out)
        self.assertNotIn("model-default", out)

    def test_hermes_agent_alias_entry_is_read_too(self):
        out = self._render_from_gateway("anthropic/claude-opus-5", alias="hermes-agent")
        self.assertIn("Backing model: claude-opus-5", out)
        self.assertIn("Provider: Anthropic (anthropic)", out)

    def test_omits_provider_line_for_an_unmapped_litellm_handler(self):
        # openai_compatible installs render every alias as hosted_vllm/<model>.
        # The served id keeps its own spelling: vLLM ids are case-sensitive.
        out = self._render_from_gateway("hosted_vllm/meta-llama/Llama-3.1-8B-Instruct")
        self.assertIsNotNone(out)
        self.assertIn("Backing model: meta-llama/Llama-3.1-8B-Instruct", out)
        self.assertNotIn("Provider:", out)

    def test_provider_key_is_matched_casefolded(self):
        out = self._render_from_gateway("Vertex_AI/gemini-3.8-flash")
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", out)

    def test_filters_out_placeholder_names_in_any_case(self):
        for backing in (
            "custom/model-default",
            "custom/hermes-agent",
            "model-default",
            "Model-Default",
            "custom/HERMES-AGENT",
            "custom",
            "openai/Custom",
        ):
            agent_roster._next_gateway_probe_at = 0.0
            with self.subTest(backing=backing):
                self.assertIsNone(self._render_from_gateway(backing))

    def test_env_overrides_are_not_a_source(self):
        # Nothing in the chart, operator or installer sets these on the agent
        # container; the gateway is the one source of the backing model.
        env = {
            "LITELLM_BACKING_MODEL": "anthropic/claude-opus-5",
            "MODEL_PROVIDER": "anthropic",
            "MODEL_NAME": "claude-opus-5",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(agent_roster.render_model_identity())
        out = self._render_from_gateway("vertex_ai/gemini-3.8-flash", env=env)
        self.assertIn("Backing model: gemini-3.8-flash", out)
        self.assertNotIn("claude", out)

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

    def test_returns_none_without_a_gateway(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("urllib.request.urlopen") as mocked_open:
                self.assertIsNone(agent_roster.render_model_identity())
        mocked_open.assert_not_called()

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

    def test_logs_when_model_info_names_no_alias_or_is_not_an_object(self):
        no_alias = {"data": [{"model_name": "gemini-only", "litellm_params": {"model": "vertex_ai/gemini-3.8-flash"}}]}
        cases = ((json.dumps(no_alias).encode("utf-8"), "entry with a backing model"), (b"[]", "not a JSON object"))
        for body, expected in cases:
            agent_roster._next_gateway_probe_at = 0.0
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = body
            with mock.patch.dict(os.environ, {"INFERENCE_GATEWAY_SERVICE_HOST": "10.0.0.1"}, clear=True):
                with mock.patch("urllib.request.urlopen", return_value=resp):
                    with mock.patch.object(agent_roster, "log") as mocked_log:
                        self.assertIsNone(agent_roster.render_model_identity())
            mocked_log.assert_called_once()
            self.assertIn(expected, mocked_log.call_args[0][0])
            self.assertGreater(agent_roster._next_gateway_probe_at, 0.0)


if __name__ == "__main__":
    unittest.main()
