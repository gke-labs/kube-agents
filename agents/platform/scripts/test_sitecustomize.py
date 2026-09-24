"""Tests for the deferred runtime-patch hook in sitecustomize.

The contract these pin down is a performance one with a correctness tail. The
operator puts this directory on ``PYTHONPATH`` for the agent container, so
``sitecustomize`` executes in every Python the pod starts — including the four
interpreters Hermes' environment probe spawns per session and the fresh
subprocess kanban spawns per card. Calling the ``install()`` hooks eagerly there
dragged ``slack_bolt`` and its transitive dependencies into all of them. So:
nothing heavy at startup, but each patch must still be in place before the
gateway reaches the thing it patches — a Slack adapter the gateway holds no real
bot token without, and the card-delivery path that would otherwise drop every
file the agent wrote inside the shell sandbox.
"""

import importlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Loaded by path rather than by name, because `import sitecustomize` cannot
# reach this one. CPython's `site.py` imports whatever `sitecustomize` it finds
# at interpreter startup, before any test code runs, so `sys.modules` is
# already bound by the time the `sys.path.insert` above happens. Debian and
# Ubuntu ship their own at `/usr/lib/python3.N/sitecustomize.py` — including on
# `ubuntu-latest`, where `.github/workflows/k8s-operator-test.yml` runs
# `make -C k8s-operator test` against the system interpreter with no
# `actions/setup-python` step. The plain import bound that module and every
# test here failed with `AttributeError: no attribute 'install_hook'`.
#
# Loading by path also removes the quieter half of the hazard: had the system
# module happened to export these names, the suite would have exercised the
# wrong module and passed.
_spec = importlib.util.spec_from_file_location(
    "_repo_sitecustomize", SCRIPTS_DIR / "sitecustomize.py"
)
sitecustomize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sitecustomize)


class InstallHookRegistrationTest(unittest.TestCase):
    """``install_hook`` defers the configured relays' patches, and the rest."""

    def test_no_relay_configured_still_defers_the_unconditional_patches(self):
        # These fix a gateway behaviour that has nothing to do with a relay, so
        # a direct-token install needs them as much as a proxied one does.
        meta_path = []
        finder = sitecustomize.install_hook(meta_path=meta_path, environ={})
        self.assertEqual(finder._module_names, sitecustomize.UNCONDITIONAL_PATCHES)
        self.assertIs(meta_path[0], finder)

    def test_empty_relay_url_is_not_configured(self):
        meta_path = []
        finder = sitecustomize.install_hook(
            meta_path=meta_path, environ={"SLACK_RELAY_URL": ""}
        )
        self.assertEqual(finder._module_names, sitecustomize.UNCONDITIONAL_PATCHES)

    def test_registers_only_the_configured_relay(self):
        meta_path = ["existing-finder"]
        finder = sitecustomize.install_hook(
            meta_path=meta_path, environ={"SLACK_RELAY_URL": "http://relay"}
        )
        self.assertIsInstance(finder, sitecustomize.PatchOnImport)
        self.assertEqual(
            finder._module_names,
            ("slack_relay_patch",) + sitecustomize.UNCONDITIONAL_PATCHES,
        )
        self.assertNotIn("google_chat_relay_patch", finder._module_names)
        # Ahead of the standard finders, so the loader wrap happens before the
        # module is handed to them.
        self.assertIs(meta_path[0], finder)

    def test_registers_both_relays_when_both_configured(self):
        meta_path = []
        finder = sitecustomize.install_hook(
            meta_path=meta_path,
            environ={
                "SLACK_RELAY_URL": "http://relay",
                "GOOGLE_CHAT_RELAY_URL": "http://relay",
            },
        )
        self.assertEqual(
            finder._module_names,
            ("google_chat_relay_patch", "slack_relay_patch")
            + sitecustomize.UNCONDITIONAL_PATCHES,
        )


class DeferredInstallTest(unittest.TestCase):
    """The patch fires on the trigger import, and not a moment sooner."""

    def setUp(self):
        self.calls = []
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        # test_slack_relay_patch stubs `gateway` into sys.modules and leaves it
        # there. A module already in sys.modules is returned without consulting
        # sys.meta_path at all, so without this the finder would never be asked
        # and every assertion below would pass or fail for the wrong reason.
        # Put the stubs back afterwards; they are not ours to delete.
        self._saved_modules = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "gateway" or name.startswith("gateway.")
        }
        self.addCleanup(sys.modules.update, self._saved_modules)

        # A stand-in for gateway.platform_registry that records whether it
        # executed, so we can prove the wrap did not swallow the real import.
        root = Path(self.tmpdir.name)
        (root / "gateway").mkdir()
        (root / "gateway" / "__init__.py").write_text("")
        (root / "gateway" / "platform_registry.py").write_text(
            textwrap.dedent(
                """
                class PlatformRegistry:
                    pass

                EXECUTED = True
                """
            )
        )
        (root / "unrelated_module.py").write_text("VALUE = 1\n")

        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        importlib.invalidate_caches()

        fake_relay = types.ModuleType("fake_relay_patch")
        fake_relay.install = lambda: self.calls.append("installed")
        sys.modules["fake_relay_patch"] = fake_relay
        self.addCleanup(sys.modules.pop, "fake_relay_patch", None)

        self.finder = sitecustomize.PatchOnImport(["fake_relay_patch"])
        sys.meta_path.insert(0, self.finder)
        self.addCleanup(self._discard_finder)
        self.addCleanup(self._purge_gateway)

    def _discard_finder(self):
        if self.finder in sys.meta_path:
            sys.meta_path.remove(self.finder)

    def _purge_gateway(self):
        for name in ("gateway.platform_registry", "gateway", "unrelated_module"):
            sys.modules.pop(name, None)

    def test_unrelated_import_does_not_install(self):
        importlib.import_module("unrelated_module")
        self.assertEqual(self.calls, [])

    def test_trigger_import_installs_and_still_executes_the_module(self):
        self.assertEqual(self.calls, [])
        registry = importlib.import_module("gateway.platform_registry")
        self.assertEqual(self.calls, ["installed"])
        # The wrapped loader must run the real module body, not replace it.
        self.assertTrue(registry.EXECUTED)
        self.assertTrue(hasattr(registry, "PlatformRegistry"))

    def test_install_runs_after_the_registry_is_importable(self):
        # Each install() begins with `from gateway.platform_registry import
        # PlatformRegistry`. If the hook fired before the module body finished,
        # that import would fail or see a half-built module.
        seen = []
        sys.modules["fake_relay_patch"].install = lambda: seen.append(
            importlib.import_module("gateway.platform_registry").PlatformRegistry
        )
        importlib.import_module("gateway.platform_registry")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].__name__, "PlatformRegistry")

    def test_finder_latches_after_firing(self):
        importlib.import_module("gateway.platform_registry")
        self.assertEqual(self.calls, ["installed"])
        # Re-importing must not double-apply; the patches are not all
        # idempotent by construction, they rely on their own sentinel flags.
        self._purge_gateway()
        importlib.invalidate_caches()
        importlib.import_module("gateway.platform_registry")
        self.assertEqual(self.calls, ["installed"])

    def test_missing_gateway_package_is_tolerated(self):
        # Wrapper scripts run under the system Python, which sees this
        # directory through PYTHONPATH but not Hermes' venv.
        def explode():
            raise ModuleNotFoundError("No module named 'gateway'", name="gateway")

        sys.modules["fake_relay_patch"].install = explode
        importlib.import_module("gateway.platform_registry")  # must not raise

    def test_unrelated_import_error_propagates(self):
        def explode():
            raise ModuleNotFoundError("No module named 'slack_bolt'", name="slack_bolt")

        sys.modules["fake_relay_patch"].install = explode
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("gateway.platform_registry")


class FinderMustNotImportTest(unittest.TestCase):
    """The finder answers without importing, because Python calls it with the
    global import lock held.

    ``importlib.util.find_spec`` imports the parent package to read its
    ``__path__``. In a kanban worker the main thread is importing ``gateway``
    at the same moment the plugin-discovery thread imports
    ``gateway.platform_registry``; the hook then waited for the main thread
    while holding the global import lock, and the main thread waited for the
    global import lock. Every such worker sat with an 83-byte transcript until
    the dispatcher's stale timer killed it.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        (self.root / "gateway").mkdir()
        (self.root / "gateway" / "__init__.py").write_text("")
        (self.root / "gateway" / "platform_registry.py").write_text("EXECUTED = True\n")
        sys.path.insert(0, str(self.root))
        self.addCleanup(sys.path.remove, str(self.root))
        importlib.invalidate_caches()
        self._saved_modules = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "gateway" or name.startswith("gateway.")
        }
        self.addCleanup(sys.modules.update, self._saved_modules)
        self.addCleanup(self._purge_gateway)

    def _purge_gateway(self):
        for name in ("gateway.platform_registry", "gateway"):
            sys.modules.pop(name, None)

    def test_find_spec_does_not_import_the_parent_package(self):
        finder = sitecustomize.PatchOnImport([])
        spec = finder.find_spec(
            "gateway.platform_registry", [str(self.root / "gateway")], None
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        self.assertNotIn("gateway", sys.modules)

    @unittest.skipIf(
        sys.version_info >= (3, 14),
        "3.14 waits on the parent's module lock before it consults any finder, so the old "
        "hook cannot deadlock there and this test would pass on both versions",
    )
    def test_two_threads_importing_the_package_and_the_trigger_both_finish(self):
        # The losing order, forced: thread A is inside gateway/__init__ when
        # thread B imports the trigger module through the hook, and A then
        # needs the global import lock for a fresh module. Under the old hook
        # neither thread ever returns (measured on 3.12 and 3.13), so this runs
        # in a subprocess with a deadline rather than wedging the test process.
        (self.root / "gateway" / "__init__.py").write_text(
            textwrap.dedent(
                """
                import sys
                ctl = sys.modules["_deadlock_ctl"]
                ctl.started.set()
                ctl.go.wait(10)
                import gateway_helper  # a new module: needs the global import lock
                """
            )
        )
        (self.root / "gateway_helper.py").write_text("VALUE = 1\n")
        script = textwrap.dedent(
            f"""
            import importlib, importlib.util, sys, threading, types
            sys.path.insert(0, {str(self.root)!r})
            spec = importlib.util.spec_from_file_location(
                "_sc", {str(SCRIPTS_DIR / "sitecustomize.py")!r}
            )
            sc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sc)
            sys.meta_path[:] = [f for f in sys.meta_path if type(f).__name__ != "PatchOnImport"]
            ctl = types.ModuleType("_deadlock_ctl")
            ctl.started, ctl.go, ctl.in_finder = threading.Event(), threading.Event(), threading.Event()
            sys.modules["_deadlock_ctl"] = ctl

            # Signals the moment thread B is inside the hook, so A is released
            # only then: the race is forced, not left to a sleep.
            class Signalling(sc.PatchOnImport):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == sc.TRIGGER_MODULE:
                        ctl.in_finder.set()
                    return super().find_spec(fullname, path, target)

            patch = types.ModuleType("fake_patch")
            patch.install = lambda: None
            sys.modules["fake_patch"] = patch
            sys.meta_path.insert(0, Signalling(["fake_patch"]))
            a = threading.Thread(target=lambda: importlib.import_module("gateway"), daemon=True)
            b = threading.Thread(
                target=lambda: importlib.import_module("gateway.platform_registry"), daemon=True
            )
            a.start()
            assert ctl.started.wait(10)
            b.start()
            assert ctl.in_finder.wait(10), "thread B never reached the hook"
            ctl.go.set()
            a.join(5)
            b.join(5)
            print("OK" if not (a.is_alive() or b.is_alive()) else "DEADLOCK", flush=True)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.stdout.strip(), "OK", result.stdout + result.stderr)


class StartupCostTest(unittest.TestCase):
    """The regression guard: importing sitecustomize stays cheap."""

    def test_startup_does_not_import_the_relay_modules(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        env["SLACK_RELAY_URL"] = "http://127.0.0.1:9/relay"
        env["GOOGLE_CHAT_RELAY_URL"] = "http://127.0.0.1:9/relay"
        # `-S` would skip site processing, and with it sitecustomize; this has
        # to be a normal interpreter start to measure what the pod measures.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; print(sorted(m for m in sys.modules "
                "if m.split('.')[0] in {'slack_relay_patch', "
                "'google_chat_relay_patch', 'sandbox_artifact_patch', "
                "'sandbox_exec', 'yaml', 'slack_bolt', 'slack_sdk', "
                "'aiohttp', 'gateway'}))",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "[]",
            "sitecustomize pulled relay machinery into a plain interpreter start: "
            + result.stdout,
        )

    def test_sitecustomize_is_actually_loaded_by_that_interpreter(self):
        # Guards the test above from passing for the wrong reason — an empty
        # module list means nothing if sitecustomize never ran at all.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        env["SLACK_RELAY_URL"] = "http://127.0.0.1:9/relay"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; f=[type(x).__name__ for x in sys.meta_path]; "
                "print('PatchOnImport' in f, 'sitecustomize' in sys.modules)",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True")


if __name__ == "__main__":
    unittest.main()
