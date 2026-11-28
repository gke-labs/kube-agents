"""docker-publish-ghcr.yml pushes each image to GHCR and to the candidate repository.

An environment or the release reads a commit's images from the candidate
repository when its registry variable names it, and autopush starts when this
workflow completes. So the one
build that pushes an image to GHCR pushes it there too, under the commit tag,
which also makes it one digest in both; the job is logged in to both before
the first build. The signatures stay on the GHCR references.
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
_PUBLISH_JOBS = ("publish-operator", "publish-agents")
_MAIN_ONLY = "github.ref == 'refs/heads/main'"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"
# What google-github-actions/auth names the credentials file it writes into the workspace.
_AUTH_CREDENTIALS_FILE_PATTERN = "gha-creds-*.json"
_ARTIFACT_REGISTRY_PREFIX = re.compile(r"^([a-z0-9-]+-docker\.pkg\.dev)/[a-z0-9-]+/[a-z0-9-]+$")


def _uses(step):
    return str(step.get("uses", "")).split("@")[0]


class PublishToBothRegistriesTest(unittest.TestCase):
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

    def test_every_release_image_is_built_once_and_pushed_to_both_under_the_commit_tag(self):
        """The commit tag in each registry; `latest`, on main alone, on GHCR
        alone: nothing reads a moving tag in the candidate repository."""
        names = []
        for step in self.builds:
            with self.subTest(step=step.get("name")):
                self.assertIs(step["with"]["push"], True)
                tags = self._tags(step)
                self.assertEqual(len(tags), 3, tags)
                ghcr_latest, ghcr_sha, candidate_sha = tags
                name = re.fullmatch(r"ghcr\.io/\$\{\{ env\.IMAGE_REPOSITORY \}\}/([a-z0-9-]+):\$\{\{ github\.sha \}\}", ghcr_sha)
                self.assertIsNotNone(name, ghcr_sha)
                image = name.group(1)
                names.append(image)
                self.assertEqual(candidate_sha, f"${{{{ env.CANDIDATE_REGISTRY_PREFIX }}}}/{image}:${{{{ github.sha }}}}")
                self.assertEqual(
                    ghcr_latest,
                    f"${{{{ {_MAIN_ONLY} && format('ghcr.io/{{0}}/{image}:latest', env.IMAGE_REPOSITORY) || '' }}}}",
                )
        self.assertEqual(len(names), len(set(names)), "an image is built twice")
        self.assertEqual(sorted(names), sorted(parse_required_release_images(_COMMON_SH.read_text())))

    def test_only_a_push_starts_it_and_the_workflow_itself_holds_no_credential(self):
        """Its jobs mint a Google credential that writes the candidate
        repository, so a pull request must not start it, and the token and
        package permissions stay on the jobs that are guarded to this
        repository."""
        on = self.doc.get("on", self.doc.get(True))
        self.assertEqual(list(on), ["push"])
        self.assertEqual(self.doc["permissions"], {"contents": "read"})

    def test_each_job_is_logged_in_to_both_registries_before_its_first_build(self):
        for job_name, job in self.jobs.items():
            with self.subTest(job=job_name):
                steps = job["steps"]
                first_build = next(i for i, step in enumerate(steps) if _uses(step) == _BUILD_ACTION)
                before = steps[:first_build]
                logins = [step for step in before if _uses(step) == _LOGIN_ACTION]
                self.assertEqual([login["with"]["registry"] for login in logins], ["ghcr.io", "${{ env.CANDIDATE_REGISTRY_HOST }}"])
                auth = [step for step in before if _uses(step) == _AUTH_ACTION]
                self.assertEqual(len(auth), 1)
                self.assertEqual(auth[0]["with"]["token_format"], "access_token")
                self.assertLess(before.index(auth[0]), before.index(logins[1]))
                self.assertEqual(logins[1]["with"]["username"], "oauth2accesstoken")
                self.assertEqual(logins[1]["with"]["password"], f"${{{{ steps.{auth[0]['id']}.outputs.access_token }}}}")
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

    def test_each_image_is_signed_once_on_its_ghcr_reference(self):
        """Signing the candidate copies is not part of the publish: a failure
        there would fail a run whose images are already pushed."""
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
                self.assertEqual(registries, ["ghcr.io/$IMAGE_REPOSITORY"])


if __name__ == "__main__":
    unittest.main()
