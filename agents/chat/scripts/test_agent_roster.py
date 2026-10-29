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

    def test_excludes_dot_prefixed_archive_directories(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["platform", "tse"])
            (base / "platform" / "SOUL.md").write_text("# Platform Agent\n\nManages fleet GitOps.\n")
            (base / "tse" / "SOUL.md").write_text("# GKE Support Agent (TSE)\n\nTriages L1 GKE support incidents.\n")
            archived = base / ".deleted" / "tse-20261021T000000Z"
            archived.mkdir(parents=True)
            (archived / "SOUL.md").write_text("# Archived Profile\n\nShould not appear in roster.\n")

            discovered = agent_roster.discover(base)
            self.assertIsNotNone(discovered)
            self.assertEqual([entry["name"] for entry in discovered], ["platform", "tse"])

    def test_a_cluster_agent_shows_the_cluster_it_targets(self):
        # #3082: the assignee id is not a name to show a person, and the cluster
        # name cannot be read back out of it. The scaffold's cluster_identity
        # block (yaml.safe_dump, sort_keys=False) supplies it.
        with TemporaryDirectory() as tmp:
            name = "cluster-my-proj-ka-today-2-northamerica-northeast1"
            base = self._with_profiles(tmp, ["platform", name])
            (base / name / "CAPABILITIES.md").write_text("Read-only cluster diagnostics.")
            (base / name / "config.yaml").write_text(
                "model: x\ncluster_identity:\n  project: my-proj\n  cluster: ka-today-2\n"
                "  location: northamerica-northeast1\ncontext_file_max_chars: 100000\n"
            )
            out = agent_roster.render()
            self.assertIn(
                f"- {name} (the ka-today-2 cluster agent; project my-proj, location northamerica-northeast1): "
                "Read-only cluster diagnostics.",
                out,
            )

    def test_the_identity_note_rides_each_name_in_a_shared_role(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["cluster-a", "cluster-b"])
            for n, c in (("cluster-a", "alpha"), ("cluster-b", "beta")):
                (base / n / "CAPABILITIES.md").write_text("Shared template.")
                (base / n / "config.yaml").write_text(f"cluster_identity:\n  cluster: {c}\n")
            out = agent_roster.render()
            self.assertIn("  - cluster-a (the alpha cluster agent)\n", out)
            self.assertIn("  - cluster-b (the beta cluster agent)\n", out)

    def test_no_identity_no_note(self):
        # No config.yaml, no block, an empty cluster, or the block ending before
        # a cluster key: the line is as before.
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["p1", "p2", "p3", "p4"])
            (base / "p2" / "config.yaml").write_text("model: x\n")
            (base / "p3" / "config.yaml").write_text("cluster_identity:\n  cluster: ''\n")
            (base / "p4" / "config.yaml").write_text("cluster_identity:\n  project: x\nother: y\n  cluster: nope\n")
            for n in ("p1", "p2", "p3", "p4"):
                (base / n / "CAPABILITIES.md").write_text(f"role {n}")
            out = agent_roster.render()
            for n in ("p1", "p2", "p3", "p4"):
                self.assertIn(f"- {n}: role {n}", out)
            self.assertNotIn("cluster agent", out)

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


class TestHostAndScopeSections(unittest.TestCase):
    """render() appends Host Cluster Identity and GCP Projects in Scope (#3143, #3286)."""

    HOST_ENV = {
        "GKE_CLUSTER_NAME": "platform-agent-host",
        "GKE_PROJECT_ID": "kube-agents-demo",
        "GKE_LOCATION": "us-central1",
    }
    ENV_NAMES = (
        "GKE_CLUSTER_NAME", "GKE_PROJECT_ID", "GKE_LOCATION", "GCP_PROJECT_ID",
        "KUBEAGENTS_SCOPE_FILE", "PLATFORM_AGENT_HOME",
    )

    def _fleet(self, tmp: str) -> Path:
        base = Path(tmp) / "profiles"
        (base / "platform").mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet work.\n")
        clusters = {
            "cluster-kube-agents-demo-platform-agent-host-us-central1": (
                "kube-agents-demo", "platform-agent-host", "us-central1"),
            "cluster-gca-gke-2025-kcc-gitops-hub-us-east1-c": (
                "gca-gke-2025", "kcc-gitops-hub", "us-east1-c"),
        }
        for name, (project, cluster, location) in clusters.items():
            (base / name).mkdir()
            (base / name / "CAPABILITIES.md").write_text("One cluster's runtime state.\n")
            (base / name / "config.yaml").write_text(
                "cluster_identity:\n"
                f"  cluster: {cluster}\n  location: {location}\n  project: {project}\n"
            )
        return base

    def _render(self, base: Path, env: dict[str, str]) -> str:
        from unittest import mock

        clean = {k: v for k, v in os.environ.items() if k not in self.ENV_NAMES}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return agent_roster.render(base)

    def _snapshot(self, tmp: str, payload: dict) -> None:
        import json

        (Path(tmp) / "fleet_scope.json").write_text(json.dumps(payload))

    def test_host_identity_names_the_matching_cluster_agent(self):
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp), self.HOST_ENV)
        self.assertIn(
            agent_roster.HOST_IDENTITY_HEADING + "\n"
            "- cluster=platform-agent-host, project=kube-agents-demo, location=us-central1, "
            "assignee=cluster-kube-agents-demo-platform-agent-host-us-central1",
            out,
        )

    def test_no_cluster_name_no_host_section(self):
        # Two cluster agents; a project alone must not pick one.
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp), {"GKE_PROJECT_ID": "gca-gke-2025"})
        self.assertNotIn("Host Cluster Identity", out)

    def test_an_unexpanded_placeholder_is_unset(self):
        # Hermes hands the router MCP child "${VAR}" for an unset variable (a kind install).
        placeholders = {k: "${" + k + "}" for k in self.ENV_NAMES}
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp), placeholders)
        self.assertNotIn("Host Cluster Identity", out)
        self.assertNotIn("${", out)
        self.assertIn(agent_roster.SCOPE_UNDECLARED, out)

    def test_a_host_with_no_matching_profile_advertises_no_assignee(self):
        env = dict(self.HOST_ENV, GKE_CLUSTER_NAME="fresh-host")
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp), env)
        self.assertIn("- cluster=fresh-host, project=kube-agents-demo, location=us-central1\n", out)
        self.assertNotIn("assignee=", out)

    def test_a_host_profile_without_identity_matches_by_its_derived_name(self):
        env = dict(self.HOST_ENV, GKE_CLUSTER_NAME="fresh-host")
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            (base / "cluster-kube-agents-demo-fresh-host-us-central1").mkdir()
            out = self._render(base, env)
        self.assertIn("assignee=cluster-kube-agents-demo-fresh-host-us-central1", out)

    def test_derived_name_fallback_rejects_colliding_profile_with_different_identity(self):
        # _profile_name is not injective on interior hyphens: ("a-b", "c", loc) and
        # ("a", "b-c", loc) both derive cluster-a-b-c-<loc>. When the colliding
        # profile carries a stamped cluster_identity for the other cluster, the
        # host line must not advertise it as assignee.
        env = {
            "GKE_PROJECT_ID": "a-b",
            "GKE_CLUSTER_NAME": "c",
            "GKE_LOCATION": "us-central1",
        }
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            colliding = base / "cluster-a-b-c-us-central1"
            colliding.mkdir()
            (colliding / "CAPABILITIES.md").write_text("One cluster's runtime state.\n")
            (colliding / "config.yaml").write_text(
                "cluster_identity:\n  cluster: b-c\n  location: us-central1\n  project: a\n"
            )
            out = self._render(base, env)
        host_section = out.split(agent_roster.HOST_IDENTITY_HEADING)[1].split("\n\n")[0]
        self.assertIn("- cluster=c, project=a-b, location=us-central1", host_section)
        self.assertNotIn("assignee=", host_section)

    def test_numeric_host_project_matches_the_scaffolded_profile_and_adopts_its_id(self):
        # spec.harness.projectId accepts a project number; the reconcile resolves
        # it to the project ID in fleet_scope.json's management row before
        # scaffolding profiles, whether or not spec.scope is declared.
        env = dict(self.HOST_ENV, GKE_PROJECT_ID="123456789012")
        for boundary in (False, True):
            with self.subTest(boundary=boundary), TemporaryDirectory() as tmp:
                base = self._fleet(tmp)
                self._snapshot(tmp, {
                    "boundary": boundary, "present": boundary, "declared": {},
                    "projects": [
                        {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                    ],
                })
                out = self._render(base, env)
                self.assertIn(
                    agent_roster.HOST_IDENTITY_HEADING + "\n"
                    "- cluster=platform-agent-host, project=kube-agents-demo, location=us-central1, "
                    "assignee=cluster-kube-agents-demo-platform-agent-host-us-central1",
                    out,
                )
                self.assertNotIn("123456789012", out)

    def test_numeric_host_project_does_not_elect_another_projects_same_named_cluster(self):
        # When the host's own profile is absent (excluded in spec.scope.exclude.clusters
        # or not yet scaffolded) and another project has a same-named cluster in the
        # same location, numeric GKE_PROJECT_ID must never adopt that other project's
        # ID or assignee — neither before the snapshot exists nor after.
        env = {
            "GKE_CLUSTER_NAME": "prod",
            "GKE_PROJECT_ID": "123456789012",
            "GKE_LOCATION": "us-central1",
        }
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            other = base / "cluster-team-a-prod-us-central1"
            other.mkdir()
            (other / "CAPABILITIES.md").write_text("One cluster's runtime state.\n")
            (other / "config.yaml").write_text(
                "cluster_identity:\n  cluster: prod\n  location: us-central1\n  project: team-a\n"
            )
            before_snapshot = self._render(base, env)
            before_host = before_snapshot.split(agent_roster.HOST_IDENTITY_HEADING)[1].split("\n\n")[0]
            self.assertIn("- cluster=prod, location=us-central1", before_host)
            self.assertNotIn("project=", before_host)
            self.assertNotIn("assignee=", before_host)
            self.assertNotIn("team-a", before_host)

            self._snapshot(tmp, {
                "boundary": True, "present": True,
                "declared": {"projects": ["team-a"]},
                "projects": [
                    {"id": "host-proj", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                    {"id": "team-a", "state": "in-scope", "outcome": "ok", "via": ["projects"]},
                ],
            })
            after_snapshot = self._render(base, env)
            after_host = after_snapshot.split(agent_roster.HOST_IDENTITY_HEADING)[1].split("\n\n")[0]
            self.assertIn("- cluster=prod, project=host-proj, location=us-central1", after_host)
            self.assertNotIn("assignee=", after_host)
            self.assertNotIn("team-a", after_host)

    def test_numeric_host_project_without_a_matching_profile_omits_the_number(self):
        env = {
            "GKE_CLUSTER_NAME": "fresh-host",
            "GKE_PROJECT_ID": "123456789012",
            "GKE_LOCATION": "us-central1",
        }
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp), env)
        host_section = out.split(agent_roster.HOST_IDENTITY_HEADING)[1].split("\n\n")[0]
        self.assertIn("- cluster=fresh-host, location=us-central1", host_section)
        self.assertNotIn("123456789012", out)
        self.assertNotIn("project=", host_section)
        self.assertNotIn("assignee=", host_section)

    def test_numeric_host_project_with_ambiguous_cluster_location_omits_both(self):
        env = {
            "GKE_CLUSTER_NAME": "shared-cluster",
            "GKE_PROJECT_ID": "123456789012",
            "GKE_LOCATION": "us-central1",
        }
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            for proj in ("proj-a", "proj-b"):
                name = f"cluster-{proj}-shared-cluster-us-central1"
                (base / name).mkdir()
                (base / name / "CAPABILITIES.md").write_text("One cluster's runtime state.\n")
                (base / name / "config.yaml").write_text(
                    "cluster_identity:\n"
                    f"  cluster: shared-cluster\n  location: us-central1\n  project: {proj}\n"
                )
            out = self._render(base, env)
        host_section = out.split(agent_roster.HOST_IDENTITY_HEADING)[1].split("\n\n")[0]
        self.assertIn("- cluster=shared-cluster, location=us-central1", host_section)
        self.assertNotIn("123456789012", out)
        self.assertNotIn("project=", host_section)
        self.assertNotIn("assignee=", host_section)

    def test_profile_name_matches_the_scaffold(self):
        platform_scripts = str(Path(__file__).resolve().parents[2] / "platform" / "scripts")
        if platform_scripts not in sys.path:
            sys.path.append(platform_scripts)
        import cluster_agent_profile

        cases = [
            ("kube-agents-demo", "platform-agent-host", "us-central1"),
            ("Proj_1", "My Cluster", "us-east1-c"),
            ("p--x", "--c--", "-loc-"),
            ("p" * 40, "c" * 30, "northamerica-northeast1"),
            ("a" * 63, "b", "c"),
        ]
        for project, cluster, location in cases:
            self.assertEqual(
                agent_roster._profile_name(project, cluster, location),
                cluster_agent_profile.profile_name(project, cluster, location),
                (project, cluster, location),
            )
        self.assertEqual(agent_roster.PROFILE_NAME_MAX_LEN, cluster_agent_profile.MAX_NAME_LEN)

    def test_scope_from_the_snapshot_lists_unread_rows_with_their_outcome(self):
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            self._snapshot(tmp, {
                "boundary": True, "present": True,
                "declared": {"projects": ["gca-gke-2025", "team-b"]},
                "projects": [
                    {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                    {"id": "gca-gke-2025", "state": "in-scope", "outcome": "ok", "via": ["projects"]},
                    {"id": "team-b", "state": "in-scope", "outcome": "denied", "via": ["projects"]},
                    {"id": "old", "state": "retiring", "outcome": "ok", "via": ["projects"]},
                ],
            })
            out = self._render(base, self.HOST_ENV)
        self.assertIn(
            "GCP Projects in Scope:\n"
            "- GCP projects in scope (3): kube-agents-demo, gca-gke-2025, team-b (denied)",
            out,
        )
        self.assertNotIn("old", out.split("GCP Projects in Scope:")[1])

    def test_a_declared_project_with_no_row_is_listed_unresolved(self):
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            self._snapshot(tmp, {
                "boundary": True, "present": True,
                "declared": {"projects": ["no-clusters-here"]},
                "projects": [
                    {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                ],
            })
            out = self._render(base, {})
        self.assertIn("kube-agents-demo, no-clusters-here (unresolved)", out)

    def test_snapshot_with_unresolved_container_uses_unresolved_partial_reason(self):
        cases = [
            {
                "boundary": True, "present": False,
                "declared": {"folders": ["123"]},
                "projects": [
                    {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                ],
            },
            {
                "boundary": True, "present": True,
                "declared": {"folders": ["123"]},
                "containers": [{"id": "folders/123", "outcome": "denied"}],
                "projects": [
                    {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                ],
            },
        ]
        for payload in cases:
            with self.subTest(present=payload["present"]), TemporaryDirectory() as tmp:
                base = self._fleet(tmp)
                self._snapshot(tmp, payload)
                out = self._render(base, {})
                scope_section = out.split(agent_roster.SCOPE_HEADING)[1]
                self.assertIn(
                    f"- GCP projects in scope (partial: {agent_roster.SCOPE_PARTIAL_UNRESOLVED}): kube-agents-demo",
                    scope_section,
                )
                self.assertNotRegex(scope_section, r"GCP projects in scope \(\d+\)")
                self.assertNotIn("declared-scope", scope_section)

    def test_scope_bounds_large_in_scope_and_over_cap_lists(self):
        # An install with many readable or over-cap rows must cap named projects
        # at SCOPE_NAMED_LIMIT and summarize the remainder on every turn.
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            self._snapshot(tmp, {
                "boundary": True, "present": True,
                "declared": {"folders": ["123"]},
                "projects": [
                    {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                    {"id": "app-prod", "state": "in-scope", "outcome": "ok", "via": ["folders/123"]},
                    *[
                        {"id": f"cap-{i:02d}", "state": "in-scope", "outcome": "over-cap", "via": ["folders/123"]}
                        for i in range(30)
                    ],
                ],
            })
            out = self._render(base, {})
        scope_section = out.split(agent_roster.SCOPE_HEADING)[1]
        self.assertIn("- GCP projects in scope (32): kube-agents-demo, app-prod, cap-00 (over-cap)", scope_section)
        self.assertIn("cap-17 (over-cap), and 12 more (12 over-cap)", scope_section)
        self.assertNotIn("cap-18", scope_section)

        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            self._snapshot(tmp, {
                "boundary": True, "present": True,
                "declared": {"folders": ["123"]},
                "projects": [
                    *[
                        {"id": f"ok-{i:02d}", "state": "in-scope", "outcome": "ok",
                         "via": ["management"] if i == 0 else ["folders/123"]}
                        for i in range(25)
                    ],
                    *[
                        {"id": f"cap-{i:02d}", "state": "in-scope", "outcome": "over-cap", "via": ["folders/123"]}
                        for i in range(10)
                    ],
                    *[
                        {"id": f"den-{i:02d}", "state": "in-scope", "outcome": "denied", "via": ["folders/123"]}
                        for i in range(2)
                    ],
                ],
            })
            out_mixed = self._render(base, {})
        mixed_section = out_mixed.split(agent_roster.SCOPE_HEADING)[1]
        self.assertIn("- GCP projects in scope (37): ok-00", mixed_section)
        self.assertIn("ok-19, and 17 more (10 over-cap, 2 denied)", mixed_section)
        self.assertNotIn("ok-20", mixed_section)
        self.assertNotIn("cap-00", mixed_section)

        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            self._snapshot(tmp, {
                "boundary": True, "present": True,
                "declared": {"folders": ["123"]},
                "projects": [
                    {"id": f"ok-{i:02d}", "state": "in-scope", "outcome": "ok",
                     "via": ["management"] if i == 0 else ["folders/123"]}
                    for i in range(25)
                ],
            })
            out_ok = self._render(base, {})
        ok_section = out_ok.split(agent_roster.SCOPE_HEADING)[1]
        self.assertIn("- GCP projects in scope (25): ok-00", ok_section)
        self.assertTrue(ok_section.strip().endswith("ok-19, and 5 more"), ok_section)
        self.assertNotIn("ok-20", ok_section)

    def test_boundary_false_reads_the_render_and_says_partial(self):
        # The CR gained spec.scope since the last tick: the snapshot predates it.
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            self._snapshot(tmp, {
                "boundary": False, "declared": {},
                "projects": [
                    {"id": "kube-agents-demo", "state": "in-scope", "outcome": "ok", "via": ["management"]},
                ],
            })
            render = Path(tmp) / "scope.json"
            render.write_text('{"present": true, "projects": ["a", "b"]}')
            out = self._render(base, {"KUBEAGENTS_SCOPE_FILE": str(render),
                                      "GCP_PROJECT_ID": "kube-agents-demo"})
        scope_section = out.split(agent_roster.SCOPE_HEADING)[1]
        self.assertIn(f"- GCP projects in scope (partial: {agent_roster.SCOPE_PARTIAL_PENDING}): ", scope_section)
        self.assertIn("kube-agents-demo, a (unresolved), b (unresolved)", scope_section)
        self.assertIn("Say the list is partial", scope_section)
        for jargon in ("sweep", "swept", "render", "snapshot", "management project"):
            self.assertNotIn(jargon, scope_section)

    def test_folders_only_render_with_no_snapshot_is_partial(self):
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            render = Path(tmp) / "scope.json"
            render.write_text('{"present": true, "folders": ["123"]}')
            out = self._render(base, {"KUBEAGENTS_SCOPE_FILE": str(render),
                                      "GCP_PROJECT_ID": "kube-agents-demo"})
        scope_section = out.split(agent_roster.SCOPE_HEADING)[1]
        self.assertIn(f"- GCP projects in scope (partial: {agent_roster.SCOPE_PARTIAL_PENDING}): ", scope_section)
        self.assertNotRegex(scope_section, r"GCP projects in scope \(\d+\)")
        for jargon in ("sweep", "swept", "render", "snapshot", "management project"):
            self.assertNotIn(jargon, scope_section)

    def test_numeric_management_project_render_uses_front_door_partial_reason(self):
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            render = Path(tmp) / "scope.json"
            render.write_text('{"present": true, "projects": ["a"]}')
            out = self._render(base, {"KUBEAGENTS_SCOPE_FILE": str(render),
                                      "GCP_PROJECT_ID": "123456789012"})
        scope_section = out.split(agent_roster.SCOPE_HEADING)[1]
        self.assertIn(
            f"- GCP projects in scope (partial: {agent_roster.SCOPE_PARTIAL_NUMERIC_HOST}): a (unresolved)",
            scope_section,
        )
        self.assertNotIn("123456789012", scope_section)
        for jargon in ("sweep", "swept", "render", "snapshot", "management project"):
            self.assertNotIn(jargon, scope_section)

    def test_an_unreadable_render_says_so(self):
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp),
                               {"KUBEAGENTS_SCOPE_FILE": str(Path(tmp) / "missing.json")})
        self.assertIn(agent_roster.SCOPE_UNREADABLE, out)

    def test_no_declared_scope_says_so(self):
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp), self.HOST_ENV)
        self.assertIn(agent_roster.SCOPE_UNDECLARED, out)

    def test_a_missing_scope_reader_costs_only_the_scope_section(self):
        from unittest import mock

        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            with mock.patch.object(agent_roster, "_scope_reader", return_value=None):
                out = self._render(base, self.HOST_ENV)
        self.assertIn("Host Cluster Identity", out)
        self.assertNotIn("GCP Projects in Scope", out)

    def test_an_empty_fleet_still_carries_host_and_scope(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            base.mkdir()
            out = self._render(base, self.HOST_ENV)
        self.assertTrue(out.startswith(agent_roster.EMPTY_ROSTER))
        self.assertIn("- cluster=platform-agent-host", out)

    def test_format_roster_is_unchanged_by_the_new_sections(self):
        # list_agents and the plugin both call render(); format_roster stays the
        # specialist list #3264 shaped, with no host or scope text.
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            out = agent_roster.format_roster(agent_roster.discover(base))
        self.assertNotIn("Host Cluster Identity", out)
        self.assertIn("(the kcc-gitops-hub cluster agent; project gca-gke-2025, location us-east1-c)", out)


class TestRouterEnvCarriesTheRosterInputs(unittest.TestCase):
    """list_agents renders what the injected block renders only if its env block passes the inputs."""

    def test_router_env_block_names_every_variable_render_reads(self):
        import yaml

        config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
        env = config["mcp_servers"]["router"]["env"]
        needed = {agent_roster.HOST_CLUSTER_ENV, agent_roster.HOST_LOCATION_ENV,
                  agent_roster.HOST_PROJECT_ENV}
        reader = agent_roster._scope_reader()
        self.assertIsNotNone(reader)
        needed |= {reader.SCOPE_FILE_ENV, *reader.MANAGEMENT_PROJECT_ENVS}
        for name in sorted(needed):
            self.assertEqual(env.get(name), "${" + name + "}", name)


if __name__ == "__main__":
    unittest.main()
