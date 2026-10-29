"""Unit tests for the shared model_identity plugin (#3295)."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

import model_identity  # noqa: E402


class TestModelIdentityPlugin(unittest.TestCase):
    def setUp(self) -> None:
        model_identity._reset_cache()

    def tearDown(self) -> None:
        model_identity._reset_cache()

    def _mock_resp(self, payload: object) -> mock.MagicMock:
        resp = mock.MagicMock()
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value.read.return_value = raw
        return resp

    def test_split_backing_extracts_provider_and_model(self) -> None:
        self.assertEqual(
            model_identity._split_backing("vertex_ai/gemini-3.1-pro-preview"),
            ("vertex_ai", "gemini-3.1-pro-preview"),
        )
        self.assertEqual(
            model_identity._split_backing("anthropic/claude-sonnet-4-5"),
            ("anthropic", "claude-sonnet-4-5"),
        )
        self.assertEqual(
            model_identity._split_backing("Vertex_AI/gemini-3.1-flash"),
            ("vertex_ai", "gemini-3.1-flash"),
        )
        self.assertEqual(
            model_identity._split_backing("Llama-3.1-8B-Instruct"),
            ("", "Llama-3.1-8B-Instruct"),
        )

    def test_split_backing_rejects_router_aliases_in_any_case(self) -> None:
        for alias in (
            "",
            "   ",
            "model-default",
            "Model-Default",
            "hermes-agent",
            "custom/model-default",
            "custom/HERMES-AGENT",
            "custom",
            "openai/Custom",
        ):
            self.assertEqual(model_identity._split_backing(alias), ("", ""), alias)

    def test_render_model_identity_with_vendor_provider(self) -> None:
        block = model_identity.render_model_identity("vertex_ai/gemini-3.1-pro-preview")
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("[RUNTIME MODEL IDENTITY]", block)
        self.assertIn("Backing model: gemini-3.1-pro-preview (Gemini 3.1 Pro Preview)", block)
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", block)
        self.assertNotIn("not exposed", block)

    def test_render_model_identity_omits_provider_for_self_hosted_or_unprefixed(self) -> None:
        for backing, expected_model in (
            ("hosted_vllm/meta-llama/Llama-3.1-8B-Instruct", "meta-llama/Llama-3.1-8B-Instruct"),
            ("openai_compatible/google/gemma-3-27b-it", "google/gemma-3-27b-it"),
            ("custom/google/gemma-3-27b-it", "google/gemma-3-27b-it"),
            ("Llama-3.1-8B-Instruct", "Llama-3.1-8B-Instruct"),
        ):
            with self.subTest(backing=backing):
                block = model_identity.render_model_identity(backing)
                self.assertIsNotNone(block)
                assert block is not None
                self.assertIn(f"Backing model: {expected_model}\n", block)
                self.assertNotIn("Provider:", block)
                self.assertIn(
                    "and if asked about the provider, say that provider details are not exposed in your active environment",
                    block,
                )

    def test_render_model_identity_renders_raw_provider_slug_when_not_in_label_table(self) -> None:
        block = model_identity.render_model_identity("sap/anthropic--claude-4.5-sonnet")
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("Backing model: anthropic--claude-4.5-sonnet\nProvider: sap\n", block)

    def test_env_overrides_are_not_a_source(self) -> None:
        env = {
            "LITELLM_BACKING_MODEL": "anthropic/claude-opus-5",
            "MODEL_PROVIDER": "anthropic",
            "MODEL_NAME": "claude-opus-5",
            "AGENT_MODEL_NAME": "anthropic/claude-opus-5",
            "MODEL_DEFAULT_NAME": "anthropic/claude-opus-5",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(model_identity, "_load_profile_endpoint", return_value=("", "")):
                self.assertIsNone(model_identity.render_model_identity())

    def test_load_profile_endpoint_reads_hermes_config_base_url_and_key(self) -> None:
        fake_cfg = {
            "model": {
                "default": "model-default",
                "provider": "custom",
                "base_url": "http://platform-agent-litellm.kube-agents.svc:4000/v1",
                "api_key": "sk-test-token",
            }
        }
        fake_hermes_cfg = mock.MagicMock()
        fake_hermes_cfg.load_config.return_value = fake_cfg
        with mock.patch.dict(
            sys.modules,
            {"hermes_cli": mock.MagicMock(), "hermes_cli.config": fake_hermes_cfg},
        ):
            info_url, api_key = model_identity._load_profile_endpoint()
        self.assertEqual(
            info_url,
            "http://platform-agent-litellm.kube-agents.svc:4000/v1/model/info",
        )
        self.assertEqual(api_key, "sk-test-token")

    def test_load_profile_endpoint_falls_back_to_hermes_home_yaml_and_strips_none_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "config.yaml"
            cfg_file.write_text(
                "model:\n"
                "  default: model-default\n"
                "  provider: custom\n"
                "  base_url: http://inference-gateway.kube-agents.svc:8000\n"
                "  api_key: none\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"HERMES_HOME": tmpdir, "HERMES_MANAGED_DIR": ""},
                clear=True,
            ):
                with mock.patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.config": None}):
                    info_url, api_key = model_identity._load_profile_endpoint()
        self.assertEqual(
            info_url,
            "http://inference-gateway.kube-agents.svc:8000/v1/model/info",
        )
        self.assertEqual(api_key, "")

    def test_probe_gateway_model_queries_base_url_and_passes_bearer_token(self) -> None:
        payload = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "vertex_ai/gemini-3.1-pro-preview"},
                }
            ]
        }
        captured = {}

        def _fake_open(req: urllib.request.Request, timeout: float = 0.5):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["timeout"] = timeout
            return self._mock_resp(payload)

        with mock.patch.object(model_identity._MODEL_INFO_OPENER, "open", side_effect=_fake_open):
            resolved = model_identity._probe_gateway_model(
                base_url="http://10.0.0.1:4000/v1",
                api_key="litellm-master-secret",
            )
        self.assertEqual(resolved, "vertex_ai/gemini-3.1-pro-preview")
        self.assertEqual(captured["url"], "http://10.0.0.1:4000/v1/model/info")
        self.assertEqual(captured["auth"], "Bearer litellm-master-secret")

    def test_prefers_model_default_when_hermes_agent_is_listed_first(self) -> None:
        payload = {
            "data": [
                {"model_name": "hermes-agent", "litellm_params": {"model": "anthropic/claude-opus-5"}},
                {"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-pro-preview"}},
            ]
        }
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://10.0.0.1/v1/model/info", "")
        ):
            with mock.patch.object(model_identity._MODEL_INFO_OPENER, "open", return_value=self._mock_resp(payload)):
                out = model_identity.render_model_identity()
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Backing model: gemini-3.1-pro-preview", out)
        self.assertNotIn("claude-opus-5", out)

    def test_reprobes_after_ttl_and_picks_up_switched_model(self) -> None:
        payload1 = {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-flash"}}]}
        payload2 = {"data": [{"model_name": "model-default", "litellm_params": {"model": "anthropic/claude-opus-5"}}]}
        current_time = 1000.0
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://10.0.0.1/v1/model/info", "")
        ):
            with mock.patch("time.monotonic", side_effect=lambda: current_time):
                with mock.patch.object(
                    model_identity._MODEL_INFO_OPENER,
                    "open",
                    side_effect=[self._mock_resp(payload1), self._mock_resp(payload2)],
                ) as mocked_open:
                    out1 = model_identity.render_model_identity()
                    self.assertIn("gemini-3.1-flash", out1 or "")
                    self.assertEqual(mocked_open.call_count, 1)

                    current_time += 100.0
                    out2 = model_identity.render_model_identity()
                    self.assertIn("gemini-3.1-flash", out2 or "")
                    self.assertEqual(mocked_open.call_count, 1)

                    current_time += 250.0
                    out3 = model_identity.render_model_identity()
                    self.assertIn("claude-opus-5", out3 or "")
                    self.assertEqual(mocked_open.call_count, 2)

    def test_reprobe_failure_retains_cached_model_and_enters_cooldown(self) -> None:
        payload1 = {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-flash"}}]}
        current_time = 1000.0
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://10.0.0.1/v1/model/info", "")
        ):
            with mock.patch("time.monotonic", side_effect=lambda: current_time):
                with mock.patch.object(
                    model_identity._MODEL_INFO_OPENER,
                    "open",
                    side_effect=[self._mock_resp(payload1), OSError("probe failed")],
                ) as mocked_open:
                    out1 = model_identity.render_model_identity()
                    self.assertIn("gemini-3.1-flash", out1 or "")
                    self.assertEqual(mocked_open.call_count, 1)

                    current_time += 350.0
                    out2 = model_identity.render_model_identity()
                    self.assertIn("gemini-3.1-flash", out2 or "")
                    self.assertEqual(mocked_open.call_count, 2)

                    current_time += 10.0
                    out3 = model_identity.render_model_identity()
                    self.assertIn("gemini-3.1-flash", out3 or "")
                    self.assertEqual(mocked_open.call_count, 2)

    def test_reprobe_without_qualifying_alias_clears_cached_model(self) -> None:
        seeded = {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-pro-preview"}}]}
        no_alias = {"data": [{"model_name": "gpt-4o", "litellm_params": {"model": "openai/gpt-4o"}}]}
        restored = {"data": [{"model_name": "model-default", "litellm_params": {"model": "openai/gpt-4o"}}]}
        current_time = 1000.0
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://10.0.0.1/v1/model/info", "")
        ):
            with mock.patch("time.monotonic", side_effect=lambda: current_time):
                with mock.patch.object(
                    model_identity._MODEL_INFO_OPENER,
                    "open",
                    side_effect=[self._mock_resp(seeded), self._mock_resp(no_alias), self._mock_resp(restored)],
                ) as mocked_open:
                    self.assertIn("gemini-3.1-pro-preview", model_identity.render_model_identity() or "")
                    current_time += 350.0
                    self.assertIsNone(model_identity.render_model_identity())
                    current_time += 10.0
                    self.assertIsNone(model_identity.render_model_identity())
                    self.assertEqual(mocked_open.call_count, 2)
                    current_time += 25.0
                    self.assertIn("gpt-4o", model_identity.render_model_identity() or "")
                    self.assertEqual(mocked_open.call_count, 3)

    def test_probe_bypasses_container_http_proxy(self) -> None:
        body = json.dumps(
            {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-flash"}}]}
        ).encode("utf-8")

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/model/info"
            live_env = {
                "http_proxy": "http://127.0.0.1:9",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": ".svc,.cluster.local",
            }
            with mock.patch.dict(os.environ, live_env, clear=True):
                with self.assertRaises(urllib.error.URLError):
                    urllib.request.urlopen(url, timeout=0.5)
                with mock.patch.object(
                    model_identity, "_load_profile_endpoint", return_value=(url, "")
                ):
                    out = model_identity.render_model_identity()
            self.assertIsNotNone(out)
            self.assertIn("Backing model: gemini-3.1-flash", out or "")
        finally:
            server.server_close()

    def test_register_and_handle_pre_llm_call(self) -> None:
        ctx = mock.MagicMock()
        model_identity.register(ctx)
        ctx.register_hook.assert_called_once_with("pre_llm_call", model_identity.handle_pre_llm_call)

        with mock.patch.object(
            model_identity, "_probe_gateway_model", return_value="vertex_ai/gemini-3.1-pro-preview"
        ):
            res = model_identity.handle_pre_llm_call()
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("[RUNTIME MODEL IDENTITY]", res["system_prompt"])
        self.assertIn("gemini-3.1-pro-preview", res["system_prompt"])


if __name__ == "__main__":
    unittest.main()
