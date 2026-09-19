"""Tests for the broker-side version-control routes.

`clone` and `publish` run against a real `git` and real local repositories, for
the reason `test_content_workspace.py` gives: the properties being asserted are
properties of what git does with a bundle, and a mock would assert what this file
believes git does. The forge is the seam that makes that possible — a test forge
registered in the host allowlist points `clone_url` at a directory, so the same
code path that would reach github.com reaches a bare repository on disk.

The collaboration verbs go the other way. There is no local GitHub, so those
tests drive a recorder in place of `gh` and assert two things a live call could
not tell apart: the request the forge composed, and the translation it applied to
the answer. The translation is the part with judgement in it.

Several test names say what is not proven. `test_publish_refuses_a_bundle_that
_carries_more_than_the_named_branch` is about the declaration matching the
contents; it is not a claim that the objects are safe, which is what never
checking the tree out is for, and which has its own test.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import providers
import repo_ref
import vcs_broker
from providers import (
    BROKER_VERBS,
    COLLABORATION_VERBS,
    ForgeUnsupported,
    Registry,
    validate_branch,
    validate_labels,
    validate_limit,
    validate_number,
    validate_revision,
    validate_state,
)
from vcs_broker import VcsBroker, route_table
from workspace_paths import WorkspaceError

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd: Path | str, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **GIT_ENV},
    )


def git_runner(argv, cwd, check=True, config=()):
    """The shape the broker's git runner presents.

    `config` is what the forge's credential asked for on this invocation. The
    executor turns it into a `GIT_CONFIG_COUNT` layer; here it becomes `-c`
    flags, which is the same precedence and is visible to an assertion.
    """
    flags = [flag for key, value in config for flag in ("-c", f"{key}={value}")]
    return subprocess.run(
        [argv[0], *flags, *argv[1:]],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **GIT_ENV},
    )


class RecordingCredential:
    """A credential that writes down when it was made current.

    The lifecycle tests are about *ordering* -- that nothing is spent before
    the credential is refreshed -- which is observable without a token.
    """

    def __init__(self, log: list) -> None:
        self.log = log
        self.config: tuple[tuple[str, str], ...] = ()

    def ensure(self, repo: str) -> None:
        self.log.append(repo)

    def headers(self, repo: str) -> dict:
        return {}

    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
        return self.config


class LocalForge(providers.Forge):
    """A forge whose repositories are directories.

    This is the whole point of the forge seam: `clone` and `publish` contain no
    forge at all, so pointing `clone_url` somewhere else is enough to run them
    for real against a bare repository on disk.
    """

    name = "local"
    hosts = ("local.test",)
    proposal_noun = "change proposal"

    def __init__(self, root: Path, minted: list | None = None) -> None:
        super().__init__()
        self.root = Path(root)
        self.minted: list[str] = [] if minted is None else minted
        self.credential = RecordingCredential(self.minted)

    @classmethod
    def for_config(cls, config):
        return ()

    def parse(self, url: str) -> str:
        return "/".join(providers.repo_segments(url, self.hosts))

    def clone_url(self, repo: str) -> str:
        return str(self.root / repo)


class ProposingLocalForge(LocalForge):
    """`LocalForge` plus the one collaboration verb the `advance` check asks.

    A directory has no proposals, so the plain `LocalForge` above is the forge
    that cannot be asked. This one answers, out of a set the test sets, and it
    is what proves the check is a lookup rather than a reading of the request.
    """

    verbs = ("proposal-list",)

    def __init__(self, root, minted=None, open_sources=()):
        super().__init__(root, minted)
        self.open_sources = set(open_sources)
        self.listed: list[dict] = []

    def proposal_list(self, api, repo, payload):
        self.listed.append(dict(payload))
        source = payload.get("source")
        found = [{"id": 1, "source": source}] if source in self.open_sources else []
        return {"proposals": found, "truncated": False}


class Recorder:
    """A stand-in for the forge CLI, holding what it saw and what it answers."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []

    def __call__(self, argv, stdin=None, *_args, **_kwargs):
        self.calls.append(list(argv))
        self.stdin.append(stdin)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return subprocess.CompletedProcess(argv, 0, text, "")

    @property
    def path(self) -> str:
        """The API path of the last call, which is what most assertions want."""
        return self.calls[-1][4]

    @property
    def body(self) -> dict:
        """The JSON body of the last call, which never travels in argv."""
        return json.loads(self.stdin[-1] or "null")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class ValidatorTest(unittest.TestCase):
    def test_branch_accepts_ordinary_names(self):
        for good in ("main", "fix/replicas", "release-1.2", "a"):
            self.assertEqual(validate_branch(good), good)
        self.assertEqual(validate_branch("  main  "), "main")

    def test_branch_refuses_what_git_would_read_as_something_else(self):
        for bad in (
            "--upload-pack=touch /tmp/x",
            "fix/..%2fetc",
            "a..b",
            "main@{1}",
            "main.lock",
            "",
            None,
            "/leading",
            "spa ced",
        ):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_branch(bad)

    def test_revision_wants_a_full_object_id(self):
        full = "0" * 40
        self.assertEqual(validate_revision(full), full)
        for bad in ("0" * 39, "0" * 41, "HEAD", "0" * 40 + "^", "", None):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_revision(bad)

    def test_number_refuses_a_bool(self):
        # `True` is an int in Python, and `issues/True` is a 404 the caller
        # cannot read as a validation error.
        self.assertEqual(validate_number(7), 7)
        for bad in (True, 0, -1, "3", None, 1.0):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_number(bad)

    def test_limit_defaults_and_caps(self):
        self.assertEqual(validate_limit(None), providers.DEFAULT_PAGE_SIZE)
        self.assertEqual(validate_limit(5), 5)
        self.assertEqual(validate_limit(10_000), providers.MAX_PAGE_SIZE)
        with self.assertRaises(WorkspaceError):
            validate_limit(0)

    def test_state_and_labels(self):
        self.assertEqual(validate_state(None), "open")
        self.assertEqual(validate_state("  CLOSED "), "closed")
        with self.assertRaises(WorkspaceError):
            validate_state("merged")
        self.assertEqual(validate_labels([" bug ", "p1"]), ["bug", "p1"])
        self.assertEqual(validate_labels(None), [])
        for bad in ("bug", [""], [3]):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_labels(bad)


# ---------------------------------------------------------------------------
# the allowlist
# ---------------------------------------------------------------------------


class HostResolutionTest(unittest.TestCase):
    """The allowlist is the security boundary, so these cases are load-bearing.

    A caller-supplied URL decides which forge a credential is presented to.
    Everything downstream composes its own clone URL from validated segments,
    but only because this step refused the ones it should.
    """

    def setUp(self):
        self.registry = Registry({})

    def resolve_forge(self, url):
        return self.registry.resolve(url)

    def test_scheme_comes_off_before_the_host_is_read(self):
        # `repo_ref` owns the parse; what this suite pins is that the registry
        # reads the host from it rather than searching the string.
        self.assertEqual(repo_ref.parse("https://github.com/acme/infra").host, "github.com")
        self.assertEqual(repo_ref.parse("git@github.com:acme/infra.git").host, "github.com")
        self.assertEqual(repo_ref.parse("ssh://gitlab.com/acme/infra").host, "gitlab.com")
        self.assertEqual(repo_ref.parse("acme/infra").host, "")

    def test_userinfo_cannot_hide_the_host(self):
        # `oauth2:x@evil.example/acme/infra` split at the first `:` reads
        # `oauth2`, which is not a host and would fall through to the
        # bare-name default.
        self.assertEqual(
            repo_ref.parse("https://oauth2:token@evil.example/acme/infra").host,
            "evil.example",
        )
        with self.assertRaises(ForgeUnsupported):
            self.resolve_forge("https://oauth2:token@evil.example/acme/infra")

    def test_a_bare_name_means_github(self):
        forge, repo = self.resolve_forge("acme/infra")
        self.assertEqual(forge.name, "github")
        self.assertEqual(repo, "acme/infra")

    def test_an_unknown_host_is_refused_rather_than_defaulted(self):
        with self.assertRaises(ForgeUnsupported) as caught:
            self.resolve_forge("https://git.internal.example/acme/infra")
        self.assertEqual(caught.exception.status, 501)
        self.assertIn("github.com", str(caught.exception))

    def test_a_recognised_but_unserved_host_names_the_gap(self):
        forge, repo = self.resolve_forge("https://gitlab.com/acme/infra")
        self.assertEqual(forge.name, "gitlab")
        self.assertEqual(repo, "acme/infra")
        with self.assertRaises(ForgeUnsupported) as caught:
            forge.clone_url(repo)
        self.assertIn("no credential is configured", str(caught.exception))

    def test_urls_in_every_form_reach_the_same_repository(self):
        forge = self.registry.default
        for url in (
            "acme/infra",
            "https://github.com/acme/infra",
            "https://github.com/acme/infra.git",
            "https://www.github.com/acme/infra",
            "git@github.com:acme/infra.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(forge.parse(url), "acme/infra")

    def test_a_deeper_path_is_refused(self):
        forge = self.registry.default
        for bad in ("acme", "acme/infra/tree/main", "acme/../etc", ""):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                forge.parse(bad)

    def test_the_clone_url_is_composed_here_not_taken_from_the_caller(self):
        forge, repo = self.resolve_forge(
            "https://oauth2:token@github.com/acme/infra.git"
        )
        self.assertEqual(forge.clone_url(repo), "https://github.com/acme/infra.git")

    def test_only_a_cli_backed_forge_grants_a_cli(self):
        # What the credentialed process may run is derived from the forges this
        # install actually built, not from the union of every forge that could
        # exist. A stub grants nothing.
        self.assertEqual(self.registry.executables, ("gh",))
        for stub in self.registry.stubs:
            self.assertEqual(stub.cli, "")


# ---------------------------------------------------------------------------
# clone and publish, against a real git
# ---------------------------------------------------------------------------


class RepositoryVerbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.forges = base / "forges"
        self.origin = self.forges / "acme/infra"
        self.origin.mkdir(parents=True)
        git(self.origin, "init", "--quiet", "--bare", "--initial-branch=main")

        seed = base / "seed"
        seed.mkdir()
        git(seed, "init", "--quiet", "--initial-branch=main")
        (seed / "README.md").write_text("origin\n")
        (seed / "run.sh").write_text("#!/bin/sh\necho hi\n")
        os.chmod(seed / "run.sh", 0o755)
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "first")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "--quiet", "origin", "main")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.seed = seed
        self.origin_head = git(seed, "rev-parse", "HEAD").stdout.strip()

        self.refreshed: list[str] = []
        self.forge = LocalForge(self.forges, self.refreshed)

        self.scratch = base / "scratch"
        self.broker = VcsBroker(self.scratch, git_runner=git_runner)
        # Registering an extra forge is a dict entry, which is the claim the
        # rest of this class rests on: `clone` and `publish` reach a directory
        # by the same code path that reaches a hosted forge.
        self.broker.registry.hosts["local.test"] = self.forge

    # -- helpers ---------------------------------------------------------

    def clone_locally(self, dest: str = "work") -> tuple[Path, dict]:
        """Do what `vcs.py clone` does: fetch a bundle and unpack it."""
        answer = self.broker.clone({"repository": "local.test/acme/infra"})
        work = Path(self.tmp.name) / dest
        bundle = Path(self.tmp.name) / f"{dest}.bundle"
        bundle.write_bytes(base64.b64decode(answer["bundleBase64"]))
        git(
            self.tmp.name,
            "clone",
            "--quiet",
            "--branch",
            answer["branch"],
            str(bundle),
            str(work),
        )
        git(work, "remote", "remove", "origin")
        return work, answer

    def bundle_of(self, work: Path, branch: str, base: str) -> str:
        out = Path(self.tmp.name) / f"{branch.replace('/', '-')}.out.bundle"
        git(work, "bundle", "create", str(out), branch, f"^{base}")
        return base64.b64encode(out.read_bytes()).decode("ascii")

    def commit_in(self, work: Path, path: str, text: str, message: str) -> str:
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(work, "add", "--", path)
        git(work, "commit", "--quiet", "-m", message)
        return git(work, "rev-parse", "HEAD").stdout.strip()

    def remote_tip(self, branch: str) -> str:
        return git(self.origin, "rev-parse", f"refs/heads/{branch}").stdout.strip()

    # -- clone -----------------------------------------------------------

    def test_clone_returns_a_bundle_that_restores_the_history(self):
        work, answer = self.clone_locally()
        self.assertEqual(answer["forge"], "local")
        self.assertEqual(answer["repo"], "acme/infra")
        self.assertEqual(answer["branch"], "main")
        self.assertEqual(answer["revision"], self.origin_head)
        self.assertEqual((work / "README.md").read_text(), "origin\n")
        self.assertEqual(
            git(work, "rev-parse", "HEAD").stdout.strip(), self.origin_head
        )

    def test_the_bundle_carries_head_so_the_clone_is_not_unborn(self):
        # A bundle written from a named branch alone has no HEAD ref, and a
        # clone from it lands with nothing checked out and a log that reports no
        # revisions. This is the assertion that pins the `HEAD` in the argv.
        work, _ = self.clone_locally()
        status = git(work, "status", "--porcelain=v2", "--branch")
        self.assertNotIn("branch.oid (initial)", status.stdout)
        self.assertEqual(git(work, "rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_clone_leaves_nothing_behind(self):
        self.broker.clone({"repository": "local.test/acme/infra"})
        self.assertEqual(sorted(p.name for p in self.scratch.iterdir()), [])

    def test_clone_makes_the_credential_current_before_it_spends(self):
        self.broker.clone({"repository": "local.test/acme/infra"})
        self.assertEqual(self.refreshed, ["acme/infra"])

    def test_the_forge_s_git_config_reaches_the_git_the_broker_runs(self):
        # How a credential is *presented* to git differs by forge -- a helper
        # pin, an http header, an insteadOf -- and the seam that carries it is
        # `git_config`. Asserted with a harmless key, because the property under
        # test is that whatever the credential asked for arrives at all, on
        # every invocation and without the broker knowing what it means.
        seen: list[tuple] = []

        def recording_git(argv, cwd, check=True, config=()):
            seen.append(tuple(config))
            return git_runner(argv, cwd, check, config)

        self.forge.credential.config = (("credential.helper", "!true"),)
        broker = VcsBroker(self.scratch / "recorded", git_runner=recording_git)
        broker.registry.hosts["local.test"] = self.forge
        answer = broker.clone({"repository": "local.test/acme/infra"})

        self.assertEqual(answer["revision"], self.origin_head)
        self.assertTrue(seen)
        self.assertTrue(all(call == (("credential.helper", "!true"),) for call in seen))

    def test_clone_refuses_depth_because_a_bundle_cannot_carry_one(self):
        # `git bundle create` in a shallow repository succeeds and writes a
        # bundle whose boundary revisions name parents it does not hold; the
        # clone at the far end then fails with "remote did not send all
        # necessary objects". Refusing here is the answer the caller can act on.
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.clone({"repository": "local.test/acme/infra", "depth": 1})
        self.assertIn("shallow boundary", str(caught.exception))

    def test_a_named_branch_makes_the_clone_single_branch(self):
        git(self.seed, "checkout", "--quiet", "-b", "side")
        self.commit_in(self.seed, "side.txt", "s\n", "side work")
        git(self.seed, "push", "--quiet", "origin", "side")
        answer = self.broker.clone(
            {"repository": "local.test/acme/infra", "branch": "main"}
        )
        self.assertEqual(answer["branch"], "main")
        work = Path(self.tmp.name) / "narrow"
        bundle = Path(self.tmp.name) / "narrow.bundle"
        bundle.write_bytes(base64.b64decode(answer["bundleBase64"]))
        git(self.tmp.name, "clone", "--quiet", "--branch", "main", str(bundle), str(work))
        self.assertNotIn("side", git(work, "branch", "--all").stdout)

    def test_clone_refuses_a_history_over_the_ceiling(self):
        self.broker.max_bundle_bytes = 1
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.clone({"repository": "local.test/acme/infra"})
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(caught.exception.fields.get("code"), "BUNDLE_TOO_LARGE")
        # And still nothing left behind on the refusal path.
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_clone_refuses_a_working_tree_over_the_ceiling(self):
        self.broker.max_clone_bytes = 1
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.clone({"repository": "local.test/acme/infra"})
        self.assertEqual(caught.exception.fields.get("code"), "CLONE_TOO_LARGE")

    def test_clone_of_an_unserved_forge_says_what_is_missing(self):
        with self.assertRaises(ForgeUnsupported):
            self.broker.clone({"repository": "https://gitlab.com/acme/infra"})

    # -- publish ---------------------------------------------------------

    def test_publish_puts_the_caller_s_revisions_on_the_remote(self):
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "fix/replicas")
        tip = self.commit_in(work, "README.md", "changed\n", "change it")
        result = self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "fix/replicas",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(
                    work, "fix/replicas", answer["revision"]
                ),
            }
        )
        self.assertEqual(result["revision"], tip)
        self.assertEqual(self.remote_tip("fix/replicas"), tip)

    def test_publish_reads_the_bundle_without_a_transport(self):
        # This one is here because the suite missed the bug. `git_runner` above
        # runs a bare git, and the executor in production does not: it pins
        # `GIT_ALLOW_PROTOCOL=https`, which refuses the `file` transport that a
        # `fetch <path>` of the incoming bundle needs. Publish therefore passed
        # every test here and answered 502 `transport 'file' not allowed` on a
        # real install, while every read verb passed there too -- reading
        # travels as `bundle create`, which fetches nothing.
        #
        # Reproducing the pin inside `git_runner` is not the fix: the origin
        # remotes in these tests *are* local paths, so the pin would refuse the
        # setup rather than the thing under test. What is asserted instead is
        # the property the pin cares about -- the bundle is read by a
        # subcommand that opens no transport, and no git in the publish path
        # takes the bundle's path as a remote.
        seen: list[tuple[str, ...]] = []

        def recording_git(argv, cwd, check=True, config=()):
            seen.append(tuple(argv))
            return git_runner(argv, cwd, check, config)

        broker = VcsBroker(self.scratch / "transport", git_runner=recording_git)
        broker.registry.hosts["local.test"] = self.forge
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "no-transport")
        self.commit_in(work, "README.md", "changed\n", "change it")
        broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "no-transport",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(
                    work, "no-transport", answer["revision"]
                ),
            }
        )

        unbundled = [argv for argv in seen if argv[1:3] == ("bundle", "unbundle")]
        self.assertEqual(len(unbundled), 1, seen)
        bundles = [argv[3] for argv in unbundled]
        for argv in seen:
            if argv[1] in {"fetch", "clone", "ls-remote", "push"}:
                self.assertFalse(
                    set(argv) & set(bundles),
                    f"{argv[1]} was handed the bundle path as a remote: {argv}",
                )

    def test_publish_preserves_the_executable_bit(self):
        # The mode is the property arm B could not carry, so it gets its own
        # assertion at the other end of the round trip.
        work, answer = self.clone_locally()
        self.assertTrue(os.access(work / "run.sh", os.X_OK))
        git(work, "checkout", "--quiet", "-b", "mode")
        (work / "next.sh").write_text("#!/bin/sh\necho next\n")
        os.chmod(work / "next.sh", 0o755)
        git(work, "add", "-A")
        git(work, "commit", "--quiet", "-m", "add a script")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "mode",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(work, "mode", answer["revision"]),
            }
        )
        listing = git(self.origin, "ls-tree", "mode", "next.sh").stdout
        self.assertTrue(listing.startswith("100755"), listing)

    def test_publish_never_checks_the_incoming_objects_out(self):
        # A hook among the incoming objects is only dangerous if something
        # materialises it. The assertion is on the filesystem the broker used:
        # after the push, its scratch tree is gone and the hook never ran.
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "hooked")
        marker = Path(self.tmp.name) / "hook-ran"
        hooks = work / "shipped-hooks"
        hooks.mkdir()
        (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(hooks / "post-checkout", 0o755)
        (work / ".gitattributes").write_text("* filter=evil\n")
        git(work, "add", "-A")
        git(work, "commit", "--quiet", "-m", "carry a hook and a filter")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "hooked",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(work, "hooked", answer["revision"]),
            }
        )
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_publish_refuses_a_bundle_that_does_not_descend_from_the_base(self):
        # An unrelated history: the objects are fine, the claim is not.
        work, answer = self.clone_locally()
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git(other, "init", "--quiet", "--initial-branch=orphan")
        (other / "x").write_text("x\n")
        git(other, "add", "-A")
        git(other, "commit", "--quiet", "-m", "unrelated")
        out = Path(self.tmp.name) / "orphan.bundle"
        git(other, "bundle", "create", str(out), "orphan")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "orphan",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": base64.b64encode(out.read_bytes()).decode(),
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "NOT_FAST_FORWARD")

    def test_publish_survives_the_target_advancing(self):
        # The case this suite used to assert a refusal for, and the reason it
        # is now asserted the other way: requiring the bundle to contain
        # everything on the target means a topic branch must be rebased onto
        # the tip of the shared branch before every publish, so any push by
        # anyone in between refuses a change that would have merged cleanly.
        # On a shared branch that is most of them, and the refusal it handed
        # back said to clone again -- the one operation that discards the work.
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "late")
        tip = self.commit_in(work, "mine.txt", "mine\n", "mine")
        bundle = self.bundle_of(work, "late", answer["revision"])
        # Somebody else pushes to main between the clone and the publish.
        self.commit_in(self.seed, "theirs.txt", "theirs\n", "theirs")
        git(self.seed, "push", "--quiet", "origin", "main")
        result = self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "late",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": bundle,
            }
        )
        self.assertEqual(result["revision"], tip)
        self.assertEqual(self.remote_tip("late"), tip)

    def test_publish_refuses_when_the_target_was_rewritten(self):
        # What BASE_MOVED is for after the change above: the revision this copy
        # was cloned at is not on the target any more, so the target was
        # replaced rather than advanced and there is nothing to build on.
        # "Clone again and reapply" is the right advice here and only here.
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "late")
        self.commit_in(work, "mine.txt", "mine\n", "mine")
        bundle = self.bundle_of(work, "late", answer["revision"])
        git(self.seed, "checkout", "--quiet", "--orphan", "rewritten")
        self.commit_in(self.seed, "fresh.txt", "fresh\n", "a new root")
        git(self.seed, "push", "--quiet", "--force", "origin", "rewritten:main")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "late",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": bundle,
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "BASE_MOVED")

    def test_publish_refuses_to_clobber_a_diverged_branch(self):
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "shared")
        self.commit_in(work, "README.md", "mine\n", "mine")
        bundle = self.bundle_of(work, "shared", answer["revision"])
        # The same branch name, built independently, already on the remote.
        git(self.seed, "checkout", "--quiet", "-b", "shared")
        self.commit_in(self.seed, "other.txt", "theirs\n", "theirs")
        git(self.seed, "push", "--quiet", "origin", "shared")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "shared",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": bundle,
                }
            )
        self.assertEqual(caught.exception.fields.get("code"), "BRANCH_DIVERGED")

    def test_publish_accepts_a_fast_forward_of_an_existing_branch(self):
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "rolling")
        first = self.commit_in(work, "a.txt", "a\n", "a")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "rolling",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(work, "rolling", answer["revision"]),
            }
        )
        second = self.commit_in(work, "b.txt", "b\n", "b")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "rolling",
                "target": "main",
                "baseRevision": first,
                "bundleBase64": self.bundle_of(work, "rolling", first),
            }
        )
        self.assertEqual(self.remote_tip("rolling"), second)

    def test_publish_refuses_a_bundle_carrying_more_than_the_named_branch(self):
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "declared")
        self.commit_in(work, "a.txt", "a\n", "a")
        git(work, "branch", "smuggled")
        out = Path(self.tmp.name) / "two.bundle"
        git(work, "bundle", "create", str(out), "declared", "smuggled", f"^{answer['revision']}")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "declared",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": base64.b64encode(out.read_bytes()).decode(),
                }
            )
        self.assertIn("exactly refs/heads/declared", str(caught.exception))
        self.assertNotIn("smuggled", git(self.origin, "branch", "--list").stdout)

    def test_publish_refuses_input_that_is_not_a_bundle(self):
        for payload, fragment in (
            ({"bundleBase64": "not base64!!"}, "valid base64"),
            ({"bundleBase64": ""}, "base64 bundle"),
            ({}, "base64 bundle"),
        ):
            with self.subTest(payload=payload), self.assertRaises(WorkspaceError) as c:
                self.broker.publish(
                    {
                        "repository": "local.test/acme/infra",
                        "branch": "x",
                        "target": "main",
                        "baseRevision": "0" * 40,
                        **payload,
                    }
                )
            self.assertIn(fragment, str(c.exception))

    def test_publish_refuses_an_oversized_bundle_before_it_unpacks_anything(self):
        self.broker.max_bundle_bytes = 4
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "x",
                    "target": "main",
                    "baseRevision": "0" * 40,
                    "bundleBase64": base64.b64encode(b"much too long").decode(),
                }
            )
        self.assertEqual(caught.exception.fields.get("code"), "BUNDLE_TOO_LARGE")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_publish_refuses_to_write_to_the_branch_it_was_cloned_from(self):
        """The three ancestry checks cannot catch this one.

        `clone` leaves the working copy on the shared branch, so committing
        without cutting a branch first produces a perfectly valid fast-forward
        of it — which every check below passes and which puts the agent's
        revisions on trunk.
        """
        work, answer = self.clone_locally()
        self.commit_in(work, "README.md", "straight to main\n", "no branch")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "main",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "main", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "TARGET_IS_BRANCH")
        self.assertEqual(self.remote_tip("main"), answer["revision"])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_publish_refuses_the_default_branch_whatever_target_says(self):
        """Review finding: the branch/target comparison was bypassable.

        `branch` and `target` are both the caller's fields. Naming any other
        existing branch as `target` skipped the comparison, set `existing_head`
        so the base check was skipped too, and left two ancestry checks that a
        fast-forward of the shared branch satisfies. The broker now asks the
        remote which branch is its default and refuses that one outright.
        """
        git(self.seed, "checkout", "--quiet", "-b", "release")
        git(self.seed, "push", "--quiet", "origin", "release")
        git(self.seed, "checkout", "--quiet", "main")
        work, answer = self.clone_locally()
        self.commit_in(work, "README.md", "straight to main\n", "no branch")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "main",
                    "target": "release",
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "main", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "PROTECTED_BRANCH")
        self.assertEqual(self.remote_tip("main"), answer["revision"])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_publish_refuses_the_configured_base_branch(self):
        # Run branches (run/**) are protected from direct publication unconditionally (#1498),
        # as are configured base branches (CREDENTIAL_PROXY_BASE_BRANCH or GITOPS_BASE_BRANCH).
        git(self.seed, "checkout", "--quiet", "-b", "run/test-cluster/fix-task")
        git(self.seed, "push", "--quiet", "origin", "run/test-cluster/fix-task")
        git(self.seed, "checkout", "--quiet", "main")
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "run/test-cluster/fix-task")
        self.commit_in(work, "README.md", "direct to run branch\n", "bypass")
        # Refused unconditionally without any env override
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "run/test-cluster/fix-task",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "run/test-cluster/fix-task", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "PROTECTED_BRANCH")

        broker_with_env = vcs_broker.VcsBroker(
            self.scratch, git_runner=self.broker._git_runner, base_branch="custom-broker-base"
        )
        broker_with_env.registry.hosts["local.test"] = self.forge
        self.assertEqual(broker_with_env.base_branch, "custom-broker-base")
        git(work, "checkout", "--quiet", "-b", "custom-broker-base")
        self.commit_in(work, "README.md", "direct to custom base\n", "bypass-custom")
        with self.assertRaises(WorkspaceError) as caught:
            broker_with_env.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "custom-broker-base",
                    "target": "main",
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "custom-broker-base", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "PROTECTED_BRANCH")

        # Refusal via environment variable override (including refs/heads/ prefix normalization)
        git(work, "checkout", "--quiet", "-b", "env-broker-base")
        self.commit_in(work, "README.md", "direct to env base\n", "bypass-env")
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_BASE_BRANCH": "refs/heads/env-broker-base"}):
            self.assertEqual(self.broker.base_branch, "")
            with self.assertRaises(WorkspaceError) as caught:
                self.broker.publish(
                    {
                        "repository": "local.test/acme/infra",
                        "branch": "env-broker-base",
                        "target": "main",
                        "baseRevision": answer["revision"],
                        "bundleBase64": self.bundle_of(work, "env-broker-base", answer["revision"]),
                    }
                )
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.fields.get("code"), "PROTECTED_BRANCH")

    def test_publish_refuses_hardcoded_protected_branches_and_ref_prefixes(self):
        # VcsBroker.publish refuses main, master, production and refs/heads/ prefixes (#1498, Thread 12)
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "feature")
        self.commit_in(work, "README.md", "direct to protected\n", "bypass")
        bundle_b64 = self.bundle_of(work, "feature", answer["revision"])
        for branch_name in ("master", "production", "refs/heads/main", "refs/heads/master"):
            with self.subTest(branch=branch_name):
                with self.assertRaises(WorkspaceError) as caught:
                    self.broker.publish(
                        {
                            "repository": "local.test/acme/infra",
                            "branch": branch_name,
                            "target": "main",
                            "baseRevision": answer["revision"],
                            "bundleBase64": bundle_b64,
                        }
                    )
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(caught.exception.fields.get("code"), "PROTECTED_BRANCH")

    def test_publish_refuses_the_branch_the_client_says_it_cloned(self):
        # A non-default branch cloned and published under another target is
        # what the default-branch check cannot see; the client names the
        # cloned branch and the broker refuses it.
        git(self.seed, "checkout", "--quiet", "-b", "release")
        git(self.seed, "push", "--quiet", "origin", "release")
        git(self.seed, "checkout", "--quiet", "main")
        work, answer = self.clone_locally()
        # Stand on the non-default branch the remote has, as a copy cloned
        # from it would; the copy has no remote of its own, by design.
        git(work, "checkout", "--quiet", "-b", "release")
        self.commit_in(work, "README.md", "onto release\n", "no branch")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "release",
                    "target": "main",
                    "clonedFrom": "release",
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "release", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "CLONED_BRANCH")

    def test_publish_still_reaches_a_non_default_branch_of_the_callers_own(self):
        # The guard is about the default branch only; a topic branch that
        # already exists on the remote is the second-publish case and must
        # keep working.
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "topic")
        first = self.commit_in(work, "a.txt", "a\n", "a")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "topic",
                "target": "main",
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(work, "topic", answer["revision"]),
            }
        )
        self.assertEqual(self.remote_tip("topic"), first)

    def test_advance_is_how_a_proposal_branch_gets_a_second_publish(self):
        # The refusal above is about a copy that wandered onto the branch it
        # came down on. A copy taken *of* a proposal branch in order to add to
        # it is the other situation, and it is the whole of how an open
        # proposal gets revised: there is nowhere else its revisions live.
        git(self.seed, "checkout", "--quiet", "-b", "topic")
        git(self.seed, "push", "--quiet", "origin", "topic")
        git(self.seed, "checkout", "--quiet", "main")
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "topic")
        second = self.commit_in(work, "b.txt", "b\n", "another round")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "topic",
                "target": "main",
                "clonedFrom": "topic",
                "advance": True,
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(work, "topic", answer["revision"]),
            }
        )
        self.assertEqual(self.remote_tip("topic"), second)

    def test_advance_is_refused_on_a_branch_carrying_no_open_proposal(self):
        """The waiver is for a proposal branch, and the forge is what says so.

        Without this, `advance` is a field that turns the refusal off: a worker
        that clones `release-1.2`, commits, and publishes `--target main
        --advance` fast-forwards a long-lived shared branch -- the live incident
        the refusal was added for -- and the client's own refusal text names the
        flag to any worker that meets one.
        """
        forge = ProposingLocalForge(self.forges, self.refreshed, open_sources=())
        self.broker.registry.hosts["local.test"] = forge
        git(self.seed, "checkout", "--quiet", "-b", "release-1.2")
        git(self.seed, "push", "--quiet", "origin", "release-1.2")
        git(self.seed, "checkout", "--quiet", "main")
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "release-1.2")
        self.commit_in(work, "b.txt", "b\n", "straight onto the release branch")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "release-1.2",
                    "target": "main",
                    "clonedFrom": "release-1.2",
                    "advance": True,
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "release-1.2", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.fields.get("code"), "CLONED_BRANCH")
        self.assertEqual(forge.listed[0]["source"], "release-1.2")
        self.assertEqual(self.remote_tip("release-1.2"), self.origin_head)

    def test_advance_is_honoured_when_the_forge_finds_the_proposal(self):
        forge = ProposingLocalForge(
            self.forges, self.refreshed, open_sources={"topic"}
        )
        self.broker.registry.hosts["local.test"] = forge
        git(self.seed, "checkout", "--quiet", "-b", "topic")
        git(self.seed, "push", "--quiet", "origin", "topic")
        git(self.seed, "checkout", "--quiet", "main")
        work, answer = self.clone_locally()
        git(work, "checkout", "--quiet", "-b", "topic")
        second = self.commit_in(work, "b.txt", "b\n", "another round")
        self.broker.publish(
            {
                "repository": "local.test/acme/infra",
                "branch": "topic",
                "target": "main",
                "clonedFrom": "topic",
                "advance": True,
                "baseRevision": answer["revision"],
                "bundleBase64": self.bundle_of(work, "topic", answer["revision"]),
            }
        )
        self.assertEqual(self.remote_tip("topic"), second)

    def test_advance_does_not_reach_the_default_branch(self):
        # Everything else still applies to it. The default-branch refusal is
        # the one that does not come from the request, so it is the one worth
        # proving the opt-in cannot talk its way past.
        work, answer = self.clone_locally()
        self.commit_in(work, "c.txt", "c\n", "onto main")
        with self.assertRaises(WorkspaceError) as caught:
            self.broker.publish(
                {
                    "repository": "local.test/acme/infra",
                    "branch": "main",
                    "target": "release",
                    "clonedFrom": "main",
                    "advance": True,
                    "baseRevision": answer["revision"],
                    "bundleBase64": self.bundle_of(work, "main", answer["revision"]),
                }
            )
        self.assertEqual(caught.exception.fields.get("code"), "PROTECTED_BRANCH")

    def test_scratch_names_come_from_a_counter_not_from_the_caller(self):
        first = self.broker._scratch("clone")
        second = self.broker._scratch("clone")
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertEqual(path.parent, self.scratch)
            self.assertTrue(path.name.startswith("clone-"))


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


class CapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = VcsBroker(Path(self.tmp.name) / "scratch", git_runner=git_runner)

    def test_a_served_forge_lists_every_verb(self):
        answer = self.broker.capabilities({"repository": "acme/infra"})
        self.assertEqual(answer["forge"], self.broker.registry.default.name)
        self.assertEqual(answer["proposalNoun"], "pull request")
        self.assertEqual(
            sorted(answer["verbs"]), sorted({*BROKER_VERBS, *COLLABORATION_VERBS})
        )
        self.assertEqual(answer["missing"], [])

    def test_gitlab_answers_with_its_own_noun_and_its_gaps(self):
        answer = self.broker.capabilities({"repository": "https://gitlab.com/a/b"})
        self.assertEqual(answer["forge"], "gitlab")
        self.assertEqual(answer["proposalNoun"], "merge request")
        self.assertEqual(answer["verbs"], [])
        self.assertTrue(answer["missing"])

    def test_an_unknown_host_answers_rather_than_raising(self):
        # Discovery is the one verb that must not fail on an unserved host: a
        # caller asking "can you do this" deserves "no, because", not a 501.
        answer = self.broker.capabilities({"repository": "https://git.example/a/b"})
        self.assertIsNone(answer["forge"])
        self.assertEqual(answer["verbs"], [])
        self.assertIn("not a forge this install serves", answer["missing"][0])

    def test_capabilities_spends_no_credential(self):
        minted = []
        broker = VcsBroker(
            Path(self.tmp.name) / "s2",
            git_runner=git_runner,
            refresh=lambda provider, repo: minted.append((provider, repo)),
        )
        broker.capabilities({"repository": "acme/infra"})
        self.assertEqual(minted, [])


# ---------------------------------------------------------------------------
# the collaboration verbs
# ---------------------------------------------------------------------------


class CollaborationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.minted: list[str] = []

    def broker(self, *answers) -> tuple[VcsBroker, Recorder]:
        recorder = Recorder(list(answers))
        return (
            VcsBroker(
                Path(self.tmp.name) / "scratch",
                git_runner=git_runner,
                cli_runner=recorder,
                refresh=lambda provider, repo: self.minted.append((provider, repo)),
            ),
            recorder,
        )

    def test_only_the_api_subcommand_is_ever_invoked(self):
        # A forge CLI's higher-level subcommands infer a repository from a
        # `.git/config` found above the cwd, which is the file this design
        # exists to keep away from the credentialed process.
        broker, recorder = self.broker([], [], {"number": 1}, {"number": 1})
        broker.proposal_list({"repository": "acme/infra"})
        broker.issue_list({"repository": "acme/infra"})
        broker.proposal_view({"repository": "acme/infra", "number": 1})
        broker.issue_view({"repository": "acme/infra", "number": 1})
        for argv in recorder.calls:
            self.assertEqual(argv[1], "api")

    def test_a_proposal_is_translated_into_three_states(self):
        merged = {
            "number": 7,
            "title": "Bump replicas",
            "state": "closed",
            "merged_at": "2026-08-01T00:00:00Z",
            "user": {"login": "kube-agents-bot[bot]"},
            "head": {"ref": "fix/replicas"},
            "base": {"ref": "main"},
            "html_url": "https://github.com/acme/infra/pull/7",
        }
        broker, _ = self.broker(merged)
        answer = broker.proposal_view({"repository": "acme/infra", "number": 7})
        proposal = answer["proposal"]
        self.assertEqual(proposal["state"], "merged")
        self.assertEqual(proposal["source"], "fix/replicas")
        self.assertEqual(proposal["target"], "main")
        # `[bot]` comes off here, not at the caller: `forge.py` records what
        # comparing an unnormalised login costs.
        self.assertEqual(proposal["author"], "kube-agents-bot")
        self.assertEqual(answer["forge"], "github")
        self.assertEqual(answer["repo"], "acme/infra")

    def test_a_proposal_says_where_its_source_branch_lives_and_what_is_on_it(self):
        # `source` is a branch name and nothing else. A proposal opened from a
        # fork carries the bare name, so a caller deciding "is this mine" on
        # the name alone accepts any fork's branch spelled the same way -- and
        # the caller that does this then amends by pushing that name to *this*
        # repository, creating a branch somebody else chose the name of.
        broker, _ = self.broker(
            {
                "number": 7,
                "state": "open",
                "head": {
                    "ref": "platform-agent/bump",
                    "sha": "c0ffee1",
                    "repo": {"full_name": "acme/infra"},
                },
                "base": {"ref": "main"},
                "labels": [{"name": "agent:ignore"}, {"name": "kind/bug"}],
            }
        )
        proposal = broker.proposal_view({"repository": "acme/infra", "number": 7})[
            "proposal"
        ]
        self.assertEqual(proposal["source"], "platform-agent/bump")
        self.assertEqual(proposal["sourceRepo"], "acme/infra")
        self.assertEqual(proposal["sourceRevision"], "c0ffee1")
        self.assertEqual(proposal["labels"], ["agent:ignore", "kind/bug"])

    def test_a_deleted_fork_leaves_the_source_repository_unnamed(self):
        # Not this repository, and not the fork either -- the forge has stopped
        # saying. Answering `""` is what lets a caller fail closed; answering
        # the repository being read would be a claim the forge did not make.
        broker, _ = self.broker(
            {
                "number": 8,
                "state": "open",
                "head": {"ref": "platform-agent/bump", "sha": "c0ffee1", "repo": None},
                "base": {"ref": "main"},
            }
        )
        proposal = broker.proposal_view({"repository": "acme/infra", "number": 8})[
            "proposal"
        ]
        self.assertEqual(proposal["sourceRepo"], "")
        self.assertEqual(proposal["source"], "platform-agent/bump")

    def test_a_comment_carries_an_identity_unique_across_endpoints(self):
        # The numeric id is unique only within the endpoint that issued it, so
        # a conversation comment and a review comment on one proposal can share
        # it. A caller keying "already answered" on the number alone would let
        # an answer to either suppress the other.
        broker, _ = self.broker(
            {"number": 7, "state": "open"},
            [{"id": 111, "user": {"login": "alice"}, "body": "please rebase"}],
            [{"id": 111, "user": {"login": "bob"}, "body": "nit", "path": "a.py"}],
            [],
        )
        comments = broker.proposal_view(
            {"repository": "acme/infra", "number": 7, "comments": True}
        )["comments"]
        refs = {comment["ref"] for comment in comments}
        self.assertEqual(refs, {"issue-111", "review_comment-111"})
        # And the pair it is built from is still there, because that pair is
        # what `proposal-acknowledge` takes back.
        for comment in comments:
            self.assertEqual(comment["ref"], f"{comment['kind']}-{comment['id']}")

    def test_closed_and_merged_are_different_outcomes(self):
        # GitHub encodes the difference in a nullable date field; nowhere else
        # does, and a caller should not have to know that.
        for node, expected in (
            ({"state": "open"}, "open"),
            ({"state": "closed"}, "closed"),
            ({"state": "closed", "merged_at": "2026-01-01T00:00:00Z"}, "merged"),
        ):
            with self.subTest(node=node):
                broker, _ = self.broker(node)
                answer = broker.proposal_view(
                    {"repository": "acme/infra", "number": 1}
                )
                self.assertEqual(answer["proposal"]["state"], expected)

    def test_proposal_create_sends_the_branches_as_head_and_base(self):
        broker, recorder = self.broker({"number": 3, "state": "open"})
        broker.proposal_create(
            {
                "repository": "acme/infra",
                "source": "fix/replicas",
                "target": "main",
                "title": "  Bump replicas  ",
                "body": "why",
                "draft": True,
            }
        )
        argv = recorder.calls[-1]
        self.assertEqual(argv[2:5], ["--method", "POST", "repos/acme/infra/pulls"])
        # The body goes over stdin, not argv: prose a caller wrote must not end
        # up in a `CalledProcessError`, in `ps`, or in a log line written by
        # something that did not know it was handling it.
        self.assertNotIn("Bump replicas", " ".join(argv))
        self.assertEqual(
            recorder.body,
            {
                "title": "Bump replicas",
                "body": "why",
                "head": "fix/replicas",
                "base": "main",
                "draft": True,
            },
        )

    def test_proposal_create_validates_before_it_calls(self):
        broker, recorder = self.broker()
        for payload in (
            {"source": "-x", "target": "main", "title": "t"},
            {"source": "a", "target": "..", "title": "t"},
            {"source": "a", "target": "main", "title": "   "},
            {"source": "a", "target": "main"},
        ):
            with self.subTest(payload=payload), self.assertRaises(WorkspaceError):
                broker.proposal_create({"repository": "acme/infra", **payload})
        self.assertEqual(recorder.calls, [])

    def test_issue_list_drops_the_proposals_github_mixes_in(self):
        broker, recorder = self.broker(
            [
                {"number": 1, "title": "a bug"},
                {"number": 2, "title": "a PR", "pull_request": {"url": "..."}},
                {"number": 3, "title": "another bug", "labels": [{"name": "p1"}]},
            ]
        )
        answer = broker.issue_list(
            {"repository": "acme/infra", "state": "open", "labels": ["bug"]}
        )
        self.assertEqual([i["number"] for i in answer["issues"]], [1, 3])
        self.assertEqual(answer["count"], 2)
        self.assertFalse(answer["truncated"])
        self.assertEqual(answer["issues"][1]["labels"], ["p1"])
        self.assertIn("labels=bug", recorder.path)
        self.assertIn("state=open", recorder.path)

    def test_a_listing_says_when_it_is_a_page(self):
        broker, _ = self.broker([{"number": n} for n in range(3)])
        answer = broker.issue_list({"repository": "acme/infra", "limit": 3})
        self.assertTrue(answer["truncated"])

    def test_issue_view_refuses_a_proposal_number(self):
        broker, _ = self.broker({"number": 4, "pull_request": {"url": "..."}})
        with self.assertRaises(WorkspaceError) as caught:
            broker.issue_view({"repository": "acme/infra", "number": 4})
        self.assertIn("pull request", str(caught.exception))
        self.assertIn("proposal view", str(caught.exception))

    def test_a_proposals_comments_come_from_all_three_places_tagged_by_kind(self):
        # GitHub splits one conversation across the conversation tab, inline
        # review comments and review summaries; a caller reading fewer than
        # three ignores requests at random. Each carries where it came from,
        # and an empty-bodied review (an approval) is not an utterance.
        broker, recorder = self.broker(
            {"number": 9, "state": "open"},
            [{"id": 1, "user": {"login": "someone"}, "body": "looks good", "created_at": "2026-01-01T00:00:02Z"}],
            [{"id": 2, "user": {"login": "someone"}, "body": "off by one", "created_at": "2026-01-01T00:00:01Z", "path": "a.yaml", "line": 3}],
            [{"id": 3, "user": {"login": "someone"}, "body": "", "state": "APPROVED", "submitted_at": "2026-01-01T00:00:03Z"},
             {"id": 4, "user": {"login": "someone"}, "body": "summary", "submitted_at": "2026-01-01T00:00:00Z"}],
        )
        answer = broker.proposal_view(
            {"repository": "acme/infra", "number": 9, "comments": True}
        )
        self.assertEqual(
            [(c["kind"], c["body"]) for c in answer["comments"]],
            [("review", "summary"), ("review_comment", "off by one"), ("issue", "looks good")],
        )
        self.assertEqual(answer["comments"][1]["path"], "a.yaml")
        paths = [call[4] for call in recorder.calls[1:]]
        self.assertTrue(any("issues/9/comments" in p for p in paths))
        self.assertTrue(any("pulls/9/comments" in p for p in paths))
        self.assertTrue(any("pulls/9/reviews" in p for p in paths))

    def test_proposal_update_applies_labels_then_patches(self):
        broker, recorder = self.broker([{"name": "a"}], None, {"number": 9, "state": "open"})
        answer = broker.proposal_update(
            {"repository": "acme/infra", "number": 9, "title": "new", "labelsAdd": ["a"], "labelsRemove": ["b"]}
        )
        self.assertEqual(answer["proposal"]["number"], 9)
        # Labels land first so the PATCH's answer is the proposal as it now stands.
        methods = [call[3] for call in recorder.calls]
        self.assertEqual(methods, ["POST", "DELETE", "PATCH"])
        self.assertEqual(recorder.calls[1][4], "repos/acme/infra/issues/9/labels/b")
        self.assertEqual(recorder.calls[2][4], "repos/acme/infra/pulls/9")
        self.assertEqual(json.loads(recorder.stdin[2])["title"], "new")

    def test_issue_close_carries_a_neutral_reason(self):
        broker, recorder = self.broker({"number": 5, "state": "closed"})
        broker.issue_close({"repository": "acme/infra", "number": 5, "reason": "not-planned"})
        self.assertEqual(recorder.calls[-1][3], "PATCH")
        self.assertEqual(recorder.body, {"state": "closed", "state_reason": "not_planned"})
        with self.assertRaises(WorkspaceError):
            broker.issue_close({"repository": "acme/infra", "number": 5, "reason": "wontfix"})

    def test_proposal_commits_is_a_listing_of_commits(self):
        broker, _ = self.broker([{"sha": "a" * 40, "commit": {"message": "m", "committer": {"date": "2026-01-01T00:00:00Z"}}}])
        answer = broker.proposal_commits({"repository": "acme/infra", "number": 9})
        self.assertEqual(answer["count"], 1)
        self.assertEqual(answer["commits"][0]["sha"], "a" * 40)
        self.assertEqual(answer["commits"][0]["committed"], "2026-01-01T00:00:00Z")

    def test_acknowledge_reacts_on_comments_and_declines_reviews(self):
        broker, recorder = self.broker({"id": 1, "content": "eyes"})
        answer = broker.proposal_acknowledge(
            {"repository": "acme/infra", "number": 9, "comment": {"id": 44, "kind": "review_comment"}}
        )
        self.assertTrue(answer["acknowledged"])
        self.assertEqual(recorder.calls[-1][4], "repos/acme/infra/pulls/comments/44/reactions")
        answer = broker.proposal_acknowledge(
            {"repository": "acme/infra", "number": 9, "comment": {"id": 45, "kind": "review"}}
        )
        self.assertFalse(answer["acknowledged"])
        self.assertEqual(len(recorder.calls), 1)

    def test_label_ensure_creates_on_404_and_updates_otherwise(self):
        missing = subprocess.CompletedProcess(["gh"], 1, "", "gh: Not Found (HTTP 404)")
        broker, recorder = self.broker(missing, {"name": "x", "color": "fbca04"})
        answer = broker.label_ensure({"repository": "acme/infra", "name": "x", "color": "#fbca04"})
        self.assertEqual([c[3] for c in recorder.calls], ["GET", "POST"])
        self.assertEqual(answer["label"]["name"], "x")
        self.assertEqual(recorder.body["color"], "fbca04")
        broker, recorder = self.broker({"name": "x"}, {"name": "x", "color": "000000"})
        broker.label_ensure({"repository": "acme/infra", "name": "x", "color": "000000"})
        self.assertEqual([c[3] for c in recorder.calls], ["GET", "PATCH"])

    def test_identity_reads_the_login_from_the_cli_and_the_permission_from_the_api(self):
        status = subprocess.CompletedProcess(
            ["gh"], 0, "", "github.com\n  ✓ Logged in to github.com account kube-agents[bot] (keyring)\n"
        )
        broker, recorder = self.broker(status, {"permission": "write"})
        answer = broker.identity({"repository": "acme/infra"})
        self.assertEqual(answer["identity"]["login"], "kube-agents[bot]")
        # The credential's own standing is not asked of the permission endpoint
        # (an App is not a collaborator there): unknown, and one call made.
        self.assertIsNone(answer["identity"]["canWrite"])
        self.assertEqual(recorder.calls[0][1:3], ["auth", "status"])
        self.assertEqual(len(recorder.calls), 1)
        broker, recorder = self.broker(status, {"permission": "write"})
        answer = broker.identity({"repository": "acme/infra", "login": "kube-agents[bot]"})
        self.assertTrue(answer["identity"]["canWrite"])
        self.assertIn("collaborators/kube-agents%5Bbot%5D/permission", recorder.calls[1][4])
        # A login nobody knows is a definitive no; a broker fault is unknown.
        gone = subprocess.CompletedProcess(["gh"], 1, "", "gh: Not Found (HTTP 404)")
        broker, _ = self.broker(status, gone)
        self.assertFalse(broker.identity({"repository": "acme/infra", "login": "stranger"})["identity"]["canWrite"])
        broken = subprocess.CompletedProcess(["gh"], 1, "", "connect: timeout")
        broker, _ = self.broker(status, broken)
        self.assertIsNone(broker.identity({"repository": "acme/infra", "login": "stranger"})["identity"]["canWrite"])

    def test_proposal_list_asks_for_one_branch_rather_than_filtering_a_page(self):
        broker, recorder = self.broker([])
        broker.proposal_list({"repository": "acme/infra", "source": "platform-agent/fix"})
        asked = recorder.calls[-1][4]
        self.assertIn("repos/acme/infra/pulls", asked)
        # Owner-qualified, so a fork carrying the same branch name cannot
        # answer for this repository's proposal.
        self.assertIn("head=acme%3Aplatform-agent%2Ffix", asked)

    def test_issue_list_with_a_query_goes_through_search(self):
        broker, recorder = self.broker({"items": [{"number": 1, "title": "t", "state": "open", "user": {"login": "u"}}]})
        answer = broker.issue_list({"repository": "acme/infra", "query": "drift", "labels": ["kind/bug"]})
        self.assertEqual(recorder.calls[-1][4].split("?")[0], "search/issues")
        self.assertIn("repo%3Aacme%2Finfra", recorder.calls[-1][4])
        self.assertIn("is%3Aissue", recorder.calls[-1][4])
        self.assertEqual(answer["count"], 1)

    def test_issue_list_excludes_labels_at_the_forge_not_on_the_page(self):
        # No query text, only an exclusion: it still has to leave the plain
        # listing endpoint, because that endpoint can say which labels an issue
        # must carry but not which it must not. A poller watching a queue for
        # unclaimed work asks exactly this and nothing else.
        broker, recorder = self.broker({"items": []})
        broker.issue_list(
            {
                "repository": "acme/infra",
                "labels": ["kind/bug"],
                "excludeLabels": ["status:in-progress", "agent:ignore"],
            }
        )
        asked = urllib.parse.unquote_plus(recorder.calls[-1][4])
        self.assertTrue(asked.startswith("search/issues"))
        self.assertIn('label:"kind/bug"', asked)
        self.assertIn('-label:"status:in-progress"', asked)
        self.assertIn('-label:"agent:ignore"', asked)

    def test_the_search_path_orders_its_page_the_way_the_listing_does(self):
        # Left alone this endpoint answers in relevance order, which is not an
        # order a caller can predict and not the one the plain listing uses --
        # so the same verb would hand back differently ordered pages depending
        # on whether a filter was present, and `truncated` would mean a
        # different thing in each.
        broker, recorder = self.broker({"items": []})
        broker.issue_list({"repository": "acme/infra", "query": "drift"})
        asked = recorder.calls[-1][4]
        self.assertIn("sort=created", asked)
        self.assertIn("order=desc", asked)

    def test_a_diff_is_asked_for_by_media_type_and_returned_raw(self):
        broker, recorder = self.broker({"number": 9}, "diff --git a/x b/x\n")
        answer = broker.proposal_view(
            {"repository": "acme/infra", "number": 9, "diff": True}
        )
        self.assertTrue(answer["diff"].startswith("diff --git"))
        self.assertIn("Accept: application/vnd.github.v3.diff", recorder.calls[-1])

    def test_the_forge_s_own_reason_reaches_the_caller(self):
        failed = subprocess.CompletedProcess(
            ["gh"], 1, "", "gh: Validation Failed (HTTP 422)\nno commits between\n"
        )
        broker, _ = self.broker(failed)
        with self.assertRaises(WorkspaceError) as caught:
            broker.proposal_create(
                {
                    "repository": "acme/infra",
                    "source": "a",
                    "target": "main",
                    "title": "t",
                }
            )
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.fields.get("code"), "FORGE_REJECTED")
        detail = caught.exception.fields.get("detail")
        self.assertIn("Validation Failed", detail)
        # Review finding: the line that names the reason used to be dropped.
        self.assertIn("no commits between", detail)

    def test_the_reason_in_the_api_s_json_body_reaches_the_caller(self):
        # `gh api` prints the API's JSON body on stdout when the call is
        # refused; the field the guidance tells the agent to fix is in there.
        failed = subprocess.CompletedProcess(
            ["gh"], 1,
            '{"message": "Validation Failed", "errors": [{"resource": "PullRequest", '
            '"code": "custom", "message": "A pull request already exists for acme:fix."}]}',
            "gh: Validation Failed (HTTP 422)\n",
        )
        broker, _ = self.broker(failed)
        with self.assertRaises(WorkspaceError) as caught:
            broker.proposal_create(
                {"repository": "acme/infra", "source": "fix", "target": "main", "title": "t"}
            )
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("A pull request already exists for acme:fix.", caught.exception.fields.get("detail"))

    def test_failures_that_want_different_actions_are_told_apart(self):
        # A missing scope and a throttle are both HTTP 403 from GitHub, and the
        # agent should wait out one and stop on the other. If this test ever
        # collapses to one code, the caller has lost the ability to choose.
        cases = [
            ("gh: Bad credentials (HTTP 401)", 401, "FORGE_UNAUTHENTICATED"),
            ("gh: Resource not accessible (HTTP 403)", 403, "FORGE_FORBIDDEN"),
            ("gh: API rate limit exceeded (HTTP 403)", 429, "FORGE_RATE_LIMITED"),
            ("gh: secondary rate limit (HTTP 429)", 429, "FORGE_RATE_LIMITED"),
            ("gh: Not Found (HTTP 404)", 404, "FORGE_NOT_FOUND"),
            ("gh: Conflict (HTTP 409)", 409, "FORGE_CONFLICT"),
            ("gh: Server Error (HTTP 500)", 503, "FORGE_UNAVAILABLE"),
            ("gh: Bad Gateway (HTTP 502)", 503, "FORGE_UNAVAILABLE"),
            ("dial tcp: no such host", 502, "FORGE_CALL_FAILED"),
        ]
        for output, status, code in cases:
            with self.subTest(output=output):
                broker, _ = self.broker(
                    subprocess.CompletedProcess(["gh"], 1, "", output)
                )
                with self.assertRaises(WorkspaceError) as caught:
                    broker.issue_list({"repository": "acme/infra"})
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.fields.get("code"), code)
                self.assertEqual(caught.exception.fields.get("detail"), output)

    def test_a_non_json_answer_is_a_forge_failure_not_a_traceback(self):
        broker, _ = self.broker(subprocess.CompletedProcess(["gh"], 0, "<html>", ""))
        with self.assertRaises(WorkspaceError) as caught:
            broker.issue_list({"repository": "acme/infra"})
        self.assertEqual(caught.exception.fields.get("code"), "FORGE_CALL_FAILED")

    def test_every_credentialed_verb_refreshes_first(self):
        broker, _ = self.broker([], {"number": 1}, {"number": 1})
        broker.issue_list({"repository": "acme/infra"})
        broker.issue_comment({"repository": "acme/infra", "number": 1, "body": "hi"})
        broker.proposal_comment({"repository": "acme/infra", "number": 1, "body": "hi"})
        self.assertEqual(self.minted, [("github", "acme/infra")] * 3)

    def test_a_verb_that_calls_several_times_refreshes_once(self):
        # A proposal's comments are three endpoints; the credential is made
        # current once for the request, not once per call.
        broker, recorder = self.broker({"number": 9}, [], [], [])
        broker.proposal_view(
            {"repository": "acme/infra", "number": 9, "comments": True}
        )
        self.assertEqual(len(recorder.calls), 4)
        self.assertEqual(self.minted, [("github", "acme/infra")])

    def test_an_unserved_forge_refuses_the_collaboration_verbs_by_name(self):
        broker, recorder = self.broker()
        with self.assertRaises(ForgeUnsupported) as caught:
            broker.issue_list({"repository": "https://gitlab.com/acme/infra"})
        self.assertEqual(caught.exception.status, 501)
        self.assertIn("gitlab", str(caught.exception))
        # And nothing was constructed for it on the way to the refusal.
        self.assertEqual(recorder.calls, [])
        self.assertEqual(self.minted, [])


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


class RouteTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = VcsBroker(Path(self.tmp.name) / "scratch", git_runner=git_runner)

    def test_the_table_covers_exactly_what_capabilities_advertises(self):
        # Two lists of verb names in one module is two lists that can disagree;
        # this is the test that notices.
        self.assertEqual(
            sorted(route_table(self.broker)),
            sorted({*BROKER_VERBS, *COLLABORATION_VERBS}),
        )

    def test_every_route_is_bound_to_the_broker(self):
        for verb, handler in route_table(self.broker).items():
            with self.subTest(verb=verb):
                self.assertEqual(getattr(handler, "__self__", None), self.broker)


if __name__ == "__main__":
    unittest.main()
