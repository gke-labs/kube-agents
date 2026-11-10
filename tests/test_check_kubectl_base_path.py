"""deploy/sandbox/check_kubectl_base_path.py, which the sandbox image build runs
against the kubectl it installs (#4739).

The build is where it meets a real kubectl. Here it meets two stand-ins, one
that keeps the --server URL's path the way kubectl 1.34+ does on `get --raw`,
and one that drops it the way 1.32 and 1.33 do. That pins the check itself: it
passes the first, fails the second naming the path it saw, and fails a kubectl
that errors.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK = REPO / "deploy" / "sandbox" / "check_kubectl_base_path.py"

# A stand-in kubectl: reads --server and the --raw path from its argv, and GETs
# either the server URL plus the path (keep) or the server's origin plus the path
# (drop), printing the body as kubectl would.
FAKE = textwrap.dedent(
    """\
    #!{python}
    import sys, urllib.parse, urllib.request
    args = sys.argv[1:]
    server = next(a.split("=", 1)[1] for a in args if a.startswith("--server="))
    raw = args[args.index("--raw") + 1]
    mode = {mode!r}
    if mode == "fail":
        sys.stderr.write("error: the server could not find the requested resource\\n")
        sys.exit(1)
    u = urllib.parse.urlsplit(server)
    base = server if mode == "keep" else f"{{u.scheme}}://{{u.netloc}}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))  # loopback, never a proxy
    sys.stdout.write(opener.open(base + raw).read().decode())
    """
)


class CheckKubectlBasePathTest(unittest.TestCase):
    def _run(self, mode: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = pathlib.Path(tmp) / "kubectl"
            kubectl.write_text(FAKE.format(python=sys.executable, mode=mode), encoding="utf-8")
            kubectl.chmod(0o755)
            return subprocess.run(
                [sys.executable, "-I", str(CHECK), str(kubectl)],
                capture_output=True, text=True, timeout=60, check=False,
            )

    def test_a_kubectl_that_keeps_the_base_path_passes(self):
        result = self._run("keep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/clusters/probe/apis/metrics.k8s.io/v1beta1/nodes", result.stdout)

    def test_a_kubectl_that_drops_the_base_path_fails_naming_what_it_sent(self):
        result = self._run("drop")
        self.assertEqual(result.returncode, 1)
        self.assertIn("dropped the kubeconfig server's base path", result.stderr)
        self.assertIn("'/apis/metrics.k8s.io/v1beta1/nodes'", result.stderr)

    def test_a_kubectl_that_errors_fails(self):
        result = self._run("fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kubectl get --raw failed", result.stderr)

    def test_the_sandbox_image_runs_it_against_the_kubectl_it_installs(self):
        dockerfile = (REPO / "deploy" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY deploy/sandbox/check_kubectl_base_path.py /tmp/check_kubectl_base_path.py", dockerfile)
        self.assertIn("python3 -I /tmp/check_kubectl_base_path.py /usr/local/libexec/kubectl", dockerfile)
        version = next(line.split("=", 1)[1] for line in dockerfile.splitlines() if line.startswith("ARG KUBECTL_VERSION="))
        major, minor = (int(p) for p in version.split(".")[:2])
        self.assertGreaterEqual((major, minor), (1, 34), "kubectl before 1.34 drops the base path on get --raw")


if __name__ == "__main__":
    unittest.main()
