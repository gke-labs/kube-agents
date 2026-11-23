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
        # Each case arm of plain flags, a bracket of short flags (-[olnLcsv]) included, with its body
        # up to `;;`. An arm whose body sets skip=1 takes a value, whatever else it sets; an empty
        # body is a switch. Arms with a glob (--*=*, -f?*) match no flag here.
        arms = re.findall(r"((?:-[-\w\[\]]+(?:\s*\|\s*\\?\s*)?)+)\)((?:(?!;;).)*);;", body, re.S)

        def flags(names: str) -> set[str]:
            found = set()
            for name in re.findall(r"-[-\w\[\]]+", names):
                bracket = re.fullmatch(r"-\[(\w+)\]", name)
                found |= {f"-{c}" for c in bracket.group(1)} if bracket else {name}
            return found

        takes_value = set().union(*(flags(names) for names, act in arms if re.search(r"\bskip=1\b", act)))
        switches = set().union(*(flags(names) for names, act in arms if not act.strip()))
        for flag in ("--context", "-o", "-l", "-n", "-L", "-c", "-s", "-v", "--raw", "-f", "--filename", "-k",
                     "--kustomize"):
            self.assertIn(flag, takes_value)
        self.assertIn("--no-headers", switches)
        self.assertLessEqual(takes_value, set(admit.VALUE_FLAGS))
        self.assertEqual(switches & (set(admit.VALUE_FLAGS) | set(admit.LONG_FLAG_SHORTHANDS)), set())


GUARD_SOURCE = SANDBOX / "shell-memory-guard.py"
GENERATOR = SANDBOX / "kubectl_session_fastpath.py"
MIB = 1024 * 1024
GIB = 1024 * MIB
# The production paths all_fast reads, each rewritten below to a file the test controls.
FASTPATH_FILE = "/usr/local/share/kube-agents/kubectl-session-fastpath"
OSRELEASE = "/proc/sys/kernel/osrelease"
MEMORY_MAX = "/sys/fs/cgroup/memory.max"
SESSION_RUN_DIR = "/tmp/kube-agents-session/"
LIMITS = [256 * MIB, 512 * MIB, GIB, 2 * GIB, 4 * GIB, 8 * GIB, 32 * GIB]
LINUX = "6.1.0-28-cloud-amd64"


def load_guard():
    loader = importlib.machinery.SourceFileLoader("shell_memory_guard_fast_path", str(GUARD_SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


guard = load_guard()
TABLE = json.loads(COST_TABLE.read_text(encoding="utf-8"))


def generated(table_path: Path = COST_TABLE) -> str:
    """The file a2a/Dockerfile.worker writes, by the same command."""
    return subprocess.run([sys.executable, "-I", "-B", str(GENERATOR), str(SANDBOX / "kubectl-admit.py"),
                           str(GUARD_SOURCE), str(table_path)], capture_output=True, text=True, check=True,
                          timeout=30).stdout


def widest_argvs(table: dict, tier: str) -> list[tuple[str, ...]]:
    """Per scope and format, a `get` of every kind with a single-kind entry on the tier: the argv
    whose charge is a sum over the most entries."""
    out = []
    kinds = sorted({k.split(" ")[1] for k in table["tiers"][tier]["commands"] if "," not in k.split(" ")[1]})
    for output in ([], ["-o", "json"]):
        out.append(("get", ",".join(kinds), "-A", *output))
    return out


def would_refuse(argv: tuple[str, ...], table: dict, tier: str, limit: int) -> bool:
    """admit()'s one refusal on a session pod, off gVisor: a charged read, no selector, whose cost
    is above the guard's watermark at this limit less the shell's floor."""
    if not charged(list(argv)):
        return False
    cmd = admit.parse(list(argv))
    need, _listed = admit.cost(table, tier, cmd)
    cap = guard.effective_watermark(limit, False, 0) - admit.SHELL_FLOOR_BYTES
    return need > cap and not cmd["selector"]


def independent_min_limit(bound: int) -> int:
    """The smallest limit L the guard's own watermark gives one read at least `bound` at, found
    by a linear walk in MiB then bytes, with no search shared with the generator."""
    def fits(limit: int) -> bool:
        return guard.high_watermark(limit) - admit.SHELL_FLOOR_BYTES >= bound
    limit = 0
    while not fits(limit):
        limit += MIB
    while limit > 0 and fits(limit - 1):
        limit -= 1
    return limit


def independent_bound(table: dict, tier: str) -> int:
    """The largest charge cost() returns on the tier, by running cost() on every argv that can reach
    a figure: each entry's example, the widest lists, and an unknown kind (`other`)."""
    argvs = [tuple(e["example"].split()[1:]) for e in table["tiers"][tier]["commands"].values()]
    argvs += widest_argvs(table, tier) + [("get", "widgets.example.com", "-A")]
    return max(admit.cost(table, tier, admit.parse(list(a)))[0] for a in argvs if charged(list(a)))


class SessionFastPathGeneratorTest(unittest.TestCase):
    def check(self, table: dict, text: str):
        want = "".join(f"{tier} {independent_min_limit(independent_bound(table, tier))}\n"
                       for tier in sorted(table["tiers"]))
        self.assertEqual(text, want)

    def test_the_generator_matches_the_table_and_the_guard_for_every_tier(self):
        text = generated()
        self.assertEqual([line.split()[0] for line in text.splitlines()], sorted(TABLE["tiers"]))
        self.check(TABLE, text)

    def test_the_generator_follows_a_changed_table(self):
        # A new kind, a larger entry and a larger `other`: the figures move with the table.
        table = json.loads(json.dumps(TABLE))
        table["tiers"]["default"]["commands"]["get widgets cluster json"] = {"peak_mib": 900, "example": "x"}
        table["tiers"]["fleet"]["commands"]["get pods cluster json"]["peak_mib"] = 30000
        table["tiers"]["small"] = {"commands": {"get pods cluster table": {"peak_mib": 10, "example": "x"}},
                                   "other": {"peak_mib": 700}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "costs.json"
            path.write_text(json.dumps(table))
            text = generated(path)
        self.assertNotEqual(text, generated())
        self.check(table, text)

    def test_max_cost_bounds_every_argv(self):
        # admit.max_cost (the generator's bound) is reached by a widest list and never passed.
        argvs = admit_test_argvs() | generated_argvs()
        rng = random.Random(6834)
        for tier, column in TABLE["tiers"].items():
            names = sorted({k.split(" ")[1] for k in column["commands"]} | {"all", "secrets", "widgets"})
            extra = set()
            for _ in range(2000):
                kinds = ",".join(rng.sample(names, rng.randint(1, len(names))))
                extra.add(("get", kinds, *rng.choice(SCOPES), *rng.choice(OUTPUTS)))
            bound = admit.max_cost(TABLE, tier)
            with self.subTest(tier=tier):
                self.assertEqual(bound, independent_bound(TABLE, tier))
                over = [a for a in sorted(argvs | extra | set(widest_argvs(TABLE, tier)))
                        if charged(list(a)) and admit.cost(TABLE, tier, admit.parse(list(a)))[0] > bound]
                self.assertEqual(over, [])


class AllFastTest(unittest.TestCase):
    """all_fast in session-kubectl.sh: every command runs kubectl directly only where the admission
    could refuse none."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        libexec, broker, home, bindir, run = (root / "libexec", root / "broker", root / "home", root / "bin",
                                              root / "run")
        for d in (libexec, broker, home / ".kube", bindir, run):
            d.mkdir(parents=True)
        (home / ".kube" / "config").write_text("")
        for path, body in [(libexec / "kubectl", "echo fast"), (libexec / "kubectl-context-registered", "exit 0"),
                           (bindir / "python3", 'case "$2" in */kubectl-admit) echo slow ;; *) echo other ;; esac')]:
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)
        self.fastpath, self.osrelease, self.memory_max, self.run = (root / "fastpath", root / "osrelease",
                                                                    root / "memory.max", run)
        script = WRAPPER_SCRIPT.read_text(encoding="utf-8")
        for prod, test in [(FASTPATH_FILE, str(self.fastpath)), (OSRELEASE, str(self.osrelease)),
                           (MEMORY_MAX, str(self.memory_max)), (SESSION_RUN_DIR, f"{run}/")]:
            self.assertIn(prod, script)
            script = script.replace(prod, test)
        script = script.replace(LIBEXEC, f"{libexec}/").replace(BROKER_LIBEXEC, f"{broker}/")
        # The admission's own run directory, unrewritten: only the two override files moved.
        self.assertIn("KUBECTL_ADMIT_RUN_DIR=/tmp/kube-agents-session ", script)
        self.wrapper = root / "kubectl"
        self.wrapper.write_text(script)
        self.fastpath.write_text(generated())
        self.osrelease.write_text(LINUX + "\n")
        self.memory_max.write_text(f"{2 * GIB}\n")
        self.env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "KUBE_PROXY_URL": PROXY,
                    "KUBEAGENTS_KUBECTL_COST_TIER": "default"}
        self.shells = [s for s in dict.fromkeys(["/bin/sh", shutil.which("dash")]) if s]
        self.min_limit = {line.split()[0]: int(line.split()[1]) for line in generated().splitlines()}

    def path_taken(self, shell: str, argv: tuple[str, ...], env: dict | None = None) -> str:
        out = subprocess.run([shell, str(self.wrapper), *argv], env=env or self.env, capture_output=True,
                             text=True, timeout=30)
        return out.stdout.strip()

    def all_fast(self, shell: str, env: dict | None = None) -> bool:
        """all_fast's answer, read from the whole wrapper: a charged read runs kubectl directly only then."""
        got = self.path_taken(shell, ("get", "pods", "-A", "-o", "json"), env)
        self.assertIn(got, ("fast", "slow"))
        return got == "fast"

    def test_all_fast_only_where_the_admission_refuses_nothing_in_the_corpus(self):
        corpus = sorted(admit_test_argvs() | generated_argvs()
                        | {a for tier in TABLE["tiers"] for a in widest_argvs(TABLE, tier)})
        for tier, floor in self.min_limit.items():
            limits, checked = sorted(set(LIMITS) | {floor - 1, floor}), 0
            for shell in self.shells:
                for limit in limits:
                    self.memory_max.write_text(f"{limit}\n")
                    fast = self.all_fast(shell, {**self.env, "KUBEAGENTS_KUBECTL_COST_TIER": tier})
                    with self.subTest(tier=tier, limit=limit, shell=shell):
                        self.assertEqual(fast, limit >= floor)
                        if fast:
                            refused = [a for a in corpus if would_refuse(a, TABLE, tier, limit)]
                            self.assertEqual(refused, [], "all_fast skipped a read the admission refuses")
                            checked += 1
            self.assertGreater(checked, 0)
            # Just under the figure, the tier's widest read is one the admission refuses: the
            # figure is no higher than it needs to be.
            with self.subTest(tier=tier, limit=floor - 1):
                self.assertTrue(any(would_refuse(a, TABLE, tier, floor - 1) for a in widest_argvs(TABLE, tier)))

    def test_fleet_at_two_gib_is_not_all_fast(self):
        for shell in self.shells:
            with self.subTest(shell=shell):
                self.assertFalse(self.all_fast(shell, {**self.env, "KUBEAGENTS_KUBECTL_COST_TIER": "fleet"}))

    def test_a_default_pod_at_two_gib_never_starts_python(self):
        # Every argv runs kubectl directly: the whole corpus through the wrapper's own condition, cut
        # from the script and run in one shell, and a sample of the charged ones end to end.
        corpus = sorted(admit_test_argvs() | generated_argvs())
        text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
        gate = '  if all_fast || uncharged "$@"; then\n    exec "$REAL_KUBECTL" "$@"\n  fi\n'
        self.assertIn(gate, text)
        script = self.wrapper.read_text(encoding="utf-8")
        functions = script[script.index("uncharged() {"):script.index('if [ -n "${KUBE_PROXY_URL:-}" ]; then')]
        quote = lambda w: "'" + w.replace("'", "'\\''") + "'"
        lines = [f"if all_fast || uncharged {' '.join(map(quote, a))}; then echo fast; else echo slow; fi"
                 for a in corpus]
        charged_argvs = [a for a in corpus if charged(list(a))]
        self.assertGreater(len(charged_argvs), 1000)
        sample = random.Random(6834).sample(charged_argvs, 300)
        for shell in self.shells:
            with self.subTest(shell=shell):
                out = subprocess.run([shell, "-s"], input=functions + "\n".join(lines) + "\n", env=self.env,
                                     capture_output=True, text=True, timeout=300, check=True)
                self.assertEqual(out.stdout.split(), ["fast"] * len(corpus))
                with ThreadPoolExecutor(max_workers=max(4, os.cpu_count() or 4)) as pool:
                    ran = list(pool.map(lambda a: self.path_taken(shell, a), sample))
                self.assertEqual([a for a, r in zip(sample, ran) if r != "fast"], [])

    def test_anything_short_of_all_fast_keeps_todays_paths(self):
        env = dict(self.env)
        del env["KUBEAGENTS_KUBECTL_COST_TIER"]
        cases = {
            "tier unset": ({}, env),
            "tier empty": ({}, {**self.env, "KUBEAGENTS_KUBECTL_COST_TIER": ""}),
            "tier unknown": ({}, {**self.env, "KUBEAGENTS_KUBECTL_COST_TIER": "huge"}),
            "tier not plain": ({}, {**self.env, "KUBEAGENTS_KUBECTL_COST_TIER": "Default"}),
            "tier too long": ({}, {**self.env, "KUBEAGENTS_KUBECTL_COST_TIER": "d" * 33}),
            "gvisor": ({"osrelease": "4.4.0-gvisor\n"}, None),
            "gvisor, any case": ({"osrelease": "4.4.0 gVisor build\n"}, None),
            "osrelease empty": ({"osrelease": ""}, None),
            "osrelease unreadable": ({"osrelease": None}, None),
            "memory.max max": ({"memory_max": "max\n"}, None),
            "memory.max unreadable": ({"memory_max": None}, None),
            "memory.max empty": ({"memory_max": ""}, None),
            "memory.max below": ({"memory_max": f"{GIB}\n"}, None),
            "memory.max leading zero": ({"memory_max": f"0{4 * GIB}\n"}, None),
            "memory.max 19 digits": ({"memory_max": "1" + "0" * 18 + "\n"}, None),
            "generated file missing": ({"fastpath": None}, None),
            "generated file without the tier": ({"fastpath": "fleet 1\n"}, None),
            "generated figure not decimal": ({"fastpath": "default 0x10\n"}, None),
            "guard variable below": ({}, {**self.env, "SANDBOX_MEMORY_LIMIT_BYTES": str(GIB)}),
            "guard variable not decimal": ({}, {**self.env, "SANDBOX_MEMORY_LIMIT_BYTES": "2Gi"}),
            "tier file": ({"tier_file": "default\n"}, None),
            "limit file": ({"limit_file": f"{4 * GIB}\n"}, None),
        }
        files = {"osrelease": self.osrelease, "memory_max": self.memory_max, "fastpath": self.fastpath,
                 "tier_file": self.run / "kubectl-cost-tier", "limit_file": self.run / "memory-limit-bytes"}
        saved = {name: path.read_text() if path.exists() else None for name, path in files.items()}
        for shell in self.shells:
            # The baseline: everything in place is all_fast, and so is a guard variable that clears the figure.
            self.assertTrue(self.all_fast(shell))
            self.assertTrue(self.all_fast(shell, {**self.env, "SANDBOX_MEMORY_LIMIT_BYTES": str(4 * GIB)}))
            for name, (overrides, env) in cases.items():
                for key, value in overrides.items():
                    path = files[key]
                    if value is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(value)
                try:
                    with self.subTest(shell=shell, case=name):
                        self.assertFalse(self.all_fast(shell, env))
                        # The paths before all_fast still apply: an uncharged argv runs kubectl directly.
                        self.assertEqual(self.path_taken(shell, ("get", "pods", "-n", "x"), env), "fast")
                finally:
                    for key, value in saved.items():
                        if value is None:
                            files[key].unlink(missing_ok=True)
                        else:
                            files[key].write_text(value)

    def test_the_gate_reads_the_guards_gvisor_mark_and_limit_variable(self):
        text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(guard.GVISOR_RELEASE_MARK, "gvisor")
        self.assertIn("*[Gg][Vv][Ii][Ss][Oo][Rr]*", text)
        self.assertIn("${" + guard.LIMIT_ENV + ":-}", text)
        self.assertEqual(os.path.join(guard.CGROUP_ROOT, guard.CGROUP_V2_MAX), MEMORY_MAX)
        self.assertEqual(admit.TIER_ENV, "KUBEAGENTS_KUBECTL_COST_TIER")
        # The two files main() reads before the environment and the cgroup, under the session pod's run directory.
        for name in (admit.TIER_FILE, admit.LIMIT_FILE):
            self.assertIn(f"[ -e {SESSION_RUN_DIR}{os.path.basename(name)} ] && return 1", text)


if __name__ == "__main__":
    unittest.main()
