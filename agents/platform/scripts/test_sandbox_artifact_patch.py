"""Staging a sandbox-written deliverable into the gateway (#999 under #737).

The bug these cover is silent by construction. Once the shell sandbox is on,
``_deliver_kanban_artifacts`` screens every path with ``os.path.isfile`` in a
pod where the agent's files do not exist, finds nothing, and returns -- no
upload, no error, no notice. So the assertions here are mostly about what
reaches the original method, not about what it does with it.
"""

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import sandbox_artifact_patch  # noqa: E402
import sandbox_exec  # noqa: E402


class FakeTask:
    def __init__(self, result=None):
        self.result = result


def run(coro):
    return asyncio.run(coro)


# The denylist names `_screen` reads off upstream's module. Short stand-ins for
# the real tuples, which are longer and live in the base image.
FAKE_DENIED_PREFIXES = ("/etc", "/proc", "/root")
FAKE_DENIED_HOME_SUBPATHS = (".ssh", ".aws")


def install_fake_platforms_base(test, *, strict=False, upstream_denies=False):
    """Stand in for ``gateway.platforms.base``, which is in the base image.

    ``_screen`` fails closed when it cannot reach that module, so without this
    every delivery test would assert on an empty staging list for the wrong
    reason. ``upstream_denies`` drives upstream's own
    ``_path_under_denied_prefix``, which is the half resolved against the
    gateway's home; the two tuples drive the half resolved against the
    sandbox's.
    """
    base = types.ModuleType("gateway.platforms.base")
    base._MEDIA_DELIVERY_DENIED_PREFIXES = FAKE_DENIED_PREFIXES
    base._MEDIA_DELIVERY_DENIED_HOME_SUBPATHS = FAKE_DENIED_HOME_SUBPATHS
    base._media_delivery_strict_mode = lambda: strict
    base._path_under_denied_prefix = lambda resolved: upstream_denies
    platforms = types.ModuleType("gateway.platforms")
    platforms.base = base

    package = sys.modules.setdefault("gateway", types.ModuleType("gateway"))
    saved = {
        name: sys.modules.get(name)
        for name in ("gateway.platforms", "gateway.platforms.base")
    }

    def restore():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    test.addCleanup(restore)
    package.platforms = platforms
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base
    return base


def forget_sandbox_state(test):
    """Drop ``_sandbox_on``'s memo, which otherwise leaks between cases."""
    test.addCleanup(setattr, sandbox_artifact_patch, "_SANDBOX_ON", None)
    sandbox_artifact_patch._SANDBOX_ON = None


class StageTest(unittest.TestCase):
    """``_stage`` decides what crosses the boundary and what is left alone."""

    def setUp(self):
        self.reads = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._real_read_bytes = sandbox_exec.read_bytes
        self.addCleanup(setattr, sandbox_exec, "read_bytes", self._real_read_bytes)

    def fake_read(self, contents):
        """Stand in for the sandbox: a dict of path -> bytes, or ``None``."""

        def read_bytes(path, *, max_bytes, **kwargs):
            self.reads.append((path, max_bytes, kwargs))
            raw = contents.get(path)
            return None if raw is None else raw[:max_bytes]

        sandbox_exec.read_bytes = read_bytes

    def test_copies_the_bytes_and_keeps_the_basename(self):
        self.fake_read({"/opt/data/report.md": b"# findings\n"})
        staged, directory = sandbox_artifact_patch._stage(["/opt/data/report.md"])
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)

        self.assertEqual(len(staged), 1)
        self.assertEqual(os.path.basename(staged[0]), "report.md")
        with open(staged[0], "rb") as handle:
            self.assertEqual(handle.read(), b"# findings\n")

    def test_the_sandbox_copy_wins_over_a_stale_one_in_this_pod(self):
        # `sandbox_mirror` copied the agent pod's trees into the sandbox at the
        # same absolute paths and deleted nothing, so on an upgraded install
        # both pods hold the file and the one here is last month's. Skipping
        # the read because `isfile` says yes would deliver the stale copy with
        # nothing in any log to say so.
        both = os.path.join(self.tmp.name, "audit.csv")
        with open(both, "w", encoding="utf-8") as handle:
            handle.write("last month")
        self.fake_read({both: b"this month"})

        staged, directory = sandbox_artifact_patch._stage([both])
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)

        self.assertEqual(len(staged), 1)
        with open(staged[0], "rb") as handle:
            self.assertEqual(handle.read(), b"this month")

    def test_a_path_only_this_pod_has_is_left_for_the_original_to_find(self):
        local = os.path.join(self.tmp.name, "local.md")
        with open(local, "w", encoding="utf-8") as handle:
            handle.write("written here")
        self.fake_read({})

        staged, directory = sandbox_artifact_patch._stage([local])
        self.assertEqual(staged, [])
        self.assertIsNone(directory)

    def test_every_read_carries_a_timeout(self):
        # `sandbox_exec.run` waits forever when handed no timeout, and the far
        # side sources a `~/.bashrc` the model owns.
        self.fake_read({"/opt/data/report.md": b"body"})
        staged, directory = sandbox_artifact_patch._stage(["/opt/data/report.md"])
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)
        self.assertEqual(
            self.reads[0][2]["timeout"],
            sandbox_artifact_patch.STAGE_READ_TIMEOUT_SECONDS,
        )

    def test_a_path_the_subprocess_layer_rejects_costs_only_that_path(self):
        # A NUL in a model-composed path reaches `subprocess` as a ValueError,
        # not as a `False` from `isfile`. Uncaught it aborts the delivery, and
        # the notifier writes its cursor after delivery -- so the card is
        # retried on every tick, for ever.
        def read_bytes(path, *, max_bytes, **kwargs):
            if "\x00" in path:
                raise ValueError("embedded null byte")
            return b"body"

        sandbox_exec.read_bytes = read_bytes
        staged, directory = sandbox_artifact_patch._stage(
            ["/opt/data/bad\x00.md", "/opt/data/good.md"]
        )
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)
        self.assertEqual([os.path.basename(p) for p in staged], ["good.md"])

    def test_the_total_across_one_card_is_bounded(self):
        # Not `STAGE_MAX_BYTES * MAX_STAGED_ARTIFACTS`: /tmp is an emptyDir
        # whose overflow the kubelet answers with an eviction, not an ENOSPC
        # this code could catch.
        # Each one is inside the per-file cap; two of them exhaust the budget.
        chunk = min(
            sandbox_artifact_patch.STAGE_MAX_BYTES,
            sandbox_artifact_patch.STAGE_TOTAL_MAX_BYTES // 2,
        )
        self.fake_read(
            {
                "/opt/data/a.log": b"a" * chunk,
                "/opt/data/b.log": b"b" * chunk,
                "/opt/data/c.log": b"c" * chunk,
            }
        )
        staged, directory = sandbox_artifact_patch._stage(
            ["/opt/data/a.log", "/opt/data/b.log", "/opt/data/c.log"]
        )
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)

        self.assertEqual([os.path.basename(p) for p in staged], ["a.log", "b.log"])
        total = sum(os.path.getsize(path) for path in staged)
        self.assertLessEqual(total, sandbox_artifact_patch.STAGE_TOTAL_MAX_BYTES)

    def test_a_leaked_directory_older_than_the_sweep_age_is_removed(self):
        stale = tempfile.mkdtemp(prefix=sandbox_artifact_patch.STAGED_DIR_PREFIX)
        self.addCleanup(shutil.rmtree, stale, True)
        fresh = tempfile.mkdtemp(prefix=sandbox_artifact_patch.STAGED_DIR_PREFIX)
        self.addCleanup(shutil.rmtree, fresh, True)
        old = time.time() - sandbox_artifact_patch.STALE_SWEEP_AGE_SECONDS - 1
        os.utime(stale, (old, old))

        sandbox_artifact_patch._sweep_stale()

        # A SIGKILL does not run the `finally` that would have cleaned this up,
        # and the pod is killed on every rollout.
        self.assertFalse(os.path.exists(stale))
        # A delivery in flight in another process must survive the sweep.
        self.assertTrue(os.path.exists(fresh))

    def test_a_path_that_is_no_file_over_there_either_is_dropped(self):
        # The original method's "mentioned for reference only" case, which is
        # a legitimate outcome rather than a failure to report.
        self.fake_read({})
        staged, directory = sandbox_artifact_patch._stage(["/opt/data/ghost.md"])
        self.assertEqual(staged, [])
        self.assertIsNone(directory)

    def test_reads_one_byte_past_the_cap_and_refuses_what_exceeds_it(self):
        oversized = b"x" * (sandbox_artifact_patch.STAGE_MAX_BYTES + 1)
        self.fake_read({"/opt/data/huge.log": oversized})

        staged, directory = sandbox_artifact_patch._stage(["/opt/data/huge.log"])
        self.assertEqual(staged, [])
        self.assertIsNone(directory)
        # The extra byte is what makes "at the cap" distinguishable from
        # "truncated to the cap"; without it a file exactly one byte too long
        # would be pasted as if it were complete.
        self.assertEqual(
            [(path, cap) for path, cap, _ in self.reads],
            [("/opt/data/huge.log", sandbox_artifact_patch.STAGE_MAX_BYTES + 1)],
        )

    def test_a_file_exactly_at_the_cap_is_staged(self):
        exact = b"x" * sandbox_artifact_patch.STAGE_MAX_BYTES
        self.fake_read({"/opt/data/big.log": exact})
        staged, directory = sandbox_artifact_patch._stage(["/opt/data/big.log"])
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)
        self.assertEqual(len(staged), 1)

    def test_same_basename_from_two_directories_stays_two_files(self):
        self.fake_read(
            {
                "/opt/data/a/report.md": b"cluster a",
                "/opt/data/b/report.md": b"cluster b",
            }
        )
        staged, directory = sandbox_artifact_patch._stage(
            ["/opt/data/a/report.md", "/opt/data/b/report.md"]
        )
        self.addCleanup(sandbox_artifact_patch._cleanup, staged, directory)

        self.assertEqual(len(set(staged)), 2)
        self.assertEqual(
            [os.path.basename(path) for path in staged], ["report.md", "report.md"]
        )
        bodies = []
        for path in staged:
            with open(path, "rb") as handle:
                bodies.append(handle.read())
        self.assertEqual(sorted(bodies), [b"cluster a", b"cluster b"])

    def test_an_unreachable_sandbox_drops_the_artifact_rather_than_raising(self):
        def explode(path, *, max_bytes, **kwargs):
            raise sandbox_exec.SandboxUnavailable("ssh: connect: no route to host")

        sandbox_exec.read_bytes = explode
        staged, directory = sandbox_artifact_patch._stage(["/opt/data/report.md"])
        self.assertEqual(staged, [])
        self.assertIsNone(directory)

    def test_cleanup_removes_the_files_and_the_translations(self):
        self.fake_read({"/opt/data/report.md": b"body"})
        staged, directory = sandbox_artifact_patch._stage(["/opt/data/report.md"])
        target = staged[0]
        self.assertEqual(
            sandbox_artifact_patch.original_path(target), "/opt/data/report.md"
        )

        sandbox_artifact_patch._cleanup(staged, directory)
        self.assertFalse(os.path.exists(directory))
        # Unknown again, so a later notice names the path it was handed rather
        # than a stale translation for a temp directory that has been reused.
        self.assertEqual(sandbox_artifact_patch.original_path(target), target)


class CandidatesTest(unittest.TestCase):
    """Only the declared artifact list; see the module docstring for why."""

    def test_reads_the_artifacts_list(self):
        self.assertEqual(
            sandbox_artifact_patch._candidates({"artifacts": ["/opt/data/r.md"]}),
            ["/opt/data/r.md"],
        )

    def test_tilde_means_the_sandbox_home_not_this_pods(self):
        # The path names a file in the other pod. `os.path.expanduser` would
        # resolve it against the gateway's $HOME and read a different file --
        # or, more often, miss and drop the deliverable.
        expanded = os.path.join(sandbox_artifact_patch.SANDBOX_HOME, "r.md")
        self.assertEqual(
            sandbox_artifact_patch._candidates({"artifacts": ["~/r.md"]}),
            [expanded],
        )
        self.assertEqual(
            sandbox_artifact_patch._candidates({"artifacts": ["~"]}),
            [sandbox_artifact_patch.SANDBOX_HOME],
        )
        self.assertNotEqual(expanded, os.path.expanduser("~/r.md"))

    def test_deduplicates_after_expansion(self):
        expanded = os.path.join(sandbox_artifact_patch.SANDBOX_HOME, "r.md")
        self.assertEqual(
            sandbox_artifact_patch._candidates(
                {"artifacts": ["~/r.md", expanded, "/opt/data/r.md"]}
            ),
            [expanded, "/opt/data/r.md"],
        )


class ScreenTest(unittest.TestCase):
    """The delivery policy the staged path would otherwise slip past.

    Rewriting every path to one under the system temp directory means
    ``filter_local_delivery_paths`` is asked about a path this module chose
    rather than the one the model declared, so the screen has to happen here.
    """

    def test_a_path_under_a_denied_system_prefix_is_not_staged(self):
        install_fake_platforms_base(self)
        self.assertEqual(sandbox_artifact_patch._screen(["/etc/shadow"]), [])

    def test_a_credential_directory_under_the_sandbox_home_is_not_staged(self):
        # Upstream resolves this half of its denylist against the running
        # process's $HOME, which in the gateway is the wrong pod's.
        install_fake_platforms_base(self)
        key = os.path.join(sandbox_artifact_patch.SANDBOX_HOME, ".ssh", "id_ed25519")
        self.assertEqual(sandbox_artifact_patch._screen([key]), [])

    def test_upstreams_own_verdict_is_honoured(self):
        install_fake_platforms_base(self, upstream_denies=True)
        self.assertEqual(sandbox_artifact_patch._screen(["/opt/data/report.md"]), [])

    def test_an_ordinary_deliverable_survives(self):
        install_fake_platforms_base(self)
        self.assertEqual(
            sandbox_artifact_patch._screen(["/opt/data/report.md"]),
            ["/opt/data/report.md"],
        )

    def test_strict_media_delivery_stages_nothing(self):
        # Its allowlist and recency tests are questions about this pod's disk
        # and cannot be answered for a file in the sandbox. Staging would
        # launder the path past a check the operator turned on deliberately.
        install_fake_platforms_base(self, strict=True)
        self.assertEqual(sandbox_artifact_patch._screen(["/opt/data/report.md"]), [])

    def test_an_unreachable_upstream_module_fails_closed(self):
        for name in ("gateway.platforms.base", "gateway.platforms"):
            saved = sys.modules.pop(name, None)
            if saved is not None:
                self.addCleanup(sys.modules.__setitem__, name, saved)
        self.assertEqual(sandbox_artifact_patch._screen(["/opt/data/report.md"]), [])

    def test_ignores_non_strings_and_a_non_list(self):
        self.assertEqual(
            sandbox_artifact_patch._candidates({"artifacts": [None, 3, "", "/a.md"]}),
            ["/a.md"],
        )
        self.assertEqual(sandbox_artifact_patch._candidates({"artifacts": "/a.md"}), [])
        self.assertEqual(sandbox_artifact_patch._candidates(None), [])
        self.assertEqual(sandbox_artifact_patch._candidates({}), [])

    def test_caps_the_list(self):
        many = [f"/opt/data/{i}.md" for i in range(50)]
        self.assertEqual(
            len(sandbox_artifact_patch._candidates({"artifacts": many})),
            sandbox_artifact_patch.MAX_STAGED_ARTIFACTS,
        )


class InstallTest(unittest.TestCase):
    """What the wrapper hands the original method, and when it wraps at all."""

    def setUp(self):
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        calls = self.calls

        class GatewayKanbanWatchersMixin:
            async def _deliver_kanban_artifacts(
                self, *, adapter, chat_id, metadata, event_payload, task
            ):
                # Record what the artifacts looked like *and* whether they were
                # readable at the moment of the call: the wrapper deletes them
                # in a `finally`, so a later assertion would see nothing.
                paths = list((event_payload or {}).get("artifacts") or [])
                bodies = []
                for path in paths:
                    if not os.path.isfile(path):
                        bodies.append(None)
                        continue
                    with open(path, "rb") as handle:
                        bodies.append(handle.read())
                calls.append({"paths": paths, "bodies": bodies})

        self.mixin = GatewayKanbanWatchersMixin
        watchers = types.ModuleType("gateway.kanban_watchers")
        watchers.GatewayKanbanWatchersMixin = GatewayKanbanWatchersMixin
        package = types.ModuleType("gateway")
        package.kanban_watchers = watchers
        self.saved = {
            name: sys.modules.get(name)
            for name in ("gateway", "gateway.kanban_watchers")
        }
        sys.modules["gateway"] = package
        sys.modules["gateway.kanban_watchers"] = watchers
        self.addCleanup(self._restore_modules)
        install_fake_platforms_base(self)
        forget_sandbox_state(self)

        self._real_read_bytes = sandbox_exec.read_bytes
        self._real_enabled = sandbox_exec.sandbox_enabled
        self.addCleanup(setattr, sandbox_exec, "read_bytes", self._real_read_bytes)
        self.addCleanup(setattr, sandbox_exec, "sandbox_enabled", self._real_enabled)

    def _restore_modules(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def enable_sandbox(self, contents, enabled=True):
        sandbox_exec.sandbox_enabled = lambda path=None: enabled
        sandbox_exec.read_bytes = lambda path, *, max_bytes, **kw: contents.get(path)

    def deliver(self, event_payload, task=None):
        instance = self.mixin()
        run(
            instance._deliver_kanban_artifacts(
                adapter=object(),
                chat_id="spaces/AAA",
                metadata={},
                event_payload=event_payload,
                task=task or FakeTask(),
            )
        )

    def test_the_original_receives_a_readable_local_copy(self):
        self.enable_sandbox({"/opt/data/report.md": b"# findings"})
        sandbox_artifact_patch.install()
        self.deliver({"artifacts": ["/opt/data/report.md"]})

        self.assertEqual(len(self.calls), 1)
        paths = self.calls[0]["paths"]
        # The agent's own path rides along behind the staged one; the original
        # method's `isfile` screen is what drops it.
        self.assertIn("/opt/data/report.md", paths)
        self.assertEqual(self.calls[0]["bodies"][0], b"# findings")
        self.assertNotEqual(paths[0], "/opt/data/report.md")

    def test_the_staged_copy_is_gone_once_delivery_returns(self):
        self.enable_sandbox({"/opt/data/report.md": b"# findings"})
        sandbox_artifact_patch.install()
        self.deliver({"artifacts": ["/opt/data/report.md"]})
        self.assertFalse(os.path.exists(self.calls[0]["paths"][0]))

    def test_the_payload_the_caller_owns_is_not_mutated(self):
        self.enable_sandbox({"/opt/data/report.md": b"# findings"})
        sandbox_artifact_patch.install()
        payload = {"artifacts": ["/opt/data/report.md"], "summary": "done"}
        self.deliver(payload)
        self.assertEqual(payload, {"artifacts": ["/opt/data/report.md"], "summary": "done"})

    def test_sandbox_off_is_a_straight_passthrough(self):
        self.enable_sandbox({}, enabled=False)
        sandbox_artifact_patch.install()
        self.deliver({"artifacts": ["/opt/data/report.md"]})
        self.assertEqual(self.calls[0]["paths"], ["/opt/data/report.md"])

    def test_nothing_to_stage_is_a_straight_passthrough(self):
        self.enable_sandbox({})
        sandbox_artifact_patch.install()
        self.deliver({"artifacts": ["/opt/data/ghost.md"]})
        self.assertEqual(self.calls[0]["paths"], ["/opt/data/ghost.md"])

    def test_a_broken_managed_config_does_not_break_the_delivery(self):
        def explode(path=None):
            raise sandbox_exec.SandboxMisconfigured("config.yaml could not be read")

        sandbox_exec.sandbox_enabled = explode
        sandbox_artifact_patch.install()
        self.deliver({"artifacts": ["/opt/data/report.md"]})
        self.assertEqual(self.calls[0]["paths"], ["/opt/data/report.md"])

    def test_install_is_idempotent(self):
        self.enable_sandbox({"/opt/data/report.md": b"# findings"})
        sandbox_artifact_patch.install()
        first = self.mixin._deliver_kanban_artifacts
        sandbox_artifact_patch.install()
        self.assertIs(self.mixin._deliver_kanban_artifacts, first)

    def test_the_wrapper_leads_back_to_the_method_it_replaced(self):
        # deploy/docker/patches/verify_slack_relay_registry_contract.py pins
        # this method's upstream signature, and the only interpreter it can read
        # it in is one where sitecustomize has already installed this patch. It
        # gets there with `inspect.unwrap`, so dropping `functools.wraps` turns
        # that gate into a comparison of the shim against itself -- which passes
        # whatever upstream does.
        original = self.mixin._deliver_kanban_artifacts
        sandbox_artifact_patch.install()
        shim = self.mixin._deliver_kanban_artifacts
        self.assertIsNot(shim, original)
        self.assertIs(inspect.unwrap(shim), original)
        self.assertEqual(inspect.signature(shim), inspect.signature(original))

    def test_a_gateway_without_the_mixin_is_tolerated(self):
        # Raising here would abort `gateway.platform_registry`'s import and
        # take the chat connection down with it.
        sys.modules.pop("gateway.kanban_watchers")
        sys.modules["gateway"].kanban_watchers = None
        sandbox_artifact_patch.install()  # must not raise


if __name__ == "__main__":
    unittest.main()
