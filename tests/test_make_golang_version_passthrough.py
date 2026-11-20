"""`make docker-build GOLANG_VERSION=<pin>` reaches each build whose Dockerfile declares the arg.

Where the public registries are not reachable, the Go builder image comes from a
mirror, and a mirrored copy is frozen at one patch. The site tells the reader to
pass a patch-pinned `GOLANG_VERSION` when the `go` directive moves past that
patch. The root Makefile passed `GOLANG_IMAGE` to the builds and dropped
`GOLANG_VERSION`, so the pin never reached `docker build` (#6452).

These are dry runs: what is asserted is the command that make would issue.
"""

import pathlib
import re
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import REPO_ROOT, run_make  # noqa: E402

MAKE_TIMEOUT_SECONDS = 60
# `REPO` defaults through a `$(shell gcloud config get core/project)`, which a
# dry run still evaluates; naming it keeps the test off gcloud and the network.
REGISTRY = "registry.example/kube-agents"
VERSION_VAR = "GOLANG_VERSION"
PIN = "1.27.1-alpine"
# The images of `docker-build` whose Dockerfile takes its Go builder tag from the arg.
IMAGES_WITH_THE_ARG = {
    f"{REGISTRY}/platform-agent:latest",
    f"{REGISTRY}/credential-proxy:latest",
    f"{REGISTRY}/hermes-bridge:latest",
    f"{REGISTRY}/kube-proxy:latest",
}
# What the official golang image exports under the same name: a bare version.
VERSION_OF_A_GOLANG_CONTAINER = "1.27.3"


def docker_builds(*args, extra_env=None):
    """The `docker build` commands of a dry run of `docker-build`, each as a list of words."""
    proc = run_make(
        ["-n", f"REPO={REGISTRY}", *args, "docker-build"],
        timeout=MAKE_TIMEOUT_SECONDS,
        drop_env=(VERSION_VAR,),
        extra_env=extra_env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"make -n docker-build {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr}")
    return [line.split() for line in proc.stdout.splitlines() if line.startswith("docker build")]


def dockerfile_of(words):
    return words[words.index("-f") + 1]


def image_of(words):
    return words[words.index("-t") + 1]


def declares_the_arg(dockerfile):
    text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
    return re.search(rf"^ARG {VERSION_VAR}(=|$)", text, re.MULTILINE) is not None


class GolangVersionPassthroughTest(unittest.TestCase):
    def test_the_pin_reaches_each_build_whose_dockerfile_declares_it(self):
        builds = docker_builds(f"{VERSION_VAR}={PIN}")
        declared = {image_of(words) for words in builds if declares_the_arg(dockerfile_of(words))}
        self.assertEqual(declared, IMAGES_WITH_THE_ARG)
        for words in builds:
            with self.subTest(dockerfile=dockerfile_of(words), image=image_of(words)):
                if declares_the_arg(dockerfile_of(words)):
                    self.assertIn(f"{VERSION_VAR}={PIN}", words)
                    self.assertEqual(words[words.index(f"{VERSION_VAR}={PIN}") - 1], "--build-arg")
                else:
                    # An arg that the Dockerfile does not declare makes the build warn.
                    self.assertNotIn(f"{VERSION_VAR}={PIN}", words)

    def test_a_build_with_no_pin_passes_no_version(self):
        for words in docker_builds():
            self.assertFalse([word for word in words if word.startswith(f"{VERSION_VAR}=")], " ".join(words))

    def test_the_variable_of_a_golang_container_is_not_a_pin(self):
        # Inside a container of the golang image the environment holds GOLANG_VERSION.
        for words in docker_builds(extra_env={VERSION_VAR: VERSION_OF_A_GOLANG_CONTAINER}):
            self.assertFalse([word for word in words if word.startswith(f"{VERSION_VAR}=")], " ".join(words))


if __name__ == "__main__":
    unittest.main()
