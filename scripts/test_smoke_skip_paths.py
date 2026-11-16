#!/usr/bin/env python3
"""Guard for SMOKE_SKIP_RE: the smoke may skip a path only if the eval never reads it.

Run: cd scripts && python3 -m unittest test_smoke_skip_paths

A pull request that touches only skipped paths merges without the eval, so a
read path on the list lets a behaviour change merge ungraded. The list is a
deny-list on purpose: a new folder is not on it and runs the eval. This test
collects what the presubmit reads from the sources themselves and fails if
the list covers any of it:

  - the job's scripts, every hack/ script they run, source or import, and
    every tracked file those scripts name outside a comment;
  - every file the CI image build copies (deploy/docker/cloudbuild-ci.yaml
    and the Dockerfiles it names), and the ignore files that shape a build
    context;
  - the chart, the bench harness and the eval rosters, read whole.

A skipped file is also flagged when a read script or the harness names it,
which catches a path built from parts, such as
`Path(__file__).parents[3] / "scripts" / "x.py"`.

It also checks the list against Prow's copy, which merge_gate.py says to keep
identical: when Prow skips a path the gate does not, `Smoke gate` waits for a
status Prow never posts.
"""

import os
import re
import subprocess
import sys
import unittest
import urllib.request
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import merge_gate as gate

ROOT = _HERE.parent
#: The steps of pull-kube-agents-smoke-test, in the order its command runs them.
JOB_SCRIPTS = (
    "hack/ci-revalidate.sh",
    "hack/ci-teardown.sh",
    "hack/ci-connection-test.sh",
    "hack/ci-deploy.sh",
    "hack/ci-eval-pr.sh",
)
CLOUDBUILD = "deploy/docker/cloudbuild-ci.yaml"
#: Read whole: helm installs the chart, `uv run` builds and runs bench/, and the rosters pick the cases.
READ_TREES = ("charts/kube-agents/", "bench/", "hack/eval/")
#: Read without being named: bench/ has no .python-version, so `uv run` there takes the root one.
READ_FILES = (".python-version",)
#: Code the harness and the image entrypoints run, scanned for names of skipped files.
NAMING_TREES = ("bench/kube_agents_bench/", "deploy/")
#: Paths a job script names that the presubmit does not read, each with the reason.
NOT_READ = {
    "scripts/release/": "ci-deploy.sh sources common.sh only when RC_COMMIT_SHA is set, the release lane",
    "scripts/eval_dashboard/": "ci-eval-pr.sh skips the dashboard publish when PULL_NUMBER is set, so no presubmit runs it",
    "bench/cuj/": "manual critical user journeys; nothing in CI runs them (AGENTS.md)",
    "bench/dashboard/": "a SQL file for the dashboard; the harness does not read it",
    "bench/tests/": "the harness's unit tests, run by python-tests, not by the eval",
    "scripts/check_skill_size.py": "deploy/docker/skill_documents.py names it as one of its own readers",
    "LICENSE": "ci-revalidate.sh names it in its inert-path regex",
    "OWNERS": "ci-revalidate.sh names it in its inert-path regex",
    "OWNERS_ALIASES": "ci-revalidate.sh names it in its inert-path regex",
}
#: `go build` never compiles these, and no Dockerfile the build names runs `go test`.
GO_TEST_RE = re.compile(r"_test\.go$")
#: Never read because a script names them: the eval runs no tests or workflows, and scripts name
#: those and the docs in prose. A Markdown file the eval reads reaches it through an image or a
#: read tree.
NOT_NAMED_RE = re.compile(r"(^|/)test_[^/]*\.py$|_test\.go$|\.md$|^\.github/")
#: Names too common to say which file a script means.
GENERIC_NAMES = {"README.md", "OWNERS", "LICENSE", "common.sh", "__init__.py", "conftest.py"}
DOCKERFILE_RE = re.compile(r"[\w./$-]*Dockerfile[\w.$-]*")
COPY_RE = re.compile(r"^\s*(?:COPY|ADD)\s+(.*)$", re.IGNORECASE)
PATH_TOKEN_RE = re.compile(r"(?:\$\{?SCRIPT_DIR\}?/)?(?:\.\./)*[\w.-]+(?:/[\w.-]+)*")
#: shellcheck's statement of what a `source` line resolves to, for paths built at run time.
SHELLCHECK_SOURCE_RE = re.compile(r"^\s*#\s*shellcheck\s+source=(\S+)", re.MULTILINE)
SOURCE_RE = re.compile(r"^\s*(?:source|\.)\s")
IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))")
IGNORE_FILE_RE = re.compile(r"(^|/)(\.dockerignore|\.gcloudignore|[^/]*\.dockerignore)$")
PROW_YAML_URL = (
    "https://raw.githubusercontent.com/GoogleCloudPlatform/oss-test-infra/master/"
    "prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml"
)
PROW_JOB = "pull-kube-agents-smoke-test"


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.split()


def code_lines(path):
    """The lines of a script or module that are not whole-line comments."""
    text = (ROOT / path).read_text(errors="replace")
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def under(path, prefixes):
    return any(path.startswith(p) for p in prefixes)


def job_closure(files):
    """{path: the file that names it} for the job scripts and everything they name.

    Every path a hack/ script names counts. Past hack/ the scripts mention much they never
    run, so there only what they import or source counts.
    """
    named = {p: "the job" for p in JOB_SCRIPTS}
    queue = list(JOB_SCRIPTS)
    while queue:
        current = queue.pop()
        base = os.path.dirname(current)
        whole = current.startswith("hack/")
        directives = SHELLCHECK_SOURCE_RE.findall((ROOT / current).read_text(errors="replace"))
        for path in {os.path.normpath(p) for p in directives} - named.keys():
            if path in files and not NOT_NAMED_RE.search(path):
                named[path] = current
                if not under(path, NOT_READ):
                    queue.append(path)
        for line in code_lines(current):
            candidates = set()
            tokens = PATH_TOKEN_RE.findall(line) if whole or SOURCE_RE.match(line) else []
            for token in tokens:
                token = re.sub(r"^\$\{?SCRIPT_DIR\}?/", "", token).rstrip("/.")
                candidates.add(os.path.normpath(os.path.join(base, token)))
                candidates.add(os.path.normpath(token))
            module = imported_module(line) if current.endswith(".py") else None
            if module:
                candidates.add(os.path.join(base, module + ".py"))
            for path in candidates:
                if path not in files or path in named or NOT_NAMED_RE.search(path):
                    continue
                named[path] = current
                if path.endswith((".sh", ".py")) and not under(path, NOT_READ):
                    queue.append(path)
    return named


def imported_module(line):
    """The top-level module a Python import line names, or None."""
    match = IMPORT_RE.match(line)
    return (match.group(1) or match.group(2)).split(".")[0] if match else None


def build_dockerfiles():
    """Every Dockerfile cloudbuild-ci.yaml names; a shell variable in a name matches any text."""
    dockerfiles = set()
    for ref in DOCKERFILE_RE.findall((ROOT / CLOUDBUILD).read_text()):
        pattern = re.sub(r"\$\$?\{?\w+\}?", "*", ref)
        dockerfiles.update(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern) if p.is_file())
    return sorted(dockerfiles)


def image_sources(files):
    """{path: the Dockerfile that copies it} for every image cloudbuild-ci.yaml builds."""
    copied = {}
    for dockerfile in build_dockerfiles():
        joined = (ROOT / dockerfile).read_text().replace("\\\n", " ")
        sources = []
        for line in joined.splitlines():
            match = COPY_RE.match(line)
            if not match:
                continue
            if "--from" in match.group(1):
                continue
            words = [w for w in match.group(1).split() if not w.startswith("--")]
            sources.extend(w.strip('"') for w in words[:-1])
        named = [s for s in sources if s != "."]
        at_root = bool(named) and all((ROOT / s).exists() for s in named)
        context = "" if at_root else os.path.dirname(dockerfile)
        for source in sources:
            prefix = os.path.normpath(os.path.join(context, source))
            prefix = "" if prefix == "." else prefix
            for path in files:
                if path == prefix or path.startswith(prefix + "/" if prefix else ""):
                    copied.setdefault(path, dockerfile)
    return copied


def prow_skip_pattern(yaml_text):
    """The skip_if_only_changed of PROW_JOB, read without a YAML parser."""
    start = yaml_text.index(f"- name: {PROW_JOB}\n")
    end = yaml_text.find("\n  - name: ", start + 1)
    block = yaml_text[start : end if end != -1 else None]
    match = re.search(r"^\s*skip_if_only_changed:\s*'((?:[^']|'')*)'\s*$", block, re.MULTILINE)
    return match.group(1).replace("''", "'")


class SkipListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tracked()
        cls.fileset = set(cls.files)
        cls.closure = job_closure(cls.fileset)
        cls.copied = image_sources(cls.files)
        cls.skipped = [p for p in cls.files if gate.SMOKE_SKIP_RE.search(p)]

    def read_paths(self):
        """{path: why the presubmit reads it}."""
        read = {}
        for path, by in self.closure.items():
            read[path] = f"named by {by}"
        for path, dockerfile in self.copied.items():
            if not GO_TEST_RE.search(path):
                read.setdefault(path, f"copied by {dockerfile}")
        for path in self.files:
            if under(path, READ_TREES):
                read.setdefault(path, "in a tree the eval reads whole")
            if path in READ_FILES:
                read.setdefault(path, "read by a tool the eval runs")
            if IGNORE_FILE_RE.search(path):
                read.setdefault(path, "shapes a build context")
        return {p: why for p, why in read.items() if not under(p, NOT_READ)}

    def test_no_path_the_presubmit_reads_is_skipped(self):
        read = self.read_paths()
        wrong = sorted(f"{p} ({read[p]})" for p in self.skipped if p in read)
        self.assertEqual(wrong, [], "SMOKE_SKIP_RE covers paths the eval reads; take them off the list")

    def test_no_skipped_file_is_named_by_code_the_eval_runs(self):
        readers = [p for p in self.closure if p.endswith((".sh", ".py"))]
        readers += [p for p in self.files if under(p, NAMING_TREES) and p.endswith((".sh", ".py"))]
        readers = [
            p for p in readers if not (under(p, NOT_READ) or gate.SMOKE_SKIP_RE.search(p) or NOT_NAMED_RE.search(p))
        ]
        counts = Counter(os.path.basename(p) for p in self.files)
        spellings = {}
        for path in self.skipped:
            name = os.path.basename(path)
            if under(path, NOT_READ) or counts[name] > 1 or name in GENERIC_NAMES or NOT_NAMED_RE.search(path):
                continue
            if re.search(r"[_-].*\.", name):
                spellings[name] = path
            elif "." in name:
                spellings[f'"{name}"'] = spellings[f"'{name}'"] = path
            if "/" in path:
                spellings[f"{os.path.basename(os.path.dirname(path))}/{name}"] = path
        pattern = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(n) for n in spellings) + r")(?![\w-])")
        modules = {os.path.basename(p)[: -len(".py")]: p for p in self.skipped if p.endswith(".py")}
        wrong = set()
        for reader in sorted(set(readers)):
            for line in code_lines(reader):
                for match in pattern.finditer(line):
                    wrong.add(f"{spellings[match.group(1)]} (named in {reader})")
                module = imported_module(line) if reader.endswith(".py") else None
                if module in modules:
                    wrong.add(f"{modules[module]} (imported by {reader})")
        self.assertEqual(sorted(wrong), [], "code the eval runs names a skipped file")

    def test_not_read_entries_are_on_the_list(self):
        """An entry here that the list does not skip is stale, and hides nothing."""
        for prefix in NOT_READ:
            with self.subTest(prefix=prefix):
                inside = [p for p in self.files if p.startswith(prefix)]
                self.assertTrue(inside, f"{prefix} has no tracked file")
                self.assertTrue(any(gate.SMOKE_SKIP_RE.search(p) for p in inside))

    def test_the_collectors_find_what_the_eval_is_known_to_read(self):
        """If parsing drifts and these fall out, the first two tests would pass on nothing."""
        read = self.read_paths()
        for path in (
            "hack/ci-env.sh",
            "hack/ci-connection-test.sh",
            "hack/boskos_pool.py",
            "tags.env",
            "scripts/verify_ci_pool_project.py",
            "scripts/installer/gke_dns_endpoint.sh",
            "deploy/docker/patches/patchlib.py",
            "agents/platform/SOUL.md",
            "a2a/web/package.json",
            "k8s-operator/.dockerignore",
            "k8s-api-proxy/go.mod",
            "charts/kube-agents/values.yaml",
            "bench/pyproject.toml",
        ):
            with self.subTest(path=path):
                self.assertIn(path, read)

    def test_an_import_names_its_module_file(self):
        """The name check reads `import github_api`, which never spells `github_api.py`."""
        for line in ("import github_api", "from github_api import GitHubAPI", "  import github_api.sub as g"):
            with self.subTest(line=line):
                self.assertEqual(imported_module(line), "github_api")
        self.assertIsNone(imported_module("github_api.call()"))

    def test_no_image_build_compiles_go_tests(self):
        """What makes `_test.go` safe to skip although the images copy it."""
        for dockerfile in build_dockerfiles():
            with self.subTest(dockerfile=dockerfile):
                self.assertNotRegex((ROOT / dockerfile).read_text(), r"\bgo\s+test\b")

    def test_new_folders_run_the_eval(self):
        for path in ("newdir/x.py", "agents/newpersona/SOUL.md", "a2a/newsvc/main.go", "bench/newdir/x.py",
                     "hack/new_ci_step.sh", "scripts/new_tool.py", "scripts/installer/new.sh"):
            with self.subTest(path=path):
                self.assertTrue(gate.smoke_required([path]))


class ProwCopyTest(unittest.TestCase):
    def test_prow_skips_the_same_paths(self):
        try:
            with urllib.request.urlopen(PROW_YAML_URL, timeout=20) as response:
                text = response.read().decode()
        except OSError as error:
            self.skipTest(f"oss-test-infra not reachable: {error}")
        try:
            pattern = prow_skip_pattern(text)
        except (ValueError, AttributeError):
            self.fail(f"no skip_if_only_changed for {PROW_JOB} in {PROW_YAML_URL}; fix prow_skip_pattern")
        self.assertEqual(pattern, gate.SMOKE_SKIP_RE.pattern)


if __name__ == "__main__":
    unittest.main()
