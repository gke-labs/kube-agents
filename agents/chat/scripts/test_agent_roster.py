"""Tests for the shared specialist roster.

This logic used to live in ``router_server.py`` and be reachable only through
the ``list_agents`` MCP tool. It moved here when the roster started being
injected into every turn as well, so both consumers describe the same fleet in
the same words; the tests moved with it. ``test_router_server.py`` now only
checks that the tool still delegates here.
"""

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

# Add the directory containing agent_roster.py to sys.path so it can be imported.
_scripts_dir = str(Path(__file__).parent.absolute())
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
if "agent_roster" in sys.modules and getattr(sys.modules["agent_roster"], "__file__", "") != str(Path(__file__).parent / "agent_roster.py"):
    del sys.modules["agent_roster"]

import agent_roster  # noqa: E402


class TestDiscovery(unittest.TestCase):
    """The roster enumerates every profile except the front door itself."""

    def _with_profiles(self, tmp, names):
        base = Path(tmp) / "profiles"
        for name in names:
            (base / name).mkdir(parents=True)
        agent_roster.PROFILES_BASE = base
        return base

    def test_excludes_default_and_lists_specialists(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["default", "platform", "cluster-a"])
            # A CAPABILITIES.md is the preferred description source.
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            # SOUL.md is the fallback when no CAPABILITIES.md exists.
            (base / "cluster-a" / "SOUL.md").write_text("# Title\n\nRead-only cluster diagnostics.\n")

            out = agent_roster.render()
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- cluster-a: Read-only cluster diagnostics.", out)
            self.assertNotIn("default", out)

    def test_empty_when_no_specialists(self):
        with TemporaryDirectory() as tmp:
            self._with_profiles(tmp, ["default"])
            self.assertIn("No specialist agents", agent_roster.render())

    def test_an_explicit_base_overrides_the_module_default(self):
        # The injecting plugin passes its own resolved path rather than relying
        # on the module picking up the right HERMES_HOME at import time.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "elsewhere"
            (base / "platform").mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet work.")
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("- platform: Fleet work.", agent_roster.render(base))


class TestSharedRoleGrouping(unittest.TestCase):
    """Agents with an identical description are stated once, not repeated per agent.

    Every Cluster Agent is scaffolded from the same template, so a fleet of N
    clusters otherwise repeats one CAPABILITIES.md verbatim N times — the bulk of
    what the front door reads on every single delegation.
    """

    # Sized like the real Cluster Agent CAPABILITIES.md (~885 bytes), because the
    # win depends on it: the grouped form costs a fixed ~76-char preamble, so for a
    # short description and a small fleet it is actually *longer* than one line per
    # agent. Break-even is roughly (N-1) x len(desc) > 76.
    FLEET = ("Read-only diagnostics for one GKE cluster. "
             + "Scoped to a single cluster; no write paths. " * 19).strip()

    def _fleet(self, tmp):
        base = Path(tmp) / "profiles"
        for name in ("default", "platform", "cluster-a", "cluster-b", "cluster-c"):
            (base / name).mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
        for name in ("cluster-a", "cluster-b", "cluster-c"):
            (base / name / "CAPABILITIES.md").write_text(self.FLEET)
        agent_roster.PROFILES_BASE = base

    def test_shared_description_stated_once(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            out = agent_roster.render()

            # The expensive part — the repeated blob — appears exactly once...
            self.assertEqual(out.count(self.FLEET), 1)
            # ...while every cluster is still individually addressable as an assignee.
            for name in ("cluster-a", "cluster-b", "cluster-c"):
                self.assertIn(f"  - {name}", out)

    def test_unique_specialist_kept_inline_and_ordered_first(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            out = agent_roster.render()

            # A one-off specialist stays on a single `- name: desc` line.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            # Distinct specialists sort ahead of shared-role fleets: the front door
            # routes to a named specialist far more often than to a given cluster.
            self.assertLess(out.index("- platform:"), out.index("share one role"))

    def test_grouping_shrinks_the_roster(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            grouped = agent_roster.render()

        # Compare against what the un-grouped one-line-per-agent form would cost.
        ungrouped = "\n".join(
            ["- platform: Fleet + GitOps write path."]
            + [f"- {n}: {self.FLEET}" for n in ("cluster-a", "cluster-b", "cluster-c")]
        )
        self.assertLess(len(grouped), len(ungrouped))

    def test_undescribed_agents_are_not_a_shared_role(self):
        # Two profiles nothing is known about have no role in common. Grouping on
        # the placeholder would file them under "pick the one whose cluster you
        # need" — a claim about interchangeability the roster has no basis for.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "mystery-a", "mystery-b"):
                (base / name).mkdir(parents=True)
            agent_roster.PROFILES_BASE = base

            out = agent_roster.render()

            self.assertIn("- mystery-a: (no description provided)", out)
            self.assertIn("- mystery-b: (no description provided)", out)
            self.assertNotIn("share one role", out)


@unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits these tests rely on")
class TestDiscoveryDegradesOnIOError(unittest.TestCase):
    """Discovery must degrade, never raise.

    It backs both the Chat Agent's routing tool and the block injected into
    every turn, so a raise here is a front door that cannot route at all.
    pathlib swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so `is_dir()`/`is_file()`/
    `iterdir()` on the shared PVC raise PermissionError (EACCES) for real.
    """

    @contextmanager
    def _locked(self, path):
        """Make `path` unreadable, restoring the mode so TemporaryDirectory can clean up."""
        original = path.stat().st_mode
        path.chmod(0o000)
        try:
            yield
        finally:
            path.chmod(original)

    def test_unreadable_profile_costs_only_that_agent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "platform", "broken"):
                (base / name).mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            agent_roster.PROFILES_BASE = base

            with self._locked(base / "broken"):
                out = agent_roster.render()

            # The healthy specialist still routes; the unreadable one is listed
            # without a description rather than taking down the whole roster.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- broken: (no description provided)", out)

    def test_unreadable_profiles_base_is_unknown_not_empty(self):
        # "I could not read the fleet" must never be rendered as "there is no
        # fleet": the front door would stop routing and tell the user there is
        # nobody to route to. render() returns None so the injecting plugin says
        # nothing and the model falls back to `list_agents`.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            (base / "platform").mkdir(parents=True)
            agent_roster.PROFILES_BASE = base

            with self._locked(base):
                self.assertIsNone(agent_roster.render())

    def test_missing_profiles_base_returns_empty_roster(self):
        # A directory that is absent, unlike one that cannot be read, is a fact:
        # pathlib's is_dir() swallows ENOENT, so this is a genuinely empty fleet.
        with TemporaryDirectory() as tmp:
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("No specialist agents", agent_roster.render())


class TestHostAndFleetScopeRendering(unittest.TestCase):
    """The roster renders Host Cluster Identity and Fleet Clusters & GCP Projects in Scope."""

    def _setup_three_cluster_fleet(self, tmp: str) -> Path:
        root = Path(tmp)
        base = root / "profiles"
        for name in (
            "default",
            "platform",
            "cluster-gca-gke-2025-kcc-gitops-hub-us-east1-c",
            "cluster-gca-gke-2025-openrl-gpu-hub-us-east1-c",
            "cluster-kube-agents-demo-platform-agent-host-us-central1",
        ):
            (base / name).mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
        cluster_cap = "Read-only diagnostics for one GKE cluster."
        identities = {
            "cluster-gca-gke-2025-kcc-gitops-hub-us-east1-c": (
                "gca-gke-2025",
                "kcc-gitops-hub",
                "us-east1-c",
            ),
            "cluster-gca-gke-2025-openrl-gpu-hub-us-east1-c": (
                "gca-gke-2025",
                "openrl-gpu-hub",
                "us-east1-c",
            ),
            "cluster-kube-agents-demo-platform-agent-host-us-central1": (
                "kube-agents-demo",
                "platform-agent-host",
                "us-central1",
            ),
        }
        for prof, (proj, clust, loc) in identities.items():
            (base / prof / "CAPABILITIES.md").write_text(cluster_cap)
            (base / prof / "config.yaml").write_text(
                f"cluster_identity:\n  cluster: {clust}\n  location: {loc}\n  project: {proj}\n"
            )
        agent_roster.PROFILES_BASE = base
        return base

    def _render(self, base: Path, env: dict[str, str]) -> str:
        # clear=True: a developer shell that exported GCP_PROJECT_ID or
        # KUBEAGENTS_SCOPE_FILE (as the eval setup instructions say to) must not
        # leak into the rendered block.
        from unittest import mock

        env = {"KUBEAGENTS_SCOPE_FILE": str(base.parent / "absent.json"), **env}
        with mock.patch.dict(os.environ, env, clear=True):
            out = agent_roster.render(base)
        self.assertIsNotNone(out)
        return out

    def test_renders_host_identity_and_fleet_scope_from_env_and_scope_files(self):
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            scope_file = Path(tmp) / "scope.json"
            scope_file.write_text('{"present": true, "projects": ["gca-gke-2025"]}\n')
            out = self._render(
                base,
                {
                    "GKE_CLUSTER_NAME": "platform-agent-host",
                    "GKE_PROJECT_ID": "kube-agents-demo",
                    "GKE_LOCATION": "us-central1",
                    "KUBEAGENTS_SCOPE_FILE": str(scope_file),
                },
            )

        self.assertIn(
            'Host Cluster Identity ("this cluster" / "this host cluster"):\n'
            "- cluster=platform-agent-host, project=kube-agents-demo, "
            "location=us-central1, "
            "assignee=cluster-kube-agents-demo-platform-agent-host-us-central1",
            out,
        )
        self.assertIn(
            "- GCP projects in scope (2): gca-gke-2025, kube-agents-demo",
            out,
        )
        self.assertIn(
            "- Cluster agents and the GKE cluster each targets (3; not a live read of GKE):",
            out,
        )
        self.assertIn(
            "  - kcc-gitops-hub (project=gca-gke-2025, location=us-east1-c, "
            "assignee=cluster-gca-gke-2025-kcc-gitops-hub-us-east1-c)",
            out,
        )
        self.assertIn(
            "  - openrl-gpu-hub (project=gca-gke-2025, location=us-east1-c, "
            "assignee=cluster-gca-gke-2025-openrl-gpu-hub-us-east1-c)",
            out,
        )
        self.assertIn(
            "  - platform-agent-host (project=kube-agents-demo, location=us-central1, "
            "assignee=cluster-kube-agents-demo-platform-agent-host-us-central1, host cluster)",
            out,
        )
        self.assertNotIn("default", out)

    def test_snapshot_lists_only_in_scope_readable_projects(self):
        # The vocabulary cluster_agent_reconcile.py writes: a retiring row, a
        # denied row, an exclusion recorded under excludedProjects and in the
        # pre-exclude `declared` render, and the management project carried
        # with an unreachable outcome. A profile left behind in the retiring
        # project and a render naming another project must not re-add either.
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            leftover = base / "cluster-retired-proj-old-us-east1-b"
            leftover.mkdir()
            (leftover / "config.yaml").write_text(
                "cluster_identity:\n  cluster: old\n  location: us-east1-b\n  project: retired-proj\n"
            )
            (Path(tmp) / "fleet_scope.json").write_text(
                '{"declared": {"projects": ["gca-gke-2025", "sandbox-scratch"]}, '
                '"excludedProjects": ["sandbox-scratch"], "projects": ['
                '{"id": "gca-gke-2025", "state": "in-scope", "outcome": "ok", "via": ["explicit"]}, '
                '{"id": "kube-agents-demo", "state": "in-scope", "outcome": "unreachable", "via": ["management"]}, '
                '{"id": "retired-proj", "state": "retiring", "outcome": "ok", "via": []}, '
                '{"id": "denied-proj", "state": "in-scope", "outcome": "denied", "via": ["explicit"]}'
                "]}\n"
            )
            scope_file = Path(tmp) / "scope.json"
            scope_file.write_text('{"present": true, "projects": ["render-only"]}\n')
            out = self._render(base, {"KUBEAGENTS_SCOPE_FILE": str(scope_file)})

        self.assertIn(
            "- GCP projects in scope (3): denied-proj (denied), gca-gke-2025, kube-agents-demo\n",
            out,
        )
        for absent in ("sandbox-scratch", "retired-proj,", "render-only"):
            self.assertNotIn(absent, out.split("- Cluster agents")[0])

    def test_render_drops_projects_matching_exclude_globs(self):
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            scope_file = Path(tmp) / "scope.json"
            scope_file.write_text(
                '{"present": true, "projects": ["gca-gke-2025", "sandbox-scratch"], '
                '"exclude": {"projects": ["sandbox-*"], "clusters": []}}\n'
            )
            out = self._render(base, {"KUBEAGENTS_SCOPE_FILE": str(scope_file)})

        self.assertIn("- GCP projects in scope (2): gca-gke-2025, kube-agents-demo", out)
        self.assertNotIn("sandbox-scratch", out)

    def test_a_numeric_host_project_is_not_listed_beside_its_id(self):
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            (Path(tmp) / "fleet_scope.json").write_text(
                '{"projects": [{"id": "kube-agents-demo", "state": "in-scope", '
                '"outcome": "ok", "via": ["management"]}]}\n'
            )
            out = self._render(
                base,
                {
                    "GKE_CLUSTER_NAME": "platform-agent-host",
                    "GKE_PROJECT_ID": "123456789012",
                    "GKE_LOCATION": "us-central1",
                },
            )

        self.assertIn("- GCP projects in scope (1): kube-agents-demo\n", out)

    def test_a_partial_host_identity_elects_no_host_cluster(self):
        # Two cluster agents share gca-gke-2025; a project alone must not pick one.
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            out = self._render(base, {"GKE_PROJECT_ID": "gca-gke-2025"})

        self.assertNotIn("Host Cluster Identity", out)
        self.assertNotIn(", host cluster)", out)

    def test_a_host_with_no_matching_profile_advertises_no_assignee(self):
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            out = self._render(
                base,
                {
                    "GKE_CLUSTER_NAME": "fresh-host",
                    "GKE_PROJECT_ID": "kube-agents-demo",
                    "GKE_LOCATION": "us-central1",
                },
            )

        self.assertIn(
            'Host Cluster Identity ("this cluster" / "this host cluster"):\n'
            "- cluster=fresh-host, project=kube-agents-demo, location=us-central1\n",
            out,
        )
        self.assertNotIn("fresh-host-us-central1", out)
        self.assertNotIn(", host cluster)", out)

    def test_a_host_profile_without_cluster_identity_matches_by_its_derived_name(self):
        with TemporaryDirectory() as tmp:
            base = self._setup_three_cluster_fleet(tmp)
            (base / "cluster-kube-agents-demo-fresh-host-us-central1").mkdir()
            out = self._render(
                base,
                {
                    "GKE_CLUSTER_NAME": "fresh-host",
                    "GKE_PROJECT_ID": "kube-agents-demo",
                    "GKE_LOCATION": "us-central1",
                },
            )

        self.assertIn("assignee=cluster-kube-agents-demo-fresh-host-us-central1", out)

    def test_stdlib_cluster_identity_parser_fallback_and_malformed_config(self):
        from unittest import mock

        text = (
            "# comment\n"
            "agent:\n"
            "  environment_probe: false\n"
            "cluster_identity:\n"
            "  cluster: 'kcc-gitops-hub'\n"
            '  location: "us-east1-c"\n'
            "  project: gca-gke-2025\n"
            "context_file_max_chars: 100000\n"
        )
        with mock.patch.dict(sys.modules, {"yaml": None}):
            parsed = agent_roster._parse_cluster_identity_text(text)
        self.assertEqual(
            parsed,
            {
                "project": "gca-gke-2025",
                "cluster": "kcc-gitops-hub",
                "location": "us-east1-c",
            },
        )
        self.assertIsNone(agent_roster._parse_cluster_identity_text("not: [valid: yaml"))
        self.assertIsNone(
            agent_roster._parse_cluster_identity_text("cluster_identity:\n  cluster: only-one\n")
        )


class TestChatSoulHostAndFleetRules(unittest.TestCase):
    """agents/chat/SOUL.md instructs direct static answers and host cluster disambiguation."""

    def test_soul_contains_static_fleet_and_host_routing_rules(self):
        soul_path = Path(__file__).resolve().parents[1] / "SOUL.md"
        text = soul_path.read_text(encoding="utf-8")
        self.assertIn("## 1.4 Static Identity, Fleet Scope & Host Cluster Resolution", text)
        self.assertIn("Host Cluster Identity", text)
        self.assertIn("Fleet Clusters & GCP Projects in Scope", text)
        self.assertIn("without calling `kanban_create`", text)
        self.assertIn("never ask which cluster they mean", text)
        # Cluster inventory stays a kanban lookup (bench/tasks/agent-kanban-smoke).
        self.assertIn("Which GKE clusters are running, how many there are, and what versions they run are **not** static", text)


if __name__ == "__main__":
    unittest.main()
