"""Tests for deploy/shared/default_profile_config.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching tests/test_profile_overlay.py: PyYAML is the only
import beyond the standard library, and it already ships in every environment that runs
the agent.

This runs once per pod start against the front door's own config.yaml, and both of its
failure modes are silent. Lose a runtime key and `/sethome` forgets the home channel on
the next restart with no log line; lose an operator key and the agent quietly talks to
the wrong model endpoint. The cases below are the ones the three-way merge exists for.
"""

import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

_SHARED = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared"

# default_profile_config imports profile_overlay by name, so load that first under the
# name it expects. Same trick the module itself uses in the image, where both files sit
# in /opt/defaults/scripts.
for _name in ("profile_overlay", "default_profile_config"):
    _spec = importlib.util.spec_from_file_location(_name, _SHARED / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)

dpc = sys.modules["default_profile_config"]


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True))


def read(path):
    return yaml.safe_load(path.read_text()) or {}


class ReconcileTest(unittest.TestCase):
    """The per-key rules, exercised directly."""

    def test_untouched_key_takes_the_new_baseline(self):
        self.assertEqual({"a": 9}, dpc.reconcile({"a": 1}, {"a": 1}, {"a": 9}))

    def test_runtime_edit_wins_when_the_baseline_held_still(self):
        self.assertEqual({"a": 5}, dpc.reconcile({"a": 1}, {"a": 5}, {"a": 1}))

    def test_operator_wins_when_both_changed(self):
        self.assertEqual({"a": 7}, dpc.reconcile({"a": 1}, {"a": 5}, {"a": 7}))

    def test_runtime_only_key_survives(self):
        # Nothing else can restore it: neither baseline has ever mentioned it.
        self.assertEqual({"a": 9, "b": 2}, dpc.reconcile({"a": 1}, {"a": 1, "b": 2}, {"a": 9}))

    def test_absence_is_not_a_deletion(self):
        # save_config drops keys whose value equals a Hermes default, so a missing key
        # carries no information and must not remove anything.
        self.assertEqual({"a": 7}, dpc.reconcile({"a": 1}, {}, {"a": 7}))

    def test_baseline_dropping_a_key_removes_it(self):
        self.assertEqual({}, dpc.reconcile({"a": 1}, {"a": 1}, {}))

    def test_nested_dicts_merge_key_by_key(self):
        base = {"platforms": {"google_chat": {"typing_status_text": "x"}}}
        live = {
            "platforms": {
                "google_chat": {"typing_status_text": "x"},
                "slack": {"enabled": True, "home_channel": {"id": "C1"}},
            }
        }
        baseline = {
            "platforms": {
                "google_chat": {"typing_status_text": "x", "enabled": False},
                "slack": {"enabled": True},
            }
        }
        self.assertEqual(
            {
                "platforms": {
                    "google_chat": {"typing_status_text": "x", "enabled": False},
                    "slack": {"enabled": True, "home_channel": {"id": "C1"}},
                }
            },
            dpc.reconcile(base, live, baseline),
        )

    def test_runtime_list_replaces_when_the_baseline_held_still(self):
        # A toolset switched off in the dashboard writes a SHORTER list. Unioning would
        # put the entry back on every restart.
        self.assertEqual(
            {"t": ["a", "b"]},
            dpc.reconcile({"t": ["a", "b", "c"]}, {"t": ["a", "b"]}, {"t": ["a", "b", "c"]}),
        )

    def test_moved_baseline_list_keeps_runtime_additions(self):
        self.assertEqual(
            {"t": ["a", "b", "c", "d", "x"]},
            dpc.reconcile(
                {"t": ["a", "b", "c"]}, {"t": ["a", "b", "x"]}, {"t": ["a", "b", "c", "d"]}
            ),
        )

    def test_inputs_are_not_mutated(self):
        base, live, baseline = {"a": 1}, {"a": 1, "b": {"c": 2}}, {"a": 9}
        dpc.reconcile(base, live, baseline)
        self.assertEqual({"a": 1}, base)
        self.assertEqual({"a": 1, "b": {"c": 2}}, live)
        self.assertEqual({"a": 9}, baseline)


class AdoptTest(unittest.TestCase):
    """The first start on a volume, where there is no third input to merge against."""

    def test_a_declared_key_comes_from_the_new_baseline(self):
        self.assertEqual({"a": 9}, dpc.adopt({"a": 1}, {"a": 9}))

    def test_an_undeclared_key_is_runtime_state_and_survives(self):
        self.assertEqual({"a": 9, "b": 2}, dpc.adopt({"a": 1, "b": 2}, {"a": 9}))

    def test_undeclared_keys_survive_inside_a_declared_subtree(self):
        # Where `/sethome` writes: `platforms` and `platforms.slack` are both declared,
        # `home_channel` is not.
        self.assertEqual(
            {"platforms": {"slack": {"enabled": True, "home_channel": {"id": "C1"}}}},
            dpc.adopt(
                {"platforms": {"slack": {"enabled": False, "home_channel": {"id": "C1"}}}},
                {"platforms": {"slack": {"enabled": True}}},
            ),
        )

    def test_a_declared_list_is_not_negotiated(self):
        # The whole point: no `base` means no way to tell a runtime removal from an
        # older image's shorter list, and only one of those two mistakes is silent and
        # permanent. Take the declaration.
        self.assertEqual({"t": ["a", "b", "c"]}, dpc.adopt({"t": ["a", "b"]}, {"t": ["a", "b", "c"]}))

    def test_inputs_are_not_mutated(self):
        ours, theirs = {"a": 1, "b": {"c": 2}}, {"a": 9}
        dpc.adopt(ours, theirs)
        self.assertEqual({"a": 1, "b": {"c": 2}}, ours)
        self.assertEqual({"a": 9}, theirs)


class RebuildTest(unittest.TestCase):
    """End-to-end against files, the way the entrypoint calls it."""

    IMAGE = {
        "model": {"provider": "custom", "base_url": "http://litellm/v1"},
        "platforms": {"google_chat": {"typing_status_text": "Kage is thinking…"}},
        "plugins": {"enabled": ["hermes_otel", "session_store"]},
    }
    OVERLAY = {
        "model": {"provider": "custom", "base_url": "http://litellm/v1"},
        "platforms": {"google_chat": {"enabled": False}, "slack": {"enabled": True}},
        "plugins": {"enabled": ["hermes_otel", "session_store", "legacy_slash_commands"]},
        "approvals": {"cron_mode": "approve"},
    }

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.image = self.tmp / "chat-template" / "config.yaml"
        self.overlay = self.tmp / "agent-config" / "profile-default.overlay.yaml"
        self.config = self.tmp / "data" / "config.yaml"
        self.baseline = self.config.with_name(dpc.BASELINE_FILENAME)
        write(self.image, self.IMAGE)
        write(self.overlay, self.OVERLAY)

    def rebuild(self, overlays=None):
        return dpc.rebuild(
            self.config,
            self.image,
            [self.overlay] if overlays is None else overlays,
        )

    def test_fresh_volume_gets_image_plus_overlay(self):
        self.rebuild()
        got = read(self.config)
        # The operator's keys land...
        self.assertTrue(got["platforms"]["slack"]["enabled"])
        self.assertEqual("approve", got["approvals"]["cron_mode"])
        # ...and the image's own keys are not dropped by them.
        self.assertEqual("Kage is thinking…", got["platforms"]["google_chat"]["typing_status_text"])
        self.assertEqual(
            ["hermes_otel", "session_store", "legacy_slash_commands"], got["plugins"]["enabled"]
        )

    def test_first_start_on_an_existing_volume_loses_nothing(self):
        # Before this code shipped, the entrypoint force-copied the image config over the
        # live file on every start, so that is what a pre-upgrade volume holds. With no
        # recorded baseline it must not be mistaken for a pile of runtime edits.
        write(self.config, self.IMAGE)
        self.assertFalse(self.baseline.exists())
        self.rebuild()
        self.assertEqual(read(self.config), read(self.baseline))

    def test_an_image_upgrade_reaches_a_pre_existing_volume(self):
        # The regression. A volume that predates this script holds the OLD image's
        # config; the first start sees the NEW one. Substituting the new image for the
        # missing baseline made every key the release changed look like a runtime edit,
        # so `ours` won — and since the baseline written at the end is the correct new
        # one, every later start re-derived the same verdict. The upgrade never landed,
        # on any restart, with no log line. Both of the `${HERMES_HOME}` router path and
        # the `incident_context` plugin were lost this way.
        old_image = {
            "mcp_servers": {"router": {"args": ["/opt/data/scripts/router_server.py"]}},
            "plugins": {"enabled": ["hermes_otel"]},
            "platforms": {"google_chat": {"typing_status_text": "old"}},
        }
        new_image = {
            "mcp_servers": {"router": {"args": ["${HERMES_HOME}/scripts/router_server.py"]}},
            "plugins": {"enabled": ["hermes_otel", "incident_context"]},
            "platforms": {"google_chat": {"typing_status_text": "new"}},
        }
        write(self.config, old_image)  # the pre-upgrade volume
        write(self.image, new_image)
        self.overlay.unlink()
        self.assertFalse(self.baseline.exists())

        for start in range(1, 4):  # and it must not drift back on a later restart
            self.rebuild()
            got = read(self.config)
            self.assertEqual(
                ["${HERMES_HOME}/scripts/router_server.py"],
                got["mcp_servers"]["router"]["args"],
                f"start {start}",
            )
            self.assertIn("incident_context", got["plugins"]["enabled"], f"start {start}")
            self.assertEqual("new", got["platforms"]["google_chat"]["typing_status_text"])

    def test_the_upgrade_keeps_the_runtime_state_it_finds(self):
        # The other half of the case above: adopting the declarations must not cost the
        # volume the keys only the runtime knows about.
        live = dict(self.IMAGE)
        live["platforms"] = {
            "google_chat": {"typing_status_text": "Kage is thinking…"},
            "slack": {"home_channel": {"id": "C123", "platform": "slack"}},
        }
        live["monitoring"] = {"install_id": "d3adbeef"}
        write(self.config, live)
        self.assertFalse(self.baseline.exists())

        self.rebuild()

        got = read(self.config)
        self.assertEqual({"id": "C123", "platform": "slack"}, got["platforms"]["slack"]["home_channel"])
        self.assertEqual("d3adbeef", got["monitoring"]["install_id"])
        self.assertTrue(got["platforms"]["slack"]["enabled"])  # and the overlay still lands

    def test_home_channel_survives_a_restart(self):
        self.rebuild()
        live = read(self.config)
        live["platforms"]["slack"]["home_channel"] = {"id": "C123", "platform": "slack"}
        live["monitoring"] = {"install_id": "d3adbeef"}
        write(self.config, live)

        self.rebuild()  # the restart

        got = read(self.config)
        self.assertEqual({"id": "C123", "platform": "slack"}, got["platforms"]["slack"]["home_channel"])
        self.assertEqual("d3adbeef", got["monitoring"]["install_id"])
        self.assertTrue(got["platforms"]["slack"]["enabled"])

    def test_a_changed_overlay_wins_over_a_stale_runtime_edit(self):
        self.rebuild()
        live = read(self.config)
        live["approvals"]["cron_mode"] = "never"
        write(self.config, live)

        overlay = dict(self.OVERLAY, approvals={"cron_mode": "auto"})
        write(self.overlay, overlay)
        self.rebuild()

        self.assertEqual("auto", read(self.config)["approvals"]["cron_mode"])

    def test_an_unchanged_overlay_leaves_a_runtime_edit_alone(self):
        self.rebuild()
        live = read(self.config)
        live["approvals"]["cron_mode"] = "never"
        write(self.config, live)

        self.rebuild()

        self.assertEqual("never", read(self.config)["approvals"]["cron_mode"])

    def test_an_image_change_reaches_the_agent(self):
        self.rebuild()
        write(self.image, dict(self.IMAGE, toolsets=["kanban"]))
        self.rebuild()
        self.assertEqual(["kanban"], read(self.config)["toolsets"])

    def test_a_withdrawn_overlay_key_disappears(self):
        self.rebuild()
        overlay = {k: v for k, v in self.OVERLAY.items() if k != "approvals"}
        write(self.overlay, overlay)
        self.rebuild()
        self.assertNotIn("approvals", read(self.config))

    def test_no_overlay_is_not_an_error(self):
        # Plain docker, or the kustomize bases: there is no operator and no ConfigMap.
        self.overlay.unlink()
        self.rebuild()
        self.assertEqual(self.IMAGE, read(self.config))

    def test_rebuild_is_idempotent(self):
        self.rebuild()
        first = read(self.config)
        self.rebuild()
        self.assertEqual(first, read(self.config))

    def test_missing_image_config_is_loud(self):
        self.image.unlink()
        with self.assertRaises(FileNotFoundError):
            self.rebuild()

    def test_baseline_records_no_runtime_state(self):
        self.rebuild()
        live = read(self.config)
        live["monitoring"] = {"install_id": "d3adbeef"}
        write(self.config, live)
        self.rebuild()
        self.assertNotIn("monitoring", read(self.baseline))

    def test_a_peer_replicas_boot_keeps_the_live_leaders_runtime_edits(self):
        # Above one replica EVERY pod's agent container reaches step 2d. PLATFORM_AGENT_ROLE
        # tells the containers of one pod apart, never the replicas of a Deployment — those
        # all come from a single PodTemplateSpec — so a follower booting during a rolling
        # update re-runs this against the volume the live leader is already using. The
        # entrypoint is allowed to say so only because the repeat is a no-op that carries
        # runtime keys through; if that ever stops being true, a scale-up silently reverts
        # `/sethome`.
        self.rebuild()
        live = read(self.config)
        live["platforms"]["slack"]["home_channel"] = "C0FFEE"  # /sethome, on the leader
        live["monitoring"] = {"install_id": "d3adbeef"}
        write(self.config, live)

        self.rebuild()  # the peer replica's boot

        got = read(self.config)
        self.assertEqual("C0FFEE", got["platforms"]["slack"]["home_channel"])
        self.assertEqual("d3adbeef", got["monitoring"]["install_id"])
        # ...and the operator's keys are still there, so it is a no-op, not a revert.
        self.assertEqual("approve", got["approvals"]["cron_mode"])

    def test_writers_do_not_share_a_staging_path(self):
        # A fixed `config.yaml.tmp` is atomic only while exactly one writer exists. The
        # step-1.6 bootstrap lock normally guarantees that but has three documented ways to
        # let two through — no `flock` binary, an unwritable $TARGET_DIR, and the
        # `flock -w 300` timeout that proceeds "concurrently with the peer container". On a
        # shared staging name the loser's os.replace fires on a path the winner already
        # renamed away, and a reader in between sees config.yaml at zero bytes.
        staged = []
        real_replace = dpc.os.replace

        def spy(src, dst):
            staged.append(str(src))
            return real_replace(src, dst)

        with mock.patch.object(dpc.os, "replace", spy):
            self.rebuild()
            self.rebuild()

        self.assertEqual(len(staged), len(set(staged)), f"staging paths collided: {staged}")
        self.assertEqual([], list(self.config.parent.glob("*.tmp")))


class ArgvUnionTest(unittest.TestCase):
    """`args` is the one list the image and the operator must not merely agree about.

    Every other list here is a set — plugins, toolsets, disabled_toolsets — where a
    union is the intended behaviour and a stray extra entry is at worst inert. A
    command line is a sequence: union two differing declarations and you get two argv
    words, and the process runs the first. The router MCP would then start against a
    path that does not exist and the Chat Agent would have no specialist roster.
    """

    CHAT_CONFIG = pathlib.Path(__file__).resolve().parents[1] / "agents" / "chat" / "config.yaml"

    def test_differing_args_concatenate_rather_than_replace(self):
        # The mechanism, stated once so the guard below has a reason attached.
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        image, overlay = tmp / "image.yaml", tmp / "overlay.yaml"
        write(image, {"mcp_servers": {"router": {"args": ["/opt/data/scripts/router_server.py"]}}})
        write(overlay, {"mcp_servers": {"router": {"args": ["/var/lib/kage/scripts/router_server.py"]}}})
        baseline = dpc.build_baseline(image, [overlay])
        self.assertEqual(
            baseline["mcp_servers"]["router"]["args"],
            ["/opt/data/scripts/router_server.py", "/var/lib/kage/scripts/router_server.py"],
        )

    def test_the_image_declares_the_router_script_home_independently(self):
        # So the operator's declaration can be byte-identical at ANY agentHome and the
        # union above collapses to one entry. renderConfigYAML holds the other half;
        # the operator's TestRenderConfigYAMLListsMatchChatConfig compares the two,
        # under a custom agentHome as well as the default.
        args = yaml.safe_load(self.CHAT_CONFIG.read_text())["mcp_servers"]["router"]["args"]
        self.assertEqual(args, ["${HERMES_HOME}/scripts/router_server.py"])


if __name__ == "__main__":
    unittest.main()
