"""`check_cert_manager_version` in hack/check-image-inventory.sh fails when the
full-install composition's `cert_manager_version` default, the commented copy
in terraform.tfvars.example, and the cert-manager tags in images.json move
apart.

CI only ever runs the script on a tree where the check passes, so every fail
path would otherwise execute nowhere. The function is lifted from the script's
own text and run under bash against synthetic inputs, so the assertions are
against the code that ships rather than a copy (the approach of
tests/test_check_image_inventory_go_directive.py).
"""

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_constant, lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# The functions the check needs, each lifted by name. A rename fails here
# loudly instead of silently shrinking what is tested.
_LIFTED_FUNCTIONS = ("fail", "check_cert_manager_version")

# The constants the function reads, lifted by name for the same reason.
_LIFTED_CONSTANTS = (
    "CERT_MANAGER_VARIABLE",
    "CERT_MANAGER_VARIABLES_TF",
    "CERT_MANAGER_TFVARS_EXAMPLE",
    "CERT_MANAGER_NAME_PREFIX",
)

# The call site, a whole line, asserted present because the lift below
# supplies its own: a function that is defined and never called keeps every
# gate green.
_CALL_SITE_PATTERN = r"^check_cert_manager_version$"

_VERSION = "v1.21.3"
_OTHER_VERSION = "v1.21.2"
_CERT_MANAGER_NAMES = ("cert-manager-controller", "cert-manager-webhook")
_UNRELATED_IMAGE = {"name": "busybox", "tag": "1.37"}


def _variables_tf(default_line: str) -> str:
    return (
        'variable "before" {\n  default = "v0.0.0"\n}\n\n'
        'variable "cert_manager_version" {\n'
        '  description = "cert-manager chart version."\n'
        "  type        = string\n"
        f"{default_line}"
        "}\n\n"
        'variable "after" {\n  default = "v0.0.0"\n}\n'
    )


def _default(version: str) -> str:
    return f'  default     = "{version}" # the chart version\n'


def _example(version: str) -> str:
    return f'# enable_cert_manager = true\n# cert_manager_version = "{version}"\n'


def _inventory(tags: dict) -> str:
    images = [{"name": name, "tag": tag} for name, tag in tags.items()]
    return json.dumps({"images": [_UNRELATED_IMAGE, *images]})


def _run_check(variables_tf: str, example: str, inventory: str) -> subprocess.CompletedProcess:
    text = _SCRIPT.read_text()
    functions = "".join(lift_function(name, text, _SCRIPT) for name in _LIFTED_FUNCTIONS)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        composition = root / "terraform" / "examples" / "full-install"
        composition.mkdir(parents=True)
        (composition / "variables.tf").write_text(variables_tf)
        (composition / "terraform.tfvars.example").write_text(example)
        (root / "images.json").write_text(inventory)
        script = (
            "set -u\nstatus=0\nINVENTORY=images.json\n"
            + "".join(lift_constant(name, text, _SCRIPT) for name in _LIFTED_CONSTANTS)
            + functions
            + "check_cert_manager_version\nexit $status\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, check=False
        )


class CheckCertManagerVersionTest(unittest.TestCase):
    def _all_at(self, version: str) -> str:
        return _inventory({name: version for name in _CERT_MANAGER_NAMES})

    def test_one_version_everywhere_passes(self):
        result = _run_check(_variables_tf(_default(_VERSION)), _example(_VERSION), self._all_at(_VERSION))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_terraform_default_behind_the_inventory_fails_per_entry(self):
        result = _run_check(
            _variables_tf(_default(_OTHER_VERSION)), _example(_OTHER_VERSION), self._all_at(_VERSION)
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        for name in _CERT_MANAGER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f"pins '{name}' at '{_VERSION}'", result.stderr)

    def test_one_inventory_entry_behind_fails_and_names_it(self):
        tags = {_CERT_MANAGER_NAMES[0]: _VERSION, _CERT_MANAGER_NAMES[1]: _OTHER_VERSION}
        result = _run_check(_variables_tf(_default(_VERSION)), _example(_VERSION), _inventory(tags))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"pins '{_CERT_MANAGER_NAMES[1]}' at '{_OTHER_VERSION}'", result.stderr)
        self.assertNotIn(f"pins '{_CERT_MANAGER_NAMES[0]}'", result.stderr)

    def test_tfvars_example_behind_the_default_fails(self):
        result = _run_check(
            _variables_tf(_default(_VERSION)), _example(_OTHER_VERSION), self._all_at(_VERSION)
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"example is '{_OTHER_VERSION}'", result.stderr)

    def test_tfvars_example_without_the_line_fails(self):
        result = _run_check(
            _variables_tf(_default(_VERSION)), "# enable_cert_manager = true\n", self._all_at(_VERSION)
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("example is '<unset>'", result.stderr)

    def test_variable_without_a_quoted_default_fails(self):
        result = _run_check(_variables_tf(""), _example(_VERSION), self._all_at(_VERSION))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("has no quoted default", result.stderr)

    def test_default_is_read_from_its_own_variable_block(self):
        """The variables before and after it carry a different quoted default;
        reading outside the block would pick one of them up."""
        result = _run_check(_variables_tf(""), _example("v0.0.0"), self._all_at("v0.0.0"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("has no quoted default", result.stderr)

    def test_inventory_without_a_cert_manager_entry_fails(self):
        result = _run_check(_variables_tf(_default(_VERSION)), _example(_VERSION), _inventory({}))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("compares cert_manager_version to nothing", result.stderr)


class CallSiteTest(unittest.TestCase):
    def test_script_calls_the_check(self):
        self.assertRegex(_SCRIPT.read_text(), re.compile(_CALL_SITE_PATTERN, re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
