"""Unit tests for cluster_agent_profile.profile_name (the kanban assignee resolver).

Run: python3 -m unittest agents.platform.scripts.test_cluster_agent_profile

profile_name is a pure, deterministic function; the module imports without pyyaml
(that import is lazy, only on the scaffold path).
"""

import contextlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import threading
import yaml
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# profile_overlay and profile_plugins ship beside these scripts in the image
# (/opt/defaults/scripts); in the repo they live in deploy/shared.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "shared"))

import cluster_agent_profile as cap  # noqa: E402
import credential_proxy_client  # noqa: E402
import profile_scaffold  # noqa: E402
import terminal_env_pin  # noqa: E402
import gke_endpoint  # noqa: E402


POD_RANGE = "10.92.0.0/14"
ADMIT_REMEDY = gke_endpoint.REMEDY_ADMIT_POD_RANGE.format(pod_cidr=POD_RANGE)
# The shim's one line per retry (credential_proxy_client.report_retry): printed
# before the next attempt, whose kubectl may write after it.
BUSY_RETRY_LINE = ("credential proxy busy (HTTP 503): the credential proxy is at its limit of 8 concurrent "
                   "commands and this request waited 60s without reaching a free slot; retrying in 1.7s (attempt 2)")


def a_decision(**overrides):
    """An --internal-ip decision for a same-subnet private cluster, which carries
    no remedy; override fields per test. `a_blocked_decision` is the shape with
    something to say."""
    base = dict(
        flags=("--internal-ip",), kind=gke_endpoint.KIND_INTERNAL_IP, address="10.10.0.2",
        same_network=True, authorized_networks=("10.0.0.0/8", "172.16.0.0/12"), remedy="",
    )
    base.update(overrides)
    return gke_endpoint.EndpointDecision(**base)


def a_blocked_decision(**overrides):
    """The public IP kept because the private endpoint's list does not admit the
    agent's Pod range: the decision the remedy exists for."""
    base = dict(flags=(), kind=gke_endpoint.KIND_IP, address="203.0.113.10",
                authorized_networks=("203.0.113.5/32",), remedy=ADMIT_REMEDY)
    base.update(overrides)
    return a_decision(**base)

MAX = cap.MAX_NAME_LEN  # 63


class ProfileNameTest(unittest.TestCase):
    def test_basic_shape(self):
        self.assertEqual(
            cap.profile_name("agentic-harness-demo", "kage-management", "us-central1"),
            "cluster-agentic-harness-demo-kage-management-us-central1",
        )

    def test_deterministic(self):
        a = cap.profile_name("p", "c", "us-central1")
        b = cap.profile_name("p", "c", "us-central1")
        self.assertEqual(a, b)

    def test_valid_profile_id_chars(self):
        # Only lowercase alnum + dashes; matches Hermes _PROFILE_ID_RE expectations.
        name = cap.profile_name("Proj_X", "My.Cluster", "US-Central1")
        self.assertRegex(name, r"^[a-z0-9][a-z0-9-]*$")
        self.assertLessEqual(len(name), MAX)

    def test_collapses_and_lowercases(self):
        # Uppercase + non-alnum runs collapse to single dashes.
        name = cap.profile_name("A__B", "c//d", "e")
        self.assertEqual(name, "cluster-a-b-c-d-e")

    def test_long_name_is_hashed_and_bounded(self):
        long_cluster = "x" * 120
        name = cap.profile_name("proj", long_cluster, "us-central1")
        self.assertLessEqual(len(name), MAX)
        # hashed form ends with -<8 hex>
        self.assertRegex(name, r"-[0-9a-f]{8}$")

    def test_long_name_stable_hash(self):
        long_cluster = "y" * 120
        self.assertEqual(
            cap.profile_name("proj", long_cluster, "loc"),
            cap.profile_name("proj", long_cluster, "loc"),
        )


class PinKubeconfigEnvTest(unittest.TestCase):
    def test_writes_kubeconfig_line(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        cap._pin_kubeconfig_env(home, kubeconfig)
        self.assertEqual((home / ".env").read_text(), f"KUBECONFIG={kubeconfig}\n")

    def test_idempotent_and_preserves_other_lines(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        (home / ".env").write_text("FOO=bar\nKUBECONFIG=/stale/path\n")
        cap._pin_kubeconfig_env(home, kubeconfig)
        cap._pin_kubeconfig_env(home, kubeconfig)  # second run must not duplicate
        text = (home / ".env").read_text()
        self.assertEqual(text.count("KUBECONFIG="), 1)
        self.assertIn("FOO=bar\n", text)
        self.assertIn(f"KUBECONFIG={kubeconfig}\n", text)


class PushSandboxLayoutTest(unittest.TestCase):
    """The mirror pass step 2e runs so the sandbox has somewhere to write.

    It shells out to another pod's worth of SSH round trips, and it runs on the
    reconcile path, so the two things worth pinning are that it is bounded and
    that it cannot take the scaffold down with it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mirror = self.tmp / "sandbox_mirror.py"
        self.mirror.write_text("")
        self._patch(cap, "SANDBOX_MIRROR", self.mirror)

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def push(self):
        with redirect_stderr(io.StringIO()) as err:
            cap._push_sandbox_layout("cluster-one")
        return err.getvalue()

    def test_the_mirror_call_is_bounded(self):
        # The reconcile engine calls this once per onboarded cluster. An
        # unbounded wait on a sandbox that is up but wedged stops the reconcile
        # for every cluster behind this one, not just this one.
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.push()
        self.assertEqual(
            run.call_args.kwargs["timeout"], cap.SANDBOX_MIRROR_TIMEOUT_SECONDS
        )
        argv = run.call_args.args[0]
        self.assertIn("--skeleton-only", argv)
        # This profile's home alone: every home on every scaffold made onboarding
        # N clusters O(N^2) (#3701).
        self.assertEqual(argv[argv.index("--profile") + 1], "cluster-one")
        # Past the mirror's own wait, so this timeout only fires when the
        # mirror itself is stuck rather than racing it.
        self.assertLess(
            float(argv[argv.index("--wait") + 1]), cap.SANDBOX_MIRROR_TIMEOUT_SECONDS
        )

    def test_a_mirror_that_hangs_does_not_fail_the_scaffold(self):
        # Without the sandbox layout the new profile's shell starts in the
        # machine home, which is where it starts anyway. Raising here would
        # instead leave the cluster unonboarded over a cosmetic difference.
        timed_out = subprocess.TimeoutExpired(
            cmd=[str(self.mirror)], timeout=cap.SANDBOX_MIRROR_TIMEOUT_SECONDS
        )
        with mock.patch.object(subprocess, "run", side_effect=timed_out):
            stderr = self.push()
        self.assertIn("could not push the profile layout", stderr)

    def test_a_mirror_that_exits_non_zero_does_not_fail_the_scaffold(self):
        failed = subprocess.CalledProcessError(1, [str(self.mirror)], "", "no route")
        with mock.patch.object(subprocess, "run", side_effect=failed):
            stderr = self.push()
        self.assertIn("could not push the profile layout", stderr)

    def test_an_install_without_the_mirror_script_runs_nothing(self):
        self._patch(cap, "SANDBOX_MIRROR", self.tmp / "absent.py")
        with mock.patch.object(subprocess, "run") as run:
            self.push()
        run.assert_not_called()



class RemoveSandboxLayoutTest(PushSandboxLayoutTest):
    """delete_profile's half of the mirror: the same script, the same bounds (#4389)."""

    def push(self):
        with redirect_stderr(io.StringIO()) as err:
            cap._remove_sandbox_layout("cluster-one")
        return err.getvalue()

    def test_the_mirror_call_is_bounded(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.push()
        self.assertEqual(run.call_args.kwargs["timeout"], cap.SANDBOX_MIRROR_TIMEOUT_SECONDS)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], str(self.mirror))
        self.assertIn("--skeleton-only", argv)
        self.assertEqual(argv[argv.index("--remove-profile") + 1], "cluster-one")
        self.assertNotIn("--profile", argv)
        self.assertLess(float(argv[argv.index("--wait") + 1]), cap.SANDBOX_MIRROR_TIMEOUT_SECONDS)

    def test_a_mirror_that_hangs_does_not_fail_the_scaffold(self):
        timed_out = subprocess.TimeoutExpired(cmd=[str(self.mirror)], timeout=cap.SANDBOX_MIRROR_TIMEOUT_SECONDS)
        with mock.patch.object(subprocess, "run", side_effect=timed_out):
            self.assertIn("could not remove the profile layout", self.push())

    def test_a_mirror_that_exits_non_zero_does_not_fail_the_scaffold(self):
        failed = subprocess.CalledProcessError(1, [str(self.mirror)], "", "no route")
        with mock.patch.object(subprocess, "run", side_effect=failed):
            self.assertIn("could not remove the profile layout", self.push())


class DeleteProfileTest(unittest.TestCase):
    """A delete removes the sandbox home too, and never a live profile's (#4389)."""

    NAME = "cluster-one"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home_root = self.tmp / "data"
        self.home = self.home_root / "profiles" / self.NAME
        (self.home / "workspace").mkdir(parents=True)
        for attr, value in (("HERMES_HOME", self.home_root), ("PROFILES_BASE", self.home_root / "profiles")):
            original = getattr(cap, attr)
            setattr(cap, attr, value)
            self.addCleanup(setattr, cap, attr, original)
        self.removed = []
        for target, replacement in (
            ("_remove_sandbox_layout", self.removed.append),
            # `hermes profile delete` is tolerated failing; here it is absent.
            ("HERMES_BIN", str(self.tmp / "no-hermes")),
        ):
            patcher = mock.patch.object(cap, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def delete(self):
        with redirect_stderr(io.StringIO()):
            cap.delete_profile(self.NAME)

    def test_a_delete_removes_the_home_here_and_then_in_the_sandbox(self):
        self.delete()
        self.assertFalse(self.home.exists())
        self.assertEqual(self.removed, [self.NAME])

    def test_a_home_still_here_after_the_delete_keeps_its_sandbox_home(self):
        # rmtree(ignore_errors=True) can leave the home behind; whatever is left, the
        # profile is not gone, and its sandbox home is not this code's to remove.
        with mock.patch.object(cap.shutil, "rmtree", lambda *a, **k: None):
            self.delete()
        self.assertTrue(self.home.exists())
        self.assertEqual(self.removed, [])

    def test_without_the_lock_the_sandbox_home_stays(self):
        # A stale skeleton costs disk; a removed live one costs a profile its kubeconfig.
        (self.home_root / cap.PROFILE_LOCKS_DIRNAME).write_text("a file where the lock dir goes")
        self.delete()
        self.assertFalse(self.home.exists())
        self.assertEqual(self.removed, [])

    def test_a_delete_waits_for_a_scaffold_of_the_same_name(self):
        # Held here as create_profile holds it: the delete must not touch either home
        # until the scaffold is done, or it removes the sandbox home the scaffold's
        # kubeconfig is about to land in.
        started = threading.Event()
        worker = threading.Thread(target=lambda: (started.set(), self.delete()))
        with cap._profile_lock(self.NAME) as locked:
            self.assertTrue(locked)
            worker.start()
            started.wait(5)
            worker.join(0.5)
            self.assertTrue(worker.is_alive(), "the delete ran while a scaffold held the lock")
            self.assertTrue(self.home.exists())
            self.assertEqual(self.removed, [])
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.removed, [self.NAME])

    def test_a_scaffold_waits_for_a_delete_of_the_same_name(self):
        entered = []
        with mock.patch.object(cap, "_create_profile", lambda *a: entered.append(a) or self.NAME), \
             mock.patch.object(cap, "TEMPLATE_DIR", self.tmp):
            worker = threading.Thread(target=cap.create_profile, args=("proj", "clu", "us-east1"))
            name = cap.profile_name("proj", "clu", "us-east1")
            with cap._profile_lock(name):
                worker.start()
                worker.join(0.5)
                self.assertTrue(worker.is_alive(), "the scaffold ran while a delete held the lock")
                self.assertEqual(entered, [])
            worker.join(10)
        self.assertEqual(len(entered), 1)

    def test_a_scaffold_without_the_lock_is_refused(self):
        entered = []
        (self.home_root / cap.PROFILE_LOCKS_DIRNAME).write_text("a file where the lock dir goes")
        with mock.patch.object(cap, "_create_profile", lambda *a: entered.append(a) or self.NAME), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            cap.create_profile("proj", "clu", "us-east1")
        self.assertIn("could not take the profile lock", str(refused.exception))
        self.assertEqual(entered, [])

    def test_an_unopenable_lock_is_logged_with_what_each_caller_does(self):
        (self.home_root / cap.PROFILE_LOCKS_DIRNAME).write_text("a file where the lock dir goes")
        with redirect_stderr(io.StringIO()) as err:
            cap.delete_profile(self.NAME)
        self.assertIn("could not open the profile lock", err.getvalue())
        self.assertIn("a create refuses, a delete leaves the sandbox home", err.getvalue())

    def test_other_names_do_not_wait(self):
        with cap._profile_lock("cluster-two"):
            worker = threading.Thread(target=self.delete)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.removed, [self.NAME])


class DeleteProfilesTest(unittest.TestCase):
    """A prune's deletes, with the sandbox removals batched into one mirror call (#4574)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home_root = self.tmp / "data"
        (self.home_root / "profiles").mkdir(parents=True)
        for attr, value in (("HERMES_HOME", self.home_root), ("PROFILES_BASE", self.home_root / "profiles")):
            original = getattr(cap, attr)
            setattr(cap, attr, value)
            self.addCleanup(setattr, cap, attr, original)
        self.calls = []
        self.answer = lambda names: {n: "removed" for n in names}
        self.keep_home = set()

        def delete_here(name):
            if name not in self.keep_home:
                shutil.rmtree(cap.profile_home(name), ignore_errors=True)

        self.all_names = []

        def remove(names):
            # #4467's guarantee, per name: its lock is held while its sandbox home goes.
            held = []
            for n in names:
                with profile_scaffold.profile_lock(self.home_root, n, blocking=False) as free:
                    held.append(not free)
            busy = 0
            for n in self.all_names:
                with profile_scaffold.profile_lock(self.home_root, n, blocking=False) as free:
                    busy += not free
            self.calls.append((list(names), held))
            self.busy.append(busy)
            return self.answer(names)

        self.busy = []

        for target, replacement in (("_delete_profile", delete_here), ("_remove_sandbox_layouts", remove)):
            patcher = mock.patch.object(cap, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def profiles(self, *names):
        self.all_names += names
        for name in names:
            (self.home_root / "profiles" / name / "workspace").mkdir(parents=True)
        return list(names)

    def delete(self, names):
        with redirect_stderr(io.StringIO()) as err:
            outcomes = cap.delete_profiles(names)
        self.log = err.getvalue()
        return outcomes

    def test_one_mirror_call_for_the_whole_prune(self):
        names = self.profiles("cluster-c", "cluster-a", "cluster-b")
        self.assertEqual(self.delete(names), {n: "deleted" for n in names})
        self.assertEqual(self.calls, [(["cluster-a", "cluster-b", "cluster-c"], [True, True, True])])
        for name in names:
            self.assertFalse(cap.profile_home(name).exists())

    def test_a_large_prune_runs_in_chunks_with_one_chunk_of_locks_held(self):
        names = self.profiles(*(f"cluster-{i}" for i in range(5)))
        with mock.patch.object(cap, "DELETE_BATCH_MAX", 2):
            outcomes = self.delete(names)
        self.assertEqual(outcomes, {n: "deleted" for n in names})
        self.assertEqual([len(c) for c, _ in self.calls], [2, 2, 1])
        for chunk, held in self.calls:
            self.assertTrue(all(held), chunk)
        # No more locks held at any call than one chunk: earlier chunks' are released.
        self.assertEqual(self.busy, [2, 2, 1])
        for name in names:
            with profile_scaffold.profile_lock(self.home_root, name, blocking=False) as free:
                self.assertTrue(free, f"{name}'s lock was still held after the batch")

    def test_a_held_lock_skips_that_name_alone(self):
        names = self.profiles("cluster-a", "cluster-busy", "cluster-c")
        with profile_scaffold.profile_lock(self.home_root, "cluster-busy") as held:
            self.assertTrue(held)
            outcomes = self.delete(names)
        self.assertEqual(outcomes["cluster-busy"], "locked")
        self.assertTrue(cap.profile_home("cluster-busy").exists())
        self.assertEqual(self.calls[0][0], ["cluster-a", "cluster-c"])
        self.assertEqual(outcomes["cluster-a"], "deleted")
        self.assertIn("cluster-busy: not deleted this run, its profile lock is held", self.log)

    def test_a_home_that_survives_the_delete_keeps_its_sandbox_home(self):
        names = self.profiles("cluster-a", "cluster-stays")
        self.keep_home.add("cluster-stays")
        outcomes = self.delete(names)
        self.assertEqual(outcomes, {"cluster-a": "deleted", "cluster-stays": "home-remains"})
        self.assertEqual(self.calls[0][0], ["cluster-a"])

    def test_an_unopenable_lock_deletes_here_and_keeps_the_sandbox_home(self):
        # As delete_profile without its lock; skipping outright would keep the
        # project retiring for as long as the lock directory stays broken.
        names = self.profiles("cluster-a", "cluster-b")
        real = profile_scaffold.profile_lock

        def lock(root, name, blocking=True, on_error=None):
            if name == "cluster-b":
                on_error(OSError(30, "Read-only file system"))
                return contextlib.nullcontext(False)
            return real(root, name, blocking=blocking, on_error=on_error)

        with mock.patch.object(cap, "profile_lock", side_effect=lock):
            outcomes = self.delete(names)
        self.assertEqual(outcomes, {"cluster-a": "deleted", "cluster-b": "sandbox-unlocked"})
        self.assertFalse(cap.profile_home("cluster-b").exists())
        self.assertEqual(self.calls[0][0], ["cluster-a"])

    def test_the_mirror_s_answer_is_reported_per_name(self):
        names = self.profiles("cluster-a", "cluster-b", "cluster-c", "cluster-d")
        self.answer = lambda ns: {"cluster-a": "removed", "cluster-b": "left",
                                  "cluster-c": "refused", "cluster-d": "unreached"}
        self.assertEqual(self.delete(names), {"cluster-a": "deleted", "cluster-b": "sandbox-left",
                                              "cluster-c": "sandbox-refused", "cluster-d": "sandbox-unreached"})


class RemoveSandboxLayoutsTest(unittest.TestCase):
    """The batched mirror call: one argv for every name, one result line read back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        mirror = self.tmp / "sandbox_mirror.py"
        mirror.write_text("")
        patcher = mock.patch.object(cap, "SANDBOX_MIRROR", mirror)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, stdout, returncode=0):
        with mock.patch.object(subprocess, "run") as run, redirect_stderr(io.StringIO()):
            run.return_value = subprocess.CompletedProcess([], returncode, stdout, "")
            outcomes = cap._remove_sandbox_layouts(["cluster-a", "cluster-b", "cluster-c"])
        return outcomes, run

    def test_one_call_names_every_profile_and_reads_each_outcome(self):
        line = cap.MIRROR_REMOVE_RESULT_PREFIX + json.dumps(
            {"removed": ["cluster-a"], "left": ["cluster-b"], "refused": []})
        outcomes, run = self.run_with(f"sandbox-mirror: some log line\n{line}\n", returncode=2)
        self.assertEqual(outcomes, {"cluster-a": "removed", "cluster-b": "left", "cluster-c": "unreached"})
        argv = run.call_args.args[0]
        self.assertEqual(run.call_count, 1)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--remove-profile"],
                         ["cluster-a", "cluster-b", "cluster-c"])
        self.assertEqual(run.call_args.kwargs["timeout"], cap.SANDBOX_MIRROR_TIMEOUT_SECONDS)

    def test_an_install_with_no_sandbox_has_nothing_to_remove(self):
        # The mirror exits 0 before reading the names when there is no sandbox.
        outcomes, _ = self.run_with("sandbox-mirror: no ssh terminal backend in the managed config; no sandbox to mirror\n")
        self.assertEqual(set(outcomes.values()), {"removed"})

    def test_no_result_line_is_every_name_unreached(self):
        for out in ("", "sandbox-mirror: the sandbox did not answer\n", cap.MIRROR_REMOVE_RESULT_PREFIX + "not json\n"):
            with self.subTest(out=out):
                outcomes, _ = self.run_with(out, returncode=2)
                self.assertEqual(set(outcomes.values()), {"unreached"})

    def test_the_prefix_is_the_mirror_s(self):
        import importlib.util

        mirror = Path(__file__).resolve().parents[3] / "deploy" / "shared" / "sandbox_mirror.py"
        spec = importlib.util.spec_from_file_location("sandbox_mirror_for_prefix", mirror)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(cap.MIRROR_REMOVE_RESULT_PREFIX, module.REMOVE_RESULT_PREFIX)

# --- The runtime scaffold path --------------------------------------------------
#
# A cluster profile is created when its cluster is onboarded, which is not a pod start:
# nothing rolls the agent, so this path — not docker-entrypoint.sh — is the only thing
# that can give the new profile the operator's tuning and the plugins targeted at it.


class CreateProfileTest(unittest.TestCase):
    PROJECT, CLUSTER, LOCATION = "proj", "clu", "us-east1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.home_root = self.tmp / "data"
        self.template = self.tmp / "cluster-template"
        self.overlay_dir = self.tmp / "agent-config"
        self.mounts = self.tmp / "agent-plugins"
        self.template.mkdir()
        self.overlay_dir.mkdir()
        (self.template / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["hermes_otel"]}, "toolsets": ["kanban"]})
        )

        self.name = cap.profile_name(self.PROJECT, self.CLUSTER, self.LOCATION)
        self.profile = self.home_root / "profiles" / self.name

        self._patch(cap, "HERMES_HOME", self.home_root)
        self._patch(cap, "PROFILES_BASE", self.home_root / "profiles")
        self._patch(cap, "TEMPLATE_DIR", self.template)
        self._patch(cap, "SHARED_PLUGINS_DIR", self.tmp / "shared-plugins")
        self._patch(cap, "OVERLAY_DIR", self.overlay_dir)
        self._patch(cap, "PLUGIN_MOUNT_ROOT", self.mounts)
        # `hermes profile create` — the real one registers the profile and makes its home.
        self._patch(cap, "ensure_profile", self._fake_ensure_profile)
        # `gcloud container clusters get-credentials`.
        self.runs = []
        self.writes_kubeconfig = True
        self._patch(subprocess, "run", self._fake_run)
        # Which endpoint that command names. Patched explicitly rather than
        # left to fall out of the fake above: gke_endpoint reads gcloud's help
        # text to decide the flag exists at all, the fake answers every
        # command with empty stdout, and the resulting "no flag" would be an
        # accident of the mock rather than a decision the test made. The
        # predicate itself is covered in test_gke_endpoint.py.
        self.decision = None
        self.decision_kwargs = {}

        def decide(*a, **k):
            self.decision_kwargs = k
            return self.decision

        self._patch(cap, "endpoint_decision", decide)
        # Whether the connectivity probe after the scaffold succeeds.
        self.probe_exit = 0
        self.probe_stderr = ""
        # The managed terminal pin needs Hermes, which is not installed here; its own
        # behaviour is covered in tests/test_terminal_env_pin.py and at image build.
        self.pinned = []
        self.pin_error = None
        self._patch(terminal_env_pin, "pin", self._fake_pin)

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def _fake_pin(self, home):
        self.pinned.append((Path(home), (Path(home) / "USER.md").exists()))
        if self.pin_error:
            raise self.pin_error
        return True

    def _fake_ensure_profile(self, name, description, hermes_home):
        home = Path(hermes_home) / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "profile.yaml").write_text(f"name: {name}\n")
        return home

    def _fake_run(self, cmd, **kwargs):
        self.runs.append(cmd)
        if cmd[:2] == ["kubectl", "version"]:
            return subprocess.CompletedProcess(
                cmd, self.probe_exit, "Client Version: v1.33.0\n", self.probe_stderr)
        # Real gcloud writes the file its KUBECONFIG names, and the scaffold
        # checks that it did before calling the profile finished. A fake that
        # exits 0 without writing is a fake of the failure, not of the success.
        if (
            self.writes_kubeconfig
            and cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]
        ):
            path = Path((kwargs.get("env") or {})["KUBECONFIG"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("apiVersion: v1\nkind: Config\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def get_credentials_argv(self):
        for cmd in self.runs:
            if cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                return cmd
        raise AssertionError(f"no get-credentials call in {self.runs}")

    def mount(self, plugin):
        d = self.mounts / self.name / plugin
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
        return d

    def create(self):
        with redirect_stderr(io.StringIO()) as err:
            name = cap.create_profile(self.PROJECT, self.CLUSTER, self.LOCATION)
        self.stderr = err.getvalue()
        return name

    def config(self):
        return yaml.safe_load((self.profile / "config.yaml").read_text()) or {}

    def test_a_gcloud_that_wrote_nothing_is_not_a_scaffolded_profile(self):
        """Exit 0 from gcloud is not the same statement as a credential.

        The command runs in the sandbox and writes there, so this pod cannot
        see the file either way. Reporting the profile as created leaves every
        later kubectl failing with an error that names the cluster, and sends
        whoever reads it to IAM instead of to the pod that never got a
        kubeconfig.
        """
        self.writes_kubeconfig = False

        with self.assertRaises(SystemExit) as caught:
            self.create()

        self.assertIn("kubeconfig", str(caught.exception))
        # And the pin that follows it did not happen, so nothing points a
        # worker at a file that is not there.
        env_file = self.profile / ".env"
        self.assertNotIn("KUBECONFIG", env_file.read_text() if env_file.exists() else "")

    def test_pins_the_managed_terminal_before_writing_user_md(self):
        self.create()
        self.assertEqual([(self.profile, False)], self.pinned)
        self.assertTrue((self.profile / "USER.md").exists())

    def test_a_failed_terminal_pin_leaves_the_profile_unfinished(self):
        """USER.md is what reconcile and the roster read as "finished".

        A re-scaffold starts with the USER.md an earlier run wrote, so a failed pin
        has to remove it, or the profile keeps counting as usable while its
        scheduled runs would not use the managed terminal.
        """
        self.create()
        self.pin_error = terminal_env_pin.PinError("Hermes resolves TERMINAL_ENV='local'")

        with self.assertRaises(SystemExit) as caught:
            self.create()

        self.assertIn(self.name, str(caught.exception))
        self.assertIn("TERMINAL_ENV='local'", str(caught.exception))
        self.assertFalse((self.profile / "USER.md").exists())

    def test_the_kubeconfig_is_looked_for_where_kubectl_will_run(self):
        """With a sandbox the file is on its volume, not on this one."""
        missing = self.tmp / "nowhere" / "kubeconfig.yaml"
        with mock.patch.object(cap.sandbox_exec, "sandbox_enabled", return_value=True):
            probe = subprocess.CompletedProcess(["test"], 0, "", "")
            with mock.patch.object(cap.sandbox_exec, "run", return_value=probe) as run:
                self.assertTrue(cap.kubeconfig_landed(missing))
            self.assertEqual(run.call_args[0][0][1:], ["-s", str(missing)])

            probe = subprocess.CompletedProcess(["test"], 1, "", "")
            with mock.patch.object(cap.sandbox_exec, "run", return_value=probe):
                self.assertFalse(cap.kubeconfig_landed(missing))

            # A sandbox that went away between the fetch and the check answers
            # nothing, which is not the same as answering yes.
            unavailable = cap.sandbox_exec.SandboxUnavailable("gone")
            with mock.patch.object(cap.sandbox_exec, "run", side_effect=unavailable):
                self.assertFalse(cap.kubeconfig_landed(missing))

    def test_fetches_credentials_over_the_ip_endpoint_by_default(self):
        self.create()
        self.assertEqual(
            self.get_credentials_argv(),
            [
                "gcloud", "container", "clusters", "get-credentials", self.CLUSTER,
                f"--location={self.LOCATION}",
                f"--project={self.PROJECT}",
            ],
        )

    def test_the_credential_fetch_runs_as_the_login_that_owns_the_profile(self):
        """Step 2e mirrors the profile directory in as `agent:agent` 0755.

        The default `hermes` login is uid 1001 and cannot create a file in it,
        so a get-credentials that kept the default exits on EACCES and the
        profile is scaffolded without the kubeconfig every later kubectl reads.
        The alternative — widening the directory so uid 1001 could write into a
        tree uid 1000 owns — is what this assertion exists instead of.
        """
        original = cap.sandbox_exec.run
        principals = {}

        def spy(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                principals["get-credentials"] = kwargs.get(
                    "principal", cap.sandbox_exec.SANDBOX_PRINCIPAL
                )
            return original(argv, **kwargs)

        self._patch(cap.sandbox_exec, "run", spy)
        self.create()

        self.assertEqual(
            principals.get("get-credentials"),
            cap.sandbox_exec.TERMINAL_PRINCIPAL,
            "get-credentials writes into the agent-owned profile directory",
        )

    def test_fetches_credentials_over_the_dns_endpoint_when_detected(self):
        # An onboarded cluster whose control plane is only reachable by DNS. The
        # profile's pinned kubeconfig is what the Cluster Agent runs against for
        # its whole life, so the flag has to be present when it is scaffolded.
        self.decision = a_decision(flags=("--dns-endpoint",), kind=gke_endpoint.KIND_DNS,
                                   address="gke-x.gke.goog", same_network=None, remedy="")

        self.create()

        self.assertEqual(self.get_credentials_argv()[-1], "--dns-endpoint")

    def test_fetches_credentials_over_the_private_endpoint_when_detected(self):
        self.decision = a_decision()
        self.create()
        self.assertEqual(self.get_credentials_argv()[-1], "--internal-ip")

    def test_writes_the_endpoint_decision_as_preflight_readable_bullets(self):
        self.decision = a_blocked_decision()
        self.create()
        user_md = (self.profile / "USER.md").read_text()
        for key, value in {
            "endpoint": "ip",
            "endpoint-address": "203.0.113.10",
            "authorized-networks": "203.0.113.5/32",
            "endpoint-remedy": ADMIT_REMEDY,
        }.items():
            found = re.findall(rf"^[ \t]*-[ \t]*{key}:[ \t]*(.*)$", user_md, re.MULTILINE)
            self.assertTrue(found, f"`{key}` is not a `- {key}:` bullet")
            self.assertEqual(found[0].strip(), value)

    def test_a_decision_without_a_remedy_writes_no_remedy_bullet(self):
        self.decision = a_decision(authorized_networks=None)
        self.create()
        user_md = (self.profile / "USER.md").read_text()
        self.assertIn("- endpoint: internal-ip\n", user_md)
        self.assertIn("- authorized-networks: unrestricted\n", user_md)
        self.assertNotIn("endpoint-remedy", user_md)

    def test_the_remedy_bullet_is_framed_as_conditional_for_the_cluster_agent(self):
        # USER.md is the Cluster Agent's startup context; a remedy written there
        # for a cluster that answers must not read as an instruction to act.
        self.decision = a_blocked_decision()
        self.create()
        user_md = (self.profile / "USER.md").read_text()
        self.assertIn("only if `kubectl` cannot reach the API server", user_md)

    def test_the_scaffold_asks_for_the_own_cluster_past_any_backoff(self):
        # One transient failure in a reconcile sweep must not blank the record
        # of every cluster scaffolded in the following minute.
        self.decision = a_decision()
        self.create()
        self.assertIs(self.decision_kwargs.get("retry_own"), True)

    def test_a_server_answered_timeout_names_no_remedy(self):
        # "Error from server" fronts every answer the API server gave; a
        # degraded etcd that timed out was reached, and the list cannot help.
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = "Error from server: etcdserver: request timed out\n"
        self.create()
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertNotIn(ADMIT_REMEDY, self.stderr)

    def test_a_provisional_decision_is_not_recorded(self):
        # The scaffold's own describe of the agent cluster failed; the broker
        # decides again for the unflagged fetch and may splice --internal-ip,
        # so a record of this fallback would misstate what the kubeconfig names.
        self.decision = a_blocked_decision(provisional=True)
        self.create()
        user_md = (self.profile / "USER.md").read_text()
        self.assertNotIn("- endpoint", user_md)
        self.assertNotIn("endpoint-remedy", user_md)
        self.assertEqual(self.get_credentials_argv()[-1], f"--project={self.PROJECT}")

    def test_a_settled_but_empty_decision_writes_no_endpoint_bullets(self):
        # A gcloud without --dns-endpoint decides nothing about the cluster:
        # the answer is settled (no flag) but names no endpoint to record.
        self.decision = a_decision(flags=(), kind=gke_endpoint.KIND_IP, address="",
                                   same_network=None, authorized_networks=None)
        self.create()
        user_md = (self.profile / "USER.md").read_text()
        self.assertNotIn("- endpoint", user_md)

    def test_no_decision_writes_no_endpoint_bullets(self):
        self.decision = None
        self.create()
        user_md = (self.profile / "USER.md").read_text()
        self.assertNotIn("- endpoint", user_md)
        self.assertNotIn("authorized-networks", user_md)

    def test_probes_the_cluster_once_after_get_credentials_with_the_request_timeout(self):
        self.decision = a_decision()
        self.create()
        probe = [c for c in self.runs if c[:2] == ["kubectl", "version"]]
        self.assertEqual(len(probe), 1)
        self.assertIn(f"--request-timeout={cap.CONNECTIVITY_PROBE_REQUEST_TIMEOUT}", probe[0])
        # After get-credentials, so the kubeconfig exists to read.
        self.assertGreater(self.runs.index(probe[0]), self.runs.index(self.get_credentials_argv()))

    def test_the_probe_runs_as_the_login_that_owns_the_kubeconfig(self):
        """get-credentials writes the kubeconfig as the `agent` login, 0600. The
        default `hermes` login cannot read it, and the credential-proxy shim
        then refuses the kubectl with "kubeconfig is unreadable" before any
        connection is attempted -- seen live on gkedemos. The probe has to run
        as the same login the Cluster Agent's own kubectl runs as."""
        self.decision = a_decision()
        original = cap.sandbox_exec.run
        principals = {}

        def spy(argv, **kwargs):
            if argv[:2] == ["kubectl", "version"]:
                principals["probe"] = kwargs.get("principal", cap.sandbox_exec.SANDBOX_PRINCIPAL)
            return original(argv, **kwargs)

        self._patch(cap.sandbox_exec, "run", spy)
        self.create()
        self.assertEqual(principals.get("probe"), cap.sandbox_exec.TERMINAL_PRINCIPAL)

    def test_a_failed_probe_logs_the_endpoint_and_the_remedy(self):
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = "Client Version: v1.33.0\nUnable to connect to the server: dial tcp 203.0.113.10:443: i/o timeout\n"
        name = self.create()
        self.assertEqual(name, self.name, "an unreachable cluster is still a scaffolded profile")
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertIn("203.0.113.5/32", self.stderr)
        self.assertIn(ADMIT_REMEDY, self.stderr)
        self.assertIn("dial tcp 203.0.113.10:443: i/o timeout", self.stderr)
        self.assertNotIn("Client Version", self.stderr)

    def test_the_connection_failure_pattern_matches_the_preflight_copy(self):
        """cluster_preflight.sh keeps its own CONNECTIVITY_FAILURE_RE for check 5;
        the two decide the same question and must stay one pattern."""
        script = (Path(__file__).resolve().parent / "cluster_preflight.sh").read_text()
        found = re.search(r"^readonly CONNECTIVITY_FAILURE_RE='([^']*)'$", script, re.MULTILINE)
        self.assertTrue(found, "cluster_preflight.sh no longer declares CONNECTIVITY_FAILURE_RE")
        self.assertEqual(found.group(1), cap.CONNECTIVITY_FAILURE_RE.pattern)
        excluded = re.search(r"^readonly NOT_A_CONNECTION_FAILURE_RE='([^']*)'$", script, re.MULTILINE)
        self.assertTrue(excluded, "cluster_preflight.sh no longer declares NOT_A_CONNECTION_FAILURE_RE")
        self.assertEqual(excluded.group(1), cap.NOT_A_CONNECTION_FAILURE_RE.pattern)

    def test_a_probe_refused_by_rbac_names_the_endpoint_but_not_the_remedy(self):
        # A 403 is an IAM answer from a reachable control plane; sending the
        # operator to authorized networks for it would be wrong.
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = ('Error from server (Forbidden): forbidden: User "sa@x.iam.gserviceaccount.com" '
                             'cannot get path "/version"\n')
        self.create()
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertIn("Forbidden", self.stderr)
        self.assertNotIn(ADMIT_REMEDY, self.stderr)

    def test_a_certificate_failure_under_kubectls_generic_prefix_names_no_remedy(self):
        # kubectl prefixes every transport failure with "Unable to connect to
        # the server:", x509 and auth-plugin errors included; the list cannot
        # fix those, so the prefix alone must not earn the remedy.
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = ("Unable to connect to the server: x509: certificate signed by unknown authority\n")
        self.create()
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertNotIn(ADMIT_REMEDY, self.stderr)

    def test_a_probe_the_shim_refused_names_no_remedy(self):
        # "credential proxy unavailable" is the broker being down, not the
        # cluster's list; the words "connection refused" inside it must not win.
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = "credential proxy unavailable: [Errno 111] Connection refused\n"
        self.create()
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertNotIn(ADMIT_REMEDY, self.stderr)

    def test_the_shim_exclusion_matches_the_shims_prefixes_and_not_a_mention(self):
        # The shim's own messages start "credential proxy:" or "credential proxy
        # unavailable"; preflight's cap text merely mentions "the credential
        # proxy's admission wait" and is a real timeout. The shim's busy line is
        # not terminal: the retried attempt's kubectl writes after it, so it
        # must not decide the whole stderr.
        for shim in ("credential proxy unavailable: [Errno 111] Connection refused",
                     "credential proxy: kubeconfig is unreadable: /x: [Errno 13] Permission denied",
                     "credential proxy token unavailable: no token file",
                     "credential proxy error (HTTP 502): non-JSON response",
                     "credential proxy throttled: gave up after 2 attempts (HTTP 503): the credential proxy is at "
                     "its child memory budget (704 MiB in use of 704 MiB) and this request timed out waiting"):
            self.assertTrue(cap.NOT_A_CONNECTION_FAILURE_RE.search(shim), shim)
        for not_shim in ("timed out after 95s, the preflight's cap on one brokered call (the credential proxy's 60s admission wait plus 30s)",
                         BUSY_RETRY_LINE):
            self.assertFalse(cap.NOT_A_CONNECTION_FAILURE_RE.search(not_shim), not_shim)

    def test_a_probe_refused_busy_once_and_then_unreachable_still_earns_the_remedy(self):
        # The shim printed its busy line, waited, was admitted, and the kubectl
        # it then ran never reached the endpoint: the case the remedy is for.
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = BUSY_RETRY_LINE + "\nUnable to connect to the server: dial tcp 203.0.113.10:443: i/o timeout\n"
        self.create()
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertIn(ADMIT_REMEDY, self.stderr)
        self.assertIn("kubectl: Unable to connect to the server", self.stderr)

    def test_a_probe_the_shim_gave_up_on_names_no_remedy(self):
        # Busy, retried, then throttled: the shim exited without asking the
        # cluster, so the list is not what stood in the way.
        self.decision = a_blocked_decision()
        self.probe_exit = 1
        self.probe_stderr = (BUSY_RETRY_LINE + "\ncredential proxy throttled: gave up after 2 attempts (HTTP 503): "
                             "the credential proxy is at its limit of 8 concurrent commands and this request waited "
                             "60s without reaching a free slot; retry shortly\n")
        self.create()
        self.assertIn("ip endpoint (203.0.113.10)", self.stderr)
        self.assertNotIn(ADMIT_REMEDY, self.stderr)

    def test_a_passing_probe_logs_nothing_about_the_endpoint(self):
        self.decision = a_decision()
        self.create()
        self.assertNotIn("cannot reach", self.stderr)

    def test_a_failed_probe_without_a_decision_still_names_the_failure(self):
        self.decision = None
        self.probe_exit = 1
        self.probe_stderr = "Unable to connect to the server: dial tcp: i/o timeout\n"
        self.create()
        self.assertIn("cannot reach the cluster API server", self.stderr)
        self.assertIn("i/o timeout", self.stderr)

    def test_the_probe_tells_the_shim_a_deadline_under_which_no_retry_fits(self):
        # The probe's bound is shorter than one admission wait, so a retry could
        # only end in the probe's own kill with the refusal unread; the shim is
        # told a deadline that admits none and prints the first refusal.
        self.decision = a_decision()
        original = self._fake_run
        probe_envs = []

        def recording(cmd, **kwargs):
            if cmd[:2] == ["kubectl", "version"]:
                probe_envs.append(kwargs["env"])
            return original(cmd, **kwargs)

        self._patch(subprocess, "run", recording)
        self.assertEqual(self.create(), self.name)
        client = credential_proxy_client
        # The name is spelled in the script, not imported (its constants comment says why).
        self.assertEqual(client.RETRY_DEADLINE_ENV, cap.SHIM_RETRY_DEADLINE_ENV)
        self.assertEqual([str(cap.PROBE_SHIM_RETRY_DEADLINE_SECONDS)], [env[cap.SHIM_RETRY_DEADLINE_ENV] for env in probe_envs])
        deadline = float(cap.PROBE_SHIM_RETRY_DEADLINE_SECONDS)
        self.assertEqual(max(0, cap.CONNECTIVITY_PROBE_TIMEOUT_SECONDS - cap.BROKER_KUBECTL_RUN_SECONDS), deadline)
        shortest = client.BROKER_ADMISSION_WAIT_SECONDS + client.RETRY_JITTER_FLOOR * client.RETRY_BACKOFF_BASE_SECONDS
        self.assertGreater(shortest, deadline)
        self.assertIsNone(client.retry_wait(1, 0.0, None, random.Random(1), lambda: 0.0, deadline=deadline))

    def test_the_credential_fetch_tells_the_shim_a_deadline_under_which_no_retry_fits(self):
        # The fetch's 60 s cap holds one admission wait at most, so a held refusal
        # cannot reach a retry; the watermark gate's 429 chain, answered at once
        # with a 5 to 30 s Retry-After, can, and about half the draws would carry
        # the fetch past its cap with "timed out" standing in for the refusal. The
        # shim is told the cap less the broker's gcloud run bound, which admits none.
        original = self._fake_run
        fetch_calls = []

        def recording(cmd, **kwargs):
            if cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                fetch_calls.append(kwargs)
            return original(cmd, **kwargs)

        self._patch(subprocess, "run", recording)
        self.assertEqual(self.create(), self.name)
        client = credential_proxy_client
        self.assertEqual(client.RETRY_DEADLINE_ENV, cap.SHIM_RETRY_DEADLINE_ENV)
        self.assertEqual(1, len(fetch_calls))
        self.assertEqual(cap.CREDENTIAL_FETCH_TIMEOUT_SECONDS, fetch_calls[0]["timeout"])
        self.assertEqual("0", fetch_calls[0]["env"][cap.SHIM_RETRY_DEADLINE_ENV])
        deadline = float(cap.CREDENTIAL_FETCH_SHIM_RETRY_DEADLINE_SECONDS)
        self.assertEqual(max(0, cap.CREDENTIAL_FETCH_TIMEOUT_SECONDS - cap.BROKER_GCLOUD_RUN_SECONDS), deadline)
        shortest = client.BROKER_ADMISSION_WAIT_SECONDS + client.RETRY_JITTER_FLOOR * client.RETRY_BACKOFF_BASE_SECONDS
        self.assertGreater(shortest, deadline)
        self.assertIsNone(client.retry_wait(1, 0.0, None, random.Random(1), lambda: 0.0, deadline=deadline))
        self.assertIsNone(client.retry_wait(1, 0.0, "5", random.Random(1), lambda: 0.0, deadline=deadline))

    def test_a_probe_that_times_out_does_not_fail_the_scaffold(self):
        self.decision = a_decision()
        original = self._fake_run

        def hanging(cmd, **kwargs):
            if cmd[:2] == ["kubectl", "version"]:
                raise subprocess.TimeoutExpired(cmd, cap.CONNECTIVITY_PROBE_TIMEOUT_SECONDS)
            return original(cmd, **kwargs)

        self._patch(subprocess, "run", hanging)
        self.assertEqual(self.create(), self.name)
        self.assertIn("connectivity probe did not finish", self.stderr)

    def test_applies_the_cluster_class_overlay_at_scaffold_time(self):
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"api_max_retries": 8, "max_turns": 150}})
        )

        self.assertEqual(self.create(), self.name)

        cfg = self.config()
        self.assertEqual(cfg["agent"], {"api_max_retries": 8, "max_turns": 150})
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel"], "the template's config must survive")
        self.assertEqual(
            cfg["cluster_identity"],
            {"project": self.PROJECT, "cluster": self.CLUSTER, "location": self.LOCATION},
            "the identity stamp the reconciler matches on must survive the merge",
        )

    def test_links_and_enables_a_plugin_targeting_this_cluster(self):
        self.mount("clusterone")
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"max_turns": 150}})
        )
        (self.overlay_dir / f"profile-{self.name}.overlay.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["clusterone"]}})
        )

        self.create()

        link = self.profile / "plugins" / "clusterone"
        self.assertTrue(link.is_symlink(), "the plugin mounted for this profile must be linked in")
        self.assertTrue((link / "__init__.py").is_file())
        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "clusterone"], "and enabled")
        self.assertEqual(cfg["agent"]["max_turns"], 150, "alongside the class-wide tuning")

    def test_no_operator_overlays_is_not_a_failure(self):
        """A deployment without the operator has no /opt/agent-config and no mounts."""
        self._patch(cap, "OVERLAY_DIR", self.tmp / "does-not-exist")
        self._patch(cap, "PLUGIN_MOUNT_ROOT", self.tmp / "also-missing")

        self.assertEqual(self.create(), self.name)

        cfg = self.config()
        self.assertNotIn("agent", cfg, "nothing to apply means nothing applied")
        self.assertIn("cluster_identity", cfg)

    def test_rescaffolding_does_not_double_apply(self):
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"max_turns": 150}, "plugins": {"enabled": ["extra"]}})
        )
        self.create()
        first = self.config()
        self.create()
        self.assertEqual(self.config(), first)

    def test_every_user_md_field_is_readable_by_preflight(self):
        """USER.md's shape is a contract with cluster_preflight.sh's user_md_field().

        That reader matches only `- <key>: <value>` bullets. The kubeconfig line
        shipped for a long time without the leading `- `, so it parsed as
        nothing — and it is precisely the line a human edits when trying to
        repair a bad pin. Assert all four fields are bullets, so the writer
        cannot drift back out of the shape the reader can see.
        """
        self.create()
        user_md = (self.profile / "USER.md").read_text().lower()

        expected = {
            "project": self.PROJECT,
            "cluster": self.CLUSTER,
            "location": self.LOCATION,
            "kubeconfig": str(self.profile / "kubeconfig.yaml").lower(),
        }
        for key, value in expected.items():
            # The Python equivalent of the script's
            # `sed -n "s/^[[:space:]]*-[[:space:]]*<key>:[[:space:]]*//p"`.
            found = re.findall(rf"^[ \t]*-[ \t]*{key}:[ \t]*(.*)$", user_md, re.MULTILINE)
            self.assertTrue(found, f"`{key}` is not a `- {key}:` bullet, so preflight cannot read it")
            self.assertEqual(found[0].strip(), value)

    def test_preflight_still_parses_user_md_with_a_dash_anchor(self):
        """The other half of the contract above: if the reader stops requiring
        the `- ` anchor, this test is the note that USER.md's shape was written
        to satisfy it and can be relaxed too."""
        script = (Path(__file__).parent / "cluster_preflight.sh").read_text()
        self.assertIn(
            r"s/^[[:space:]]*-[[:space:]]*$1:[[:space:]]*//p",
            script,
            "user_md_field() changed; re-check the USER.md format written by create_profile",
        )

    def test_stamps_target_cluster_identity_into_soul_md(self):
        """SOUL.md is injected into the Cluster Agent system prompt on turn 1.
        Stamping the target cluster identity there keeps the agent from spending
        tool calls searching for USER.md when user_profile_enabled is false."""
        (self.template / "SOUL.md").write_text(
            "# Cluster Agent — Soul\n\n---\n\n## 1. Core Truths\n",
            encoding="utf-8",
        )
        self.create()
        soul = (self.profile / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("## Target Cluster Identity", soul)
        self.assertIn(f"- project: {self.PROJECT}", soul)
        self.assertIn(f"- cluster: {self.CLUSTER}", soul)
        self.assertIn(f"- location: {self.LOCATION}", soul)
        # Re-scaffolding is idempotent and does not duplicate the section.
        self.create()
        soul_again = (self.profile / "SOUL.md").read_text(encoding="utf-8")
        self.assertEqual(soul_again.count("## Target Cluster Identity"), 1)
        self.assertEqual(soul_again, soul)


    # --- telemetry ---
    #
    # The profile copies the image's plugin config, so it also copies its endpoint.
    # Nothing rolls the pod when a cluster is onboarded, so the entrypoint's startup sweep
    # never sees this profile: without the scaffold-time pin, a cluster agent would export
    # to the GKE managed collector no matter what the operator resolved.

    BAKED = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces"
    CUSTOM = "http://otel-collector.otel-collector.svc.cluster.local:4318"

    def bake_shared_plugin(self):
        """The hermes_otel config as the image ships it, ready for the overlay to copy."""
        shared = self.tmp / "shared-plugins" / "hermes_otel"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "config.yaml").write_text(
            yaml.safe_dump({"backends": [{"name": "gke-managed-otel", "type": "otlp", "endpoint": self.BAKED}]})
        )

    def plugin_config(self):
        return yaml.safe_load((self.profile / "plugins" / "hermes_otel" / "config.yaml").read_text())

    def test_pins_the_resolved_endpoint(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": self.CUSTOM, "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertEqual(cfg["backends"][0]["endpoint"], self.CUSTOM + "/v1/traces")
        self.assertEqual(cfg["resource_attributes"]["service.name"], "agent-gateway")

    def test_unset_endpoint_keeps_the_image_default(self):
        self.bake_shared_plugin()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            os.environ.pop("OTEL_SERVICE_NAME", None)
            os.environ.pop("OTEL_SDK_DISABLED", None)
            os.environ.pop("HERMES_OTEL_ENABLED", None)
            self.create()

        self.assertEqual(self.plugin_config()["backends"][0]["endpoint"], self.BAKED)

    def test_disabled_telemetry_disables_plugin_and_clears_backends(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_SDK_DISABLED": "true", "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_hermes_otel_enabled_false_disables_plugin(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"HERMES_OTEL_ENABLED": "false", "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_hermes_otel_enabled_true_overrides_otel_sdk_disabled(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_OTEL_ENABLED": "true",
                "OTEL_SDK_DISABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": self.CUSTOM,
            },
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertTrue(cfg.get("enabled", True))
        self.assertEqual(cfg["backends"][0]["endpoint"], self.CUSTOM + "/v1/traces")

    def test_disabled_telemetry_handles_mixed_case(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_SDK_DISABLED": "TRUE", "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])


class ResolveProfilesBaseTest(unittest.TestCase):
    def test_resolves_when_hermes_home_is_set(self):
        import importlib
        try:
            with mock.patch.dict(os.environ, {"HERMES_HOME": "/custom/data"}, clear=True):
                reloaded = importlib.reload(cap)
                self.assertEqual(reloaded.HERMES_HOME, Path("/custom/data"))
                self.assertEqual(reloaded.PROFILES_BASE, Path("/custom/data/profiles"))
                self.assertEqual(reloaded._run_env()["HERMES_HOME"], "/custom/data")
        finally:
            importlib.reload(cap)

    def test_resolves_default_when_no_env_set(self):
        import importlib
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                reloaded = importlib.reload(cap)
                self.assertEqual(reloaded.HERMES_HOME, Path("/opt/data"))
                self.assertEqual(reloaded.PROFILES_BASE, Path("/opt/data/profiles"))
                self.assertEqual(reloaded._run_env()["HERMES_HOME"], "/opt/data")
        finally:
            importlib.reload(cap)


class ListProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cap-list-test-"))
        self.patcher = mock.patch.object(cap, "PROFILES_BASE", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nonexistent_directory_returns_empty(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(cap.list_profiles(), [])

    def test_filters_reserved_and_files(self):
        (self.tmp / "default").mkdir()
        (self.tmp / "platform").mkdir()
        (self.tmp / "not-a-dir.txt").touch()
        (self.tmp / "cluster-beta").mkdir()
        (self.tmp / "cluster-alpha").mkdir()

        self.assertEqual(cap.list_profiles(), ["cluster-alpha", "cluster-beta"])

    def test_cmd_list_prints_ready_sorted_by_default(self):
        for name in ("cluster-zeta", "cluster-beta"):
            p = self.tmp / name
            p.mkdir(parents=True, exist_ok=True)
            (p / "profile.yaml").touch()
            (p / "USER.md").write_text("- project: p\n- cluster: c\n- location: l\n", encoding="utf-8")
            (p / "config.yaml").write_text(
                "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n",
                encoding="utf-8",
            )
        (self.tmp / "cluster-incomplete").mkdir()
        (self.tmp / "cluster-incomplete" / "config.yaml").write_text(
            "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n",
            encoding="utf-8",
        )

        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            cap.cmd_list(mock.MagicMock(all=False))
        self.assertEqual(out.getvalue(), "cluster-beta\ncluster-zeta\n")

        out_all = io.StringIO()
        with mock.patch("sys.stdout", out_all):
            cap.cmd_list(mock.MagicMock(all=True))
        self.assertEqual(out_all.getvalue(), "cluster-beta\ncluster-incomplete\ncluster-zeta\n")

    def test_build_parser_list_all(self):
        parser = cap.build_parser()
        args = parser.parse_args(["list"])
        self.assertFalse(args.all)
        args_all = parser.parse_args(["list", "--all"])
        self.assertTrue(args_all.all)


class ListReadyProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cap-ready-test-"))
        self.patcher = mock.patch.object(cap, "PROFILES_BASE", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scaffold(self, name: str, user_md: bool = True, identity: bool = True, scaffolded: bool = True):
        p = self.tmp / name
        p.mkdir(parents=True, exist_ok=True)
        if scaffolded:
            (p / "profile.yaml").touch()
        if user_md:
            (p / "USER.md").write_text("- project: p\n- cluster: c\n- location: l\n", encoding="utf-8")
        if identity:
            (p / "config.yaml").write_text(
                "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n",
                encoding="utf-8",
            )
        return p

    def test_ready_profiles_filters_incomplete_scaffolds(self):
        self._scaffold("cluster-ready")
        self._scaffold("cluster-no-user", user_md=False, identity=True)
        self._scaffold("cluster-unregistered", scaffolded=False)
        self._scaffold("default")
        self._scaffold("platform")

        self.assertEqual(cap.list_ready_profiles(), ["cluster-ready"])

    def test_ready_profiles_does_not_probe_sandbox_or_call_kubeconfig_landed(self):
        self._scaffold("cluster-ready")
        with mock.patch.object(cap, "kubeconfig_landed", side_effect=AssertionError("kubeconfig_landed should not be called")):
            self.assertEqual(cap.list_ready_profiles(), ["cluster-ready"])

    def test_ready_profiles_tolerates_corrupt_config_yaml(self):
        self._scaffold("cluster-ready")
        p_corrupt = self._scaffold("cluster-corrupt", user_md=True, identity=False)
        (p_corrupt / "config.yaml").write_text("invalid yaml: {{\n", encoding="utf-8")

        # Ready profiles match the dispatcher capability: scaffolded and USER.md present
        self.assertEqual(cap.list_ready_profiles(), ["cluster-corrupt", "cluster-ready"])

    def test_read_cluster_identity_robustness(self):
        # 1. Nonexistent directory
        self.assertIsNone(cap.read_cluster_identity(self.tmp / "nonexistent"))

        # 2. Scalar and list YAML raise AttributeError per docstring specification
        p1 = self.tmp / "p1"
        p1.mkdir()
        (p1 / "config.yaml").write_text("scalar_value\n", encoding="utf-8")
        with self.assertRaises(AttributeError):
            cap.read_cluster_identity(p1)

        (p1 / "config.yaml").write_text("[item1, item2]\n", encoding="utf-8")
        with self.assertRaises(AttributeError):
            cap.read_cluster_identity(p1)

        # 3. Non-dict cluster_identity
        (p1 / "config.yaml").write_text("cluster_identity: 12345\n", encoding="utf-8")
        self.assertIsNone(cap.read_cluster_identity(p1))

        # 4. Incomplete fields
        (p1 / "config.yaml").write_text("cluster_identity:\n  project: p\n", encoding="utf-8")
        self.assertIsNone(cap.read_cluster_identity(p1))

        # 5. Invalid YAML syntax
        (p1 / "config.yaml").write_text("{{invalid-yaml\n", encoding="utf-8")
        self.assertIsNone(cap.read_cluster_identity(p1))

        # 6. Valid cluster_identity
        (p1 / "config.yaml").write_text(
            "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n", encoding="utf-8"
        )
        self.assertEqual(
            cap.read_cluster_identity(p1),
            {"project": "p", "cluster": "c", "location": "l"},
        )

        # 7. Non-UTF-8 bytes raises UnicodeDecodeError
        p_bin = self.tmp / "p_bin"
        p_bin.mkdir()
        (p_bin / "config.yaml").write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            cap.read_cluster_identity(p_bin)

        # 8. Directory config.yaml raises OSError
        p_dir = self.tmp / "p_dir"
        p_dir.mkdir()
        (p_dir / "config.yaml").mkdir()
        with self.assertRaises(OSError):
            cap.read_cluster_identity(p_dir)

    def test_stamp_soul_identity_branches_and_cli_subcommand(self):
        p = self.tmp / "cluster-soul"
        p.mkdir()

        # Missing SOUL.md is a safe no-op
        cap._stamp_soul_identity(p, "proj-a", "clus-a", "us-central1")
        self.assertFalse((p / "SOUL.md").exists())

        # Branch 1: Anchor present -> inserts before SOUL_SECTION_1_ANCHOR
        (p / "SOUL.md").write_text(
            "# SOUL.md\n\nIntro paragraph.\n\n---\n\n## 1. Core Truths\n\n- Truth 1\n",
            encoding="utf-8",
        )
        cap._stamp_soul_identity(p, "proj-a", "clus-a", "us-central1")
        first = (p / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn(
            "---\n\n## Target Cluster Identity\n\n- project: proj-a\n- cluster: clus-a\n- location: us-central1\n\n---\n\n## 1. Core Truths",
            first,
        )

        # Branch 2: Existing Target Cluster Identity block -> replaces in place without duplicating
        cap._stamp_soul_identity(p, "proj-b", "clus-b", "europe-west1")
        replaced = (p / "SOUL.md").read_text(encoding="utf-8")
        self.assertEqual(replaced.count("## Target Cluster Identity"), 1)
        self.assertIn("- project: proj-b\n- cluster: clus-b\n- location: europe-west1\n", replaced)
        self.assertNotIn("proj-a", replaced)

        # Branch 3: Anchor missing and no existing block -> appends at the end
        (p / "SOUL.md").write_text("# Custom SOUL without section 1 anchor\n", encoding="utf-8")
        cap._stamp_soul_identity(p, "proj-c", "clus-c", "asia-east1")
        appended = (p / "SOUL.md").read_text(encoding="utf-8")
        self.assertTrue(
            appended.endswith(
                "\n\n---\n\n## Target Cluster Identity\n\n- project: proj-c\n- cluster: clus-c\n- location: asia-east1\n"
            )
        )

        # CLI subcommand `stamp-soul --profile-dir` reads config.yaml and stamps a fresh template SOUL.md
        (p / "SOUL.md").write_text(
            "# SOUL.md\n\n---\n\n## 1. Core Truths\n\n- Truth 1\n",
            encoding="utf-8",
        )
        (p / "config.yaml").write_text(
            "cluster_identity:\n  project: proj-cli\n  cluster: clus-cli\n  location: us-east1\n",
            encoding="utf-8",
        )
        args = cap.build_parser().parse_args(["stamp-soul", "--profile-dir", str(p)])
        cap.cmd_stamp_soul(args)
        cli_stamped = (p / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("- project: proj-cli\n- cluster: clus-cli\n- location: us-east1\n", cli_stamped)

    def test_stamp_soul_exits_non_zero_when_the_config_has_no_identity(self):
        # The entrypoint has already replaced SOUL.md with the blank template when it
        # calls this, and stamps the profile as refreshed only when every step exited
        # zero; a quiet return here would leave the blank persona stamped as current.
        p = self.tmp / "cluster-no-identity"
        p.mkdir()
        (p / "SOUL.md").write_text("# SOUL.md\n", encoding="utf-8")
        (p / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        args = cap.build_parser().parse_args(["stamp-soul", "--profile-dir", str(p)])
        with self.assertRaises(SystemExit) as raised:
            cap.cmd_stamp_soul(args)
        self.assertIn("no readable cluster_identity", str(raised.exception.code))
        self.assertEqual((p / "SOUL.md").read_text(encoding="utf-8"), "# SOUL.md\n")

    def test_list_profiles_excludes_reserved_tse_and_dot_deleted(self):
        for name in ("default", "platform", "tse", "cluster-demo", ".deleted"):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
        self.assertEqual(cap.list_profiles(), ["cluster-demo"])


if __name__ == "__main__":
    unittest.main()
