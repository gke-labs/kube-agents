"""The pool sweep closes the agent's leftovers and nothing else, from outside any run.

A remediation scenario opens a pull request in the leased project's GitOps
repository. Nothing closed it, so the next lease of that project met its
predecessor's: `create_pull_request` treats "a pull request already exists" as
success and returns the old one's URL (#1755). `hack/ci_sweep_agent_pulls.py`
is what removes the leftover; these tests pin what it is allowed to touch and
where it is allowed to run.

Five properties. First, ownership: the script holds a credential that can
write pull requests, so "which pull requests are the agent's" is a security
question. It is the same question `is_agent_pull_request` in
agents/platform/scripts/forge.py answers, and it needs all three of its
conditions: a branch prefix alone is not ownership, because anyone who can fork
can name a branch with it.

Second, the key. The sweep signs as the agent's own App through the copy of
its key in each project's KMS -- the project being swept, not any other -- and
a signature that cannot be made stops the sweep before GitHub is asked
anything.

Third, the token is narrowed at mint time -- to one repository, and to
`pull_requests: write` -- so the sweep never holds the reach the App has.

Fourth, which projects. The sweep takes only what Boskos hands out as free,
holds each for the seconds it takes, and gives every one back -- on success, on
a fault, on an unmapped name. A project a run holds is never asked for.

Fifth, a permission the organisation has withdrawn has to read as what it is.
GitHub answers that with a 403 or a 422 whose text is about tokens; the usual
cause is a change in the organisation's settings, and a reader who is told the
former goes looking in the wrong place.
"""

import base64
import http.client
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
_PROVISION = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"

_spec = importlib.util.spec_from_file_location("ci_sweep_agent_pulls", _MODULE_PATH)
sweeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweeper)

PROJECT = "kube-agents-evals-7"
REPO = "gke-agentic/kube-agents-evals-7-infra"
APP_ID = "4675512"
BOSKOS = "http://boskos.test"
OWNER = "ci-kube-agents-pull-sweep-1"
# The agent's author login. The sweep signs as the same App now, so its own
# bot is the one to match; OTHER_BOT is another App's, whose pull requests it
# must leave alone.
BOT = "kube-agents-evals-token-minter[bot]"
OTHER_BOT = "kube-agents-evals-ledger-reader[bot]"
MAPPING = {
    "kube-agents-evals-7": REPO,
    "kube-agents-evals-8": "gke-agentic/kube-agents-evals-8-infra",
    "kube-agents-evals-9": "gke-agentic/kube-agents-evals-9-infra",
}


def agent_pull(number=1, branch="platform-agent/fix-the-thing", author=BOT, head_repo=REPO):
    return {
        "number": number,
        "user": {"login": author},
        "head": {"ref": branch, "repo": {"full_name": head_repo}},
    }


def _pad(segment):
    return segment + "=" * (-len(segment) % 4)


def _http_error(code, url="https://api.github.com"):
    return urllib.error.HTTPError(url, code, "reason", {}, None)


class _GitHub:
    """A recording stand-in for api.github.com.

    Keyed on "<METHOD> <path>", so an assertion can name the call it means
    rather than an index into a list that shifts whenever a call is added.
    `pulls` is one list served for every repository, or a dict by repository.
    """

    def __init__(self, pulls=None, mint_error=None, close_errors=None):
        self.calls = []
        self.pulls = pulls if pulls is not None else []
        self.mint_error = mint_error
        # Keyed by pull-request number, so one close can fail while the rest
        # succeed.
        self.close_errors = close_errors or {}

    def _pulls_for(self, path):
        if isinstance(self.pulls, dict):
            repo = path[len("/repos/") :].split("/pulls?")[0]
            return self.pulls.get(repo, [])
        return self.pulls

    def __call__(self, request, timeout=None):
        path = request.full_url.replace(sweeper.API_ROOT, "")
        key = "%s %s" % (request.method, path)
        body = json.loads(request.data) if request.data else None
        self.calls.append((key, body))
        if key.startswith("GET /repos/") and key.endswith("/installation"):
            return io.BytesIO(json.dumps({"id": 157029058}).encode())
        if key.startswith("POST /app/installations/"):
            if self.mint_error is not None:
                raise self.mint_error
            return io.BytesIO(json.dumps({"token": "ghs_fake"}).encode())
        if key.startswith("GET /repos/") and "/pulls?" in key:
            page = int(key.rsplit("page=", 1)[1])
            return io.BytesIO(json.dumps(self._pulls_for(path) if page == 1 else []).encode())
        if key.startswith("PATCH /repos/"):
            failure = self.close_errors.get(int(key.rsplit("/", 1)[1]))
            if failure is not None:
                raise failure
            return io.BytesIO(b"{}")
        raise AssertionError("unexpected call %s" % key)

    def bodies(self, prefix):
        return [body for key, body in self.calls if key.startswith(prefix)]

    def keys(self, prefix):
        return [key for key, _ in self.calls if key.startswith(prefix)]


class _Boskos:
    """A stand-in for the Boskos server: hands out `free` in order, then 404."""

    def __init__(self, free=(), error=None, stranded=(), reset_error=None):
        self.free = list(free)
        self.error = error
        self.stranded = list(stranded)
        self.reset_error = reset_error
        self.acquired = []
        self.released = []
        self.resets = []

    def __call__(self, request, timeout=None):
        if self.error is not None:
            raise self.error
        url = request.full_url
        query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
        action = url.split("?", 1)[0].rsplit("/", 1)[1]
        if action == "acquire":
            assert query == {
                "type": sweeper.BOSKOS_RESOURCE_TYPE,
                "state": "free",
                "dest": "cleaning",
                "owner": OWNER,
            }, query
            if not self.free:
                raise _http_error(404, url)
            name = self.free.pop(0)
            self.acquired.append(name)
            return io.BytesIO(json.dumps({"name": name, "state": "cleaning"}).encode())
        if action == "release":
            assert query["dest"] == "free" and query["owner"] == OWNER, query
            self.released.append(query["name"])
            return io.BytesIO(b"")
        if action == "reset":
            self.resets.append(query)
            if self.reset_error is not None:
                raise self.reset_error
            return io.BytesIO(json.dumps({name: "an-earlier-sweep" for name in self.stranded}).encode())
        raise AssertionError("unexpected Boskos call %s" % url)


class _Cluster:
    """Routes urlopen by host: Boskos or GitHub, nothing else."""

    def __init__(self, github, boskos=None):
        self.github = github
        self.boskos = boskos or _Boskos()

    def __call__(self, request, timeout=None):
        if request.full_url.startswith(BOSKOS):
            return self.boskos(request, timeout=timeout)
        return self.github(request, timeout=timeout)


class _Gcloud:
    """A stand-in for `gcloud kms asymmetric-sign` that writes a signature."""

    def __init__(self, returncode=0, signature=b"signature"):
        self.returncode = returncode
        self.signature = signature
        self.argv = []

    def __call__(self, argv, **kwargs):
        self.argv.append(list(argv))
        if self.returncode == 0:
            target = [a for a in argv if a.startswith("--signature-file=")][0].split("=", 1)[1]
            pathlib.Path(target).write_bytes(self.signature)
        return mock.Mock(returncode=self.returncode, stdout=b"", stderr=b"permission denied")

    def flags(self, index=0):
        return {a.split("=", 1)[0]: a.split("=", 1)[1] for a in self.argv[index] if "=" in a}


def run_repo(github, gcloud=None, **kwargs):
    gcloud = gcloud or _Gcloud()
    with mock.patch.object(sweeper.urllib.request, "urlopen", _Cluster(github)):
        return sweeper.sweep_repo(PROJECT, REPO, APP_ID, runner=gcloud, **kwargs)


def run_pool(free, github=None, gcloud=None, mapping=None, **kwargs):
    boskos = _Boskos(free)
    github = github or _GitHub()
    with mock.patch.object(sweeper.urllib.request, "urlopen", _Cluster(github, boskos)):
        result = sweeper.sweep_pool(
            BOSKOS, OWNER, APP_ID, mapping or MAPPING, runner=gcloud or _Gcloud(), **kwargs
        )
    return result, boskos, github


class OwnershipTest(unittest.TestCase):
    """All three of forge.py's conditions, each one load-bearing."""

    def test_the_agents_own_pull_request_is_owned(self):
        self.assertTrue(sweeper.is_agent_pull_request(agent_pull(), REPO, BOT))

    def test_another_author_is_not(self):
        self.assertFalse(sweeper.is_agent_pull_request(agent_pull(author="some-human"), REPO, BOT))

    def test_a_branch_without_the_prefix_is_not(self):
        self.assertFalse(sweeper.is_agent_pull_request(agent_pull(branch="hotfix/urgent"), REPO, BOT))

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
        # The script closes by this prefix, and the agent chooses branch names
        # by forge.py's copy of it. Two constants that must agree, in files
        # that do not read each other.
        forge = _FORGE.read_text(encoding="utf-8")
        self.assertIn('AGENT_BRANCH_PREFIX = "%s"' % sweeper.AGENT_BRANCH_PREFIX, forge)


class AgentAuthorTest(unittest.TestCase):
    """The author looked for is the agent's bot, and only that one."""

    def test_the_agents_pull_request_is_closed(self):
        self.assertEqual(run_repo(_GitHub(pulls=[agent_pull(author=BOT)])), 1)

    def test_another_apps_pull_request_is_left(self):
        github = _GitHub(pulls=[agent_pull(author=OTHER_BOT)])
        self.assertEqual(run_repo(github), 0)
        self.assertEqual(github.keys("PATCH "), [])

    def test_the_login_and_app_id_are_the_ones_the_agent_submits_with(self):
        # Three constants in files that do not read each other: the slug the
        # deploy hands the agent, the App id the provisioning script installs,
        # and the pair written here because a sweep that cannot mint must still
        # name what it looked for.
        self.assertIn(sweeper.AGENT_APP_SLUG, _CI_DEPLOY.read_text(encoding="utf-8"))
        self.assertIn('APP_ID="%s"' % sweeper.DEFAULT_APP_ID, _PROVISION.read_text(encoding="utf-8"))
        self.assertEqual(sweeper.AGENT_BOT_LOGIN, sweeper.AGENT_APP_SLUG + "[bot]")


class SigningKeyTest(unittest.TestCase):
    """The JWT is signed by the swept project's own key, and by nothing else."""

    def test_the_signature_comes_from_the_swept_projects_key(self):
        gcloud = _Gcloud()
        run_repo(_GitHub(), gcloud=gcloud)
        flags = gcloud.flags()
        self.assertEqual(gcloud.argv[0][:3], ["gcloud", "kms", "asymmetric-sign"])
        self.assertEqual(flags["--project"], PROJECT)
        self.assertEqual(
            (flags["--location"], flags["--keyring"], flags["--key"], flags["--version"]),
            ("us-central1", "github-token-minter-keyring", "github-token-minter-key", "1"),
        )
        self.assertEqual(flags["--digest-algorithm"], "sha256")

    def test_a_key_that_will_not_sign_stops_before_github_is_asked(self):
        github = _GitHub()
        with self.assertRaises(sweeper.SweepError) as caught:
            run_repo(github, gcloud=_Gcloud(returncode=1))
        self.assertIn(PROJECT, str(caught.exception))
        self.assertEqual(github.calls, [])

    def test_a_signed_jwt_names_the_app(self):
        gcloud = _Gcloud()
        token = sweeper.app_jwt(APP_ID, PROJECT, runner=gcloud)
        header, payload, signature = token.split(".")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(_pad(header)))["alg"], "RS256")
        claims = json.loads(base64.urlsafe_b64decode(_pad(payload)))
        self.assertEqual(claims["iss"], APP_ID)
        # GitHub refuses an exp more than ten minutes out; the backdated iat is
        # what makes the whole window fit under that with room for skew.
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertLessEqual(claims["exp"] - int(__import__("time").time()), 540)
        self.assertEqual(base64.urlsafe_b64decode(_pad(signature)), b"signature")


class TokenScopeTest(unittest.TestCase):
    """The token is narrowed twice, and neither narrowing is decoration."""

    def test_the_token_is_scoped_to_one_repository(self):
        github = _GitHub()
        run_repo(github)
        self.assertEqual(github.bodies("POST /app/installations/")[0]["repositories"], ["kube-agents-evals-7-infra"])

    def test_the_token_asks_for_pull_requests_write_and_nothing_else(self):
        # The installation also holds contents and issues write, for the
        # agent. A token that inherited it whole would carry both here.
        github = _GitHub()
        run_repo(github)
        self.assertEqual(github.bodies("POST /app/installations/")[0]["permissions"], {"pull_requests": "write"})

    def test_the_installation_is_resolved_from_the_repository(self):
        github = _GitHub()
        run_repo(github)
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
        self.assertEqual(run_repo(github), 2)
        self.assertEqual(github.keys("PATCH "), ["PATCH /repos/%s/pulls/1" % REPO, "PATCH /repos/%s/pulls/5" % REPO])

    def test_closing_sets_the_state_and_nothing_else(self):
        github = _GitHub(pulls=[agent_pull()])
        run_repo(github)
        self.assertEqual(github.bodies("PATCH ")[0], {"state": "closed"})

    def test_an_empty_repository_closes_nothing(self):
        github = _GitHub(pulls=[])
        self.assertEqual(run_repo(github), 0)
        self.assertEqual(github.keys("PATCH "), [])

    def test_dry_run_reports_without_closing(self):
        github = _GitHub(pulls=[agent_pull()])
        self.assertEqual(run_repo(github, dry_run=True), 1)
        self.assertEqual(github.keys("PATCH "), [])


class CloseFailureTest(unittest.TestCase):
    """One close that fails must not abandon the ones behind it."""

    def test_the_rest_are_still_closed(self):
        github = _GitHub(pulls=[agent_pull(number=n) for n in (1, 2, 3)], close_errors={2: _http_error(409)})
        with self.assertRaises(sweeper.SweepError):
            run_repo(github)
        self.assertEqual(github.keys("PATCH "), ["PATCH /repos/%s/pulls/%d" % (REPO, n) for n in (1, 2, 3)])

    def test_the_failure_names_what_was_left_open(self):
        github = _GitHub(pulls=[agent_pull(number=7)], close_errors={7: _http_error(403)})
        with self.assertRaises(sweeper.SweepError) as caught:
            run_repo(github)
        self.assertIn("#7", str(caught.exception))

    def test_an_unreachable_github_mid_sweep_is_survived(self):
        github = _GitHub(pulls=[agent_pull(number=1), agent_pull(number=2)], close_errors={1: OSError("connection reset")})
        with self.assertRaises(sweeper.SweepError):
            run_repo(github)
        self.assertIn("PATCH /repos/%s/pulls/2" % REPO, github.keys("PATCH "))

    def test_a_response_cut_short_mid_close_is_survived(self):
        # IncompleteRead is an HTTPException, not an OSError; before this arm a
        # half-read PATCH response aborted the loop.
        github = _GitHub(
            pulls=[agent_pull(number=1), agent_pull(number=2)],
            close_errors={1: http.client.IncompleteRead(b"")},
        )
        with self.assertRaises(sweeper.SweepError):
            run_repo(github)
        self.assertIn("PATCH /repos/%s/pulls/2" % REPO, github.keys("PATCH "))


class MintFailureTest(unittest.TestCase):
    """A withdrawn permission must not read as a code fault."""

    def test_a_permission_the_installation_lacks_names_the_human_step(self):
        for code in sweeper.PERMISSION_NOT_GRANTED_CODES:
            with self.subTest(code=code):
                github = _GitHub(mint_error=_http_error(code))
                with self.assertRaises(sweeper.SweepError) as caught:
                    run_repo(github)
                self.assertIn("organisation owner", str(caught.exception))
                self.assertEqual(github.keys("PATCH "), [])

    def test_a_credential_fault_names_the_app_and_the_repository(self):
        github = _GitHub(mint_error=_http_error(401))
        with self.assertRaises(sweeper.SweepError) as caught:
            run_repo(github)
        message = str(caught.exception)
        self.assertIn(APP_ID, message)
        self.assertIn(REPO, message)
        self.assertNotIn("organisation owner", message)


class MappingTest(unittest.TestCase):
    """The project list is hack/ci-deploy.sh's, read where it lives."""

    def test_the_real_mapping_is_read_whole(self):
        mapping = sweeper.pool_repos(_CI_DEPLOY)
        self.assertGreaterEqual(len(mapping), 30)
        self.assertEqual(mapping["kube-agents-evals-7"], REPO)
        self.assertTrue(all(repo.startswith("gke-agentic/") for repo in mapping.values()), mapping)

    def test_a_script_without_the_function_is_a_fault_not_an_empty_pool(self):
        with mock.patch.object(pathlib.Path, "read_text", return_value="echo nothing\n"):
            with self.assertRaises(sweeper.SweepError):
                sweeper.pool_repos(_CI_DEPLOY)


class PoolTest(unittest.TestCase):
    """Only what Boskos hands out as free, once each, and every one given back."""

    def test_every_free_project_is_swept_once_and_released(self):
        eight = "gke-agentic/kube-agents-evals-8-infra"
        (closed, failures, unmapped), boskos, github = run_pool(
            ["kube-agents-evals-7", "kube-agents-evals-8"],
            _GitHub(pulls={REPO: [agent_pull()], eight: [agent_pull(head_repo=eight)]}),
        )
        self.assertEqual(boskos.acquired, ["kube-agents-evals-7", "kube-agents-evals-8"])
        self.assertEqual(boskos.released, boskos.acquired)
        self.assertEqual(closed, {"kube-agents-evals-7": 1, "kube-agents-evals-8": 1})
        self.assertEqual((failures, unmapped), ({}, []))
        self.assertEqual(
            sorted(github.keys("PATCH ")),
            ["PATCH /repos/gke-agentic/kube-agents-evals-7-infra/pulls/1", "PATCH /repos/gke-agentic/kube-agents-evals-8-infra/pulls/1"],
        )

    def test_a_project_boskos_keeps_is_never_touched(self):
        # evals-9 is leased by a run: Boskos never offers it, so nothing here
        # can reach it. The sweep does not list the pool or read any state.
        (closed, _, _), boskos, github = run_pool(["kube-agents-evals-7"])
        self.assertNotIn("kube-agents-evals-9", closed)
        self.assertNotIn("kube-agents-evals-9", boskos.acquired)
        self.assertFalse(any("evals-9-infra" in key for key, _ in github.calls))

    def test_each_project_is_signed_with_its_own_key(self):
        gcloud = _Gcloud()
        run_pool(["kube-agents-evals-7", "kube-agents-evals-8"], gcloud=gcloud)
        self.assertEqual([gcloud.flags(i)["--project"] for i in range(2)], ["kube-agents-evals-7", "kube-agents-evals-8"])

    def test_an_unmapped_project_is_released_and_not_swept(self):
        (closed, failures, unmapped), boskos, github = run_pool(["kube-agents-evals-99", "kube-agents-evals-7"])
        self.assertEqual(unmapped, ["kube-agents-evals-99"])
        self.assertEqual(boskos.released, ["kube-agents-evals-99", "kube-agents-evals-7"])
        self.assertEqual(list(closed), ["kube-agents-evals-7"])
        self.assertFalse(any("evals-99" in key for key, _ in github.calls))

    def test_a_project_that_fails_is_released_and_the_rest_are_swept(self):
        github = _GitHub(mint_error=_http_error(401))
        (closed, failures, _), boskos, _ = run_pool(["kube-agents-evals-7", "kube-agents-evals-8"], github)
        self.assertEqual(sorted(failures), ["kube-agents-evals-7", "kube-agents-evals-8"])
        self.assertEqual(boskos.released, ["kube-agents-evals-7", "kube-agents-evals-8"])
        self.assertEqual(closed, {})

    def test_a_project_is_released_when_github_is_unreachable(self):
        def unreachable(request, timeout=None):
            raise OSError("connection reset")

        boskos = _Boskos(["kube-agents-evals-7"])
        with mock.patch.object(sweeper.urllib.request, "urlopen", _Cluster(unreachable, boskos)):
            _, failures, _ = sweeper.sweep_pool(BOSKOS, OWNER, APP_ID, MAPPING, runner=_Gcloud())
        self.assertEqual(list(failures), ["kube-agents-evals-7"])
        self.assertEqual(boskos.released, ["kube-agents-evals-7"])

    def test_a_pool_with_nothing_free_sweeps_nothing(self):
        (closed, failures, unmapped), boskos, github = run_pool([])
        self.assertEqual((closed, failures, unmapped), ({}, {}, []))
        self.assertEqual(github.calls, [])

    def test_a_repeated_offer_ends_the_walk_after_three(self):
        # Boskos may hand a just-released project straight back. The walk
        # stops after three in a row rather than looping for the job's window,
        # and each repeat is still released.
        free = ["kube-agents-evals-7"] + ["kube-agents-evals-7"] * 5 + ["kube-agents-evals-8"]
        (closed, _, _), boskos, _ = run_pool(free)
        self.assertEqual(list(closed), ["kube-agents-evals-7"])
        self.assertEqual(len(boskos.acquired), 1 + sweeper.BOSKOS_MAX_CONSECUTIVE_REPEATS)
        self.assertEqual(boskos.released, boskos.acquired)

    def test_every_run_first_frees_what_an_earlier_sweep_left_behind(self):
        # The one thing that could take a project out of the pool: a sweep
        # killed mid-hold. Boskos's own reset returns anything older than the
        # expiry to free before this run acquires a thing.
        _, boskos, _ = run_pool(["kube-agents-evals-7"])
        self.assertEqual(
            boskos.resets,
            [{"type": sweeper.BOSKOS_RESOURCE_TYPE, "state": "cleaning", "dest": "free", "expire": sweeper.BOSKOS_STRANDED_AFTER}],
        )

    def test_a_reset_that_fails_does_not_stop_the_sweep(self):
        boskos = _Boskos(["kube-agents-evals-7"], reset_error=_http_error(500, BOSKOS))
        github = _GitHub(pulls=[agent_pull()])
        with mock.patch.object(sweeper.urllib.request, "urlopen", _Cluster(github, boskos)):
            closed, failures, _ = sweeper.sweep_pool(BOSKOS, OWNER, APP_ID, MAPPING, runner=_Gcloud())
        self.assertEqual((closed, failures), ({"kube-agents-evals-7": 1}, {}))

    def test_a_termination_mid_sweep_releases_the_held_project(self):
        # Prow's SIGTERM, delivered while a repository is being read: the
        # exception unwinds through the hold and the project goes back to free.
        def terminated(request, timeout=None):
            raise sweeper.Terminated("signal 15")

        boskos = _Boskos(["kube-agents-evals-7", "kube-agents-evals-8"])
        with mock.patch.object(sweeper.urllib.request, "urlopen", _Cluster(terminated, boskos)):
            with self.assertRaises(sweeper.Terminated):
                sweeper.sweep_pool(BOSKOS, OWNER, APP_ID, MAPPING, runner=_Gcloud())
        self.assertEqual(boskos.acquired, ["kube-agents-evals-7"])
        self.assertEqual(boskos.released, ["kube-agents-evals-7"])

    def test_dry_run_holds_and_releases_without_closing(self):
        (closed, _, _), boskos, github = run_pool(["kube-agents-evals-7"], _GitHub(pulls=[agent_pull()]), dry_run=True)
        self.assertEqual(closed, {"kube-agents-evals-7": 1})
        self.assertEqual(github.keys("PATCH "), [])
        self.assertEqual(boskos.released, ["kube-agents-evals-7"])


class ExitCodeTest(unittest.TestCase):
    """main() turns every fault into a nonzero exit and a line on stderr."""

    def _main(self, argv, github=None, boskos=None, gcloud=None):
        cluster = _Cluster(github or _GitHub(), boskos or _Boskos(["kube-agents-evals-7"]))
        with mock.patch.object(sweeper.urllib.request, "urlopen", cluster), mock.patch.object(
            sweeper.subprocess, "run", gcloud or _Gcloud()
        ):
            return sweeper.main(argv + ["--ci-deploy-script", str(_CI_DEPLOY)])

    def test_a_clean_pool_sweep_exits_zero(self):
        argv = ["--pool", "--boskos-server", BOSKOS, "--boskos-owner", OWNER]
        self.assertEqual(self._main(argv, _GitHub(pulls=[agent_pull()])), 0)

    def test_a_project_that_could_not_be_swept_exits_one(self):
        argv = ["--pool", "--boskos-server", BOSKOS, "--boskos-owner", OWNER]
        self.assertEqual(self._main(argv, _GitHub(mint_error=_http_error(401))), 1)

    def test_a_termination_exits_with_the_signal_code_after_releasing(self):
        def terminated(request, timeout=None):
            raise sweeper.Terminated("signal 15")

        boskos = _Boskos(["kube-agents-evals-7"])
        argv = ["--pool", "--boskos-server", BOSKOS, "--boskos-owner", OWNER]
        self.assertEqual(self._main(argv, terminated, boskos), sweeper.TERMINATED_EXIT_CODE)
        self.assertEqual(boskos.released, ["kube-agents-evals-7"])

    def test_sigterm_is_turned_into_the_exception(self):
        with self.assertRaises(sweeper.Terminated):
            sweeper._terminate(15, None)

    def test_an_unreachable_boskos_exits_one(self):
        argv = ["--pool", "--boskos-server", BOSKOS, "--boskos-owner", OWNER]
        self.assertEqual(self._main(argv, boskos=_Boskos(error=OSError("connection refused"))), 1)

    def test_one_project_sweeps_its_mapped_repository_without_boskos(self):
        github = _GitHub(pulls=[agent_pull()])
        boskos = _Boskos(error=AssertionError("Boskos must not be asked"))
        self.assertEqual(self._main(["--project", PROJECT], github, boskos), 0)
        self.assertIn("PATCH /repos/%s/pulls/1" % REPO, github.keys("PATCH "))

    def test_one_unmapped_project_exits_one(self):
        self.assertEqual(self._main(["--project", "kube-agents-evals-99"]), 1)


if __name__ == "__main__":
    unittest.main()
