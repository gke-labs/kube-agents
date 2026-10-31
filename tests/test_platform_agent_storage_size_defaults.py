"""One data volume size default, in the four places that carry it.

The operator's agentDataStorageSize is the size a CR with no spec.storage.size
gets. install.defaults.env's DEFAULT_PLATFORM_AGENT_STORAGE_SIZE and the
Terraform example's platform_agent_storage_size default are written into every
generated terraform.tfvars, and main.tf passes the chart nothing while the
variable equals its platform_agent_default_storage_size local, so the CR keeps
no spec.storage. If the copies drift, a default install either pins its CR to a
stale size, which a later raise of the operator default then cannot reach, or
asks for a size below the operator's, which the admission webhook refuses as a
shrink on an existing install. The installer and the Terraform variable also
take one grammar and one upper limit, so a value one accepts the other does not
refuse.

Run: python3 -m unittest discover -s tests -p 'test_platform_agent_storage_size_defaults.py' -v
"""

import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GO_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_DEFAULTS = _REPO_ROOT / "install.defaults.env"
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_COMPOSITION = _REPO_ROOT / "terraform" / "examples" / "full-install"


def _go_default() -> str:
    match = re.search(r'^\s*agentDataStorageSize\s*=\s*"([^"]+)"$', _GO_MANIFESTS.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError("agentDataStorageSize is not a string constant in platformagent_manifests.go")
    return match.group(1)


def _installer_default() -> str:
    # Sourced, as the front doors and lifecycle.sh read it, rather than parsed.
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; . "{_DEFAULTS}"; printf %s "$DEFAULT_PLATFORM_AGENT_STORAGE_SIZE"'],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _variable_block() -> str:
    variables = (_COMPOSITION / "variables.tf").read_text()
    start = variables.index('variable "platform_agent_storage_size" {')
    return variables[start : variables.index("\n}\n", start) + 3]


def _terraform_default() -> str:
    match = re.search(r'^\s*default\s*=\s*"([^"]+)"$', _variable_block(), re.MULTILINE)
    if match is None:
        raise AssertionError("platform_agent_storage_size has no string default")
    return match.group(1)


def _main_tf_local() -> str:
    match = re.search(
        r'^\s*platform_agent_default_storage_size\s*=\s*"([^"]+)"$',
        (_COMPOSITION / "main.tf").read_text(),
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError("main.tf has no platform_agent_default_storage_size local")
    return match.group(1)


class DataStorageSizeDefaultParityTest(unittest.TestCase):
    def test_the_installer_and_terraform_defaults_equal_the_operator_default(self):
        go = _go_default()
        self.assertEqual(_installer_default(), go, "install.defaults.env")
        self.assertEqual(_terraform_default(), go, "variables.tf")
        self.assertEqual(_main_tf_local(), go, "main.tf")

    def test_main_tf_passes_nothing_for_the_default(self):
        main = (_COMPOSITION / "main.tf").read_text()
        self.assertIn(
            "}, var.platform_agent_storage_size == local.platform_agent_default_storage_size ? {} : {\n"
            "      storage = { size = var.platform_agent_storage_size }\n",
            main,
        )

    def test_the_installer_and_the_variable_take_one_grammar(self):
        installer = re.search(
            r'^is_valid_platform_agent_storage_size\(\) \{\n  \[\[ "\$\{1:-\}" =~ (.+) \]\] \|\| return 1$',
            _INSTALLER_COMMON.read_text(),
            re.MULTILINE,
        )
        self.assertIsNotNone(installer, "installer_common.sh has no is_valid_platform_agent_storage_size")
        terraform = re.search(
            r'can\(regex\("(.+)", var\.platform_agent_storage_size\)\)',
            _variable_block(),
        )
        self.assertIsNotNone(terraform, "platform_agent_storage_size has no regex validation")
        self.assertEqual(installer.group(1), terraform.group(1))
        self.assertRegex(_go_default(), installer.group(1))

    def test_the_installer_and_the_variable_take_one_limit(self):
        installer = re.search(
            r"^readonly PLATFORM_AGENT_STORAGE_SIZE_MAX_MI=([0-9]+)$", _INSTALLER_COMMON.read_text(), re.MULTILINE
        )
        self.assertIsNotNone(installer, "installer_common.sh has no PLATFORM_AGENT_STORAGE_SIZE_MAX_MI")
        terraform = re.search(r"\) <= ([0-9]+), true\)", _variable_block())
        self.assertIsNotNone(terraform, "platform_agent_storage_size has no upper-limit validation")
        self.assertEqual(installer.group(1), terraform.group(1))
        self.assertEqual(int(installer.group(1)), 64 * 1024 * 1024, "the limit is 64Ti in Mi")

    def test_the_variable_names_the_crd_field(self):
        self.assertIn("spec.storage.size", _variable_block())


if __name__ == "__main__":
    unittest.main()
