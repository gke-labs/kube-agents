#!/usr/bin/env python3
"""vcs_client.py as a library: the shape a consumer script imports, not the CLI.

The command-line suite (`skills/version-control/scripts/test_vcs.py`) drives
every operation through `vcs.py`, which is a thin front over this module; what
that suite cannot show is that the module stands on its own for a caller that
never builds an argparse namespace. These tests are that caller.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import subprocess
import sys
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

    def test_capabilities_takes_a_repository_without_a_working_copy(self):
        with mock.patch.object(vcs_client, "call", lambda verb, payload: {"verb": verb, **payload}):
            self.assertEqual(
                vcs_client.capabilities("acme/infra"),
                {"verb": "capabilities", "repository": "acme/infra"},
            )

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
                    "size": len(blob), "bundleBase64": base64.b64encode(blob).decode("ascii")}
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


if __name__ == "__main__":
    unittest.main()
