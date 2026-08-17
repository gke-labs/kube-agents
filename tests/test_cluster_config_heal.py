"""Tests for deploy/shared/cluster_config_heal.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest: PyYAML is the only import beyond the standard library, and
it already ships in every environment that runs the agent.

The backfill runs at pod startup against every cluster profile already on the PVC, and
its failure mode is the one it exists to fix: a Cluster Agent whose persona tells it it
MUST call `send_notification` while its config declares no server that provides it. The
agent writes the triage and drops it, and nothing anywhere says so. These cover the
parts that are easy to get wrong: additive-only, idempotency, and never disturbing the
`cluster_identity` stamp that reconcile matches a profile to its cluster by.
"""

import importlib.util
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

import yaml

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared" / "cluster_config_heal.py"
)
_spec = importlib.util.spec_from_file_location("cluster_config_heal", _MODULE_PATH)
cch = importlib.util.module_from_spec(_spec)
sys.modules["cluster_config_heal"] = cch
_spec.loader.exec_module(cch)


# A cut-down stand-in for /opt/cluster-template/config.yaml: the `notify` server and the
# two toolsets that expose it, plus a neighbour server so "additive" can be told apart
# from "copied the template over the top".
TEMPLATE = {
    "mcp_servers": {
        "gke": {"command": "gke-mcp"},
        "notify": {
            "command": "/opt/hermes/.venv/bin/python3",
            "args": ["${HERMES_HOME}/scripts/notify_server.py"],
            "lazy": True,
            "env": {"SESSION_KV_API_KEY": "${SESSION_KV_API_KEY}"},
        },
    },
    "platform_toolsets": {
        "cli": ["mcp-gke", "mcp-notify"],
        "api_server": ["mcp-gke", "mcp-notify"],
    },
}

# What a profile scaffolded before `notify` existed looks like on the PVC, identity
# stamp and all.
STALE_PROFILE = {
    "mcp_servers": {"gke": {"command": "gke-mcp"}},
    "platform_toolsets": {"cli": ["mcp-gke"], "api_server": ["mcp-gke"]},
    "cluster_identity": {
        "project": "my-project",
        "cluster": "my-cluster",
        "location": "us-central1",
    },
    "agent": {"max_turns": 120},
}


class HealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.template = self.tmp / "cluster-template" / "config.yaml"
        self.profile = self.tmp / "profiles" / "cluster-x" / "config.yaml"
        self.write(self.template, TEMPLATE)
        self.write(self.profile, STALE_PROFILE)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=True))

    def read(self):
        return yaml.safe_load(self.profile.read_text())

    def heal(self):
        return cch.heal(self.profile, self.template)

    def test_a_stale_profile_gains_the_server_and_both_toolset_entries(self):
        changes = self.heal()
        config = self.read()
        self.assertEqual(
            config["mcp_servers"]["notify"], TEMPLATE["mcp_servers"]["notify"]
        )
        self.assertIn("mcp-notify", config["platform_toolsets"]["cli"])
        self.assertIn("mcp-notify", config["platform_toolsets"]["api_server"])
        self.assertEqual(len(changes), 3)

    def test_the_identity_stamp_and_everything_else_survive(self):
        # The whole reason this is a targeted merge rather than a config re-sync:
        # reconcile reads `cluster_identity` to match a profile to its cluster, and a
        # profile it cannot identify gets a duplicate scaffolded beside it.
        self.heal()
        config = self.read()
        self.assertEqual(config["cluster_identity"], STALE_PROFILE["cluster_identity"])
        self.assertEqual(config["agent"], {"max_turns": 120})
        self.assertEqual(config["mcp_servers"]["gke"], {"command": "gke-mcp"})

    def test_a_healed_profile_is_not_rewritten_again(self):
        self.heal()
        before = self.profile.read_bytes()
        mtime = self.profile.stat().st_mtime_ns
        self.assertEqual(self.heal(), [])
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual(self.profile.stat().st_mtime_ns, mtime)

    def test_an_existing_server_block_is_never_overwritten(self):
        # A profile whose `notify` has been tuned — by an operator overlay, or by hand —
        # keeps what it has. This backfills absence; it does not enforce the template.
        stale = dict(STALE_PROFILE)
        stale["mcp_servers"] = {
            **STALE_PROFILE["mcp_servers"],
            "notify": {"command": "custom", "timeout": 900},
        }
        self.write(self.profile, stale)
        self.heal()
        self.assertEqual(
            self.read()["mcp_servers"]["notify"], {"command": "custom", "timeout": 900}
        )

    def test_a_half_healed_profile_gets_only_the_missing_half(self):
        # A run interrupted between the two writes, or a config hand-edited to drop the
        # toolset entry, leaves a server that exists and is exposed nowhere. The two are
        # checked independently so the second half is still repairable.
        stale = dict(STALE_PROFILE)
        stale["mcp_servers"] = {
            **STALE_PROFILE["mcp_servers"],
            "notify": TEMPLATE["mcp_servers"]["notify"],
        }
        self.write(self.profile, stale)
        changes = self.heal()
        self.assertEqual(
            sorted(changes),
            [
                "platform_toolsets.api_server[mcp-notify]",
                "platform_toolsets.cli[mcp-notify]",
            ],
        )
        self.assertIn("mcp-notify", self.read()["platform_toolsets"]["api_server"])

    def test_a_toolset_the_profile_does_not_declare_is_not_invented(self):
        # `platform_toolsets.api_server` absent means this profile is not exposed over
        # the API server at all. Creating the list would hand it a surface the image
        # never gave it.
        stale = dict(STALE_PROFILE)
        stale["platform_toolsets"] = {"cli": ["mcp-gke"]}
        self.write(self.profile, stale)
        self.heal()
        self.assertNotIn("api_server", self.read()["platform_toolsets"])

    def test_an_empty_mcp_servers_key_is_filled_rather_than_crashed_on(self):
        # `mcp_servers:` with nothing under it parses as None, and None is what
        # dict.setdefault hands back for a key that already exists.
        stale = dict(STALE_PROFILE)
        stale["mcp_servers"] = None
        self.write(self.profile, stale)
        self.heal()
        self.assertIn("notify", self.read()["mcp_servers"])

    def test_no_template_means_no_change(self):
        # A dashboard sidecar, or any image built without /opt/cluster-template. Doing
        # nothing is right; writing an empty config is not.
        before = self.profile.read_bytes()
        self.assertEqual(cch.heal(self.profile, self.tmp / "nowhere.yaml"), [])
        self.assertEqual(self.profile.read_bytes(), before)

    def test_a_template_without_the_server_backfills_nothing(self):
        # The template dropping `notify` is a removal, and removal is not this script's
        # job — it must not then strip the server from profiles that have it.
        self.write(self.template, {"mcp_servers": {"gke": {"command": "gke-mcp"}}})
        self.assertEqual(self.heal(), [])

    def test_a_missing_profile_config_is_a_no_op_not_a_crash(self):
        # The entrypoint guards on the file existing, but this runs per profile in a
        # loop and a raise would take the remaining profiles down with it.
        self.assertEqual(cch.heal(self.tmp / "gone" / "config.yaml", self.template), [])

    def test_the_cli_reports_what_it_added(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cch.main(
                ["--profile-dir", str(self.profile.parent), "--template", str(self.template)]
            )
        self.assertEqual(rc, 0)
        self.assertIn("mcp_servers.notify", err.getvalue())
        self.assertIn("notify", self.read()["mcp_servers"])

    def test_the_cli_is_silent_when_there_is_nothing_to_do(self):
        self.heal()
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cch.main(
                ["--profile-dir", str(self.profile.parent), "--template", str(self.template)]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")

    def test_no_temp_file_is_left_behind(self):
        self.heal()
        self.assertEqual(
            [p.name for p in self.profile.parent.iterdir()], ["config.yaml"]
        )


if __name__ == "__main__":
    unittest.main()
