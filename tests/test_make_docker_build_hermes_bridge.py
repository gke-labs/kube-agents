"""`make docker-build` also builds the Hermes bridge image, from the agent image of the same run.

The operator takes the bridge sidecar's image from the agent image on the
`PlatformAgent` when that image has the name `platform-agent` and a tag: the
same registry path and tag, with the name `hermes-bridge` (`a2aBridgeImage` in
k8s-operator/internal/controller/platformagent_a2a_bridge.go). So a path that
builds a `platform-agent` image must also give that tag a `hermes-bridge`
image. `make docker-build` built the agent image and no bridge image (#6180).

These are dry runs: what is asserted is the command that make would issue.
"""

import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import run_make  # noqa: E402

MAKE_TIMEOUT_SECONDS = 60
# `REPO` defaults through a `$(shell gcloud config get core/project)`, which a
# dry run still evaluates; naming it keeps the test off gcloud and the network.
REGISTRY = "registry.example/kube-agents"
AGENT_IMAGE = f"{REGISTRY}/platform-agent:latest"
BRIDGE_IMAGE = f"{REGISTRY}/hermes-bridge:latest"
BRIDGE_DOCKERFILE = "a2a/Dockerfile.hermes-bridge"
BRIDGE_TARGET = "docker-build-hermes-bridge"
GOLANG_MIRROR = "registry.example/mirror/golang"


def docker_builds(*args):
    """The `docker build` commands of a dry run, each as a list of words, in order."""
    proc = run_make(["-n", f"REPO={REGISTRY}", *args], timeout=MAKE_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise AssertionError(f"make -n {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr}")
    return [line.split() for line in proc.stdout.splitlines() if line.startswith("docker build")]


def tagged(builds, image):
    """The positions of the builds that tag `image`."""
    return [i for i, words in enumerate(builds) if image in words]


class DockerBuildHermesBridgeTest(unittest.TestCase):
    def assert_bridge_follows_agent(self, builds):
        [agent] = tagged(builds, AGENT_IMAGE)
        [bridge] = tagged(builds, BRIDGE_IMAGE)
        self.assertLess(agent, bridge, "the bridge image is built FROM the agent image, so the agent build comes first")
        words = builds[bridge]
        self.assertIn(f"PLATFORM_AGENT_IMAGE={AGENT_IMAGE}", words)
        self.assertEqual(words[words.index("-f") + 1], BRIDGE_DOCKERFILE)
        # The deployment nodes are amd64; an arm64 host must not compile its own architecture in.
        self.assertEqual(words[words.index("--platform") + 1], "linux/amd64")
        # The Dockerfile copies go.mod from the a2a module root.
        self.assertEqual(words[-1], "a2a")

    def test_docker_build_builds_the_bridge_from_the_agent_image_of_the_same_run(self):
        self.assert_bridge_follows_agent(docker_builds("docker-build"))

    def test_the_bridge_target_alone_builds_the_agent_image_first(self):
        self.assert_bridge_follows_agent(docker_builds(BRIDGE_TARGET))

    def test_a_mirrored_golang_base_reaches_the_bridge_build(self):
        builds = docker_builds(BRIDGE_TARGET, f"GOLANG_IMAGE={GOLANG_MIRROR}")
        [bridge] = tagged(builds, BRIDGE_IMAGE)
        self.assertIn(f"GOLANG_IMAGE={GOLANG_MIRROR}", builds[bridge])


if __name__ == "__main__":
    unittest.main()
