"""Every remote MCP server this repository declares names itself on the wire.

    python3 -m unittest discover -s tests -p 'test_*.py'

Calls to `developerknowledge.googleapis.com/mcp` and `container.googleapis.com/mcp`
used to go out with undici's default `node` User-Agent, so the API teams serving
those endpoints could not tell kube-agents traffic from any other Node process.
Each `mcp_servers` entry that launches the mcp-remote proxy now passes a
`--header User-Agent: …` naming the product and the build it came from.

Three ways that silently stops being true, one check each:

* A new remote server is added and nobody remembers the flag. Every entry whose
  argv is the proxy is checked, not a list of the two that exist today.
* The version placeholder outlives the environment variable it reads. Hermes
  leaves an unresolved `${VAR}` in place verbatim, so a renamed or dropped
  `ENV KUBE_AGENTS_VERSION` in the Dockerfile turns the header into the literal
  string rather than failing anything.
* The two files that are merged at image build drift apart.
  `deploy/docker/merge_configs.py` unions two arg lists, and the union
  deduplicates, so one spelling in the shared defaults and a different one in
  the platform overlay do NOT produce two headers — the repeated `--header`
  token collapses and the overlay's value is left as a stray positional that
  mcp-remote silently discards. So the merge is run here rather than reasoned
  about, and what is asserted is the shape of the merged argv.

`agents/platform/scripts/test_mcp_env_contract.py` makes the same
assert-against-the-merged-config argument for the platform profile's toolsets
and environment. This file is separate because its subject is every remote
server in every config, the Cluster Agent template included, and because the
two live under different test roots (see AGENTS.md, "Where Tests Go").
"""

import pathlib
import sys
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED_DEFAULTS = REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml"
# Named separately because the merge test needs this one specifically: it is the
# overlay half of what the image build merges into the platform profile.
PLATFORM_CONFIG = REPO_ROOT / "agents" / "platform" / "config.yaml"
DOCKERFILE = REPO_ROOT / "deploy" / "docker" / "Dockerfile"
# Every Cloud Build config that builds the `platform` target: the publish one the
# release workflows and the dev loop submit, and the CI one hack/ci-deploy.sh
# submits for a pull request's evaluation cluster. A config that stops passing
# the version publishes images whose traffic reports the `dev` default, which is
# what a laptop build reports too.
CLOUDBUILDS = (
    REPO_ROOT / "deploy" / "docker" / "cloudbuild.yaml",
    REPO_ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml",
)

PROXY_ARG = "/opt/mcp-remote/dist/proxy.js"
VERSION_VAR = "KUBE_AGENTS_VERSION"

# Discovered, not listed: a config.yaml added for a new agent profile is checked
# the day it lands. agents/chat/config.yaml declares no remote server today and
# is scanned anyway, which costs nothing and is the point — the flag is easy to
# forget precisely when someone is copying an existing entry into a new profile.
CONFIGS = (SHARED_DEFAULTS,) + tuple(sorted((REPO_ROOT / "agents").glob("*/config.yaml")))

# One string at every call site, version placeholder included. Asserted whole
# rather than by pattern: the point of the header is that the API teams can key
# a dashboard on it, and a per-file variation is what quietly splits that key.
USER_AGENT = "User-Agent: kube-agents/${%s}" % VERSION_VAR


def remote_servers(config_path):
    """Yield (alias, args) for every entry that launches the mcp-remote proxy."""
    document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for alias, spec in (document.get("mcp_servers") or {}).items():
        args = (spec or {}).get("args") or []
        if PROXY_ARG in args:
            yield alias, args


def header_values(args):
    """The value of every `--header` flag in *args*, in order."""
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--header"]


def positional_args(args):
    """What mcp-remote is left holding after it splices out the flag pairs.

    args[0] is the script path node runs; mcp-remote never sees it. Of what
    remains it takes the first positional as the server URL and the second, if
    there is one, as an optional callback port. So a stray extra positional is
    not a loud error — it is parsed to NaN and dropped.
    """
    positional = []
    index = 1
    while index < len(args):
        if args[index] == "--header":
            index += 2
            continue
        positional.append(args[index])
        index += 1
    return positional


class RemoteMcpUserAgentTest(unittest.TestCase):
    def test_every_remote_server_sends_a_user_agent(self):
        checked = 0
        for config_path in CONFIGS:
            for alias, args in remote_servers(config_path):
                where = f"{alias} in {config_path.relative_to(REPO_ROOT)}"
                headers = header_values(args)
                self.assertEqual(
                    len(headers),
                    1,
                    f"{where} passes {len(headers)} --header flags, expected exactly "
                    "one carrying the User-Agent",
                )
                self.assertEqual(
                    headers[0],
                    USER_AGENT,
                    f"{where} sends {headers[0]!r} rather than {USER_AGENT!r}, the "
                    "one string the API teams key their dashboards on",
                )
                checked += 1
        self.assertGreaterEqual(
            checked,
            5,
            "found fewer remote MCP servers than this repository declares — the "
            "scan above is matching nothing and would pass an empty tree",
        )

    def test_the_argv_still_reaches_mcp_remote_as_a_flag_pair(self):
        """`--header` and its value are adjacent, and the URL is still there.

        mcp-remote splices out `--header <value>` pairs and then reads the URL as
        the first remaining positional. A value that lost its flag, or a flag
        that lost its value, leaves the URL in the wrong place and the proxy
        exits on a usage error at first tool call.
        """
        for config_path in CONFIGS:
            for alias, args in remote_servers(config_path):
                where = f"{alias} in {config_path.relative_to(REPO_ROOT)}"
                self.assertNotEqual(
                    args[-1],
                    "--header",
                    f"{where} ends on a --header with no value",
                )
                self.assert_argv_shape(args, where)

    def assert_argv_shape(self, args, where):
        positional = positional_args(args)
        self.assertEqual(
            len(positional),
            1,
            f"{where} leaves {positional!r} for mcp-remote to read, expected only "
            "the server URL",
        )
        self.assertTrue(
            positional[0].startswith("https://"),
            f"{where} would send mcp-remote to {positional[0]!r}",
        )

    def test_the_build_time_merge_yields_one_header_per_server(self):
        """The union of the two platform-side files is still one command line.

        Checked on the merged argv's SHAPE, not on its header count. The union
        is `list(dict.fromkeys(a + b))` — deduplicating — so two spellings do
        not yield two headers: the repeated `--header` token collapses and the
        divergent value is left stranded as a second positional, which
        mcp-remote reads as its optional callback port and discards. Counting
        headers here would pass on exactly the drift this test exists to catch.
        """
        sys.path.insert(0, str(REPO_ROOT / "deploy" / "docker"))
        try:
            from merge_configs import merge
        finally:
            sys.path.pop(0)

        base = yaml.safe_load(SHARED_DEFAULTS.read_text(encoding="utf-8"))
        overlay = yaml.safe_load(PLATFORM_CONFIG.read_text(encoding="utf-8"))
        merged = merge(base, overlay)

        for alias, spec in merged["mcp_servers"].items():
            args = (spec or {}).get("args") or []
            if PROXY_ARG not in args:
                continue
            where = f"{alias}, merged from the shared defaults and the platform overlay"
            self.assert_argv_shape(args, where)
            self.assertEqual(
                header_values(args),
                [USER_AGENT],
                f"{where}, sends {header_values(args)!r}; the two files have to "
                "spell the flag identically because merge_configs unions arg lists",
            )

    def test_the_image_defines_the_version_the_header_interpolates(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            rf"(?m)^ENV {VERSION_VAR}=",
            f"the configs interpolate ${{{VERSION_VAR}}} from the agent process "
            "environment; without the ENV the header ships the literal placeholder",
        )
        for cloudbuild in CLOUDBUILDS:
            self.assertRegex(
                cloudbuild.read_text(encoding="utf-8"),
                rf'{VERSION_VAR}="?\$_{VERSION_VAR}',
                f"{cloudbuild.relative_to(REPO_ROOT)} no longer passes the version "
                "through to the build, so every image it publishes would report the "
                "`dev` default",
            )


if __name__ == "__main__":
    unittest.main()
