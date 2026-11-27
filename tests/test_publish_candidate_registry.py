"""docker-publish-ghcr.yml pushes each image to the candidate repository alone.

An environment and the release read a commit's images from the candidate
repository through their registry variables, and autopush starts when this
workflow completes. GHCR holds releases: nothing here pushes to it or moves
its `latest`, each image is signed on its candidate reference, and the decision
whether a release-branch commit already has images asks the candidate
repository.
"""

import pathlib
import re
import unittest

import yaml

from tests.testing.release import parse_required_release_images

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docker-publish-ghcr.yml"
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"
_BUILD_ACTION = "docker/build-push-action"
_LOGIN_ACTION = "docker/login-action"
_AUTH_ACTION = "google-github-actions/auth"
_SETUP_GCLOUD_ACTION = "google-github-actions/setup-gcloud"
_PUBLISH_JOBS = ("publish-operator", "publish-agents")
_DECIDE_JOB = "decide"
_DECIDE_SCRIPT = "./scripts/release/decide_image_publish.sh"
_GHCR_HOST = "ghcr.io"
_PACKAGES_PERMISSION = "packages"
_BRIDGE_BUILD_STEP_ID = "build-hermes-bridge"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"
# What google-github-actions/auth names the credentials file it writes into the workspace.
_AUTH_CREDENTIALS_FILE_PATTERN = "gha-creds-*.json"
_ARTIFACT_REGISTRY_PREFIX = re.compile(r"^([a-z0-9-]+-docker\.pkg\.dev)/[a-z0-9-]+/[a-z0-9-]+$")


def _uses(step):
    return str(step.get("uses", "")).split("@")[0]


class PublishToCandidateRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(_WORKFLOW.read_text())
        cls.jobs = {name: cls.doc["jobs"][name] for name in _PUBLISH_JOBS}
        cls.builds = [step for job in cls.jobs.values() for step in job["steps"] if _uses(step) == _BUILD_ACTION]

    def _tags(self, step):
        return [line.strip() for line in step["with"]["tags"].splitlines() if line.strip()]

    def test_the_candidate_repository_is_an_artifact_registry_repository_and_its_host_is_its_own(self):
        match = _ARTIFACT_REGISTRY_PREFIX.match(self.doc["env"]["CANDIDATE_REGISTRY_PREFIX"])
        self.assertIsNotNone(match)
        self.assertEqual(self.doc["env"]["CANDIDATE_REGISTRY_HOST"], match.group(1))

    def test_every_release_image_is_built_once_and_pushed_to_the_candidate_repository_alone(self):
        """The commit tag and no other: no push moves a `latest`."""
        names = []
        for step in self.builds:
            with self.subTest(step=step.get("name")):
                self.assertIs(step["with"]["push"], True)
                tags = self._tags(step)
                self.assertEqual(len(tags), 1, tags)
                (candidate_sha,) = tags
                name = re.fullmatch(
                    r"\$\{\{ env\.CANDIDATE_REGISTRY_PREFIX \}\}/([a-z0-9-]+):\$\{\{ github\.sha \}\}", candidate_sha
                )
                self.assertIsNotNone(name, candidate_sha)
                image = name.group(1)
                names.append(image)
        self.assertEqual(len(names), len(set(names)), "an image is built twice")
        self.assertEqual(sorted(names), sorted(parse_required_release_images(_COMMON_SH.read_text())))

    def test_only_a_push_starts_it_and_the_workflow_itself_holds_no_credential(self):
        """Its jobs mint a Google credential that writes the candidate
        repository, so a pull request must not start it, and the token
        permission stays on the jobs that are guarded to this repository."""
        on = self.doc.get("on", self.doc.get(True))
        self.assertEqual(list(on), ["push"])
        self.assertEqual(self.doc["permissions"], {"contents": "read"})

    def test_nothing_in_the_workflow_reaches_ghcr(self):
        """No step names GHCR and no job may write packages: a release
        publishes there."""
        self.assertNotIn(_PACKAGES_PERMISSION, self.doc.get("permissions", {}))
        for job_name, job in self.doc["jobs"].items():
            with self.subTest(job=job_name):
                self.assertNotIn(_PACKAGES_PERMISSION, job.get("permissions", {}))
                for step in job["steps"]:
                    rendered = yaml.safe_dump(step)
                    self.assertNotIn(_GHCR_HOST, rendered, step.get("name"))

    def test_each_image_is_signed_once_on_its_candidate_reference(self):
        """By digest, in the registry the image was pushed to."""
        signed = {}
        for job in self.jobs.values():
            for step in job["steps"]:
                for line in str(step.get("run", "")).splitlines():
                    match = re.fullmatch(r'cosign sign --yes "(.+)/([a-z0-9-]+)@\$([A-Z0-9_]+)"', line.strip())
                    if match:
                        signed.setdefault(match.group(2), []).append(match.group(1))
        self.assertEqual(sorted(signed), sorted(parse_required_release_images(_COMMON_SH.read_text())))
        for image, registries in signed.items():
            with self.subTest(image=image):
                self.assertEqual(registries, ["$CANDIDATE_REGISTRY_PREFIX"])

    def test_the_bridge_is_built_from_the_platform_image_this_job_pushed(self):
        bridge = next(step for step in self.builds if step.get("id") == _BRIDGE_BUILD_STEP_ID)
        self.assertEqual(
            bridge["with"]["build-args"].strip(),
            "PLATFORM_AGENT_IMAGE=${{ env.CANDIDATE_REGISTRY_PREFIX }}/platform-agent:${{ github.sha }}",
        )

    def test_the_decision_asks_the_candidate_repository_as_the_identity_that_writes_it(self):
        """A release-branch commit `main` already built has its images in the
        candidate repository, and only a Google identity can ask it."""
        decide = self.doc["jobs"][_DECIDE_JOB]
        steps = decide["steps"]
        run = next(i for i, step in enumerate(steps) if step.get("run") == _DECIDE_SCRIPT)
        before = [_uses(step) for step in steps[:run]]
        self.assertIn(_AUTH_ACTION, before)
        self.assertIn(_SETUP_GCLOUD_ACTION, before)
        self.assertLess(before.index(_AUTH_ACTION), before.index(_SETUP_GCLOUD_ACTION))
        decide_auth = next(step for step in steps if _uses(step) == _AUTH_ACTION)
        publish_auth = next(step for step in self.jobs[_PUBLISH_JOBS[0]]["steps"] if _uses(step) == _AUTH_ACTION)
        for key in ("workload_identity_provider", "service_account"):
            self.assertEqual(decide_auth["with"][key], publish_auth["with"][key])
        self.assertEqual(decide["permissions"], {"contents": "read", "id-token": "write"})
        self.assertIn("github.repository == 'gke-labs/kube-agents'", decide["if"])
        self.assertNotIn("env", steps[run])
        self.assertNotIn("REGISTRY_PREFIX", self.doc["env"])

    def test_each_job_is_logged_in_to_the_candidate_registry_before_its_first_build(self):
        for job_name, job in self.jobs.items():
            with self.subTest(job=job_name):
                steps = job["steps"]
                first_build = next(i for i, step in enumerate(steps) if _uses(step) == _BUILD_ACTION)
                before = steps[:first_build]
                logins = [step for step in before if _uses(step) == _LOGIN_ACTION]
                self.assertEqual([login["with"]["registry"] for login in logins], ["${{ env.CANDIDATE_REGISTRY_HOST }}"])
                auth = [step for step in before if _uses(step) == _AUTH_ACTION]
                self.assertEqual(len(auth), 1)
                self.assertEqual(auth[0]["with"]["token_format"], "access_token")
                self.assertLess(before.index(auth[0]), before.index(logins[0]))
                self.assertEqual(logins[0]["with"]["username"], "oauth2accesstoken")
                self.assertEqual(logins[0]["with"]["password"], f"${{{{ steps.{auth[0]['id']}.outputs.access_token }}}}")
                self.assertEqual(job["permissions"]["id-token"], "write")
                self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"])
                for step in steps:
                    self.assertIsNone(step.get("continue-on-error"), step.get("name"))

    def test_the_google_credentials_file_stays_out_of_every_build_context(self):
        """The auth step writes it into the workspace, which is the context of
        every build here that names no directory of its own; those builds read
        the root .dockerignore, having no ignore file beside their Dockerfile."""
        self.assertIn(_AUTH_CREDENTIALS_FILE_PATTERN, _DOCKERIGNORE.read_text().splitlines())
        root_context = [step for step in self.builds if step["with"].get("context", ".") in (".", "./")]
        self.assertTrue(root_context)
        for step in root_context:
            with self.subTest(step=step.get("name")):
                dockerfile = _REPO_ROOT / step["with"]["file"]
                self.assertFalse(dockerfile.with_name(dockerfile.name + ".dockerignore").exists())


if __name__ == "__main__":
    unittest.main()
