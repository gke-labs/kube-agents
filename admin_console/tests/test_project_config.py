from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from admin_console.project_config import (
    build_project_candidates,
    is_valid_project_id,
    load_provisioned_target,
)


class ProjectConfigTest(unittest.TestCase):
    def test_loads_only_valid_deployment_coordinates(self):
        """The `export` spelling is read, and only the allowlisted keys are.

        install.env.example documents bare `K=V`, but the installers source the
        file, so `export K=V` is valid there and a hand-edited install.env may
        carry it. Everything outside the allowlist -- the API keys and tokens
        this file also holds -- must stay unread.
        """
        with tempfile.TemporaryDirectory() as directory:
            install_env = Path(directory) / "install.env"
            install_env.write_text(
                "\n".join(
                    (
                        "export PROJECT_ID=test-project-01",
                        "export CLUSTER_NAME=test-cluster-01",
                        "export REGION=us-east4",
                        "export NAMESPACE=kubeagents-system",
                        "export API_KEY=must-not-be-read",
                    )
                ),
                encoding="utf-8",
            )

            target = load_provisioned_target(install_env)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.project_id, "test-project-01")
        self.assertEqual(target.cluster_name, "test-cluster-01")
        self.assertEqual(target.location, "us-east4")
        self.assertEqual(target.namespace, "kubeagents-system")

    def test_rejects_shell_expression_in_an_exported_project_value(self):
        with tempfile.TemporaryDirectory() as directory:
            install_env = Path(directory) / "install.env"
            install_env.write_text(
                "export PROJECT_ID=$(touch /tmp/portal-must-not-execute)\n",
                encoding="utf-8",
            )
            self.assertIsNone(load_provisioned_target(install_env))

    def test_loads_a_hand_authored_install_env(self):
        """install.env is a dotenv: bare `K=V`, no `export`.

        A pattern that required `export` read nothing out of it and returned
        None, which the portal treats as "no provisioned target" and quietly
        falls back to the query parameter -- so the regression showed up as the
        wrong cluster preselected, not as an error.
        """
        with tempfile.TemporaryDirectory() as directory:
            install_env = Path(directory) / "install.env"
            install_env.write_text(
                "\n".join(
                    (
                        "# kube-agents install configuration",
                        "PROJECT_ID=test-project-01",
                        "CLUSTER_NAME=test-cluster-01",
                        "REGION=us-east4",
                        "GEMINI_API_KEY=must-not-be-read",
                    )
                ),
                encoding="utf-8",
            )
            target = load_provisioned_target(install_env)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.project_id, "test-project-01")
        self.assertEqual(target.cluster_name, "test-cluster-01")
        self.assertEqual(target.location, "us-east4")
        self.assertEqual(target.namespace, "kubeagents-system")

    def test_a_legacy_vars_sh_is_not_read_at_all(self):
        """install.env is the only source; k8s-operator/scripts/vars.sh is not.

        Nothing has written that file since the installers switched to
        install.env, so a copy still on disk is state from an install that has
        since been re-run. Merging it in -- which this used to do, with
        install.env winning key by key -- let its keys survive wherever
        install.env happened to be silent, which is how a stale region and
        namespace reached the portal. Every key it sets here is one install.env
        does not, so anything but the install.env values fails this test.
        """
        with tempfile.TemporaryDirectory() as directory:
            legacy_state = Path(directory) / "k8s-operator" / "scripts"
            legacy_state.mkdir(parents=True)
            (legacy_state / "vars.sh").write_text(
                "export PROJECT_ID=stale-project-01\n"
                "export CLUSTER_NAME=stale-cluster-01\n"
                "export REGION=us-central1\n"
                "export NAMESPACE=agents\n",
                encoding="utf-8",
            )
            install_env = Path(directory) / "install.env"
            install_env.write_text(
                "PROJECT_ID=test-project-01\nCLUSTER_NAME=test-cluster-01\n",
                encoding="utf-8",
            )
            target = load_provisioned_target(install_env)

        assert target is not None
        self.assertEqual(target.project_id, "test-project-01")
        self.assertEqual(target.cluster_name, "test-cluster-01")
        # install.env sets neither, and the legacy file cannot supply them any
        # more, so both fall back to the defaults.
        self.assertEqual(target.location, "")
        self.assertEqual(target.namespace, "kubeagents-system")

    def test_rejects_shell_expression_in_a_bare_install_env_assignment(self):
        """The never-sourced guarantee holds for the documented spelling too.

        The exported spelling is covered above; this is the one
        install.env.example tells operators to write.
        """
        with tempfile.TemporaryDirectory() as directory:
            install_env = Path(directory) / "install.env"
            install_env.write_text(
                "PROJECT_ID=$(touch /tmp/portal-must-not-execute)\n",
                encoding="utf-8",
            )
            self.assertIsNone(load_provisioned_target(install_env))


class VariableReferencesTest(unittest.TestCase):
    """install.env is sourced by the installers, so `${VAR}` resolves there.

    Reading it literally instead was silent: the literal fails
    CLUSTER_NAME_PATTERN and the portal shows no cluster scope, which looks
    exactly like an install that never configured one.
    """

    def _target(self, body: str, directory: str):
        install_env = Path(directory) / "install.env"
        install_env.write_text(body, encoding="utf-8")
        return load_provisioned_target(install_env)

    def test_a_reference_to_an_earlier_key_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                "PROJECT_ID=test-project-01\n"
                "CLUSTER_NAME=${PROJECT_ID}-host\n"
                "REGION=us-east4\n",
                directory,
            )
        assert target is not None
        self.assertEqual(target.cluster_name, "test-project-01-host")

    def test_the_braceless_spelling_resolves_too(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                "PROJECT_ID=test-project-01\nCLUSTER_NAME=$PROJECT_ID-host\n",
                directory,
            )
        assert target is not None
        self.assertEqual(target.cluster_name, "test-project-01-host")

    def test_double_quotes_do_not_suppress_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                'PROJECT_ID=test-project-01\nCLUSTER_NAME="${PROJECT_ID}-host"\n',
                directory,
            )
        assert target is not None
        self.assertEqual(target.cluster_name, "test-project-01-host")

    def test_single_quotes_suppress_expansion_as_the_shell_does(self):
        """The shell would leave this literal, so the portal must agree with it
        rather than invent a cluster the installers never provisioned."""
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                "PROJECT_ID=test-project-01\nCLUSTER_NAME='${PROJECT_ID}-host'\n",
                directory,
            )
        assert target is not None
        # The literal is not a valid cluster name, so it is dropped rather than
        # resolved -- which is what sourcing the file would have produced too.
        self.assertEqual(target.cluster_name, "")

    def test_a_reference_forward_to_a_later_key_is_left_literal(self):
        """The shell resolves in file order and so does this. A forward
        reference expands to nothing there; here it stays as written and fails
        validation, which is the same visible answer."""
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                "CLUSTER_NAME=${PROJECT_ID}-host\nPROJECT_ID=test-project-01\n",
                directory,
            )
        assert target is not None
        self.assertEqual(target.project_id, "test-project-01")
        self.assertEqual(target.cluster_name, "")

    def test_a_reference_to_a_non_allowlisted_key_is_not_resolved(self):
        """Only allowlisted keys enter the expansion scope, so the API keys and
        tokens install.env also holds never reach it."""
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                "GEMINI_API_KEY=must-not-be-read\n"
                "PROJECT_ID=test-project-01\n"
                "CLUSTER_NAME=${GEMINI_API_KEY}\n",
                directory,
            )
        assert target is not None
        self.assertEqual(target.cluster_name, "")

    def test_a_reference_to_a_key_only_the_legacy_state_file_sets_is_literal(self):
        """The expansion scope is install.env and nothing else.

        This used to resolve: the legacy vars.sh was read first and seeded the
        scope, so install.env could name a key only that file set. Now the file
        is not read, so the reference is left as written and fails validation
        -- the same visible answer as any other unresolvable reference, rather
        than a cluster name built out of state from a superseded install.
        """
        with tempfile.TemporaryDirectory() as directory:
            legacy_state = Path(directory) / "k8s-operator" / "scripts"
            legacy_state.mkdir(parents=True)
            (legacy_state / "vars.sh").write_text(
                "export NAMESPACE=stale-namespace\n", encoding="utf-8"
            )
            install_env = Path(directory) / "install.env"
            install_env.write_text(
                "PROJECT_ID=test-project-01\n"
                "CLUSTER_NAME=${NAMESPACE}-host\n"
                "REGION=us-east4\n",
                encoding="utf-8",
            )
            target = load_provisioned_target(install_env)
        assert target is not None
        self.assertEqual(target.cluster_name, "")
        self.assertEqual(target.namespace, "kubeagents-system")

    def test_command_substitution_is_still_never_expanded(self):
        """Expanding `$VAR` must not have opened the door to `$(...)`."""
        with tempfile.TemporaryDirectory() as directory:
            target = self._target(
                "PROJECT_ID=test-project-01\n"
                "CLUSTER_NAME=$(touch /tmp/portal-must-not-execute)\n"
                "REGION=us-east4\n",
                directory,
            )
        assert target is not None
        self.assertEqual(target.cluster_name, "")

    def test_candidates_are_valid_and_deduplicated(self):
        self.assertTrue(is_valid_project_id("test-project-01"))
        self.assertFalse(is_valid_project_id("Not A Project"))
        candidates = build_project_candidates(
            None,
            "test-project-01",
            "test-project-01",
        )
        self.assertEqual(
            [(item.project_id, item.source) for item in candidates],
            [("test-project-01", "active gcloud configuration")],
        )

    def test_candidates_distinguish_saved_and_url_projects(self):
        candidates = build_project_candidates(
            None,
            "active-project-01",
            "url-project-01",
            "saved-project-01",
        )

        self.assertEqual(
            [(item.project_id, item.source) for item in candidates],
            [
                ("active-project-01", "active gcloud configuration"),
                ("saved-project-01", "saved connection"),
                ("url-project-01", "URL selection"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
