"""The worker image's kubectl wrapper runs the kubectl admission on a session pod.

a2a/Dockerfile.worker copies deploy/sandbox/session-kubectl.sh to /usr/local/bin/kubectl. This test
takes that script, points its absolute paths at stubs, and runs it: with KUBE_PROXY_URL set
(a session pod under the cluster view) it must exec deploy/sandbox/kubectl-admit.py with the
session pod's settings for an argv the admission may charge, and kubectl directly for one it never
charges (tests/test_session_kubectl_fast_path.py checks that split); without it (an AgentProfile
worker) it must exec the broker's credential-proxy-exec, as before, and never the admission. The shell
sandbox's own wrapper (deploy/sandbox/Dockerfile) must run the admission without those settings.
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
# What session-kubectl.sh's all_fast reads from the image and the host.
ALL_FAST_READS = ("/usr/local/share/kube-agents/kubectl-session-fastpath", "/proc/sys/kernel/osrelease",
                  "/sys/fs/cgroup/memory.max")
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
    # The env sets no KUBEAGENTS_KUBECTL_COST_TIER, so the wrapper's all_fast returns false at its
    # first check and these runs test the paths below it. The files all_fast reads (the fast-path
    # figures, /proc and /sys) point at stubs that do not exist, so the host's own never decide
    # either (tests/test_session_kubectl_fast_path.py tests all_fast itself).
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
        script = wrapper_script()
        for path in ALL_FAST_READS:
            self.assertIn(path, script)
            script = script.replace(path, str(self.root / "absent" / Path(path).name))
        script = script.replace(LIBEXEC, f"{libexec}/").replace(BROKER_LIBEXEC, f"{broker}/")
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

    def test_an_argv_the_admission_never_charges_runs_kubectl_directly(self):
        for argv in (("get", "pods", "-n", "x"), ("--context", "gke_p_l_c", "get", "pod/p1"), ("logs", "-f", "x"),
                     ("apply", "-f", "-")):
            with self.subTest(argv=argv):
                got = self.run_wrapper({"KUBE_PROXY_URL": PROXY, "CREDENTIAL_PROXY_URL": "http://broker"}, *argv)
                self.assertEqual(got["ran"], "kubectl")
                self.assertEqual(got["argv"], list(argv))
                self.assertEqual(got["env"], dict.fromkeys(SETTINGS))  # as before the admission

    def test_the_wrapper_exits_with_kubectls_code_and_passes_stdin_on_both_paths(self):
        # The admission's exit code is kubectl's, and the wrapper's is the admission's.
        base = {"PATH": self.path, "HOME": str(self.home), "KUBE_PROXY_URL": PROXY}
        for argv, ran in ((("apply", "-f", "-"), "kubectl"), (("get", "nodes", "-f", "-o", "json"), "kubectl"),
                          (("get", "nodes", "-o", "json"), "admit")):
            for rc in (0, 1, 3, 75):
                with self.subTest(argv=argv, rc=rc):
                    out = subprocess.run(["/bin/sh", str(self.wrapper), *argv],
                                         env={**base, "STUB_RC": str(rc)}, input="kind: x\n",
                                         capture_output=True, text=True, timeout=30)
                    self.assertEqual(out.returncode, rc)
                    self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1])["ran"], ran)

    def test_kubectl_or_the_admission_runs_in_the_wrappers_own_process(self):
        # exec'd, not a child: a signal sent only to the wrapper's pid (a backgrounded
        # port-forward or `logs -f`) reaches kubectl, directly or through the admission.
        base = {"PATH": self.path, "HOME": str(self.home), "KUBE_PROXY_URL": PROXY}
        for argv, ran in ((("get", "ns"), "admit"), (("logs", "-f", "x"), "kubectl")):
            with self.subTest(argv=argv):
                proc = subprocess.Popen(["/bin/sh", str(self.wrapper), *argv], env=base,
                                        stdout=subprocess.PIPE, text=True)
                out, _ = proc.communicate(timeout=30)
                got = json.loads(out.strip().splitlines()[-1])
                self.assertEqual(got["ran"], ran)
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

    def test_the_image_writes_the_session_fast_path_figures_from_its_own_admission_guard_and_table(self):
        # deploy/sandbox/kubectl_session_fastpath.py, run on the installed files once python3 is in,
        # writes the file session-kubectl.sh's all_fast reads (tests/test_session_kubectl_fast_path.py
        # checks its figures).
        text = DOCKERFILE.read_text(encoding="utf-8")
        target = "/usr/local/share/kube-agents/kubectl-session-fastpath"
        self.assertIn("COPY deploy/sandbox/kubectl_session_fastpath.py /tmp/kubectl_session_fastpath.py\n", text)
        run = re.search(r"(?m)^RUN python3 -I -B /tmp/kubectl_session_fastpath\.py (\S+) \\\n\s+(\S+) (\S+) \\\n"
                        rf"\s+>{re.escape(target)} \\\n\s+&& chmod 0644 {re.escape(target)} \\\n"
                        rf"\s+&& test -s {re.escape(target)} \\\n\s+&& rm /tmp/kubectl_session_fastpath\.py$", text)
        self.assertIsNotNone(run)
        self.assertEqual(run.groups(), (ADMIT, GUARD, COSTS))
        for before in (f"deploy/sandbox/kubectl-admit.py {ADMIT}", f"deploy/sandbox/shell-memory-guard.py {GUARD}",
                       f"deploy/sandbox/kubectl-costs.json {COSTS}", "apt-get install -y --no-install-recommends",
                       "COPY deploy/sandbox/kubectl_session_fastpath.py"):
            self.assertLess(text.index(before), run.start(), before)
        self.assertIn(target, WRAPPER_SCRIPT.read_text(encoding="utf-8"))

    def test_the_session_pod_writes_no_limit_file(self):
        # The limit comes from the cgroup (guard.memory_limit) only while nothing writes this.
        self.assertNotIn("/tmp/kube-agents-session/memory-limit-bytes", DOCKERFILE.read_text(encoding="utf-8"))


SANDBOX_DOCKERFILE = ROOT / "deploy" / "sandbox" / "Dockerfile"


def sandbox_wrapper_script() -> str:
    """The script deploy/sandbox/Dockerfile printf's to /usr/local/bin/kubectl, one quoted line each."""
    text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    block = re.search(r"printf '%s\\n' \\\n((?:\s*'[^']*' \\\n)+)\s*>/usr/local/bin/kubectl ", text)
    if block is None:
        raise AssertionError(f"{SANDBOX_DOCKERFILE} does not printf a kubectl wrapper")
    return "".join(line + "\n" for line in re.findall(r"'([^']*)' \\", block.group(1)))


class SandboxWrapperTest(unittest.TestCase):
    """The shell sandbox's wrapper drops the session pod's admission settings before the admission runs,
    so a sandbox session cannot point it at another state directory or guard, or turn its refusals off."""

    def test_the_sandbox_wrapper_runs_the_admission_without_the_session_pod_settings(self):
        script = sandbox_wrapper_script()
        self.assertIn("unset " + " ".join(SETTINGS) + "\n", script)
        self.assertLess(script.index("unset "), script.index("exec python3 -I " + ADMIT))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "python3").symlink_to(sys.executable)
            admit = root / "kubectl-admit"
            admit.write_text(STUB.format(python=sys.executable, name="admit", settings=SETTINGS))
            admit.chmod(0o755)
            wrapper = root / "kubectl"
            wrapper.write_text(script.replace(ADMIT, str(admit)))
            env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "HOME": tmp, "KUBECTL_ADMIT_RUN_DIR": tmp,
                   "KUBECTL_ADMIT_GUARD_PATH": str(admit), "KUBECTL_ADMIT_ON_BUSY": "run"}
            out = subprocess.run(["/bin/sh", str(wrapper), "get", "pods", "-A"], env=env, capture_output=True,
                                 text=True, check=True, timeout=30)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["ran"], "admit")
        self.assertEqual(got["argv"], ["get", "pods", "-A"])
        self.assertEqual(got["env"], dict.fromkeys(SETTINGS))


if __name__ == "__main__":
    unittest.main()
