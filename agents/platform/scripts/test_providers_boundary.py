"""The tests that make the layout a rule rather than a convention.

The modularity claim in this design is one sentence -- *a forge's name appears
in its own package and nowhere else* -- and a claim like that decays under
deadline unless something fails when it stops being true. Two checks, both
cheap enough to run on every change:

- an import boundary, parsed rather than grepped, so that "Bitbucket cannot
  reach into GitHub's translation" is a build failure and not a code-review
  preference;
- a name guard, which is a grep, because an `if host == "github.com":` needs no
  import and the parsed check would never see it.

The second is crude on purpose. It is the one that catches the special case
someone adds at 6pm.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

from providers import AVAILABLE

SCRIPTS = Path(__file__).resolve().parent
PROVIDERS = SCRIPTS / "providers"

# The file the parser is pointed at to prove it can see a violation. Excluded
# from the sweep so a failed assertion cannot leave a tripwire behind that then
# fails a different test on the next run.
PROBE = "_boundary_probe.py"

# What a forge package is allowed to import from the shared contract. Anything
# else under `providers/` is either another forge or the registry, and a forge
# that imports the registry has made the dependency point the wrong way.
SHARED_MODULES = frozenset(
    {"base", "validate", "errors", "identity", "transport", "credentials"}
)

# Derived, not listed: the packages that hold a forge are wherever the classes
# in `AVAILABLE` were defined. A new forge is covered by these tests the moment
# it is registered, without editing this file.
FORGE_PACKAGES = frozenset(cls.__module__.split(".")[1] for cls in AVAILABLE)


def module_name(path: Path) -> str:
    """The dotted name a file would be imported as."""
    parts = path.relative_to(SCRIPTS).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports_of(path: Path) -> list[str]:
    """Every module this file imports, with relative imports resolved.

    A relative import is the same edge in the dependency graph as an absolute
    one -- `from ..base import Forge` and `from providers.base import Forge`
    reach the same module -- so the rules have to be checked against the
    resolved name or half of them are unenforced.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    package = module_name(path).rsplit(".", 1)[0] if path.name != "__init__.py" else module_name(path)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - node.level + 1]
                prefix = ".".join([*base, node.module] if node.module else base)
            else:
                prefix = node.module or ""
            found.append(prefix)
            # `from providers import Forge` and `from . import translate` are
            # the same statement shape; whether the name is a module or a class
            # is not knowable here, so both spellings are recorded and the
            # rules are written to accept a package name on its own.
            found.extend(f"{prefix}.{alias.name}" for alias in node.names if prefix)
    return found


def python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != PROBE
    )


def is_stdlib(name: str) -> bool:
    return name.split(".")[0] in sys.stdlib_module_names


class ImportBoundaryTest(unittest.TestCase):
    def test_the_forge_packages_are_discovered_from_the_registry(self):
        # If this is empty every other test in the file passes vacuously.
        self.assertTrue(FORGE_PACKAGES)
        for package in FORGE_PACKAGES:
            self.assertTrue((PROVIDERS / package / "__init__.py").is_file())

    def test_nothing_outside_providers_reaches_into_a_provider(self):
        # The broker, the credential proxy and every skill see one surface:
        # `providers`. Reaching past it is how a caller acquires a dependency
        # on a particular forge without anyone deciding to give it one.
        for path in python_files():
            if PROVIDERS in path.parents:
                continue
            for name in imports_of(path):
                parts = name.split(".")
                if parts[0] != "providers" or len(parts) < 2:
                    continue
                with self.subTest(module=module_name(path), imported=name):
                    self.assertNotIn(
                        parts[1],
                        FORGE_PACKAGES,
                        "import from the `providers` package surface instead",
                    )

    def test_the_registry_is_the_only_shared_module_that_knows_a_forge(self):
        # Somewhere has to name the classes; the design's answer is that it is
        # exactly one file, and that the file names classes rather than hosts.
        for path in sorted(PROVIDERS.glob("*.py")):
            for name in imports_of(path):
                parts = name.split(".")
                if len(parts) < 2 or parts[0] != "providers":
                    continue
                if parts[1] not in FORGE_PACKAGES:
                    continue
                with self.subTest(module=module_name(path), imported=name):
                    self.assertEqual(path.name, "registry.py")

    def test_a_forge_imports_the_shared_contract_and_nothing_else(self):
        # The rule that matters. It is also what keeps the shared modules
        # honest: the moment `errors.py` grew one forge's message heuristics,
        # every other forge would be importing them through the front door and
        # this test would not notice.
        for package in sorted(FORGE_PACKAGES):
            for path in sorted((PROVIDERS / package).rglob("*.py")):
                own = f"providers.{package}"
                for name in imports_of(path):
                    if is_stdlib(name) or name.startswith(own):
                        continue
                    parts = name.split(".")
                    with self.subTest(module=module_name(path), imported=name):
                        self.assertEqual(parts[0], "providers")
                        self.assertGreaterEqual(len(parts), 2)
                        self.assertIn(parts[1], SHARED_MODULES)

    def test_the_test_finds_the_violation_it_exists_to_find(self):
        # A boundary test that passes because it parsed nothing is the failure
        # mode here, so the parser is pointed at the violation directly.
        source = "from providers.github import GitHubForge\nimport os\n"
        scratch = SCRIPTS / PROBE
        scratch.write_text(source)
        self.addCleanup(scratch.unlink)
        self.assertIn("providers.github", imports_of(scratch))


class ForgeNameGuardTest(unittest.TestCase):
    """No forge's name in the code that is supposed to be forge-neutral.

    The import test cannot see a string. This one is a substring search over
    source text, deliberately including comments and docstrings: prose that
    explains the general rule by way of one forge is how the next reader learns
    that the general rule has an exception.
    """

    def neutral_files(self) -> list[Path]:
        return [SCRIPTS / "vcs_broker.py"] + [
            path
            for path in sorted(PROVIDERS.glob("*.py"))
            if path.name != "registry.py"
        ]

    def test_the_broker_and_the_shared_contract_name_no_forge(self):
        for path in self.neutral_files():
            text = path.read_text().lower()
            for package in sorted(FORGE_PACKAGES):
                with self.subTest(file=path.name, forge=package):
                    self.assertNotIn(package, text)

    def test_the_shared_contract_names_no_forge_cli_either(self):
        # `gh` was in the broker image before any of this and would have been
        # the easiest constant to leave behind. What may run is derived from
        # the forges an install built, so the string belongs to one package.
        binaries = {cls.cli for cls in AVAILABLE if cls.cli}
        self.assertTrue(binaries)
        for path in self.neutral_files():
            words = set(path.read_text().replace('"', " ").replace("'", " ").split())
            for binary in sorted(binaries):
                with self.subTest(file=path.name, cli=binary):
                    self.assertNotIn(binary, words)

    def test_the_registry_names_a_forge_only_where_it_must(self):
        # The one acknowledged exception, held to its terms: the name appears
        # in an import and in `AVAILABLE`, and nowhere that could be a decision.
        lines = (PROVIDERS / "registry.py").read_text().splitlines()
        for number, line in enumerate(lines, start=1):
            lowered = line.lower()
            for package in sorted(FORGE_PACKAGES):
                if package not in lowered:
                    continue
                with self.subTest(line=number, forge=package):
                    self.assertTrue(
                        lowered.lstrip().startswith("from .")
                        or "available" in lowered,
                        f"registry.py:{number} decides something about {package}",
                    )


if __name__ == "__main__":
    unittest.main()
