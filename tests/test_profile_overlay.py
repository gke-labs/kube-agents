"""Tests for deploy/shared/profile_overlay.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest: PyYAML is the only import beyond the standard library, and
it already ships in every environment that runs the agent.

The merge runs at pod startup against every targeted Hermes profile, and its failure
mode is silent — a profile keeps stale limits, or loses ones it should have, and the
symptom surfaces much later as a worker that stops mid-task. These cover the parts that
are easy to get wrong: removal, idempotency, and not clobbering later edits.
"""

import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

import yaml

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared" / "profile_overlay.py"
)
_spec = importlib.util.spec_from_file_location("profile_overlay", _MODULE_PATH)
po = importlib.util.module_from_spec(_spec)
sys.modules["profile_overlay"] = po
_spec.loader.exec_module(po)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True))


def read(path):
    return yaml.safe_load(path.read_text()) or {}


class OverlayMergeTest(unittest.TestCase):
    """Each test gets a throwaway profile directory seeded like an image-built one."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profile = self.tmp / "profiles" / "platform"
        write(
            self.profile / "config.yaml",
            {
                "plugins": {"enabled": ["hermes_otel", "tool_call_audit"]},
                "platform_toolsets": {"cli": ["hermes-cli", "gke"]},
                "memory": {"memory_enabled": False},
            },
        )

    def overlay(self, data, name="o.yaml"):
        p = self.tmp / name
        write(p, data)
        return p

    def config(self):
        return read(self.profile / "config.yaml")

    def test_merge_unions_lists_and_preserves_image_keys(self):
        po.apply_overlay(
            self.profile,
            self.overlay({"plugins": {"enabled": ["stockout"]}, "agent": {"max_turns": 200}}),
        )
        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "tool_call_audit", "stockout"])
        self.assertEqual(cfg["agent"], {"max_turns": 200})
        # Image-owned keys the overlay never mentioned must survive untouched.
        self.assertEqual(cfg["memory"], {"memory_enabled": False})
        self.assertEqual(cfg["platform_toolsets"]["cli"], ["hermes-cli", "gke"])

    def test_merge_is_idempotent(self):
        ov = self.overlay({"plugins": {"enabled": ["stockout"]}, "agent": {"max_turns": 200}})
        po.apply_overlay(self.profile, ov)
        first = self.config()
        po.apply_overlay(self.profile, ov)
        self.assertEqual(self.config(), first)

    def test_removing_the_overlay_reverts_the_profile(self):
        """The regression this file exists for.

        Cluster profiles are not force-synced from the image, so without an unapply
        step a merged value persists forever after the operator stops emitting it —
        the CR would say "no tuning" while every cluster agent still ran the old
        limits.
        """
        po.apply_overlay(
            self.profile,
            self.overlay({"plugins": {"enabled": ["stockout"]}, "agent": {"max_turns": 200}}),
        )
        self.assertIn("agent", self.config())

        po.apply_overlay(self.profile, None)  # operator stopped emitting a key
        cfg = self.config()

        self.assertNotIn("agent", cfg, "operator-applied agent block must be removed")
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "tool_call_audit"])
        self.assertEqual(cfg["memory"], {"memory_enabled": False})
        self.assertFalse((self.profile / po.STATE_FILENAME).exists())

    def test_shrinking_an_overlay_removes_only_the_dropped_keys(self):
        po.apply_overlay(self.profile, self.overlay({"agent": {"max_turns": 200, "api_max_retries": 8}}, "big.yaml"))
        po.apply_overlay(self.profile, self.overlay({"agent": {"max_turns": 200}}, "small.yaml"))
        self.assertEqual(self.config()["agent"], {"max_turns": 200})

    def test_unapply_does_not_clobber_a_later_edit(self):
        po.apply_overlay(self.profile, self.overlay({"agent": {"max_turns": 200}}))
        cfg = self.config()
        cfg["agent"]["max_turns"] = 500  # somebody else changed it
        write(self.profile / "config.yaml", cfg)

        po.apply_overlay(self.profile, None)
        self.assertEqual(self.config()["agent"]["max_turns"], 500)

    def test_missing_config_is_reported_not_crashed(self):
        ghost = self.tmp / "profiles" / "ghost"
        ghost.mkdir(parents=True)
        self.assertIn("skipped", po.apply_overlay(ghost, None))

    def test_corrupt_state_file_is_tolerated(self):
        (self.profile / po.STATE_FILENAME).write_text("{not json")
        po.apply_overlay(self.profile, self.overlay({"agent": {"max_turns": 200}}))
        self.assertEqual(self.config()["agent"], {"max_turns": 200})

    def test_unapply_keeps_a_list_entry_the_image_already_had(self):
        """An overlay naming something the image already ships must not delete it.

        A plugin declaring a toolset the profile already lists would otherwise take the
        image's own entry with it on removal — permanently, for cluster profiles, whose
        config.yaml is never force-synced back.
        """
        po.apply_overlay(self.profile, self.overlay({"platform_toolsets": {"cli": ["gke"]}}))
        self.assertEqual(self.config()["platform_toolsets"]["cli"], ["hermes-cli", "gke"])

        po.apply_overlay(self.profile, None)
        self.assertEqual(
            self.config()["platform_toolsets"]["cli"],
            ["hermes-cli", "gke"],
            "image-owned 'gke' must survive removal of an overlay that also named it",
        )

    def test_unapply_restores_a_scalar_the_overlay_overrode(self):
        """Removal restores the image's value rather than deleting the key."""
        write(
            self.profile / "config.yaml",
            {"agent": {"max_turns": 90}, "memory": {"memory_enabled": False}},
        )
        po.apply_overlay(self.profile, self.overlay({"agent": {"max_turns": 200}}))
        self.assertEqual(self.config()["agent"]["max_turns"], 200)

        po.apply_overlay(self.profile, None)
        self.assertEqual(
            self.config()["agent"]["max_turns"], 90, "the image's own value must come back"
        )

    def test_unapply_removes_only_the_entries_it_added(self):
        po.apply_overlay(
            self.profile, self.overlay({"plugins": {"enabled": ["gke", "stockout"]}})
        )
        po.apply_overlay(self.profile, None)
        # 'stockout' was ours; the two image plugins were not.
        self.assertEqual(self.config()["plugins"]["enabled"], ["hermes_otel", "tool_call_audit"])

    def test_state_records_overlay_and_prior_values(self):
        po.apply_overlay(self.profile, self.overlay({"platform_toolsets": {"cli": ["gke"]}}))
        state = json.loads((self.profile / po.STATE_FILENAME).read_text())
        self.assertEqual(state["overlay"], {"platform_toolsets": {"cli": ["gke"]}})
        self.assertEqual(state["before"], {"platform_toolsets": {"cli": ["hermes-cli", "gke"]}})

    def test_legacy_state_format_is_still_understood(self):
        """Records written before `before` existed must not wedge the merge."""
        (self.profile / po.STATE_FILENAME).write_text(json.dumps({"agent": {"max_turns": 200}}))
        po.apply_overlay(self.profile, self.overlay({"agent": {"max_turns": 300}}))
        self.assertEqual(self.config()["agent"]["max_turns"], 300)

    def test_main_refuses_a_traversing_profile_dir(self):
        # profiles/.. resolves to the agent home, whose config.yaml is the default
        # profile's. That file does take an overlay, but only when the caller says so
        # with --profile-name default; arriving there by a ConfigMap key that happened
        # to resolve to ".." would apply the wrong profile's overlay to the running
        # agent's own config.
        err = io.StringIO()
        with redirect_stderr(err):
            rc = po.main(["--profile-dir", str(self.tmp / "profiles" / "..")])
        self.assertEqual(rc, 2)
        self.assertIn("not a valid profile name", err.getvalue())


class OverlayResolutionTest(unittest.TestCase):
    """Which overlay files apply to a profile, and what happens when both do.

    A cluster profile is the only one with two: the class overlay carrying
    `tuning.cluster`, and its own if a plugin targets that specific cluster. Resolving
    only the class overlay is what left such a plugin mounted but never enabled.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.overlay_dir = self.tmp / "agent-config"
        self.overlay_dir.mkdir()
        self.profile = self.tmp / "profiles" / "cluster-proj-prod-us-east1"
        write(self.profile / "config.yaml", {"plugins": {"enabled": ["hermes_otel"]}})

    def write_class(self, data):
        write(self.overlay_dir / po.CLUSTER_CLASS_OVERLAY, data)

    def write_own(self, data, name=None):
        name = name or self.profile.name
        write(self.overlay_dir / f"profile-{name}.overlay.yaml", data)

    def config(self):
        return read(self.profile / "config.yaml")

    def test_platform_profile_takes_only_its_own(self):
        self.write_class({"agent": {"max_turns": 150}})
        self.write_own({"agent": {"max_turns": 200}}, name="platform")
        got = [p.name for p in po.overlays_for("platform", self.overlay_dir)]
        self.assertEqual(got, ["profile-platform.overlay.yaml"])

    def test_cluster_profile_takes_the_class_overlay_then_its_own(self):
        self.write_class({"agent": {"max_turns": 150}})
        self.write_own({"plugins": {"enabled": ["clusterone"]}})
        got = [p.name for p in po.overlays_for(self.profile.name, self.overlay_dir)]
        self.assertEqual(
            got,
            [po.CLUSTER_CLASS_OVERLAY, f"profile-{self.profile.name}.overlay.yaml"],
            "the class overlay must merge first so the per-profile one wins a conflict",
        )

    def test_a_cluster_targeted_plugin_is_enabled_alongside_the_class_tuning(self):
        """The regression: the plugin was mounted for this profile but never enabled."""
        self.write_class({"agent": {"api_max_retries": 8, "max_turns": 150}})
        self.write_own({"plugins": {"enabled": ["clusterone"]}})

        po.sync_profile(self.profile, self.overlay_dir)
        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "clusterone"])
        self.assertEqual(cfg["agent"], {"api_max_retries": 8, "max_turns": 150})

    def test_the_more_specific_overlay_wins_a_conflict(self):
        self.write_class({"agent": {"max_turns": 150}})
        self.write_own({"agent": {"max_turns": 400}})
        po.sync_profile(self.profile, self.overlay_dir)
        self.assertEqual(self.config()["agent"]["max_turns"], 400)

    def test_withdrawing_one_of_two_overlays_removes_only_its_keys(self):
        self.write_class({"agent": {"max_turns": 150}})
        self.write_own({"plugins": {"enabled": ["clusterone"]}})
        po.sync_profile(self.profile, self.overlay_dir)

        (self.overlay_dir / f"profile-{self.profile.name}.overlay.yaml").unlink()
        po.sync_profile(self.profile, self.overlay_dir)

        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel"], "the plugin must be disabled again")
        self.assertEqual(cfg["agent"]["max_turns"], 150, "class-wide tuning must survive")

    def test_withdrawing_both_reverts_the_profile(self):
        self.write_class({"agent": {"max_turns": 150}})
        self.write_own({"plugins": {"enabled": ["clusterone"]}})
        po.sync_profile(self.profile, self.overlay_dir)

        for f in self.overlay_dir.iterdir():
            f.unlink()
        po.sync_profile(self.profile, self.overlay_dir)

        self.assertEqual(self.config(), {"plugins": {"enabled": ["hermes_otel"]}})
        self.assertFalse((self.profile / po.STATE_FILENAME).exists())

    def test_sync_refuses_a_traversing_profile_dir(self):
        with self.assertRaises(ValueError):
            po.sync_profile(self.tmp / "profiles" / "..", self.overlay_dir)

    def test_cli_resolves_overlays_from_the_directory(self):
        self.write_class({"agent": {"max_turns": 150}})
        self.write_own({"plugins": {"enabled": ["clusterone"]}})
        rc = po.main(["--profile-dir", str(self.profile), "--overlay-dir", str(self.overlay_dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.config()["plugins"]["enabled"], ["hermes_otel", "clusterone"])
        self.assertEqual(self.config()["agent"]["max_turns"], 150)


class DefaultProfileTest(unittest.TestCase):
    """The front door, reached by --profile-name rather than by directory name.

    Its home IS $HERMES_HOME, so the directory is called "data" and the name cannot be
    read off it. It is also the one profile whose config.yaml the running agent writes
    to — /sethome, the install id, saved preferences — which is why the operator sends
    it only the narrow set it owns and the merge has to leave everything else alone.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.overlay_dir = self.tmp / "agent-config"
        self.overlay_dir.mkdir()
        # $HERMES_HOME itself, not a directory under profiles/.
        self.home = self.tmp / "data"
        write(
            self.home / "config.yaml",
            {
                "plugins": {"enabled": ["hermes_otel"]},
                "kanban": {"max_in_progress": 2},
                "platforms": {"google_chat": {"home_channel": "spaces/AAA"}},
            },
        )

    def write_default(self, data):
        write(self.overlay_dir / "profile-default.overlay.yaml", data)

    def config(self):
        return read(self.home / "config.yaml")

    def run_main(self):
        return po.main(
            [
                "--profile-dir", str(self.home),
                "--profile-name", "default",
                "--overlay-dir", str(self.overlay_dir),
            ]
        )

    def test_default_overlay_merges_into_the_agent_home(self):
        self.write_default(
            {
                "plugins": {"enabled": ["adapter"]},
                "approvals": {"e2e_test_setting": {"enabled": True}},
                "kanban": {"max_in_progress": 8},
            }
        )
        self.assertEqual(self.run_main(), 0)
        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "adapter"])
        self.assertEqual(cfg["approvals"], {"e2e_test_setting": {"enabled": True}})
        self.assertEqual(cfg["kanban"]["max_in_progress"], 8)

    def test_the_agents_own_keys_survive(self):
        """/sethome's home_channel is the whole reason this file stays writable."""
        self.write_default({"plugins": {"enabled": ["adapter"]}})
        self.run_main()
        self.assertEqual(
            self.config()["platforms"]["google_chat"]["home_channel"], "spaces/AAA"
        )

    def test_withdrawing_the_overlay_reverts_the_front_door(self):
        self.write_default({"plugins": {"enabled": ["adapter"]}, "kanban": {"max_in_progress": 8}})
        self.run_main()

        (self.overlay_dir / "profile-default.overlay.yaml").unlink()
        self.assertEqual(self.run_main(), 0)

        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel"], "plugin must be disabled again")
        self.assertEqual(cfg["kanban"]["max_in_progress"], 2, "the image's cap must come back")
        self.assertFalse((self.home / po.STATE_FILENAME).exists())

    def test_no_cluster_class_overlay_reaches_the_front_door(self):
        """`tuning.cluster` is for cluster profiles; the name must not match it."""
        write(self.overlay_dir / po.CLUSTER_CLASS_OVERLAY, {"agent": {"max_turns": 150}})
        self.run_main()
        self.assertNotIn("agent", self.config())

    def test_a_named_profiles_overlay_is_not_applied_here(self):
        write(self.overlay_dir / "profile-platform.overlay.yaml", {"agent": {"max_turns": 150}})
        self.run_main()
        self.assertNotIn("agent", self.config())

    def test_the_name_is_still_required_to_be_default(self):
        """--profile-name is a narrow escape hatch, not a way past validation."""
        err = io.StringIO()
        with redirect_stderr(err):
            rc = po.main(["--profile-dir", str(self.home), "--profile-name", "../evil"])
        self.assertEqual(rc, 2)
        self.assertIn("not a valid profile name", err.getvalue())

    def test_sync_profile_still_refuses_the_default_profile(self):
        """The sweep over profiles/ must never pick the front door up by accident."""
        with self.assertRaises(ValueError):
            po.sync_profile(self.tmp / "profiles" / "default", self.overlay_dir)


class ProfileNameValidationTest(unittest.TestCase):
    def test_names(self):
        cases = [
            ("platform", True),
            ("cluster-proj-name-region", True),
            # The default profile lives at the home root, not under profiles/.
            ("default", False),
            # ConfigMap keys may contain dots, so the derived name must not escape.
            ("..", False),
            (".", False),
            ("Platform", False),
            ("bad/name", False),
            ("", False),
        ]
        for name, ok in cases:
            with self.subTest(name=name):
                self.assertIs(po.valid_profile_name(name), ok)


if __name__ == "__main__":
    unittest.main()
