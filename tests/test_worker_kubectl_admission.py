"""The worker image's kubectl wrapper runs the kubectl admission on a session pod.

a2a/Dockerfile.worker copies deploy/sandbox/session-kubectl.sh to /usr/local/bin/kubectl. This test
takes that script, points its absolute paths at stubs, and runs it: with KUBE_PROXY_URL set
(a session pod under the cluster view) it must exec deploy/sandbox/kubectl-admit.py with the
session pod's settings; without it (an AgentProfile worker) it must exec the broker's
credential-proxy-exec, as before, and never the admission.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "a2a" / "Dockerfile.worker"
WRAPPER = "/usr/local/bin/kubectl"
ADMIT = "/usr/local/libexec/kubectl-admit"
GUARD = "/usr/local/libexec/shell-memory-guard"
COSTS = "/usr/local/share/kube-agents/kubectl-costs.json"
LIBEXEC = "/usr/local/libexec/"
BROKER_LIBEXEC = "/opt/credential-proxy/libexec/"
PROXY = "https://agent-kube-proxy.ns.svc.cluster.local:8443"
SETTINGS = ("KUBECTL_ADMIT_RUN_DIR", "KUBECTL_ADMIT_GUARD_PATH", "KUBECTL_ADMIT_ON_BUSY")

# Prints which stub ran, its arguments and the admission's settings, as one JSON line.
STUB = """#!{python}
import json, os, sys
print(json.dumps({{"ran": {name!r}, "argv": sys.argv[1:], "pid": os.getpid(),
                  "env": {{k: os.environ.get(k) for k in {settings!r}}}}}))
sys.exit(int(os.environ.get("STUB_RC", "0")))
"""


WRAPPER_SCRIPT = ROOT / "deploy" / "sandbox" / "session-kubectl.sh"
WRAPPER_COPY = "COPY --chmod=0755 deploy/sandbox/session-kubectl.sh /usr/local/bin/kubectl"


def wrapper_script() -> str:
    """The script the Dockerfile copies to /usr/local/bin/kubectl."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    if WRAPPER_COPY not in text:
        raise AssertionError(f"{DOCKERFILE} does not copy {WRAPPER_SCRIPT.name} to {WRAPPER}")
    return WRAPPER_SCRIPT.read_text(encoding="utf-8")


class WorkerWrapperTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        libexec, broker, home = self.root / "libexec", self.root / "broker", self.root / "home"
        for d in (libexec, broker, home / ".kube"):
            d.mkdir(parents=True)
        (home / ".kube" / "config").write_text("")  # registered: the wrapper skips the client
        for path, name in [(libexec / "kubectl-admit", "admit"), (libexec / "kubectl", "kubectl"),
                           (broker / "credential-proxy-exec", "credential-proxy-exec")]:
            path.write_text(STUB.format(python=sys.executable, name=name, settings=SETTINGS))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        registered = libexec / "kubectl-context-registered"
        registered.write_text("#!/bin/sh\nexit 0\n")
        registered.chmod(0o755)
        self.home, self.libexec = home, libexec
        script = wrapper_script().replace(LIBEXEC, f"{libexec}/").replace(BROKER_LIBEXEC, f"{broker}/")
        self.wrapper = self.root / "kubectl"
        self.wrapper.write_text(script)
        self.wrapper.chmod(0o755)
        # `python3 -I` in the script: the interpreter running this test, so the stub needs no PATH.
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "python3").symlink_to(sys.executable)
        self.path = f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    def run_wrapper(self, env: dict[str, str], *argv: str) -> dict:
        base = {"PATH": self.path, "HOME": str(self.home)}
        out = subprocess.run(["/bin/sh", str(self.wrapper), *argv], env={**base, **env},
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_the_kube_proxy_branch_runs_the_admission_with_the_session_pod_settings(self):
        for argv in (("get", "pods", "-A"), ("--context", "gke_p_l_c", "get", "ns")):
            with self.subTest(argv=argv):
                got = self.run_wrapper({"KUBE_PROXY_URL": PROXY, "CREDENTIAL_PROXY_URL": "http://broker"}, *argv)
                self.assertEqual(got["ran"], "admit")
                self.assertEqual(got["argv"], list(argv))
                self.assertEqual(got["env"], {"KUBECTL_ADMIT_RUN_DIR": "/tmp/kube-agents-session",
                                              # GUARD, with the stubs' directory for /usr/local/libexec
                                              "KUBECTL_ADMIT_GUARD_PATH": f"{self.libexec}/shell-memory-guard",
                                              "KUBECTL_ADMIT_ON_BUSY": "run"})

    def test_the_wrapper_exits_with_the_admissions_code_and_passes_stdin(self):
        # The admission's exit code is kubectl's, and steps after it read it here.
        base = {"PATH": self.path, "HOME": str(self.home), "KUBE_PROXY_URL": PROXY}
        for rc in (0, 1, 3, 75):
            with self.subTest(rc=rc):
                out = subprocess.run(["/bin/sh", str(self.wrapper), "apply", "-f", "-"],
                                     env={**base, "STUB_RC": str(rc)}, input="kind: x\n",
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(out.returncode, rc)
                self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1])["ran"], "admit")

    def test_the_admission_runs_in_the_wrappers_own_process(self):
        # exec'd, not a child: a signal sent only to the wrapper's pid (a backgrounded
        # port-forward or `logs -f`) reaches the admission, and through it kubectl.
        base = {"PATH": self.path, "HOME": str(self.home), "KUBE_PROXY_URL": PROXY}
        proc = subprocess.Popen(["/bin/sh", str(self.wrapper), "get", "ns"], env=base,
                                stdout=subprocess.PIPE, text=True)
        out, _ = proc.communicate(timeout=30)
        got = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(got["ran"], "admit")
        self.assertEqual(got["pid"], proc.pid)

    def test_without_a_kube_proxy_the_broker_runs_kubectl_and_the_admission_never_does(self):
        got = self.run_wrapper({"CREDENTIAL_PROXY_URL": "http://broker"}, "get", "pods", "-A")
        self.assertEqual(got["ran"], "credential-proxy-exec")
        self.assertEqual(got["argv"], ["kubectl", "get", "pods", "-A"])
        self.assertEqual(got["env"], dict.fromkeys(SETTINGS))

    def test_with_neither_proxy_kubectl_runs_directly(self):
        got = self.run_wrapper({}, "version", "--client")
        self.assertEqual(got["ran"], "kubectl")
        self.assertEqual(got["env"], dict.fromkeys(SETTINGS))

    def test_the_image_installs_the_admission_its_guard_and_table_at_the_sandbox_paths(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        sandbox = (ROOT / "deploy" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
        for source, target in [("deploy/sandbox/kubectl-admit.py", ADMIT),
                               ("deploy/sandbox/shell-memory-guard.py", GUARD),
                               ("deploy/sandbox/kubectl-costs.json", COSTS)]:
            with self.subTest(target=target):
                self.assertRegex(text, rf"(?m)^COPY --chmod=0[67]44 {re.escape(source)} {re.escape(target)}$")
                self.assertIn(f"{source} {target}", sandbox)  # the same path as the sandbox's
        # A library only: the worker adapter is PID 1, and nothing starts the guard.
        self.assertNotIn("shell-memory-guard &", text)
        self.assertNotRegex(text, r"(?m)^(ENTRYPOINT|CMD).*shell-memory-guard")
        # The admission's real kubectl is the one the image installs.
        admit_source = (ROOT / "deploy" / "sandbox" / "kubectl-admit.py").read_text(encoding="utf-8")
        self.assertIn('KUBECTL_NEXT = "/usr/local/libexec/kubectl"', admit_source)
        self.assertIn("-o /usr/local/libexec/kubectl ", text)

    def test_the_session_pod_writes_no_limit_file(self):
        # The limit comes from the cgroup (guard.memory_limit) only while nothing writes this.
        self.assertNotIn("/tmp/kube-agents-session/memory-limit-bytes", DOCKERFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
