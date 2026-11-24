"""The session worker image installs its harness SDK from a committed lockfile.

a2a/Dockerfile.worker used to run `npm install @anthropic-ai/claude-agent-sdk@<v>`
with no lockfile, so the SDK was pinned but its own dependencies resolved to
whatever the registry served at build time (#6948). A just-published transitive
version that the registry had not finished serving failed an image build that
way. The image now copies a2a/worker-harness/package.json and package-lock.json
and runs `npm ci`. These tests hold the pieces together: the Dockerfile, the
manifest, the lock, and the dependabot entry that proposes bumps.

The Dockerfile is read as instructions, not as text, so a comment can neither
fail nor satisfy a check. The lock is held to a registry URL and a sha512 hash
on every entry, since `npm ci` accepts an entry that carries no hash.
"""

import copy
import json
import pathlib
import re
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "a2a" / "Dockerfile.worker"
_HARNESS_DIR = _REPO_ROOT / "a2a" / "worker-harness"
_PACKAGE_JSON = _HARNESS_DIR / "package.json"
_PACKAGE_LOCK = _HARNESS_DIR / "package-lock.json"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"
_HARNESS_GO = _REPO_ROOT / "a2a" / "worker-adapter" / "harness.go"
_HARNESS_GITIGNORE = _HARNESS_DIR / ".gitignore"
_A2A_DOCKERIGNORE = _REPO_ROOT / "a2a" / ".dockerignore"
_ROOT_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"

_SDK = "@anthropic-ai/claude-agent-sdk"
# The platform package whose binary DefaultHarnessPath names.
_SDK_LINUX_X64 = "@anthropic-ai/claude-agent-sdk-linux-x64"
_PUBLIC_REGISTRY_PREFIX = "https://registry.npmjs.org/"
# A sha512 digest is 64 bytes: 86 base64 characters and two of padding.
_SHA512_INTEGRITY = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")
_LOCK_ROOT = ""
_HARNESS_DEPENDABOT_DIR = "/a2a/worker-harness"
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_HARNESS_COPY = "a2a/worker-harness/package.json a2a/worker-harness/package-lock.json ./"
# The whole of the image's one package-manager RUN. A flag on `npm ci` (such as
# --omit=optional, which drops the platform package that carries the harness
# binary) or a second install command changes this list and fails the test.
_NPM_RUN_COMMANDS = ["npm ci", "npm cache clean --force"]
_PACKAGE_MANAGER = re.compile(r"\b(npm|npx|pnpm|yarn|corepack)\b")
_SHELL_AND = "&&"
_CONTINUATION = "\\"
_COMMENT = "#"
# A here-document puts shell lines where _instructions expects instructions.
_HEREDOC = re.compile(r"<<-?\s*[\"']?\w")


def _instructions(dockerfile_text):
    """The Dockerfile's instructions in order, as (keyword, arguments) pairs.

    Continuation lines are joined, and comment and blank lines are dropped, as
    the Dockerfile parser does, including inside a continued instruction.
    """
    instructions = []
    pending = []
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(_COMMENT):
            continue
        continued = line.endswith(_CONTINUATION)
        pending.append(line[: -len(_CONTINUATION)] if continued else line)
        if continued:
            continue
        keyword, _, arguments = " ".join(pending).partition(" ")
        instructions.append((keyword.upper(), " ".join(arguments.split())))
        pending = []
    if pending:
        raise ValueError("the Dockerfile ends inside a continued instruction")
    for keyword, arguments in instructions:
        if _HEREDOC.search(arguments):
            raise ValueError(f"_instructions does not read here-documents: {keyword} {arguments}")
    return instructions


def _harness_install_problems(dockerfile_text):
    """What is wrong with how the Dockerfile installs the harness; empty when nothing is."""
    instructions = _instructions(dockerfile_text)
    npm_runs = [
        index
        for index, (keyword, arguments) in enumerate(instructions)
        if keyword == "RUN" and _PACKAGE_MANAGER.search(arguments)
    ]
    if len(npm_runs) != 1:
        return [f"expected one RUN that names a package manager, found {len(npm_runs)}"]
    problems = []
    npm_run = npm_runs[0]
    commands = [command.strip() for command in instructions[npm_run][1].split(_SHELL_AND)]
    if commands != _NPM_RUN_COMMANDS:
        problems.append(f"the npm RUN is {commands}, not {_NPM_RUN_COMMANDS}")
    stage_start = max(
        (index for index, (keyword, _) in enumerate(instructions[:npm_run]) if keyword == "FROM"), default=0
    )
    if ("COPY", _HARNESS_COPY) not in instructions[stage_start:npm_run]:
        problems.append(f"no `COPY {_HARNESS_COPY}` before the npm RUN in its stage")
    return problems


def _lock_entry_problems(packages):
    """Every non-root lock entry that lacks a public registry URL or a sha512 hash."""
    problems = []
    for name, info in packages.items():
        if name == _LOCK_ROOT:
            continue
        if not str(info.get("resolved", "")).startswith(_PUBLIC_REGISTRY_PREFIX):
            problems.append(f"{name}: resolved is {info.get('resolved')!r}")
        if not _SHA512_INTEGRITY.match(str(info.get("integrity", ""))):
            problems.append(f"{name}: integrity is {info.get('integrity')!r}")
    return problems


class WorkerHarnessLockTest(unittest.TestCase):
    def test_the_dockerfile_installs_from_the_lock_with_npm_ci(self):
        self.assertEqual(_harness_install_problems(_DOCKERFILE.read_text()), [])

    def test_the_manifest_pins_the_sdk_exactly(self):
        deps = json.loads(_PACKAGE_JSON.read_text())["dependencies"]
        self.assertEqual(list(deps), [_SDK])
        self.assertRegex(deps[_SDK], _EXACT_VERSION, "a range here lets npm ci and the manifest disagree")

    def test_the_lock_matches_the_manifest_and_carries_the_harness_binary_package(self):
        manifest = json.loads(_PACKAGE_JSON.read_text())
        lock = json.loads(_PACKAGE_LOCK.read_text())
        packages = lock["packages"]
        self.assertEqual(packages[""]["dependencies"], manifest["dependencies"])
        version = manifest["dependencies"][_SDK]
        self.assertEqual(packages[f"node_modules/{_SDK}"]["version"], version)
        self.assertEqual(packages[f"node_modules/{_SDK_LINUX_X64}"]["version"], version)
        self.assertIn(f"/app/node_modules/{_SDK_LINUX_X64}/claude", _HARNESS_GO.read_text())

    def test_every_locked_package_has_a_public_registry_url_and_a_sha512_hash(self):
        # Every entry, with none skipped: a link, bundled or workspace entry has
        # no URL or hash of its own, and the lock holds none of those today.
        packages = json.loads(_PACKAGE_LOCK.read_text())["packages"]
        self.assertGreater(len(packages), 1, "the lock holds no package but the root")
        self.assertEqual(_lock_entry_problems(packages), [])

    def test_dependabot_proposes_bumps_for_the_harness_directory(self):
        updates = yaml.safe_load(_DEPENDABOT.read_text())["updates"]
        harness = [
            u for u in updates if u["package-ecosystem"] == "npm" and u.get("directory") == _HARNESS_DEPENDABOT_DIR
        ]
        self.assertEqual(len(harness), 1, "the harness directory needs exactly one npm entry")
        # Its own group, so an SDK bump is never batched with another directory's.
        self.assertEqual(list(harness[0]["groups"]), ["worker-harness"])

    def test_a_local_install_stays_out_of_git_and_every_build_context(self):
        # A developer who runs npm ci here to check a bump must not commit the
        # tree or send it in a build context: an a2a-context build and the
        # repository-root build of Dockerfile.worker both copy a2a/.
        self.assertIn("node_modules/", _HARNESS_GITIGNORE.read_text().splitlines())
        self.assertIn("worker-harness/node_modules", _A2A_DOCKERIGNORE.read_text().splitlines())
        # A bare `node_modules/` in a .dockerignore matches only the context root.
        self.assertIn("**/node_modules/", _ROOT_DOCKERIGNORE.read_text().splitlines())


class LockEntryCheckTest(unittest.TestCase):
    """_lock_entry_problems names each defect in a changed copy of the lock."""

    def setUp(self):
        self.packages = json.loads(_PACKAGE_LOCK.read_text())["packages"]
        self.name = f"node_modules/{_SDK_LINUX_X64}"

    def _problems_after(self, change):
        packages = copy.deepcopy(self.packages)
        change(packages[self.name])
        return _lock_entry_problems(packages)

    def test_an_entry_without_a_hash_is_a_problem(self):
        problems = self._problems_after(lambda info: info.pop("integrity"))
        self.assertEqual(problems, [f"{self.name}: integrity is None"])

    def test_a_lock_with_no_hash_at_all_is_a_problem_per_entry(self):
        packages = copy.deepcopy(self.packages)
        for name, info in packages.items():
            if name != _LOCK_ROOT:
                del info["integrity"]
        self.assertEqual(len(_lock_entry_problems(packages)), len(packages) - 1)

    def test_a_weaker_or_empty_hash_is_a_problem(self):
        for integrity in ("sha1-2jmj7l5rSw0yVb/vlWAYkK/YBwk=", "sha512-", ""):
            with self.subTest(integrity=integrity):
                problems = self._problems_after(lambda info: info.update(integrity=integrity))
                self.assertEqual(problems, [f"{self.name}: integrity is {integrity!r}"])

    def test_an_entry_without_a_url_is_a_problem(self):
        problems = self._problems_after(lambda info: info.pop("resolved"))
        self.assertEqual(problems, [f"{self.name}: resolved is None"])

    def test_a_url_off_the_public_registry_is_a_problem(self):
        for resolved in (
            "https://registry.example.com/x/-/x-1.0.0.tgz",
            "http://registry.npmjs.org/x/-/x-1.0.0.tgz",
            "https://registry.npmjs.org.example.com/x/-/x-1.0.0.tgz",
            "git+ssh://git@github.com/example/x.git#0123456",
        ):
            with self.subTest(resolved=resolved):
                problems = self._problems_after(lambda info: info.update(resolved=resolved))
                self.assertEqual(problems, [f"{self.name}: resolved is {resolved!r}"])


class HarnessInstallCheckTest(unittest.TestCase):
    """_harness_install_problems reads instructions, in changed copies of the Dockerfile."""

    _NPM_RUN = "RUN npm ci \\\n    && npm cache clean --force\n"
    _COPY = f"COPY {_HARNESS_COPY}\n"

    def setUp(self):
        self.text = _DOCKERFILE.read_text()
        # The changes below replace these lines, so each must be there to replace.
        self.assertEqual(self.text.count(self._NPM_RUN), 1)
        self.assertEqual(self.text.count(self._COPY), 1)

    def _problems_with_run(self, run):
        return _harness_install_problems(self.text.replace(self._NPM_RUN, run + "\n"))

    def test_a_comment_that_names_npm_install_is_not_a_problem(self):
        above = self.text.replace(self._NPM_RUN, "# This used to be `RUN npm install`.\n" + self._NPM_RUN)
        self.assertEqual(_harness_install_problems(above), [])
        inside = "RUN npm ci \\\n    # not npm install\n    && npm cache clean --force"
        self.assertEqual(self._problems_with_run(inside), [])

    def test_a_changed_npm_run_is_a_problem(self):
        for run in (
            "RUN npm ci && npm add left-pad && npm cache clean --force",
            "RUN npm ci --omit=optional && npm cache clean --force",
            "RUN npm ci; npm add left-pad && npm cache clean --force",
            "RUN npm install && npm cache clean --force",
            "RUN npm i && npm cache clean --force",
            "RUN npm ci",
            "RUN npm cache clean --force",
        ):
            with self.subTest(run=run):
                problems = self._problems_with_run(run)
                self.assertEqual(len(problems), 1)
                self.assertIn("the npm RUN is", problems[0])

    def test_a_second_or_missing_package_manager_run_is_a_problem(self):
        for run, count in (
            (self._NPM_RUN + "RUN npm add left-pad", 2),
            (self._NPM_RUN + "RUN npx left-pad", 2),
            (self._NPM_RUN + "RUN yarn add left-pad", 2),
            ("# RUN npm ci && npm cache clean --force", 0),
        ):
            with self.subTest(run=run):
                self.assertEqual(
                    self._problems_with_run(run),
                    [f"expected one RUN that names a package manager, found {count}"],
                )

    def test_a_missing_or_misplaced_copy_of_the_lock_is_a_problem(self):
        expected = [f"no `COPY {_HARNESS_COPY}` before the npm RUN in its stage"]
        build_stage_workdir = "WORKDIR /src\n"
        self.assertEqual(self.text.count(build_stage_workdir), 1)
        without = self.text.replace(self._COPY, "")
        for case, text in (
            ("commented out", self.text.replace(self._COPY, "# " + self._COPY)),
            ("manifest only", self.text.replace(self._COPY, "COPY a2a/worker-harness/package.json ./\n")),
            ("after the RUN", without.replace(self._NPM_RUN, self._NPM_RUN + self._COPY)),
            ("in the build stage", without.replace(build_stage_workdir, build_stage_workdir + self._COPY)),
        ):
            with self.subTest(case=case):
                self.assertEqual(_harness_install_problems(text), expected)

    def test_a_here_document_is_refused_rather_than_misread(self):
        with self.assertRaisesRegex(ValueError, "here-documents"):
            self._problems_with_run("RUN <<EOF\nnpm install\nEOF")


if __name__ == "__main__":
    unittest.main()
