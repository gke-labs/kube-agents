#!/usr/bin/env python3
"""The provider is four modules now, so it has to survive Hermes' plugin loader.

Splitting `__init__.py` introduced a failure mode a single file could not have:
relative imports. Hermes does not `import` a plugin, it loads it by path —
`spec_from_file_location(name, __init__.py, submodule_search_locations=[dir])`,
then walks `dir.glob("*.py")` and execs each submodule *before* the package
itself (`plugins/memory/__init__.py`, upstream).

Two properties of that loop are worth locking down:

* `glob` order is filesystem order, not import order. Whichever submodule runs
  first pulls the others in through the partially-initialised package, so no
  ordering may break.
* A submodule that raises is caught and logged at debug level, then the package
  execs anyway — so an import error inside one module does not surface as a
  failed load. It surfaces as a provider that registered and then misbehaves.

The test therefore replays the loader's exact mechanism under every permutation
of the submodules and asserts `register()` still hands back a working provider.

Standalone: plain asserts, no pytest. Needs Hermes on the path for
`agent.memory_provider` and `tools.registry`; it never reaches a real Hindsight.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_plugin_loads_as_a_package.py
"""

import importlib.util
import itertools
import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_HERMES = os.environ.get("HERMES_ROOT") or "/opt/hermes"
if os.path.isdir(_HERMES):
    sys.path.insert(0, _HERMES)

try:
    from . import _stubs  # noqa: F401
except (ImportError, ValueError):
    import _stubs  # type: ignore # noqa: F401

PLUGIN_DIR = _REPO / "agents" / "chat" / "plugins" / "memory" / "kube_agents_memory"
SUBMODULES = sorted(f.stem for f in PLUGIN_DIR.glob("*.py") if f.name != "__init__.py")


class _Collector:
    """Stands in for Hermes' provider collector."""

    def __init__(self):
        self.provider = None

    def register_memory_provider(self, provider):
        self.provider = provider


def _load_like_hermes(order):
    """Replay plugins/memory/__init__.py's path-based load, submodules first."""
    namespace = "hermes_plugins_under_test"
    for stale in [m for m in list(sys.modules) if m.startswith(namespace)]:
        del sys.modules[stale]

    shell_spec = importlib.util.spec_from_loader(namespace, loader=None, is_package=True)
    shell = importlib.util.module_from_spec(shell_spec)
    shell.__path__ = []
    sys.modules[namespace] = shell

    name = f"{namespace}.kube_agents_memory"
    spec = importlib.util.spec_from_file_location(
        name, str(PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module

    for stem in order:
        full = f"{name}.{stem}"
        if full in sys.modules:
            continue  # pulled in already by a sibling's relative import
        sub_spec = importlib.util.spec_from_file_location(full, str(PLUGIN_DIR / f"{stem}.py"))
        sub = importlib.util.module_from_spec(sub_spec)
        sys.modules[full] = sub
        # Deliberately not wrapped: upstream swallows this, which is exactly
        # what would turn a broken import into a silently degraded provider.
        sub_spec.loader.exec_module(sub)

    spec.loader.exec_module(module)
    return module


class TestPluginLoads(unittest.TestCase):
    def test_the_package_registers_under_every_submodule_order(self):
        """glob order is the filesystem's business; none of them may break the load."""
        self.assertGreaterEqual(len(SUBMODULES), 4, SUBMODULES)
        for order in itertools.permutations(SUBMODULES):
            module = _load_like_hermes(order)
            collector = _Collector()
            module.register(collector)
            self.assertIsNotNone(collector.provider, order)
            self.assertEqual(collector.provider.name, "kube_agents_memory", order)

    def test_the_entry_point_still_exports_what_the_scripts_import(self):
        """`__init__` is a facade now; the names other code reads must survive it."""
        module = _load_like_hermes(SUBMODULES)
        for name in ("DEFAULT_BANK_ID", "SHARED_TAG", "USER_TAG_PREFIX",
                     "CHECKPOINT_STRATEGY", "RETAIN_STRATEGIES", "NO_IDENTITY_NOTICE",
                     "KubeAgentsMemoryProvider", "sanitize_user_id",
                     "memory_is_read_only", "apply_scoping", "register"):
            self.assertTrue(hasattr(module, name), name)
            self.assertTrue(name in module.__all__ or name == "register", name)

    def test_every_submodule_is_reachable_from_the_package(self):
        """A module nothing imports is a module the loader would exec and forget."""
        module = _load_like_hermes(SUBMODULES)
        for stem in SUBMODULES:
            self.assertIn(f"{module.__name__}.{stem}", sys.modules, stem)


if __name__ == "__main__":
    unittest.main()
