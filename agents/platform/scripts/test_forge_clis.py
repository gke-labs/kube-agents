#!/usr/bin/env python3
"""Tests for `forge_clis` — which tools an install may run, given its forges."""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_clis  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GITPROVIDER_GO = REPOSITORY_ROOT / "k8s-operator/api/v1alpha1/gitprovider.go"


class ConfiguredProvidersTest(unittest.TestCase):
    def test_nothing_configured_means_github(self):
        """The variable is new, so most sidecars running this code will not have
        it set. Rendering nothing would strip `gh` from every install that
        predates it."""
        self.assertEqual((forge_clis.GITHUB,), forge_clis.configured_providers(""))

    def test_the_environment_is_the_default_source(self):
        with mock.patch.dict(
            os.environ, {forge_clis.FORGE_PROVIDERS_ENV: "github"}, clear=False
        ):
            self.assertEqual((forge_clis.GITHUB,), forge_clis.configured_providers())

    def test_a_list_is_split_lowercased_and_deduplicated(self):
        self.assertEqual(
            ("github", "gitlab"),
            forge_clis.configured_providers(" GitHub , gitlab ,github, "),
        )


class AllowedExecutablesTest(unittest.TestCase):
    def test_the_base_tools_are_always_present(self):
        for configured in ("", "github", "gitlab", "nonesuch"):
            with self.subTest(configured=configured):
                allowed = forge_clis.allowed_executables(configured)
                for tool in forge_clis.BASE_EXECUTABLES:
                    self.assertIn(tool, allowed)

    def test_a_github_install_gets_gh(self):
        self.assertIn("gh", forge_clis.allowed_executables("github"))

    def test_an_install_that_does_not_serve_github_does_not_get_gh(self):
        """The point of deriving the list rather than taking the union: an
        allowlist holding every tool the harness could ever use grants every
        install more than it uses."""
        self.assertNotIn("gh", forge_clis.allowed_executables("gitlab"))

    def test_an_unknown_provider_contributes_no_binary_and_raises_nothing(self):
        """Either a forge with no CLI or a provider newer than this image.
        Refusing to start would take down an install over a provider it may not
        even use; the allowlist refuses the tool later, by name, where an
        operator can read it."""
        self.assertEqual(
            forge_clis.BASE_EXECUTABLES, forge_clis.allowed_executables("nonesuch")
        )

    def test_the_environment_cannot_introduce_an_executable(self):
        """The security property this module exists for. `FORGE_EXECUTABLES` is
        compiled in and the variable selects from it, so anything able to set one
        variable on the sidecar still cannot name a binary to run."""
        known = set(forge_clis.BASE_EXECUTABLES) | set(
            forge_clis.FORGE_EXECUTABLES.values()
        )
        for hostile in ("sh", "curl", "gh,sh", "/bin/sh", "python3"):
            with self.subTest(hostile=hostile):
                self.assertTrue(
                    set(forge_clis.allowed_executables(hostile)) <= known,
                    "an environment value named a binary the table does not hold",
                )


class TheTwoSidesAgreeTest(unittest.TestCase):
    """`credential_proxy` enforces this list and `credential_proxy_client` offers
    it, from opposite sides of the credential boundary. A shim narrower than the
    enforcer refuses a tool the install is entitled to; a wider one turns a clear
    local refusal into a confusing remote one."""

    def test_the_shim_offers_exactly_what_the_executor_allows(self):
        import credential_proxy
        import credential_proxy_client

        self.assertEqual(
            tuple(credential_proxy_client.SUPPORTED_EXECUTABLES),
            tuple(credential_proxy.CommandExecutor(60, 1024, "/tmp").ALLOWED_EXECUTABLES),
        )


class TheOperatorAndTheSidecarNameTheSameToolsTest(unittest.TestCase):
    """`GitProvider.CLI` and `FORGE_EXECUTABLES` are the same mapping in two
    languages, and until this test a comment in each file was the only thing
    keeping them in step.

    Getting it wrong is quiet. The operator renders a provider *name*, and
    `forge_executables` drops a name it does not know without raising — which is
    right for a forge that has no CLI, and indistinguishable from a forge whose
    CLI someone added to the Go registry alone. That install starts, runs, and
    refuses the tool it was provisioned for.

    Reading the Go source rather than running the operator, for the same reason
    `TestFQDNPatternList_MatchesKustomizeManifest` reads the kustomize YAML: the
    two artefacts ship separately and the check has to span them.
    """

    @staticmethod
    def _go_string_constants(source):
        """Every `Name = "value"` in the file, resolved through one indirection.

        `CLI: GitHubCLI` names a constant, not a literal, so the registry cannot
        be read without them.
        """
        constants = dict(re.findall(r'^\t(\w+)\s*=\s*"([^"]*)"$', source, re.M))
        for name, value in re.findall(r"^\t(\w+)\s*=\s*(\w+)$", source, re.M):
            if value in constants:
                constants[name] = constants[value]
        return constants

    def _go_registry(self):
        source = GITPROVIDER_GO.read_text()
        constants = self._go_string_constants(source)
        body = re.search(
            r"var gitProviders = map\[string\]\*GitProvider\{\n(.*?)\n\}\n",
            source,
            re.S,
        )
        self.assertIsNotNone(body, f"could not find the registry in {GITPROVIDER_GO}")

        def resolve(token):
            return constants.get(token, token.strip('"'))

        # The key may be a constant or a quoted literal, and an entry this
        # pattern misses is skipped in silence — which is the failure this whole
        # test exists to stop, so the count is checked against the opening
        # braces below rather than trusted.
        entries = re.findall(r"^\t[\w\"]+: \{\n(.*?)^\t\},$", body.group(1), re.S | re.M)
        self.assertEqual(
            len(re.findall(r"^\t\S+: \{$", body.group(1), re.M)),
            len(entries),
            "an entry in the registry was not parsed; widen the pattern",
        )

        registry = {}
        for entry in entries:
            name = re.search(r"^\t\tName:\s+(\S+),$", entry, re.M)
            self.assertIsNotNone(name, f"registry entry has no Name:\n{entry}")
            cli = re.search(r"^\t\tCLI:\s+(\S+),$", entry, re.M)
            registry[resolve(name.group(1))] = resolve(cli.group(1)) if cli else ""

        self.assertTrue(registry, f"parsed no providers out of {GITPROVIDER_GO}")
        return registry

    def test_every_provider_the_operator_knows_agrees_with_this_table(self):
        for name, cli in self._go_registry().items():
            with self.subTest(provider=name):
                self.assertEqual(
                    cli,
                    forge_clis.FORGE_EXECUTABLES.get(name, ""),
                    f"the operator and the sidecar disagree about {name}'s CLI",
                )

    def test_this_table_names_no_provider_the_operator_will_not_render(self):
        """The other direction. A tool here for a provider the operator cannot be
        configured for is unreachable, and reads as though the forge is
        supported."""
        self.assertEqual(
            set(),
            set(forge_clis.FORGE_EXECUTABLES) - set(self._go_registry()),
        )


if __name__ == "__main__":
    unittest.main()
