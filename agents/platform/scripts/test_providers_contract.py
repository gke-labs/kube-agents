"""One suite, run against every forge this install has, not one file per forge.

Each forge package ships a `fixtures/` directory of recorded API responses --
the JSON its host actually returns for each verb it claims to serve -- and this
suite reads them. The assertions are about the *neutral* shape: that a proposal
has three states rather than one forge's two-plus-a-timestamp, that a listing
says when it is truncated, that a verb a forge does not serve refuses by name.

The point of the inversion is where the cost of a second forge falls. A file
per forge means holding a new one to the same assertions is a shared-test edit
somebody has to remember; here a new package ships its fixtures and this file
does not change.

Fixtures rather than a live API because CI has no credential and no egress, and
recorded rather than invented because an invented fixture encodes what its
author believed the API returns, which is the belief the test was supposed to
be checking.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from providers import AVAILABLE, COLLABORATION_VERBS, ForgeUnsupported
from providers.credentials import BrokeredCredential
from workspace_paths import WorkspaceError

SCRIPTS = Path(__file__).resolve().parent

# What each verb's answer is keyed by, and which concept that key holds. Derived
# from the verb name because the naming is the contract: `issue-list` returns
# `issues`, and a forge that returned something else has not implemented the
# verb the caller asked for.
CONCEPTS = {"proposal": "proposal", "issue": "issue"}

# The fields a caller may rely on, per concept. A forge may not omit one and may
# not add its own vocabulary alongside them -- the second is the failure that
# matters, because a caller that finds `head.ref` in the answer starts using it.
SHAPES: dict[str, frozenset[str]] = {
    "proposal": frozenset(
        {
            "number",
            "title",
            "state",
            "draft",
            "author",
            "source",
            "target",
            "url",
            "created",
            "updated",
            "body",
        }
    ),
    "issue": frozenset(
        {
            "number",
            "title",
            "state",
            "author",
            "labels",
            "assignees",
            "url",
            "created",
            "updated",
            "body",
        }
    ),
    "comment": frozenset({"author", "created", "body", "url"}),
}

PROPOSAL_STATES = frozenset({"open", "closed", "merged"})


def fixtures_dir(forge_class: type) -> Path:
    package = Path(sys.modules[forge_class.__module__].__file__).parent
    return package / "fixtures"


class Recorded:
    """The transport's `api`, answering from a fixture instead of the network."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, path, *, params=None, body=None, raw=None) -> Any:
        self.calls.append((method, path, params, body, raw))
        if raw:
            # A raw request asks for a media type rather than JSON; no fixture
            # models a diff, and none needs to -- it is returned unparsed.
            return "diff --git a/x b/x\n"
        if not self.responses:
            raise AssertionError(f"the forge made an unfixtured call: {method} {path}")
        return self.responses.pop(0)


def forge_cases() -> list[tuple[str, type]]:
    return sorted(((cls.name, cls) for cls in AVAILABLE), key=lambda pair: pair[0])


class ContractTest(unittest.TestCase):
    """Written as one test per property, subtesting over forges and verbs.

    The other arrangement -- a generated TestCase per forge -- reads better in
    a runner and hides which property failed behind a forge's name. What a
    reviewer needs from a failure here is the property.
    """

    def instances(self) -> list[tuple[str, Any, Path]]:
        built = []
        for name, cls in forge_cases():
            for forge in cls.for_config({}):
                built.append((name, forge, fixtures_dir(cls)))
        self.assertTrue(built, "no forge in AVAILABLE built an instance")
        return built

    def load(self, directory: Path, verb: str) -> dict:
        return json.loads((directory / f"{verb}.json").read_text())

    def invoke(self, forge, verb: str, fixture: dict) -> tuple[Any, Recorded]:
        api = Recorded(fixture["responses"])
        method = getattr(forge, verb.replace("-", "_"))
        return method(api, "acme/infra", dict(fixture["payload"])), api

    # -- coverage -----------------------------------------------------------

    def test_a_forge_ships_a_fixture_for_every_verb_it_claims(self):
        # The check that keeps the rest of this file from passing vacuously: a
        # forge could claim all eight and be tested on none.
        for name, forge, directory in self.instances():
            for verb in forge.verbs:
                with self.subTest(forge=name, verb=verb):
                    self.assertTrue(
                        (directory / f"{verb}.json").is_file(),
                        f"{name} claims `{verb}` and ships no recorded response",
                    )

    def test_a_forge_claims_only_verbs_that_exist(self):
        for name, forge, _ in self.instances():
            for verb in forge.verbs:
                with self.subTest(forge=name, verb=verb):
                    self.assertIn(verb, COLLABORATION_VERBS)

    def test_a_verb_a_forge_does_not_serve_refuses_by_name(self):
        # An install that cannot do something should say so in a form the agent
        # can report and route around. A `NotImplementedError` in the process
        # holding the credential is not one.
        for name, forge, _ in self.instances():
            for verb in COLLABORATION_VERBS:
                if verb in forge.verbs:
                    continue
                with self.subTest(forge=name, verb=verb):
                    with self.assertRaises(ForgeUnsupported) as caught:
                        self.invoke(forge, verb, {"payload": {}, "responses": []})
                    self.assertEqual(caught.exception.status, 501)
                    self.assertIn(verb, str(caught.exception))

    # -- the neutral shape --------------------------------------------------

    def test_every_verb_answers_in_the_shape_its_name_promises(self):
        for name, forge, directory in self.instances():
            for verb in forge.verbs:
                concept, _, action = verb.partition("-")
                fixture = self.load(directory, verb)
                with self.subTest(forge=name, verb=verb):
                    answer, _ = self.invoke(forge, verb, fixture)
                    if action == "comment":
                        self.assertEqual(
                            set(answer["comment"]), SHAPES["comment"]
                        )
                    elif action == "list":
                        key = f"{concept}s"
                        self.assertEqual(
                            sorted(answer), sorted([key, "count", "truncated"])
                        )
                        self.assertEqual(answer["count"], len(answer[key]))
                        self.assertIsInstance(answer["truncated"], bool)
                        for item in answer[key]:
                            self.assertEqual(set(item), SHAPES[CONCEPTS[concept]])
                    else:
                        self.assertEqual(
                            set(answer[concept]), SHAPES[CONCEPTS[concept]]
                        )

    def test_a_proposal_has_three_states_everywhere(self):
        # Closed and merged are different outcomes on every forge. Where one
        # encodes the difference outside its `state` field, the translation is
        # what hides that, and this is the assertion that it did.
        for name, forge, directory in self.instances():
            for verb in ("proposal-view", "proposal-list", "proposal-create"):
                if verb not in forge.verbs:
                    continue
                fixture = self.load(directory, verb)
                with self.subTest(forge=name, verb=verb):
                    answer, _ = self.invoke(forge, verb, fixture)
                    items = answer.get("proposals") or [answer["proposal"]]
                    for item in items:
                        self.assertIn(item["state"], PROPOSAL_STATES)

    def test_a_listing_declares_itself_truncated_against_the_limit(self):
        for name, forge, directory in self.instances():
            for verb in ("proposal-list", "issue-list"):
                if verb not in forge.verbs:
                    continue
                fixture = self.load(directory, verb)
                key = f"{verb.partition('-')[0]}s"
                with self.subTest(forge=name, verb=verb):
                    payload = dict(fixture["payload"], limit=1)
                    answer, _ = self.invoke(
                        forge, verb, {**fixture, "payload": payload}
                    )
                    self.assertTrue(answer["truncated"])
                    self.assertGreaterEqual(len(answer[key]), 1)

    def test_reading_comments_returns_them_in_the_comment_shape(self):
        for name, forge, directory in self.instances():
            for verb in ("proposal-view", "issue-view"):
                if verb not in forge.verbs:
                    continue
                fixture = self.load(directory, verb)
                if not fixture["payload"].get("comments"):
                    continue
                with self.subTest(forge=name, verb=verb):
                    answer, _ = self.invoke(forge, verb, fixture)
                    self.assertTrue(answer["comments"])
                    for item in answer["comments"]:
                        self.assertEqual(set(item), SHAPES["comment"])

    # -- what the forge asked for -------------------------------------------

    def test_a_forge_composes_a_request_and_not_a_url(self):
        # The transport owns the host. A forge that returned an absolute URL
        # would be choosing where the credential is presented, which is the one
        # decision the host allowlist exists to keep away from it.
        for name, forge, directory in self.instances():
            for verb in forge.verbs:
                fixture = self.load(directory, verb)
                with self.subTest(forge=name, verb=verb):
                    _, api = self.invoke(forge, verb, fixture)
                    self.assertTrue(api.calls)
                    for method, path, params, body, _raw in api.calls:
                        self.assertIn(method, {"GET", "POST", "PATCH", "PUT", "DELETE"})
                        self.assertNotIn("://", path)
                        self.assertFalse(path.startswith("/"))
                        self.assertIn("acme/infra", path)
                        self.assertIsInstance(params, (dict, type(None)))
                        self.assertIsInstance(body, (dict, type(None)))

    def test_a_write_verb_sends_its_prose_in_a_body(self):
        # Not in a path and not in a query. What a caller wrote must not end up
        # in an argv, in `ps`, or in a `CalledProcessError` some layer logs.
        for name, forge, directory in self.instances():
            for verb in forge.verbs:
                if not verb.endswith(("-create", "-comment")):
                    continue
                fixture = self.load(directory, verb)
                prose = fixture["payload"].get("body") or ""
                with self.subTest(forge=name, verb=verb):
                    _, api = self.invoke(forge, verb, fixture)
                    method, path, params, body, _raw = api.calls[-1]
                    self.assertEqual(method, "POST")
                    self.assertIsInstance(body, dict)
                    if prose:
                        self.assertIn(prose, [str(value) for value in body.values()])
                        self.assertNotIn(prose, path)
                        self.assertNotIn(
                            prose, [str(value) for value in (params or {}).values()]
                        )

    def test_the_shared_validators_reject_the_same_inputs_for_every_forge(self):
        # Validation is a property of the caller's request, not of a forge's
        # API, so a forge that reimplemented it looser would be the one an
        # attacker picks. These are refused before any call is made.
        bad = {
            "proposal-view": {"number": "3"},
            "issue-view": {"number": 0},
            "proposal-comment": {"number": 1, "body": "   "},
            "issue-comment": {"number": True, "body": "hi"},
            "proposal-create": {"title": "t", "source": "--upload-pack=x", "target": "main"},
            "issue-create": {"title": "", "body": "b"},
            "issue-list": {"labels": "bug"},
            "proposal-list": {"state": "merged"},
        }
        for name, forge, _ in self.instances():
            for verb, payload in bad.items():
                if verb not in forge.verbs:
                    continue
                with self.subTest(forge=name, verb=verb):
                    api = Recorded([])
                    method = getattr(forge, verb.replace("-", "_"))
                    with self.assertRaises(WorkspaceError):
                        method(api, "acme/infra", payload)
                    self.assertEqual(api.calls, [])

    # -- the prohibition ----------------------------------------------------

    def test_no_forge_runs_a_subprocess(self):
        # A forge package is ordinary Python inside the process that holds the
        # token, so nothing at the language level stops it from shelling out --
        # which is why this is a test rather than an assumption. Every control
        # on an executed command lives in one executor, and a forge that ran
        # its own command would be a second path past all of them with none of
        # them reporting that they had been skipped.
        for name, forge, directory in self.instances():
            for verb in forge.verbs:
                fixture = self.load(directory, verb)
                with self.subTest(forge=name, verb=verb):
                    with mock.patch.object(
                        subprocess, "run", side_effect=AssertionError("ran a command")
                    ), mock.patch.object(
                        subprocess, "Popen", side_effect=AssertionError("ran a command")
                    ):
                        self.invoke(forge, verb, fixture)

    def test_a_clone_url_is_composed_and_points_at_the_forge_s_own_host(self):
        for name, forge, _ in self.instances():
            with self.subTest(forge=name):
                self.assertTrue(forge.hosts)
                repo = forge.parse(f"https://{forge.hosts[0]}/acme/infra.git")
                url = forge.clone_url(repo)
                self.assertTrue(url.startswith("https://"))
                self.assertIn(forge.hosts[0], url)
                self.assertNotIn("@", url)

    def test_a_forge_declares_a_cli_only_when_it_is_reached_through_one(self):
        for name, forge, _ in self.instances():
            with self.subTest(forge=name):
                self.assertIn(forge.transport, {"cli", "http"})
                if forge.transport == "http":
                    self.assertEqual(forge.cli, "")
                else:
                    self.assertTrue(forge.cli)

    def test_capabilities_costs_nothing(self):
        # It is the call an agent makes to find out what it can do, so it must
        # not be the call that spends a token discovering it cannot.
        for name, forge, _ in self.instances():
            with self.subTest(forge=name):
                answer = forge.capabilities("acme/infra")
                self.assertEqual(answer["forge"], name)
                self.assertEqual(answer["repo"], "acme/infra")
                self.assertTrue(answer["proposalNoun"])
                for verb in forge.verbs:
                    self.assertIn(verb, answer["verbs"])


class BrokeredCredentialTest(unittest.TestCase):
    """The one refusal `ensure` must not swallow."""

    def test_a_transient_refresh_failure_is_swallowed(self):
        # The behaviour the class is built around: the broker may already hold
        # a valid token, in which case a failed re-acquisition is the only
        # thing that failed and the verb should still run.
        def blow_up(provider, repo):
            raise RuntimeError("the helper is not there")

        BrokeredCredential("acme", blow_up).ensure("acme/infra")

    def test_an_authorization_refusal_is_not(self):
        # `refresh_forge_credential` asks the managed-repository list and
        # raises `PermissionError` when the answer is no. Swallowed, that let
        # the verb proceed against a repository that had just been refused --
        # on a token that was perfectly valid, which is why nothing downstream
        # would have stopped it.
        def refuse(provider, repo):
            raise PermissionError(f"{repo} is not one this install manages")

        with self.assertRaises(PermissionError):
            BrokeredCredential("acme", refuse).ensure("acme/not-ours")


if __name__ == "__main__":
    unittest.main()
