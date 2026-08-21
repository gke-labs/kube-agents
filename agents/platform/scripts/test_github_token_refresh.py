import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

from github_token_refresh import refresh_git_credentials


class GitHubTokenRefreshTest(unittest.TestCase):
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_delegates_without_receiving_token(self, urlopen, run):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        urlopen.return_value = response

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            token = refresh_git_credentials("owner/repository")

        self.assertEqual("", token)
        run.assert_not_called()
        request = urlopen.call_args.args[0]
        self.assertEqual(
            "http://127.0.0.1:8765/v1/github/refresh", request.full_url
        )

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_repos")
    def test_scopes_token_to_all_managed_repos_in_org(self, get_managed_repos, urlopen, run):
        import json
        get_managed_repos.return_value = ["owner/repo1", "owner/repo2", "other-org/repo3"]

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            return MagicMock()

        run.side_effect = fake_run

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"fake-installation-token"
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repo1")

        self.assertEqual("fake-installation-token", token)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("owner", body["org_name"])
        self.assertEqual(["repo1", "repo2"], body["repositories"])
        self.assertEqual("platform-agent-scope", body["scope"])


if __name__ == "__main__":
    unittest.main()
