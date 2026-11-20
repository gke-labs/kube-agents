"""The gateway's caller-auth hook, and the chart's and kustomize base's wiring of it.

`charts/kube-agents/files/litellm_caller_auth.py` is LiteLLM's
`general_settings.custom_auth`. These tests drive it with a stand-in for
LiteLLM's `UserAPIKeyAuth` (the real one is not installed here): the master
key is the admin, a key in the digest map is its class, no key or a
placeholder is `keyless`, anything else is a 401, and no raw key reaches the
returned object, the exception or the log. The render cases need `helm` and
skip without it.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml
from fastapi import HTTPException

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "litellm_caller_auth.py"
_BASE = _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "litellm" / "base"
_ANTHROPIC_OVERLAY = _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "litellm" / "overlays" / "anthropic"
_KEY = "litellm_caller_auth.py"
_MOUNT = "/app/litellm_caller_auth.py"
_CUSTOM_AUTH = "litellm_caller_auth.caller_auth"
_MASTER_KEY_REF = "os.environ/LITELLM_MASTER_KEY"
_MASTER_SECRET = "litellm-master-key"
_KEYS_SECRET = "litellm-caller-keys"
_PERMIT_ENV = "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY"
_HELM_BASE_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
    "-s",
    "templates/litellm.yaml",
]

# What callers send today, each found in the caller's own code.
_CALLER_PLACEHOLDERS = {"none", "a2a-playground"}
# The routes callers use: the agents' and Hindsight's OpenAI clients, the
# curator and decision memory (/chat/completions under /v1), the worker's
# Claude Code (/v1/messages, its count_tokens), the web console (/model/info,
# /v1/chat/completions), Hermes's model listing (/v1/models).
_CALLER_ROUTES = {
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/models",
    "/models",
    "/model/info",
    "/v1/model/info",
    "/health",
}
_MASTER = "sk-master-0123456789abcdefghijklmnopqrstuvwxyz0123"
_AGENT_KEY = "agent-key-not-sk-prefixed-0123456789abcdef"
_OTHER_KEY = "AIzaSyNotARealKeyButShapedLikeOne0123456789"


class _StubUserAPIKeyAuth:
    """LiteLLM's UserAPIKeyAuth, as far as the hook relies on it: it hashes an
    api_key only when it starts with sk-, and stores anything else as given."""

    def __init__(self, **fields):
        api_key = fields.get("api_key")
        if isinstance(api_key, str) and api_key.startswith("sk-"):
            fields["api_key"] = hashlib.sha256(api_key.encode()).hexdigest()
        self.fields = fields
        for name, value in fields.items():
            setattr(self, name, value)


def _litellm_installed() -> bool:
    module = sys.modules.get("litellm")
    if module is not None:
        # Another test file's stand-in has no spec; a real import does.
        return getattr(module, "__spec__", None) is not None and getattr(module, "__file__", None) is not None
    return importlib.util.find_spec("litellm") is not None


_HAVE_LITELLM = _litellm_installed()


def _load_hook():
    """Import the hook, with stand-ins for the LiteLLM modules it imports when LiteLLM is absent."""
    if not _HAVE_LITELLM:
        litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
        constants = types.ModuleType("litellm.constants")
        constants.LITELLM_PROXY_MASTER_KEY_ALIAS = "litellm_proxy_master_key"
        constants.LITELLM_PROXY_ADMIN_NAME = "default_user_id"
        proxy = types.ModuleType("litellm.proxy")
        proxy_types = types.ModuleType("litellm.proxy._types")
        proxy_types.UserAPIKeyAuth = _StubUserAPIKeyAuth
        proxy_types.LitellmUserRoles = types.SimpleNamespace(PROXY_ADMIN="proxy_admin", INTERNAL_USER="internal_user")
        sys.modules.update(
            {
                "litellm": litellm,
                "litellm.constants": constants,
                "litellm.proxy": proxy,
                "litellm.proxy._types": proxy_types,
            }
        )
    spec = importlib.util.spec_from_file_location("litellm_caller_auth", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class _Request:
    def __init__(self, path: str):
        self.scope = {"type": "http", "path": path}


class _HookTestCase(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys_file = pathlib.Path(self.tmp.name) / "keys.json"
        env = mock.patch.dict(
            os.environ,
            {self.hook.MASTER_KEY_ENV: _MASTER, self.hook.CALLER_KEYS_FILE_ENV: str(self.keys_file)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.log = logging.getLogger("kube_agents.litellm_caller_auth")

    def write_map(self, entries: dict, bump_ns: int = 0) -> None:
        self.keys_file.write_text(json.dumps(entries))
        if bump_ns:
            stat = self.keys_file.stat()
            os.utime(self.keys_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + bump_ns))

    def call(self, key, path="/v1/chat/completions"):
        return asyncio.run(self.hook.caller_auth(_Request(path), key))

    def refused(self, key, path="/v1/chat/completions") -> HTTPException:
        with self.assertRaises(HTTPException) as caught:
            self.call(key, path)
        return caught.exception


class TestWhoIsAdmitted(_HookTestCase):
    def test_a_known_key_is_its_class_under_its_digest(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        token = self.call(_AGENT_KEY)
        self.assertEqual(token.api_key, _digest(_AGENT_KEY))
        self.assertEqual(token.key_alias, "agent-gateway")
        self.assertEqual(token.team_id, "agent-gateway")
        self.assertEqual(token.team_alias, "agent-gateway")
        self.assertEqual(token.user_role, "internal_user")
        self.assertEqual(set(token.allowed_routes), _CALLER_ROUTES)

    def test_an_sk_prefixed_known_key_is_still_stored_as_its_digest(self):
        key = "sk-" + _AGENT_KEY
        self.write_map({_digest(key): "profile-read"})
        token = self.call(key)
        self.assertEqual(token.api_key, _digest(key))
        self.assertEqual(token.key_alias, "profile-read")

    def test_the_master_key_is_the_admin_under_litellms_alias(self):
        token = self.call(_MASTER, path="/key/generate")
        self.assertEqual(token.user_role, "proxy_admin")
        self.assertEqual(token.api_key, "litellm_proxy_master_key")
        self.assertEqual(token.user_id, "default_user_id")
        self.assertFalse(getattr(token, "allowed_routes", None))

    def test_no_key_and_an_empty_key_are_keyless(self):
        for key in (None, ""):
            with self.subTest(key=key):
                token = self.call(key)
                self.assertEqual(token.api_key, "keyless")
                self.assertEqual(token.key_alias, "keyless")
                self.assertEqual(token.team_id, "keyless")
                self.assertEqual(set(token.allowed_routes), _CALLER_ROUTES)

    def test_every_placeholder_a_caller_sends_is_keyless(self):
        self.assertEqual(set(self.hook.PLACEHOLDER_KEYS), _CALLER_PLACEHOLDERS)
        for key in sorted(_CALLER_PLACEHOLDERS):
            with self.subTest(key=key):
                token = self.call(key, path="/v1/messages")
                self.assertEqual(token.api_key, "keyless")
                self.assertEqual(token.key_alias, "keyless")

    def test_an_unknown_key_is_refused_401(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        for key in (_OTHER_KEY, "sk-unknown", _AGENT_KEY + "x", _MASTER[:-1]):
            with self.subTest(key=key):
                self.assertEqual(self.refused(key).status_code, 401)

    def test_a_missing_map_knows_no_key_and_keyless_still_works(self):
        self.assertFalse(self.keys_file.exists())
        self.assertEqual(self.refused(_AGENT_KEY).status_code, 401)
        self.assertEqual(self.call(None).key_alias, "keyless")

    def test_an_empty_map_is_valid(self):
        self.write_map({})
        self.assertEqual(self.refused(_AGENT_KEY).status_code, 401)
        self.assertEqual(self.call("none").key_alias, "keyless")

    def test_with_no_master_key_nothing_is_admin(self):
        with mock.patch.dict(os.environ, {self.hook.MASTER_KEY_ENV: ""}):
            self.assertEqual(self.call("").key_alias, "keyless")
            self.assertEqual(self.call(None).key_alias, "keyless")
            self.assertEqual(self.refused(_MASTER).status_code, 401)
        with mock.patch.dict(os.environ):
            del os.environ[self.hook.MASTER_KEY_ENV]
            self.assertEqual(self.refused(_MASTER).status_code, 401)


class TestRoutes(_HookTestCase):
    def test_the_hook_opens_exactly_the_caller_routes(self):
        self.assertEqual(set(self.hook.CALLER_ROUTES), _CALLER_ROUTES)

    def test_a_caller_route_is_open_to_keyless_and_known_callers(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        for path in sorted(_CALLER_ROUTES):
            with self.subTest(path=path):
                self.assertEqual(self.call(None, path).key_alias, "keyless")
                self.assertEqual(self.call(_AGENT_KEY, path).key_alias, "agent-gateway")

    def test_other_routes_are_refused_401_to_keyless_and_known_callers(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        # Paths under /health and /v1/messages that LiteLLM's prefix-matching
        # allowed_routes check would admit, and admin routes.
        for path in ("/health/test_connection", "/health/drain", "/health/services", "/v1/messages/batches",
                     "/key/generate", "/config/update", "/model/new", "/spend/logs", "/"):
            with self.subTest(path=path):
                for key in (None, "none", _AGENT_KEY):
                    refusal = self.refused(key, path)
                    self.assertEqual(refusal.status_code, 401)
                    self.assertEqual(refusal.detail, self.hook.REFUSED_ROUTE_DETAIL)

    def test_the_admin_reaches_any_route(self):
        for path in ("/key/generate", "/health/test_connection", "/v1/chat/completions"):
            with self.subTest(path=path):
                self.assertEqual(self.call(_MASTER, path).user_role, "proxy_admin")


class TestMapReload(_HookTestCase):
    def test_a_changed_map_is_read_without_a_restart(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        self.assertEqual(self.call(_AGENT_KEY).key_alias, "agent-gateway")
        rotated = "rotated-" + _AGENT_KEY
        self.write_map({_digest(rotated): "agent-gateway"}, bump_ns=1_000_000)
        self.assertEqual(self.call(rotated).key_alias, "agent-gateway")
        self.assertEqual(self.refused(_AGENT_KEY).status_code, 401)

    def test_a_removed_map_drops_every_key(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        self.assertEqual(self.call(_AGENT_KEY).key_alias, "agent-gateway")
        self.keys_file.unlink()
        self.assertEqual(self.refused(_AGENT_KEY).status_code, 401)

    def test_an_unchanged_map_is_not_reparsed(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        self.call(_AGENT_KEY)
        with mock.patch.object(self.hook, "_read_map", side_effect=AssertionError("re-read")):
            self.assertEqual(self.call(_AGENT_KEY).key_alias, "agent-gateway")

    def test_a_map_that_does_not_parse_keeps_the_last_one(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        self.call(_AGENT_KEY)
        with self.assertLogs(self.log, level="ERROR"):
            self.keys_file.write_text("{not json")
            stat = self.keys_file.stat()
            os.utime(self.keys_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertEqual(self.call(_AGENT_KEY).key_alias, "agent-gateway")

    def test_entries_that_are_not_a_digest_and_a_class_are_dropped(self):
        self.write_map(
            {
                _digest(_AGENT_KEY): "agent-gateway",
                _OTHER_KEY: "pasted-key",  # a key where a digest belongs
                _digest(_OTHER_KEY).upper(): "upper-hex",
                _digest("a"): "keyless",  # a key cannot pass for no key
                _digest("b"): "litellm_proxy_master_key",
                _digest("c"): "Bad Class",
                _digest("d"): 7,
            }
        )
        with self.assertLogs(self.log, level="WARNING") as logs:
            self.assertEqual(self.call(_AGENT_KEY).key_alias, "agent-gateway")
        self.assertNotIn(_OTHER_KEY, "\n".join(logs.output))
        for key in (_OTHER_KEY, "a", "b", "c", "d"):
            with self.subTest(key=key):
                self.assertEqual(self.refused(key).status_code, 401)


class TestNoRawKeyLeaves(_HookTestCase):
    """LiteLLM puts api_key into the hashed_api_key label unhashed unless it starts
    with sk-, and records the presented key on any refusal that is not a 401."""

    def _values(self, token) -> str:
        fields = token.fields if hasattr(token, "fields") else token.model_dump()
        return repr(sorted((name, repr(value)) for name, value in fields.items()))

    def test_no_admitted_token_carries_the_raw_key(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        self.assertNotIn(_AGENT_KEY, self._values(self.call(_AGENT_KEY)))
        self.assertNotIn(_MASTER, self._values(self.call(_MASTER)))
        self.assertNotIn(_MASTER[len("sk-"):], self._values(self.call(_MASTER)))

    def test_refusals_carry_no_key_and_no_cause(self):
        self.write_map({_digest(_AGENT_KEY): "agent-gateway"})
        for key, path in ((_OTHER_KEY, "/v1/chat/completions"), (_AGENT_KEY, "/key/generate")):
            with self.subTest(path=path):
                refusal = self.refused(key, path)
                self.assertEqual(refusal.status_code, 401)
                self.assertNotIn(key, str(refusal.detail))
                self.assertIsNone(refusal.__cause__)

    def test_an_unexpected_error_becomes_a_plain_401(self):
        # A KeyError's message is its key: the worst case for a log line.
        with mock.patch.object(self.hook._caller_keys, "lookup", side_effect=KeyError(_OTHER_KEY)):
            with self.assertLogs(self.log, level="ERROR") as logs:
                refusal = self.refused(_OTHER_KEY)
        self.assertEqual(refusal.status_code, 401)
        self.assertEqual(refusal.detail, self.hook.REFUSED_KEY_DETAIL)
        self.assertIsNone(refusal.__cause__)
        self.assertTrue(refusal.__suppress_context__)
        self.assertNotIn(_OTHER_KEY, "\n".join(logs.output))

    def test_nothing_the_hook_logs_carries_a_key(self):
        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        self.log.addHandler(handler)
        self.addCleanup(self.log.removeHandler, handler)
        self.write_map({_digest(_AGENT_KEY): "agent-gateway", _OTHER_KEY: "x"})
        self.call(_AGENT_KEY)
        self.call(_MASTER)
        self.refused(_OTHER_KEY)
        self.keys_file.write_text("{broken " + _OTHER_KEY)
        stat = self.keys_file.stat()
        os.utime(self.keys_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.refused(_OTHER_KEY)
        self.assertTrue(records)
        for secret_value in (_AGENT_KEY, _OTHER_KEY, _MASTER):
            self.assertNotIn(secret_value, "\n".join(records))


class TestBothPathsShipIt(unittest.TestCase):
    def test_the_kustomize_copy_is_the_chart_file(self):
        self.assertEqual((_BASE / _KEY).read_text(), _CHART_FILE.read_text())

    def test_the_kustomize_configs_name_the_hook_and_open_metrics(self):
        for directory in (_BASE, _ANTHROPIC_OVERLAY):
            with self.subTest(config=str(directory)):
                config = yaml.safe_load((directory / "config.yaml").read_text())
                self.assertEqual(config["general_settings"], {"master_key": _MASTER_KEY_REF, "custom_auth": _CUSTOM_AUTH})
                self.assertIs(config["litellm_settings"]["require_auth_for_metrics_endpoint"], False)

    def test_the_kustomize_base_ships_and_mounts_it(self):
        kustomization = yaml.safe_load((_BASE / "kustomization.yaml").read_text())
        files = next(g["files"] for g in kustomization["configMapGenerator"] if g["name"] == "litellm-config")
        self.assertIn(_KEY, files)
        deployment = yaml.safe_load((_BASE / "deployment.yaml").read_text())
        spec = deployment["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, container["volumeMounts"])
        hook = _load_hook()
        keys_mount = next(m for m in container["volumeMounts"] if m["name"] == "caller-keys")
        self.assertEqual(keys_mount["mountPath"], os.path.dirname(hook.DEFAULT_CALLER_KEYS_FILE))
        self.assertNotIn("subPath", keys_mount)
        volume = next(v for v in spec["volumes"] if v["name"] == "caller-keys")
        self.assertEqual(volume["secret"]["secretName"], _KEYS_SECRET)
        self.assertTrue(volume["secret"]["optional"])
        env = {e["name"]: e for e in container["env"]}
        ref = env["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"]
        self.assertEqual((ref["name"], ref["key"], ref["optional"]), (_MASTER_SECRET, "LITELLM_MASTER_KEY", True))
        # Kustomize cannot generate the Secret, so this base boots without it.
        self.assertEqual(env[_PERMIT_ENV]["value"], "true")


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    def _documents(self, extra_args: list[str]) -> list[dict]:
        proc = subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    def test_every_render_ships_names_and_mounts_the_hook(self):
        hook = _load_hook()
        for args in (
            [],
            ["--set", "litellm.otel=true"],
            ["--set-string", "litellm.modelProvider=anthropic"],
            ["--set-string", "litellm.modelProvider=vertex_ai", "--set-string", "litellm.modelDefaultName=claude-opus-5"],
            ["--set", "litellm.redaction.enabled=true"],
        ):
            with self.subTest(args=args):
                documents = self._documents(args)
                configmap = next(d for d in documents if d["kind"] == "ConfigMap")
                self.assertEqual(configmap["data"][_KEY], _CHART_FILE.read_text())
                config = yaml.safe_load(configmap["data"]["config.yaml"])
                self.assertEqual(config["general_settings"], {"master_key": _MASTER_KEY_REF, "custom_auth": _CUSTOM_AUTH})
                self.assertIs(config["litellm_settings"]["require_auth_for_metrics_endpoint"], False)
                deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "litellm")
                spec = deployment["spec"]["template"]["spec"]
                container = spec["containers"][0]
                self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, container["volumeMounts"])
                keys_mount = next(m for m in container["volumeMounts"] if m["name"] == "caller-keys")
                self.assertEqual(keys_mount["mountPath"], os.path.dirname(hook.DEFAULT_CALLER_KEYS_FILE))
                self.assertNotIn("subPath", keys_mount)
                volume = next(v for v in spec["volumes"] if v["name"] == "caller-keys")
                self.assertEqual(volume["secret"]["secretName"], _KEYS_SECRET)
                self.assertTrue(volume["secret"]["optional"])

    def test_the_master_key_comes_from_the_generated_secret_and_the_permit_is_gone(self):
        documents = self._documents([])
        secret = next(d for d in documents if d["kind"] == "Secret" and d["metadata"]["name"] == _MASTER_SECRET)
        value = secret["stringData"]["LITELLM_MASTER_KEY"]
        self.assertTrue(value.startswith("sk-"))
        self.assertGreaterEqual(len(value), 48)
        deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "litellm")
        env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertNotIn(_PERMIT_ENV, env)
        ref = env["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"]
        self.assertEqual((ref["name"], ref["key"]), (_MASTER_SECRET, "LITELLM_MASTER_KEY"))
        self.assertNotIn("optional", ref)
        configmap = next(d for d in documents if d["kind"] == "ConfigMap")
        self.assertNotIn(value, json.dumps(configmap))

    def test_the_hook_source_feeds_the_rollout_checksum(self):
        # A subPath ConfigMap mount never updates in place; only the checksum rolls the pods.
        template = (_CHART / "templates" / "litellm.yaml").read_text()
        line = next(l for l in template.splitlines() if l.startswith("{{- $checksumInput :="))
        self.assertIn("$callerAuthSource", line)
        self.assertEqual(line.count("%s"), len(re.findall(r" \$[A-Za-z]+", line.split('"')[-1])))


if __name__ == "__main__":
    unittest.main()
