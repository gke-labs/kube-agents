"""Unit tests for chat_platforms — which platforms a message is posted to.

Run: python3 -m unittest agents.platform.scripts.test_chat_platforms

The invariant under test: a platform the install has enabled is never silently
dropped, and the function never resolves to nothing.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_platforms as cp  # noqa: E402


MISSING = "/nonexistent/config.yaml"


def _tmp_yaml(text: str) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(text)
    tmp.close()
    return tmp.name


def _config(text: str):
    """A CONFIG_PATH patch pointing at a temp config.yaml holding `text`.

    The managed scope is pinned away at the same time: it outranks CONFIG_PATH, so a
    test that left it on its real default would be asserting against whatever
    /etc/hermes the machine running the suite happens to have.
    """
    return mock.patch.multiple(cp, CONFIG_PATH=_tmp_yaml(text), MANAGED_CONFIG_PATH=MISSING)


def _managed(text: str, profile: str | None = None):
    """A MANAGED_CONFIG_PATH patch, with CONFIG_PATH set only if `profile` is given."""
    return mock.patch.multiple(
        cp,
        MANAGED_CONFIG_PATH=_tmp_yaml(text),
        CONFIG_PATH=MISSING if profile is None else _tmp_yaml(profile),
    )


def _env(**kwargs):
    """Replace the environment with exactly `kwargs` — no host leakage."""
    return mock.patch.dict(os.environ, kwargs, clear=True)


class EnvSignalTest(unittest.TestCase):
    """A deployed pod: config.yaml settles nothing, the environment settles it all."""

    def setUp(self):
        # A missing file is the "config says nothing" case and needs no temp file.
        # Both files, so nothing above the environment can answer.
        self.no_config = mock.patch.multiple(
            cp, CONFIG_PATH=MISSING, MANAGED_CONFIG_PATH=MISSING
        )
        self.no_config.start()
        self.addCleanup(self.no_config.stop)

    def test_relay_url_enables_slack(self):
        # The regression #989 is about: SLACK_RELAY_URL is what the operator
        # actually sets on this container when spec.integration.slack.enabled.
        with _env(SLACK_RELAY_URL="http://127.0.0.1:8780"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_both_relay_urls_enable_both_google_chat_first(self):
        with _env(SLACK_RELAY_URL="http://x", GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_bot_token_absent_is_not_the_question(self):
        # The defect in the sibling resolvers: SLACK_BOT_TOKEN is a credential and
        # lives in the credential-proxy container, so a Slack install that has
        # everything except the token must still resolve to slack.
        with _env(SLACK_HOME_CHANNEL="C0123ABCD"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_empty_string_is_not_a_signal(self):
        # The operator renders SLACK_HOME_CHANNEL only when it is set, but an
        # empty value reaching us must not read as "Slack is on".
        with _env(SLACK_RELAY_URL="", SLACK_HOME_CHANNEL="   "):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_nothing_configured_falls_back_rather_than_returning_empty(self):
        # Preserves the pre-#989 behaviour: an install this cannot read is no
        # worse off than it was, and never sends to nowhere.
        with _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])


class ConfigFileTest(unittest.TestCase):
    def test_config_enables_without_any_env(self):
        with _config("platforms:\n  slack:\n    enabled: true\n"), _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_explicit_false_overrides_an_env_signal(self):
        # Somebody turning Slack off on a pod the operator still renders
        # SLACK_RELAY_URL for. The file is the more specific statement.
        with _config("platforms:\n  slack:\n    enabled: false\n"), \
             _env(SLACK_RELAY_URL="http://x", GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_config_naming_one_platform_does_not_silence_the_other(self):
        # Resolution is per platform, not per source. A config that mentions only
        # Slack must not hide a Google Chat the environment knows about — the
        # short-circuit bug this module exists to avoid.
        with _config("platforms:\n  slack:\n    enabled: true\n"), \
             _env(GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_platforms_block_without_enabled_is_not_an_answer(self):
        # The operator-managed shape: renderConfigYAML puts `enabled` in the
        # managed scope at /etc/hermes, so this file carries the subtree with no
        # `enabled` key. Must fall through to the env, not read as False.
        with _config("platforms:\n  slack:\n    home_channel: C0123ABCD\n"), \
             _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_malformed_config_degrades_to_the_env(self):
        with _config("platforms: [this is not a mapping\n"), \
             _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_valid_yaml_of_the_wrong_shape_does_not_raise(self):
        # The case the test above does NOT cover: `platforms: [a, b]` is valid YAML,
        # so safe_load returns cleanly and the wrong type reaches the traversal. A
        # `.get` on a list raises outside the try, escapes _notify — which
        # cluster_agent_reconcile.main does not guard — and fails an hourly run that
        # has already created and pruned profiles, in a script contracted to exit 0.
        for text in ("platforms: [google_chat, slack]\n", "platforms: slack\n",
                     "platforms: 3\n", "platforms:\n  slack: enabled\n"):
            with self.subTest(config=text):
                with _config(text), _env(SLACK_RELAY_URL="http://x"):
                    self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_a_whole_document_of_the_wrong_shape_does_not_raise(self):
        with _config("- just\n- a list\n"), _env(GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_valueless_enabled_is_not_an_explicit_no(self):
        # `enabled:` with nothing after it parses to None. That is "this file does not
        # say", not "this file says no", so it must not outrank the relay URL the
        # operator rendered — the distinction _platforms_enabled_in's docstring promises.
        with _config("platforms:\n  slack:\n    enabled:\n"), \
             _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_empty_config_degrades_to_the_env(self):
        with _config(""), _env(GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_no_pyyaml_degrades_to_the_env(self):
        # PyYAML is on the agent image, so this branch never runs there — it exists
        # because the import is inside the function precisely so that an interpreter
        # without it still resolves a platform instead of failing the cron run.
        # `sys.modules[name] = None` is what makes `import name` raise ImportError.
        with _config("platforms:\n  slack:\n    enabled: false\n"), \
             _env(SLACK_RELAY_URL="http://x"), \
             mock.patch.dict(sys.modules, {"yaml": None}):
            # The config's explicit `false` would have won had it been readable.
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])


class ManagedScopeTest(unittest.TestCase):
    """The operator's own answer, which outranks both sources below it."""

    def test_managed_scope_settles_both_platforms(self):
        # The shape renderConfigYAML always writes: neither `enabled` field carries
        # `omitempty`, so both keys are emitted as explicit booleans every reconcile.
        with _managed("platforms:\n  google_chat:\n    enabled: true\n"
                      "  slack:\n    enabled: true\n"), _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_managed_false_beats_a_stale_relay_url(self):
        # The regression #1111's docstring warns whoever lands second about, and the
        # reason the managed scope had to come across rather than being left out: an
        # operator set slack.enabled: false, but SLACK_RELAY_URL is still on the
        # container. Reading the environment here re-enables a leg that was turned
        # off. It must not.
        with _managed("platforms:\n  google_chat:\n    enabled: true\n"
                      "  slack:\n    enabled: false\n"), \
             _env(SLACK_RELAY_URL="http://x", GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_managed_scope_outranks_the_profile_file(self):
        # /sethome and hand edits write the profile's file; the CR writes the managed
        # one. When they disagree the operator wins.
        with _managed("platforms:\n  slack:\n    enabled: false\n",
                      profile="platforms:\n  slack:\n    enabled: true\n"), _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_a_platform_the_managed_scope_omits_falls_through(self):
        # Per platform, not per source. A managed file that names only Slack must not
        # hide the Google Chat the profile file knows about — the same short circuit
        # this module exists to avoid, one source higher up.
        with _managed("platforms:\n  slack:\n    enabled: true\n",
                      profile="platforms:\n  google_chat:\n    enabled: true\n"), _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_unreadable_managed_scope_degrades_to_the_sources_below(self):
        with _managed("platforms: [not a mapping\n"), _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])


class ConfigPathContractTest(unittest.TestCase):
    """Both path constants are copied rather than imported; neither may drift."""

    @staticmethod
    def _definition(module: str, name: str) -> str:
        # Read as text rather than imported: importing either neighbour for its
        # constant would pull FastMCP in, which ImportCostTest below exists to
        # prevent. Consumes lines until the parentheses balance, so a one-line and a
        # wrapped definition are both handled.
        src = (Path(__file__).resolve().parent / module).read_text().splitlines()
        start = next(i for i, l in enumerate(src) if l.startswith(f"{name} ="))
        block: list[str] = []
        for line in src[start:]:
            block.append(line.strip())
            joined = "".join(block)
            if joined.count("(") == joined.count(")"):
                break
        return " ".join(block)

    def test_config_path_matches_agent_common_server(self):
        # CONFIG_PATH is deliberately repeated rather than imported — importing
        # agent_common_server for it would pull FastMCP in. The copy is the cheap half
        # of that trade; this is the other half, so the two cannot silently
        # desynchronise.
        self.assertEqual(self._definition("chat_platforms.py", "CONFIG_PATH"),
                         self._definition("agent_common_server.py", "CONFIG_PATH"))

    def test_managed_config_path_matches_session_kv_server(self):
        # The same trade, for the constant #1111 added. These two modules resolve the
        # managed scope independently and must agree on where it is: a divergence
        # would put the cron report relay and the reconcile summary on different
        # answers to the same question, which is the whole failure this module is
        # named for.
        self.assertEqual(self._definition("chat_platforms.py", "MANAGED_CONFIG_PATH"),
                         self._definition("session_kv_server.py", "MANAGED_CONFIG_PATH"))


class ImportCostTest(unittest.TestCase):
    def test_importing_does_not_pull_in_the_mcp_stack(self):
        # The reason this module exists rather than a helper on
        # agent_common_server: a `no_agent` cron script must not pay for a
        # FastMCP import to find out where to send one message.
        #
        # In a subprocess, not against this interpreter's sys.modules — under
        # `unittest discover` a sibling test module has already imported FastMCP
        # long before this runs, so the in-process form asserts nothing.
        probe = (
            "import sys; import chat_platforms; "
            "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
        )
        res = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(res.stdout.strip(), "False", res.stderr)


if __name__ == "__main__":
    unittest.main()
