"""Contract tests for the Platform Agent's effective MCP configuration.

Run: python3 -m unittest agents.platform.scripts.test_mcp_env_contract

Every assertion here is made against the MERGED config — what actually lands at
/opt/platform-template/config.yaml — not against either source file, because
neither file alone is what the agent reads. deploy/docker/Dockerfile builds it
by running deploy/docker/merge_configs.py over deploy/shared/defaults/config.yaml
(base) and agents/platform/config.yaml (overlay), and that merge unions lists
rather than replacing them. A toolset entry deleted from the overlay and left in
the base survives; a test that read only the overlay would call that a pass.
The real merge() is imported rather than reimplemented for the same reason.
"""

import ast
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(REPO_ROOT / "deploy" / "docker"))

from merge_configs import merge  # noqa: E402

BASE_CONFIG = REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml"
OVERLAY_CONFIG = REPO_ROOT / "agents" / "platform" / "config.yaml"

TOOLSET_SURFACES = ("cli", "api_server")


def merged_config():
    """The platform profile's config.yaml as the image build produces it."""
    base = yaml.safe_load(BASE_CONFIG.read_text()) or {}
    overlay = yaml.safe_load(OVERLAY_CONFIG.read_text()) or {}
    return merge(base, overlay)


def env_names_bound_from_the_environment(source: str):
    """Env vars whose VALUE the module consumes, as opposed to merely probing.

    The distinction is what separates a defect from a design choice, and the
    presence of a default argument does not draw it. Bug B's read was
    `os.environ.get("API_SERVER_KEY", "").strip()` — a default was supplied and
    the module still failed closed on the empty string it produced. What made
    it a defect is that the value was consumed.

    So: a call whose result is used only to pick a branch is a PROBE and is
    excluded — the module has a defined answer either way, which is a working
    fallback rather than a broken read. Everything else is a BOUND read: the
    value flows into a request, a path, or a comparison, and an undeclared
    variable means it flows in empty.
    """
    tree = ast.parse(source)
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }

    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        func = node.func
        is_env_read = isinstance(func, ast.Attribute) and (
            func.attr == "getenv"
            or (
                func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            )
        )
        if not is_env_read:
            continue

        parent = parents.get(node)
        is_probe = (
            (isinstance(parent, (ast.If, ast.While, ast.IfExp)) and parent.test is node)
            or isinstance(parent, ast.BoolOp)
            or (isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not))
        )
        if not is_probe:
            names.add(first.value)
    return names


class ToolsetsMatchServersTest(unittest.TestCase):
    """A toolset entry and its server declaration have to move together."""

    def setUp(self):
        self.merged = merged_config()

    def test_every_mcp_toolset_entry_has_a_declared_server(self):
        # The failure this catches: deleting an mcp_servers block but leaving
        # `- mcp-<name>` in a toolset list. Hermes is then asked to expose a
        # server that was never declared.
        declared = set(self.merged.get("mcp_servers", {}))
        for surface in TOOLSET_SURFACES:
            entries = self.merged["platform_toolsets"][surface]
            for entry in entries:
                if not entry.startswith("mcp-"):
                    continue  # hermes-cli / hermes-api-server are native.
                self.assertIn(
                    entry[len("mcp-") :],
                    declared,
                    f"{surface} lists {entry} with no matching mcp_servers block",
                )

    def test_every_declared_server_is_exposed_by_some_toolset(self):
        # The mirror image, and the more expensive one: Hermes spawns a child
        # process for a declared server whether or not a toolset exposes it, so
        # a leftover block is a process started for nothing.
        exposed = {
            entry[len("mcp-") :]
            for surface in TOOLSET_SURFACES
            for entry in self.merged["platform_toolsets"][surface]
            if entry.startswith("mcp-")
        }
        for name in self.merged.get("mcp_servers", {}):
            self.assertIn(
                name, exposed, f"mcp_servers declares {name}, no toolset exposes it"
            )

    def test_the_two_surfaces_agree_on_their_mcp_servers(self):
        cli, api = (
            {e for e in self.merged["platform_toolsets"][s] if e.startswith("mcp-")}
            for s in TOOLSET_SURFACES
        )
        self.assertEqual(cli, api)


class AgentCommonIsNotMountedTest(unittest.TestCase):
    """call_agent stays unmounted; re-adding it needs more than a config line.

    The A2A tool could not reach the Platform Agent in this deployment, for
    three independent reasons, none of which a config change fixes:

      * the agent container's API_SERVER_KEY is "cluster-internal-trusted", the
        non-secret loopback sentinel — the real key is sidecar-only, and two
        operator tests pin that split;
      * the tool targets https://platform-agent…:8642, and that Service
        forwards to the sidecar's PLAINTEXT agent-API listener on 8643, so the
        TLS handshake fails before any auth happens;
      * the loopback API server behind it belongs to the `default` (Chat)
        profile, so a tool documented as "call the Platform Agent" would post
        to the Chat Agent.

    It also reintroduced the blocking-relay shape this repo removed with
    `ask_agent`. Delegation is kanban-only.
    """

    def setUp(self):
        self.merged = merged_config()

    def test_the_server_is_not_declared(self):
        self.assertNotIn("agent_common", self.merged.get("mcp_servers", {}))

    def test_no_toolset_exposes_it(self):
        for surface in TOOLSET_SURFACES:
            self.assertNotIn(
                "mcp-agent_common", self.merged["platform_toolsets"][surface]
            )

    def test_removing_it_from_the_overlay_alone_would_not_have_worked(self):
        # Guards the reasoning, not just the result. merge_configs.merge unions
        # lists, so had the entry been dropped only from agents/platform/
        # config.yaml, the base copy would have put it straight back at image
        # build and this whole change would have been a silent no-op.
        base = yaml.safe_load(BASE_CONFIG.read_text()) or {}
        resurrected = merge(
            {"platform_toolsets": {"cli": ["mcp-agent_common"]}},
            {"platform_toolsets": {"cli": base["platform_toolsets"]["cli"]}},
        )
        self.assertIn(
            "mcp-agent_common", resurrected["platform_toolsets"]["cli"]
        )

    def test_the_module_itself_is_still_importable(self):
        # Only the tool surface was withdrawn. platform_mcp_server and
        # session_kv_server both import helpers from this module, so deleting
        # the file would break two live servers.
        self.assertTrue((SCRIPTS_DIR / "agent_common_server.py").is_file())


class LocalServersDeclareTheEnvTheyReadTest(unittest.TestCase):
    """A local MCP server reads only the env its own block names.

    Hermes strips an MCP child's environment down to an allowlist — a live
    platform-profile child held exactly HERMES_HOME, HOME, LC_CTYPE and PATH —
    so a variable reaches a server only if that server's `env:` block declares
    it. Nothing else in the config makes this visible, and the symptom is a
    fail-closed error returned as an ordinary tool string, which reads to the
    model as a result and never reaches a log. This is the mechanical check.

    Run against the pre-fix tree it fails on `agent_common` / `API_SERVER_KEY`,
    which is the bug it exists to prevent recurring.

    Known gap: presence probes are exempt, so a variable read ONLY as
    `if os.environ.get("X"):` goes unchecked. That is deliberate — such a read
    has a working answer when the variable is absent — but it does mean a
    branch which can never be taken raises no alarm here.
    """

    # HERMES_HOME, HOME, LC_CTYPE and PATH reach every MCP child whether or not
    # a block names them, so reading one is not evidence of a missing
    # declaration. Kept explicit rather than folded into each block's `env:`.
    ALLOWLIST_PASSTHROUGH = frozenset({"HERMES_HOME", "HOME", "LC_CTYPE", "PATH"})

    def test_each_local_python_server_declares_what_it_reads(self):
        merged = merged_config()
        checked = 0
        for name, block in merged.get("mcp_servers", {}).items():
            if not str(block.get("command", "")).endswith("python3"):
                continue
            script = SCRIPTS_DIR / Path(block["args"][0]).name
            if not script.is_file():
                continue  # Shipped from elsewhere in the image; not ours to read.
            declared = set(block.get("env", {})) | self.ALLOWLIST_PASSTHROUGH
            for var in env_names_bound_from_the_environment(script.read_text()):
                self.assertIn(
                    var,
                    declared,
                    f"{script.name} consumes the value of {var}, but the "
                    f"{name} mcp_servers block does not declare it — the child "
                    f"never receives it, so the read always yields the default",
                )
            checked += 1
        # A guard that passes because it examined nothing is not a passing
        # guard: platform_control is a local Python server and must be seen.
        self.assertGreater(checked, 0, "no local Python MCP server was examined")


if __name__ == "__main__":
    unittest.main()
