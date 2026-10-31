"""Every plugin the chat profile ships is one the chat profile enables.

Hermes calls a plugin's register(ctx) only when the profile's plugins.enabled
names it, so a plugin directory under agents/chat/defaults/plugins/ that the
list in agents/chat/config.yaml omits is copied into every image and never
loaded, and nothing in the suite says so: the plugin's own tests call
register() directly. This holds the two together. The operator's own copy of
the list, frontDoorPlugins, is held to config.yaml by
TestFrontDoorPluginsMatchChatConfig in the operator's tests.
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHAT_CONFIG = _REPO_ROOT / "agents" / "chat" / "config.yaml"
_CHAT_PLUGINS = _REPO_ROOT / "agents" / "chat" / "defaults" / "plugins"
# A plugin is a directory with a plugin.yaml; a directory without one is a
# shared module (common/) and nothing Hermes loads.
_PLUGIN_MANIFEST = "plugin.yaml"


_REGISTER = "register"


def _exports_register(init_py: pathlib.Path) -> bool:
    """Whether the package's __init__.py binds the name `register` at top level: a
    def, an import of the name, or an assignment to it. Parsed, not searched, so a
    comment that mentions register() does not pass for the import it describes."""
    if not init_py.is_file():
        return False
    tree = ast.parse(init_py.read_text(), filename=str(init_py))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _REGISTER:
            return True
        if isinstance(node, ast.ImportFrom) and any((a.asname or a.name) == _REGISTER for a in node.names):
            return True
        # An annotation with no value (`register: Callable`) binds nothing at
        # runtime and does not count.
        if isinstance(node, ast.Assign) or (isinstance(node, ast.AnnAssign) and node.value is not None):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == _REGISTER for t in targets):
                return True
    return False


class ChatProfilePluginsTest(unittest.TestCase):
    def test_every_shipped_chat_plugin_exposes_register_from_its_package(self):
        """Hermes imports the plugin package and calls register() on it; a register() that
        lives only in a submodule loads as "Plugin 'x' has no register() function" and is
        never called. Static: the package's __init__.py defines or imports the name."""
        shipped = sorted(p.parent for p in _CHAT_PLUGINS.glob(f"*/{_PLUGIN_MANIFEST}"))
        self.assertTrue(shipped)
        without = [d.name for d in shipped if not _exports_register(d / "__init__.py")]
        self.assertEqual([], without, f"plugin package __init__.py neither defines nor imports register: {without}")

    def test_the_register_check_reads_the_code_and_not_the_comments(self):
        """A comment naming register() is the likely thing left behind when the import is
        forgotten, and it must not satisfy the check; a def, an import or an assignment does."""
        cases = {
            "# Hermes calls register() on plugin.py\n": False,
            "": False,
            '"""register lives in plugin.py"""\n': False,
            "from .plugin import register\n": True,
            "from .plugin import register as register\n": True,
            "def register(ctx):\n    pass\n": True,
            "from . import plugin\nregister = plugin.register\n": True,
            "register: object\n": False,
            "from . import plugin\nregister: object = plugin.register\n": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            for source, want in cases.items():
                init_py = pathlib.Path(tmp) / "__init__.py"
                init_py.write_text(source)
                self.assertEqual(want, _exports_register(init_py), source)
            self.assertFalse(_exports_register(pathlib.Path(tmp) / "missing" / "__init__.py"))

    def test_every_shipped_chat_plugin_is_enabled_on_the_chat_profile(self):
        enabled = (yaml.safe_load(_CHAT_CONFIG.read_text()) or {}).get("plugins", {}).get("enabled") or []
        self.assertTrue(enabled, f"plugins.enabled is gone from {_CHAT_CONFIG}; this test would pass against nothing")
        shipped = sorted(p.parent.name for p in _CHAT_PLUGINS.glob(f"*/{_PLUGIN_MANIFEST}"))
        self.assertTrue(shipped, f"no plugin under {_CHAT_PLUGINS}; this test would pass against nothing")
        missing = [name for name in shipped if name not in enabled]
        self.assertEqual([], missing, f"shipped under {_CHAT_PLUGINS} but not in {_CHAT_CONFIG} plugins.enabled: {missing}")


if __name__ == "__main__":
    unittest.main()
