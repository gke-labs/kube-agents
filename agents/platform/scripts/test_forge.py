#!/usr/bin/env python3
"""Tests for forge.py.

The seam is `vcs_client.call` — the last point before a version-control verb
leaves this pod. Everything below it is the credential broker's, tested in
`test_vcs_broker.py` and `test_providers_contract.py`; everything above it is
what this file is for. That is a much smaller surface than it used to be: the
provider that lived here shelled `gh`, so these tests once pinned REST paths,
`--paginate` flags, three comment endpoints and a collaborator-permission
lookup. All of that is now the forge module's, asked for once instead of twice.

What is left, and carries the weight:

* **The verb and its payload are what this module decides.** A read that asks
  for a page of 30 where the caller assumed 100, or a `proposal-view` that
  forgets `comments: True`, is a sweep that quietly sees less than it thinks.
* **A refusal keeps its reason code.** `FORGE_RATE_LIMITED`, `FORGE_FORBIDDEN`
  and `FORGE_NOT_FOUND` send an operator to three different places, and the
  single `REPO_UNREACHABLE` this used to raise for every failure named none of
  them.
* **An unsupported host raises rather than falling back.** The fallback is what
  would send GitHub calls on behalf of a URL naming somebody else's forge. The
  decision is the broker's now; keeping it legible here is this file's job.
* **Identity, permission and capability are per repository.** An install
  serving two forges authenticates as two accounts. Asking once and reusing the
  answer is the GitHub-only assumption in miniature.
* **The hop into the sandbox is transparent.** The agent pod has no
  `CREDENTIAL_PROXY_URL`, so a verb crosses to a copy of this same module and
  comes back. A refusal must survive that crossing with its code intact, and a
  transport that was down must not read as a repository that was quiet.
* **An unknown permission is not a "no".** A non-member is a definitive no; a
  proxy fault is no answer at all, and the sweep turns a "no" into a public
  refusal carrying a marker that stops the request ever being retried.
"""

import json
import logging
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forge  # noqa: E402
import sandbox_exec  # noqa: E402
import vcs_client  # noqa: E402


def protocol_members() -> set[str]:
    """The operations `forge.ForgeProvider` declares.

    Taken off the Protocol rather than written out, so a member added to it is
    in scope for the conformance checks the moment it is added.
    """
    return {
        name
        for name, value in vars(forge.ForgeProvider).items()
        if callable(value) and not name.startswith("_")
    }


REPO = "acme/toolkit"
OTHER_REPO = "gitlab.example/acme/toolkit"
VIEWER = "kube-agents-bot"


def proposal(number=12, **fields):
    """One `proposal-list` row, in the shape `translate.proposal` produces."""
    node = {
        "number": number,
        "title": "a change",
        "state": "open",
        "author": "kube-agents-bot",
        "source": "platform-agent/fix-1",
        "sourceRepo": REPO,
        "sourceRevision": "a" * 40,
        "target": "main",
        "labels": [],
        "url": f"https://forge.invalid/pulls/{number}",
    }
    node.update(fields)
    return node


def comment(ref="issue-1", **fields):
    """One `proposal-view` comment, in the shape `translate.comment` produces."""
    node = {
        "ref": ref,
        "id": int(ref.rsplit("-", 1)[-1]),
        "kind": ref.rsplit("-", 1)[0],
        "author": "reviewer",
        "body": "@agent please fix",
        "created": "2026-09-01T10:00:00Z",
    }
    node.update(fields)
    return node


def _a_pull_request() -> "forge.PullRequest":
    """A pull request the retry tests can pass around. Its fields do not matter."""
    return forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")


class FakeBroker:
    """`vcs_client.call`, as a scripted table of answers.

    `answers` maps a verb to what the broker returns, or to a callable taking
    the payload for the few tests that need to answer differently per call.
    `refuse` maps a verb to a `VcsError` to raise instead.
    """

    def __init__(self, answers=None, refuse=None):
        self.answers = dict(answers or {})
        self.refuse = dict(refuse or {})
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, verb, payload):
        self.calls.append((verb, dict(payload)))
        if verb in self.refuse:
            raise self.refuse[verb]
        answer = self.answers.get(verb, {})
        return answer(payload) if callable(answer) else answer

    # -- reading the record ------------------------------------------------
    def verbs(self):
        return [verb for verb, _ in self.calls]

    def payloads(self, verb):
        return [payload for name, payload in self.calls if name == verb]

    def one(self, verb):
        sent = self.payloads(verb)
        assert len(sent) == 1, f"{verb} was called {len(sent)} times"
        return sent[0]


class BrokerCase(unittest.TestCase):
    """Patches the seam and turns the sandbox hop off.

    `sandbox_enabled` is False for every test but `ForwardTest`'s: that is the
    shape a skill script the model runs already has -- it is inside the sandbox
    and calls the broker directly -- and it keeps the tests of *what* is asked
    separate from the tests of *how* the question travels.
    """

    def setUp(self):
        self.broker = FakeBroker()
        for target, value in (("call", self.broker),):
            patcher = mock.patch.object(vcs_client, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        enabled = mock.patch.object(sandbox_exec, "sandbox_enabled", lambda: False)
        enabled.start()
        self.addCleanup(enabled.stop)

    def provider(self, **answers):
        self.broker.answers.update(answers)
        return forge.provider_for()


# ---- selection -------------------------------------------------------------


class ProviderSelectionTest(BrokerCase):
    """`provider_for`. The parse itself is `test_repo_ref.py`'s subject."""

    def test_no_repository_still_yields_a_provider(self):
        """What the sweep passes: it discovers repositories after choosing."""
        self.assertIsInstance(forge.provider_for(), forge.BrokerProvider)

    def test_one_provider_serves_every_readable_repository(self):
        """The reason the class is no longer named after a forge.

        `provider_for` used to pick an implementation from the host, so a
        repository on a forge with no class here raised before any call was
        made. The host table is the broker's now: every readable value gets the
        same provider, and which forge it is is decided on the credential side.
        """
        for value in (
            "acme/toolkit",
            "https://github.com/acme/toolkit",
            "git@gitlab.com:group/project",
            "https://bitbucket.org/acme/toolkit",
        ):
            with self.subTest(value=value):
                self.assertIsInstance(
                    forge.provider_for(value), forge.BrokerProvider
                )

    def test_an_unparseable_value_carries_the_operator_facing_reason_code(self):
        """Still local, and still refused before a round trip is spent on it."""
        with self.assertRaises(forge.RepoUnparseable) as ctx:
            forge.provider_for("../..")
        self.assertEqual(ctx.exception.reason, "GIT_REPO_UNPARSEABLE")

    def test_a_host_with_no_forge_module_raises_rather_than_falling_back(self):
        """The refusal moved to the broker; the reason code an operator reads did not.

        Falling back is what would point GitHub calls at a same-named
        repository on behalf of a URL naming somebody else's forge, so the
        broker's `FORGE_UNSUPPORTED` is the one refusal that keeps a code of
        this module's own rather than being passed through.
        """
        self.broker.refuse["identity"] = vcs_client.VcsError(
            "no forge module serves gitlab.com", code="FORGE_UNSUPPORTED"
        )
        with self.assertRaises(forge.UnknownForgeHost) as ctx:
            forge.call("identity", {}, "git@gitlab.com:group/project")
        self.assertEqual(ctx.exception.reason, "FORGE_HOST_UNSUPPORTED")
        self.assertEqual(ctx.exception.value, "gitlab.com")

    def test_an_unsupported_host_with_no_host_to_name_names_the_value(self):
        """Both arms of `_host_of`, which fall together on the same answer.

        `acme/toolkit` parses -- it is the hostless shorthand -- and its `host`
        is empty; `not a ref` does not parse at all. Neither has a host to put
        in the refusal, so both name the value the operator configured rather
        than an empty string.
        """
        self.broker.refuse["identity"] = vcs_client.VcsError(
            "unsupported", code="FORGE_UNSUPPORTED"
        )
        for configured in ("acme/toolkit", "not a ref"):
            with self.subTest(configured=configured):
                with self.assertRaises(forge.UnknownForgeHost) as ctx:
                    forge.call("identity", {}, configured)
                self.assertEqual(ctx.exception.value, configured)


# ---- policy that never reaches a forge -------------------------------------


class NormaliseLoginTest(unittest.TestCase):
    def test_bot_suffix_is_stripped(self):
        self.assertEqual(forge.normalise_login("kube-agents-bot[bot]"), "kube-agents-bot")

    def test_case_is_folded(self):
        self.assertEqual(forge.normalise_login("Kube-Agents-Bot"), "kube-agents-bot")

    def test_app_prefix_is_stripped(self):
        """Some list endpoints spell an App `app/<name>`."""
        self.assertEqual(forge.normalise_login("app/kube-agents-bot"), "kube-agents-bot")

    def test_rest_and_graphql_spellings_converge(self):
        """The whole point: the two APIs disagree, the comparison must not."""
        self.assertEqual(
            forge.normalise_login("kube-agents-bot[bot]"),
            forge.normalise_login("kube-agents-bot"),
        )

    def test_all_three_spellings_of_one_app_converge(self):
        """Regression for an observed infinite re-answer loop.

        The sweep sees the PR author as `app/x` and its own past comments as
        `x[bot]`. When those did not normalise to the same key, no marker the
        agent had written was recognised as its own, so every tick re-answered
        the same comment.
        """
        spellings = ["app/kube-agents-bot", "kube-agents-bot[bot]", "kube-agents-bot"]
        keys = {forge.normalise_login(s) for s in spellings}
        self.assertEqual(keys, {"kube-agents-bot"})

    def test_empty_is_tolerated(self):
        self.assertEqual(forge.normalise_login(""), "")
        self.assertEqual(forge.normalise_login(None), "")


class IsAgentPullRequestTest(unittest.TestCase):
    """Three conditions, and a test for each one failing on its own."""

    def _pr(
        self,
        head_ref="platform-agent/fix-1",
        author="kube-agents-bot[bot]",
        head_repo=REPO,
        labels=(),
    ):
        return forge.PullRequest(
            number=7,
            head_ref=head_ref,
            author=author,
            labels=labels,
            head_repo=head_repo,
        )

    def _ours(self, pr, viewer=VIEWER):
        return forge.is_agent_pull_request(pr, REPO, viewer)

    def test_our_own_pull_request_qualifies(self):
        self.assertTrue(self._ours(self._pr()))

    def test_a_human_branch_does_not(self):
        self.assertFalse(self._ours(self._pr(head_ref="feat/whatever")))

    def test_a_branch_merely_containing_the_prefix_does_not(self):
        self.assertFalse(self._ours(self._pr(head_ref="wip/platform-agent/x")))

    def test_a_fork_branch_with_our_prefix_is_not_ours(self):
        """The branch name is the attacker's to choose on a fork.

        A cross-repository proposal carries the bare branch name, so anyone who
        can fork this repository can open one that reads
        `platform-agent/anything`. Accepting it would hand a stranger's proposal
        to `submit-suggestion`, which amends by pushing `head_ref` to *this*
        repository.
        """
        self.assertFalse(
            self._ours(self._pr(author="stranger", head_repo="stranger/toolkit"))
        )

    def test_our_prefix_on_a_fork_is_still_not_ours_even_authored_by_us(self):
        self.assertFalse(self._ours(self._pr(head_repo="somebody/toolkit")))

    def test_a_deleted_fork_reads_as_not_ours(self):
        """`sourceRepo` is empty once the fork is gone; unknown is not local."""
        self.assertFalse(self._ours(self._pr(head_repo="")))

    def test_someone_elses_pull_request_on_our_branch_name_is_not_ours(self):
        """Where the re-answer loop came from.

        A maintainer can push `platform-agent/x` here and open a proposal on
        it. Keying identity off `pr.author` would then make a human the agent's
        "self", so no marker it wrote would be recognised as its own and every
        tick would re-answer the same comment.
        """
        self.assertFalse(self._ours(self._pr(author="maintainer")))

    def test_the_author_comparison_is_normalised(self):
        self.assertTrue(self._ours(self._pr(author="App/Kube-Agents-Bot")))

    def test_repository_comparison_is_case_insensitive(self):
        self.assertTrue(self._ours(self._pr(head_repo="Acme/Toolkit")))

    def test_no_viewer_means_nothing_is_ours(self):
        self.assertFalse(self._ours(self._pr(), viewer=""))

    def test_ignore_label_opts_out(self):
        self.assertTrue(self._pr(labels=("agent:ignore",)).is_ignored)
        self.assertFalse(self._pr(labels=("bug",)).is_ignored)


# ---- the call ---------------------------------------------------------------


class CallTest(BrokerCase):
    """`forge.call`: one verb, one repository, one exception type above it."""

    def test_the_repository_travels_with_the_verb(self):
        self.broker.answers["capabilities"] = {"acknowledge": True}
        forge.call("capabilities", {}, REPO)
        self.assertEqual(self.broker.one("capabilities")["repository"], REPO)

    def test_the_caller_s_payload_is_not_mutated(self):
        """The repository is added to a copy; the caller may reuse its dict."""
        payload = {"number": 12}
        forge.call("proposal-view", payload, REPO)
        self.assertEqual(payload, {"number": 12})

    def test_a_refusal_keeps_the_broker_s_own_reason_code(self):
        """One code per fault, where there used to be `REPO_UNREACHABLE` for all of them.

        A dead credential, an absent repository and a rate limit are three
        different things to go and fix, and the warning an operator reads is
        built straight out of `reason`.
        """
        for code in ("FORGE_FORBIDDEN", "FORGE_NOT_FOUND", "FORGE_RATE_LIMITED"):
            with self.subTest(code=code):
                self.broker.refuse["proposal-list"] = vcs_client.VcsError(
                    "nope", code=code, detail="the forge said so"
                )
                with self.assertRaises(forge.ForgeError) as ctx:
                    forge.call("proposal-list", {}, REPO)
                self.assertEqual(ctx.exception.reason, code)
                self.assertEqual(ctx.exception.value, "the forge said so")

    def test_a_refusal_with_no_code_is_the_brokers_and_is_named_so(self):
        """The same fault must not be two codes in two sweeps.

        A codeless `VcsError` out of `vcs_client.call` is always a fault on
        this side of the seam -- `CREDENTIAL_PROXY_URL` unset, the socket
        refused, the token unprojected, an answer that is not JSON -- and the
        call never reached a forge. `resolver.py handle_poll` reports that
        class as `BROKER_UNREACHABLE`; this reported it as `REPO_UNREACHABLE`,
        so a restarting broker was one code in the issues card and another in
        the PR-watcher's, and the code is what an operator's glossary keys on.
        """
        self.broker.refuse["proposal-list"] = vcs_client.VcsError("the proxy fell over")
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-list", {}, REPO)
        self.assertEqual(ctx.exception.reason, "BROKER_UNREACHABLE")
        self.assertEqual(ctx.exception.value, "the proxy fell over")

    # -- the one retry -----------------------------------------------------
    def test_a_transient_failure_on_a_read_is_tried_once_more(self):
        """What `_call(..., retry_transient=True)` gave the reads before the port.

        A 502 on `proposal-list` that is not retried aborts the whole tick: the
        sweep catches one `ForgeError` for the repository loop and posts a
        "watcher is not running" card, so a blip costs every repository ten
        minutes rather than costing one call a second attempt.
        """
        for code in sorted(forge.TRANSIENT_CODES):
            with self.subTest(code=code):
                broker = FakeBroker()
                attempts = []

                def answer(payload, _attempts=attempts):
                    _attempts.append(payload)
                    if len(_attempts) == 1:
                        raise vcs_client.VcsError("blip", code=code)
                    return {"proposals": [], "truncated": False}

                broker.answers["proposal-list"] = answer
                with mock.patch.object(vcs_client, "call", broker):
                    result = forge.call(
                        "proposal-list", {}, REPO, retry_transient=True
                    )
                self.assertEqual(result, {"proposals": [], "truncated": False})
                self.assertEqual(len(attempts), 2)

    def test_the_second_attempt_is_the_last_one(self):
        """One retry, not a loop. A forge that is down stays down for this tick."""
        self.broker.refuse["proposal-list"] = vcs_client.VcsError(
            "still down", code="FORGE_UNAVAILABLE"
        )
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-list", {}, REPO, retry_transient=True)
        self.assertEqual(ctx.exception.reason, "FORGE_UNAVAILABLE")
        self.assertEqual(len(self.broker.payloads("proposal-list")), 2)

    def test_a_definitive_refusal_is_not_retried(self):
        """Nothing changes between two calls that a bad credential would survive.

        `FORGE_RATE_LIMITED` is in here deliberately: the broker's guidance for
        it says to wait, and an immediate second call spends the quota it is
        asking for back.
        """
        for code in (
            "FORGE_UNAUTHENTICATED",
            "FORGE_NOT_FOUND",
            "FORGE_FORBIDDEN",
            "FORGE_RATE_LIMITED",
            "FORGE_CONFLICT",
            forge.REASON_SANDBOX_UNREACHABLE,
        ):
            with self.subTest(code=code):
                broker = FakeBroker(
                    refuse={"proposal-list": vcs_client.VcsError("no", code=code)}
                )
                with mock.patch.object(vcs_client, "call", broker):
                    with self.assertRaises(forge.ForgeError):
                        forge.call("proposal-list", {}, REPO, retry_transient=True)
                self.assertEqual(len(broker.payloads("proposal-list")), 1)

    def test_a_write_is_never_retried(self):
        """`retry_transient` is off by default, and the writes leave it off.

        `proposal-comment` that failed after the forge accepted it would post
        the reviewer's answer a second time; there is no idempotency key on that
        route to make the repeat a no-op.
        """
        self.broker.refuse["proposal-comment"] = vcs_client.VcsError(
            "blip", code="FORGE_UNAVAILABLE"
        )
        provider = self.provider()
        with self.assertRaises(forge.ForgeError):
            provider.post_comment(REPO, _a_pull_request(), "hello")
        self.assertEqual(len(self.broker.payloads("proposal-comment")), 1)

    def test_every_read_the_sweep_makes_asks_for_the_retry(self):
        """The reads, named here so a new one does not quietly go without it.

        Checked by running each and counting the calls, rather than by reading
        the argument off the source: what matters is that the second attempt
        happens, not that a keyword appears.
        """
        pr = _a_pull_request()
        reads = {
            "identity": lambda p: p.viewer_login(REPO),
            "proposal-list": lambda p: p.list_open_prs(REPO),
            "proposal-view": lambda p: p.list_comments(REPO, pr),
            "proposal-commits": lambda p: p.list_commits(REPO, pr),
        }
        for verb, read in reads.items():
            with self.subTest(verb=verb):
                broker = FakeBroker(
                    refuse={verb: vcs_client.VcsError("blip", code="FORGE_UNAVAILABLE")}
                )
                with mock.patch.object(vcs_client, "call", broker):
                    with self.assertRaises(forge.ForgeError):
                        read(forge.provider_for())
                self.assertEqual(len(broker.payloads(verb)), 2, verb)

    def test_detail_is_truncated_so_a_warning_stays_readable(self):
        """The warnings go in a Chat card; a page of JSON pushes the rest out of it."""
        self.broker.refuse["proposal-comment"] = vcs_client.VcsError(
            "rejected", code="FORGE_REJECTED", detail="x" * 5000
        )
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-comment", {}, REPO)
        self.assertEqual(len(ctx.exception.value), forge.MAX_DETAIL_CHARS)


# ---- the hop ----------------------------------------------------------------


class ForwardTest(unittest.TestCase):
    """The sandbox crossing. `sandbox_enabled` is True for all of it."""

    def setUp(self):
        enabled = mock.patch.object(sandbox_exec, "sandbox_enabled", lambda: True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.broker = FakeBroker()
        seam = mock.patch.object(vcs_client, "call", self.broker)
        seam.start()
        self.addCleanup(seam.stop)

    def _run(self, returncode=0, stdout="", stderr="", side_effect=None):
        fake = mock.Mock(
            side_effect=side_effect,
            return_value=subprocess.CompletedProcess(
                [], returncode, stdout=stdout, stderr=stderr
            ),
        )
        patcher = mock.patch.object(sandbox_exec, "run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_the_verb_crosses_instead_of_being_sent_from_here(self):
        """The agent pod has no `CREDENTIAL_PROXY_URL`, by design and permanently."""
        run = self._run(stdout=json.dumps({"answer": {"identity": {"login": VIEWER}}}))
        answer = forge.call("identity", {}, REPO)
        self.assertEqual(answer, {"identity": {"login": VIEWER}})
        self.assertEqual(self.broker.calls, [])
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["python3", forge.SANDBOX_FORGE, "call", "identity", "--repository", REPO])

    def test_the_payload_travels_on_stdin_never_on_the_command_line(self):
        """A comment body is a reviewer's own words, thousands of characters of them.

        There is no path both ends can see either -- this pod and the sandbox
        share no filesystem -- so fd 0 is the only route left, and it is also
        the one with no quoting rules to get wrong.
        """
        run = self._run(stdout=json.dumps({"answer": {}}))
        body = "please fix `this`; also \"that\" & $everything\n" * 200
        forge.call("proposal-comment", {"number": 12, "body": body}, REPO)
        self.assertEqual(
            json.loads(run.call_args.kwargs["stdin"]), {"number": 12, "body": body}
        )
        self.assertNotIn(body, " ".join(run.call_args.args[0]))

    def test_the_hop_is_bounded(self):
        run = self._run(stdout=json.dumps({"answer": {}}))
        forge.call("capabilities", {}, REPO)
        self.assertEqual(run.call_args.kwargs["timeout"], forge.FORWARD_TIMEOUT_S)

    def test_a_refusal_survives_the_crossing_with_its_code(self):
        """Flattening it to "the far side exited 1" loses what the callers act on."""
        self._run(
            stdout=json.dumps(
                {
                    "refusal": {
                        "message": "too many requests",
                        "code": "FORGE_RATE_LIMITED",
                        "detail": "retry after 60s",
                    }
                }
            )
        )
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-list", {}, REPO)
        self.assertEqual(ctx.exception.reason, "FORGE_RATE_LIMITED")
        self.assertEqual(ctx.exception.value, "retry after 60s")

    def test_an_unsupported_host_still_arrives_as_an_unknown_forge_host(self):
        self._run(
            stdout=json.dumps(
                {"refusal": {"message": "no module", "code": "FORGE_UNSUPPORTED"}}
            )
        )
        with self.assertRaises(forge.UnknownForgeHost):
            forge.call("identity", {}, "https://gitlab.com/group/project")

    def test_an_unreachable_sandbox_is_not_a_quiet_repository(self):
        """The verb never ran, and "nothing to do" is the one answer that must not be produced."""
        self._run(side_effect=sandbox_exec.SandboxUnavailable("ssh: connect refused"))
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-list", {}, REPO)
        self.assertEqual(ctx.exception.reason, forge.REASON_SANDBOX_UNREACHABLE)

    def test_a_timed_out_hop_reports_the_same_way(self):
        self._run(side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=90))
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-list", {}, REPO)
        self.assertEqual(ctx.exception.reason, forge.REASON_SANDBOX_UNREACHABLE)
        self.assertIn("90s", ctx.exception.value)

    def test_a_far_side_that_itself_failed_reports_its_last_stderr_line(self):
        """`main` answers a refusal with exit 0, so a non-zero exit is the file, not the forge."""
        self._run(
            returncode=127,
            stderr="Traceback (most recent call last):\npython3: No such file",
        )
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("capabilities", {}, REPO)
        # The sandbox's own code. It used to fall through to the codeless
        # fallback, which names the broker -- a component that answered fine.
        self.assertEqual(ctx.exception.reason, "SANDBOX_UNREACHABLE")
        self.assertIn("exited 127", ctx.exception.value)
        self.assertIn("No such file", ctx.exception.value)

    def test_output_that_is_not_json_is_a_fault_not_an_empty_answer(self):
        """Anything the sandbox prints before the answer lands here; silence would read as no proposals."""
        self._run(stdout="Warning: something on stderr leaked into stdout\n")
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.call("proposal-list", {}, REPO)
        self.assertIn("not JSON", ctx.exception.value)
        self.assertEqual(ctx.exception.reason, "SANDBOX_UNREACHABLE")

    def test_json_that_is_not_an_envelope_is_the_same_fault(self):
        """Parsing is not the check -- `null` and a list parse and answer nothing.

        The reads below the parse are `.get`, so any of these used to leave
        `AttributeError` out of a module whose every caller catches
        `ForgeError`: the sweep died on the far side's shape instead of
        reporting it.
        """
        for printed in ("null", "[]", '"a string"', "3"):
            with self.subTest(printed=printed):
                self._run(stdout=printed + "\n")
                with self.assertRaises(forge.ForgeError) as ctx:
                    forge.call("proposal-list", {}, REPO)
                self.assertEqual(ctx.exception.reason, "SANDBOX_UNREACHABLE")
                self.assertIn("not an envelope", ctx.exception.value)


class MainTest(unittest.TestCase):
    """`forge.main`, the far side of the hop."""

    def setUp(self):
        self.broker = FakeBroker()
        seam = mock.patch.object(vcs_client, "call", self.broker)
        seam.start()
        self.addCleanup(seam.stop)

    def _main(self, argv, payload):
        stdin = mock.patch.object(sys, "stdin", io_text(payload))
        stdout = mock.patch.object(sys, "stdout", io_text(""))
        stdin.start()
        out = stdout.start()
        try:
            rc = forge.main(argv)
        finally:
            stdin.stop()
            stdout.stop()
        return rc, out.getvalue()

    def test_an_answer_is_printed_as_an_answer_envelope(self):
        self.broker.answers["identity"] = {"identity": {"login": VIEWER}}
        rc, out = self._main(
            ["call", "identity", "--repository", REPO], json.dumps({})
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"answer": {"identity": {"login": VIEWER}}})
        self.assertEqual(self.broker.one("identity")["repository"], REPO)

    def test_the_payload_is_forwarded_verbatim(self):
        rc, _ = self._main(
            ["call", "proposal-view", "--repository", REPO],
            json.dumps({"number": 12, "comments": True, "limit": 100}),
        )
        self.assertEqual(rc, 0)
        sent = self.broker.one("proposal-view")
        self.assertEqual(sent["number"], 12)
        self.assertTrue(sent["comments"])
        self.assertEqual(sent["limit"], 100)

    def test_a_refusal_exits_zero_and_carries_the_code(self):
        """It is an answer. An exit status would leave the code nowhere to go."""
        self.broker.refuse["proposal-list"] = vcs_client.VcsError(
            "forbidden", code="FORGE_FORBIDDEN", detail="the app is not installed"
        )
        rc, out = self._main(
            ["call", "proposal-list", "--repository", REPO], json.dumps({})
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out)["refusal"],
            {
                "message": "forbidden",
                "code": "FORGE_FORBIDDEN",
                "detail": "the app is not installed",
            },
        )

    def test_an_unreadable_payload_is_a_non_zero_exit(self):
        """Nothing was asked of the forge, so there is no refusal to report."""
        for payload in ("not json at all", json.dumps([1, 2, 3])):
            with self.subTest(payload=payload):
                stderr = mock.patch.object(sys, "stderr", io_text(""))
                stderr.start()
                self.addCleanup(stderr.stop)
                rc, out = self._main(
                    ["call", "identity", "--repository", REPO], payload
                )
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertEqual(self.broker.calls, [])

    def test_the_two_halves_meet(self):
        """A round trip through the real `_forward` and the real `main`.

        The two sides are the same file, which is the reason there is no second
        one to keep in step -- but they are still an envelope written at one end
        and read at the other, so this drives the pair rather than either alone.
        """
        self.broker.answers["proposal-commits"] = {
            "commits": [{"sha": "b" * 40, "committed": "2026-09-01T11:00:00Z"}],
            "count": 1,
            "truncated": False,
        }

        def far_side(argv, **kwargs):
            out = io_text("")
            with mock.patch.object(sys, "stdin", io_text(kwargs["stdin"])), \
                 mock.patch.object(sys, "stdout", out):
                rc = forge.main(argv[2:])
            return subprocess.CompletedProcess(argv, rc, stdout=out.getvalue(), stderr="")

        with mock.patch.object(sandbox_exec, "sandbox_enabled", lambda: True), \
             mock.patch.object(sandbox_exec, "run", far_side):
            answer = forge.call("proposal-commits", {"number": 12, "limit": 100}, REPO)
        self.assertEqual(answer["commits"][0]["sha"], "b" * 40)


def io_text(value: str):
    import io

    return io.StringIO(value)


# ---- identity, permission, capability ---------------------------------------


class ViewerLoginTest(BrokerCase):
    def test_the_account_is_read_from_identity_and_normalised(self):
        provider = self.provider(identity={"identity": {"login": "Kube-Agents-Bot[bot]"}})
        self.assertEqual(provider.viewer_login(REPO), VIEWER)

    def test_identity_is_asked_of_the_repository_s_forge(self):
        """There is no install-wide viewer: two forges, two accounts."""
        provider = self.provider(
            identity=lambda payload: {
                "identity": {"login": "here-bot" if payload["repository"] == REPO else "there-bot"}
            }
        )
        self.assertEqual(provider.viewer_login(REPO), "here-bot")
        self.assertEqual(provider.viewer_login(OTHER_REPO), "there-bot")

    def test_it_asks_about_the_credential_and_not_about_a_login(self):
        provider = self.provider(identity={"identity": {"login": VIEWER}})
        provider.viewer_login(REPO)
        self.assertNotIn("login", self.broker.one("identity"))

    def test_it_is_resolved_once_per_repository(self):
        provider = self.provider(identity={"identity": {"login": VIEWER}})
        for _ in range(3):
            provider.viewer_login(REPO)
        self.assertEqual(len(self.broker.payloads("identity")), 1)

    def test_a_call_that_did_not_happen_raises_with_its_reason(self):
        """A transport that is down must not read as a nameless credential.

        Identity is the first verb a tick sends, so an unreachable sandbox
        reaches this before anything else. Collapsing it into "" would put the
        repository in the sweep's `nameless` list and post the one warning that
        says the credential is broken -- sending an operator to the credential
        for a pod that is merely restarting. Raising puts it in the guard that
        prints the reason code instead.
        """
        for code in ("SANDBOX_UNREACHABLE", "FORGE_RATE_LIMITED", "FORGE_UNAUTHENTICATED"):
            with self.subTest(code=code):
                self.broker.refuse["identity"] = vcs_client.VcsError("no", code=code)
                provider = forge.provider_for()
                with self.assertRaises(forge.ForgeError) as raised:
                    provider.viewer_login(REPO)
                self.assertEqual(raised.exception.reason, code)

    def test_an_answer_carrying_no_login_is_empty_and_is_cached(self):
        """This is the nameless credential, and it is the only one.

        The forge answered; what it answered with names no account. Cached
        because otherwise every pull request in the sweep pays for the same
        lookup.
        """
        provider = self.provider(identity={"identity": {"login": ""}})
        for _ in range(3):
            self.assertEqual(provider.viewer_login(REPO), "")
        self.assertEqual(len(self.broker.payloads("identity")), 1)

    def test_a_permission_lookup_teaches_the_viewer_for_free(self):
        """Every `identity` answer carries the viewer, so resolving a commenter pays for it."""
        provider = self.provider(
            identity={"identity": {"login": VIEWER, "canWrite": True}}
        )
        self.assertTrue(provider._has_write(REPO, "maintainer"))
        self.assertEqual(provider.viewer_login(REPO), VIEWER)
        self.assertEqual(len(self.broker.payloads("identity")), 1)


class PermissionTest(BrokerCase):
    def _provider(self, can_write):
        return self.provider(
            identity=lambda payload: {
                "identity": {"login": VIEWER, "canWrite": can_write}
            }
        )

    def test_a_granted_permission_is_known(self):
        self.assertIs(self._provider(True)._has_write(REPO, "maintainer"), True)

    def test_a_non_member_is_a_definitive_no(self):
        self.assertIs(self._provider(False)._has_write(REPO, "stranger"), False)

    def test_an_unanswered_permission_is_none_and_not_false(self):
        """`False` becomes a public refusal carrying a marker that is never retried.

        A five-second network blip would then refuse a maintainer permanently,
        and they would have to notice and re-comment to get anywhere.
        """
        self.assertIsNone(self._provider(None)._has_write(REPO, "maintainer"))

    def test_a_refusal_is_not_an_answer_either(self):
        self.broker.refuse["identity"] = vcs_client.VcsError("boom", code="FORGE_UNAVAILABLE")
        provider = forge.provider_for()
        with self.assertLogs(forge.LOGGER, logging.INFO):
            self.assertIsNone(provider._has_write(REPO, "maintainer"))

    def test_a_refusal_is_remembered_for_the_tick(self):
        """"Nothing answered" is cached the way an answer is, and for the tick only.

        A broker that is refusing is refusing for every comment author on every
        swept pull request, so re-asking would pay the timeout once per
        commenter and hold the sweep open for the sum of them. The instance is
        built fresh each tick, which is what keeps this from being a refusal
        remembered past the outage that caused it.
        """
        self.broker.refuse["identity"] = vcs_client.VcsError("boom", code="FORGE_UNAVAILABLE")
        provider = forge.provider_for()
        with self.assertLogs(forge.LOGGER, logging.INFO):
            self.assertIsNone(provider._has_write(REPO, "maintainer"))
        # What the first lookup cost, retries included -- `_identity` asks with
        # `retry_transient`, so an unavailable forge is more than one call.
        once = len(self.broker.payloads("identity"))
        for _ in range(3):
            self.assertIsNone(provider._has_write(REPO, "maintainer"))
        self.assertEqual(len(self.broker.payloads("identity")), once)

    def test_an_empty_login_costs_no_call(self):
        provider = self._provider(True)
        self.assertIs(provider._has_write(REPO, ""), False)
        self.assertEqual(self.broker.calls, [])

    def test_permission_is_looked_up_once_per_account(self):
        provider = self._provider(True)
        for _ in range(3):
            provider._has_write(REPO, "maintainer")
            provider._has_write(REPO, "Maintainer")
        self.assertEqual(len(self.broker.payloads("identity")), 1)

    def test_the_same_account_on_two_repositories_is_two_questions(self):
        """Writing is a permission on a repository, not a property of the account."""
        provider = self._provider(True)
        provider._has_write(REPO, "maintainer")
        provider._has_write(OTHER_REPO, "maintainer")
        self.assertEqual(len(self.broker.payloads("identity")), 2)

    def test_a_bot_and_a_user_of_the_same_name_are_two_accounts(self):
        """Two questions to the forge, and the App one says so.

        They arrive spelled the same: the forge module strips `[bot]` from
        every author it emits and reports the fact beside the author as `bot`.
        Keyed on the login alone the App `foo[bot]` and the user `foo` share
        one slot -- whichever commented first decides trust for both, which
        either clears the sweep's only trust gate for a non-collaborator or
        writes a permanent `agent-refused` marker at a maintainer. And the
        question has to *carry* the flag: asked about the bare `foo`, the forge
        answers for the user, an unrelated principal or a 404, never the App.
        Through `list_comments`, because `_has_write` handed `foo[bot]` directly
        is a spelling that can no longer arrive there.
        """
        answers = {("foo", False): True, ("foo", True): False}
        provider = self.provider(
            **{
                "proposal-view": {
                    "proposal": proposal(),
                    "comments": [
                        comment("issue-1", author="foo"),
                        comment("issue-2", author="foo", bot=True),
                    ],
                },
                "identity": lambda payload: {
                    "identity": {
                        "login": VIEWER,
                        "canWrite": answers[(payload["login"], payload.get("bot", False))],
                    }
                },
            }
        )
        human, app = provider.list_comments(REPO, _a_pull_request())
        self.assertIs(human.can_write, True)
        self.assertIs(app.can_write, False)
        asked = self.broker.payloads("identity")
        self.assertEqual(len(asked), 2)
        self.assertNotIn("bot", asked[0])
        self.assertIs(asked[1]["bot"], True)
        # Cached per account, not per login: the same two again cost nothing.
        provider.list_comments(REPO, _a_pull_request())
        self.assertEqual(len(self.broker.payloads("identity")), 2)


class CapabilityTest(BrokerCase):
    def test_acknowledge_is_read_from_its_own_field(self):
        """Not from `verbs`: every forge routes `proposal-acknowledge`.

        One with no reactions accepts the verb and answers
        `{"acknowledged": false}` having done nothing, so the verb list says the
        call is accepted and this field says it would achieve something.
        """
        provider = self.provider(
            capabilities={"acknowledge": False, "verbs": ["proposal-acknowledge"]}
        )
        self.assertFalse(provider.supports_acknowledge(REPO))

    def test_a_forge_with_reactions_says_so(self):
        provider = self.provider(capabilities={"acknowledge": True})
        self.assertTrue(provider.supports_acknowledge(REPO))

    def test_it_is_asked_once_per_repository(self):
        provider = self.provider(capabilities={"acknowledge": True})
        for _ in range(3):
            provider.supports_acknowledge(REPO)
        provider.supports_acknowledge(OTHER_REPO)
        self.assertEqual(len(self.broker.payloads("capabilities")), 2)

    def test_capabilities_that_cannot_be_read_answer_no(self):
        """The 👀 is a courtesy; not knowing costs the same as not being able to."""
        self.broker.refuse["capabilities"] = vcs_client.VcsError("down")
        provider = forge.provider_for()
        with self.assertLogs(forge.LOGGER, logging.INFO):
            self.assertFalse(provider.supports_acknowledge(REPO))


# ---- the operations ----------------------------------------------------------


class ListOpenPrsTest(BrokerCase):
    def test_only_the_open_ones_are_asked_for_and_a_full_page_at_that(self):
        provider = self.provider(**{"proposal-list": {"proposals": []}})
        provider.list_open_prs(REPO)
        sent = self.broker.one("proposal-list")
        self.assertEqual(sent["state"], "open")
        self.assertEqual(sent["limit"], forge.PAGE_SIZE)
        self.assertEqual(sent["page"], 1)
        self.assertEqual(sent["repository"], REPO)

    def test_rows_are_normalised(self):
        provider = self.provider(
            **{"proposal-list": {"proposals": [proposal(labels=["bug", "agent:ignore"])]}}
        )
        (pr,) = provider.list_open_prs(REPO)
        self.assertEqual(pr.number, 12)
        self.assertEqual(pr.head_ref, "platform-agent/fix-1")
        self.assertEqual(pr.head_repo, REPO)
        self.assertEqual(pr.head_sha, "a" * 40)
        self.assertEqual(pr.author, "kube-agents-bot")
        self.assertEqual(pr.labels, ("bug", "agent:ignore"))
        self.assertTrue(pr.is_ignored)

    def test_the_head_repository_is_what_tells_a_fork_from_this_one(self):
        """`sourceRepo` is the first of the three things `is_agent_pull_request` checks."""
        provider = self.provider(
            **{"proposal-list": {"proposals": [proposal(sourceRepo="stranger/toolkit")]}}
        )
        (pr,) = provider.list_open_prs(REPO)
        self.assertFalse(forge.is_agent_pull_request(pr, REPO, VIEWER))

    def test_a_deleted_fork_leaves_an_empty_head_repo(self):
        provider = self.provider(
            **{"proposal-list": {"proposals": [proposal(sourceRepo=None)]}}
        )
        (pr,) = provider.list_open_prs(REPO)
        self.assertEqual(pr.head_repo, "")

    def test_missing_fields_do_not_crash_the_sweep(self):
        provider = self.provider(**{"proposal-list": {"proposals": [{}]}})
        (pr,) = provider.list_open_prs(REPO)
        self.assertEqual((pr.number, pr.head_ref, pr.author, pr.labels), (0, "", "", ()))

    def test_a_truncated_page_is_followed_by_the_next(self):
        """The provider this replaced paginated, and its test said why.

        "Human pull requests share that budget, so on a busy repository the
        agent's own would fall out of the window." GitHub lists newest first,
        so the proposals that fall out are the oldest -- the ones that have
        waited longest for an answer -- and every tick loses the same ones.
        """
        pages = {
            1: {"proposals": [proposal(1)], "truncated": True},
            2: {"proposals": [proposal(2)], "truncated": True},
            3: {"proposals": [proposal(3)], "truncated": False},
        }
        provider = self.provider(**{"proposal-list": lambda payload: pages[payload["page"]]})
        with mock.patch.object(forge.LOGGER, "warning") as warn:
            numbers = [pr.number for pr in provider.list_open_prs(REPO)]
        self.assertEqual(numbers, [1, 2, 3])
        self.assertEqual(
            [sent["page"] for sent in self.broker.payloads("proposal-list")], [1, 2, 3]
        )
        # Read to the end, so nothing to warn about and nothing for the sweep.
        warn.assert_not_called()
        self.assertEqual(provider.truncations(), [])

    def test_a_listing_that_never_ends_is_reported_rather_than_hidden(self):
        """A bound on the walk, and the walk says when it hit it.

        A forge whose `truncated` was wrong would otherwise be paged forever on
        every ten-minute tick; past the bound the listing is read short, and a
        short listing that looks complete is the thing the deleted test guarded
        against -- so it is logged and recorded for the operator warning.
        """
        provider = self.provider(
            **{"proposal-list": {"proposals": [proposal()], "truncated": True}}
        )
        with self.assertLogs(forge.LOGGER, logging.WARNING) as logs:
            listed = provider.list_open_prs(REPO)
        self.assertEqual(len(listed), forge.MAX_PAGES)
        self.assertEqual(len(self.broker.payloads("proposal-list")), forge.MAX_PAGES)
        self.assertIn(f"ran past {forge.MAX_PAGES} pages", "\n".join(logs.output))
        self.assertEqual(provider.truncations(), [f"the open proposals on {REPO}"])

    def test_a_complete_page_says_nothing(self):
        provider = self.provider(
            **{"proposal-list": {"proposals": [proposal()], "truncated": False}}
        )
        with mock.patch.object(forge.LOGGER, "warning") as warn:
            provider.list_open_prs(REPO)
        warn.assert_not_called()


class ListCommentsTest(BrokerCase):
    def setUp(self):
        super().setUp()
        self.pr = forge.PullRequest(
            number=12, head_ref="platform-agent/x", author=VIEWER, head_repo=REPO
        )

    def _provider(self, comments, can_write=True):
        return self.provider(
            **{
                "proposal-view": {"proposal": proposal(), "comments": comments},
                "identity": {"identity": {"login": VIEWER, "canWrite": can_write}},
            }
        )

    def test_the_conversation_is_asked_for_explicitly(self):
        """`proposal-view` answers the proposal alone unless the comments are requested."""
        provider = self._provider([])
        provider.list_comments(REPO, self.pr)
        sent = self.broker.one("proposal-view")
        self.assertEqual(sent["number"], 12)
        self.assertTrue(sent["comments"])
        self.assertEqual(sent["limit"], forge.PAGE_SIZE)

    def test_rows_are_normalised(self):
        provider = self._provider(
            [comment("review_comment-5", path="main.go", line=42, body="tighten this")]
        )
        (c,) = provider.list_comments(REPO, self.pr)
        self.assertEqual(c.ref, "review_comment-5")
        self.assertEqual(c.numeric_id, 5)
        self.assertEqual(c.kind, "review_comment")
        self.assertEqual(c.path, "main.go")
        self.assertEqual(c.line, 42)
        self.assertEqual(c.body, "tighten this")
        self.assertEqual(c.created_at, "2026-09-01T10:00:00Z")

    def test_the_order_the_forge_gave_is_kept(self):
        """Merging and ordering three endpoints into one conversation is the forge module's job."""
        provider = self._provider(
            [
                comment("issue-1", created="2026-09-01T10:00:00Z"),
                comment("review_comment-1", created="2026-09-01T10:00:01Z"),
                comment("review-9", created="2026-09-01T10:00:02Z"),
            ]
        )
        refs = [c.ref for c in provider.list_comments(REPO, self.pr)]
        self.assertEqual(refs, ["issue-1", "review_comment-1", "review-9"])

    def test_ref_distinguishes_comments_that_share_a_numeric_id(self):
        """Two of the three endpoints number independently, so an id alone is not an identity.

        An answered-marker keyed on the number would let an answer to either
        suppress the other.
        """
        provider = self._provider([comment("issue-1"), comment("review_comment-1")])
        comments = provider.list_comments(REPO, self.pr)
        self.assertEqual(len({c.ref for c in comments}), 2)
        self.assertEqual(len({c.numeric_id for c in comments}), 1)

    def test_write_permission_becomes_a_boolean(self):
        provider = self._provider([comment(author="maintainer")], can_write=True)
        (c,) = provider.list_comments(REPO, self.pr)
        self.assertTrue(c.can_write)
        self.assertTrue(c.can_write_known)

    def test_a_read_only_account_cannot_direct_the_agent(self):
        provider = self._provider([comment(author="stranger")], can_write=False)
        (c,) = provider.list_comments(REPO, self.pr)
        self.assertFalse(c.can_write)
        self.assertTrue(c.can_write_known)

    def test_an_unanswered_lookup_fails_closed_but_says_it_did_not_answer(self):
        provider = self._provider([comment(author="maintainer")], can_write=None)
        (c,) = provider.list_comments(REPO, self.pr)
        self.assertFalse(c.can_write)
        self.assertFalse(c.can_write_known)

    def test_permission_is_one_call_per_distinct_author(self):
        provider = self._provider(
            [
                comment("issue-1", author="maintainer"),
                comment("issue-2", author="maintainer"),
                comment("issue-3", author="stranger"),
            ]
        )
        provider.list_comments(REPO, self.pr)
        self.assertEqual(len(self.broker.payloads("identity")), 2)

    def test_is_bot_comes_from_the_verb_and_not_from_the_login(self):
        """The suffix is gone by the time this side sees the author.

        `translate.actor` removes `[bot]` from every login the forge module
        emits, so reading "is this an automation" off the spelling answers
        False for every comment there is -- which silently retires the gate
        that stops two agents answering each other. The verb carries the fact
        instead, and this is the shape the real broker sends: an author with no
        suffix and `bot` beside it.
        """
        provider = self._provider(
            [comment(author="kube-agents-bot", bot=True), comment(ref="issue-2")]
        )
        bot, human = provider.list_comments(REPO, self.pr)
        self.assertTrue(bot.is_bot)
        self.assertFalse(human.is_bot)

    def test_a_truncated_conversation_refuses_rather_than_returning_short(self):
        """The one listing where a partial answer is wrong, not merely incomplete.

        The caller subtracts the requests it already answered by finding its
        own markers in this list. A marker past the ceiling reads as a request
        nobody answered, and the reply it provokes lands past the ceiling too,
        so the same reviewer is answered again on every tick. Refusing is what
        turns that into one operator warning.
        """
        provider = self.provider(
            **{
                "proposal-view": {
                    "proposal": proposal(),
                    "comments": [comment("issue-1")],
                    "commentsTruncated": True,
                },
                "identity": {"identity": {"login": VIEWER, "canWrite": True}},
            }
        )
        with self.assertRaises(forge.ForgeError) as caught:
            provider.list_comments(REPO, self.pr)
        self.assertEqual(caught.exception.reason, forge.REASON_CONVERSATION_TRUNCATED)
        self.assertIn(f"{REPO}#12", str(caught.exception))

    def test_a_conversation_that_fits_is_not_refused(self):
        provider = self.provider(
            **{
                "proposal-view": {
                    "proposal": proposal(),
                    "comments": [comment("issue-1")],
                    "commentsTruncated": False,
                },
                "identity": {"identity": {"login": VIEWER, "canWrite": True}},
            }
        )
        self.assertEqual(len(provider.list_comments(REPO, self.pr)), 1)


class PostCommentTest(BrokerCase):
    def test_the_body_is_a_field_in_the_request(self):
        provider = self.provider(**{"proposal-comment": {}})
        pr = forge.PullRequest(number=12, head_ref="x", author=VIEWER)
        body = "a reviewer's own words, quoted back\n" * 100
        provider.post_comment(REPO, pr, body)
        sent = self.broker.one("proposal-comment")
        self.assertEqual(sent, {"number": 12, "body": body, "repository": REPO})

    def test_a_failed_post_is_not_swallowed(self):
        """Nothing downstream can tell an unsaid answer from a said one."""
        self.broker.refuse["proposal-comment"] = vcs_client.VcsError(
            "rejected", code="FORGE_REJECTED"
        )
        provider = forge.provider_for()
        pr = forge.PullRequest(number=12, head_ref="x", author=VIEWER)
        with self.assertRaises(forge.ForgeError):
            provider.post_comment(REPO, pr, "hello")


class AcknowledgeTest(BrokerCase):
    PR = forge.PullRequest(number=12, head_ref="platform-agent/x", author=VIEWER)

    def _comment(self, kind="issue", numeric_id=5):
        return forge.Comment(
            ref=f"{kind}-{numeric_id}",
            numeric_id=numeric_id,
            author="reviewer",
            body="",
            can_write=True,
            created_at="",
            kind=kind,
        )

    def test_the_comment_is_named_by_id_and_kind(self):
        """The pair the verb takes, rather than a `ref` split back apart here."""
        provider = self.provider(**{"proposal-acknowledge": {"acknowledged": True}})
        self.assertTrue(
            provider.acknowledge(REPO, self.PR, self._comment("review_comment", 7))
        )
        sent = self.broker.one("proposal-acknowledge")
        self.assertEqual(sent["comment"], {"id": 7, "kind": "review_comment"})
        # The shipped forge does not read it. It is sent because a comment id
        # is not everywhere sufficient to locate a comment, and a request shape
        # that depended on which forge answered would defeat the point.
        self.assertEqual(sent["number"], 12)

    def test_a_kind_with_nowhere_to_react_is_a_false_the_broker_answers(self):
        provider = self.provider(**{"proposal-acknowledge": {"acknowledged": False}})
        self.assertFalse(provider.acknowledge(REPO, self.PR, self._comment("review", 9)))

    def test_a_failed_reaction_never_blocks_the_answer(self):
        """Best-effort by contract: the 👀 exists so the reviewer sees something inside the tick."""
        self.broker.refuse["proposal-acknowledge"] = vcs_client.VcsError(
            "forbidden", code="FORGE_FORBIDDEN"
        )
        provider = forge.provider_for()
        with self.assertLogs(forge.LOGGER, logging.INFO) as logs:
            self.assertFalse(provider.acknowledge(REPO, self.PR, self._comment()))
        self.assertIn("issue-5", "\n".join(logs.output))


class ListCommitsTest(BrokerCase):
    def setUp(self):
        super().setUp()
        self.pr = forge.PullRequest(number=12, head_ref="x", author=VIEWER)

    def test_shas_are_returned_in_the_order_given_with_their_dates(self):
        provider = self.provider(
            **{
                "proposal-commits": {
                    "commits": [
                        {"sha": "a" * 40, "committed": "2026-09-01T10:00:00Z"},
                        {"sha": "b" * 40, "committed": "2026-09-01T11:00:00Z"},
                    ]
                }
            }
        )
        commits = provider.list_commits(REPO, self.pr)
        self.assertEqual([c.sha for c in commits], ["a" * 40, "b" * 40])
        self.assertEqual(commits[-1].committed_at, "2026-09-01T11:00:00Z")

    def test_the_committer_date_is_the_one_that_travels(self):
        """A rebase preserves an author date written weeks ago; the question is "after the request?"."""
        provider = self.provider(
            **{
                "proposal-commits": {
                    "commits": [
                        {
                            "sha": "a" * 40,
                            "committed": "2026-09-01T11:00:00Z",
                            "authored": "2026-08-01T09:00:00Z",
                        }
                    ]
                }
            }
        )
        (c,) = provider.list_commits(REPO, self.pr)
        self.assertEqual(c.committed_at, "2026-09-01T11:00:00Z")

    def test_a_missing_date_is_empty_not_absent(self):
        """Unverifiable, which the caller treats as a failed claim rather than a pass."""
        provider = self.provider(
            **{"proposal-commits": {"commits": [{"sha": "a" * 40}]}}
        )
        (c,) = provider.list_commits(REPO, self.pr)
        self.assertEqual(c.committed_at, "")

    def test_a_row_without_a_sha_is_dropped(self):
        provider = self.provider(
            **{"proposal-commits": {"commits": [{"committed": "2026-09-01T11:00:00Z"}]}}
        )
        self.assertEqual(provider.list_commits(REPO, self.pr), [])

    def test_the_newest_commit_is_on_the_last_page_and_is_read(self):
        """An amendment is checked against these, and the amendment is the newest.

        The forge lists oldest first, so a pull request past one page keeps its
        newest commit -- the one a reply is about -- on the last page. "A
        long-lived pull request outruns one page, and a missed commit reads as
        a false claim", as the deleted test put it.
        """
        pages = {
            1: {"commits": [{"sha": "a" * 40}], "truncated": True},
            2: {"commits": [{"sha": "b" * 40}], "truncated": False},
        }
        provider = self.provider(
            **{"proposal-commits": lambda payload: pages[payload["page"]]}
        )
        commits = provider.list_commits(REPO, self.pr)
        self.assertEqual([c.sha for c in commits], ["a" * 40, "b" * 40])
        self.assertEqual(
            [sent["page"] for sent in self.broker.payloads("proposal-commits")], [1, 2]
        )
        self.assertEqual(provider.truncations(), [])

    def test_a_listing_read_past_the_bound_names_the_proposal(self):
        provider = self.provider(
            **{
                "proposal-commits": {
                    "commits": [{"sha": "a" * 40}],
                    "truncated": True,
                }
            }
        )
        with self.assertLogs(forge.LOGGER, logging.WARNING) as logs:
            provider.list_commits(REPO, self.pr)
        self.assertIn("#12", "\n".join(logs.output))
        self.assertEqual(provider.truncations(), [f"the commits on #12 on {REPO}"])

    def test_a_failure_raises_rather_than_reporting_no_commits(self):
        """An empty list is "you did not amend the branch", posted publicly."""
        self.broker.refuse["proposal-commits"] = vcs_client.VcsError("down")
        provider = forge.provider_for()
        with self.assertRaises(forge.ForgeError):
            provider.list_commits(REPO, self.pr)


class ProtocolConformanceTest(unittest.TestCase):
    def test_the_provider_implements_every_operation(self):
        """Read off the Protocol, not copied from it.

        The hand-kept list here was seven names while `ForgeProvider` had eight,
        so `truncations` -- which the sweep calls on every tick -- was outside
        every conformance check in the suite. A list that has to be edited
        alongside the Protocol is a list that will be a member short again.
        """
        members = protocol_members()
        self.assertIn("truncations", members)
        provider = forge.BrokerProvider()
        for name in members:
            self.assertTrue(callable(getattr(provider, name, None)), name)

    def test_the_design_document_names_the_same_operations(self):
        """`docs/README.md` points a reader at §3 of that document for this protocol.

        It reproduced seven of the eight members and called them "the complete
        set", which is how a second provider written from the document would
        miss one and raise `AttributeError` mid-sweep. The document is the
        published surface; this keeps it honest without anyone re-reading it.
        """
        design = (
            Path(__file__).resolve().parents[3]
            / "docs/designs/pr-comment-conversation.md"
        )
        block = re.search(
            r"class ForgeProvider\(Protocol\):(.*?)```", design.read_text(), re.S
        )
        self.assertIsNotNone(block, "the protocol block is no longer in §3")
        documented = set(re.findall(r"def (\w+)\(", block.group(1)))
        self.assertEqual(
            protocol_members(),
            documented,
            "docs/designs/pr-comment-conversation.md §3 and forge.ForgeProvider "
            "disagree about the protocol",
        )
        # The other design that names the count, in prose rather than in a
        # block: it said "seven" for a round after the eighth member landed,
        # and contradicted the document it defers to in the same sentence.
        words = {7: "seven", 8: "eight", 9: "nine", 10: "ten"}
        word = words[len(protocol_members())]
        prose = " ".join(
            (design.parent / "version-control-support.md").read_text().split()
        )
        for phrase in (f"`ForgeProvider` as {word} operations", f"each of its {word} operations"):
            self.assertIn(
                phrase,
                prose,
                "docs/designs/version-control-support.md counts the protocol's "
                f"operations differently from forge.ForgeProvider ({word})",
            )

    def test_nothing_here_branches_on_which_forge_it_is(self):
        """One class, every forge -- the reason it is no longer `GitHubProvider`.

        Forge names do appear in this file, in comments explaining why a
        normalisation exists and which forge forced it. That is the same
        allowance `providers/registry.py` has and the opposite of a code path:
        what this asserts is that no method dispatches on a host, which is the
        thing that would make this a second implementation again.
        """
        import inspect

        source = inspect.getsource(forge.BrokerProvider)
        # Every spelling a dispatch could take, not just the double-quoted
        # equality: a guard that reads for one of them passes a rewrite into
        # any of the others, which is the regression it exists to catch.
        for name in ("github", "gitlab", "bitbucket"):
            for spelling in (
                f'== "{name}', f"== '{name}", f'!= "{name}', f"!= '{name}",
                f'in ("{name}', f"in ('{name}", f'startswith("{name}',
                f"startswith('{name}", f'"{name}" in ', f"'{name}' in ",
            ):
                self.assertNotIn(spelling, source.lower(), f"{name}: {spelling}")
        self.assertNotIn("_host_of", source)


if __name__ == "__main__":
    unittest.main()
