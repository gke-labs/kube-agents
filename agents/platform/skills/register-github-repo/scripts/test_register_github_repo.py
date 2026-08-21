#!/usr/bin/env python3
import os
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../scripts"))

import register_github_repo


class TestRegisterGitHubRepo(unittest.TestCase):
    def test_invalid_repo_formats(self):
        for invalid in ["", "no-slash", "too/many/slashes", "/empty-owner", "empty-repo/"]:
            with self.subTest(repo=invalid):
                self.assertEqual(register_github_repo.register_repo(invalid), 1)

    @patch("register_github_repo.refresh_git_credentials", side_effect=Exception("Mint failed"))
    def test_token_minting_failure(self, _mock_refresh):
        self.assertEqual(register_github_repo.register_repo("acme/fleet"), 1)

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.run", side_effect=FileNotFoundError("gh"))
    def test_gh_cli_missing(self, _mock_run, _mock_refresh):
        self.assertEqual(register_github_repo.register_repo("acme/fleet"), 1)

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.run", side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="Forbidden"))
    def test_gh_access_denied(self, _mock_run, _mock_refresh):
        self.assertEqual(register_github_repo.register_repo("acme/fleet"), 1)

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.run")
    @patch("register_github_repo.get_managed_repos", side_effect=RuntimeError("ConfigMap missing"))
    def test_configmap_read_failure(self, _mock_get_managed, mock_run, _mock_refresh):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertEqual(register_github_repo.register_repo("acme/fleet"), 1)

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.run")
    @patch("register_github_repo.get_managed_repos", return_value=["acme/fleet", "acme/other"])
    def test_already_registered_returns_0(self, _mock_get_managed, mock_run, _mock_refresh):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertEqual(register_github_repo.register_repo("acme/fleet"), 0)
        # Verify no run commands were invoked since it early-exits
        mock_run.assert_not_called()

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.get_managed_repos", return_value=["acme/first"])
    @patch("register_github_repo.run")
    def test_successful_registration_patches_configmap(self, mock_run, _mock_get_managed, _mock_refresh):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertEqual(register_github_repo.register_repo("acme/second"), 0)
        # Check that kubectl patch was called first
        patch_call = mock_run.call_args_list[0]
        cmd = patch_call[0][0]
        self.assertEqual(cmd[0], "kubectl")
        self.assertEqual(cmd[1], "patch")
        self.assertIn('"managed_repos": "acme/first, acme/second"', cmd[-1])

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.get_managed_repos", return_value=["acme/first"])
    @patch("register_github_repo.run")
    def test_kubectl_missing_on_patch(self, mock_run, _mock_get_managed, _mock_refresh):
        # First call (kubectl) raises FileNotFoundError
        mock_run.side_effect = FileNotFoundError("kubectl")
        self.assertEqual(register_github_repo.register_repo("acme/second"), 1)

    @patch("register_github_repo.refresh_git_credentials")
    @patch("register_github_repo.get_managed_repos", return_value=["acme/first"])
    @patch("register_github_repo.run")
    def test_kubectl_patch_error(self, mock_run, _mock_get_managed, _mock_refresh):
        mock_run.side_effect = subprocess.CalledProcessError(1, ["kubectl"], stderr="Conflict")
        self.assertEqual(register_github_repo.register_repo("acme/second"), 1)


if __name__ == "__main__":
    unittest.main()
