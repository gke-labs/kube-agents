import email.message
import io
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import github_token_refresh
from github_token_refresh import (
    get_current_git_repo,
    main,
    refresh_git_credentials,
)


class _Clock:
    """A stand-in for time.monotonic: the given readings, then the last one forever."""

    def __init__(self, readings, then):
        self._readings = list(readings)
        self._then = then

    def __call__(self):
        return self._readings.pop(0) if self._readings else self._then


class GitHubTokenRefreshTest(unittest.TestCase):
    def setUp(self):
        # These tests describe the gcloud path. The metadata server is asked
        # before it and would otherwise take the generic urlopen mocks below for
        # its own answer; MetadataIdentityTest covers it.
        no_metadata = patch("github_token_refresh.metadata_identity_token", return_value=None)
        no_metadata.start()
        self.addCleanup(no_metadata.stop)

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
    def test_get_current_git_repo_ssh_over_443(self, run):
        res = MagicMock()
        res.stdout = "ssh://git@ssh.github.com:443/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_rejects_lookalike_hosts(self, run):
        res = MagicMock()
        run.return_value = res
        for url in (
            "https://evil.example/github.com/gke-labs/kube-agents.git",
            "https://github.com.evil.example/gke-labs/kube-agents.git",
            "https://notgithub.com/gke-labs/kube-agents.git",
            "git@evil.example:github.com/gke-labs/kube-agents.git",
            "https://github.com@evil.example/gke-labs/kube-agents.git",
            "https://evil.example/x.git?github.com",
            "https://evil.example/x.git#github.com",
        ):
            with self.subTest(url=url):
                res.stdout = url + "\n"
                self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_www_alias(self, run):
        res = MagicMock()
        res.stdout = "https://www.github.com/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertEqual("gke-labs/kube-agents", get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_rejects_non_slug_paths(self, run):
        res = MagicMock()
        run.return_value = res
        for url in (
            # A deep link, not a clone URL: nothing downstream on the direct
            # Minty path would reject "kube-agents/tree/main" as a repository.
            "https://github.com/gke-labs/kube-agents/tree/main",
            "https://github.com/../../etc/passwd",
            "https://github.com/%2e%2e/x.git",
            "https://github.com/gke-labs",
            "https://github.com/",
        ):
            with self.subTest(url=url):
                res.stdout = url + "\n"
                self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_non_github_remote_returns_none(self, run):
        res = MagicMock()
        res.stdout = "https://gitlab.com/gke-labs/kube-agents.git\n"
        run.return_value = res
        self.assertIsNone(get_current_git_repo())

    @patch("github_token_refresh.subprocess.run")
    def test_get_current_git_repo_local_path_returns_none(self, run):
        res = MagicMock()
        run.return_value = res
        for url in (
            "/srv/git/kube-agents.git",
            # `git remote add origin github.com/gke-labs/kube-agents` succeeds:
            # git takes it as a relative local path, not a GitHub URL. It is a
            # directory name, and `repo_ref`'s shorthand lift gives it an
            # inferred github.com host -- which is right for a repository
            # someone registered and wrong for a remote git emitted. Admitting
            # it here mints an installation token for whichever org the
            # directory happens to name.
            "github.com/gke-labs/kube-agents",
            "GitHub.com/gke-labs/kube-agents",
        ):
            with self.subTest(url=url):
                res.stdout = url + "\n"
                self.assertIsNone(get_current_git_repo())

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

    def test_refresh_git_credentials_refuses_a_host_shaped_repository(self):
        """The slug gate, not a slash count.

        The check this replaced counted separators, so every value here passed
        it and reached the Minty branch below, which splits on the first slash
        and posts the left half as an org name: `github.com/acme` would have
        been minted for an org called `github.com`. Nothing downstream of that
        branch validates, so these have to fail here or not at all.

        `" acme/toolkit "` is deliberately absent: the line above the gate
        strips whitespace and slashes and it is the *stripped* value that goes
        on to the broker, so normalising it there is safe. What is not safe is
        normalising and then posting the original, which is why
        `credential_proxy` gets the strict predicate.
        """
        for repository in (
            "github.com/acme",
            "www.github.com/acme",
            "ssh.github.com/acme",
            "acme/..",
            "acme/-toolkit",
            "acme/toolkit.git",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(RuntimeError) as cm:
                    refresh_git_credentials(repository)
                self.assertIn(
                    "Could not identify target repository", str(cm.exception)
                )

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
            "http://127.0.0.1:8765/v1/forge/refresh", request.full_url
        )
        # Named with its host and provider: a broker serving more than one
        # forge refuses a bare owner/name.
        self.assertEqual(
            {"provider": "github", "repository": "https://github.com/owner/repository"},
            json.loads(request.data),
        )

    @patch("github_token_refresh.wif_credentials.fetch_identity_token")
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_federated_identity_replaces_gcloud(self, urlopen, run, fetch):
        # The co-located sandbox proxy. gcloud refuses to mint an ID token from
        # an external_account credential, so calling it here is not a fallback
        # that costs a retry -- it is the failure the federated branch exists to
        # avoid, and it must not be reached at all.
        fetch.return_value = "an.id.token"
        response = MagicMock()
        # status is compared against 500 before the body is read, so a bare
        # MagicMock here is a TypeError rather than a 200.
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b"ghs_installation_token"
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repository")

        self.assertEqual("ghs_installation_token", token)
        self.assertNotIn(
            "print-identity-token",
            " ".join(str(call.args[0]) for call in run.call_args_list),
        )
        self.assertEqual("an.id.token", urlopen.call_args.args[0].headers["X-oidc-token"])

    @patch("github_token_refresh.wif_credentials.fetch_identity_token")
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_a_host_with_neither_identity_source_still_asks_gcloud(self, urlopen, run, fetch):
        # A host with neither a federated credential nor a metadata server
        # (setUp): fetch_identity_token returns None and gcloud is the last
        # resort, exactly as it was.
        fetch.return_value = None
        run.return_value = MagicMock(stdout="gcloud.id.token\n")
        response = MagicMock()
        # status is compared against 500 before the body is read, so a bare
        # MagicMock here is a TypeError rather than a 200.
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b"ghs_installation_token"
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            refresh_git_credentials("owner/repository")

        self.assertIn(
            "print-identity-token",
            " ".join(str(call.args[0]) for call in run.call_args_list),
        )
        self.assertEqual(
            "gcloud.id.token", urlopen.call_args.args[0].headers["X-oidc-token"]
        )

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_github_repos")
    def test_scopes_token_to_all_managed_repos_in_org(
        self, get_managed_github_repos, urlopen, run
    ):
        import json

        get_managed_github_repos.return_value = [
            "owner/repo1",
            "owner/repo2",
            "other-org/repo3",
        ]

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            return MagicMock()

        run.side_effect = fake_run

        response = MagicMock()
        response.status = 200
        response.read.return_value = b"fake-installation-token"
        response.__enter__.return_value = response
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repo1")

        self.assertEqual("fake-installation-token", token)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("owner", body["org_name"])
        self.assertEqual(["repo1", "repo2"], body["repositories"])
        self.assertEqual("platform-agent-scope", body["scope"])

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_github_repos")
    def test_scoped_repositories_attached_to_token_and_not_printed_to_stdout(
        self, get_managed_github_repos, urlopen, run
    ):
        get_managed_github_repos.return_value = [
            "owner/repo1",
            "owner/repo2",
            "other-org/repo3",
        ]

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            return MagicMock(returncode=0, stdout="")

        run.side_effect = fake_run

        response = MagicMock()
        response.status = 200
        response.read.return_value = b"fake-installation-token"
        response.__enter__.return_value = response
        urlopen.return_value = response

        out = io.StringIO()
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            with patch("sys.stdout", out):
                token = refresh_git_credentials("owner/repo1")

        self.assertEqual("fake-installation-token", token)
        self.assertEqual("", out.getvalue())
        self.assertEqual(
            ("owner/repo1", "owner/repo2"),
            getattr(token, "scoped_repositories", ()),
        )

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_github_repos")
    def test_managed_repos_expansion_failure_logs_warning(
        self, get_managed_github_repos, urlopen, run, mock_log
    ):
        import json

        get_managed_github_repos.side_effect = RuntimeError("ConfigMap not found")

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            return MagicMock()

        run.side_effect = fake_run

        response = MagicMock()
        response.status = 200
        response.read.return_value = b"fake-installation-token"
        response.__enter__.return_value = response
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repo1")

        self.assertEqual("fake-installation-token", token)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(["repo1"], body["repositories"])
        mock_log.assert_any_call(
            "WARNING: Could not expand managed repositories for token scoping: ConfigMap not found"
        )

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_sidecar_502(self, urlopen, sleep):
        # The sidecar has already executed retries internally; client fails fast
        err_502 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/forge/refresh",
            502,
            "Bad Gateway",
            email.message.Message(),
            io.BytesIO(b"Bad Gateway"),
        )
        urlopen.side_effect = err_502

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("HTTP 502", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_transport_error(self, urlopen, sleep):
        err_conn = urllib.error.URLError("Connection refused")
        urlopen.side_effect = err_conn

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn(
            "Credential sidecar failed to refresh GitHub auth", str(cm.exception)
        )
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_fails_immediately_on_4xx_without_retry(self, urlopen, sleep):
        err_403 = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/forge/refresh",
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

        self.assertIn(
            "Credential sidecar failed to refresh GitHub auth", str(cm.exception)
        )

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    def test_direct_minty_gcloud_auth_audiences_fallback(self, mock_get_managed, run):
        # First call with --audiences raises, second call without flags succeeds
        res_fail = Exception("gcloud auth print-identity-token --audiences rejected")
        res_ok = MagicMock()
        res_ok.stdout = "fallback-oidc-token\n"
        run.side_effect = [res_fail, res_ok, MagicMock(), MagicMock(), MagicMock()]

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
            self.assertIn(
                "Retrieved Google OIDC token via gcloud is empty", str(cm.exception)
            )

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_retries_on_5xx_and_succeeds(self, urlopen, sleep, mock_get_managed, run):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.side_effect = [run_oidc, MagicMock(), MagicMock(), MagicMock()]

        err_500 = urllib.error.HTTPError(
            "http://token-broker",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(b"Internal Error"),
        )
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = b"ghs_token_12345\n"
        ok_response.__enter__.return_value = ok_response

        urlopen.side_effect = [err_500, ok_response]

        with patch.dict(os.environ, {}, clear=True):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("ghs_token_12345", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_retries_on_connection_error_and_succeeds(
        self, urlopen, sleep, mock_get_managed, run
    ):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.side_effect = [run_oidc, MagicMock(), MagicMock(), MagicMock()]

        err_conn = urllib.error.URLError("Connection reset by peer")
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = b"ghs_token_12345\n"
        ok_response.__enter__.return_value = ok_response

        urlopen.side_effect = [err_conn, ok_response]

        with patch.dict(os.environ, {}, clear=True):
            token = refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertEqual("ghs_token_12345", token)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.01)

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_fails_immediately_on_403_without_retry(
        self, urlopen, sleep, run
    ):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.return_value = run_oidc

        err_403 = urllib.error.HTTPError(
            "http://token-broker",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Repository not allowed"),
        )
        urlopen.side_effect = err_403

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository", initial_delay=0.01)

        self.assertIn("Repository not allowed", str(cm.exception))
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_direct_minty_fails_after_max_retries_on_persistent_5xx(
        self, urlopen, sleep, run
    ):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.return_value = run_oidc

        err_500 = urllib.error.HTTPError(
            "http://token-broker",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(b"Database unavailable"),
        )
        urlopen.side_effect = err_500

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials(
                    "owner/repository",
                    max_attempts=3,
                    initial_delay=0.01,
                    backoff_factor=2.0,
                )

        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_has_calls([call(0.01), call(0.02)])

    @patch("github_token_refresh.subprocess.run")
    def test_direct_minty_empty_token_body_raises(self, run):
        run_oidc = MagicMock()
        run_oidc.stdout = "mock-oidc-token\n"
        run.return_value = run_oidc

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"   \n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as cm:
                    refresh_git_credentials("owner/repository")
                self.assertIn("Token received from Minty is empty", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    def test_main_cli_execution(self, run):
        with patch.object(sys, "argv", ["github_token_refresh.py", "org/repo"]):
            with patch("github_token_refresh.refresh_git_credentials") as refresh_mock:
                main()
                refresh_mock.assert_called_once_with("org/repo")

    @patch("github_token_refresh.subprocess.run")
    def test_main_cli_execution_failure_exits(self, run):
        with patch.object(sys, "argv", ["github_token_refresh.py", "org/repo"]):
            with patch(
                "github_token_refresh.refresh_git_credentials",
                side_effect=Exception("boom"),
            ):
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(1, cm.exception.code)

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    def test_skips_setup_git_when_credential_helper_already_configured(
        self, mock_get_managed, run
    ):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            if cmd[:2] == ["gh", "auth"] and cmd[2] == "login":
                return MagicMock(returncode=0)
            if cmd[:2] == ["git", "config"]:
                return MagicMock(
                    returncode=0,
                    stdout="!/usr/bin/gh auth git-credential\n",
                )
            if cmd[:3] == ["gh", "auth", "setup-git"]:
                self.fail("gh auth setup-git must not be run when helper is already configured")
            return MagicMock()

        run.side_effect = fake_run

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"ghs_test_token\n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                token = refresh_git_credentials("owner/repository")

        self.assertEqual("ghs_test_token", token)
        self.assertTrue(any(c[:2] == ["git", "config"] for c in calls))
        self.assertFalse(any(c[:3] == ["gh", "auth", "setup-git"] for c in calls))

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    def test_runs_setup_git_when_credential_helper_not_configured(
        self, mock_get_managed, run
    ):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            if cmd[:2] == ["gh", "auth"] and cmd[2] == "login":
                return MagicMock(returncode=0)
            if cmd[:2] == ["git", "config"]:
                return MagicMock(returncode=1, stdout="")
            if cmd[:3] == ["gh", "auth", "setup-git"]:
                return MagicMock(returncode=0)
            return MagicMock()

        run.side_effect = fake_run

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"ghs_test_token\n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                token = refresh_git_credentials("owner/repository")

        self.assertEqual("ghs_test_token", token)
        self.assertTrue(any(c[:2] == ["git", "config"] for c in calls))
        self.assertTrue(any(c[:3] == ["gh", "auth", "setup-git"] for c in calls))

    @patch("github_token_refresh.subprocess.run")
    @patch("gitops_workspace.get_managed_github_repos", return_value=[])
    def test_setup_git_failure_includes_stderr_in_raised_error(
        self, mock_get_managed, run
    ):
        import subprocess

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="fake-oidc-token\n")
            if cmd[:2] == ["gh", "auth"] and cmd[2] == "login":
                return MagicMock(returncode=0)
            if cmd[:2] == ["git", "config"]:
                return MagicMock(returncode=1, stdout="")
            if cmd[:3] == ["gh", "auth", "setup-git"]:
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    stderr="error: could not lock config file /home/.gitconfig: File exists",
                )
            return MagicMock()

        run.side_effect = fake_run

        with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
            ok_response = MagicMock()
            ok_response.status = 200
            ok_response.read.return_value = b"ghs_test_token\n"
            ok_response.__enter__.return_value = ok_response
            urlopen.return_value = ok_response

            with patch.dict(os.environ, {}, clear=True):
                out = io.StringIO()
                with patch("sys.stdout", out):
                    with self.assertRaises(RuntimeError) as cm:
                        refresh_git_credentials("owner/repository")

        self.assertEqual("", out.getvalue())
        self.assertIn(
            "error: could not lock config file /home/.gitconfig: File exists",
            str(cm.exception),
        )


class SandboxForwardTest(unittest.TestCase):
    """The gateway pod holds nothing that can mint, so it forwards.

    Without this branch a `no_agent` cron job on the gateway falls through to
    the direct mint, which would mint on the pod the credential split exists
    to keep empty.
    """

    def setUp(self):
        # The direct mint these tests fall through to would otherwise GET the
        # real metadata address from whatever host runs the suite.
        no_metadata = patch("github_token_refresh.metadata_identity_token", return_value=None)
        no_metadata.start()
        self.addCleanup(no_metadata.stop)

    def _sandbox(self, enabled, completed=None):
        import subprocess

        module = MagicMock()
        module.sandbox_enabled.return_value = enabled
        module.run.return_value = completed or subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        )
        return module

    def test_the_gateway_forwards_the_mint_into_the_sandbox(self):
        sandbox = self._sandbox(True)
        with patch.dict(sys.modules, {"sandbox_exec": sandbox}):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual("", refresh_git_credentials("owner/repository"))
        sandbox.run.assert_called_once_with(
            [
                "python3",
                github_token_refresh.SANDBOX_REFRESH_SCRIPT,
                "owner/repository",
            ],
            timeout=github_token_refresh.SANDBOX_REFRESH_TIMEOUT_SECONDS,
        )

    def test_a_nonzero_exit_in_the_sandbox_raises_rather_than_returning_quietly(self):
        import subprocess

        sandbox = self._sandbox(
            True,
            subprocess.CompletedProcess([], 3, stdout="", stderr="broker said no\n"),
        )
        with patch.dict(sys.modules, {"sandbox_exec": sandbox}):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as cm:
                    refresh_git_credentials("owner/repository")
        self.assertIn("exit 3", str(cm.exception))
        self.assertIn("broker said no", str(cm.exception))

    @patch("github_token_refresh.subprocess.run")
    def test_no_sandbox_leaves_the_direct_mint_alone(self, run):
        # The sandbox is not configured, so this is the credential-holding
        # deployment and the branch has to stay out of the way.
        sandbox = self._sandbox(False)
        run.side_effect = [Exception("fail1"), Exception("fail2")]
        with patch.dict(sys.modules, {"sandbox_exec": sandbox}):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as cm:
                    refresh_git_credentials("owner/repository")
        self.assertIn("Failed to retrieve Google OIDC token", str(cm.exception))
        sandbox.run.assert_not_called()

    def test_the_credential_proxy_url_still_wins(self):
        # In the sandbox both are true. Taking the sandbox branch there would
        # ssh into the pod the call is already running in.
        sandbox = self._sandbox(True)
        with patch.dict(sys.modules, {"sandbox_exec": sandbox}):
            with patch("github_token_refresh.urllib.request.urlopen") as urlopen:
                response = MagicMock()
                response.status = 200
                response.__enter__.return_value = response
                urlopen.return_value = response
                with patch.dict(
                    os.environ,
                    {"CREDENTIAL_PROXY_URL": "http://broker:8765"},
                    clear=True,
                ):
                    self.assertEqual("", refresh_git_credentials("owner/repository"))
        sandbox.run.assert_not_called()


class LooksLikeAuthFailureTest(unittest.TestCase):
    def _proc(self, returncode: int, stderr: str = ""):
        import subprocess
        return subprocess.CompletedProcess(["gh"], returncode, stdout="", stderr=stderr)

    def test_auth_status_failure_is_always_auth_failure(self):
        self.assertTrue(
            github_token_refresh.looks_like_auth_failure(["auth", "status"], self._proc(1))
        )

    def test_bad_credentials_and_401(self):
        self.assertTrue(
            github_token_refresh.looks_like_auth_failure(
                ["api"], self._proc(1, "HTTP 401: Bad credentials")
            )
        )
        self.assertTrue(
            github_token_refresh.looks_like_auth_failure(
                ["api"], self._proc(1, "requires authentication")
            )
        )
        self.assertTrue(
            github_token_refresh.looks_like_auth_failure(
                ["api"], self._proc(1, "token is invalid")
            )
        )

    def test_success_and_non_auth_failures(self):
        self.assertFalse(
            github_token_refresh.looks_like_auth_failure(["api"], self._proc(0))
        )
        self.assertFalse(
            github_token_refresh.looks_like_auth_failure(
                ["api"], self._proc(1, "HTTP 404: Not Found")
            )
        )
        self.assertFalse(
            github_token_refresh.looks_like_auth_failure(
                ["api"], self._proc(github_token_refresh.GH_MISSING_RC, "binary not found")
            )
        )
        self.assertFalse(
            github_token_refresh.looks_like_auth_failure(
                ["api"], self._proc(github_token_refresh.GH_TIMEOUT_RC, "timed out")
            )
        )


class RefreshCredentialsOnceTest(unittest.TestCase):
    def test_refresh_credentials_once_at_most_once(self):
        github_token_refresh.reset_refresh_state()
        with patch("gitops_workspace.get_managed_github_repos", return_value=["acme/toolkit"]), \
             patch("github_token_refresh.refresh_git_credentials") as mock_refresh:
            self.assertTrue(github_token_refresh.refresh_credentials_once())
            mock_refresh.assert_called_once_with("acme/toolkit")
            self.assertFalse(github_token_refresh.refresh_credentials_once())
            self.assertEqual(mock_refresh.call_count, 1)


class ReadOnlyMintTest(unittest.TestCase):
    """`--read-only`: one repository, the read scope, straight to Minty, nothing installed."""

    def setUp(self):
        # These tests describe the gcloud path. The metadata server is asked
        # before it and would otherwise take the generic urlopen mocks below for
        # its own answer; MetadataIdentityTest covers it.
        no_metadata = patch("github_token_refresh.metadata_identity_token", return_value=None)
        no_metadata.start()
        self.addCleanup(no_metadata.stop)

    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    @patch("gitops_workspace.get_managed_github_repos")
    def test_requests_the_read_scope_for_one_repository_and_installs_nothing(
        self, get_managed_github_repos, urlopen, run, fetch
    ):
        import json

        # The managed list is what the write mint widens its request with. The
        # read mint must not consult it: the token is per clone of one
        # repository, and widening it would grant a read of every managed
        # repository to a clone that asked for one context repository.
        get_managed_github_repos.return_value = ["owner/repo1", "owner/repo2"]

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gcloud":
                return MagicMock(stdout="fake-oidc-token\n")
            self.fail(f"the read-only mint ran a subprocess it must not: {cmd}")

        run.side_effect = fake_run
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"fake-read-token\n"
        response.__enter__.return_value = response
        urlopen.return_value = response

        # Even with a sidecar URL in the environment: this runs *in* the
        # sidecar, and there is nothing to delegate to.
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://sidecar.invalid"}):
            token = github_token_refresh.mint_read_only_token("owner/repo1")

        self.assertEqual("fake-read-token", token)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("owner", body["org_name"])
        self.assertEqual(["repo1"], body["repositories"])
        self.assertEqual("platform-agent-read-scope", body["scope"])
        get_managed_github_repos.assert_not_called()
        for invocation in run.call_args_list:
            self.assertEqual("gcloud", invocation.args[0][0])

    def test_refuses_anything_that_is_not_an_owner_slash_repo(self):
        for repository in (None, "", "owner", "github.com/owner", "owner/repo/extra"):
            with self.subTest(repository=repository):
                with self.assertRaises(RuntimeError):
                    github_token_refresh.mint_read_only_token(repository)

    @patch("github_token_refresh.mint_read_only_token", return_value="fake-read-token")
    def test_main_prints_the_token_and_only_the_token(self, mint):
        out = io.StringIO()
        with patch.object(sys, "argv", ["github_token_refresh.py", "--read-only", "owner/repo"]):
            with patch("sys.stdout", out):
                main()
        self.assertEqual("fake-read-token", out.getvalue())
        mint.assert_called_once_with("owner/repo")

    @patch("github_token_refresh.mint_read_only_token", side_effect=RuntimeError("Minty down"))
    def test_main_read_only_failure_exits_nonzero_with_nothing_on_stdout(self, mint):
        out = io.StringIO()
        with patch.object(sys, "argv", ["github_token_refresh.py", "--read-only", "owner/repo"]):
            with patch("sys.stdout", out):
                with self.assertRaises(SystemExit) as cm:
                    main()
        self.assertEqual(1, cm.exception.code)
        self.assertEqual("", out.getvalue())

    @patch("github_token_refresh.subprocess.run")
    def test_main_without_the_flag_still_refreshes(self, run):
        # The positional form every existing caller uses is unchanged.
        with patch.object(sys, "argv", ["github_token_refresh.py", "org/repo"]):
            with patch("github_token_refresh.refresh_git_credentials") as refresh:
                main()
        refresh.assert_called_once_with("org/repo")

    @patch("github_token_refresh.subprocess.run")
    def test_main_prints_scoped_repositories_to_stdout_on_success(self, run):
        out = io.StringIO()
        with patch.object(sys, "argv", ["github_token_refresh.py", "owner/repo1"]):
            with patch(
                "github_token_refresh.refresh_git_credentials",
                return_value=github_token_refresh.RefreshToken(
                    "fake-token", ("owner/repo1", "owner/repo2")
                ),
            ) as refresh:
                with patch("sys.stdout", out):
                    main()
        refresh.assert_called_once_with("owner/repo1")
        self.assertEqual("owner/repo1\nowner/repo2\n", out.getvalue())


class MetadataIdentityTest(unittest.TestCase):
    """The identity token comes from the metadata server directly; gcloud is the fallback."""

    MINTY = github_token_refresh.TOKEN_BROKER_URL
    METADATA = github_token_refresh.METADATA_IDENTITY_URL

    def setUp(self):
        github_token_refresh.identity_note.update(minted="", fell_through="")
        # A credential file configured on the host running the suite would
        # route these tests to gcloud; the one test about that sets its own.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in github_token_refresh.CREDENTIAL_FILE_VARIABLES:
            os.environ.pop(name, None)

    def _urlopen(self, metadata, minty=b"ghs_installation_token"):
        """A urlopen that answers the metadata server from `metadata` and Minty with `minty`.

        `metadata` is a list consumed one answer per call: bytes are a body
        (a list of bytes is a body delivered in those chunks; a tuple of a
        body and a number is a Content-Length answer that closed with that
        many bytes still owed), an exception instance is raised. The same
        fake is installed as `metadata_open`,
        the proxy-less opener the metadata GET goes through, for the test's
        duration.
        """
        answers = list(metadata)
        self.metadata_calls = []

        def fake(request, timeout=None):
            url = request.full_url
            response = MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            if url.startswith(self.METADATA):
                self.metadata_calls.append(request)
                self.assertEqual("Google", request.headers["Metadata-flavor"])
                self.assertEqual(github_token_refresh.METADATA_TIMEOUT_SECONDS, timeout)
                answer = answers.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                owed = 0
                if isinstance(answer, tuple):
                    answer, owed = answer
                chunks = answer if isinstance(answer, list) else [answer]
                # read1(n) hands out the chunks, one per receive, then EOF;
                # `length` is what a Content-Length answer still owes then.
                response.read1.side_effect = chunks + [b""]
                response.length = owed
            else:
                self.assertEqual(self.MINTY, url)
                response.read.return_value = minty
            return response

        opener = patch("github_token_refresh.metadata_open", fake)
        opener.start()
        self.addCleanup(opener.stop)
        return fake

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_the_metadata_server_is_asked_before_gcloud(self, urlopen, run, fetch, log):
        urlopen.side_effect = self._urlopen([b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU\n"])
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repository")

        self.assertEqual("ghs_installation_token", token)
        # The branch that minted is on the last line too: the sidecar keeps
        # the tail of this output.
        last = str(log.call_args.args[0])
        self.assertTrue(last.startswith("GitHub authentication successfully configured"), last)
        self.assertIn("(identity token from the metadata server in ", last)
        self.assertNotIn(
            "print-identity-token",
            " ".join(str(call.args[0]) for call in run.call_args_list),
        )
        self.assertEqual(
            "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU", urlopen.call_args.args[0].headers["X-oidc-token"]
        )

    def test_the_metadata_request_names_the_audience_and_the_full_format(self):
        seen = []

        def fake(request, timeout=None):
            seen.append(request)
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        with patch("github_token_refresh.metadata_open", fake), patch("github_token_refresh.log"):
            self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        self.assertIn(f"audience={urllib.parse.quote(self.MINTY, safe='')}", seen[0].full_url)
        self.assertIn("format=full", seen[0].full_url)
        self.assertEqual("Google", seen[0].headers["Metadata-flavor"])

    def test_the_metadata_opener_sends_nothing_through_an_http_proxy(self):
        # The broker forwards HTTP_PROXY into this helper for its GitHub and
        # Minty traffic; the link-local metadata address must not take it. The
        # opener is built at import, so the module is reloaded with a proxy in
        # the environment: the default opener then carries a ProxyHandler for
        # it, and the metadata opener must carry none. (build_opener drops a
        # ProxyHandler that has no proxies to handle.)
        import importlib

        def http_proxies(opener):
            # Only the http entry: the host running the suite may carry other
            # *_proxy variables of its own.
            return [h.proxies.get("http") for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]

        with patch.dict(os.environ, {"http_proxy": "http://egress.invalid:3128"}):
            try:
                module = importlib.reload(github_token_refresh)
                self.assertEqual(["http://egress.invalid:3128"], http_proxies(urllib.request.build_opener()))
                self.assertEqual([], http_proxies(module.metadata_open.__self__))
            finally:
                importlib.reload(github_token_refresh)

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_a_short_dotted_body_is_not_a_token(self, sleep, log):
        # Three runs with two dots is not a JWT: a JWT's header is a JSON
        # object, so it begins `eyJ`, and no segment is three characters.
        for body in (b"x.y.z", b"1.2.3", b"ok.ok.ok", b"abcdefghij.abcdefghij.abcdefghij"):
            with self.subTest(body=body):
                self._urlopen([body])
                self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
                self.assertIn("body is not a JWT", str(log.call_args.args[0]))
        sleep.assert_not_called()

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_a_configured_credential_file_keeps_the_identity_on_gcloud(self, urlopen, run, fetch, log):
        # gcloud pointed at a key file presents that account, not the
        # instance's; the metadata server must not outrank it.
        urlopen.side_effect = self._urlopen([])

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="gcloud.id.token\n")
            return MagicMock()

        run.side_effect = fake_run
        for name in github_token_refresh.CREDENTIAL_FILE_VARIABLES:
            with self.subTest(variable=name):
                with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "", name: "/var/run/key.json"}):
                    refresh_git_credentials("owner/repository")
                self.assertEqual([], self.metadata_calls)
                self.assertEqual("gcloud.id.token", urlopen.call_args.args[0].headers["X-oidc-token"])
                lines = [str(call.args[0]) for call in log.call_args_list]
                self.assertTrue(any(f"{name} names a credential file" in line for line in lines), lines)
                self.assertIn(f"through gcloud in", lines[-1])
                self.assertIn(f"({name} names a credential file)", lines[-1])
                os.environ.pop(name, None)

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_a_textual_error_page_is_not_a_token(self, sleep, log):
        # Whatever answers at the address with a 200 that is not a JWT: an
        # egress proxy's page, a captive gateway. It falls through, no retry.
        self._urlopen([b"<html><title>403 Forbidden</title></html>"])
        self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        sleep.assert_not_called()
        line = str(log.call_args.args[0])
        self.assertIn("body is not a JWT", line)
        self.assertIn("asking gcloud", line)

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_an_attempt_that_outlives_its_bound_is_given_up_on_and_retried(self, sleep, log):
        # A peer that is slow between socket operations (a header byte at a
        # time, a dripping body) never trips the socket timeout; the wall-clock
        # bound around the attempt is what ends it, as a slow answer: retried
        # once, then given up on.
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(request, timeout=None):
            release.wait()
            raise urllib.error.URLError(ConnectionResetError("released"))

        with patch("github_token_refresh.metadata_open", stuck), patch.object(
            github_token_refresh, "METADATA_TIMEOUT_SECONDS", 0.05
        ):
            self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        sleep.assert_called_once_with(github_token_refresh.METADATA_RETRY_DELAY_SECONDS)
        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertIn("attempt 1 timed out; retrying", lines[0])
        self.assertIn("attempt still running after 0.05s", lines[-1])

    def test_the_metadata_opener_follows_no_redirect(self):
        # A 3xx from the address is an error that falls through, not a hop to
        # somewhere else under its own timeouts.
        opener = github_token_refresh.metadata_open.__self__
        redirectors = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(1, len(redirectors))
        self.assertIsNone(redirectors[0].redirect_request(None, None, 302, "Found", {}, "http://elsewhere.invalid/"))

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_a_token_cut_short_by_an_early_close_is_not_a_token(self, sleep, log):
        # Content-Length 1104, 900 bytes sent, then a clean close: read1 says
        # EOF without raising, and the cut fell inside the signature, so the
        # shape check alone would pass it. It falls through, no retry.
        head = "eyJ" + "a" * 20 + "." + "b" * 30 + "." + "c" * 845
        self._urlopen([(head.encode(), 204)])
        self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        sleep.assert_not_called()
        line = str(log.call_args.args[0])
        self.assertIn("IncompleteRead", line)
        self.assertIn("asking gcloud", line)

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_a_body_larger_than_a_token_is_not_a_token(self, sleep, log):
        self._urlopen([[b"a" * 1024] * 9])
        self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        sleep.assert_not_called()
        self.assertIn("larger than a token", str(log.call_args.args[0]))

    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_one_slow_metadata_answer_is_retried_not_failed(self, urlopen, run, fetch, sleep):
        # urlopen raises a bare timeout while reading and wraps one raised while
        # connecting in URLError; both are slow answers and both are retried.
        urlopen.side_effect = self._urlopen(
            [urllib.error.URLError(socket.timeout("timed out")), b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU"]
        )
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            refresh_git_credentials("owner/repository")

        self.assertEqual(
            "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU", urlopen.call_args.args[0].headers["X-oidc-token"]
        )
        sleep.assert_called_once_with(github_token_refresh.METADATA_RETRY_DELAY_SECONDS)
        self.assertNotIn(
            "print-identity-token",
            " ".join(str(call.args[0]) for call in run.call_args_list),
        )

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_no_metadata_server_falls_through_to_gcloud_and_says_so(
        self, urlopen, run, fetch, sleep, log
    ):
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        urlopen.side_effect = self._urlopen([refused])

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="gcloud.id.token\n")
            return MagicMock()

        run.side_effect = fake_run
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            refresh_git_credentials("owner/repository")

        self.assertEqual(
            "gcloud.id.token", urlopen.call_args.args[0].headers["X-oidc-token"]
        )
        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertTrue(any("no identity token from the metadata server" in line and "Connection refused" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("Minted the broker OIDC token through gcloud in") for line in lines), lines)
        # A refusal is a definite no: not retried, no pause.
        self.assertEqual(1, len(self.metadata_calls))
        sleep.assert_not_called()

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_an_error_status_from_the_metadata_server_falls_through_to_gcloud(
        self, urlopen, run, fetch, sleep, log
    ):
        # An unannotated ServiceAccount, or a placement whose federated shape
        # fetch_identity_token declines: the endpoint answers an error status.
        not_found = urllib.error.HTTPError(self.METADATA, 404, "Not Found", email.message.Message(), io.BytesIO(b""))
        urlopen.side_effect = self._urlopen([not_found])

        def fake_run(cmd, **kwargs):
            if "print-identity-token" in cmd:
                return MagicMock(stdout="gcloud.id.token\n")
            return MagicMock()

        run.side_effect = fake_run
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            refresh_git_credentials("owner/repository")

        self.assertEqual(
            "gcloud.id.token", urlopen.call_args.args[0].headers["X-oidc-token"]
        )
        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertTrue(any("no identity token from the metadata server" in line and "404" in line for line in lines), lines)
        sleep.assert_not_called()
        self.assertIn("through gcloud in", lines[-1])
        self.assertIn("(the metadata server gave none: HTTP Error 404", lines[-1])

    @patch("github_token_refresh.time.sleep")
    def test_an_empty_metadata_body_is_no_token(self, sleep):
        self._urlopen([b"  \n"])
        self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        self.assertEqual(1, len(self.metadata_calls))
        sleep.assert_not_called()

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_a_bare_read_timeout_is_retried_and_an_empty_body_after_it_is_named_as_such(self, sleep, log):
        # A timeout raised while reading arrives bare, not wrapped in URLError;
        # it is retried like the connect shape. The empty body that follows is
        # what the log names, not the timeout before it.
        self._urlopen([socket.timeout("timed out"), b""])
        self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        self.assertEqual(2, len(self.metadata_calls))
        sleep.assert_called_once_with(github_token_refresh.METADATA_RETRY_DELAY_SECONDS)
        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertIn("attempt 1 timed out; retrying", lines[0])
        self.assertIn("empty token", lines[-1])
        self.assertNotIn("timed out", lines[-1])

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    @patch("github_token_refresh.time.monotonic")
    def test_a_slow_then_recovered_mint_reports_the_whole_steps_time(self, clock, sleep, log):
        # The first attempt times out at 3 s, the retry answers in 70 ms: the
        # line says what the step cost, not what the last attempt did.
        # The step's start, the timeout's reason stamp, then the line.
        clock.side_effect = _Clock([100.0, 103.0], 103.57)
        self._urlopen([socket.timeout("timed out"), b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU"])
        self.assertEqual("eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU", github_token_refresh.metadata_identity_token(self.MINTY))
        self.assertEqual(
            "Minted the broker OIDC token from the metadata server in 3.57s (attempt 2).",
            str(log.call_args.args[0]),
        )

    @patch("github_token_refresh.log")
    @patch("github_token_refresh.time.sleep")
    def test_a_body_that_is_not_utf8_falls_through_instead_of_raising(self, sleep, log):
        # Whatever answers in the metadata address's place (a proxy's binary
        # error page) is not a token; it falls through to gcloud like a 404.
        self._urlopen([b"\xff\xfe\x00binary"])
        self.assertIsNone(github_token_refresh.metadata_identity_token(self.MINTY))
        self.assertEqual(1, len(self.metadata_calls))
        sleep.assert_not_called()
        line = str(log.call_args.args[0])
        self.assertIn("asking gcloud", line)
        self.assertIn("codec can't decode", line)

    @patch("github_token_refresh.metadata_identity_token", return_value=None)
    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    def test_a_gcloud_failure_names_its_stderr_and_its_timeout(self, run, fetch, metadata):
        run.side_effect = [
            subprocess.CalledProcessError(
                1, ["gcloud", "auth", "print-identity-token", "--audiences=x"],
                stderr="ERROR: Invalid account type for `--audiences`.",
            ),
            subprocess.TimeoutExpired(["gcloud", "auth", "print-identity-token"], 5),
        ]
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository")

        message = str(cm.exception)
        self.assertIn("Failed to retrieve Google OIDC token via gcloud", message)
        self.assertIn("Invalid account type", message)
        self.assertIn("timed out after 5 seconds", message)

    @patch("github_token_refresh.wif_credentials.fetch_identity_token", return_value=None)
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_a_gh_login_failure_keeps_what_gh_printed(self, urlopen, run, fetch):
        urlopen.side_effect = self._urlopen([b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIn0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU"])
        run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "auth", "login", "--with-token"],
            stderr="error validating token: HTTP 401: Bad credentials",
        )
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            with self.assertRaises(RuntimeError) as cm:
                refresh_git_credentials("owner/repository")

        self.assertIn("Failed to configure GitHub auth in gh CLI", str(cm.exception))
        self.assertIn("Bad credentials", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
