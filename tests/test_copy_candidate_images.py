"""Unit tests for scripts/release/copy_candidate_images.sh.

copy-candidate-images.yml runs it on a dispatch, to copy a commit's images
from GHCR into a second registry under the same `:<sha>` tags; the second class
holds that workflow's wiring. The registry is a
mock docker that keeps state: `imagetools create` records the destination at
the source's digest (or misbehaves, when a case asks it to), and `imagetools
inspect` answers from that state, so a test can read what the destination
holds after the run and not only which commands ran.
"""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tests.testing.common import (
    MOCK_DEFAULT_REGISTRY_PREFIX,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_CANDIDATE_RELEASE_IMAGES,
    MOCK_REQUIRED_RELEASE_IMAGES,
    commit_required_release_images,
    create_mock_ghcr_curl_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "copy_candidate_images.sh"
_DESTINATION_PREFIX = "us-docker.pkg.dev/mock-project/mock-candidates"

_MOCK_DOCKER = """#!{python}
import json, sys
STATE, LOG = {state!r}, {log!r}
args = sys.argv[1:]
with open(LOG, "a") as log:
    log.write(" ".join(args) + "\\n")
state = json.load(open(STATE))
images = state["images"]
if args[:3] == ["buildx", "imagetools", "inspect"]:
    if "--format" not in args or args[-1] not in images or args[-1] in state["unreadable"]:
        sys.exit(1)
    print(images[args[-1]])
    sys.exit(0)
if args[:3] == ["buildx", "imagetools", "create"]:
    destination, source = args[args.index("--tag") + 1], args[-1]
    name = source.rsplit("/", 1)[-1].split(":")[0]
    if name in state["create_fails"]:
        sys.exit(1)
    images[destination] = state["wrong_digest"] if name in state["create_corrupts"] else images[source]
    state["unreadable"] = [ref for ref in state["unreadable"] if ref != destination]
    json.dump(state, open(STATE, "w"))
    sys.exit(0)
sys.exit(1)
"""


def _digest(name):
    """A digest per image name, so a copy landing under the wrong name shows."""
    return "sha256:" + (name.encode().hex() * 64)[:64]


class CopyCandidateImagesTest(unittest.TestCase):
    _WRONG_DIGEST = "sha256:" + "f" * 64

    def setUp(self):
        self._tmp, self.repo, self.git = create_mock_git_repo()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.bin_dir = create_minimal_tools_bin(root)
        self.state_path = root / "registry.json"
        self.log_path = root / "docker.log"
        self.images = MOCK_CANDIDATE_RELEASE_IMAGES
        self.sha = commit_required_release_images(self.repo, self.git, self.images)

    def _registry(self, held=None, create_fails=(), create_corrupts=(), source_missing=(), unreadable=()):
        """A registry holding every source image but `source_missing`, plus `held`.

        An `unreadable` reference answers no inspect until something is
        copied to it: a registry that could not be asked, not an absent image.
        """
        images = {
            f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{name}:{self.sha}": _digest(name)
            for name in self.images
            if name not in source_missing
        }
        images.update(held or {})
        self.state_path.write_text(
            json.dumps(
                {
                    "images": images,
                    "create_fails": list(create_fails),
                    "create_corrupts": list(create_corrupts),
                    "unreadable": list(unreadable),
                    "wrong_digest": self._WRONG_DIGEST,
                }
            )
        )
        self.log_path.write_text("")
        docker = self.bin_dir / "docker"
        docker.write_text(_MOCK_DOCKER.format(python=sys.executable, state=str(self.state_path), log=str(self.log_path)))
        docker.chmod(0o755)

    def _run(self, args=None, env=None):
        overrides = {"PATH": str(self.bin_dir), "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX}
        overrides.update(env or {})
        return subprocess.run(
            ["bash", str(_SCRIPT)] + (args if args is not None else [self.sha, _DESTINATION_PREFIX]),
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=self.repo,
        )

    def _held(self):
        return json.loads(self.state_path.read_text())["images"]

    def _creates(self):
        return [line for line in self.log_path.read_text().splitlines() if line.startswith("buildx imagetools create")]

    def _destination(self, name):
        return f"{_DESTINATION_PREFIX}/{name}:{self.sha}"

    def test_every_image_the_commit_lists_lands_at_the_sources_digest(self):
        """The commit's own list, not this checkout's longer one; each a carbon
        copy from the source registry, under the same name and tag."""
        self._registry()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        held = self._held()
        for name in self.images:
            self.assertEqual(held.get(self._destination(name)), _digest(name), name)
        self.assertEqual(
            self._creates(),
            [
                f"buildx imagetools create --prefer-index=false --tag {self._destination(name)} "
                f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{name}:{self.sha}"
                for name in self.images
            ],
        )
        for name in set(MOCK_REQUIRED_RELEASE_IMAGES) - set(self.images):
            self.assertNotIn(self._destination(name), held, f"{name} is not in the commit's list")
        self.assertIn(f"All {len(self.images)} images", proc.stdout)

    def test_an_image_already_there_at_the_same_digest_is_not_copied_again(self):
        there = self.images[0]
        self._registry(held={self._destination(there): _digest(there)})
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._creates()), len(self.images) - 1)
        self.assertFalse([line for line in self._creates() if f"/{there}:" in line])
        self.assertIn(f"= {there}: already there", proc.stdout)

    def test_a_second_run_copies_nothing(self):
        self._registry()
        self.assertEqual(self._run().returncode, 0)
        self.log_path.write_text("")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._creates(), [])

    def test_an_image_there_at_another_digest_is_replaced_and_said_so(self):
        """The destination mirrors the source. A re-run of the publish on
        `main` rebuilds the same `:<sha>`, so the source's digest is the one
        that moved; the copy follows it and raises a warning naming both."""
        stale = self.images[1]
        self._registry(held={self._destination(stale): self._WRONG_DIGEST})
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        held = self._held()
        for name in self.images:
            self.assertEqual(held.get(self._destination(name)), _digest(name), name)
        self.assertEqual(len(self._creates()), len(self.images))
        self.assertIn(f"↻ {stale}: {_digest(stale)}, replacing {self._WRONG_DIGEST}", proc.stdout)
        self.assertIn(
            f"::warning title=Candidate image replaced::{self._destination(stale)} was {self._WRONG_DIGEST}",
            proc.stdout,
        )
        self.assertEqual(proc.stdout.count("::warning"), 1, "only the replaced image warns")

    def test_a_destination_that_cannot_be_read_is_copied_to_and_read_back(self):
        """A read that fails is not "already there" and not a reason to stop:
        the copy runs, and the read-back is what decides. Whatever was there
        ends at the source's digest."""
        blind = self.images[0]
        self._registry(
            held={self._destination(blind): self._WRONG_DIGEST},
            unreadable=(self._destination(blind),),
        )
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._held()[self._destination(blind)], _digest(blind))
        self.assertEqual(len(self._creates()), len(self.images))

    def test_each_way_one_image_fails_is_named_and_the_rest_still_copy(self):
        # The first image, so the ones after it show the loop went on.
        victim = self.images[0]
        for name, kwargs, says in (
            ("source missing", {"source_missing": (victim,)}, "is not published"),
            ("the copy fails", {"create_fails": (victim,)}, "the copy to"),
            ("the copy lands at another digest", {"create_corrupts": (victim,)}, "after the copy"),
        ):
            with self.subTest(name):
                self._registry(**kwargs)
                create_mock_ghcr_curl_binary(self.bin_dir, manifest_status=1)
                proc = self._run()
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn(f"✗ {victim}: ", proc.stderr)
                self.assertIn(says, proc.stderr)
                held = self._held()
                for other in self.images:
                    if other != victim:
                        self.assertEqual(held.get(self._destination(other)), _digest(other), other)
                self.assertIn(f"::error title=Candidate images not copied::1 of {len(self.images)}", proc.stdout)

    def test_a_commit_the_registry_has_no_image_of_is_nothing_to_copy(self):
        """A dispatch can name a commit that was never published (the GA
        tagger's stamped commit has no image at all). When the source answers
        404 for every image that is a notice and exit 0, with nothing copied;
        one image missing among others is still a failure (the case above)."""
        self._registry(source_missing=tuple(self.images))
        create_mock_ghcr_curl_binary(self.bin_dir, manifest_status=1)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._creates(), [])
        self.assertIn("::notice title=Nothing to copy::", proc.stdout)
        self.assertNotIn("::error", proc.stdout)

    def test_a_source_that_cannot_be_asked_is_a_failure_not_an_unpublished_commit(self):
        """Every read failing is what an outage looks like too. Only the
        registry's own "not found" for every image is "not published": an
        error answer, or no answer at all, fails the run and copies nothing."""
        for name, stage in (
            ("the registry answers 503", lambda: create_mock_ghcr_curl_binary(self.bin_dir, manifest_http_status=503)),
            ("no token to ask with", lambda: create_mock_ghcr_curl_binary(self.bin_dir, token_exit=7)),
            ("nothing to ask with", lambda: (self.bin_dir / "curl").unlink(missing_ok=True)),
        ):
            with self.subTest(name):
                self._registry(source_missing=tuple(self.images))
                stage()
                proc = self._run()
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertNotIn("::notice", proc.stdout)
                self.assertIn(f"::error title=Candidate images not copied::{len(self.images)} of {len(self.images)}", proc.stdout)
                self.assertIn("could not be read", proc.stderr)
                self.assertEqual(self._creates(), [])

    def test_arguments_are_required_and_the_sha_is_a_full_one(self):
        self._registry()
        for args, says in (
            ([], "are required"),
            ([self.sha], "are required"),
            ([self.sha[:12], _DESTINATION_PREFIX], "not a full commit SHA"),
            (["main", _DESTINATION_PREFIX], "not a full commit SHA"),
        ):
            with self.subTest(args=args):
                proc = self._run(args)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(says, proc.stderr)
                self.assertEqual(self._creates(), [])

    def test_the_destination_cannot_be_the_source(self):
        self._registry()
        for destination in (MOCK_DEFAULT_REGISTRY_PREFIX, MOCK_DEFAULT_REGISTRY_PREFIX + "/"):
            with self.subTest(destination=destination):
                proc = self._run([self.sha, destination])
                self.assertEqual(proc.returncode, 1)
                self.assertIn("already in", proc.stderr)
                self.assertEqual(self._creates(), [])

    def test_no_docker_is_said_not_read_as_missing_images(self):
        self._registry()
        (self.bin_dir / "docker").unlink()
        proc = self._run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("'docker buildx' is required", proc.stderr)


class CopyCandidateImagesWorkflowTest(unittest.TestCase):
    """copy-candidate-images.yml: when it runs, as what, and what it runs.

    The publish workflow pushes every commit it builds to both registries, so
    the copy is for the commits that workflow did not push there: a dispatch
    names one. Nothing starts it on its own.
    """

    _COPY_COMMAND = './scripts/release/copy_candidate_images.sh "$COMMIT_SHA" "$CANDIDATE_REGISTRY_PREFIX"'
    _SHA_EXPRESSION = "${{ inputs.commit_sha }}"

    @classmethod
    def setUpClass(cls):
        import yaml

        workflows = _REPO_ROOT / ".github" / "workflows"
        cls.doc = yaml.safe_load((workflows / "copy-candidate-images.yml").read_text())
        cls.on = cls.doc.get("on", cls.doc.get(True))
        cls.job = cls.doc["jobs"]["copy"]
        cls.publish = yaml.safe_load((workflows / "docker-publish-ghcr.yml").read_text())

    def test_only_a_dispatch_starts_it(self):
        """The publish workflow pushes to the candidate repository itself;
        nothing else starts a copy."""
        self.assertEqual(list(self.on), ["workflow_dispatch"])

    def test_a_dispatch_names_the_commit(self):
        """The way to copy a commit the publish workflow did not push, on any line."""
        commit = self.on["workflow_dispatch"]["inputs"]["commit_sha"]
        self.assertIs(commit["required"], True)
        self.assertEqual(self.job["env"]["COMMIT_SHA"], self._SHA_EXPRESSION)

    def test_it_runs_in_this_repository_alone(self):
        self.assertEqual(self.job["if"], "github.repository == 'gke-labs/kube-agents'")

    def test_each_commit_is_its_own_concurrency_group(self):
        """Keyed by commit and never cancelled, so one commit's copy cannot
        displace another's the way a shared group's single pending slot does."""
        concurrency = self.doc["concurrency"]
        self.assertIn(self._SHA_EXPRESSION, concurrency["group"])
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_it_copies_and_does_nothing_else(self):
        """The default branch's script, run on the commit named by the event
        through the environment; no package write, no build, no signing, and
        the published commit is fetched, not checked out."""
        self.assertEqual(self.doc["permissions"], {"contents": "read"})
        self.assertEqual(self.job["permissions"], {"contents": "read", "id-token": "write"})
        self.assertIsInstance(self.job.get("timeout-minutes"), int)
        self.assertIsNone(self.job.get("continue-on-error"))
        self.assertRegex(self.job["env"]["CANDIDATE_REGISTRY_PREFIX"], r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+$")
        steps = self.job["steps"]
        self.assertEqual(
            [str(step["uses"]).split("@")[0] for step in steps if "uses" in step],
            ["actions/checkout", "docker/setup-buildx-action", "google-github-actions/auth", "docker/login-action"],
        )
        # The scripts are the default branch's on every path, a dispatch from
        # another ref included (which still runs that ref's workflow file).
        self.assertEqual(
            steps[0]["with"],
            {"ref": "${{ github.event.repository.default_branch }}", "persist-credentials": False},
        )
        fetch, copy = [step["run"] for step in steps if "run" in step]
        self.assertEqual(copy, self._COPY_COMMAND)
        fetch_lines = [line.strip() for line in fetch.splitlines() if line.strip()]
        self.assertEqual(fetch_lines[-1], 'git fetch --no-tags --depth=1 origin "$COMMIT_SHA"')
        # A dispatch's input is free text and the fetch comes before the
        # script's own check, so the shape is checked first, here.
        self.assertEqual(fetch_lines[0], 'if ! [[ "$COMMIT_SHA" =~ ^[0-9a-f]{40}$ ]]; then')
        self.assertIn("exit 1", fetch_lines)
        for step in steps:
            self.assertNotIn("${{", str(step.get("run", "")), "event fields reach a run: step through env only")

    def test_the_publish_workflow_pushes_where_the_copy_copies_to_as_the_same_identity(self):
        """One destination and one Google identity, spelled in two files: a
        typo in the publish workflow's copy would fail every publish. Its
        decide job signs in as that identity too, to ask the destination."""
        self.assertEqual(self.publish["env"]["CANDIDATE_REGISTRY_PREFIX"], self.job["env"]["CANDIDATE_REGISTRY_PREFIX"])
        auth_action = "google-github-actions/auth"
        (copy_auth,) = [step for step in self.job["steps"] if str(step.get("uses", "")).split("@")[0] == auth_action]
        publish_auths = [
            step
            for job in self.publish["jobs"].values()
            for step in job["steps"]
            if str(step.get("uses", "")).split("@")[0] == auth_action
        ]
        self.assertEqual(len(publish_auths), len(self.publish["jobs"]))
        for step in publish_auths:
            self.assertEqual(step["uses"], copy_auth["uses"])
            self.assertEqual(step["with"], copy_auth["with"])
        for name, job in self.publish["jobs"].items():
            with self.subTest(job=name):
                self.assertIsNone(job.get("continue-on-error"))
                self.assertNotIn("copy_candidate_images", json.dumps(job))


if __name__ == "__main__":
    unittest.main()
