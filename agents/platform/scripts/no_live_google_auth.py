"""Test support: keep a test module from reaching GCP through `google.auth`.

Some collectors open a Cloud Monitoring session with `google.auth.default()`,
the developer's application default credentials. The test requirements hold no
Google client library, so in CI that import fails and the collector goes on
with no session. On a machine that also has `google-auth` installed, a test
that leaves the session unstubbed read the real credentials file, refreshed a
token and called the Cloud Monitoring API. `install()` closes that for a whole
test module, two ways:

- in the process, `import google.auth` (and every module under it) fails with
  `ModuleNotFoundError`, with the library installed or not, and the guard
  records the attempt. The test sees the missing library CI has always given
  it. A test that wants a session passes one, or patches the factory.
- `GOOGLE_APPLICATION_CREDENTIALS` names a file that does not exist. A child
  process that calls `google.auth.default()` then fails at once: the library
  takes this variable before the developer's file and before the metadata
  server, and it does not fall back when the file is missing.

`Guard.uninstall()` restores both, and raises when an import was recorded: a
test, or a code path it reached, tried to load `google.auth`.

The first barrier holds only in the test process, and only for an import that
runs while the guard is in force: a module that bound a name from the library
before (`from google.auth import default` at its top) still holds it. The
collectors import inside the function that opens the session. A child process
is held by the second barrier alone, and the guard does not see what it tries;
a child started with an `env=` that drops the variable gets neither.

`install()` while a guard is in force returns that guard, and only the last
`uninstall()` takes it down: unittest runs a module's fixtures again around a
suite that one of the module's own tests runs.

    def setUpModule():
        global _NO_GOOGLE_AUTH
        _NO_GOOGLE_AUTH = no_live_google_auth.install()

    def tearDownModule():
        _NO_GOOGLE_AUTH.uninstall()

It follows `no_live_kubectl`. Used by tests only; nothing at runtime imports it.
"""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

GOOGLE = "google"
GOOGLE_AUTH = "google.auth"
# Every module under it: `google.auth.transport.requests`, and so on.
_UNDER_GOOGLE_AUTH = GOOGLE_AUTH + "."
AUTH_ATTRIBUTE = "auth"
CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"

# The guard's temporary directory and the file in it that is never made.
_DIR_PREFIX = "no-live-google-auth-"
_NO_CREDENTIALS_FILE = "no-such-credentials.json"

REFUSAL_MESSAGE = "google.auth is not available in unit tests (no_live_google_auth)"

# How many recorded imports the failure message quotes, and how it reads.
REPORTED_IMPORTS = 5
_IMPORT_RECORD = "{module} from {caller}"
_IMPORTS_SEPARATOR = "; "
_FAILURE_MESSAGE = (
    "{count} import(s) of google.auth left this test module unstubbed. "
    "Pass a session, or patch the factory, in the test that reaches it. First imports: {shown}"
)

# `sys.modules` had no entry for `google` when the guard went up.
_NO_ENTRY = object()

# Frames of the import machinery, which `_caller` steps over: the frozen
# bootstrap, and `importlib.import_module` and its neighbours on disk.
_FROZEN_PREFIX = "<frozen "
_IMPORTLIB_DIR = os.path.dirname(importlib.__file__) + os.sep
_UNKNOWN_CALLER = "?"

# The guard in force, if any; `install()` hands it to every caller.
_ACTIVE = None


def _is_google_auth(name: str) -> bool:
    return name == GOOGLE_AUTH or name.startswith(_UNDER_GOOGLE_AUTH)


def _caller() -> str:
    """The nearest frame outside the import machinery and this file: `<file>:<line>`."""
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        if not filename.startswith((_FROZEN_PREFIX, _IMPORTLIB_DIR)) and filename != __file__:
            return f"{filename}:{frame.f_lineno}"
        frame = frame.f_back
    return _UNKNOWN_CALLER


class _Refuser(importlib.abc.MetaPathFinder):
    """First on `sys.meta_path`: refuses `google.auth` and everything under it."""

    def __init__(self, guard):
        self._guard = guard

    def find_spec(self, fullname, path=None, target=None):
        if not _is_google_auth(fullname):
            return None
        self._guard._record(fullname)
        raise ModuleNotFoundError(REFUSAL_MESSAGE, name=fullname)


class Guard:
    """The two barriers, from construction until the last `uninstall()`."""

    def __init__(self):
        self._dir = Path(tempfile.mkdtemp(prefix=_DIR_PREFIX))
        self.no_credentials = self._dir / _NO_CREDENTIALS_FILE
        self._saved_env = os.environ.get(CREDENTIALS_ENV)
        os.environ[CREDENTIALS_ENV] = str(self.no_credentials)
        # A copy of the library that is already loaded would answer the import
        # without a look at the finders, so it leaves `sys.modules` until the end.
        self._saved_modules = {name: sys.modules.pop(name) for name in list(sys.modules) if _is_google_auth(name)}
        # `import google.auth` asks for `google` first. Where that already fails (no
        # Google package is installed, or a test blocked the name with a `None`
        # entry), the finder below would never be asked; an empty stand-in package
        # lets the import reach it. With another Google package installed, which is
        # what the test requirements give through protobuf, `google` is a real
        # namespace package and stays as it is.
        self._saved_package_entry = sys.modules.get(GOOGLE, _NO_ENTRY)
        self._stand_in_package = None
        package = sys.modules.get(GOOGLE)
        if package is None:
            sys.modules.pop(GOOGLE, None)
            if importlib.util.find_spec(GOOGLE) is None:
                package = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(GOOGLE, None, is_package=True))
                sys.modules[GOOGLE] = package
                self._stand_in_package = package
        self._saved_attribute = getattr(package, AUTH_ATTRIBUTE, None) if package is not None else None
        if self._saved_attribute is not None:
            delattr(package, AUTH_ATTRIBUTE)
        self._imports = []
        self._probing = False
        self._refuser = _Refuser(self)
        sys.meta_path.insert(0, self._refuser)
        self._holders = 0

    def _record(self, fullname: str) -> None:
        if not self._probing:
            self._imports.append(_IMPORT_RECORD.format(module=fullname, caller=_caller()))

    def imports(self) -> list[str]:
        """Each refused import, oldest first: `<module> from <file>:<line>`."""
        return list(self._imports)

    def refuses_the_import(self) -> bool:
        """Whether `import google.auth` fails now. This probe is not recorded."""
        self._probing = True
        try:
            importlib.import_module(GOOGLE_AUTH)
        except ModuleNotFoundError:
            return True
        finally:
            self._probing = False
        return False

    def uninstall(self) -> None:
        global _ACTIVE
        if _ACTIVE is not self:
            return
        self._holders -= 1
        if self._holders > 0:
            return
        _ACTIVE = None
        # A test may have put back a copy of `sys.meta_path` from before the guard.
        if self._refuser in sys.meta_path:
            sys.meta_path.remove(self._refuser)
        if self._stand_in_package is not None and sys.modules.get(GOOGLE) is self._stand_in_package:
            del sys.modules[GOOGLE]
        if self._saved_package_entry is None and GOOGLE not in sys.modules:
            sys.modules[GOOGLE] = None
        sys.modules.update(self._saved_modules)
        if self._saved_attribute is not None and sys.modules.get(GOOGLE) is not None:
            setattr(sys.modules[GOOGLE], AUTH_ATTRIBUTE, self._saved_attribute)
        if self._saved_env is None:
            os.environ.pop(CREDENTIALS_ENV, None)
        else:
            os.environ[CREDENTIALS_ENV] = self._saved_env
        shutil.rmtree(self._dir, ignore_errors=True)
        if self._imports:
            shown = _IMPORTS_SEPARATOR.join(self._imports[:REPORTED_IMPORTS])
            raise AssertionError(_FAILURE_MESSAGE.format(count=len(self._imports), shown=shown))


def install() -> Guard:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Guard()
    _ACTIVE._holders += 1
    return _ACTIVE


def assert_in_force(test, guard: Guard) -> None:
    """For one test in each guarded module: both barriers are up."""
    test.assertEqual(os.environ.get(CREDENTIALS_ENV), str(guard.no_credentials))
    test.assertFalse(guard.no_credentials.exists())
    before = guard.imports()
    test.assertTrue(guard.refuses_the_import())
    test.assertEqual(guard.imports(), before)
