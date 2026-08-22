import email.message
import io
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, call, patch

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

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_retries_on_5xx_and_succeeds(self, urlopen, sleep):
        err_502 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/github/refresh",
            502,
            "Bad Gateway",
            email.message.Message(),
            io.BytesIO(b"Bad Gateway"),
        )
        ok_response = MagicMock()
        ok_response.__enter__.return_value.status = 200
        urlopen.side_effect = [err_502, ok_response]

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_retries_on_connection_error_and_succeeds(self, urlopen, sleep):
        err_conn = urllib.error.URLError("Connection refused")
        ok_response = MagicMock()
        ok_response.__enter__.return_value.status = 200
        urlopen.side_effect = [err_conn, ok_response]

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_4xx_without_retry(self, urlopen, sleep):
        err_403 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/github/refresh",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Forbidden"),
        )
        urlopen.side_effect = err_403

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("HTTP 403", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_after_max_attempts_on_5xx(self, urlopen, sleep):
        err_503 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/github/refresh",
            503,
            "Service Unavailable",
            email.message.Message(),
            io.BytesIO(b"Service Unavailable"),
        )
        urlopen.side_effect = err_503

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials(
                    "owner/repository", max_attempts=3, initial_delay=0.01
                )

        self.assertIn("Credential sidecar failed to refresh GitHub auth", str(cm.exception))
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_has_calls([call(0.01), call(0.02)])

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_retries_on_5xx_and_succeeds(self, urlopen, sleep, run):
        # Mock gcloud auth print-identity-token
        run_gcloud = MagicMock()
        run_gcloud.stdout = "mock-oidc-token\n"
        run.return_value = run_gcloud

        err_500 = urllib.error.HTTPError(
            "http://token-minter/token",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(b"Internal Server Error"),
        )
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = b"ghs_minted_test_token_123\n"
        ok_response.__enter__.return_value = ok_response
        urlopen.side_effect = [err_500, ok_response]

        with patch.dict(os.environ, {}, clear=True):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("ghs_minted_test_token_123", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)
        # Check gh auth login called
        gh_login_calls = [
            c for c in run.call_args_list if c.args and c.args[0] == ["gh", "auth", "login", "--with-token"]
        ]
        self.assertEqual(1, len(gh_login_calls))
        self.assertEqual("ghs_minted_test_token_123", gh_login_calls[0].kwargs.get("input"))

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_fails_immediately_on_403_without_retry(self, urlopen, sleep, run):
        run_gcloud = MagicMock()
        run_gcloud.stdout = "mock-oidc-token\n"
        run.return_value = run_gcloud

        err_403 = urllib.error.HTTPError(
            "http://token-minter/token",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Repository not allowed"),
        )
        urlopen.side_effect = err_403

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("Minty returned error (HTTP 403)", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_fails_after_max_attempts_on_persistent_5xx(self, urlopen, sleep, run):
        run_gcloud = MagicMock()
        run_gcloud.stdout = "mock-oidc-token\n"
        run.return_value = run_gcloud

        err_500 = urllib.error.HTTPError(
            "http://token-minter/token",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(b"Internal Server Error"),
        )
        urlopen.side_effect = err_500

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", max_attempts=3, initial_delay=0.01)

        self.assertIn("Minty returned error (HTTP 500)", str(cm.exception))
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_has_calls([call(0.01), call(0.02)])


if __name__ == "__main__":
    unittest.main()
