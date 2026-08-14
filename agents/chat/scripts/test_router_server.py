import importlib
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Add the directory containing router_server.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))
# And the Platform Agent's, for notify_delivery. In the image the two directories
# are one — deploy/docker/Dockerfile COPYs both into /opt/defaults/scripts and the
# entrypoint copies that to $HERMES_HOME/scripts — so this only reconstructs at
# test time what is a single sys.path[0] at runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))

import agent_roster  # noqa: E402


def _load_router_server():
    """Import the module under test.

    These tests exercise only stdlib logic (delegation to agent_roster + the
    absence of the removed relay). When the hermes runtime deps (FastMCP /
    pydantic) aren't importable, fall back to minimal stubs so the module still
    imports in a bare checkout. `FastMCP().tool()` returns identity, so the
    decorated tools remain plain callables.
    """
    try:
        return importlib.import_module("router_server")
    except Exception:
        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        sys.modules.update({
            "mcp": mcp, "mcp.server": mcp_server, "mcp.server.fastmcp": fastmcp,
            "pydantic": pydantic,
        })
        return importlib.import_module("router_server")


router = _load_router_server()


class TestListAgentsDelegates(unittest.TestCase):
    """The tool is a thin wrapper over the shared roster.

    Discovery and formatting are covered in test_agent_roster.py. What matters
    here is that the tool reads the SAME module the injected block does: a
    refresh path that renders the fleet differently from the block it refreshes
    is worse than no refresh path at all.
    """

    def test_returns_the_shared_roster(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "platform", "cluster-a"):
                (base / name).mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            agent_roster.PROFILES_BASE = base

            self.assertEqual(router.list_agents(), agent_roster.render())
            self.assertIn("- platform: Fleet + GitOps write path.", router.list_agents())

    def test_resolves_the_roster_base_at_call_time(self):
        # A `from agent_roster import PROFILES_BASE` in router_server would be a
        # second binding that never sees a rebind of the module's own — the tool
        # would keep reading whichever path was current at import.
        with TemporaryDirectory() as tmp:
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("No specialist agents", router.list_agents())

    def test_an_unreadable_roster_says_so_rather_than_claiming_none(self):
        # render() answers None when discovery itself failed. A tool has to
        # return a string, and "no specialist agents" is the one string it must
        # not be: the front door would stop routing on an I/O fault.
        with mock.patch.object(agent_roster, "render", return_value=None):
            out = router.list_agents()
        self.assertEqual(out, agent_roster.UNKNOWN_ROSTER)
        self.assertNotIn("No specialist agents", out)


class TestKanbanOnly(unittest.TestCase):
    """Delegation is discovery + kanban: the synchronous ask_agent relay is gone.

    Delegation happens exclusively via the asynchronous kanban board so the user
    sees non-blocking progress in the thread; the router only advertises the
    dynamic specialist roster used to pick an assignee.
    """

    def test_ask_agent_removed(self):
        self.assertFalse(hasattr(router, "ask_agent"))

    def test_no_blocking_relay_timeout(self):
        # INVOKE_TIMEOUT only existed to bound the removed synchronous relay.
        # `_run_env` is deliberately NOT asserted absent alongside it: it came
        # back for send_notification's `hermes send`, which is a one-shot post
        # and not a reasoning loop anyone waits on.
        self.assertFalse(hasattr(router, "INVOKE_TIMEOUT"))


class TestSendNotificationIsTheFrontDoorsOnlyEgress(unittest.TestCase):
    """The Chat Agent has to be able to post into a thread it is not speaking in.

    Kanban delivers a finished card by posting into the thread the card came
    from; for a session that arrived over the API server there is none, so it
    wakes the card's creator and that turn's post IS the delivery. The creator is
    always this profile whatever the assignee, so without this tool every RCA
    delegated to a Cluster Agent — and every one the Chat Agent answered itself —
    was written and then dropped (#630).
    """

    def test_it_delegates_to_the_shared_implementation(self):
        # Shared with platform_control's tool of the same name; a second copy
        # here would be a second thing to keep in step with the KV schema.
        import notify_delivery

        with mock.patch.object(notify_delivery, "deliver_notification") as deliver:
            with mock.patch.object(router, "deliver_notification", deliver):
                router.send_notification("the report", "k8s-evt-abc123")

        deliver.assert_called_once()
        args, kwargs = deliver.call_args
        self.assertEqual(args[0], "the report")
        self.assertEqual(args[1], "k8s-evt-abc123")
        self.assertEqual(kwargs["config_path"], router.CONFIG_PATH)
        self.assertIs(kwargs["run_env"], router._run_env)

    def test_the_front_door_never_posts_a_duplicate(self):
        # A `platform` assignee posts the report itself and is THEN followed by
        # the wake, so an unconditional relay would double every RCA on the one
        # branch that already worked. The front door cannot tell the assignees
        # apart, so it always relays and the tool suppresses the duplicate.
        with mock.patch.object(router, "deliver_notification") as deliver:
            router.send_notification("the report", "k8s-evt-abc123")
        self.assertTrue(deliver.call_args.kwargs["only_if_undelivered"])

    def test_the_session_id_is_optional(self):
        # An omitted id is a home-channel post, not an error: a report in the
        # wrong place still beats no report.
        with mock.patch.object(router, "deliver_notification") as deliver:
            router.send_notification("the report")
        self.assertEqual(deliver.call_args[0][1], "")

    def test_config_path_follows_a_custom_agent_home(self):
        # A CR that sets spec.harness.hermes.agentHome moves the profile config;
        # a hardcoded /opt/data would read a file that is not there and lose the
        # `platforms` block that says which chat platform is live.
        with mock.patch.dict("os.environ", {"HERMES_HOME": "/var/lib/kage"}):
            reloaded = importlib.reload(router)
            self.assertEqual(reloaded.CONFIG_PATH, "/var/lib/kage/config.yaml")
        importlib.reload(router)

    def test_run_env_redirects_home(self):
        # Same reason agent_common_server._run_env does it: the container's home
        # is not writable by the agent user, and `hermes send` writes there.
        self.assertEqual(router._run_env()["HOME"], "/tmp")


class TestConfigCarriesTheKeysTheToolReads(unittest.TestCase):
    """Hermes hands a stdio MCP server only the keys named in its `env` block.

    Every omission here is silent at runtime: without SESSION_KV_API_KEY the
    thread lookup 401s and the report quietly goes to the home channel instead of
    threading under the alert; without the home-channel keys there is no fallback
    either. The Platform Agent's config has the same test for the same reason.
    """

    def test_router_env_lists_every_variable_send_notification_reads(self):
        import yaml

        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        env = yaml.safe_load(config_path.read_text())["mcp_servers"]["router"]["env"]
        for key in (
            "SESSION_KV_API_KEY",
            "GOOGLE_CHAT_PROJECT_ID",
            "GOOGLE_CHAT_HOME_CHANNEL",
            "SLACK_HOME_CHANNEL",
        ):
            self.assertEqual(env.get(key), "${" + key + "}", f"{key} missing from router env")

    def test_the_delegation_surface_still_excludes_platform_control(self):
        # send_notification rides on the router server precisely so the front
        # door does not get the provisioning surface with it.
        import yaml

        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        for surface in config["platform_toolsets"].values():
            self.assertNotIn("mcp-platform_control", surface)


if __name__ == "__main__":
    unittest.main()
