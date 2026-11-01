#!/usr/bin/env python3
"""vcs_client.py as a library: the shape a consumer script imports, not the CLI.

The command-line suite (`skills/version-control/scripts/test_vcs.py`) drives
every operation through `vcs.py`, which is a thin front over this module; what
that suite cannot show is that the module stands on its own for a caller that
never builds an argparse namespace. These tests are that caller.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import credential_proxy_client as client  # noqa: E402
import vcs_client  # noqa: E402

REAL_GIT = shutil.which("git") or "/usr/bin/git"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"},
    )


class ForgeCallTest(unittest.TestCase):
    def test_forge_names_the_repository_and_drops_absent_fields(self):
        seen = []

        def fake_call(verb, payload):
            seen.append((verb, payload))
            return {"ok": True}

        with mock.patch.object(vcs_client, "call", fake_call):
            answer = vcs_client.forge(
                "issue-update", {"number": 7, "title": None, "labelsAdd": ["a"]}, "acme/infra"
            )
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(seen, [("issue-update", {"number": 7, "labelsAdd": ["a"], "repository": "acme/infra"})])

    def test_a_name_registered_as_github_travels_with_its_host(self):
        # Review round 4: the broker refuses a bare name whenever it is
        # configured with two forges, which the managed list cannot see (a
        # GitLab forge declared before any GitLab repository is registered).
        # A name registered as GitHub is GitHub's whatever the broker serves,
        # so it goes on the wire as the URL it was registered by; one another
        # forge's entry shares, or nothing registers, is sent as written.
        sent = []
        entries = [
            {"type": "github", "url": "https://github.com/acme/fleet"},
            {"type": "gitlab", "url": "https://gitlab.com/acme/both"},
            {"type": "github", "url": "https://github.com/acme/both"},
            # Review (#2549): an scp remote on another forge collides too.
            {"type": "gitlab", "url": "git@gitlab.com:acme/scp.git"},
            {"type": "github", "url": "https://github.com/acme/scp"},
        ]
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}), \
                mock.patch.object(vcs_client.credential_proxy_client, "vcs_call",
                                  lambda endpoint, verb, payload: sent.append(payload["repository"]) or {}), \
                mock.patch("gitops_workspace.mounted_repo_entries",
                           side_effect=lambda key: entries if key == "managed_repos" else []):
            vcs_client._registered_urls.cache_clear()
            for name in ("acme/fleet", "Acme/Fleet", "acme/both", "acme/unregistered",
                         "gitlab.com/acme/both", "https://github.com/acme/fleet", "acme/scp"):
                vcs_client.call("issue-list", {"repository": name})
        self.assertEqual(
            ["https://github.com/acme/fleet", "https://github.com/acme/fleet", "acme/both",
             "acme/unregistered", "gitlab.com/acme/both", "https://github.com/acme/fleet", "acme/scp"],
            sent,
        )

    def test_every_managed_name_reaches_a_two_forge_broker_with_a_host(self):
        # Review (#2437): `get_managed_repos` no longer qualifies every GitHub
        # name beside a second forge, so each name it hands out must leave
        # this client host-qualified, whichever of the two did the work.
        import gitops_workspace

        entries = [
            {"type": "github", "url": "https://github.com/acme/fleet"},
            {"type": "github", "url": "https://github.com/acme/both"},
            {"type": "github", "url": "acme/hand"},
            {"type": "gitlab", "url": "https://gitlab.com/acme/both"},
            {"type": "gitlab", "url": "git@gitlab.com:group/sub/proj.git"},
            # Fresh-context review (#2437): the list skips these entries, but
            # they still collide on the client. They are a host with a port
            # and an unserved type.
            {"type": "github", "url": "https://github.com/acme/ported"},
            {"type": "gitlab", "url": "https://gitlab.example.com:8443/acme/ported"},
            {"type": "github", "url": "https://github.com/acme/forgejo"},
            {"type": "forgejo", "url": "https://forgejo.example.com/acme/forgejo"},
            {"type": "github", "url": "https://github.com/acme/gitea"},
            {"type": "gitea", "url": "https://gitea.example.com/acme/gitea"},
            {"type": "github", "url": "https://github.com/acme/mirror"},
        ]
        # A context entry on another forge also collides.
        context = [{"type": "gitlab", "url": "https://gitlab.com/acme/mirror"}]
        lists = {"managed_repos": entries, "context_repos": context}
        sent = []
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}), \
                mock.patch.object(vcs_client.credential_proxy_client, "vcs_call",
                                  lambda endpoint, verb, payload: sent.append(payload["repository"]) or {}), \
                mock.patch("gitops_workspace.mounted_repo_entries", side_effect=lambda key: lists.get(key, [])), \
                mock.patch("gitops_workspace.get_managed_repo_entries", return_value=entries):
            vcs_client._registered_urls.cache_clear()
            names = gitops_workspace.get_managed_repos()
            for name in names:
                vcs_client.call("issue-list", {"repository": name})
        vcs_client._registered_urls.cache_clear()
        self.assertEqual(
            ["acme/fleet", "github.com/acme/both", "github.com/acme/hand", "github.com/acme/ported",
             "github.com/acme/forgejo", "github.com/acme/gitea", "github.com/acme/mirror",
             "gitlab.com/acme/both", "gitlab.com/group/sub/proj", "gitea.example.com/acme/gitea"],
            names,
        )
        for repository in sent:
            with self.subTest(repository=repository):
                self.assertTrue(gitops_workspace.split_host(repository.split("://")[-1])[0])

    def test_capabilities_takes_a_repository_without_a_working_copy(self):
        with mock.patch.object(vcs_client, "call", lambda verb, payload: {"verb": verb, **payload}):
            self.assertEqual(
                vcs_client.capabilities("acme/infra"),
                {"verb": "capabilities", "repository": "acme/infra"},
            )

    def base_branch_against(self, answer=None, *, raises=None, endpoint="http://127.0.0.1:1"):
        """`base_branch("acme/infra")` against a broker that answers or refuses so."""
        seen = []

        def fake_call(verb, payload):
            seen.append((verb, payload))
            if raises:
                raise raises
            return answer

        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": endpoint}), mock.patch.object(
            vcs_client, "call", fake_call
        ):
            return vcs_client.base_branch("acme/infra"), seen

    def test_base_branch_is_what_capabilities_answers(self):
        base, seen = self.base_branch_against({"forge": "github", "baseBranch": "release"})
        self.assertEqual(base, "release")
        self.assertEqual(seen, [("capabilities", {"repository": "acme/infra"})])

    def test_base_branch_is_none_when_nothing_is_pinned_or_the_broker_predates_the_field(self):
        for answer in ({"forge": "github", "baseBranch": None}, {"forge": "github"}):
            with self.subTest(answer=answer):
                self.assertIsNone(self.base_branch_against(answer)[0])

    def test_base_branch_is_none_on_a_broker_without_the_route(self):
        unsupported = vcs_client.VcsError("old", code=vcs_client.BROKER_ROUTE_UNSUPPORTED)
        self.assertIsNone(self.base_branch_against(raises=unsupported)[0])

    def test_base_branch_is_none_with_no_broker_in_this_environment(self):
        base, seen = self.base_branch_against({"baseBranch": "release"}, endpoint="")
        self.assertIsNone(base)
        self.assertEqual(seen, [])

    def test_base_branch_raises_a_broker_that_is_there_and_refusing(self):
        # Not read as "pins nothing": a caller that fell back to the remote's
        # default here would cut a proposal the broker then refuses.
        refused = vcs_client.VcsError("down", code="FORGE_UNAVAILABLE")
        with self.assertRaises(vcs_client.VcsError):
            self.base_branch_against(raises=refused)

    def test_a_broker_refusal_keeps_its_code(self):
        error = vcs_client.credential_proxy_client.WorkspaceRequestError(
            "refused", payload={"error": "no", "code": "PROTECTED_BRANCH", "detail": "d"}
        )
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}), mock.patch.object(
            vcs_client.credential_proxy_client, "vcs_call", side_effect=error
        ):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.call("publish", {})
        self.assertEqual(caught.exception.code, "PROTECTED_BRANCH")
        self.assertEqual(caught.exception.as_json(), {"error": "no", "code": "PROTECTED_BRANCH", "detail": "d"})

    def test_a_path_refusal_keeps_the_revision_the_broker_read(self):
        error = vcs_client.credential_proxy_client.WorkspaceRequestError(
            "refused", payload={"error": "no", "code": "PATH_NOT_FOUND", "revision": "a" * 40}
        )
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}), mock.patch.object(
            vcs_client.credential_proxy_client, "vcs_call", side_effect=error
        ):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.call("snapshot", {})
        self.assertEqual(caught.exception.revision, "a" * 40)
        self.assertEqual(caught.exception.as_json()["revision"], "a" * 40)


    def test_a_broker_too_old_for_a_verb_is_not_a_broker_that_is_down(self):
        """The shape an old credential-proxy actually answers with.

        `BROKER_ROUTE_UNSUPPORTED` used to be raised only for the broker's
        `VCS_UNAVAILABLE` body, which `build_vcs_broker` says a running broker
        never sends. A broker whose image predates one of these verbs answers a
        codeless 404 instead -- `{"status": "not_found"}` from the route lookup,
        or the generic handler's on an image older than the namespace -- and
        that fell to the plain arm and was reported as `BROKER_UNREACHABLE`,
        sending an operator after a broker that is up.
        """
        for payload in ({"status": "not_found"}, {"error": "HTTP 404"}):
            with self.subTest(payload=payload):
                error = vcs_client.credential_proxy_client.WorkspaceRequestError(
                    404, payload
                )
                with mock.patch.dict(
                    os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}
                ), mock.patch.object(
                    vcs_client.credential_proxy_client, "vcs_call", side_effect=error
                ):
                    with self.assertRaises(vcs_client.VcsError) as caught:
                        vcs_client.call("issue-update", {})
                self.assertEqual(
                    caught.exception.code, vcs_client.BROKER_ROUTE_UNSUPPORTED
                )
                self.assertIn("issue-update", str(caught.exception))

    def test_a_coded_forge_404_is_still_the_forge_s_answer(self):
        """The other half: a bare 404 is the only one read as a missing route.

        `providers.errors` codes a forge 404 `FORGE_NOT_FOUND`, and reading
        that as a broker too old would send an operator to roll an image over a
        repository name that is misspelt.
        """
        error = vcs_client.credential_proxy_client.WorkspaceRequestError(
            404, {"error": "no such repository", "code": "FORGE_NOT_FOUND"}
        )
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}
        ), mock.patch.object(
            vcs_client.credential_proxy_client, "vcs_call", side_effect=error
        ):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.call("issue-update", {})
        self.assertEqual(caught.exception.code, "FORGE_NOT_FOUND")

    def test_version_control_unbuilt_keeps_its_own_arm(self):
        """`VCS_UNAVAILABLE` still answers `BROKER_ROUTE_UNSUPPORTED`.

        Unreachable on a served broker, which is why the message says to report
        it rather than to roll an image -- but it is still not a broker that is
        down, so it keeps the code rather than the codeless fallback.
        """
        error = vcs_client.credential_proxy_client.WorkspaceUnavailable(
            "version control is not available on this broker"
        )
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}
        ), mock.patch.object(
            vcs_client.credential_proxy_client, "vcs_call", side_effect=error
        ):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.call("publish", {})
        self.assertEqual(caught.exception.code, vcs_client.BROKER_ROUTE_UNSUPPORTED)

    def test_a_socket_that_drops_mid_answer_becomes_a_broker_disconnect(self):
        """The transport's half: what `urllib` leaves unwrapped, named here.

        `urllib` wraps only `h.request(...)` in `URLError`; `getresponse()` and
        the body read after it are not wrapped, and the broker opener clears
        the socket timeout once connected. So a pod evicted or rolled while an
        answer was being read raised `http.client` and `socket` types straight
        out of `vcs_call`. `IncompleteRead` is in the list because it is the
        one that carries a partial body: without an arm of its own it is a
        bare `HTTPException` reaching a caller whose whole contract is that
        this function raises one type.
        """
        for error in (
            http.client.RemoteDisconnected("Remote end closed connection"),
            http.client.IncompleteRead(b'{"ok": tr', 12),
            ConnectionResetError(104, "Connection reset by peer"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    client, "open_broker_request", side_effect=error
                ), mock.patch.object(client, "authorization_headers", return_value={}):
                    with self.assertRaises(client.BrokerDisconnected) as caught:
                        client.vcs_call("http://127.0.0.1:1", "forge", {})
                self.assertIn(type(error).__name__, str(caught.exception))

    def test_a_body_that_runs_out_while_an_error_is_read_is_still_a_disconnect(self):
        """The same drop, one layer in: the status landed, the body did not.

        A broker rolled between its response line and its body gives a real
        `HTTPError` whose payload read then tears. That read happens inside the
        `except HTTPError` clause, and Python does not offer an exception
        raised there to that clause's siblings -- so the disconnect arm at the
        foot of the same `try` cannot catch it, however it is ordered. Left
        alone it leaves `vcs_call` as a raw `http.client` type, and
        `vcs_client.call` has no arm for one.
        """

        class TornBody(io.BytesIO):
            def __init__(self, error):
                super().__init__(b"")
                self.error = error

            def read(self, *args, **kwargs):
                raise self.error

        for error in (
            http.client.IncompleteRead(b'{"error": "no', 40),
            http.client.RemoteDisconnected("Remote end closed connection"),
            ConnectionResetError(104, "Connection reset by peer"),
        ):
            with self.subTest(error=type(error).__name__):
                torn = urllib.error.HTTPError(
                    "http://127.0.0.1:1/v1/vcs/forge",
                    503,
                    "Service Unavailable",
                    {},
                    TornBody(error),
                )
                with mock.patch.object(
                    client, "open_broker_request", side_effect=torn
                ), mock.patch.object(client, "authorization_headers", return_value={}):
                    with self.assertRaises(client.BrokerDisconnected) as caught:
                        client.vcs_call("http://127.0.0.1:1", "forge", {})
                self.assertIn(type(error).__name__, str(caught.exception))

    def test_a_send_that_never_landed_stays_a_URLError(self):
        """The distinction the new type exists to keep.

        A refused connection changed nothing at the far end; one that broke
        mid-answer may have been acted on. `URLError` is itself an `OSError`,
        so without its own arm ahead of the catch it would be reported as the
        second when it is the first.
        """
        with mock.patch.object(
            client, "open_broker_request",
            side_effect=urllib.error.URLError("Connection refused"),
        ), mock.patch.object(client, "authorization_headers", return_value={}):
            with self.assertRaises(urllib.error.URLError):
                client.vcs_call("http://127.0.0.1:1", "forge", {})

    def test_a_broker_that_drops_mid_answer_is_still_a_VcsError(self):
        """The caller's half: every consumer is built on "one exception type".

        `resolver.sweep_stale_issues` says "nothing here raises" and would have
        raised, taking the poll with it; `_fetch_comments` says it returns `[]`
        rather than raising; and `github_scan_gate.run_resolver_poll` would
        turn the traceback into a `RuntimeError` where the SKILL promises a
        reason code, losing every managed repository's poll to one broken read.
        """
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}
        ), mock.patch.object(
            vcs_client.credential_proxy_client, "vcs_call",
            side_effect=client.BrokerDisconnected("RemoteDisconnected: closed"),
        ):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.call("forge", {})
        said = str(caught.exception)
        self.assertIn("broke before the answer was complete", said)
        self.assertIn("RemoteDisconnected", said)


class ProposalOpenedRecordTest(unittest.TestCase):
    """`call()` writes one stderr record per proposal the broker opened, and nothing else."""

    URL = "https://gitlab.com/acme/infra/-/merge_requests/4"
    OPENED = {"proposal": {"number": 4, "title": "Raise replicas", "url": URL, "state": "open", "body": "long"}}

    def records(self, verb, answer):
        stderr = io.StringIO()
        with mock.patch.object(vcs_client, "_call", lambda v, p: answer), mock.patch("sys.stderr", stderr):
            self.assertEqual(vcs_client.call(verb, {"repository": "acme/infra"}), answer)
        return [json.loads(line) for line in stderr.getvalue().splitlines()]

    def test_a_created_proposal_is_one_record_with_only_its_url_number_and_title(self):
        expected = [{"kube-agents.event": "proposal-opened",
                     "proposal": {"url": self.URL, "number": 4, "title": "Raise replicas"}}]
        self.assertEqual(self.records("proposal-create", self.OPENED), expected)
        self.assertEqual(self.records("proposal_create", self.OPENED), expected)

    def test_another_verb_or_an_answer_with_no_url_records_nothing(self):
        self.assertEqual(self.records("proposal-update", self.OPENED), [])
        self.assertEqual(self.records("proposal-view", self.OPENED), [])
        self.assertEqual(self.records("proposal-create", {"proposal": {"number": 4, "url": ""}}), [])
        self.assertEqual(self.records("proposal-create", {"forge": "github"}), [])

    def test_a_refused_create_records_nothing(self):
        stderr = io.StringIO()
        with mock.patch.object(vcs_client, "_call", side_effect=vcs_client.VcsError("refused")), \
                mock.patch("sys.stderr", stderr):
            with self.assertRaises(vcs_client.VcsError):
                vcs_client.call("proposal-create", {})
        self.assertEqual(stderr.getvalue(), "")

    def test_with_no_stderr_nothing_reaches_stdout(self):
        stdout = io.StringIO()
        with mock.patch.object(vcs_client, "_call", lambda v, p: self.OPENED), \
                mock.patch("sys.stderr", None), mock.patch("sys.stdout", stdout):
            self.assertEqual(vcs_client.call("proposal-create", {}), self.OPENED)
        self.assertEqual(stdout.getvalue(), "")

    def test_a_stderr_that_cannot_be_written_still_returns_the_answer(self):
        closed = io.StringIO()
        closed.close()
        with mock.patch.object(vcs_client, "_call", lambda v, p: self.OPENED), mock.patch("sys.stderr", closed):
            self.assertEqual(vcs_client.call("proposal-create", {}), self.OPENED)


class WorkingCopyTest(unittest.TestCase):
    """clone -> branch -> commit -> publish, driven as a library against a local repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = base / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet", "--initial-branch=main")
        (self.origin / "a.txt").write_text("a\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "--quiet", "-m", "seed")
        self.head = git(self.origin, "rev-parse", "HEAD").stdout.strip()
        self.published: list[dict] = []
        # The `baseBranch` the fake's `clone` answers with. Absent is a broker
        # older than the field.
        self.clone_extra: dict = {}
        root = base / "root"
        for attribute, value in (("ROOT", root), ("SESSIONS", root / ".sessions"), ("LOCAL_GIT", REAL_GIT), ("call", self.call)):
            patch = mock.patch.object(vcs_client, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, verb, payload):
        if verb == "clone":
            import base64
            bundle = Path(self.tmp.name) / "served.bundle"
            git(self.origin, "bundle", "create", str(bundle), "HEAD", "main")
            blob = bundle.read_bytes()
            return {"forge": "local", "repo": "acme/infra", "branch": "main", "revision": self.head,
                    "size": len(blob), "bundleBase64": base64.b64encode(blob).decode("ascii"),
                    **self.clone_extra}
        if verb == "publish":
            self.published.append(payload)
            return {"forge": "local", "repo": "acme/infra", "branch": payload["branch"], "revision": self._tip_of(payload)}
        raise AssertionError(verb)

    def _tip_of(self, payload) -> str:
        """The revision the broker would report: the tip of the branch in the bundle.

        A real sha rather than a placeholder, because `publish` records it as the
        branch's base and the guards that read it back run `rev-list` against it.
        """
        import base64
        scratch = Path(self.tmp.name) / "received.bundle"
        scratch.write_bytes(base64.b64decode(payload["bundleBase64"]))
        listed = git(self.origin, "bundle", "list-heads", str(scratch)).stdout
        return listed.split()[0]

    def test_the_library_round_trip(self):
        cloned = vcs_client.clone("acme/infra")
        self.assertEqual(cloned["revision"], self.head)
        self.assertEqual(cloned["remotes"], [])
        made = vcs_client.branch("acme/infra", "fix/one")
        self.assertTrue(made["created"])
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        committed = vcs_client.commit("change a", spec="acme/infra")
        self.assertEqual(committed["files"], ["a.txt"])
        answer = vcs_client.publish("acme/infra")
        self.assertEqual(answer["revisions"], 1)
        self.assertEqual(self.published[0]["branch"], "fix/one")
        self.assertEqual(self.published[0]["target"], "main")
        self.assertEqual(self.published[0]["clonedFrom"], "main")
        self.assertEqual(self.published[0]["baseRevision"], self.head)
        removed = vcs_client.discard("acme/infra")
        self.assertFalse(Path(removed["removed"]).exists())

    def _record(self) -> dict:
        return json.loads((vcs_client.SESSIONS / "local__acme__infra__main.json").read_text())

    def test_clone_carries_the_pinned_base_into_the_record_and_the_answer(self):
        self.clone_extra = {"baseBranch": "release"}
        cloned = vcs_client.clone("acme/infra")
        self.assertEqual(cloned["baseBranch"], "release")
        self.assertEqual(self._record()["baseBranch"], "release")

    def test_a_clone_from_a_broker_older_than_the_field_has_no_base(self):
        cloned = vcs_client.clone("acme/infra")
        self.assertIsNone(cloned["baseBranch"])
        self.assertIsNone(self._record()["baseBranch"])
        # And everything defaults as it did: the target is the cloned branch.
        vcs_client.branch("acme/infra", "fix/one")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("change a", spec="acme/infra")
        vcs_client.publish("acme/infra")
        self.assertEqual(self.published[0]["target"], "main")

    def test_a_copy_of_another_branch_has_no_default_target_under_a_pin(self):
        # The copy came down on main while proposals go onto release. Defaulted
        # onto release, the broker would compare main's tip with release and
        # answer BASE_MOVED, which blames a rewrite that never happened, so
        # the copy refuses before it bundles anything and names both branches.
        self.clone_extra = {"baseBranch": "release"}
        cloned = vcs_client.clone("acme/infra")
        vcs_client.branch("acme/infra", "fix/one")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("change a", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra")
        message = str(caught.exception)
        self.assertIn("cloned from main", message)
        self.assertIn("go onto release", message)
        self.assertIn("Clone again with no branch to start from release", message)
        self.assertEqual(self.published, [])
        self.assertEqual(list(vcs_client.ROOT.glob("*.bundle")), [])

        # A target the caller names is the caller's choice, and the broker
        # judges it.
        vcs_client.publish("acme/infra", "release")
        self.assertEqual(self.published[0]["target"], "release")
        self.assertEqual(self.published[0]["clonedFrom"], "main")

    def test_the_off_base_refusal_still_points_a_later_round_at_advance(self):
        # A copy of an open proposal's branch, committed on that branch: the
        # refusal comes before the one that names `--advance`, so it has to
        # name it too, or the only way on it offers is a second proposal.
        self.clone_extra = {"baseBranch": "release"}
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("another round on the proposal", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra")
        message = str(caught.exception)
        self.assertIn("to add to the proposal already open on it", message)
        self.assertIn("`--advance`", message)
        self.assertIn("`--target`", message)
        self.assertEqual(self.published, [])

        # Followed, the hint publishes the round.
        vcs_client.publish("acme/infra", target="release", advance=True)
        self.assertTrue(self.published[0]["advance"])

    def test_the_off_base_refusal_offers_no_advance_from_a_branch_of_your_own(self):
        # Moved off the cloned branch, there is no open proposal to add to, so
        # cloning again is the only way on the refusal names.
        self.clone_extra = {"baseBranch": "release"}
        cloned = vcs_client.clone("acme/infra")
        vcs_client.branch("acme/infra", "fix/one")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("a change on a branch of its own", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra")
        message = str(caught.exception)
        self.assertIn("Clone again with no branch", message)
        self.assertNotIn("`--advance`", message)
        self.assertEqual(self.published, [])

    def test_a_branch_that_was_not_switched_to_is_not_reported_as_created(self):
        """`created` says what happened, not what the argv asked for.

        `switch --create` on a name git will not take creates nothing and exits
        non-zero, and the answer already carries that exit code -- but a caller
        reading the flag rather than the code was told it had a branch of its
        own to publish while standing on the one it started from. The name here
        is one `check_ref_format` refuses for its arrangement rather than its
        characters, so it clears every validator in front of this and is
        refused by git itself.
        """
        vcs_client.clone("acme/infra")
        before = vcs_client.branch("acme/infra", "fix/one")["branch"]
        answer = vcs_client.branch("acme/infra", "fix/one/")
        self.assertNotEqual(answer["exitCode"], 0)
        self.assertFalse(answer["created"])
        # And it is still standing where it was.
        self.assertEqual(answer["branch"], before)

    def test_a_record_written_before_the_branch_was_in_its_name_can_be_discarded(self):
        """The copies an install already had when this landed are not zombies.

        Their file is `{forge}__{repo}.json`; the name derived from their
        contents is `{forge}__{repo}__{branch}.json`. Removing the derived name
        deleted the working copy and left the record, which then had to be
        disambiguated against forever and could not be cleared by any verb.
        """
        legacy_tree = vcs_client.ROOT / "local__acme__infra"
        legacy_tree.mkdir(parents=True)
        (legacy_tree / "a.txt").write_text("a\n")
        vcs_client.SESSIONS.mkdir(parents=True, exist_ok=True)
        legacy_record = vcs_client.SESSIONS / "local__acme__infra.json"
        legacy_record.write_text(
            json.dumps(
                {
                    "forge": "local",
                    "repo": "acme/infra",
                    "spec": "acme/infra",
                    "branch": "main",
                    "baseRevision": self.head,
                    "path": str(legacy_tree),
                }
            )
        )

        removed = vcs_client.discard("acme/infra", key="main")

        self.assertEqual(removed["removed"], str(legacy_tree))
        self.assertFalse(legacy_tree.exists())
        self.assertFalse(legacy_record.exists())
        self.assertEqual(vcs_client.all_sessions(), [])

    def test_publishing_from_a_pre_rollout_record_does_not_leave_two_of_them(self):
        """`discard` was taught the record's own name. `save_session` was not.

        `publish` is the one caller that saves a record it did not create, so
        the first publish out of a copy an install had open at rollout wrote a
        second file under the derived name beside the original. One working
        copy, two records: every later `resolve_session` is ambiguous and the
        reader sorts the older name first, so the publish after that reads the
        half with no `published` map and re-bundles from the clone point.
        """
        cloned = vcs_client.clone("acme/infra")
        vcs_client.branch("acme/infra", "fix/one")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("change a", spec="acme/infra")

        # Age the record by hand into the shape this rolls out onto: the file
        # name without the branch, and no `key` to derive it back from.
        current = vcs_client.SESSIONS / "local__acme__infra__main.json"
        record = json.loads(current.read_text())
        record.pop("key", None)
        legacy = vcs_client.SESSIONS / "local__acme__infra.json"
        legacy.write_text(json.dumps(record))
        current.unlink()

        vcs_client.publish("acme/infra")

        self.assertEqual(
            sorted(path.name for path in vcs_client.SESSIONS.glob("*.json")),
            ["local__acme__infra.json"],
        )
        self.assertIn("fix/one", json.loads(legacy.read_text()).get("published", {}))
        self.assertEqual(len(vcs_client.all_sessions()), 1)

    def test_a_record_derived_from_another_does_not_write_over_it(self):
        """The stamp says where *that* record came from, not where this one goes.

        A caller that reads a record and saves a changed copy of it -- a second
        working copy, another repository -- would overwrite the one it meant to
        sit beside if the stamp were honoured blindly, which is a worse failure
        than the duplicate record it is there to prevent. So it is honoured only
        while the record still names the same repository on the same forge.
        """
        vcs_client.clone("acme/infra")
        derived = dict(vcs_client.all_sessions()[0])
        derived.update({"repo": "acme/other", "spec": "acme/other"})
        vcs_client.save_session(derived)

        self.assertEqual(
            sorted(path.name for path in vcs_client.SESSIONS.glob("*.json")),
            ["local__acme__infra__main.json", "local__acme__other__main.json"],
        )

    def test_where_a_record_was_read_from_is_not_written_back_into_it(self):
        vcs_client.clone("acme/infra")
        vcs_client.branch("acme/infra", "fix/one")
        written = json.loads(
            (vcs_client.SESSIONS / "local__acme__infra__main.json").read_text()
        )
        self.assertNotIn("_file", written)

    def test_publishing_the_cloned_branch_is_refused_before_any_call(self):
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("straight onto main", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra", target="release")
        self.assertIn("cloned from", str(caught.exception))
        self.assertEqual(self.published, [])

    def test_advance_publishes_the_cloned_branch_and_says_so_to_the_broker(self):
        # The copy was taken of a proposal branch in order to add to it, which
        # is the one reason to write to the branch it came down on. The flag
        # travels: the broker refuses the same thing on its own, so a client
        # that waived the check quietly would be refused there instead.
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("another round on the proposal", spec="acme/infra")
        answer = vcs_client.publish("acme/infra", target="release", advance=True)
        self.assertEqual(answer["branch"], "main")
        self.assertTrue(self.published[0]["advance"])
        self.assertEqual(self.published[0]["clonedFrom"], "main")

    def test_one_repository_cloned_twice_gets_one_copy_per_branch(self):
        # The scratch root is shared by every card in the container, so the
        # copy is keyed on the branch as well as the repository. Keyed on the
        # repository alone, the second clone here landed on the first one.
        first = vcs_client.clone("acme/infra", key="fix/one")
        second = vcs_client.clone("acme/infra", key="fix/two")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(first["path"].endswith("local__acme__infra__fix__one"))
        self.assertEqual(
            vcs_client.resolve_session("acme/infra", key="fix/two")["path"],
            second["path"],
        )
        # And discarding one leaves the other, session file included.
        vcs_client.discard("acme/infra", key="fix/two")
        self.assertFalse(Path(second["path"]).exists())
        self.assertEqual(
            vcs_client.resolve_session("acme/infra")["path"], first["path"]
        )

    def test_two_branches_with_the_same_directory_name_do_not_share_a_copy(self):
        """`_slug` is not injective, so the collision is refused rather than taken.

        `BRANCH_RE` admits `_` and `/` is written as `__`, so `fix/one` and
        `fix__one` derive the same directory. Before this the second clone took
        it: `--force` deleted the first card's tree and `save_session` wrote
        over its record, and the first card was then told there was no local
        copy of the branch it had prepared.
        """
        first = vcs_client.clone("acme/infra", key="fix/one")
        tree = Path(first["path"])
        self.assertTrue(first["path"].endswith("local__acme__infra__fix__one"))
        (tree / "a.txt").write_text("a2\n")
        vcs_client.commit("work that never left", spec="acme/infra", key="fix/one")

        for force in (False, True):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.clone("acme/infra", key="fix__one", force=force)
            self.assertIn("fix/one", str(caught.exception))
            self.assertIn("different branch", str(caught.exception))
        # Neither the tree nor the record moved, so the first card still resolves.
        self.assertEqual(
            vcs_client.resolve_session("acme/infra", key="fix/one")["path"],
            first["path"],
        )
        self.assertEqual(vcs_client.unpublished_revisions(
            vcs_client.resolve_session("acme/infra", key="fix/one")), 1)
        # And discarding the occupant is what frees the name.
        vcs_client.discard("acme/infra", key="fix/one")
        taken = vcs_client.clone("acme/infra", key="fix__one")
        self.assertEqual(taken["path"], first["path"])

    def test_a_second_clone_refuses_to_discard_work_on_a_branch_it_is_not_standing_on(self):
        """Every branch the copy holds, not the one that is checked out.

        Branch `A` published, branch `B` committed and never published, and the
        copy switched back to `A`: a clean status and nothing past `A`'s
        published tip, so a guard that read HEAD alone waved the re-clone
        through and `B` went with the tree, with no message.
        """
        cloned = vcs_client.clone("acme/infra", key="work")
        tree = Path(cloned["path"])
        vcs_client.branch("acme/infra", "fix/a", key="work")
        (tree / "a.txt").write_text("a2\n")
        vcs_client.commit("published work", spec="acme/infra", key="work")
        vcs_client.publish("acme/infra", key="work")
        vcs_client.branch("acme/infra", "fix/b", key="work")
        (tree / "b.txt").write_text("b\n")
        vcs_client.commit("work that never left", ["b.txt"], spec="acme/infra", key="work")
        git(tree, "checkout", "--quiet", "fix/a")
        self.assertEqual(git(tree, "status", "--porcelain").stdout, "")

        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.clone("acme/infra", key="work")
        self.assertIn("unpublished revision", str(caught.exception))
        self.assertIn("fix/b", str(caught.exception))
        self.assertTrue((tree / "b.txt").exists() or git(tree, "rev-parse", "fix/b").stdout)
        # `--force` is still the way past it.
        vcs_client.clone("acme/infra", key="work", force=True)

    def test_a_second_clone_of_a_copy_with_everything_published_is_not_refused(self):
        cloned = vcs_client.clone("acme/infra", key="work")
        tree = Path(cloned["path"])
        vcs_client.branch("acme/infra", "fix/a", key="work")
        (tree / "a.txt").write_text("a2\n")
        vcs_client.commit("published work", spec="acme/infra", key="work")
        vcs_client.publish("acme/infra", key="work")
        again = vcs_client.clone("acme/infra", key="work")
        self.assertEqual(again["path"], cloned["path"])

    def _age(self, session: dict, hours: float) -> None:
        """Backdate every mark `_last_touched` reads, as if nobody had touched the copy for `hours`."""
        then = time.time() - hours * 3600
        tree = Path(session["path"])
        for path in (Path(session["_file"]), tree / ".git" / "index", tree / ".git" / "logs" / "HEAD", tree):
            if path.exists():
                os.utime(path, (then, then))

    def _session(self, key: str) -> dict:
        return next(s for s in vcs_client.all_sessions() if vcs_client.key_of(s) == key)

    def test_a_clone_reaps_copies_nobody_has_touched_inside_the_ttl(self):
        """The lease's reaper went with the lease; this is what bounds the scratch root now.

        A landed `submit` does not discard its copy, so every branch ever
        prepared would otherwise stay on the sandbox's claim for good.
        """
        vcs_client.clone("acme/infra", key="fix/old")
        vcs_client.clone("acme/infra", key="fix/recent")
        old, recent = self._session("fix/old"), self._session("fix/recent")
        self._age(old, 25)
        self._age(recent, 23)
        vcs_client.clone("acme/infra", key="fix/new")
        self.assertFalse(Path(old["path"]).exists())
        self.assertFalse(Path(old["_file"]).exists())
        self.assertTrue(Path(recent["path"]).exists())
        self.assertEqual(sorted(vcs_client.key_of(s) for s in vcs_client.all_sessions()), ["fix/new", "fix/recent"])

    def test_a_clone_does_not_reap_its_own_destination(self):
        vcs_client.clone("acme/infra", key="fix/one")
        self._age(self._session("fix/one"), 48)
        cloned = vcs_client.clone("acme/infra", key="fix/one")
        self.assertTrue(Path(cloned["path"]).exists())

    def test_a_record_naming_a_path_outside_the_root_is_never_reaped(self):
        """The record is a file the sandbox user can write; its path must not become a delete primitive."""
        vcs_client.clone("acme/infra", key="fix/one")
        session = self._session("fix/one")
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        record = Path(session["_file"])
        written = json.loads(record.read_text())
        written["path"] = str(outside)
        record.write_text(json.dumps(written))
        session["path"] = str(outside)
        self._age(session, 48)
        self.assertEqual(vcs_client.reap_stale_copies(), [])
        self.assertTrue(outside.exists())

    def test_a_ttl_of_zero_turns_the_reaper_off(self):
        vcs_client.clone("acme/infra", key="fix/one")
        self._age(self._session("fix/one"), 48)
        self.assertEqual(vcs_client.reap_stale_copies(ttl_hours=0), [])

    def test_two_copies_are_ambiguous_until_one_is_named_or_stood_in(self):
        first = vcs_client.clone("acme/infra", key="fix/one")
        vcs_client.clone("acme/infra", key="fix/two")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.resolve_session("acme/infra")
        # The refusal hands over paths rather than a choice: the caller is
        # meant to be standing in the copy, and `cd` takes a path.
        self.assertIn(first["path"], str(caught.exception))
        self.assertIn("fix/two", str(caught.exception))

        here = Path.cwd()
        self.addCleanup(os.chdir, here)
        os.chdir(first["path"])
        self.assertEqual(
            vcs_client.resolve_session("acme/infra")["path"], first["path"]
        )

    def test_advance_still_refuses_a_target_that_is_the_branch_itself(self):
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("no target of its own", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra", advance=True)
        self.assertIn("branch and target are both", str(caught.exception))
        self.assertEqual(self.published, [])

    def test_advance_does_not_default_its_target_to_the_pinned_base(self):
        # The open proposal may target another branch; only the caller knows.
        self.clone_extra = {"baseBranch": "release"}
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("no target of its own", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra", advance=True)
        self.assertIn("branch and target are both", str(caught.exception))
        self.assertEqual(self.published, [])


def build_tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    """A tar holding `members`, each a header and, for a regular file, its bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for info, data in members:
            if data is not None:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def regular(name: str, data: bytes = b"x\n") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), data


def special(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


class SnapshotTest(unittest.TestCase):
    """`snapshot`: one broker call, and a tar that is checked whole before it is written."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.into = self.tmp / "repos" / "acme__infra"
        self.sent: list[tuple[str, dict]] = []

    def answer(self, tar: bytes) -> dict:
        return {
            "forge": "github",
            "repo": "acme/infra",
            "role": "unregistered",
            "ref": "main",
            "revision": "0" * 40,
            "size": len(tar),
            "archiveBase64": base64.b64encode(tar).decode("ascii"),
            "history": "none",
        }

    def snapshot(self, tar: bytes, **kwargs):
        def fake_call(verb, payload):
            self.sent.append((verb, payload))
            return self.answer(tar)

        kwargs.setdefault("into", self.into)
        with mock.patch.object(vcs_client, "call", fake_call):
            return vcs_client.snapshot("acme/infra", **kwargs)

    def refused(self, tar: bytes, code: str = vcs_client.SNAPSHOT_MEMBER_REFUSED, **kwargs):
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.snapshot(tar, **kwargs)
        self.assertEqual(code, caught.exception.code, str(caught.exception))
        # Nothing was written: no destination, and no staging directory left.
        self.assertFalse(self.into.exists())
        self.assertEqual([], list(self.into.parent.iterdir()) if self.into.parent.exists() else [])
        return caught.exception

    def test_a_tree_is_unpacked_and_described(self):
        tar = build_tar(
            special("manifests", tarfile.DIRTYPE),
            regular("manifests/app.yaml", b"kind: app\n"),
            regular("README.md", b"hello\n"),
            special("link", tarfile.SYMTYPE, "manifests/app.yaml"),
        )
        answer = self.snapshot(tar, ref="main", paths=["manifests", "README.md", "link"])
        self.assertEqual(
            [("snapshot", {"repository": "acme/infra", "ref": "main", "paths": ["manifests", "README.md", "link"]})],
            self.sent,
        )
        self.assertEqual("kind: app\n", (self.into / "manifests/app.yaml").read_text())
        self.assertEqual("kind: app\n", (self.into / "link").read_text())
        self.assertEqual(
            {"forge": "github", "repo": "acme/infra", "role": "unregistered", "ref": "main",
             "revision": "0" * 40, "path": str(self.into), "files": 2, "bytes": 16,
             "skipped": [], "archiveBytes": len(tar), "history": "none"},
            answer,
        )
        self.assertFalse((self.into / ".git").exists())
        self.assertEqual(["acme__infra"], [p.name for p in self.into.parent.iterdir()])

    def test_an_empty_directory_may_be_the_destination(self):
        self.into.mkdir(parents=True)
        self.snapshot(build_tar(regular("a.txt")))
        self.assertTrue((self.into / "a.txt").is_file())

    def test_no_ref_and_no_paths_send_neither(self):
        self.snapshot(build_tar(regular("a.txt")))
        self.assertEqual([("snapshot", {"repository": "acme/infra"})], self.sent)

    def test_a_destination_that_is_not_empty_is_refused_before_the_call(self):
        self.into.mkdir(parents=True)
        (self.into / "keep.txt").write_text("mine\n")
        target = self.tmp / "elsewhere"
        target.mkdir()
        link = self.tmp / "link"
        link.symlink_to(target)
        afile = self.tmp / "afile"
        afile.write_text("x")
        for into in (self.into, link, afile):
            with self.subTest(into=into), self.assertRaises(vcs_client.VcsError) as caught:
                self.snapshot(build_tar(regular("a.txt")), into=into)
            self.assertEqual(vcs_client.DESTINATION_NOT_EMPTY, caught.exception.code)
        self.assertEqual([], self.sent)
        self.assertEqual("mine\n", (self.into / "keep.txt").read_text())

    def test_a_git_component_is_refused_in_any_case_and_at_any_depth(self):
        for name in (".git/config", ".GIT/config", "sub/.Git/HEAD", ".git", "a/b/.git/hooks/post-checkout", ".git./config", ".git /x"):
            with self.subTest(name=name):
                self.refused(build_tar(regular("ok.txt"), regular(name)))

    def test_a_tar_git_archive_wrote_from_a_crafted_tree_is_refused(self):
        # The tree a hostile repository could carry, written by real git:
        # `git archive` itself refuses a `.git` path, so the tar is assembled
        # from the files it would have held. The check must not depend on
        # which tool wrote the tar.
        repo = self.tmp / "crafted"
        repo.mkdir()
        git(repo, "init", "--quiet")
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=repo, input="[core]\n\thooksPath = /tmp\n",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        sub = subprocess.run(
            ["git", "mktree"], cwd=repo, input=f"100644 blob {blob}\tconfig\n",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "mktree"], cwd=repo, input=f"040000 tree {sub}\t.GIT\n100644 blob {blob}\tok.txt\n",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        listing = git(repo, "ls-tree", "-r", "--name-only", tree).stdout.split()
        self.assertIn(".GIT/config", listing)
        members = [regular(name, git(repo, "cat-file", "blob", f"{tree}:{name}").stdout.encode()) for name in listing]
        self.refused(build_tar(*members))

    def test_absolute_paths_and_parent_components_are_refused(self):
        for name in ("/etc/passwd", "../escape.txt", "a/../../escape.txt", "a/.."):
            with self.subTest(name=name):
                self.refused(build_tar(regular(name)))

    def test_symbolic_links_that_leave_the_tree_are_skipped_and_named(self):
        # Real repositories hold such links (`-> /dev/null`); one of them must
        # not cost the caller every other file, and none of them is written.
        for member, reason in (
            (special("abs", tarfile.SYMTYPE, "/etc/passwd"), "absolute or empty target"),
            (special("up", tarfile.SYMTYPE, "../outside"), "out of the tree"),
            (special("a/deep", tarfile.SYMTYPE, "../../outside"), "out of the tree"),
            (special("empty", tarfile.SYMTYPE, ""), "absolute or empty target"),
            (special("intogit", tarfile.SYMTYPE, "sub/../.git/config"), "into a `.git` path"),
        ):
            name = member[0].name
            with self.subTest(name=name):
                shutil.rmtree(self.into, ignore_errors=True)
                answer = self.snapshot(build_tar(regular("ok.txt"), member))
                self.assertTrue((self.into / "ok.txt").is_file())
                self.assertFalse(os.path.lexists(self.into / name))
                ((skipped),) = answer["skipped"]
                self.assertEqual(name, skipped["path"])
                self.assertIn(reason, skipped["reason"])

    def test_hard_links_that_leave_the_tree_are_refused(self):
        for member in (
            special("hard", tarfile.LNKTYPE, "../outside"),
            special("hardabs", tarfile.LNKTYPE, "/etc/passwd"),
            special("hardgit", tarfile.LNKTYPE, "a/../.git/config"),
        ):
            with self.subTest(name=member[0].name):
                self.refused(build_tar(regular("ok.txt"), member))

    def test_a_chain_through_a_symbolic_link_is_caught_by_these_checks_and_not_only_the_filter(self):
        # `a -> .` makes `a/x -> ../outside` normalise to `outside` but resolve
        # above the tree. The refusal names the link, which the `data` filter's
        # own message does not.
        caught = self.refused(build_tar(
            special("a", tarfile.SYMTYPE, "."),
            special("a/x", tarfile.SYMTYPE, "../outside"),
        ))
        self.assertIn("through a symbolic link", str(caught))
        # A file written through a link is refused the same way.
        caught = self.refused(build_tar(special("a", tarfile.SYMTYPE, "."), regular("a/f.txt")))
        self.assertIn("through a symbolic link", str(caught))
        # A link whose target passes through another link is not written:
        # `d/a -> ..` is the top of the tree, so `d/a/../x` is above it.
        answer = self.snapshot(build_tar(
            special("d", tarfile.DIRTYPE),
            special("d/a", tarfile.SYMTYPE, ".."),
            special("b", tarfile.SYMTYPE, "d/a/../x"),
            regular("x"),
        ))
        self.assertEqual([{"path": "b", "reason": "a link that passes through another link"}], answer["skipped"])
        self.assertFalse(os.path.lexists(self.into / "b"))
        # A hard link to a symbolic link is refused.
        shutil.rmtree(self.into)
        caught = self.refused(build_tar(
            regular("x"), special("s", tarfile.SYMTYPE, "x"), special("h", tarfile.LNKTYPE, "s"),
        ))
        self.assertIn("hard link to a symbolic link", str(caught))

    def test_a_directory_laid_out_as_a_bare_repository_is_skipped_and_listed(self):
        # git discovers such a directory as a repository and reads its config,
        # so none of it is written; git tooling commits such fixtures, so the
        # rest of the tree still is.
        for prefix in ("sub/", "a/b/"):
            for names in (
                ("HEAD", "config", "objects/pack/x", "refs/heads/main"),
                ("head", "Objects/x", "REFS/x"),
                ("HEAD", "commondir"),
            ):
                with self.subTest(prefix=prefix, names=names):
                    if self.into.exists():
                        shutil.rmtree(self.into)
                    answer = self.snapshot(build_tar(regular("ok.txt"), *(regular(prefix + name) for name in names)))
                    self.assertTrue((self.into / "ok.txt").is_file())
                    self.assertFalse((self.into / prefix.rstrip("/")).exists())
                    [entry] = answer["skipped"]
                    self.assertEqual(prefix.rstrip("/"), entry["path"])
                    self.assertIn("bare repository", entry["reason"])
        # The whole tree laid out so: nothing of it is written.
        shutil.rmtree(self.into)
        answer = self.snapshot(build_tar(regular("HEAD"), regular("objects/x"), regular("refs/y")))
        self.assertEqual([], list(self.into.iterdir()))
        self.assertEqual(".", answer["skipped"][0]["path"])
        shutil.rmtree(self.into)
        # Paired: the names alone, or two of the three, are ordinary files.
        self.snapshot(build_tar(regular("HEAD"), regular("objects/x"), regular("docs/refs/y")))
        self.assertTrue((self.into / "HEAD").is_file())

    def test_a_tar_git_archive_wrote_from_a_bare_repository_layout_writes_none_of_it(self):
        # The tree a hostile public repository can commit: a bare repository's
        # files, as plain files, whose `config` would run a program on `git log`.
        repo = self.tmp / "layout"
        repo.mkdir()
        git(repo, "init", "--quiet")
        (repo / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / "config").write_text("[core]\n\tbare = true\n[log]\n\tshowSignature = true\n")
        (repo / "objects" / "info").mkdir(parents=True)
        (repo / "objects" / "info" / "keep").write_text("")
        (repo / "refs" / "heads").mkdir(parents=True)
        (repo / "refs" / "heads" / "keep").write_text("")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "looks like docs")
        tar = subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"], cwd=str(repo), capture_output=True, check=True,
        ).stdout
        answer = self.snapshot(tar)
        self.assertEqual([], list(self.into.iterdir()))
        self.assertIn("bare repository", answer["skipped"][0]["reason"])

    def test_a_hard_link_into_a_skipped_bare_layout_is_skipped_too(self):
        answer = self.snapshot(build_tar(
            regular("ok.txt"), regular("fx/HEAD"), regular("fx/objects/x"), regular("fx/refs/y"),
            special("config-copy", tarfile.LNKTYPE, "fx/HEAD"),
        ))
        self.assertTrue((self.into / "ok.txt").is_file())
        self.assertFalse((self.into / "config-copy").exists())
        self.assertFalse((self.into / "fx").exists())
        self.assertEqual(["fx", "config-copy"], [entry["path"] for entry in answer["skipped"]])

    def test_local_git_does_not_open_a_bare_repository_it_was_not_named(self):
        bare = self.tmp / "bare"
        git(self.tmp, "init", "--quiet", "--bare", str(bare))
        with mock.patch.object(vcs_client, "LOCAL_GIT", REAL_GIT), mock.patch.object(vcs_client, "ROOT", self.tmp / "root"):
            done = vcs_client.local_git(bare, "rev-parse", "--git-dir", check=False)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("safe.bareRepository", done.stderr)

    def test_a_link_inside_the_tree_is_kept(self):
        tar = build_tar(
            special("a", tarfile.DIRTYPE),
            regular("a/file.txt", b"in\n"),
            special("a/sibling", tarfile.SYMTYPE, "file.txt"),
            special("a/up", tarfile.SYMTYPE, "../a/file.txt"),
        )
        self.snapshot(tar)
        self.assertEqual("in\n", (self.into / "a/sibling").read_text())
        self.assertEqual("in\n", (self.into / "a/up").read_text())

    def test_device_files_and_fifos_are_refused(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.refused(build_tar(regular("ok.txt"), special("dev", kind)))

    def test_a_tree_over_the_unpacked_ceiling_is_refused(self):
        tar = build_tar(regular("a.txt", b"a" * 10), regular("b.txt", b"b" * 10))
        self.refused(tar, code=vcs_client.SNAPSHOT_TOO_LARGE, max_bytes=15)
        # Paired: at the ceiling it is unpacked.
        self.snapshot(tar, max_bytes=20)
        self.assertTrue((self.into / "b.txt").is_file())

    def test_a_tar_that_does_not_parse_is_refused(self):
        self.refused(b"not a tar at all" * 64)

    def test_a_broker_refusal_writes_nothing(self):
        def refusing(verb, payload):
            raise vcs_client.VcsError("too large", code="SNAPSHOT_TOO_LARGE")

        with mock.patch.object(vcs_client, "call", refusing), self.assertRaises(vcs_client.VcsError):
            vcs_client.snapshot("acme/infra", into=self.into)
        self.assertFalse(self.into.exists())


if __name__ == "__main__":
    unittest.main()
