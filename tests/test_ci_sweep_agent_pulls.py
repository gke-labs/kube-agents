"""The teardown's pull-request sweep closes the agent's leftovers and nothing else.

A remediation scenario opens a pull request in the leased project's GitOps
repository. Nothing closed it, so the next lease of that project met its
predecessor's: `create_pull_request` treats "a pull request already exists" as
success and returns the old one's URL, and a repetition that opened nothing
could be graded as if it had (#1755). `hack/ci_sweep_agent_pulls.py` is what
removes the leftover; these tests pin what it is allowed to touch.

Four properties, and the first is the one that matters. The script runs with a
credential that can write pull requests across the pool's repositories, so
"which pull requests are the agent's" is a security question, not a tidiness
one. It is the same question `is_agent_pull_request` in
agents/platform/scripts/forge.py answers, and it needs all three of its
conditions: a branch prefix alone is not ownership, because anyone who can fork
can name a branch with it.

Second, the author it looks for is the agent's, not its own. forge.py asks that
question from inside the agent, where the two are the same account; the sweep
signs as a different App, and one that matched its own bot would close nothing
and report success.

Third, the token is narrowed at mint time -- to one repository, and to
`pull_requests: write` -- so a teardown never holds the reach the App has. The
App also holds `issues: read` for the grading path, and a token that inherited
the installation whole would carry it.

Fourth, a permission the organisation has not accepted yet has to read as what
it is. GitHub answers that with a 403 or a 422 whose text is about tokens; the
usual cause is a pending click in the organisation's settings, and a reader who
is told the former goes looking in the wrong place.
"""

import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "hack" / "ci_sweep_agent_pulls.py"
_FORGE = _REPO_ROOT / "agents" / "platform" / "scripts" / "forge.py"
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_spec = importlib.util.spec_from_file_location("ci_sweep_agent_pulls", _MODULE_PATH)
sweeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweeper)

REPO = "gke-agentic/kube-agents-evals-7-infra"
# The agent's author login, not the sweep's. The sweep signs as the ledger App,
# and SWEEPER_BOT is what it would be matching if it asked GitHub who it is.
BOT = "kube-agents-evals-token-minter[bot]"
SWEEPER_BOT = "kube-agents-evals-ledger-reader[bot]"


def agent_pull(number=1, branch="platform-agent/fix-the-thing", author=BOT, head_repo=REPO):
    return {
        "number": number,
        "user": {"login": author},
        "head": {"ref": branch, "repo": {"full_name": head_repo}},
    }


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _GitHub:
    """A recording stand-in for api.github.com.

    Keyed on "<METHOD> <path>", so an assertion can name the call it means
    rather than an index into a list that shifts whenever a call is added.
    """

    def __init__(self, pulls=None, mint_error=None, close_errors=None):
        self.calls = []
        self.pulls = pulls if pulls is not None else []
        self.mint_error = mint_error
        # Keyed by pull-request number, so one close can fail while the rest
        # succeed. There is no arm for "GET /app": asking GitHub who the sweep
        # is would be the bug, so the stub answers it with an AssertionError.
        self.close_errors = close_errors or {}

    def __call__(self, request, timeout=None):
        key = "%s %s" % (request.method, request.full_url.replace(sweeper.API_ROOT, ""))
        body = json.loads(request.data) if request.data else None
        self.calls.append((key, body))

        if key.startswith("GET /repos/") and key.endswith("/installation"):
            return _Response(json.dumps({"id": 157029058}).encode())
        if key.startswith("POST /app/installations/"):
            if self.mint_error is not None:
                raise self.mint_error
            return _Response(json.dumps({"token": "ghs_fake"}).encode())
        if key.startswith("GET /repos/") and "/pulls?" in key:
            page = int(key.rsplit("page=", 1)[1])
            return _Response(json.dumps(self.pulls if page == 1 else []).encode())
        if key.startswith("PATCH /repos/"):
            failure = self.close_errors.get(int(key.rsplit("/", 1)[1]))
            if failure is not None:
                raise failure
            return _Response(b"{}")
        raise AssertionError("unexpected call %s" % key)

    def bodies(self, prefix):
        return [body for key, body in self.calls if key.startswith(prefix)]

    def keys(self, prefix):
        return [key for key, _ in self.calls if key.startswith(prefix)]


def _http_error(code):
    return urllib.error.HTTPError("https://api.github.com", code, "reason", {}, None)


def run_sweep(github, **kwargs):
    signed = mock.Mock(returncode=0, stdout=b"signature", stderr=b"")
    with mock.patch.object(sweeper.urllib.request, "urlopen", github), mock.patch.object(
        sweeper.subprocess, "run", return_value=signed
    ):
        return sweeper.sweep(REPO, "4739812", "/etc/ledger-app-key/key.pem", **kwargs)


class OwnershipTest(unittest.TestCase):
    """All three of forge.py's conditions, each one load-bearing."""

    def test_the_agents_own_pull_request_is_owned(self):
        self.assertTrue(sweeper.is_agent_pull_request(agent_pull(), REPO, BOT))

    def test_another_author_is_not(self):
        pull = agent_pull(author="some-human")
        self.assertFalse(sweeper.is_agent_pull_request(pull, REPO, BOT))

    def test_a_branch_without_the_prefix_is_not(self):
        pull = agent_pull(branch="hotfix/urgent")
        self.assertFalse(sweeper.is_agent_pull_request(pull, REPO, BOT))

    def test_a_fork_head_is_not(self):
        # The case the prefix alone cannot catch: anyone who can fork can name
        # a branch platform-agent/anything and open a pull request from it.
        pull = agent_pull(head_repo="someone-else/kube-agents-evals-7-infra")
        self.assertFalse(sweeper.is_agent_pull_request(pull, REPO, BOT))

    def test_a_deleted_head_repository_is_not(self):
        pull = agent_pull()
        pull["head"]["repo"] = None
        self.assertFalse(sweeper.is_agent_pull_request(pull, REPO, BOT))

    def test_the_prefix_matches_the_agents(self):
        # The script deletes nothing, but it closes by this prefix, and the
        # agent chooses branch names by forge.py's copy of it. Two constants
        # that must agree, in files that do not read each other.
        forge = _FORGE.read_text(encoding="utf-8")
        self.assertIn(
            'AGENT_BRANCH_PREFIX = "%s"' % sweeper.AGENT_BRANCH_PREFIX,
            forge,
        )


class AgentAuthorTest(unittest.TestCase):
    """The author looked for is the agent's, not the sweep's own.

    forge.py asks "is this mine?" from inside the agent, where the credential
    and the author are one account. The sweep is a third party: it signs with
    the ledger App and the pull requests are the token-minter App's. Matching
    its own login would close nothing, and report a clean sweep for doing it.
    """

    def test_the_agents_pull_request_is_closed(self):
        github = _GitHub(pulls=[agent_pull(author=BOT)])
        self.assertEqual(run_sweep(github), 1)

    def test_a_pull_request_by_the_sweeps_own_bot_is_left(self):
        github = _GitHub(pulls=[agent_pull(author=SWEEPER_BOT)])
        self.assertEqual(run_sweep(github), 0)
        self.assertEqual(github.keys("PATCH "), [])

    def test_the_login_is_the_app_the_agent_submits_with(self):
        # Two constants in files that do not read each other, and no endpoint
        # from an App id to its slug -- so the name is pinned to the script
        # that hands the App to the agent.
        deploy = _CI_DEPLOY.read_text(encoding="utf-8")
        self.assertIn(sweeper.AGENT_APP_SLUG, deploy)
        self.assertEqual(sweeper.AGENT_BOT_LOGIN, sweeper.AGENT_APP_SLUG + "[bot]")


class TokenScopeTest(unittest.TestCase):
    """The token is narrowed twice, and neither narrowing is decoration."""

    def test_the_token_is_scoped_to_one_repository(self):
        github = _GitHub()
        run_sweep(github)
        mint = github.bodies("POST /app/installations/")[0]
        self.assertEqual(mint["repositories"], ["kube-agents-evals-7-infra"])

    def test_the_token_asks_for_pull_requests_write_and_nothing_else(self):
        # The App also holds issues: read, for the grading path. A token that
        # inherited the installation whole would carry it into the teardown.
        github = _GitHub()
        run_sweep(github)
        mint = github.bodies("POST /app/installations/")[0]
        self.assertEqual(mint["permissions"], {"pull_requests": "write"})

    def test_the_installation_is_resolved_from_the_repository(self):
        # One App serves thirty repositories. A hardcoded installation id is a
        # silent 404 on the other twenty-nine.
        github = _GitHub()
        run_sweep(github)
        self.assertIn("GET /repos/%s/installation" % REPO, github.keys("GET /repos/"))


class ClosingTest(unittest.TestCase):
    def test_only_the_agents_pull_requests_are_closed(self):
        github = _GitHub(
            pulls=[
                agent_pull(number=1),
                agent_pull(number=2, author="a-human"),
                agent_pull(number=3, branch="release/1.2"),
                agent_pull(number=4, head_repo="fork/kube-agents-evals-7-infra"),
                agent_pull(number=5),
            ]
        )
        closed = run_sweep(github)
        self.assertEqual(closed, 2)
        self.assertEqual(
            github.keys("PATCH "),
            ["PATCH /repos/%s/pulls/1" % REPO, "PATCH /repos/%s/pulls/5" % REPO],
        )

    def test_closing_sets_the_state_and_nothing_else(self):
        github = _GitHub(pulls=[agent_pull()])
        run_sweep(github)
        self.assertEqual(github.bodies("PATCH ")[0], {"state": "closed"})

    def test_an_empty_repository_closes_nothing(self):
        github = _GitHub(pulls=[])
        self.assertEqual(run_sweep(github), 0)
        self.assertEqual(github.keys("PATCH "), [])

    def test_dry_run_reports_without_closing(self):
        github = _GitHub(pulls=[agent_pull()])
        self.assertEqual(run_sweep(github, dry_run=True), 1)
        self.assertEqual(github.keys("PATCH "), [])


class CloseFailureTest(unittest.TestCase):
    """One close that fails must not abandon the ones behind it."""

    def test_the_rest_are_still_closed(self):
        github = _GitHub(
            pulls=[agent_pull(number=1), agent_pull(number=2), agent_pull(number=3)],
            close_errors={2: _http_error(409)},
        )
        with self.assertRaises(sweeper.SweepError):
            run_sweep(github)
        self.assertEqual(
            github.keys("PATCH "),
            ["PATCH /repos/%s/pulls/%d" % (REPO, n) for n in (1, 2, 3)],
        )

    def test_the_failure_names_what_was_left_open(self):
        github = _GitHub(pulls=[agent_pull(number=7)], close_errors={7: _http_error(403)})
        with self.assertRaises(sweeper.SweepError) as caught:
            run_sweep(github)
        self.assertIn("#7", str(caught.exception))

    def test_an_unreachable_github_mid_sweep_is_survived(self):
        github = _GitHub(
            pulls=[agent_pull(number=1), agent_pull(number=2)],
            close_errors={1: OSError("connection reset")},
        )
        with self.assertRaises(sweeper.SweepError):
            run_sweep(github)
        self.assertIn("PATCH /repos/%s/pulls/2" % REPO, github.keys("PATCH "))


class MintFailureTest(unittest.TestCase):
    """A pending organisation click must not read as a code fault."""

    def test_a_permission_not_yet_accepted_names_the_human_step(self):
        for code in sweeper.PERMISSION_NOT_GRANTED_CODES:
            with self.subTest(code=code):
                github = _GitHub(mint_error=_http_error(code))
                with self.assertRaises(sweeper.SweepError) as caught:
                    run_sweep(github)
                self.assertIn("organisation owner", str(caught.exception))
                self.assertEqual(github.keys("PATCH "), [])

    def test_a_credential_fault_names_the_app_and_the_repository(self):
        github = _GitHub(mint_error=_http_error(401))
        with self.assertRaises(sweeper.SweepError) as caught:
            run_sweep(github)
        message = str(caught.exception)
        self.assertIn("4739812", message)
        self.assertIn(REPO, message)
        self.assertNotIn("organisation owner", message)

    def test_an_unsigned_jwt_stops_before_any_call(self):
        github = _GitHub()
        signed = mock.Mock(returncode=1, stdout=b"", stderr=b"no such file")
        with mock.patch.object(sweeper.urllib.request, "urlopen", github), mock.patch.object(
            sweeper.subprocess, "run", return_value=signed
        ):
            with self.assertRaises(sweeper.SweepError):
                sweeper.sweep(REPO, "4739812", "/absent.pem")
        self.assertEqual(github.calls, [])


class ExitCodeTest(unittest.TestCase):
    """main() turns every fault into a nonzero exit and a line on stderr."""

    def _main(self, github, subprocess_result=None):
        signed = subprocess_result or mock.Mock(returncode=0, stdout=b"sig", stderr=b"")
        argv = ["--repo", REPO, "--app-id", "4739812", "--key-file", "/k.pem"]
        with mock.patch.object(sweeper.urllib.request, "urlopen", github), mock.patch.object(
            sweeper.subprocess, "run", return_value=signed
        ):
            return sweeper.main(argv)

    def test_a_clean_sweep_exits_zero(self):
        self.assertEqual(self._main(_GitHub(pulls=[agent_pull()])), 0)

    def test_a_mint_failure_exits_one(self):
        self.assertEqual(self._main(_GitHub(mint_error=_http_error(401))), 1)

    def test_an_unreachable_github_exits_one(self):
        def unreachable(request, timeout=None):
            raise OSError("connection reset")

        self.assertEqual(self._main(unreachable), 1)


if __name__ == "__main__":
    unittest.main()
