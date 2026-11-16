"""no_live_google_auth: the two barriers, with no Google package, with another one, and with the library."""

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import no_live_google_auth  # noqa: E402

CHILD_READS_THE_VARIABLE = "import os; print(os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', ''))"


def another_google_package(root: Path) -> None:
    """A Google package that is not the auth library, as protobuf is: `google` is then a
    namespace package with no `auth` in it. The test requirements give this state."""
    package = root / "google" / "protobuf"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("NAME = 'protobuf'\n", encoding="utf-8")


def a_google_auth_library(root: Path) -> None:
    """A `google.auth` package on disk, as an installed library looks to the import system. Its
    `default()` returns a marker, so a test can tell the real module from a refusal."""
    package = root / "google" / "auth"
    (package / "transport").mkdir(parents=True)
    (package / "__init__.py").write_text("def default(scopes=None):\n    return ('the real library', None)\n", encoding="utf-8")
    (package / "transport" / "__init__.py").write_text("", encoding="utf-8")
    (package / "transport" / "requests.py").write_text("class AuthorizedSession:\n    pass\n", encoding="utf-8")


class GuardTestCase(unittest.TestCase):
    """Each test starts with no guard in force and no `google` module loaded, and ends so."""

    def setUp(self):
        self.assertIsNone(no_live_google_auth._ACTIVE, "another test left a guard in force")
        saved = {name: sys.modules.pop(name) for name in list(sys.modules) if name == "google" or name.startswith("google.")}
        self.addCleanup(sys.modules.update, saved)
        self.addCleanup(self.drop_google_modules)
        self.guard = None

    def drop_google_modules(self):
        for name in [name for name in sys.modules if name == "google" or name.startswith("google.")]:
            del sys.modules[name]

    def install(self):
        self.guard = no_live_google_auth.install()
        self.addCleanup(self.take_down)
        return self.guard

    def take_down(self):
        """Take the guard down whatever the test left recorded."""
        guard = no_live_google_auth._ACTIVE
        if guard is not None:
            guard._imports.clear()
            guard._holders = 1
            guard.uninstall()

    def without_any_google_package(self):
        """Take every folder that holds a Google package off the path. The machine that runs
        this test has one when the test requirements are installed."""
        without = patch.object(sys, "path", [p for p in sys.path if not (Path(p or ".") / "google").is_dir()])
        without.start()
        self.addCleanup(without.stop)
        importlib.invalidate_caches()
        self.assertIsNone(importlib.util.find_spec("google"), "a Google package is still importable here")

    def a_blocked_name_is_blocked_again_afterwards(self):
        """Another test blocked `google` with a `None` entry. Under the guard the import of
        `google.auth` still reaches the guard, the block holds for other Google packages, and
        the entry is `None` again afterwards."""
        with patch.dict(sys.modules, {"google": None}):
            guard = no_live_google_auth.install()
            self.addCleanup(self.take_down)
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module("google.auth")
            self.assertEqual(1, len(guard.imports()), "the stand-in let the import reach the guard")
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module("google.protobuf")
            guard._imports.clear()
            guard.uninstall()
            self.assertIn("google", sys.modules)
            self.assertIsNone(sys.modules["google"])

    def the_probe_counts_only_the_guards_own_refusal(self):
        """`assert_in_force` fails when the guard's finder is gone, and the probe says no after
        `uninstall()`, also where the import fails anyway because the library is missing."""
        guard = self.install()
        no_live_google_auth.assert_in_force(self, guard)
        sys.meta_path.remove(guard._refuser)
        self.assertFalse(guard.refuses_the_import())
        with self.assertRaises(AssertionError):
            no_live_google_auth.assert_in_force(unittest.TestCase(), guard)
        sys.meta_path.insert(0, guard._refuser)
        guard.uninstall()
        self.assertFalse(guard.refuses_the_import())

    def with_on_the_path(self, make_package):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        make_package(root)
        path = patch.object(sys, "path", [str(root), *sys.path])
        path.start()
        self.addCleanup(path.stop)
        importlib.invalidate_caches()


class TestWithNoGooglePackage(GuardTestCase):
    """A Python with no Google package at all."""

    def setUp(self):
        super().setUp()
        self.without_any_google_package()

    def test_the_import_fails_and_is_recorded(self):
        guard = self.install()
        with self.assertRaises(ModuleNotFoundError) as refused:
            importlib.import_module("google.auth")
        self.assertIn("no_live_google_auth", str(refused.exception))
        self.assertEqual("google.auth", refused.exception.name)
        self.assertEqual(1, len(guard.imports()))
        self.assertTrue(guard.imports()[0].startswith("google.auth from "), guard.imports())

    def test_the_stand_in_package_answers_a_question_about_itself(self):
        self.install()
        self.assertIsNotNone(importlib.util.find_spec("google"), "a module with no spec would raise here")

    def test_a_name_that_another_test_blocked_is_blocked_again_afterwards(self):
        self.a_blocked_name_is_blocked_again_afterwards()

    def test_the_probe_counts_only_the_guards_own_refusal(self):
        self.the_probe_counts_only_the_guards_own_refusal()

    def test_another_google_package_is_still_reported_missing(self):
        self.install()
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("google.protobuf")
        self.assertEqual([], self.guard.imports(), "only google.auth is the guard's")

    def test_uninstall_leaves_no_google_module_behind(self):
        guard = self.install()
        self.assertIn("google", sys.modules, "the stand-in package lets the import reach the guard")
        guard.uninstall()
        self.assertNotIn("google", sys.modules)
        with self.assertRaises(ModuleNotFoundError) as missing:
            importlib.import_module("google.auth")
        self.assertNotIn("no_live_google_auth", str(missing.exception), "the plain missing library again")


class TestWithAnotherGooglePackage(GuardTestCase):
    """What the test requirements give, in CI too: protobuf, and no auth library."""

    def setUp(self):
        super().setUp()
        self.without_any_google_package()
        self.with_on_the_path(another_google_package)

    def test_without_the_guard_the_library_is_missing(self):
        with self.assertRaises(ModuleNotFoundError) as missing:
            importlib.import_module("google.auth")
        self.assertEqual("No module named 'google.auth'", str(missing.exception))

    def test_the_import_fails_and_is_recorded(self):
        guard = self.install()
        with self.assertRaises(ModuleNotFoundError) as refused:
            importlib.import_module("google.auth")
        self.assertEqual("google.auth", refused.exception.name)
        self.assertEqual(1, len(guard.imports()))

    def test_the_other_package_loads_and_the_real_package_stays(self):
        guard = self.install()
        self.assertEqual("protobuf", importlib.import_module("google.protobuf").NAME)
        self.assertIsNone(guard._stand_in_package, "no stand-in where `google` is a real package")
        self.assertEqual([], guard.imports())
        guard.uninstall()
        self.assertEqual("protobuf", sys.modules["google.protobuf"].NAME)

    def test_a_name_that_another_test_blocked_is_blocked_again_afterwards(self):
        self.a_blocked_name_is_blocked_again_afterwards()

    def test_the_probe_counts_only_the_guards_own_refusal(self):
        self.the_probe_counts_only_the_guards_own_refusal()


class TestWithTheLibraryInstalled(GuardTestCase):
    """What a developer who installed google-auth has."""

    def setUp(self):
        super().setUp()
        self.with_on_the_path(a_google_auth_library)

    def test_without_the_guard_the_library_loads(self):
        self.assertEqual("the real library", importlib.import_module("google.auth").default()[0])

    def test_a_name_that_another_test_blocked_is_blocked_again_afterwards(self):
        self.a_blocked_name_is_blocked_again_afterwards()

    def test_the_probe_counts_only_the_guards_own_refusal(self):
        self.the_probe_counts_only_the_guards_own_refusal()

    def test_the_import_fails_and_is_recorded(self):
        guard = self.install()
        for statement in ("import google.auth", "from google.auth.transport.requests import AuthorizedSession", "from google import auth"):
            with self.subTest(statement), self.assertRaises(ImportError):
                exec(statement, {})
        self.assertEqual(3, len(guard.imports()))

    def test_a_copy_loaded_before_the_guard_does_not_answer(self):
        loaded = importlib.import_module("google.auth")
        importlib.import_module("google.auth.transport.requests")
        guard = self.install()
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("google.auth")
        self.assertFalse(hasattr(sys.modules["google"], "auth"), "nor does the attribute on the package")
        guard._imports.clear()
        guard.uninstall()
        self.assertIs(loaded, sys.modules["google.auth"], "and it is back afterwards")
        self.assertIs(loaded, sys.modules["google"].auth)
        self.assertIn("google.auth.transport.requests", sys.modules)

    def test_what_a_collector_does_gets_the_missing_library_ci_gives(self):
        self.install()

        def default_monitoring_session():
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default()
            return AuthorizedSession(credentials)

        with self.assertRaises(ModuleNotFoundError):
            default_monitoring_session()


class TestTheVariable(GuardTestCase):
    def test_it_names_a_file_that_does_not_exist(self):
        guard = self.install()
        self.assertEqual(str(guard.no_credentials), os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        self.assertFalse(guard.no_credentials.exists())

    def test_a_child_process_gets_it(self):
        guard = self.install()
        child = subprocess.run([sys.executable, "-c", CHILD_READS_THE_VARIABLE], capture_output=True, text=True, check=True)
        self.assertEqual(str(guard.no_credentials), child.stdout.strip())

    def test_uninstall_restores_it(self):
        self.addCleanup(self.take_down)
        for before in ("/a/developer/file.json", None):
            with self.subTest(before=before), patch.dict(os.environ):
                if before is None:
                    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                else:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = before
                guard = no_live_google_auth.install()
                self.assertNotEqual(before, os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
                guard.uninstall()
                self.assertEqual(before, os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
                self.assertFalse(guard.no_credentials.parent.exists(), "the guard's folder is gone")


class TestUninstall(GuardTestCase):
    def test_it_fails_the_module_when_an_import_was_refused(self):
        guard = self.install()
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("google.auth")
        with self.assertRaises(AssertionError) as failed:
            guard.uninstall()
        self.assertIn("1 import(s) of google.auth", str(failed.exception))
        self.assertIn(os.path.basename(__file__), str(failed.exception), "it names where the import came from")
        self.assertIsNone(no_live_google_auth._ACTIVE, "and the guard is down all the same")
        self.assertNotIn(guard._refuser, sys.meta_path)

    def test_it_is_quiet_when_nothing_was_imported(self):
        guard = self.install()
        guard.uninstall()
        self.assertNotIn(guard._refuser, sys.meta_path)

    def test_it_finishes_when_a_test_put_back_an_older_meta_path(self):
        before = list(sys.meta_path)
        with patch.dict(os.environ):
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            guard = self.install()
            sys.meta_path[:] = before
            guard.uninstall()
            self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", os.environ)
        self.assertIsNone(no_live_google_auth._ACTIVE)

    def test_a_second_install_shares_the_guard_until_the_last_uninstall(self):
        first = self.install()
        second = no_live_google_auth.install()
        self.assertIs(first, second)
        second.uninstall()
        self.assertIs(first, no_live_google_auth._ACTIVE)
        self.assertIn(first._refuser, sys.meta_path)
        first.uninstall()
        self.assertIsNone(no_live_google_auth._ACTIVE)

    def test_an_uninstall_after_the_last_one_does_nothing(self):
        guard = self.install()
        guard.uninstall()
        guard.uninstall()
        self.assertIsNone(no_live_google_auth._ACTIVE)


class TestAssertInForce(GuardTestCase):
    def test_it_passes_under_the_guard_and_records_nothing(self):
        guard = self.install()
        no_live_google_auth.assert_in_force(self, guard)
        self.assertEqual([], guard.imports())

    def test_it_fails_when_the_variable_was_changed(self):
        guard = self.install()
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/somewhere/else.json"}):
            probe = types.SimpleNamespace(
                assertEqual=unittest.TestCase().assertEqual, assertFalse=unittest.TestCase().assertFalse,
                assertTrue=unittest.TestCase().assertTrue,
            )
            with self.assertRaises(AssertionError):
                no_live_google_auth.assert_in_force(probe, guard)


if __name__ == "__main__":
    unittest.main()
