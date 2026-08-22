import email.message
import io
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, call, patch

import github_token_refresh
from github_token_refresh import (
    get_current_git_repo,
    main,
    refresh_git_credentials,
)


class GitHubTokenRefreshTest(unittest.TestCase):
    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_https(self, run):
        res = MagicMock()
        res.stdout = "https://github.com/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_ssh(self, run):
        res = MagicMock()
        res.stdout = "git@github.com:gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_failure_returns_none(self, run):
        run.side_effect = Exception("git not found")
        self.assertIsNone(get_current_git_repo())

    def test_refresh_git_credentials_invalid_repo_raises(self):
        with patch("github_token_refresh.get_current_git_repo", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("")
            self.assertIn("Could not identify target repository", str(cm.exception))

        with self.assertRaises(RuntimeError) as cm:
            refresh_git_credentials("invalid-repo-no-slash")
        self.assertIn("Could not identify target repository", str(cm.exception))

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

    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_general_exception_raises_runtime_error(self, urlopen):
        urlopen.side_effect = TypeError("unexpected type error")

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("Credential sidecar failed to refresh GitHub auth", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_gcloud_auth_audiences_fallback(self, run):
        # First call with --audiences raises, second call without flags succeeds
        res_fail = Exception("gcloud auth print-identity-token --audiences rejected")
        res_ok = MagicMock()
        res_ok.stdout = "fallback-oidc-token\n"
        run.side_effect = [res_fail, res_ok, MagicMock(), MagicMock()]

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"ghs_token_xyz\n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                token = refresh_git_credentials("owner/repository")

            self.assertEqual("ghs_token_xyz", token)

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_gcloud_auth_failure_raises(self, run):
        run.side_effect = [Exception("fail1"), Exception("fail2")]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository")
            self.assertIn("Failed to retrieve Google OIDC token", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_empty_oidc_token_raises(self, run):
        res = MagicMock()
        res.stdout = "   \n"
        run.return_value = res
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository")
            self.assertIn("Retrieved Google OIDC token via gcloud is empty", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_retries_on_5xx_and_succeeds(self, urlopen, sleep, run):
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

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_empty_token_raises(self, urlopen, run):
        run_gcloud = MagicMock()
        run_gcloud.stdout = "mock-oidc-token\n"
        run.return_value = run_gcloud

        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = b"   \n"
        ok_response.__enter__.return_value = ok_response
        urlopen.return_value = ok_response

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", max_attempts=1)
            self.assertIn("Token received from Minty is empty", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_general_exception_raises(self, urlopen, run):
        run_gcloud = MagicMock()
        run_gcloud.stdout = "mock-oidc-token\n"
        run.return_value = run_gcloud

        urlopen.side_effect = TypeError("unexpected error")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", max_attempts=1)
            self.assertIn("Failed to connect to Minty", str(cm.exception))

    @patch("github_token_refresh.refresh_git_credentials")
    def test_main_cli_success_and_failure(self, refresh):
        with patch.object(github_token_refresh.sys, "argv", ["github_token_refresh.py", "org/repo"]):
            main()
            refresh.assert_called_with("org/repo")

        refresh.side_effect = Exception("boom")
        with patch.object(github_token_refresh.sys, "argv", ["github_token_refresh.py"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(1, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
