"""Tests for deploy/shared/sandbox_mirror.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching tests/test_profile_overlay.py.

Two things this script gets to decide, and both fail quietly if it decides
wrong. If the exclusion rules are too narrow it copies a credential or a
session database into the pod that exists so the model cannot reach them, and
nothing complains. If they are too wide it leaves the model's work on the agent
pod's volume after an upgrade, which is the failure the migration was written
to prevent and which looks exactly like the files having been deleted.

So most of what is below is the exclusion table, asserted against a home laid
out like the one on the install this was written against. The transfer itself
is covered end to end by driving a real tar into a real directory with the SSH
hop replaced by `sh -c`.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared" / "sandbox_mirror.py"
)
_spec = importlib.util.spec_from_file_location("sandbox_mirror", _MODULE_PATH)
sm = importlib.util.module_from_spec(_spec)
sys.modules["sandbox_mirror"] = sm
_spec.loader.exec_module(sm)


# A trimmed copy of the machine home on kage-management: enough of each class
# for the exclusion rules to have something to be wrong about.
MACHINE_HOME_DIRS = [
    # the model's, and the whole point of the migration
    "scratch",
    "gitops",
    "artifacts",
    "plans",
    "workspace",
    "home",
    "tmp",
    "infra",
    "infra-repo",
    "infra_repo",
    "work-d0452361",
    # Hermes'
    "sessions",
    "logs",
    "cache",
    "cron",
    "memories",
    "hindsight",
    "kanban",
    "plugins",
    "hooks",
    "sandboxes",
    "lazy-packages",
    "venv-yaml",
    "lost+found",
    "__pycache__",
    # the image's
    "skills",
    "governance",
    "scripts",
    # credentials
    ".ssh",
    ".kubeconfigs",
]

MACHINE_HOME_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "SETTINGS.md",
    "config.yaml",
    "config.yaml.bak",
    "kubeconfig.yaml",
    "state.db",
    "state.db-wal",
    "kanban.db",
    "models_dev_cache.json",
    ".env",
    ".bootstrap_completed",
    "unblock.py",
    "hermes-verify-export.py",
]


def build_home(root: pathlib.Path, profiles=("platform", "cluster-a")) -> None:
    for name in MACHINE_HOME_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "content").write_text("x")
    for name in MACHINE_HOME_FILES:
        (root / name).write_text("x")
    for profile in profiles:
        home = root / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        for name in ("workspace", "plans", "logs", "sessions", "governance"):
            (home / name).mkdir(exist_ok=True)
            (home / name / "content").write_text("x")
        for name in ("config.yaml", ".env", "SOUL.md", "state.db"):
            (home / name).write_text("x")


class ExclusionRules(unittest.TestCase):
    def test_credentials_never_cross(self):
        for name in (
            ".env",
            ".ssh",
            ".kubeconfigs",
            "kubeconfig.yaml",
            "auth.lock",
            # A file, not the directory the name suggests: a cached GKE token.
            "gke_gcloud_auth_plugin_cache",
        ):
            self.assertIsNotNone(
                sm.is_excluded(name),
                f"{name} holds or names a credential and must stay on the agent pod",
            )

    def test_the_process_home_never_crosses(self):
        # $HERMES_HOME/home is the pod's $HOME. On the install this was written
        # against it held 831 MiB of pip and gcloud cache, 46 MiB of
        # kubeconfigs under .kube, and gcloud's credentials under .config —
        # a fifth of the sandbox's volume, and a credential path into the pod
        # that exists to have none.
        self.assertIsNotNone(sm.is_excluded("home"))

    def test_hermes_log_and_process_state_never_cross(self):
        for name in (
            "logs",
            "agent.log",
            "agent.log.1",
            "gateway.pid",
            "gateway.lock",
            "gateway-starts.log",
            "gateway_state.json",
            "processes.json",
            "channel_directory.json",
            "google_chat_thread_counts.json",
        ):
            self.assertIsNotNone(sm.is_excluded(name), name)

    def test_databases_and_their_write_ahead_logs_never_cross(self):
        for name in ("state.db", "state.db-wal", "state.db-shm", "kanban.db", "sessions.db"):
            self.assertIsNotNone(sm.is_excluded(name), name)

    def test_image_owned_trees_never_cross(self):
        # The sandbox entrypoint replaces these from /opt/defaults on every
        # start, so a copy from the agent pod is undone at the next restart at
        # best and shadows a newer image at worst.
        for name in ("skills", "governance", "scripts"):
            self.assertEqual(sm.is_excluded(name), "delivered by the sandbox image", name)

    def test_image_owned_names_what_the_sandbox_image_stages(self):
        # These two sets are excluded for opposite reasons and the log line says
        # which, so a name in the wrong one makes the audit trail lie. The live
        # sandbox stages governance, scripts and skills at /opt/defaults and
        # nothing else; the persona files are withheld because nothing reads
        # them through the shell, not because something replaces them.
        dockerfile = (
            pathlib.Path(__file__).resolve().parents[1] / "deploy/sandbox/Dockerfile"
        ).read_text()
        for name in sm.IMAGE_OWNED:
            self.assertIn(
                f"/opt/defaults/{name}",
                dockerfile,
                f"{name} is called image-owned but the sandbox image does not stage it",
            )
        self.assertFalse(sm.IMAGE_OWNED & sm.AGENT_POD_ONLY)

    def test_the_persona_stays_in_the_agent_pod(self):
        for name in ("SOUL.md", "AGENTS.md", "CAPABILITIES.md", "USER.md", "profile.yaml"):
            self.assertEqual(
                sm.is_excluded(name),
                "stays in the agent pod; nothing reads it through the shell",
                name,
            )

    def test_settings_md_is_left_to_the_configmap_mount(self):
        # The operator mounts the rendered per-install SETTINGS.md into the
        # sandbox. Migrating the agent pod's copy would land on top of it.
        self.assertIsNotNone(sm.is_excluded("SETTINGS.md"))

    def test_the_model_s_working_directories_do_cross(self):
        for name in (
            "scratch",
            "gitops",
            "artifacts",
            "plans",
            "workspace",
            "tmp",
            # None of these four is named by any instruction; the model
            # invented them. An allowlist would have dropped all four, which
            # is the case the denylist exists for.
            "infra",
            "infra-repo",
            "infra_repo",
            "work-d0452361",
        ):
            self.assertIsNone(
                sm.is_excluded(name), f"{name} is the model's work and must be migrated"
            )

    def test_every_skeleton_directory_is_one_the_rules_would_migrate(self):
        # Otherwise the layout and the migration disagree: the directory is
        # created empty on the sandbox and its contents are then withheld.
        for name in sm.SKELETON_DIRS:
            self.assertIsNone(sm.is_excluded(name), name)


class HomeEnumeration(unittest.TestCase):
    def test_machine_home_first_then_every_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            build_home(root)
            self.assertEqual(
                sm.home_relative_paths(root),
                ["", "profiles/cluster-a", "profiles/platform"],
            )

    def test_a_home_with_no_profiles_directory_is_still_a_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sm.home_relative_paths(pathlib.Path(tmp)), [""])


class Candidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        build_home(self.root)
        self.homes = sm.home_relative_paths(self.root)
        self.include, self.skipped = sm.migration_candidates(self.root, self.homes)
        self.addCleanup(self.tmp.cleanup)

    def test_nothing_under_a_profile_home_leaks_a_credential(self):
        for rel in self.include:
            self.assertNotIn(".env", rel)
            self.assertNotIn("config.yaml", rel)

    def test_profile_working_directories_are_included_under_their_profile(self):
        self.assertIn("profiles/platform/workspace", self.include)
        self.assertIn("profiles/cluster-a/plans", self.include)

    def test_the_profiles_directory_itself_is_not_a_candidate(self):
        # It is walked one level down instead. Including it too would copy
        # every profile home wholesale, exclusion rules and all.
        self.assertNotIn("profiles", self.include)
        self.assertIn(("profiles", "walked separately"), self.skipped)

    def test_every_entry_is_either_included_or_skipped_with_a_reason(self):
        seen = set(self.include) | {rel for rel, _ in self.skipped}
        for home in self.homes:
            base = self.root / home if home else self.root
            for entry in base.iterdir():
                rel = f"{home}/{entry.name}" if home else entry.name
                self.assertIn(rel, seen, f"{rel} was neither copied nor accounted for")


class Budget(unittest.TestCase):
    def test_smallest_first_so_one_huge_clone_does_not_evict_everything(self):
        sizes = {"scratch": 10, "gitops": 100, "plans": 1}
        kept, dropped = sm.apply_budget(sizes, 20)
        self.assertEqual(kept, ["plans", "scratch"])
        self.assertEqual(dropped, [("gitops", 100)])

    def test_a_budget_of_zero_drops_everything_and_names_it(self):
        kept, dropped = sm.apply_budget({"scratch": 10}, 0)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [("scratch", 10)])

    def test_no_budget_keeps_everything(self):
        # The sandbox volume is sized from the agent's, so the default copy is
        # bounded only by free space. A cap that reappears here truncates a
        # migration silently, which is the failure this path exists to prevent.
        sizes = {"scratch": 10, "gitops": 10**12}
        kept, dropped = sm.apply_budget(sizes, None)
        self.assertEqual(kept, ["gitops", "scratch"])
        self.assertEqual(dropped, [])

    def test_the_default_cap_is_off_and_free_space_is_what_bounds_the_copy(self):
        self.assertIsNone(sm.effective_budget(sm.DEFAULT_MAX_BYTES, None))

        # Free space always applies, less the headroom that keeps the volume
        # writable for sshd and the shell.
        free = 4 * 1024 * 1024 * 1024
        self.assertEqual(
            sm.effective_budget(sm.DEFAULT_MAX_BYTES, free),
            free - sm.FREE_SPACE_HEADROOM,
        )

        # An explicit --max-bytes is an escape hatch, and the tighter of the two wins.
        self.assertEqual(sm.effective_budget(1024, free), 1024)
        self.assertEqual(sm.effective_budget(free, 1024 + sm.FREE_SPACE_HEADROOM), 1024)

    def test_a_full_volume_yields_a_zero_budget_rather_than_a_negative_one(self):
        kept, dropped = sm.apply_budget(
            {"scratch": 10}, sm.effective_budget(sm.DEFAULT_MAX_BYTES, 0)
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [("scratch", 10)])


def gnu_tar() -> bool:
    try:
        out = subprocess.run(["tar", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "GNU tar" in out.stdout


@unittest.skipUnless(
    gnu_tar(),
    "needs GNU tar: --skip-old-files and --files-from --null are GNU spellings, and "
    "the pod this runs in is Debian. macOS ships bsdtar, so these skip there and "
    "run in CI",
)
class Transfer(unittest.TestCase):
    """The real tar pipeline, with `sh -c` standing in for the SSH hop.

    ssh_base_command returns an argv the transfer appends `--` and a command
    string to, and `sh -c` has the same shape. That makes the pipe, the
    NUL-separated file list and --skip-old-files all exercised for real.
    """

    def setUp(self):
        self.src = tempfile.TemporaryDirectory()
        self.dst = tempfile.TemporaryDirectory()
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.dst.cleanup)
        self.source = pathlib.Path(self.src.name)
        self.dest = pathlib.Path(self.dst.name)

    def fake_ssh(self):
        return ["sh", "-c"]

    def run_transfer(self, paths):
        # `sh -c CMD -- ` would make "--" $0, so the command string has to be
        # the last argument. transfer() appends ["--", cmd]; sh reads the "--"
        # as end-of-options and the command as the script. Same shape as ssh.
        sm.transfer(self.fake_ssh(), self.source, str(self.dest), paths)

    def test_a_directory_tree_arrives_intact(self):
        (self.source / "scratch" / "deep").mkdir(parents=True)
        (self.source / "scratch" / "deep" / "note.md").write_text("kept")
        self.run_transfer(["scratch"])
        self.assertEqual((self.dest / "scratch" / "deep" / "note.md").read_text(), "kept")

    def test_an_existing_file_on_the_sandbox_is_not_overwritten(self):
        # The two pods have no start ordering, so this can land after the model
        # has already written in the sandbox. --skip-old-files is what keeps a
        # late migration from replacing a newer file with the agent pod's copy.
        (self.source / "scratch").mkdir()
        (self.source / "scratch" / "note.md").write_text("older, from the agent pod")
        (self.dest / "scratch").mkdir()
        (self.dest / "scratch" / "note.md").write_text("newer, written in the sandbox")
        self.run_transfer(["scratch"])
        self.assertEqual(
            (self.dest / "scratch" / "note.md").read_text(),
            "newer, written in the sandbox",
        )

    def test_an_executable_stays_executable(self):
        (self.source / "scratch").mkdir()
        script = self.source / "scratch" / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        self.run_transfer(["scratch"])
        self.assertTrue(os.access(self.dest / "scratch" / "run.sh", os.X_OK))

    def test_a_path_with_a_space_survives_the_nul_separated_list(self):
        (self.source / "my work").mkdir()
        (self.source / "my work" / "a.txt").write_text("ok")
        self.run_transfer(["my work"])
        self.assertEqual((self.dest / "my work" / "a.txt").read_text(), "ok")

    def test_a_credential_nested_inside_a_migrated_directory_does_not_cross(self):
        # The first live run of this script copied /opt/data/tmp, which the
        # model had been running gcloud inside, and carried a cached GKE access
        # token across in tmp/gke_gcloud_auth_plugin_cache. `tmp` is a directory
        # the rules are right to migrate; what has to be dropped is what is
        # inside it, which only tar's --exclude can see.
        work = self.source / "tmp"
        (work / ".kube").mkdir(parents=True)
        (work / ".kube" / "config").write_text("clusters: [...]")
        (work / "gke_gcloud_auth_plugin_cache").write_text('{"access_token": "ya29.fake"}')
        (work / ".config" / "gcloud").mkdir(parents=True)
        (work / ".config" / "gcloud" / "credentials.db").write_text("secret")
        (work / "deeper" / "sub").mkdir(parents=True)
        (work / "deeper" / "sub" / ".env").write_text("API_KEY=secret")
        (work / "notes.md").write_text("kept")

        self.run_transfer(["tmp"])

        self.assertEqual((self.dest / "tmp" / "notes.md").read_text(), "kept")
        for leaked in (
            "tmp/.kube/config",
            "tmp/gke_gcloud_auth_plugin_cache",
            "tmp/.config/gcloud/credentials.db",
            "tmp/deeper/sub/.env",
        ):
            self.assertFalse(
                (self.dest / leaked).exists(), f"{leaked} reached the sandbox"
            )

    def test_a_nested_cache_does_not_spend_the_sandbox_volume(self):
        (self.source / "scratch" / "repo" / "node_modules" / "left-pad").mkdir(parents=True)
        (self.source / "scratch" / "repo" / "node_modules" / "left-pad" / "i.js").write_text("x")
        (self.source / "scratch" / "repo" / "main.py").write_text("keep")
        self.run_transfer(["scratch"])
        self.assertEqual((self.dest / "scratch" / "repo" / "main.py").read_text(), "keep")
        self.assertFalse((self.dest / "scratch" / "repo" / "node_modules").exists())


class RecursiveExclusion(unittest.TestCase):
    """The Python mirror of tar's --exclude matching, used by the size estimate.

    Transfer above proves tar's behaviour; this proves the estimate agrees with
    it, so the budget and the dry-run plan count the bytes that actually move.
    """

    def test_a_bare_pattern_matches_at_any_depth(self):
        for path in (".kube", "tmp/.kube", "scratch/a/b/.kube"):
            self.assertEqual(sm.recursively_excluded(path), ".kube", path)

    def test_a_two_component_pattern_needs_both_components(self):
        self.assertEqual(sm.recursively_excluded("home/.config/gcloud"), ".config/gcloud")
        self.assertIsNone(sm.recursively_excluded("scratch/gcloud"))

    def test_a_partial_name_is_not_a_match(self):
        self.assertIsNone(sm.recursively_excluded("scratch/kubernetes"))
        self.assertIsNone(sm.recursively_excluded("scratch/.kubernetes-notes"))
        self.assertIsNone(sm.recursively_excluded("gitops/env"))

    def test_the_model_s_own_files_are_untouched(self):
        for path in ("scratch/notes.md", "gitops/repo/.git/HEAD", "plans/q3.md"):
            self.assertIsNone(sm.recursively_excluded(path), path)

    def test_measure_does_not_count_what_will_not_be_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "scratch" / ".cache").mkdir(parents=True)
            (home / "scratch" / ".cache" / "blob").write_bytes(b"x" * 10_000)
            (home / "scratch" / "note.md").write_bytes(b"y" * 100)
            self.assertEqual(sm.measure(home, ["scratch"]), {"scratch": 100})


class ManagedConfig(unittest.TestCase):
    def write(self, body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        handle.write(body)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_no_terminal_block_means_no_sandbox(self):
        self.assertIsNone(sm.read_terminal_config(self.write("model: x\n")))

    def test_a_local_backend_means_no_sandbox(self):
        self.assertIsNone(sm.read_terminal_config(self.write("terminal:\n  backend: local\n")))

    def test_a_missing_file_means_no_sandbox(self):
        self.assertIsNone(sm.read_terminal_config("/nonexistent/config.yaml"))

    def test_the_ssh_block_comes_back_whole(self):
        path = self.write(
            "terminal:\n"
            "  backend: ssh\n"
            "  ssh_host: platform-agent-shell-0.example\n"
            "  ssh_user: agent\n"
            "  ssh_port: 2222\n"
            "  ssh_key: /etc/sandbox-ssh/id_ed25519\n"
        )
        terminal = sm.read_terminal_config(path)
        argv = sm.ssh_base_command(terminal)
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)
        self.assertIn("/etc/sandbox-ssh/id_ed25519", argv)
        self.assertEqual(argv[-1], "agent@platform-agent-shell-0.example")
        # BatchMode, or a sandbox that has lost its host key turns a background
        # startup step into one that blocks on a password prompt forever.
        self.assertIn("BatchMode=yes", argv)


class DryRun(unittest.TestCase):
    def test_dry_run_reports_the_plan_and_touches_nothing_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            build_home(root)
            config = root / "managed.yaml"
            config.write_text(
                "terminal:\n  backend: ssh\n  ssh_host: unreachable.invalid\n"
            )
            out = subprocess.run(
                [
                    sys.executable,
                    str(_MODULE_PATH),
                    "--agent-home",
                    str(root),
                    "--config",
                    str(config),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertIn("scratch", report["would_copy"])
            self.assertIn(".env", report["skipped"])
            self.assertIn("profiles/platform/workspace", report["would_copy"])


class MigrationMarker(unittest.TestCase):
    """When the run may declare itself finished.

    The marker is what every later start reads to skip the copy, so writing it
    while paths are still on the agent pod's volume is the one-way door: the
    budget that was too tight for this start becomes permanent.
    """

    def drive(self, max_bytes):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        build_home(root)
        config = root / "managed.yaml"
        config.write_text("terminal:\n  backend: ssh\n  ssh_host: sandbox.invalid\n")

        commands = []

        def remote(ssh, command, check=True):
            commands.append(command)
            # The sandbox's own marker is present -- that is the check for
            # --remote-root naming the right volume -- and the migration marker
            # is not, which is what a first run looks like.
            missing = command.startswith("test -f") and sm.MIGRATION_MARKER in command
            return subprocess.CompletedProcess([], 1 if missing else 0, "", "")

        patches = [
            unittest.mock.patch.object(sm, "remote", remote),
            unittest.mock.patch.object(sm, "wait_for_sandbox", lambda *a, **k: True),
            unittest.mock.patch.object(sm, "push_skeleton", lambda *a, **k: None),
            unittest.mock.patch.object(sm, "remote_free_bytes", lambda *a, **k: None),
            unittest.mock.patch.object(sm, "transfer", lambda *a, **k: None),
            unittest.mock.patch.object(sm, "log", lambda message: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        code = sm.main(
            ["--agent-home", str(root), "--config", str(config), "--max-bytes", str(max_bytes)]
        )
        written = [c for c in commands if sm.MIGRATION_MARKER in c and c.startswith("cat >")]
        return code, written

    def test_a_budget_that_left_paths_behind_writes_no_marker(self):
        # One byte: everything is over budget, so nothing is copied. Marking
        # that complete strands the model's work on the agent pod's volume
        # forever, because no later start looks again.
        code, written = self.drive(max_bytes=1)
        self.assertEqual(0, code)
        self.assertEqual([], written, "the migration is not finished; the marker says it is")

    def test_a_run_that_copied_everything_writes_the_marker(self):
        code, written = self.drive(max_bytes=0)
        self.assertEqual(0, code)
        self.assertEqual(1, len(written))
        recorded = json.loads(written[0].split("\n", 1)[1].rsplit("\n", 2)[0])
        # What landed, not what was considered: the summary is the only record
        # of the run, and a dropped path listed as copied reads as data loss
        # having been intentional.
        self.assertIn("scratch", recorded["copied"])
        self.assertNotIn(".env", recorded["copied"])


class Skeleton(unittest.TestCase):
    """The layout push, against a real filesystem with `sh -c` as the SSH hop.

    Everything below the sandbox's /opt/data is owned by uid 1000, so the model
    decides what is sitting on a skeleton path when this runs. A plain
    `mkdir -p` returned 1 for any of it, the entrypoint turned that into
    `exit 1` for the whole gateway container, and nothing on either side ever
    cleared it -- the sandbox entrypoint only unlinks symlinks and only rewrites
    the trees it ships in /opt/defaults. `touch /opt/data/scratch` from a sandbox
    shell was a permanent CrashLoopBackOff the agent could not repair.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.logged = []
        patcher = unittest.mock.patch.object(sm, "log", self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def push(self, homes=("",)):
        # `sh -c CMD` has the same argv shape ssh does; see Transfer above.
        sm.push_skeleton(["sh", "-c"], str(self.root), list(homes))

    def displaced(self, name):
        found = [p for p in self.root.iterdir() if p.name.startswith(f"{name}{sm.DISPLACED_SUFFIX}")]
        self.assertEqual(1, len(found), f"expected one displaced {name}, got {found}")
        return found[0]

    def test_every_skeleton_directory_is_created_for_every_home(self):
        self.push(homes=["", "profiles/platform"])
        for home in ("", "profiles/platform"):
            base = self.root / home if home else self.root
            for name in sm.SKELETON_DIRS:
                self.assertTrue((base / name).is_dir(), f"{home}/{name} missing")

    def test_a_file_where_a_directory_belongs_is_displaced_not_fatal(self):
        (self.root / "scratch").write_text("the model put a file here")
        self.push()
        self.assertTrue((self.root / "scratch").is_dir())
        # Renamed, not deleted. It is broken state either way, but it is the
        # model's own byte and this is not the code that decides it is worthless.
        self.assertEqual("the model put a file here", self.displaced("scratch").read_text())

    def test_a_symlink_where_a_directory_belongs_is_displaced_too(self):
        # `[ -d ]` accepts a symlink that points at a directory, so testing for
        # a directory alone would leave this one in place -- and the migration
        # would then extract the model's files through it.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "workspace").symlink_to(elsewhere)
        self.push()
        target = self.root / "workspace"
        self.assertTrue(target.is_dir())
        self.assertFalse(target.is_symlink())
        self.assertTrue(self.displaced("workspace").is_symlink())

    def test_a_home_root_that_is_a_file_is_displaced_before_its_own_skeleton(self):
        # Targets go parent-first for this: displacing profiles/platform after
        # trying to mkdir profiles/platform/scratch inside it is too late.
        (self.root / "profiles").mkdir()
        (self.root / "profiles" / "platform").write_text("not a directory")
        self.push(homes=["", "profiles/platform"])
        self.assertTrue((self.root / "profiles" / "platform" / "scratch").is_dir())

    def test_a_displacement_is_reported_so_somebody_can_look_at_it(self):
        (self.root / "tmp").write_text("x")
        self.push()
        self.assertTrue(
            any("was not a directory" in line and "/tmp" in line for line in self.logged),
            f"the displacement was silent: {self.logged}",
        )

    def test_a_push_that_cannot_be_repaired_still_raises(self):
        # Displacing is not the same as swallowing. A remote that fails for a
        # reason this cannot fix -- a read-only volume, a dead connection --
        # still has to reach main(), which decides whether it is fatal.
        with self.assertRaises(RuntimeError):
            sm.push_skeleton(["sh", "-c"], "/proc/nonexistent-and-unwritable", [""])


class ClusterIdentities(unittest.TestCase):
    """The one file AGENT_POD_ONLY holds back and a Cluster Agent still needs.

    cluster_preflight.sh runs over SSH like every other command the agent
    issues, so the USER.md it reads is the sandbox's copy. Without this push
    there is no sandbox copy, and preflight reports every Cluster Agent as
    having no identity. Driven against a real filesystem with `sh -c` as the
    SSH hop, the way Skeleton and Transfer above are: the quoting is half of
    what this function does, and a mock for the remote would not test it.
    """

    def setUp(self):
        agent = tempfile.TemporaryDirectory()
        self.addCleanup(agent.cleanup)
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.agent_home = pathlib.Path(agent.name)
        self.sandbox_root = pathlib.Path(sandbox.name)
        self.logged = []
        patcher = unittest.mock.patch.object(sm, "log", self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def profile(self, name, identity=None):
        """A profile home on both sides; its USER.md only on the agent's."""
        home = f"{sm.PROFILES_DIR}/{name}"
        (self.agent_home / home).mkdir(parents=True)
        (self.sandbox_root / home).mkdir(parents=True)
        if identity is not None:
            (self.agent_home / home / sm.CLUSTER_IDENTITY_FILE).write_text(identity)
        return home

    def push(self, homes):
        sm.push_cluster_identities(
            ["sh", "-c"], self.agent_home, str(self.sandbox_root), list(homes)
        )

    def landed(self, home):
        path = self.sandbox_root / home / sm.CLUSTER_IDENTITY_FILE
        return path.read_text() if path.exists() else None

    def fail_on_call(self, *args, **kwargs):
        self.fail(f"a remote call with nothing to deliver: {args}")

    def test_a_cluster_profile_s_identity_crosses(self):
        home = self.profile("cluster-alpha", "# Cluster alpha\n")
        self.push(["", home])
        self.assertEqual("# Cluster alpha\n", self.landed(home))

    def test_every_cluster_profile_crosses_in_one_call(self):
        homes = [self.profile(f"cluster-{n}", f"identity {n}\n") for n in ("a", "b", "c")]
        calls = []

        def counting_remote(ssh, command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(ssh, 0, "", "")

        with unittest.mock.patch.object(sm, "remote", counting_remote):
            self.push(homes)
        self.assertEqual(1, len(calls), f"one round trip per start, not per profile: {calls}")
        for name in ("a", "b", "c"):
            self.assertIn(f"cluster-{name}", calls[0])

    def test_the_persona_of_a_non_cluster_profile_stays_behind(self):
        # AGENT_POD_ONLY holds USER.md back because it is the persona. Only a
        # Cluster Agent's copy is an identity stamp, and only it is excepted.
        platform = self.profile("platform", "# The Platform Agent persona\n")
        (self.agent_home / sm.CLUSTER_IDENTITY_FILE).write_text("# The machine persona\n")
        self.push(["", platform])
        self.assertIsNone(self.landed(platform))
        self.assertIsNone(self.landed(""))

    def test_a_profile_merely_mentioning_the_prefix_is_not_one(self):
        home = self.profile("my-cluster-notes", "not an identity\n")
        self.push([home])
        self.assertIsNone(self.landed(home))

    def test_a_profile_with_no_identity_yet_is_skipped_without_a_remote_call(self):
        # create_profile's step 2e runs before the identity is written, so this
        # is the ordinary case on a scaffold and not an error worth reporting.
        home = self.profile("cluster-alpha")
        with unittest.mock.patch.object(sm, "remote", self.fail_on_call):
            self.push([home])
        self.assertEqual([], self.logged)

    def test_one_missing_identity_does_not_hold_back_the_others(self):
        empty = self.profile("cluster-empty")
        full = self.profile("cluster-full", "identity\n")
        self.push([empty, full])
        self.assertIsNone(self.landed(empty))
        self.assertEqual("identity\n", self.landed(full))

    def test_a_re_scaffolded_identity_replaces_the_stale_one(self):
        # The transfer's tar refuses to overwrite, deliberately. This must, or
        # a profile rebuilt against a different cluster keeps answering with
        # the old one.
        home = self.profile("cluster-alpha", "cluster: new\n")
        (self.sandbox_root / home / sm.CLUSTER_IDENTITY_FILE).write_text("cluster: old\n")
        self.push([home])
        self.assertEqual("cluster: new\n", self.landed(home))

    def test_the_content_reaches_the_far_side_verbatim(self):
        # It is written by the scaffold from cluster metadata, so it is not
        # model-authored, but it does reach a shell as an argument and a
        # backtick or a $( in a cluster description must stay text.
        body = "name: `whoami`\ndesc: $(id) 'quoted' \"double\" \\ tail\n"
        home = self.profile("cluster-alpha", body)
        self.push([home])
        self.assertEqual(body, self.landed(home))

    def test_the_delivery_is_logged_so_a_missing_identity_can_be_traced(self):
        home = self.profile("cluster-alpha", "identity\n")
        self.push([home])
        self.assertTrue(
            any(sm.CLUSTER_IDENTITY_FILE in line and home in line for line in self.logged),
            f"the push was silent: {self.logged}",
        )

    def test_a_write_that_fails_raises_rather_than_reporting_success(self):
        # main() turns this into EXIT_RETRY. Swallowing it would leave preflight
        # reporting no identity with nothing anywhere saying why.
        home = f"{sm.PROFILES_DIR}/cluster-alpha"
        (self.agent_home / home).mkdir(parents=True)
        (self.agent_home / home / sm.CLUSTER_IDENTITY_FILE).write_text("identity\n")
        with self.assertRaises(RuntimeError):
            sm.push_cluster_identities(
                ["sh", "-c"], self.agent_home, "/proc/nonexistent-and-unwritable", [home]
            )


class MirrorExitCodes(unittest.TestCase):
    """Which failures hold the gateway container down, and which do not.

    The agent pod's entrypoint exits 1 on any code other than EXIT_RETRY, so
    this is the boundary between "the model's files are stranded, refuse to
    start" and "the next start fixes it". Getting it wrong in the permissive
    direction hides data loss; getting it wrong in the strict direction lets a
    prompt injection stop the agent for good.
    """

    def drive(self, remote, transfer=lambda *a, **k: None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        build_home(root)
        config = root / "managed.yaml"
        config.write_text("terminal:\n  backend: ssh\n  ssh_host: sandbox.invalid\n")

        patches = [
            unittest.mock.patch.object(sm, "remote", remote),
            unittest.mock.patch.object(sm, "wait_for_sandbox", lambda *a, **k: True),
            unittest.mock.patch.object(sm, "remote_free_bytes", lambda *a, **k: None),
            unittest.mock.patch.object(sm, "transfer", transfer),
            unittest.mock.patch.object(sm, "log", lambda message: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return sm.main(["--agent-home", str(root), "--config", str(config)])

    @staticmethod
    def ok(command, check=True):
        missing = command.startswith("test -f") and sm.MIGRATION_MARKER in command
        return subprocess.CompletedProcess([], 1 if missing else 0, "", "")

    def test_a_failed_layout_push_asks_for_a_retry_rather_than_a_crash_loop(self):
        def remote(ssh, command, check=True):
            if "mkdir -p" in command:
                raise RuntimeError("mkdir -p: File exists")
            return self.ok(command, check)

        self.assertEqual(sm.EXIT_RETRY, self.drive(remote))

    def test_a_failed_marker_write_asks_for_a_retry(self):
        # The copy already landed. Re-running it costs a tar the sandbox
        # discards -- transfer passes --skip-old-files -- and that is cheaper
        # than an agent that will not start.
        def remote(ssh, command, check=True):
            if command.startswith("cat >"):
                raise RuntimeError("no space left on device")
            return self.ok(command, check)

        self.assertEqual(sm.EXIT_RETRY, self.drive(remote))

    def test_a_missing_sandbox_marker_refuses_to_write_without_holding_the_pod_down(self):
        # The marker sits under the same uid-1000 /opt/data as everything else,
        # so `rm -f /opt/data/.sandbox` from a sandbox shell produces exactly
        # this, and the sandbox does not rewrite it until its own container
        # restarts. Fatal here meant the model could crash-loop the gateway with
        # one command and then nothing was left to repair it with.
        def remote(ssh, command, check=True):
            if command.startswith("test -f") and sm.SANDBOX_MARKER in command:
                return subprocess.CompletedProcess([], 1, "", "")
            return self.ok(command, check)

        copied = []
        self.assertEqual(
            sm.EXIT_RETRY, self.drive(remote, lambda *a, **k: copied.append(a))
        )
        # Refusing is still the point: coming up is not the same as writing to a
        # root that may not be the sandbox's volume.
        self.assertEqual([], copied)

    def test_a_copy_that_ran_and_failed_asks_for_a_retry(self):
        # `transfer` reads the agent pod's home and never removes from it, so a
        # tar that dies halfway leaves every byte where it was. The next start
        # runs the copy again and --skip-old-files keeps whatever landed. Fatal
        # here would hold the gateway down over a failure the restart repairs.
        attempted = []

        def failing_transfer(*args, **kwargs):
            attempted.append(args)
            raise RuntimeError("tar: exit 2")

        commands = []

        def remote(ssh, command, check=True):
            commands.append(command)
            return self.ok(command, check)

        self.assertEqual(sm.EXIT_RETRY, self.drive(remote, failing_transfer))
        # The copy has to have been attempted, or this is testing the earlier
        # refusals rather than the one it names.
        self.assertEqual(1, len(attempted))
        # And no marker, or the retry would skip the copy it still owes.
        self.assertEqual(
            [], [c for c in commands if c.startswith("cat >") and sm.MIGRATION_MARKER in c]
        )

    def test_an_unclassified_failure_is_still_fatal(self):
        # EXIT_FATAL is what an unhandled exception exits with on its own, and
        # that is the conservative way round: a state nobody has reasoned about
        # should not continue silently.
        def remote(ssh, command, check=True):
            raise TypeError("something nobody classified")

        with self.assertRaises(TypeError):
            self.drive(remote)
        self.assertEqual(1, sm.EXIT_FATAL)


if __name__ == "__main__":
    unittest.main()
