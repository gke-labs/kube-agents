"""Unit tests for the shared model_identity plugin (#3295)."""

from __future__ import annotations

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

    def test_load_profile_endpoint_expands_env_ref_and_strips_placeholder_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "config.yaml"
            cfg_file.write_text(
                "model:\n"
                "  default: model-default\n"
                "  provider: custom\n"
                "  base_url: http://inference-gateway.kube-agents.svc.cluster.local/v1\n"
                "  api_key: ${LITELLM_API_KEY}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.config": None}):
                with mock.patch.dict(
                    os.environ,
                    {"HERMES_MANAGED_DIR": tmpdir, "LITELLM_API_KEY": "caller-key-123"},
                    clear=True,
                ):
                    info_url, api_key = model_identity._load_profile_endpoint()
                self.assertEqual(
                    info_url,
                    "http://inference-gateway.kube-agents.svc.cluster.local/v1/model/info",
                )
                self.assertEqual(api_key, "caller-key-123")

                # Unset env ref must stay empty (not echo literal ${...} or unrelated OPENAI_API_KEY).
                with mock.patch.dict(
                    os.environ,
                    {"HERMES_MANAGED_DIR": tmpdir, "OPENAI_API_KEY": "unrelated-key"},
                    clear=True,
                ):
                    _, unset_key = model_identity._load_profile_endpoint()
                self.assertEqual(unset_key, "")

        self.assertEqual(
            model_identity._normalize_endpoint("http://inference-gateway/v1", "no-key-required"),
            ("http://inference-gateway/v1/model/info", ""),
        )

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
            thread.join(timeout=2.0)

    def test_probe_uses_model_info_litellm_provider_fallback(self) -> None:
        payload = {
            "data": [
                {
                    "model_name": "model-default",
                    "litellm_params": {"model": "gpt-4o"},
                    "model_info": {"litellm_provider": "openai"},
                }
            ]
        }
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://inference-gateway/v1/model/info", "")
        ), mock.patch.object(model_identity._MODEL_INFO_OPENER, "open", return_value=self._mock_resp(payload)):
            out = model_identity.render_model_identity()
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Backing model: gpt-4o (GPT-4o)", out)
        self.assertIn("Provider: OpenAI (openai)", out)

    def test_probe_rejects_non_http_scheme_and_oversized_response(self) -> None:
        with mock.patch.object(model_identity._MODEL_INFO_OPENER, "open") as mock_open:
            self.assertEqual(model_identity._probe_gateway_model(base_url="file:///etc/passwd"), "")
            mock_open.assert_not_called()

        oversized = b"x" * (model_identity._MAX_RESPONSE_BYTES + 10)
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://inference-gateway/v1/model/info", "")
        ), mock.patch.object(model_identity._MODEL_INFO_OPENER, "open", return_value=self._mock_resp(oversized)):
            self.assertEqual(model_identity._probe_gateway_model(), "")

    def test_missing_endpoint_sets_cooldown_and_clears_expired_cache(self) -> None:
        payload = {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-pro"}}]}
        now = [1000.0]
        with mock.patch.object(model_identity.time, "monotonic", side_effect=lambda: now[0]):
            with mock.patch.object(
                model_identity, "_load_profile_endpoint", return_value=("http://inference-gateway/v1/model/info", "")
            ), mock.patch.object(model_identity._MODEL_INFO_OPENER, "open", return_value=self._mock_resp(payload)):
                self.assertEqual(model_identity._probe_gateway_model(), "vertex_ai/gemini-3.1-pro")
            now[0] += model_identity._PROBE_SUCCESS_CACHE_TTL_SECS + 1.0
            with mock.patch.object(model_identity, "_load_profile_endpoint", return_value=("", "")) as mock_ep:
                self.assertEqual(model_identity._probe_gateway_model(), "")
                self.assertEqual(model_identity._probe_gateway_model(), "")
                self.assertEqual(mock_ep.call_count, 1)

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
        self.assertEqual(set(res.keys()), {"context"})
        self.assertTrue(res["context"].startswith("\n\n[RUNTIME MODEL IDENTITY]\n"))
        self.assertIn("gemini-3.1-pro-preview", res["context"])

    def test_handle_pre_llm_call_fails_open_on_empty_or_exception(self) -> None:
        with mock.patch.object(model_identity, "render_model_identity", return_value=None):
            self.assertIsNone(model_identity.handle_pre_llm_call())
        with mock.patch.object(model_identity, "render_model_identity", side_effect=RuntimeError("boom")):
            self.assertIsNone(model_identity.handle_pre_llm_call())

    def test_format_display_model_joins_minor_version_digits_with_dot(self) -> None:
        self.assertEqual(model_identity._format_display_model("claude-sonnet-4-5"), "Claude Sonnet 4.5")
        self.assertEqual(
            model_identity._format_display_model("claude-3-7-sonnet-20250219"),
            "Claude 3.7 Sonnet 20250219",
        )
        self.assertEqual(
            model_identity._format_display_model("gemini-3.1-pro-preview"),
            "Gemini 3.1 Pro Preview",
        )
        block = model_identity.render_model_identity("anthropic/claude-sonnet-4-5")
        self.assertIsNotNone(block)
        self.assertIn("Backing model: claude-sonnet-4-5 (Claude Sonnet 4.5)", block or "")

    def test_cloud_resource_paths_and_arns_strip_project_and_account_ids(self) -> None:
        vertex_ep = "vertex_ai/projects/secret-host-proj/locations/us-central1/endpoints/987654321"
        self.assertEqual(model_identity._split_backing(vertex_ep), ("vertex_ai", "987654321"))
        block_ep = model_identity.render_model_identity(vertex_ep) or ""
        self.assertIn("Backing model: 987654321", block_ep)
        self.assertNotIn("secret-host-proj", block_ep)

        garden = "vertex_ai/publishers/google/models/gemini-2.5-pro"
        self.assertEqual(model_identity._split_backing(garden), ("vertex_ai", "gemini-2.5-pro"))
        block_garden = model_identity.render_model_identity(garden) or ""
        self.assertIn("Backing model: gemini-2.5-pro (Gemini 2.5 Pro)", block_garden)
        self.assertIn("Provider: Google Cloud Vertex AI (vertex_ai)", block_garden)

        bedrock_arn = (
            "bedrock/arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-5-v1:0"
        )
        self.assertEqual(
            model_identity._split_backing(bedrock_arn),
            ("bedrock", "us.anthropic.claude-sonnet-4-5-v1:0"),
        )
        block_arn = model_identity.render_model_identity(bedrock_arn) or ""
        self.assertNotIn("123456789012", block_arn)

        # Bare resource paths without a provider prefix are refused so project/account IDs never leak.
        self.assertEqual(
            model_identity._split_backing("projects/secret-host-proj/locations/us-central1/endpoints/987654321"),
            ("", ""),
        )
        self.assertEqual(
            model_identity._split_backing("arn:aws:bedrock:us-east-1:123456789012:inference-profile/foo"),
            ("", ""),
        )

    def test_extract_entry_model_honors_litellm_provider_for_org_prefixed_models(self) -> None:
        entry_vllm = {
            "model_name": "model-default",
            "litellm_params": {"model": "google/gemma-3-27b-it"},
            "model_info": {"litellm_provider": "hosted_vllm"},
        }
        self.assertEqual(
            model_identity._extract_entry_model(entry_vllm),
            "hosted_vllm/google/gemma-3-27b-it",
        )
        block = model_identity.render_model_identity(model_identity._extract_entry_model(entry_vllm)) or ""
        self.assertIn("Backing model: google/gemma-3-27b-it\n", block)
        self.assertNotIn("Provider:", block)

        # LiteLLM sub-provider suffixes (e.g. vertex_ai-language-models) do not double-prefix.
        entry_vertex = {
            "model_name": "model-default",
            "litellm_params": {"model": "vertex_ai/gemini-3.1-pro-preview"},
            "model_info": {"litellm_provider": "vertex_ai-language-models"},
        }
        self.assertEqual(
            model_identity._extract_entry_model(entry_vertex),
            "vertex_ai/gemini-3.1-pro-preview",
        )

    def test_probe_does_not_follow_redirects_or_leak_bearer_token(self) -> None:
        redirect_hits = []

        class _RedirectTarget(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                redirect_hits.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def log_message(self, *_args) -> None:
                return

        target_srv = HTTPServer(("127.0.0.1", 0), _RedirectTarget)
        target_thread = threading.Thread(target=target_srv.handle_request, daemon=True)
        target_thread.start()

        target_url = f"http://127.0.0.1:{target_srv.server_port}/stolen"

        class _Redirector(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", target_url)
                self.end_headers()

            def log_message(self, *_args) -> None:
                return

        redir_srv = HTTPServer(("127.0.0.1", 0), _Redirector)
        redir_thread = threading.Thread(target=redir_srv.handle_request, daemon=True)
        redir_thread.start()
        try:
            resolved = model_identity._probe_gateway_model(
                base_url=f"http://127.0.0.1:{redir_srv.server_port}/v1",
                api_key="agent-bearer-token",
            )
            self.assertEqual(resolved, "")
            self.assertEqual(redirect_hits, [])
        finally:
            redir_srv.server_close()
            redir_thread.join(timeout=2.0)
            target_srv.server_close()

    def test_non_dict_json_clears_cached_model_and_sets_cooldown(self) -> None:
        seeded = {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-3.1-pro"}}]}
        now = [1000.0]
        with mock.patch.object(model_identity.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://10.0.0.1/v1/model/info", "")
        ), mock.patch.object(
            model_identity._MODEL_INFO_OPENER,
            "open",
            side_effect=[self._mock_resp(seeded), self._mock_resp(["not-a-dict"])],
        ) as mocked_open:
            self.assertEqual(model_identity._probe_gateway_model(), "vertex_ai/gemini-3.1-pro")
            now[0] += model_identity._PROBE_SUCCESS_CACHE_TTL_SECS + 1.0
            self.assertEqual(model_identity._probe_gateway_model(), "")
            # Still inside retry cooldown -> second call does not re-probe.
            self.assertEqual(model_identity._probe_gateway_model(), "")
            self.assertEqual(mocked_open.call_count, 2)

    def test_stale_probe_does_not_overwrite_newer_cache_generation(self) -> None:
        payload_old = {"data": [{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/old-model"}}]}
        payload_new = {"data": [{"model_name": "model-default", "litellm_params": {"model": "anthropic/new-model"}}]}
        first_entered = threading.Event()
        release_first = threading.Event()
        calls = 0

        def _slow_then_fast(req: urllib.request.Request, timeout: float = 0.5):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                release_first.wait(timeout=2.0)
                return self._mock_resp(payload_old)
            return self._mock_resp(payload_new)

        results: list[str] = []
        with mock.patch.object(
            model_identity, "_load_profile_endpoint", return_value=("http://10.0.0.1/v1/model/info", "")
        ), mock.patch.object(model_identity._MODEL_INFO_OPENER, "open", side_effect=_slow_then_fast):
            t1 = threading.Thread(target=lambda: results.append(model_identity._probe_gateway_model()))
            t1.start()
            self.assertTrue(first_entered.wait(timeout=2.0))
            fast_res = model_identity._probe_gateway_model()
            release_first.set()
            t1.join(timeout=2.0)

        self.assertEqual(fast_res, "anthropic/new-model")
        self.assertEqual(results, ["anthropic/new-model"])
        self.assertEqual(model_identity._cached_gateway_model, "anthropic/new-model")

    def test_load_profile_endpoint_env_fallbacks(self) -> None:
        with mock.patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.config": None}):
            with mock.patch.dict(
                os.environ,
                {"OPENAI_BASE_URL": "http://gateway.local:4000/v1", "LITELLM_API_KEY": "env-key-1"},
                clear=True,
            ):
                self.assertEqual(
                    model_identity._load_profile_endpoint(),
                    ("http://gateway.local:4000/v1/model/info", "env-key-1"),
                )
            with mock.patch.dict(
                os.environ,
                {"HERMES_BASE_URL": "http://hermes-gw.local:4000", "OPENAI_API_KEY": "env-key-2"},
                clear=True,
            ):
                self.assertEqual(
                    model_identity._load_profile_endpoint(),
                    ("http://hermes-gw.local:4000/v1/model/info", "env-key-2"),
                )

    def test_plugin_manifest_and_all_profile_configs_enable_model_identity(self) -> None:
        import yaml

        repo_root = _PLUGIN_DIR.parents[3]
        manifest = yaml.safe_load((_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "model_identity")
        self.assertIn("pre_llm_call", manifest["provides_hooks"])

        for profile in ("chat", "platform", "cluster", "tse"):
            cfg = yaml.safe_load((repo_root / "agents" / profile / "config.yaml").read_text(encoding="utf-8"))
            self.assertIn("model_identity", cfg["plugins"]["enabled"], profile)


if __name__ == "__main__":
    unittest.main()
