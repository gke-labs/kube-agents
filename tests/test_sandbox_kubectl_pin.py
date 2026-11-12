"""The shell sandbox's kubectl pin stays at or above the floor its Dockerfile
states: 1.34, the first minor whose `get --raw` keeps the kubeconfig server's
path. The kube-proxy kubeconfigs name the cluster in that path
(`${KUBE_PROXY_URL}/clusters/<ctx>`), so on an older kubectl every paged
collection read reaches the proxy's default cluster and nothing fails.
`deploy/sandbox/smoke-test.sh` proves the behaviour against the installed
binary; this test keeps a pin bump from going back below the floor without
the image build running."""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "deploy/sandbox/Dockerfile"
WORKER_DOCKERFILE = REPO_ROOT / "a2a/Dockerfile.worker"
SMOKE_TEST = REPO_ROOT / "deploy/sandbox/smoke-test.sh"
VERSION_ARG_RE = re.compile(r"^ARG KUBECTL_VERSION=(?P<version>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$", re.MULTILINE)
SHA256_ARG_RE = re.compile(r"^ARG KUBECTL_SHA256_(?:AMD64|ARM64)=[0-9a-f]{64}$", re.MULTILINE)
# client-go v0.34.0: Request.RequestURI goes through AbsPath, which prepends
# the server URL's path; v0.33 and earlier overwrote it.
FLOOR_MINOR = 34
# The smoke test's expectation: the recorded request path carries the prefix.
SMOKE_EXPECTATION = "request path /clusters/seeded-a/api/v1/pods?limit=1"


class KubectlPin(unittest.TestCase):
    def test_pin_is_at_or_above_the_raw_path_floor(self):
        for df in (DOCKERFILE, WORKER_DOCKERFILE):
            with self.subTest(dockerfile=df.relative_to(REPO_ROOT)):
                match = VERSION_ARG_RE.search(df.read_text(encoding="utf-8"))
                self.assertIsNotNone(match, f"{df} declares no ARG KUBECTL_VERSION=<major>.<minor>.<patch>")
                self.assertEqual(int(match.group("version")), 1)
                self.assertGreaterEqual(
                    int(match.group("minor")),
                    FLOOR_MINOR,
                    f"kubectl 1.{match.group('minor')} drops the kubeconfig server's /clusters/<ctx> path on --raw; the floor is 1.{FLOOR_MINOR}",
                )

    def test_both_checksums_are_pinned(self):
        for df in (DOCKERFILE, WORKER_DOCKERFILE):
            with self.subTest(dockerfile=df.relative_to(REPO_ROOT)):
                self.assertEqual(len(SHA256_ARG_RE.findall(df.read_text(encoding="utf-8"))), 2)

    def test_smoke_test_proves_the_path_against_the_binary(self):
        self.assertIn(SMOKE_EXPECTATION, SMOKE_TEST.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
