import os
import unittest
from unittest.mock import MagicMock, patch

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


if __name__ == "__main__":
    unittest.main()
