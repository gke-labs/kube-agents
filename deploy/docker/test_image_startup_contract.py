"""Contract tests for what the agent image guarantees about its own startup.

Run: python3 -m unittest discover -s deploy/docker -p 'test_*.py'

Three guarantees are pinned here, all invisible from any single source file:

  * the process the entrypoint execs starts inside the shared workspace, so the
    credential-proxy shims are not refused before they run;
  * the vendored Python tree carries a bytecode cache, since the runtime cannot
    build one; and
  * the event watcher's emergency stop reads the variable the operator writes,
    and reads it the way the CRD promises.

The entrypoint assertions run the real script rather than reading it. Every
step it takes is gated on an absolute image path (/opt/hermes, /opt/defaults)
that does not exist on a test machine, so it walks its guards and reaches the
exec with no image present — which makes the honest test available, and a
regex over the file the weaker substitute.
"""

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "deploy" / "shared" / "docker-entrypoint.sh"
START_SERVICES = REPO_ROOT / "deploy" / "shared" / "start-services.sh"
DOCKERFILE = REPO_ROOT / "deploy" / "docker" / "Dockerfile"
MANIFESTS_GO = (
    REPO_ROOT
    / "k8s-operator"
    / "internal"
    / "controller"
    / "platformagent_manifests.go"
)

# The default both sides have to agree on. The proxy refuses a cwd outside
# CREDENTIAL_PROXY_WORKSPACE_ROOT, so if the entrypoint's default home and the
# operator's diverge, the agent starts in a directory its own shims reject.
DEFAULT_AGENT_HOME = "/opt/data"


def stage_body(dockerfile_text: str, target: str) -> str:
    """The Dockerfile text belonging to one `FROM … AS <target>` stage."""
    stages = re.split(r"^FROM .*? AS (\S+)\s*$", dockerfile_text, flags=re.M)
    # re.split with one group yields [pre, name, body, name, body, ...].
    for name, body in zip(stages[1::2], stages[2::2]):
        if name == target:
            return body
    raise AssertionError(f"no `FROM … AS {target}` stage in the Dockerfile")


def platform_stage(dockerfile_text: str) -> str:
    """The Dockerfile text belonging to the `platform` target."""
    return stage_body(dockerfile_text, "platform")


def joined_instructions(dockerfile_text: str) -> list:
    """Every instruction in the text, line continuations joined into one line.

    A shell command in a Dockerfile is wrapped across physical lines with
    trailing backslashes, and the parts that matter to a caller here — an
    interpreter and its `||` fallback, a `cp` and the `chmod` that repairs it —
    sit on different ones. Comment lines are dropped, including inside a
    continuation, which is how BuildKit reads them: the instruction continues
    around the comment rather than ending at it.
    """
    instructions = []
    current = []
    for line in dockerfile_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        current.append(line.rstrip().removesuffix("\\"))
        if not line.rstrip().endswith("\\"):
            instructions.append(" ".join(current))
            current = []
    if current:
        instructions.append(" ".join(current))
    return instructions


# Dockerfile keywords are case-insensitive. Upper-casing them on the way out
# means a lower-case `copy` cannot slip past a caller that matches on "COPY".
DOCKERFILE_KEYWORDS = frozenset(
    """ADD ARG CMD COPY ENTRYPOINT ENV EXPOSE FROM HEALTHCHECK LABEL MAINTAINER
    ONBUILD RUN SHELL STOPSIGNAL USER VOLUME WORKDIR""".split()
)


def keyed_instructions(dockerfile_text: str) -> list:
    """(keyword, argument) per instruction, keyword upper-cased."""
    out = []
    for instruction in joined_instructions(dockerfile_text):
        parts = instruction.split(None, 1)
        if len(parts) == 2 and parts[0].upper() in DOCKERFILE_KEYWORDS:
            out.append((parts[0].upper(), parts[1]))
    return out


class EntrypointStartsInsideTheWorkspaceTest(unittest.TestCase):
    """The exec'd process must not inherit a cwd the credential proxy rejects.

    The image inherits WORKDIR /opt/hermes from the upstream base. Every
    credentialed CLI (kubectl, gcloud, gh, git) is a PATH shim that posts
    os.getcwd() with each request, and the proxy refuses any cwd outside the
    workspace root — so launched from /opt/hermes, even `kubectl version
    --client` fails before it runs. The cwd is the only lever that reaches every
    caller: Hermes resolves the terminal and execute_code directories from a
    ladder ending at os.getcwd(), the local backend overwrites any configured
    terminal.cwd with it, and kanban workers inherit it as child processes.
    """

    def run_entrypoint(self, home: Path, start_in: Path):
        """Run the entrypoint from `start_in` and return the exec'd cwd."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PLATFORM_AGENT_HOME": str(home),
        }
        result = subprocess.run(
            ["sh", str(ENTRYPOINT), "sh", "-c", "pwd"],
            cwd=str(start_in),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode, 0, f"entrypoint failed:\n{result.stderr}"
        )
        return result.stdout.strip().splitlines()[-1]

    def test_the_exec_d_process_starts_in_the_agent_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            elsewhere = Path(tmp) / "elsewhere"
            home.mkdir()
            elsewhere.mkdir()
            # `elsewhere` stands in for /opt/hermes: a real directory outside
            # the workspace, which is exactly where the container would start.
            self.assertEqual(
                Path(self.run_entrypoint(home, elsewhere)).resolve(),
                home.resolve(),
            )

    def test_it_does_not_simply_inherit_the_starting_directory(self):
        # The pre-fix behaviour, stated as its own assertion so that a change
        # which quietly drops the `cd` fails here with an obvious message
        # rather than only in the test above.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            elsewhere = Path(tmp) / "elsewhere"
            home.mkdir()
            elsewhere.mkdir()
            self.assertNotEqual(
                Path(self.run_entrypoint(home, elsewhere)).resolve(),
                elsewhere.resolve(),
            )

    def test_the_change_of_directory_cannot_kill_the_container(self):
        # The script runs under `set -e`, so an unguarded `cd` to a missing
        # directory would abort the container outright. A pod that cannot reach
        # its workspace should still come up and say so.
        text = ENTRYPOINT.read_text()
        opener = 'if ! cd "$TARGET_DIR"; then'
        self.assertIn(
            opener,
            text,
            # Named rather than dumped: the script is 300 lines, and an
            # assertion that prints all of them buries what it is asking for.
            f"docker-entrypoint.sh does not guard its cd with `{opener}`",
        )
        self.assertIn(
            "WARN",
            text.split(opener, 1)[1].split("fi", 1)[0],
            "the guarded cd fails silently; a broken mount should say so",
        )

    def test_the_default_home_is_the_one_the_operator_uses(self):
        # The two are set independently — the entrypoint from its own default,
        # CREDENTIAL_PROXY_WORKSPACE_ROOT by the operator — and the fix is only
        # correct while they agree.
        self.assertIn(
            f'TARGET_DIR="${{PLATFORM_AGENT_HOME:-{DEFAULT_AGENT_HOME}}}"',
            ENTRYPOINT.read_text(),
        )
        go = MANIFESTS_GO.read_text()
        self.assertIn(f'homeDir := "{DEFAULT_AGENT_HOME}"', go)
        self.assertRegex(
            go,
            r'Name:\s*"CREDENTIAL_PROXY_WORKSPACE_ROOT",\s*Value:\s*homeDir',
        )


class EventWatcherEmergencyStopTest(unittest.TestCase):
    """The red button that stops cluster event ingestion mid-storm.

    `spec.harness.eventWatcher.enabled: false` reaches the sidecar as one
    environment variable and is acted on by one `case` statement in
    start-services.sh. Neither end can be checked from the other: the operator's
    tests never read the script, the script is never run outside the image, and
    a container with no watcher in it stays Ready and looks healthy. So the
    failure mode is silent in both directions — a rename leaves the button
    pressing nothing, and a parsing slip stops event ingestion on installs that
    never asked for it.

    The gate is extracted from the real script and run, rather than pattern
    matched: what matters is the answer bash gives for a given value, and the
    `case` patterns are shell globs whose behaviour is easy to misread.
    """

    def setUp(self):
        self.script = START_SERVICES.read_text()
        # The variable name as the script itself spells it, so a rename on
        # either side of the contract fails rather than half-applying.
        match = re.search(
            r'case "\$\{([A-Za-z_][A-Za-z0-9_]*):-true\}" in', self.script
        )
        self.assertIsNotNone(
            match,
            "start-services.sh no longer switches on a defaulted "
            "EVENT_WATCHER_ENABLED-style variable; the emergency stop in the "
            "PlatformAgent CRD now reaches nothing",
        )
        self.env_var = match.group(1)

        body = re.search(
            r"^event_watcher_disabled\(\) \{.*?^\}$",
            self.script,
            flags=re.M | re.S,
        )
        self.assertIsNotNone(
            body, "start-services.sh has no event_watcher_disabled function"
        )
        self.gate = body.group(0)

    def ask_gate(self, value):
        """Run the real gate for `value` (None = unset) and return its verdict."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if value is not None:
            env[self.env_var] = value
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"set -euo pipefail\n{self.gate}\n"
                "if event_watcher_disabled; then echo DISABLED; "
                "else echo ENABLED; fi",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_an_unset_variable_keeps_the_watcher_running(self):
        # Every install that predates the field, and every one that never sets
        # it. Going quiet on upgrade would be the worst possible default: no
        # incident is reported and nothing says why.
        self.assertEqual(self.ask_gate(None), "ENABLED")

    def test_false_stops_the_watcher(self):
        for value in ("false", "False", "FALSE"):
            with self.subTest(value=value):
                self.assertEqual(self.ask_gate(value), "DISABLED")

    def test_true_is_what_the_operator_writes_on_a_normal_install(self):
        # strconv.FormatBool emits exactly this, so it is the value present on
        # the overwhelming majority of pods.
        self.assertEqual(self.ask_gate("true"), "ENABLED")

    def test_an_unrecognised_value_fails_towards_watching(self):
        # Deliberate asymmetry. A typo that silently stops event ingestion is
        # invisible; one that leaves the watcher running shows up with the next
        # event. So the ambiguous case keeps watching — and says so.
        self.assertEqual(self.ask_gate("flase"), "ENABLED")
        self.assertEqual(self.ask_gate(""), "ENABLED")

    def test_the_gate_runs_before_the_watcher_is_launched(self):
        # A gate placed after the binary starts would disable nothing.
        start = self.script.index("start_event_watcher() {")
        launched = self.script.index("/usr/local/bin/k8s-event-watcher", start)
        gated = self.script.index("if event_watcher_disabled; then", start)
        self.assertLess(
            gated,
            launched,
            "start_event_watcher launches the watcher before consulting the "
            f"{self.env_var} gate",
        )

    def test_the_disabled_path_says_so_loudly(self):
        # The only signal there is. The readiness probe covers the credential
        # proxy alone, so a pod with no watcher is externally identical to a
        # healthy one; the log line has to name the consequence and the way
        # back, not just the flag.
        disabled_branch = self.script[
            self.script.index("if event_watcher_disabled; then") :
        ].split("\n  fi\n", 1)[0]
        self.assertIn("DISABLED", disabled_branch)
        self.assertIn("spec.harness.eventWatcher.enabled", disabled_branch)

    def test_the_operator_writes_the_variable_the_script_reads(self):
        # The other half of the contract, and the half `go test` cannot see.
        self.assertRegex(
            MANIFESTS_GO.read_text(),
            rf'Name:\s*"{self.env_var}"',
            f"the operator never sets {self.env_var}, so the CRD's "
            "eventWatcher.enabled field controls nothing",
        )


class BytecodeIsBakedAtBuildTimeTest(unittest.TestCase):
    """The image must ship the cache the runtime is unable to produce.

    The base image sets PYTHONDONTWRITEBYTECODE=1 and /opt/hermes is read-only
    to the pod's uid, so nothing can write a __pycache__ at runtime: a live pod
    shows 9366 .py files under /opt/hermes and 0 .pyc. Every Python process the
    container starts therefore re-parses the whole tree from source.
    """

    def setUp(self):
        self.stage = platform_stage(DOCKERFILE.read_text())

    def compileall_command(self):
        """The whole RUN instruction that precompiles, continuations joined."""
        for instruction in joined_instructions(self.stage):
            if "compileall" in instruction:
                return instruction
        self.fail("the platform stage does not precompile /opt/hermes")

    def test_the_platform_stage_precompiles_the_vendored_tree(self):
        self.assertIn("/opt/hermes", self.compileall_command())

    def test_it_compiles_with_the_interpreter_that_will_import(self):
        # A .pyc is only loaded by an interpreter whose magic tag matches the
        # one that wrote it, so a bare `python3` here would bake a cache the
        # venv silently ignores.
        self.assertIn("/opt/hermes/.venv/bin/python3", self.compileall_command())

    def test_it_does_not_ask_for_optimized_bytecode(self):
        # The runtime sets neither PYTHONOPTIMIZE nor -O, so it looks for the
        # plain .cpython-NNN.pyc. An `-o` here would write .opt-N files that
        # nothing loads.
        self.assertNotRegex(
            self.compileall_command(), r"\s-o\b|\s--optimize\b"
        )

    def test_a_module_that_will_not_compile_does_not_break_the_build(self):
        # Large vendored trees carry the odd file that cannot be compiled. The
        # cache is an optimisation; losing part of it is today's behaviour, and
        # not a reason to fail the image.
        self.assertIn("||", self.compileall_command())


class BytecodePremiseTest(unittest.TestCase):
    """The interpreter behaviour the build step depends on.

    Baking the cache only helps if PYTHONDONTWRITEBYTECODE blocks *writing* and
    not *reading*, and only works if compileall ignores the flag. Both are
    documented CPython behaviour rather than anything this repo controls, which
    is exactly why they are worth pinning: if a future base image changes
    either, the Dockerfile step becomes a silent no-op.
    """

    def setUp(self):
        # Some distributions patch cache_from_source to redirect __pycache__
        # out of the source tree — macOS's system Python writes to
        # ~/Library/Caches instead. Such an interpreter cannot answer a
        # question about in-tree bytecode, and quietly passing would be worse
        # than not running.
        probe = importlib.util.cache_from_source("/tmp/probe.py")
        if not probe.startswith("/tmp/"):
            self.skipTest(
                f"{sys.executable} redirects the bytecode cache to {probe}; "
                "the container's interpreter does not"
            )
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = Path(self.tmp) / "pkg"
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("")
        (self.pkg / "mod.py").write_text("X = 1\n")

    def compileall(self):
        return subprocess.run(
            [sys.executable, "-m", "compileall", "-q", self.tmp],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_compileall_writes_a_cache_even_when_writes_are_suppressed(self):
        result = self.compileall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            list(self.pkg.glob("__pycache__/mod.*.pyc")),
            "compileall honoured PYTHONDONTWRITEBYTECODE and wrote nothing; "
            "the Dockerfile step would bake no cache at all",
        )

    def test_an_existing_cache_is_read_even_when_writes_are_suppressed(self):
        self.assertEqual(self.compileall().returncode, 0)

        # Rewrite the source to a different value, keeping its length and mtime
        # so the cache still validates. If the import returns the OLD value,
        # the .pyc was used; if the new one, the cache was ignored.
        source = self.pkg / "mod.py"
        stat = source.stat()
        source.write_text("X = 2\n")
        os.utime(source, (stat.st_atime, stat.st_mtime))

        result = subprocess.run(
            [sys.executable, "-c", "from pkg import mod; print(mod.X)"],
            cwd=self.tmp,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "1",
            "the baked cache was ignored and the source recompiled",
        )

    def test_a_stale_cache_is_rejected_rather_than_trusted(self):
        # The other half of the safety argument: the baked .pyc is validated
        # against its source, so it cannot serve stale code if the tree is
        # ever rebuilt on top of it.
        self.assertEqual(self.compileall().returncode, 0)
        source = self.pkg / "mod.py"
        source.write_text("X = 99\n")  # mtime moves this time.

        result = subprocess.run(
            [sys.executable, "-c", "from pkg import mod; print(mod.X)"],
            cwd=self.tmp,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.stdout.strip(), "99")


class SkillProvenanceContractTest(unittest.TestCase):
    """What the build writes and what the entrypoint checks have to stay one thing.

    The manifest is produced by `sha256sum` in the Dockerfile and read by
    verify_skills_provenance.py at boot, and neither file imports the other.
    Every assertion below is a way the two can drift apart while both still
    look correct on their own — and each drift fails somewhere far from here:
    a tree the build stopped covering verifies clean forever, a tree the build
    still covers but the entrypoint stopped checking does the same, and an
    exclusion the two disagree about crash-loops every pod of a perfectly good
    image.
    """

    TREES = (
        "/opt/hermes/skills",
        "/opt/platform-template/skills",
        "/opt/cluster-template/skills",
    )

    def setUp(self):
        self.stage = platform_stage(DOCKERFILE.read_text())
        self.entrypoint = ENTRYPOINT.read_text()
        spec = importlib.util.spec_from_file_location(
            "verify_skills_provenance",
            REPO_ROOT / "agents" / "platform" / "scripts" / "verify_skills_provenance.py",
        )
        self.vsp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.vsp)

    def generation_block(self):
        """The Dockerfile RUN that writes the manifests, continuations joined."""
        for instruction in joined_instructions(self.stage):
            if "sha256sum" in instruction:
                return instruction
        self.fail("the platform stage does not generate a skill manifest")

    def test_the_build_and_the_verifier_agree_on_the_manifest_name(self):
        # The manifest sits inside the tree it describes, so both sides have to
        # leave the same filename out of its own checksums. Disagree, and the
        # verifier reports the manifest as an untracked file on every boot.
        self.assertIn(self.vsp.MANIFEST_NAME, self.generation_block())

    def test_every_tree_the_build_covers_is_checked_at_boot(self):
        block = self.generation_block()
        for tree in self.TREES:
            with self.subTest(tree=tree):
                self.assertIn(tree, block)
                self.assertIn(tree, self.entrypoint)

    def test_bytecode_is_compiled_before_it_is_hashed(self):
        # `compileall /opt/hermes` writes __pycache__ under /opt/hermes/skills,
        # so its position relative to `sha256sum` decides whether bytecode is
        # inside the manifest or an ordering accident. It runs in this same RUN
        # and ahead of the manifest loop, which is what makes covering it
        # deterministic. Move it back out to a RUN of its own, or after the
        # loop, and every boot of a correct image fails closed on files the
        # manifest never saw.
        block = self.generation_block()
        self.assertIn("compileall", block)
        self.assertLess(block.index("compileall"), block.index("sha256sum"))

    def test_bytecode_is_not_carved_out_of_the_manifest(self):
        # CPython's default invalidation is source mtime plus size, not a
        # content hash, and nothing here passes --invalidation-mode
        # checked-hash. A .pyc is therefore an independent artifact: rewrite one
        # under a preserved mtime and the interpreter runs it. An exclusion is
        # also a name a symlink can be given, which is why neither side has one.
        block = self.generation_block()
        self.assertNotIn("__pycache__", block)
        self.assertNotIn(".pyc", block)
        for gone in ("EXCLUDED_DIRS", "EXCLUDED_SUFFIXES", "is_excluded"):
            with self.subTest(name=gone):
                self.assertFalse(hasattr(self.vsp, gone))

    def test_the_generated_manifest_is_checked_for_completeness(self):
        # `find … | sort > manifest` exits with sort's status, and no POSIX sh
        # has pipefail, so a find that died half-way ships a short manifest from
        # a build that exited 0 — and that manifest then verifies clean.
        self.assertRegex(self.generation_block(), r"wc -l")

    def test_a_symlinked_tree_breaks_the_build_and_not_the_pod(self):
        # `find -type f` tests the link rather than its target, so a symlink is
        # never in the manifest, and the verifier refuses every one it finds.
        # The two only stay consistent if a base image that starts shipping
        # links is caught in a red build instead of in a pod that will not start.
        self.assertIn("-type l", self.generation_block())

    def test_the_trees_are_taken_away_from_the_runtime_user(self):
        # The barrier the boot check cannot be: the agent runs as uid 10000, so
        # a tree it still owns can be rewritten between the check and the next
        # one. Losing this line leaves detection with nothing behind it.
        self.assertIn("chown -R root:root", self.generation_block())

    def test_the_barrier_covers_the_personas_and_not_just_the_skills(self):
        # SOUL.md, AGENTS.md, CAPABILITIES.md, governance/, cron/ and config.yaml
        # sit beside skills/ in the same templates and are force-synced into every
        # profile by entrypoint step 2.6. Narrowing the chown back to the skills
        # subdirectories would leave the more load-bearing half of the image's
        # prompt material writable by the uid it is meant to be protected from.
        #
        # /opt/hermes/plugins, /opt/defaults/scripts and /opt/chat-template are in
        # the same list for the same reason and are covered by no manifest, so this
        # assertion is the only thing holding them: the first is Python imported
        # into the agent's own process; the second holds the checker for the
        # manifests generated just above, and a checker the checked party can
        # rewrite reports whatever it is told to; the third is the config template
        # entrypoint step 2d back-fills absent keys into the live default profile
        # from, on every boot, so a key added to the image copy installs itself.
        #
        # The barrier is allowed to be applied in more than one RUN of the stage:
        # the base plugin tree is chowned in the stage's FIRST RUN so its ~130MB
        # copy-up layer caches across template edits, with the repo's staged
        # plugins arriving root-owned through the final RUN's cp -a. What this
        # test holds is that every root is behind SOME root chown in the stage,
        # not where that chown sits.
        code = "\n".join(
            line for line in self.stage.splitlines() if not line.lstrip().startswith("#")
        )
        chowns = re.findall(r"chown -R root:root ([^;\n]*)", code)
        self.assertTrue(chowns, "the platform stage no longer chowns anything to root")
        covered = " ".join(chowns)
        for root in (
            "/opt/hermes/skills",
            "/opt/hermes/plugins",
            "/opt/platform-template",
            "/opt/cluster-template",
            "/opt/chat-template",
            "/opt/defaults/scripts",
        ):
            with self.subTest(root=root):
                self.assertRegex(covered, rf"{re.escape(root)}(\s|$)")

    def test_the_barrier_reaches_the_sidecars_copy_of_the_shared_scripts(self):
        # /opt/defaults/scripts exists twice. The platform stage's copy is
        # root-owned by the chown above; the credential-proxy stage builds FROM
        # agent-base and fills its own, so nothing above reaches it. Leaving that
        # one hermes-owned would put the weaker copy in the container holding the
        # credentials — credential_proxy.py, github_token_refresh.py and
        # gke_endpoint.py all run out of it as uid 10000 — and would quietly
        # falsify the stage comment claiming the two directories are identical.
        sidecar = stage_body(DOCKERFILE.read_text(), "credential-proxy")
        copies = re.findall(
            r"^COPY((?:\s+--\S+)*)((?:[^\n]*\\\n)*[^\n]*)$", sidecar, flags=re.M
        )
        into_scripts = [
            flags for flags, body in copies if "/opt/defaults/scripts" in body
        ]
        self.assertTrue(
            into_scripts, "no COPY into /opt/defaults/scripts in credential-proxy"
        )
        for flags in into_scripts:
            self.assertIn("--chown=root:root", flags)

    def test_a_tree_missing_at_build_time_fails_the_build(self):
        # The asymmetry that would otherwise point the wrong way: the boot check
        # refuses to start a pod whose tree has no manifest beside it, so a build
        # that quietly skipped a missing tree would ship a green image that
        # crash-loops the fleet. Both halves have to be fail-closed.
        block = self.generation_block()
        self.assertNotIn('[ -d "$d" ] || continue', block)
        existence_check = block[block.index('[ -d "$d" ]') : block.index("-type l")]
        self.assertIn("exit 1", existence_check)

    def test_verification_precedes_the_copy_onto_the_pvc(self):
        # A tree that fails must not have reached a profile first. Step 2's bulk
        # copy is the first thing that spreads any of it.
        check = self.entrypoint.index("verify_skills_provenance.py")
        copy = self.entrypoint.index("cp -ru /opt/defaults/.")
        self.assertLess(check, copy)

    def test_the_boot_check_refuses_to_start_rather_than_warning(self):
        # Every other step in the entrypoint degrades with a WARN. This one must
        # not: continuing means loading prompt material nobody can account for.
        step = self.entrypoint[self.entrypoint.index("SKILL_PROVENANCE_SCRIPT="):]
        step = step[: step.index("\n# 1.6 ")]
        self.assertIn("exit 1", step)
        self.assertNotIn("WARN", step)

    def test_the_boot_check_runs_under_the_venv_interpreter(self):
        # A bare `python3` is not guaranteed to exist in the image, and the one
        # the runtime actually uses is the venv's.
        step = self.entrypoint[self.entrypoint.index("SKILL_PROVENANCE_SCRIPT="):]
        step = step[: step.index("\n# 1.6 ")]
        self.assertIn('"$INSTALL_DIR/.venv/bin/python3"', step)

    def test_the_manifest_mode_lets_a_profile_be_overlaid_twice(self):
        # The manifest sits inside the tree it describes, so every copy of that
        # tree carries it onto the PVC — and both copies preserve mode while
        # neither can preserve ownership. A read-only manifest therefore lands
        # owned by the runtime uid with no write bit, and the next overlay of
        # that profile raises out of profile_scaffold.overlay_template, dropping
        # every item after `skills`. The mode is taken from the Dockerfile rather
        # than assumed, so re-introducing `chmod 444` fails here.
        mode = re.search(
            rf"chmod (\d+) \"\$d/{re.escape(self.vsp.MANIFEST_NAME)}\"", self.generation_block()
        )
        self.assertIsNotNone(mode, "the build no longer sets an explicit manifest mode")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "skills"
            (source / "one").mkdir(parents=True)
            (source / "one" / "SKILL.md").write_text("---\nname: one\n---\n")
            manifest = source / self.vsp.MANIFEST_NAME
            manifest.write_text("0  ./one/SKILL.md\n")
            manifest.chmod(int(mode.group(1), 8))

            profile = Path(tmp) / "profile" / "skills"
            shutil.copytree(source, profile, dirs_exist_ok=True)
            shutil.copytree(source, profile, dirs_exist_ok=True)

    def test_a_missing_verifier_stops_the_pod_rather_than_the_check(self):
        # What decides whether the check is mandatory has to be the manifest, not
        # the script. Both are root-owned now — the manifest inside the tree it
        # describes, the script in /opt/defaults/scripts — so this is no longer
        # about an `rm` the runtime uid can issue; it is about the alternative to
        # reporting a missing checker being to skip the tree it would have
        # checked. Gate on the script and a truncated image verifies clean
        # forever, silently, in the one step whose point is failing closed.
        step = self.entrypoint[self.entrypoint.index("SKILL_PROVENANCE_SCRIPT="):]
        step = step[: step.index("\n# 1.6 ")]
        self.assertIn(f'[ -f "$_tree/{self.vsp.MANIFEST_NAME}" ] || continue', step)
        gate = step[: step.index("--manifest")]
        self.assertIn('[ ! -f "$SKILL_PROVENANCE_SCRIPT" ]', gate)
        self.assertIn("exit 1", gate)

    def test_the_verifier_is_shipped_where_the_entrypoint_looks_for_it(self):
        # It is COPYed into /opt/defaults/scripts with the rest of the platform
        # scripts; the entrypoint hard-codes that path rather than the PVC copy,
        # which `cp -ru` can leave older than the image.
        self.assertIn("/opt/defaults/scripts/verify_skills_provenance.py", self.entrypoint)
        self.assertIn("agents/platform/scripts/ /opt/defaults/scripts/", self.stage)


# The tree no normalization loop walks, so every write into it has to carry its
# own mode. Held as a prefix rather than a set of filenames: the point is that a
# fourth module dropped in beside the three is caught too.
UNNORMALIZED_TREE = "/opt/hermes/hermes_cli"

# The trees the `platform` stage's loop does normalize, which is what lets the
# unmoded COPYs into them stand.
NORMALIZED_TREES = ("/opt/hermes/plugins", "/opt/hermes/gateway", "/opt/hermes/tools")

FILE_WRITING_COMMANDS = ("cp", "mv", "install")

# `do`, `then` and friends lead a segment when a write sits inside a loop or a
# conditional; stripping them reads the write as the write it is.
SHELL_KEYWORDS = ("do", "done", "then", "else", "elif", "fi", "!")


def grants_world_read(mode: str) -> bool:
    """True when a `chmod` or `--chmod` mode argument grants read to `other`.

    Both notations, because a false red here reads as "your correct Dockerfile
    is wrong" and invites someone to change working code to satisfy a parser.
    """
    if re.fullmatch(r"[0-7]{3,4}", mode):
        return bool(int(mode, 8) & 0o004)
    # Symbolic. Only a clause naming `o` or `a` reaches `other`, and only `+`
    # or `=` grants: `a+r`, `ugo+r` and `o=rX` do, `u+r` and `go-r` do not.
    return any(
        re.fullmatch(r"[ugoa]*[ao][ugoa]*[+=][rwxXst]*r[rwxXst]*", clause)
        for clause in mode.split(",")
    )


def copy_destination_and_mode(argument: str):
    """(destination, --chmod value or None) for a COPY, or (None, None).

    Heredoc COPYs are skipped — the repository has none, and guessing at one
    would be a false failure rather than a caught bug.
    """
    if "<<" in argument:
        return None, None
    try:
        tokens = shlex.split(argument)
    except ValueError:
        return None, None
    mode = None
    while tokens and tokens[0].startswith("--"):
        flag = tokens.pop(0)
        if flag.startswith("--chmod="):
            mode = flag.split("=", 1)[1]
    if len(tokens) < 2:
        return None, None
    return tokens[-1], mode


def install_mode(tokens):
    """The mode an `install` command was given, in either notation, or None."""
    for index, token in enumerate(tokens):
        if token.startswith("--mode="):
            return token.split("=", 1)[1]
        if token.startswith("-m") and len(token) > 2:
            return token[2:]
        if token == "-m" and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def run_commands(argument: str):
    """The tokenized commands a shell would run for one RUN body."""
    for segment in re.split(r"&&|\|\||[;|]", argument):
        tokens = segment.split()
        while tokens and tokens[0] in SHELL_KEYWORDS:
            tokens.pop(0)
        if tokens:
            yield tokens


def normalization_loops(stage_text: str):
    """The tree lists of the stage's `for d in … ; do find … chmod` loops.

    The lists themselves, not the enclosing RUN's text: a tree that a later
    `cp -a` in the same RUN merely mentions is not a tree the loop normalizes,
    and matching on the RUN as a whole cannot tell the two apart.
    """
    for keyword, argument in keyed_instructions(stage_text):
        if keyword != "RUN" or "-type f -exec chmod" not in argument:
            continue
        for trees in re.findall(r"\bfor\s+\w+\s+in\s+(.*?);?\s*\bdo\b", argument):
            yield trees.split()


class HermesCliPatchesShipWorldReadableTest(unittest.TestCase):
    """The patched modules have to be readable by the uid that imports them.

    `agentplugins/lib/plugin_image.sh:798` states the mechanism and stages the
    plugin image against it; the same thing reaches the agent image by a
    different route. Git tracks one permission bit, so a tree checked out under
    `umask 077` carries mode 0600 throughout, an unmoded `COPY` brings that into
    the image, and `cp` — which masks the source's mode by the umask in force
    rather than repairing it — carries it the rest of the way. The agent runs as
    uid 10000, so the same Dockerfile yields a working image on one builder and,
    on another, one that dies on its first import:

        PermissionError: [Errno 13] Permission denied:
          '/opt/hermes/hermes_cli/kanban_scheduling.py'

    Precompiled bytecode does not paper over it. CPython derives a `.pyc`'s
    mode from its source's, so an unreadable source has an unreadable cache.

    Nothing else catches a regression here. CI builds under `umask 022`, where
    an unmoded COPY lands at 0644 by luck and every gate stays green; the
    failure reaches only whoever builds in a hardened environment, at agent
    startup, as a stack trace with no visible connection to their umask.

    Scope. These are pinned for `/opt/hermes/hermes_cli` because it is the one
    patch destination no normalization loop walks, so a write into it is the one
    that has to carry its own mode. The other destinations are swept by the
    `platform` stage's loop, which is checked below — but only in the `platform`
    image. `credential-proxy` builds from `agent-base` and never runs that loop,
    so the unmoded COPYs into /opt/hermes/{tools,gateway} reach it as-is; that is
    latent rather than live, since the sidecar runs only
    /opt/defaults/scripts/credential_proxy.py and imports none of them.

    These read the Dockerfile, so they pin what the build is told to do and not
    what a layer ends up holding, and they see only `COPY` and the `cp`/`mv`/
    `install` family. A module arriving by `ADD`, a redirect, `tar -x`, or a
    Python applier using `shutil.copy` — which also preserves the source mode —
    is outside them, as are the base image's own modes and the `/tmp/*` staging
    directories the patch layers copy through.
    """

    def setUp(self):
        self.text = DOCKERFILE.read_text()
        self.instructions = keyed_instructions(self.text)
        self.assertTrue(self.instructions, "the Dockerfile parsed to nothing")

    def test_every_copy_into_hermes_cli_sets_a_world_readable_mode(self):
        offenders = []
        for keyword, argument in self.instructions:
            if keyword != "COPY":
                continue
            destination, mode = copy_destination_and_mode(argument)
            if destination is None or not destination.startswith(UNNORMALIZED_TREE):
                continue
            if mode is None:
                offenders.append(f"{destination} (no --chmod)")
            elif not grants_world_read(mode):
                offenders.append(f"{destination} (--chmod={mode})")
        self.assertEqual(
            [],
            offenders,
            f"these COPY instructions write into {UNNORMALIZED_TREE}, which no "
            "normalization loop walks, without a world-readable --chmod. On a builder "
            "whose umask is stricter than 022 the file lands unreadable and the agent "
            "dies at import as uid 10000. Add --chmod=0644.",
        )

    def test_every_run_that_writes_into_hermes_cli_sets_a_world_readable_mode(self):
        offenders = []
        for keyword, argument in self.instructions:
            if keyword != "RUN":
                continue
            commands = list(run_commands(argument))
            # A world-readable chmod anywhere in the same RUN is an equally good
            # fix, so collect those before judging the writes. A chmod to some
            # other mode is not a fix and does not count.
            chmodded = set()
            for tokens in commands:
                if tokens[0] != "chmod":
                    continue
                # Flags first, so `chmod -R a+r <tree>` reads as the fix it is.
                operands = [token for token in tokens[1:] if not token.startswith("-")]
                if len(operands) > 1 and grants_world_read(operands[0]):
                    chmodded.update(operands[1:])
            for tokens in commands:
                if tokens[0] not in FILE_WRITING_COMMANDS:
                    continue
                destination = tokens[-1]
                if not destination.startswith(UNNORMALIZED_TREE):
                    continue
                if destination in chmodded:
                    continue
                mode = install_mode(tokens) if tokens[0] == "install" else None
                if mode is not None and grants_world_read(mode):
                    continue
                offenders.append(" ".join(tokens))
        self.assertEqual(
            [],
            offenders,
            f"these commands write into {UNNORMALIZED_TREE} without setting a "
            "world-readable mode. `cp` gives the destination the source's mode masked "
            "by the umask, so a 0600 source stays 0600 and the uid 10000 the agent runs "
            "as cannot read it. Use `install -m 0644` instead.",
        )

    def test_the_platform_stage_still_normalizes_the_other_patch_destinations(self):
        # The two tests above are scoped to hermes_cli because everywhere else a
        # patch lands is swept by the platform stage's loop. Dropping a tree from
        # that loop would silently widen the bug rather than fail anything, so
        # pin its membership here.
        loops = list(normalization_loops(platform_stage(self.text)))
        self.assertTrue(
            loops,
            "the platform stage no longer normalizes any tree with a `for d in … ; do "
            "find … -type f -exec chmod` loop; the unmoded COPYs into /opt/hermes/tools "
            "and /opt/hermes/gateway now depend on the builder's umask",
        )
        swept = {tree for trees in loops for tree in trees}
        for tree in NORMALIZED_TREES:
            self.assertIn(
                tree,
                swept,
                f"{tree} is written by COPY instructions that set no mode, so it has to "
                "stay in the platform stage's normalization loop",
            )


if __name__ == "__main__":
    unittest.main()
