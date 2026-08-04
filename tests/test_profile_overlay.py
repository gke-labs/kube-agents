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
        # profiles/.. resolves to the agent home, whose config.yaml is the operator's
        # authoritative default-profile render — mounted read-only, but it must never
        # be a target regardless.
        err = io.StringIO()
        with redirect_stderr(err):
            rc = po.main(["--profile-dir", str(self.tmp / "profiles" / "..")])
        self.assertEqual(rc, 2)
        self.assertIn("not a valid profile name", err.getvalue())


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
