#!/usr/bin/env python3
"""Which forges an install builds, from the configuration the operator mounts.

    python3 -m pytest -q agents/platform/scripts/test_providers_registry_config.py

Three rules are pinned here, each a place a credential could otherwise be
presented on a guess: with no configuration the install is what it always was;
a host belongs to exactly one forge; and a repository named without a host is
refused once more than one forge could own it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import providers
from providers import registry as registry_module
from providers.base import Forge, ForgeUnsupported

# Through the package surface, as the boundary test requires of everything
# outside `providers/`.
GitHubForge = next(cls for cls in providers.AVAILABLE if cls.name == "github")


class _TestForge(Forge):
    """A second forge, configured per host the way a self-hosted one is."""

    name = "testforge"
    transport = "http"

    def __init__(self, host):
        super().__init__()
        self.hosts = (host,)

    @classmethod
    def for_config(cls, config):
        return tuple(
            cls(entry["host"])
            for entry in config.get("forges") or ()
            if entry.get("provider") == cls.name
        )

    def parse(self, url):
        return "/".join(providers.registry.repo_ref.parse(url).segments[-2:])


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(registry_module.FORGES_CONFIG_ENV, None)
        os.environ.pop(registry_module.FORGES_INLINE_ENV, None)

    def configure(self, document):
        path = self.dir / "forges.json"
        path.write_text(document if isinstance(document, str) else json.dumps(document))
        os.environ[registry_module.FORGES_CONFIG_ENV] = str(path)
        return path


class NoConfigurationTest(_ConfigCase):
    def test_an_install_with_no_forge_configuration_is_github_as_always(self):
        registry = providers.Registry()
        self.assertEqual(["github"], [forge.name for forge in registry.forges])
        self.assertIs(registry.default, registry.forges[0])
        forge, repo = registry.resolve("acme/infra")
        self.assertEqual(("github", "acme/infra"), (forge.name, repo))

    def test_the_contract_suites_empty_configuration_still_builds_github(self):
        self.assertEqual(1, len(tuple(GitHubForge.for_config({}))))


class ConfigurationFileTest(_ConfigCase):
    def test_entries_are_normalised(self):
        # The provider and host are normalised here; `allowedPaths` is handed
        # to the forge as written, for it to trim and to refuse what it must.
        self.configure(
            {"forges": [{"provider": "TestForge", "host": "Git.Example.com", "allowedPaths": ["/acme/"]}]}
        )
        self.assertEqual(
            [{"provider": "testforge", "host": "git.example.com", "token_path": "", "allowed_paths": ("/acme/",)}],
            registry_module.load_forge_entries(),
        )

    def test_github_refuses_allowed_paths_rather_than_ignoring_them(self):
        # Review round 2: accepted and never enforced, it read as narrowing
        # the installation token beside a forge where it does.
        self.configure({"forges": [{"provider": "github", "host": "github.com", "allowedPaths": ["acme"]}]})
        with self.assertRaises(ValueError) as caught:
            providers.Registry()
        self.assertIn("not supported for github", str(caught.exception))

    def test_a_configuration_that_lists_github_builds_it(self):
        self.configure({"forges": [{"provider": "github", "host": "github.com"}]})
        self.assertEqual(["github"], [f.name for f in providers.Registry().forges])

    def test_a_configuration_that_leaves_github_out_does_not_build_it(self):
        # A GitLab-only install has no GitHub to hand a bare name to.
        self.configure({"forges": []})
        registry = providers.Registry()
        self.assertEqual((), registry.forges)
        self.assertIsNone(registry.default)
        with self.assertRaises(ForgeUnsupported) as caught:
            registry.resolve("acme/infra")
        # Review round 2: the placeholders for unconfigured forges were
        # listed as configured, pointing the operator at the wrong thing.
        self.assertIn("Configured: none", str(caught.exception))

    def test_absent_allowed_paths_are_kept_apart_from_an_empty_list(self):
        self.configure({"forges": [
            {"provider": "github", "host": "github.com"},
            {"provider": "testforge", "host": "a.example.com", "allowedPaths": []},
        ]})
        entries = registry_module.load_forge_entries()
        self.assertEqual([None, ()], [e["allowed_paths"] for e in entries])

    def test_an_enterprise_host_is_refused_until_it_is_served(self):
        self.configure({"forges": [{"provider": "github", "host": "github.example.com"}]})
        with self.assertRaises(ValueError) as caught:
            providers.Registry()
        self.assertIn("github.example.com", str(caught.exception))

    def test_a_named_file_that_cannot_be_read_stops_the_build(self):
        for document in ("{not json", json.dumps({"forges": "github"}), json.dumps([1])):
            with self.subTest(document=document):
                self.configure(document)
                with self.assertRaises(ValueError):
                    providers.Registry()
        os.environ[registry_module.FORGES_CONFIG_ENV] = str(self.dir / "absent.json")
        with self.assertRaises(ValueError):
            providers.Registry()

    def test_an_entry_needs_a_provider_and_a_hostname(self):
        for entry in (
            {"host": "gitlab.example.com"},
            {"provider": "testforge", "host": "https://gitlab.example.com"},
            # Review finding: a port loaded and then matched no request,
            # because resolution reads a URL's host without its port.
            {"provider": "testforge", "host": "gitlab.example.com:8443"},
            {"provider": "testforge", "host": "gitlab.example.com", "allowedPaths": "acme"},
        ):
            with self.subTest(entry=entry):
                self.configure({"forges": [entry]})
                with self.assertRaises(ValueError):
                    registry_module.load_forge_entries()


class UnclaimedProviderTest(_ConfigCase):
    def test_a_provider_no_forge_class_serves_stops_the_build(self):
        # Review finding: a misspelt provider, or one this image predates,
        # built a broker with no forges that refused everything at runtime.
        for provider in ("githib", "gitlab", "gitea"):
            with self.subTest(provider=provider):
                self.configure({"forges": [{"provider": provider, "host": "git.example.test"}]})
                with self.assertRaises(ValueError) as caught:
                    providers.Registry()
                self.assertIn(provider, str(caught.exception))

    def test_inline_forges_env_builds_gitea_with_scheme_and_port(self):
        os.environ[registry_module.FORGES_INLINE_ENV] = json.dumps([
            {
                "name": "gitea",
                "provider": "gitea",
                "host": "gitea.example.test",
                "scheme": "http",
                "port": 3000,
                "tokenFile": "/var/run/secrets/kubeagents/forges/gitea/token",
            }
        ])
        registry = providers.Registry()
        self.assertEqual(["gitea"], [f.name for f in registry.forges])
        self.assertIs(registry.default, registry.forges[0])
        forge, repo = registry.resolve("acme/infra")
        self.assertEqual(("gitea", "acme/infra"), (forge.name, repo))
        self.assertEqual("http://gitea.example.test:3000/acme/infra.git", forge.clone_url(repo))


class TwoForgesTest(_ConfigCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(registry_module, "AVAILABLE", (GitHubForge, _TestForge))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_bare_name_is_refused_once_two_forges_could_own_it(self):
        self.configure(
            {"forges": [
                {"provider": "github", "host": "github.com"},
                {"provider": "testforge", "host": "git.example.test"},
            ]}
        )
        registry = providers.Registry()
        self.assertIsNone(registry.default)
        with self.assertRaises(ForgeUnsupported) as caught:
            registry.resolve("acme/infra")
        self.assertIn("names no host", str(caught.exception))

    def test_the_named_host_decides_the_forge(self):
        self.configure(
            {"forges": [
                {"provider": "github", "host": "github.com"},
                {"provider": "testforge", "host": "git.example.test"},
            ]}
        )
        registry = providers.Registry()
        for spec, name in (
            ("https://github.com/acme/infra", "github"),
            ("github.com/acme/infra", "github"),
            ("https://git.example.test/acme/infra", "testforge"),
            ("git.example.test/acme/infra", "testforge"),
        ):
            with self.subTest(spec=spec):
                self.assertEqual(name, registry.resolve(spec)[0].name)

    def test_one_configured_forge_keeps_the_bare_name(self):
        self.configure({"forges": [{"provider": "testforge", "host": "git.example.test"}]})
        registry = providers.Registry()
        forge, _ = registry.resolve("acme/infra")
        self.assertEqual("testforge", forge.name)

    def test_a_host_two_entries_claim_is_refused(self):
        self.configure(
            {"forges": [
                {"provider": "testforge", "host": "git.example.test"},
                {"provider": "testforge", "host": "git.example.test"},
            ]}
        )
        with self.assertRaises(ValueError) as caught:
            providers.Registry()
        self.assertIn("git.example.test", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
