"""Tests for the eval_run_tag plugin: registers its middleware only while the switch is on.

Run: python3 -m unittest agents/platform/plugins/eval_run_tag/test_eval_run_tag_plugin.py
"""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The module as the image installs it, hermes_cli/eval_run_tag.py.
eval_run_tag = _load("hermes_cli.eval_run_tag", REPO / "deploy" / "docker" / "patches" / "eval_run_tag.py")
plugin = _load("eval_run_tag_plugin", HERE / "__init__.py")


def setUpModule():
    package = types.ModuleType("hermes_cli")
    package.eval_run_tag = eval_run_tag
    patcher = mock.patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.eval_run_tag": eval_run_tag})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class RegisterTest(unittest.TestCase):
    def test_off_registers_nothing(self):
        ctx = mock.Mock()
        with mock.patch.dict(os.environ, {eval_run_tag.ENABLE_ENV: ""}):
            plugin.register(ctx)
        self.assertEqual(ctx.mock_calls, [])

    def test_on_registers_the_llm_request_middleware(self):
        ctx = mock.Mock()
        with mock.patch.dict(os.environ, {eval_run_tag.ENABLE_ENV: "1"}):
            plugin.register(ctx)
        ctx.register_middleware.assert_called_once_with("llm_request", eval_run_tag.on_llm_request)

    def test_a_missing_module_registers_nothing(self):
        ctx = mock.Mock()
        with mock.patch.object(plugin, "TAG_MODULES", ("no_such_module_here",)), \
                mock.patch.dict(os.environ, {eval_run_tag.ENABLE_ENV: "1"}):
            plugin.register(ctx)
        self.assertEqual(ctx.mock_calls, [])


class ManifestTest(unittest.TestCase):
    def test_the_manifest_declares_the_middleware(self):
        manifest = yaml.safe_load((HERE / "plugin.yaml").read_text())
        self.assertEqual(manifest["name"], "eval_run_tag")
        self.assertEqual(manifest["provides_middleware"], ["llm_request"])

    def test_the_default_platform_and_cluster_profiles_enable_it(self):
        for profile in ("chat", "platform", "cluster"):
            config = yaml.safe_load((REPO / "agents" / profile / "config.yaml").read_text())
            self.assertIn("eval_run_tag", config["plugins"]["enabled"], profile)


if __name__ == "__main__":
    unittest.main()
