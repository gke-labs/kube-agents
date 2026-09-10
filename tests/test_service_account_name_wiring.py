"""Every GSA name the composition accepts reaches the pod that impersonates it.

The full-install composition names three service accounts from variables
(agent_service_account_id, github_minter_service_account_id,
litellm_service_account_id) so a second install in one project can pick its own.
Each name has two consumers: the module that creates the GSA, and the chart value
that annotates the workload's KSA with it. Wiring only the first is a silent
failure -- the chart falls back to its values.yaml default, which is the first
install's account, so the second install's pod impersonates an account it does
not own or one that no longer exists. Nothing in `terraform validate` or the
installer tests notices; the HCL is pinned here.
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN_TF = _REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
_MINTER_OUTPUTS_TF = (
    _REPO_ROOT / "terraform" / "modules" / "github-minter" / "outputs.tf"
)

# (variable, module instance, expression the chart value must read). The chart
# value reads the module rather than the variable so that a null input, which
# selects the module default, still annotates the name the module created.
_WIRING = (
    (
        "agent_service_account_id",
        "kube_agents_iam",
        "module.kube_agents_iam.service_account_email",
    ),
    (
        "github_minter_service_account_id",
        "github_minter",
        "module.github_minter[0].service_account_id",
    ),
    (
        "litellm_service_account_id",
        "litellm_vertex_iam",
        "module.litellm_vertex_iam[0].service_account_email",
    ),
)


def _module_block(text, name):
    match = re.search(
        rf'^module\s+"{name}"\s*\{{(.*?)^\}}', text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"no module {name} in main.tf"
    return match.group(1)


class ServiceAccountNameWiringTest(unittest.TestCase):
    def setUp(self):
        self.main_tf = _MAIN_TF.read_text()

    def test_each_variable_names_its_module_gsa(self):
        for variable, module, _ in _WIRING:
            with self.subTest(variable=variable):
                self.assertRegex(
                    _module_block(self.main_tf, module),
                    rf"(?m)^\s*service_account_id\s*=\s*var\.{variable}\s*$",
                )

    def test_each_module_gsa_reaches_the_chart(self):
        for variable, _, expression in _WIRING:
            with self.subTest(variable=variable):
                self.assertIn(expression, self.main_tf)

    def test_the_minter_annotation_reads_the_module_account_id(self):
        """The chart appends @<project> itself, so gsaName is the short id, and
        the module owns the output that carries it."""
        self.assertRegex(
            self.main_tf,
            r"gsaName\s*=\s*module\.github_minter\[0\]\.service_account_id",
        )
        self.assertRegex(
            _MINTER_OUTPUTS_TF.read_text(),
            r'(?s)output "service_account_id" \{.*?google_service_account\.minter\.account_id',
        )

    def test_the_minter_annotation_is_conditional_on_the_module(self):
        """A null gsaName is a Helm null, which deletes the chart default rather
        than keeping it, so the key is only merged in when the module exists."""
        self.assertRegex(
            self.main_tf,
            r"var\.enable_github_minter\s*\?\s*\{\s*gsaName\s*=",
        )


if __name__ == "__main__":
    unittest.main()
