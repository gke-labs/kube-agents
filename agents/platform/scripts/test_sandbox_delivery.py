#!/usr/bin/env python3
"""The sandbox's copy of the shared scripts, against what the agents ask for.

Since #737 the agent's shell runs in a different container, so a SKILL.md line
like `python3 /opt/data/scripts/gitops_workspace.py` resolves in the sandbox
image rather than the agent image. Nothing connects the two: adding a script and
calling it from a skill passes every check in this repository and then fails at
runtime with "No such file or directory", in a pod nobody was looking at.

deploy/sandbox/Dockerfile carries the list, as an allowlist rather than a copy of
the directory — most of agents/platform/scripts/ is agent-pod machinery that
cannot work there. This holds the allowlist to three properties:

  - every shared script an agent is told to run is either on it or deliberately
    stubbed,
  - it is closed under import, so nothing on it fails at its first `import`,
  - nothing on it names an interpreter the sandbox image does not have.

Run: python3 agents/platform/scripts/test_sandbox_delivery.py
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "agents/platform/scripts"
SKILL_SCRIPTS = REPO / "agents/platform/skills"
DOCKERFILE = REPO / "deploy/sandbox/Dockerfile"

# The second staging directory: root-owned, and the one the agent pod's crons
# run over ssh. Everything else here is about what the *model* can run; this is
# about what the trusted login runs, which must not be a file the model owns.
TRUSTED_DIR = "/opt/vcs/libexec/platform"

# Where an agent is told what to run. The cluster files are here because cluster
# profiles run in the same pod as the platform agent and reach the same sandbox —
# cluster_preflight.sh is on the allowlist only because agents/cluster/SOUL.md
# makes it the first command of every kanban task.
INSTRUCTION_GLOBS = (
    "agents/platform/skills/*/SKILL.md",
    "agents/platform/SOUL.md",
    "agents/platform/AGENTS.md",
    "agents/cluster/SOUL.md",
    "agents/cluster/AGENTS.md",
    "agents/cluster/skills/*/SKILL.md",
)

# A shared script named by its *runtime* path: `/opt/data/scripts/x.py` or
# `"$HERMES_HOME"/scripts/x.py`. Both forms tell the model where the file is at
# runtime, whether the sentence goes on to run it or to say what is in it — either
# way the file has to be there.
#
# A repo-relative citation (`scripts/platform_mcp_server.py`, "the source of truth
# for the naming") is not one of those. It points at this checkout, for a reader
# who wants to know how something works, and the sandbox needing a copy does not
# follow. Hence the anchor on the root rather than on `scripts/`.
CALL = re.compile(
    r"(?:/opt/data|\$\{?HERMES_HOME\}?|\$\{?PLATFORM_AGENT_HOME\}?)"
    r"[\"']?/scripts/([A-Za-z0-9_]+\.(?:py|sh))"
)

# What makes a script agent-pod-only, and so a stub rather than a copy. Each of
# these exists in the agent image or on its PVC and has deliberately not been given
# to the sandbox: the `hermes` binary and its profile subcommands, the profiles
# tree, and the SQLite boards and session databases the gateway writes — the
# sandbox holds no database at all, so any sqlite3 call here is reaching for one
# that lives on the other side.
AGENT_POD_RESOURCES = re.compile(
    r"\bhermes\b|HERMES_KANBAN_DB|\bsqlite3\b|/profiles/|state\.db"
)


def dockerfile_paths(dest: str) -> set[str]:
    """Basenames the Dockerfile puts under `dest`.

    Reads the COPY lines rather than a hand-kept duplicate of them, so the
    Dockerfile stays the single place the allowlist is written down. Continuation
    lines are joined first: the allowlist COPY spans seven of them.
    """
    text = DOCKERFILE.read_text()
    joined = re.sub(r"\\\n\s*", " ", text)
    found: set[str] = set()
    for line in joined.splitlines():
        if not line.startswith("COPY ") or dest not in line:
            continue
        for token in line.split():
            if token.startswith("agents/") or token.startswith("deploy/"):
                found.add(Path(token).name)
            elif token.startswith(dest) and not token.endswith("/"):
                # `COPY <src> /opt/defaults/scripts/<renamed>` — the stubs.
                found.add(Path(token).name)
    return found


def baked_scripts() -> set[str]:
    """The real copies: what a skill can actually run in the sandbox.

    The stub paths are excluded rather than counted. They exist so the failure
    explains itself, and treating one as delivered would let a script that imports
    it pass a closure check it should fail.
    """
    return dockerfile_paths("/opt/defaults/scripts") - stubbed_scripts() - {
        "agent-pod-only-stub.py"
    }


def stubbed_scripts() -> set[str]:
    """The paths the stub is copied to: present, and refusing to run."""
    text = re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text())
    return {
        Path(m.group(1)).name
        for m in re.finditer(
            r"agent-pod-only-stub\.py\s+(/opt/defaults/scripts/\S+)", text
        )
    }


def forwarded_paths() -> dict[str, str]:
    """Every script an agent-pod cron runs in the sandbox as `hermes`.

    Imported rather than listed as strings so the test moves when the constant
    does. A third crossing added later belongs here; nothing else in the suite
    will notice it otherwise.
    """
    # resolver.py lives in its skill, not in `scripts/`, so its directory is not
    # already on the path the way `forge`'s is.
    sys.path.insert(0, str(SKILL_SCRIPTS / "github-issue-resolver/scripts"))
    try:
        import forge
        import resolver
    finally:
        sys.path.pop(0)

    return {
        "forge": forge.SANDBOX_FORGE,
        "resolver": resolver.SANDBOX_RESOLVER,
    }


# Where the agent pod's copy of a shared script looks for its siblings. Both are
# chowned to `agent` at boot, so neither may be on the import path of the copy
# the sandbox runs as `hermes`.
AGENT_WRITABLE = ("/opt/data", "/opt/defaults")


def staged_source(name: str) -> Path | None:
    """The file in this checkout that the Dockerfile stages under `TRUSTED_DIR`."""
    for path in (SCRIPTS / name, *SKILL_SCRIPTS.glob(f"*/scripts/{name}")):
        if path.exists():
            return path
    return None


def unguarded_path_appends(path: Path) -> list[str]:
    """Additions of an agent-writable directory to `sys.path` that always run.

    Guarded means inside an `if` that asks where this file is. The agent pod's
    copy needs those directories to find its siblings; the trusted copy has
    `sys.path[0]` and a closure staged beside it, and must not carry a directory
    uid 1000 can write even as a fallback behind site-packages.
    """
    tree = ast.parse(path.read_text())
    guarded = {
        id(node)
        for branch in ast.walk(tree)
        if isinstance(branch, ast.If)
        and "TRUSTED_CLOSURE" in ast.unparse(branch.test)
        for node in ast.walk(branch)
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in guarded:
            continue
        if ast.unparse(node.func) not in ("sys.path.append", "sys.path.insert"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and str(
                argument.value
            ).startswith(AGENT_WRITABLE):
                found.append(argument.value)
    return found


def local_imports(path: Path, universe: set[str]) -> set[str]:
    """Sibling modules `path` imports, including the ones inside functions.

    ast rather than a regex: half of these are deferred imports in a function
    body, and `from github_token_refresh import refresh_git_credentials` and
    `import gitops_workspace` are different nodes.
    """
    if path.suffix != ".py":
        return set()
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {f"{m}.py" for m in found if f"{m}.py" in universe}


class SandboxDelivery(unittest.TestCase):
    def setUp(self):
        self.baked = baked_scripts()
        self.stubbed = stubbed_scripts()
        self.shared = {p.name for p in SCRIPTS.iterdir() if p.suffix in (".py", ".sh")}

    def test_every_script_an_agent_is_told_to_run_reaches_the_sandbox(self):
        """Or is stubbed, which is the deliberate answer for the agent-pod-only ones.

        The failure this prevents is silent on both sides. Nothing in the agent
        image changes when a skill starts calling a new script, and the sandbox
        image is built from a list that does not know the skill exists.
        """
        wanted: dict[str, list[str]] = {}
        for pattern in INSTRUCTION_GLOBS:
            for doc in REPO.glob(pattern):
                for name in CALL.findall(doc.read_text()):
                    if name in self.shared:
                        wanted.setdefault(name, []).append(
                            str(doc.relative_to(REPO))
                        )

        self.assertTrue(wanted, "found no script calls at all — the regex has rotted")

        missing = {
            name: sorted(set(where))
            for name, where in wanted.items()
            if name not in self.baked and name not in self.stubbed
        }
        self.assertEqual(
            {},
            missing,
            "these shared scripts are named in an agent's instructions but do not "
            "reach the sandbox, where the shell now runs. Add each to the allowlist "
            "COPY in deploy/sandbox/Dockerfile, or — if it needs the hermes binary, "
            "the profiles tree, or the agent pod's PVC — to the stub list beside it: "
            f"{missing}",
        )

    def test_the_allowlist_is_closed_under_import(self):
        """A script whose import is absent fails on its first line, in the sandbox.

        Checked transitively: pr_conversation imports pr_triggers, which imports
        forge, and only the first of those is obvious from the skill.
        """
        gaps: dict[str, set[str]] = {}
        for name in sorted(self.baked):
            path = SCRIPTS / name
            if not path.exists():
                continue
            for dep in local_imports(path, self.shared):
                if dep not in self.baked and dep not in self.stubbed:
                    gaps.setdefault(name, set()).add(dep)
        self.assertEqual(
            {},
            {k: sorted(v) for k, v in gaps.items()},
            "these baked scripts import shared modules the sandbox does not have",
        )

    def test_the_skill_scripts_imports_are_on_the_allowlist_too(self):
        """The skill trees are copied whole, so their imports are the real demand.

        A skill's own scripts/ arrives with it. What does not is anything it
        reaches for in agents/platform/scripts/ — which is where the allowlist came
        from in the first place, and where it drifts.
        """
        gaps: dict[str, set[str]] = {}
        for path in REPO.glob("agents/platform/skills/*/scripts/*.py"):
            if path.name.startswith("test_"):
                continue
            for dep in local_imports(path, self.shared):
                if dep not in self.baked and dep not in self.stubbed:
                    gaps.setdefault(
                        str(path.relative_to(REPO)), set()
                    ).add(dep)
        self.assertEqual(
            {},
            {k: sorted(v) for k, v in gaps.items()},
            "these skill scripts import shared modules the sandbox does not have",
        )

    def test_every_forwarded_script_is_the_root_owned_copy(self):
        """A cron forwards as `hermes`, so it must not run a file uid 1000 owns.

        /opt/data is the agent's volume and an edit there survives until the next
        restart -- the smoke test plants one deliberately. Forwarding to that copy
        would run the model's code with the cron's credential.

        Both entry points are checked, not just the first one found: forge.py's
        hop was moved here and resolver.py's was left behind under /opt/data for
        a review round, which is the failure this shape of test exists to stop
        repeating.
        """
        staged = dockerfile_paths(TRUSTED_DIR)
        for name, path in forwarded_paths().items():
            with self.subTest(entry_point=name):
                self.assertTrue(
                    path.startswith(TRUSTED_DIR + "/"),
                    f"the forwarded path {path} is not under {TRUSTED_DIR}",
                )
                self.assertIn(Path(path).name, staged)

    def test_the_root_owned_copy_is_closed_under_import(self):
        """A module missing here is not an error: it is found under /opt/data.

        `sys.path[0]` is the script's own directory, so the fallback is silent
        and it is the agent-owned copy -- the boundary gone with nothing to show
        for it. deploy/sandbox/trusted-closure-guard.py catches this at build
        time by reading `__file__` off what actually loaded; this catches it at
        review time, on a tree nobody has to build.
        """
        staged = dockerfile_paths(TRUSTED_DIR)
        universe = self.shared | {Path(p).name for p in forwarded_paths().values()}
        seen: set[str] = set()
        pending = {Path(p).name for p in forwarded_paths().values()}
        while pending:
            name = pending.pop()
            seen.add(name)
            for path in (SCRIPTS / name, *SKILL_SCRIPTS.glob(f"*/scripts/{name}")):
                if path.exists():
                    pending |= local_imports(path, universe) - seen
                    break
        self.assertEqual(
            set(),
            seen - staged,
            f"these modules are imported by a forwarded script but not staged "
            f"in {TRUSTED_DIR}",
        )

    def test_no_staged_script_puts_an_agent_writable_directory_on_its_path(self):
        """Staging the closure root-owned is undone if the path reaches back out.

        `/opt/defaults/scripts` and `/opt/data/scripts` are appended, so they are
        reached only for a module nothing else supplies -- which is exactly the
        module a closure gap leaves open, and exactly the file uid 1000 gets to
        write. The build guard proves the closure is complete for what loads at
        import time; this is what keeps the gap from being fillable at all.
        """
        offenders = {}
        for name in sorted(dockerfile_paths(TRUSTED_DIR)):
            path = staged_source(name)
            if path is None:
                continue
            appended = unguarded_path_appends(path)
            if appended:
                offenders[name] = appended
        self.assertEqual(
            {},
            offenders,
            f"these are staged in {TRUSTED_DIR} and add an agent-writable "
            "directory to sys.path unconditionally; gate it on the file's own "
            "location, the way vcs_client.py does",
        )

    def test_nothing_baked_names_the_agent_images_interpreter(self):
        """`#!/opt/hermes/.venv/bin/python3` resolves in one image and not the other.

        bash reports a missing interpreter as "No such file or directory" against
        the *script's* path, so the error names a file that is sitting right there.
        python:3.11-slim has no /usr/bin/python3 either, so there is nothing to fall
        through to — the shebang has to be /usr/bin/env.
        """
        offenders = []
        candidates = [SCRIPTS / n for n in self.baked]
        candidates += [
            p
            for p in REPO.glob("agents/platform/skills/*/scripts/*")
            if p.is_file() and not p.name.startswith("test_")
        ]
        for path in candidates:
            if not path.exists():
                continue
            first = path.read_bytes().split(b"\n", 1)[0]
            if first.startswith(b"#!") and b"/opt/hermes/" in first:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(
            [],
            sorted(offenders),
            "these run in the sandbox, which has no /opt/hermes/.venv; "
            "use #!/usr/bin/env python3",
        )

    def test_the_stubs_are_agent_pod_only_for_a_reason(self):
        """A stub is a capability the sandbox took away, so keep the list short.

        Not a guess at what belongs: each of these names something the sandbox was
        deliberately not given. If one stops needing it, it should move to the
        allowlist rather than stay stubbed — a stub is a capability the boundary
        took away, and the list only earns its place while every entry has to be
        on it.
        """
        self.assertTrue(self.stubbed, "expected at least one stubbed script")
        for name in sorted(self.stubbed):
            path = SCRIPTS / name
            self.assertTrue(path.exists(), f"{name} is stubbed but does not exist")
            self.assertRegex(
                path.read_text(),
                AGENT_POD_RESOURCES,
                f"{name} is stubbed as agent-pod-only but names none of the "
                "resources that make a script agent-pod-only; if it can run in the "
                "sandbox, bake it instead of stubbing it",
            )
            self.assertNotIn(
                name,
                self.baked,
                f"{name} is both baked and stubbed — the later COPY wins and which "
                "one that is depends on the order of two lines",
            )


if __name__ == "__main__":
    unittest.main()
