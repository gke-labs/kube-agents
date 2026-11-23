"""The session kubectl wrapper's fast path never skips a read the admission would charge.

deploy/sandbox/session-kubectl.sh decides in the shell, with no fork, which argv the admission
(deploy/sandbox/kubectl-admit.py) would pass through uncharged, and execs kubectl directly for
those. This test runs that decision, with stub kubectl and admission, over every argv
deploy/sandbox/test_kubectl_admit.py exercises plus a generated set, and asserts that every argv
admit() would charge (a list whose scope is the cluster) takes the admission.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "deploy" / "sandbox"
WRAPPER_SCRIPT = SANDBOX / "session-kubectl.sh"
ADMIT_TESTS = SANDBOX / "test_kubectl_admit.py"
COST_TABLE = SANDBOX / "kubectl-costs.json"
LIBEXEC = "/usr/local/libexec/"
BROKER_LIBEXEC = "/opt/credential-proxy/libexec/"
PROXY = "https://agent-kube-proxy.ns.svc.cluster.local:8443"
# The functions test_kubectl_admit.py hands an argv to, as positional words; `charge` takes a tier first.
ARGV_CALLS = {"parsed": 0, "key": 0, "admit": 0, "main": 0, "charge": 1}


def load_admission():
    loader = importlib.machinery.SourceFileLoader("kubectl_admit_fast_path", str(SANDBOX / "kubectl-admit.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


admit = load_admission()


def charged(argv: list[str]) -> bool:
    """Whether admit() reaches its charge for this argv: by its own rule, a slotted command whose
    list (kinds_of) has the cluster scope (scope_of)."""
    cmd = admit.parse(argv)
    if admit.unslotted(cmd, argv):
        return False
    kinds = admit.kinds_of(cmd)
    return bool(kinds) and admit.scope_of(cmd, kinds) == admit.CLUSTER_SCOPE


def _strings(nodes) -> list[str] | None:
    if all(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in nodes):
        return [n.value for n in nodes]
    return None


def admit_test_argvs() -> set[tuple[str, ...]]:
    """Every argv literal in test_kubectl_admit.py: each tuple or list of string literals (which
    takes in every `cases` and `for argv in [...]` entry, and some lists that are not argvs, harmless
    here), each call that passes words to parsed/key/admit/main/charge, and each cost-table example
    its table test keys."""
    out: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(ADMIT_TESTS.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.Tuple, ast.List)):
            words = _strings(node.elts)
            if words is not None:
                out.add(tuple(words))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ARGV_CALLS:
                words = _strings([a for a in node.args if not isinstance(a, ast.Starred)])
                if words is not None and not any(isinstance(a, ast.Starred) for a in node.args):
                    out.add(tuple(words[ARGV_CALLS[name]:]))
    for column in json.loads(COST_TABLE.read_text(encoding="utf-8"))["tiers"].values():
        for entry in column["commands"].values():
            out.add(tuple(entry["example"].split()[1:]))
    return out


KINDS = ["pods", "po", "deploy", "svc", "events", "secrets", "nodes", "no", "namespaces", "ns", "pv",
         "clusterroles", "all", "pods,deploy", "pods,nodes", "pod/x", "nodes/n1", "pods,pod/x", "pods.v1",
         "pods.metrics.k8s.io", "widgets.example.com", "deployments.apps", "Pods", "pods,", "ALL",
         "certificates.cert-manager.io"]
VERBS = ["get", "describe", "logs", "top", "delete", "events", "apply"]
SCOPES = [[], ["-A"], ["--all-namespaces"], ["--all-namespaces=false"], ["-A=false"], ["-n", "x"],
          ["--namespace=x"], ["--namespace", "x"], ["-nteam"], ["-n", "x", "-A"], ["-Ao", "json"], ["-oA"],
          ["-Aojson"], ["-Al", "app=x"], ["-lapp=A"], ["-A", "--all-namespaces=false"]]
OUTPUTS = [[], ["-o", "json"], ["-ojson"], ["-o", "wide"], ["--output=yaml"], ["--output", "name"],
           ["-o", "jsonpath={.items}"], ["--sort-by", ".metadata.name"], ["--no-headers"]]
EXTRAS = [[], ["-l", "app=x"], ["-lapp=x"], ["--selector", "a=b"], ["--field-selector=x"], ["--raw", "/api"],
          ["-w"], ["--watch"], ["--watch=false"], ["--watch-only"], ["-f", "-"], ["-k", "dir"], ["--filename=x"],
          ["second"], ["-h"], ["--help=false"], ["--unknown-flag"], ["-x"], ["--", "y"], ["-"]]
GLOBALS = [[], ["--context", "c"], ["--context=c"], ["-n", "ns"], ["--kubeconfig", "f"], ["-v", "6"], ["-v6"],
           ["--as", "get"], ["--context", "get"], ["--unknown", "x"], ["--request-timeout=5s"], ["-s", "https://x"]]


def generated_argvs() -> set[tuple[str, ...]]:
    """Kinds x verbs x scopes x flag positions in full, each with an output, an extra flag and a
    global flag drawn from a fixed seed; then outputs x extras x globals in full on a few kinds; then
    every long flag either side of the verb."""
    rng = random.Random(6834)
    out: set[tuple[str, ...]] = set()
    for kind, verb, scope, after in itertools.product(KINDS, VERBS, SCOPES, (True, False)):
        g, o, e = rng.choice(GLOBALS), rng.choice(OUTPUTS), rng.choice(EXTRAS)
        flags = [*scope, *o, *e]
        out.add(tuple([*g, verb, kind, *flags] if after else [*g, verb, *flags, kind]))
        out.add(tuple([verb, kind, *scope] if after else [verb, *scope, kind]))
    for kind, o, e, g in itertools.product(["pods", "nodes", "all", "pod/x"], OUTPUTS, EXTRAS, GLOBALS):
        out.add(tuple([*g, "get", kind, *o, *e]))
    flags = set(re.findall(r"--[a-z][a-z-]*", WRAPPER_SCRIPT.read_text(encoding="utf-8")))
    flags |= {f for f in admit.VALUE_FLAGS} | set(admit.LONG_FLAG_SHORTHANDS)
    for f in sorted(flags):
        for argv in ([f, "get", "nodes"], [f, "x", "get", "pods"], ["get", f, "nodes"], ["get", "pods", f, "nodes"],
                     [f, "get", "pods", "-A"], ["get", f, "x", "nodes"]):
            out.add(tuple(argv))
    return out


class FastPathParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        libexec, broker, home, bindir = root / "libexec", root / "broker", root / "home", root / "bin"
        for d in (libexec, broker, home / ".kube", bindir):
            d.mkdir(parents=True)
        (home / ".kube" / "config").write_text("")
        # Which path ran: kubectl directly (fast), or python3 running the admission (slow).
        for path, body in [(libexec / "kubectl", "echo fast"), (libexec / "kubectl-context-registered", "exit 0"),
                           (bindir / "python3", 'case "$2" in */kubectl-admit) echo slow ;; *) echo other ;; esac')]:
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)
        script = WRAPPER_SCRIPT.read_text(encoding="utf-8")
        script = script.replace(LIBEXEC, f"{libexec}/").replace(BROKER_LIBEXEC, f"{broker}/")
        cls.wrapper = root / "kubectl"
        cls.wrapper.write_text(script)
        cls.env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "KUBE_PROXY_URL": PROXY}
        # The image's /bin/sh, and dash, the strictest POSIX shell here, when installed.
        cls.shells = [s for s in dict.fromkeys(["/bin/sh", shutil.which("dash")]) if s]

    def path_taken(self, shell: str, argv: tuple[str, ...]) -> str:
        out = subprocess.run([shell, str(self.wrapper), *argv], env=self.env, capture_output=True, text=True,
                             timeout=30)
        return out.stdout.strip()

    def paths(self, shell: str, argvs: list[tuple[str, ...]]) -> dict[tuple[str, ...], str]:
        """The path the whole wrapper takes for each argv, one process each."""
        with ThreadPoolExecutor(max_workers=max(4, os.cpu_count() or 4)) as pool:
            return dict(zip(argvs, pool.map(lambda a: self.path_taken(shell, a), argvs)))

    def decisions(self, shell: str, argvs: list[tuple[str, ...]]) -> dict[tuple[str, ...], str]:
        """The wrapper's own decision function, cut from the script, run on every argv in one shell
        process: one process per argv is too slow for thousands of them. paths() checks that the
        whole wrapper agrees with it on a sample."""
        text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
        start = text.index("uncharged() {")
        function = text[start:text.index("\n}\n", start) + 3]
        quote = lambda w: "'" + w.replace("'", "'\\''") + "'"
        lines = [f"if uncharged {' '.join(map(quote, a))}; then echo fast; else echo slow; fi" for a in argvs]
        out = subprocess.run([shell, "-s"], input=function + "\n".join(lines) + "\n", env=self.env,
                             capture_output=True, text=True, timeout=120, check=True)
        got = out.stdout.split()
        self.assertEqual(len(got), len(argvs))
        return dict(zip(argvs, got))

    def test_the_admission_tests_argvs_were_extracted(self):
        argvs = admit_test_argvs()
        for want in [("get", "pods", "-Ao", "json"), ("--context", "c", "get", "po", "--all-namespaces"),
                     ("get", "pods", "-A", "-w=false", "--watch-only"), ("get", "nodes", "-o", "json"),
                     ("get", "pods", "-A"), ("logs", "x", "-f", "--follow=false"), ("get",), ()]:
            self.assertIn(want, argvs)
        self.assertGreater(len(argvs), 100)

    def test_every_charged_argv_takes_the_admission(self):
        from_tests = sorted(admit_test_argvs())
        argvs = sorted(set(from_tests) | generated_argvs())
        expect = {a: charged(list(a)) for a in argvs}
        sample = sorted(set(from_tests) | set(random.Random(6834).sample(argvs, 400)))
        for shell in self.shells:
            with self.subTest(shell=shell):
                got = self.decisions(shell, argvs)
                self.assertEqual(set(got.values()) - {"fast", "slow"}, set())
                leaks = [a for a in argvs if expect[a] and got[a] != "slow"]
                self.assertEqual(leaks, [], "charged argv took the fast path")
                # The whole wrapper, stubs and all, takes the path its function chose.
                ran = self.paths(shell, sample)
                self.assertEqual({a: ran[a] for a in sample}, {a: got[a] for a in sample})
                uncharged = [a for a in argvs if not expect[a]]
                fast = [a for a in uncharged if got[a] == "fast"]
                sys.stderr.write(f"\n{shell}: {len(argvs)} argv ({len(from_tests)} from the admission's tests), "
                                 f"{len(argvs) - len(uncharged)} charged, all on the admission; {len(fast)} of "
                                 f"{len(uncharged)} uncharged on the fast path; {len(sample)} run end to end\n")
                if os.environ.get("FAST_PATH_SHOW_SLOW"):
                    for a in uncharged:
                        if got[a] != "fast":
                            sys.stderr.write(f"  slow, uncharged: {list(a)}\n")

    def test_common_uncharged_commands_take_the_fast_path(self):
        cases = [("get", "pods"), ("get", "pods", "-n", "x", "-o", "json"), ("--context", "c", "get", "po", "-n", "x"),
                 ("get", "pod", "p1", "-A"), ("get", "pod/p1", "-o", "yaml"), ("get", "nodes", "n1"),
                 ("describe", "pod", "x", "-n", "y"), ("get", "pods", "-w"), ("get", "--raw", "/api/v1/nodes"),
                 ("logs", "x", "-f"), ("exec", "-it", "x", "--", "sh"), ("apply", "-f", "-"), ("version",),
                 ("get", "deploy,svc", "-l", "app=x"), ("top", "pods", "-A")]
        for shell in self.shells:
            got = self.paths(shell, cases)
            for argv in cases:
                with self.subTest(shell=shell, argv=argv):
                    self.assertFalse(charged(list(argv)))
                    self.assertEqual(got[argv], "fast")

    def test_flags_the_wrapper_reads_agree_with_the_admission(self):
        # A flag the wrapper skips a word after must take one in parse() too, and a flag it reads as
        # a switch must not; otherwise the two could find different verbs.
        text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
        body = text[text.index("uncharged() {"):text.index("\n}\n", text.index("uncharged() {"))]
        arms = re.findall(r"((?:--?[\w-]+(?:\s*\|\s*\\?\s*)?)+)\)\s*(skip=1|;;)", body)
        takes_value = {f for names, act in arms if act == "skip=1" for f in re.findall(r"--?[\w-]+", names)}
        switches = {f for names, act in arms if act == ";;" for f in re.findall(r"--?[\w-]+", names)}
        self.assertIn("--context", takes_value)
        self.assertIn("--no-headers", switches)
        self.assertLessEqual(takes_value, set(admit.VALUE_FLAGS))
        self.assertEqual(switches & (set(admit.VALUE_FLAGS) | set(admit.LONG_FLAG_SHORTHANDS)), set())


if __name__ == "__main__":
    unittest.main()
