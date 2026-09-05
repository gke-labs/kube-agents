"""The two Vertex resources in the serving project are gated, and nothing else is.

`vertex_manage_serving_project = false` exists for a serving project the applying
identity cannot administer. The composition's contract is narrow: exactly the
API enablement and the gateway's role grant -- the resources whose `project` is
`local.vertex_project` -- follow the variable, and the gateway's service account
and its Workload Identity binding, which live in `project_id`, do not. A revert
of either `count` to `local.use_vertex` would re-create the 403 the variable
exists to avoid and no unit test of the installer would notice, because the
installer only writes the tfvars line. The shell half is covered in
`test_install_script.py` and `test_installer_common.py`; the HCL is pinned here.
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FULL_INSTALL = _REPO_ROOT / "terraform" / "examples" / "full-install"
_MAIN_TF = _FULL_INSTALL / "main.tf"
_VARIABLES_TF = _FULL_INSTALL / "variables.tf"

_SERVING_PROJECT_RESOURCES = (
    ("google_project_service", "vertex_ai"),
    ("google_project_iam_member", "litellm_vertex_user"),
)


def _block(text, kind, name):
    match = re.search(
        rf'^(resource|module)\s+"{kind}"\s+"{name}"\s*\{{(.*?)^\}}',
        text,
        re.MULTILINE | re.DOTALL,
    ) or re.search(
        rf'^(module)\s+"{name}"\s*\{{(.*?)^\}}', text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"no block {kind} {name} in main.tf"
    return match.group(2)


class VertexManageServingProjectTest(unittest.TestCase):
    def setUp(self):
        self.main_tf = _MAIN_TF.read_text()

    def test_the_variable_exists_and_defaults_to_true(self):
        text = _VARIABLES_TF.read_text()
        block = re.search(
            r'variable "vertex_manage_serving_project" \{(.*?)^\}',
            text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(block, "variables.tf lacks vertex_manage_serving_project")
        self.assertRegex(block.group(1), r"type\s*=\s*bool")
        self.assertRegex(block.group(1), r"default\s*=\s*true")

    def test_the_local_combines_the_provider_and_the_variable(self):
        match = re.search(
            r"^\s*manage_vertex_serving\s*=\s*(.+)$", self.main_tf, re.MULTILINE
        )
        self.assertIsNotNone(match, "no manage_vertex_serving local in main.tf")
        expression = match.group(1)
        self.assertIn("local.use_vertex", expression)
        self.assertIn("var.vertex_manage_serving_project", expression)

    def test_both_serving_project_resources_follow_the_local(self):
        for kind, name in _SERVING_PROJECT_RESOURCES:
            with self.subTest(resource=f"{kind}.{name}"):
                body = _block(self.main_tf, kind, name)
                self.assertRegex(body, r"project\s*=\s*local\.vertex_project")
                self.assertRegex(
                    body, r"count\s*=\s*local\.manage_vertex_serving\s*\?\s*1\s*:\s*0"
                )

    def test_the_gateway_identity_does_not_follow_it(self):
        # The service account and its binding live in project_id and must
        # exist whichever way the variable is set; that is what lets the
        # operator grant the role by hand to a stable email.
        body = _block(self.main_tf, "module", "litellm_vertex_iam")
        self.assertRegex(body, r"project_id\s*=\s*var\.project_id")
        self.assertRegex(body, r"count\s*=\s*local\.use_vertex\s*\?\s*1\s*:\s*0")
        self.assertNotIn("manage_vertex_serving", body)

    def test_nothing_else_lives_in_the_serving_project(self):
        # The README claims these are the only two; keep the claim honest.
        users = re.findall(
            r'^(resource|module)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{(.*?)^\}',
            self.main_tf,
            re.MULTILINE | re.DOTALL,
        )
        in_serving_project = sorted(
            name or kind
            for _, kind, name, body in users
            if re.search(r"project\s*=\s*local\.vertex_project", body)
        )
        self.assertEqual(
            in_serving_project, sorted(name for _, name in _SERVING_PROJECT_RESOURCES)
        )


if __name__ == "__main__":
    unittest.main()
