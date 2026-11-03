"""Tests for the shared specialist roster.

This logic used to live in ``router_server.py`` and be reachable only through
the ``list_agents`` MCP tool. It moved here when the roster started being
injected into every turn as well, so both consumers describe the same fleet in
the same words; the tests moved with it. ``test_router_server.py`` now only
checks that the tool still delegates here.
"""

import os
import sys
import time
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

# The fleet-scale roster gate (#4440): the injected roster measured at the fleet
# the gate names, against the budget it names.
# FLEET_SCALE_PROFILES is N, the Cluster Agent profile count the roster is
# rendered at.
FLEET_SCALE_PROFILES = 2500
# ROSTER_TOKEN_BUDGET is the most the injected text may cost every turn.
ROSTER_TOKEN_BUDGET = 2000
# CHARS_PER_TOKEN is the approximation the gate allows: this module is
# stdlib-only, so there is no tokenizer to count with.
CHARS_PER_TOKEN = 4
# ROSTER_RENDER_SECONDS bounds one render at N; the gate as a whole has two
# minutes, and the Go half of it takes about one second.
ROSTER_RENDER_SECONDS = 60
# ROSTER_FIX_ISSUE is the change that brought the roster under budget.
ROSTER_FIX_ISSUE = "#4432"
# CACHE_AGED_SECONDS is how far back a fixture's mtimes are set so the cache
# trusts them (agent_roster.CACHE_RACY_SECONDS refuses a tree changed within
# the last two seconds, because filesystem timestamps are coarse).
CACHE_AGED_SECONDS = 60
# CACHE_REAGED_SECONDS is a second, different old age: a path mutated and then
# dated here is past the racy window, so only a key that carries its mtime
# makes the next render re-read it.
CACHE_REAGED_SECONDS = 30
# CACHE_LONG_WALK_SECONDS is how long a fleet-scale walk is made to appear
# (through a fake clock) so a stamp that lands during it sits past the racy
# window by the time the stamps are read: only the walk-start rule explains
# the next render's re-read.
CACHE_LONG_WALK_SECONDS = 10
# One fixture profile: the name as cluster_agent_profile.profile_name derives
# it, and the identity block the scaffold stamps into config.yaml.
FLEET_SCALE_PROJECT = "fleet-project"
FLEET_SCALE_LOCATION = "us-central1"
FLEET_SCALE_CLUSTER_FORMAT = "cluster-{index:04d}"

# Every environment variable render() reads, cleared before a render so the
# host and scope sections come from the test's own env and nothing else.
ROSTER_ENV_NAMES = (
    "GKE_CLUSTER_NAME", "GKE_PROJECT_ID", "GKE_LOCATION", "GCP_PROJECT_ID",
    "KUBEAGENTS_SCOPE_FILE", "PLATFORM_AGENT_HOME",
)
# The one description every scaffolded Cluster Agent shares.
CLUSTER_ROLE = "One cluster's runtime state.\n"


def _render_roster(base: Path, env: dict[str, str]) -> str | None:
    """render(base) with ROSTER_ENV_NAMES cleared and env applied on top."""
    from unittest import mock

    clean = {k: v for k, v in os.environ.items() if k not in ROSTER_ENV_NAMES}
    clean.update(env)
    with mock.patch.dict(os.environ, clean, clear=True):
        return agent_roster.render(base)


def _write_cluster_profile(base: Path, name: str, project: str, cluster: str, location: str) -> None:
    """One scaffolded Cluster Agent profile: the shared role and its identity block."""
    (base / name).mkdir()
    (base / name / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
    (base / name / "config.yaml").write_text(
        "cluster_identity:\n"
        f"  cluster: {cluster}\n  location: {location}\n  project: {project}\n"
    )


def _age(path: Path, seconds: int = CACHE_AGED_SECONDS) -> None:
    """Date ``path`` ``seconds`` back, so the cache does not treat it as racy."""
    then = time.time_ns() - seconds * 1_000_000_000
    os.utime(path, ns=(then, then))


def _forget() -> None:
    """Drop the module's cached roster, so a test starts from a miss."""
    agent_roster._cache = None


@contextmanager
def _counting_reads():
    """Count every file read and directory listing pathlib performs inside the block."""
    from unittest import mock

    read_text, iterdir = Path.read_text, Path.iterdir
    with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text) as reads, \
            mock.patch.object(Path, "iterdir", autospec=True, side_effect=iterdir) as lists:
        counts = {"reads": reads, "lists": lists}
        yield counts


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
            _write_cluster_profile(base, name, project, cluster, location)
        return base

    def _render(self, base: Path, env: dict[str, str]) -> str:
        return _render_roster(base, env)

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
        placeholders = {k: "${" + k + "}" for k in ROSTER_ENV_NAMES}
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
            (colliding / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
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
            (other / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
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
                (base / name / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
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


class TestFleetScaleRoster(unittest.TestCase):
    """The injected roster at N=2,500 Cluster Agents, against its token budget (#4440).

    Every Cluster Agent is scaffolded from one template, so the fixture shares
    one description across the fleet and format_roster groups them under it;
    what remains is one line per agent, its assignee id and its cluster note,
    which is what grows with N.
    """

    def _fleet(self, tmp: str) -> Path:
        base = Path(tmp) / "profiles"
        (base / "platform").mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet work.\n")
        for index in range(1, FLEET_SCALE_PROFILES + 1):
            cluster = FLEET_SCALE_CLUSTER_FORMAT.format(index=index)
            name = agent_roster._profile_name(FLEET_SCALE_PROJECT, cluster, FLEET_SCALE_LOCATION)
            _write_cluster_profile(base, name, FLEET_SCALE_PROJECT, cluster, FLEET_SCALE_LOCATION)
        return base

    def _render(self, base: Path) -> str:
        # The host and scope sections are constant in N; render without a
        # host so the measurement is the roster the fleet grows.
        out = _render_roster(base, {})
        self.assertIsNotNone(out)
        return out

    def test_the_injected_roster_fits_the_token_budget(self):
        """The budget ROSTER_FIX_ISSUE brought the roster under.

        Before it, format_roster named every one of the N agents with its
        cluster note, about 130 characters a line at the scaffold's name
        length, so the injected block cost tens of thousands of tokens on
        every turn. Now a shared-role group past the threshold is counted by
        project and location, and the block is constant in N.
        """
        with TemporaryDirectory() as tmp:
            out = self._render(self._fleet(tmp))
        tokens = len(out) // CHARS_PER_TOKEN
        self.assertLess(
            tokens,
            ROSTER_TOKEN_BUDGET,
            f"the roster at N={FLEET_SCALE_PROFILES} is about {tokens} tokens "
            f"({len(out)} characters at {CHARS_PER_TOKEN} per token), over the "
            f"{ROSTER_TOKEN_BUDGET}-token budget",
        )
        self.assertIn(f"The following {FLEET_SCALE_PROFILES} agents share one role", out)
        self.assertIn(f"  - by project: {FLEET_SCALE_PROJECT} ({FLEET_SCALE_PROFILES})", out)
        self.assertIn(f"  - by location: {FLEET_SCALE_LOCATION} ({FLEET_SCALE_PROFILES})", out)
        self.assertIn(agent_roster.SUMMARY_HINT, out)
        # The named block is gone, the distinct specialist is not.
        self.assertNotIn(FLEET_SCALE_CLUSTER_FORMAT.format(index=1), out)
        self.assertIn("- platform: Fleet work.", out)

    def test_a_second_render_at_fleet_scale_reads_nothing(self):
        # The other half of the acceptance: pre_llm_call reads no files when
        # nothing changed. The first render reads two files per profile
        # (CAPABILITIES.md and config.yaml; SOUL.md only when the first is
        # missing) plus the scope snapshot; the second, the same tree, pays
        # stats and no reads and no listing.
        _forget()
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            _age(base)
            with _counting_reads() as cold:
                first = self._render(base)
            with _counting_reads() as counts:
                second = self._render(base)
        self.assertEqual(first, second)
        profiles = FLEET_SCALE_PROFILES + 1  # plus platform
        self.assertGreaterEqual(cold["reads"].call_count, 2 * profiles)
        self.assertLessEqual(cold["reads"].call_count, 2 * profiles + 2)
        self.assertEqual(counts["reads"].call_count, 0, "a cache hit read a file")
        self.assertEqual(counts["lists"].call_count, 0, "a cache hit listed a directory")

    def test_the_fixture_is_the_fleet_the_gate_measures(self):
        # Checked on discovery, the renderer's input, not on the rendered
        # text: a check that pinned today's one-line-per-agent shape would
        # go red in the pull request that closes ROSTER_FIX_ISSUE, in a test
        # not marked expectedFailure, while the pinned test flipped green.
        with TemporaryDirectory() as tmp:
            agents = agent_roster.discover(self._fleet(tmp))
        self.assertIsNotNone(agents)
        clusters = [a for a in agents if a.get(agent_roster.IDENTITY_FIELD_PREFIX + agent_roster.IDENTITY_NAME_KEY)]
        self.assertEqual(len(clusters), FLEET_SCALE_PROFILES)
        self.assertEqual(len({a["responsibilities"] for a in clusters}), 1, "every Cluster Agent shares one role")

    def test_the_roster_renders_within_the_gate_budget(self):
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp)
            started = time.monotonic()
            self._render(base)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, ROSTER_RENDER_SECONDS, f"rendering N={FLEET_SCALE_PROFILES} took {elapsed:.1f}s")


class TestRosterCache(unittest.TestCase):
    """render() re-reads the tree only when something it depends on changed."""

    def setUp(self):
        _forget()
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name) / "profiles"
        self.base.mkdir()
        self._profile("platform", "Fleet work.")
        _write_cluster_profile(self.base, "cluster-p-a-us", "p", "a", "us")
        _age(self.base)

    def tearDown(self):
        _forget()
        self._tmp.cleanup()

    def _profile(self, name, capabilities):
        (self.base / name).mkdir()
        (self.base / name / "CAPABILITIES.md").write_text(capabilities)

    def _render(self, env=None):
        return _render_roster(self.base, env or {})

    def _reads_on_render(self, env=None):
        with _counting_reads() as counts:
            out = self._render(env)
        return out, counts["reads"].call_count

    def test_a_hit_reads_no_files(self):
        self._render()
        out, reads = self._reads_on_render()
        self.assertEqual(reads, 0)
        self.assertIn("- platform: Fleet work.", out)
        self.assertIn("cluster-p-a-us (the a cluster agent", out)

    def test_adding_a_profile_invalidates(self):
        self.assertNotIn("cluster-new", self._render())
        _write_cluster_profile(self.base, "cluster-new", "p", "new", "us")
        # The directory was just written: racy, so re-read with no ageing.
        self.assertIn("cluster-new (the new cluster agent", self._render())
        # And once the tree is quiet it caches again.
        _age(self.base)
        self._render()
        _, reads = self._reads_on_render()
        self.assertEqual(reads, 0)

    def test_removing_a_profile_invalidates(self):
        self.assertIn("cluster-p-a-us", self._render())
        for child in (self.base / "cluster-p-a-us").iterdir():
            child.unlink()
        (self.base / "cluster-p-a-us").rmdir()
        self.assertNotIn("cluster-p-a-us", self._render())

    # The tests above mutate the tree and render at once, so the re-read comes
    # from the racy window, which refuses any stamp within two seconds of now.
    # The ones below date the mutated path to a second, different old age
    # before rendering: the racy guard is then silent and only a key that
    # carries that path's mtime makes the render re-read, one per keyed stamp.

    def test_an_aged_add_misses_on_the_directory_stamp(self):
        self.assertNotIn("cluster-new", self._render())
        _write_cluster_profile(self.base, "cluster-new", "p", "new", "us")
        _age(self.base, CACHE_REAGED_SECONDS)
        out, reads = self._reads_on_render()
        self.assertGreater(reads, 0)
        self.assertIn("cluster-new (the new cluster agent", out)

    def test_an_aged_removal_misses_on_the_directory_stamp(self):
        self.assertIn("cluster-p-a-us", self._render())
        for child in (self.base / "cluster-p-a-us").iterdir():
            child.unlink()
        (self.base / "cluster-p-a-us").rmdir()
        _age(self.base, CACHE_REAGED_SECONDS)
        out, reads = self._reads_on_render()
        self.assertGreater(reads, 0)
        self.assertNotIn("cluster-p-a-us", out)

    def test_an_aged_snapshot_rewrite_misses_on_the_snapshot_stamp(self):
        snapshot = self.base.parent / "fleet_scope.json"
        snapshot.write_text(
            '{"boundary": true, "declared": {"projects": ["p"]}, "projects": '
            '[{"id": "p", "state": "in-scope", "outcome": "ok", "via": ["projects"]}]}\n'
        )
        _age(snapshot)
        self.assertIn("GCP projects in scope (1): p", self._render())
        snapshot.write_text(
            '{"boundary": true, "declared": {"projects": ["p", "q"]}, "projects": '
            '[{"id": "p", "state": "in-scope", "outcome": "ok", "via": ["projects"]}, '
            '{"id": "q", "state": "in-scope", "outcome": "ok", "via": ["projects"]}]}\n'
        )
        _age(snapshot, CACHE_REAGED_SECONDS)
        out, reads = self._reads_on_render()
        self.assertGreater(reads, 0)
        self.assertIn("GCP projects in scope (2): p, q", out)

    def test_an_aged_render_rewrite_misses_on_the_render_stamp(self):
        render = self.base.parent / "render.json"
        render.write_text('{"present": true, "projects": ["p"]}\n')
        _age(render)
        env = {"KUBEAGENTS_SCOPE_FILE": str(render)}
        self.assertNotIn("q (unresolved)", self._render(env))
        render.write_text('{"present": true, "projects": ["p", "q"]}\n')
        _age(render, CACHE_REAGED_SECONDS)
        out, reads = self._reads_on_render(env)
        self.assertGreater(reads, 0)
        self.assertIn("q (unresolved)", out)

    def _aged_unstamped_profile(self, name):
        home = self.base / name
        home.mkdir()
        (home / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
        (home / "config.yaml").write_text("model: x\n")
        for path in (self.base, home, home / "config.yaml"):
            _age(path)
        self.assertIn(f"  - {name}\n", self._render())
        _, reads = self._reads_on_render()
        self.assertEqual(reads, 0, "an aged unstamped profile is cached")
        return home

    def test_an_aged_stamp_misses_on_the_unstamped_profiles_config_stamp(self):
        # The stamp rewrites config.yaml in place: the file's mtime moves, the
        # profile directory's and the profiles directory's do not.
        home = self._aged_unstamped_profile("cluster-p-c-us")
        (home / "config.yaml").write_text("model: x\ncluster_identity:\n  cluster: c\n  location: us\n  project: p\n")
        _age(home / "config.yaml", CACHE_REAGED_SECONDS)
        out, reads = self._reads_on_render()
        self.assertGreater(reads, 0)
        self.assertIn("cluster-p-c-us (the c cluster agent", out)

    def test_an_aged_overlay_misses_on_the_unstamped_profiles_directory_stamp(self):
        # An overlay adds a file beside config.yaml: the profile directory's
        # mtime moves and nothing else does.
        home = self._aged_unstamped_profile("cluster-p-c-us")
        (home / "overlay.yaml").write_text("x: 1\n")
        _age(home, CACHE_REAGED_SECONDS)
        _, reads = self._reads_on_render()
        self.assertGreater(reads, 0)

    def test_a_stamp_landing_during_the_walk_is_not_cached_with_the_content_read_before_it(self):
        # At fleet scale the walk is seconds long. An unstamped profile whose
        # config.yaml is stamped after the walk read it, but before the stamps
        # are taken, must not be stored: the stamp would vouch for content
        # that has no identity. The clock is faked so the walk appears long
        # and the stamp sits past the racy-by-now window when read; the same
        # clock holds for the next render, so nothing but the walk-start rule
        # can explain its re-read.
        from types import SimpleNamespace
        from unittest import mock

        home = self.base / "cluster-p-c-us"
        home.mkdir()
        (home / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
        (home / "config.yaml").write_text("model: x\n")
        for path in (self.base, home, home / "config.yaml"):
            _age(path)
        offset = [0]
        clock = SimpleNamespace(time_ns=lambda: time.time_ns() + offset[0])
        real = agent_roster.discover

        def stamped_during_the_walk(root):
            agents = real(root)
            (home / "config.yaml").write_text(
                "model: x\ncluster_identity:\n  cluster: c\n  location: us\n  project: p\n"
            )
            offset[0] = CACHE_LONG_WALK_SECONDS * 1_000_000_000
            return agents

        with mock.patch.object(agent_roster, "time", clock), \
                mock.patch.object(agent_roster, "discover", side_effect=stamped_during_the_walk):
            self.assertIn("  - cluster-p-c-us\n", self._render())
        with mock.patch.object(agent_roster, "time", clock):
            out, reads = self._reads_on_render()
        self.assertGreater(reads, 0, "the pre-stamp content was cached under the post-stamp mtime")
        self.assertIn("cluster-p-c-us (the c cluster agent", out)

    def test_a_tree_changed_within_the_racy_window_is_not_cached(self):
        # Filesystem timestamps are coarse: two writes inside one tick share
        # an mtime, so a tree touched just now is re-read until it is quiet.
        now = time.time_ns()
        os.utime(self.base, ns=(now, now))
        self._render()
        _, reads = self._reads_on_render()
        self.assertGreater(reads, 0)

    def test_a_scope_snapshot_rewrite_invalidates(self):
        snapshot = self.base.parent / "fleet_scope.json"
        snapshot.write_text(
            '{"boundary": true, "declared": {"projects": ["p"]}, "projects": '
            '[{"id": "p", "state": "in-scope", "outcome": "ok", "via": ["projects"]}]}\n'
        )
        _age(snapshot)
        self.assertIn("GCP projects in scope (1): p", self._render())
        snapshot.write_text(
            '{"boundary": true, "declared": {"projects": ["p", "q"]}, "projects": '
            '[{"id": "p", "state": "in-scope", "outcome": "ok", "via": ["projects"]}, '
            '{"id": "q", "state": "in-scope", "outcome": "ok", "via": ["projects"]}]}\n'
        )
        # A rewrite in place moves the file's mtime, not the directory's.
        self.assertIn("GCP projects in scope (2): p, q", self._render())

    def test_a_description_lost_to_a_read_fault_is_not_cached(self):
        # Before the cache a fault cost one turn; it must not now cost every
        # turn until the next scaffold moves the directory's mtime.
        from unittest import mock

        real = Path.read_text

        def flaky(path, *args, **kwargs):
            if path.name == "CAPABILITIES.md" and path.parent.name == "platform":
                raise OSError(5, "EIO")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=flaky):
            out = self._render()
        self.assertIn(f"- platform: {agent_roster.NO_DESCRIPTION}", out)
        out, reads = self._reads_on_render()
        self.assertGreater(reads, 0)
        self.assertIn("- platform: Fleet work.", out)

    def test_a_fault_in_an_overlapping_discovery_is_not_cached_over_a_clean_one(self):
        # The gateway runs each session's turn on its own thread, so two
        # discoveries overlap after one invalidation. The interleaving, in one
        # thread: discovery A's walk notes a fault; discovery B starts inside
        # A's window, walks clean and caches; A finishes with a roster missing
        # the faulted profile. A must not store that roster over B's.
        from unittest import mock

        real = agent_roster.discover

        def interleaved(root):
            agent_roster._note_degraded()
            with mock.patch.object(agent_roster, "discover", real):
                agent_roster.render(root)
            return [a for a in real(root) if a["name"] != "platform"]

        with mock.patch.object(agent_roster, "discover", side_effect=interleaved):
            self.assertNotIn("platform", self._render())
        out, reads = self._reads_on_render()
        self.assertEqual(reads, 0, "B's clean roster stays cached")
        self.assertIn("- platform: Fleet work.", out)

    def test_a_skipped_profile_is_not_cached(self):
        from unittest import mock

        real = Path.is_dir

        def flaky(path):
            if path.name == "platform":
                raise OSError(13, "EACCES")
            return real(path)

        with mock.patch.object(Path, "is_dir", autospec=True, side_effect=flaky):
            self.assertNotIn("platform", self._render())
        self.assertIn("- platform: Fleet work.", self._render())

    def test_an_unreadable_scope_is_not_cached(self):
        missing = self.base.parent / "render.json"
        self.assertIn(agent_roster.SCOPE_UNREADABLE, self._render({"KUBEAGENTS_SCOPE_FILE": str(missing)}))
        missing.write_text('{"present": true, "projects": ["q"]}\n')
        _age(missing)
        self.assertIn("q (unresolved)", self._render({"KUBEAGENTS_SCOPE_FILE": str(missing)}))

    def test_a_scope_render_rewrite_invalidates(self):
        # No snapshot: the scope section comes from the operator's render, a
        # mounted ConfigMap whose file gets a new mtime on update.
        render = self.base.parent / "render.json"
        render.write_text('{"present": true, "projects": ["p"]}\n')
        _age(render)
        env = {"KUBEAGENTS_SCOPE_FILE": str(render)}
        out = self._render(env)
        self.assertIn("p (unresolved)", out)
        self.assertNotIn("q (unresolved)", out)
        self._render(env)
        _, reads = self._reads_on_render(env)
        self.assertEqual(reads, 0)
        render.write_text('{"present": true, "projects": ["p", "q"]}\n')
        self.assertIn("q (unresolved)", self._render(env))

    def test_an_unstamped_cluster_profile_is_cached_on_its_own_stamps(self):
        # One directory that never gets stamped (a dead scaffold, a plugin
        # mount point) must not turn the cache off for the whole fleet; it is
        # keyed on its own mtimes instead, so the in-place stamp still misses.
        home = self.base / "cluster-p-c-us"
        home.mkdir()
        (home / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
        (home / "config.yaml").write_text("model: x\n")
        for path in (self.base, home, home / "config.yaml"):
            _age(path)
        self.assertIn("  - cluster-p-c-us\n", self._render())
        _, reads = self._reads_on_render()
        self.assertEqual(reads, 0, "an aged unstamped profile is cached")
        (home / "config.yaml").write_text("model: x\ncluster_identity:\n  cluster: c\n  location: us\n  project: p\n")
        self.assertIn("cluster-p-c-us (the c cluster agent", self._render())

    def test_an_unstamped_cluster_profile_is_re_read_until_stamped(self):
        # The scaffold creates the directory, then rewrites config.yaml in
        # place; the second step changes no directory mtime, so a roster
        # cached between the two would never learn the cluster's name.
        (self.base / "cluster-p-b-us").mkdir()
        (self.base / "cluster-p-b-us" / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
        _age(self.base)
        out = self._render()
        self.assertIn("  - cluster-p-b-us\n", out)
        (self.base / "cluster-p-b-us" / "config.yaml").write_text(
            "cluster_identity:\n  cluster: b\n  location: us\n  project: p\n"
        )
        self.assertIn("cluster-p-b-us (the b cluster agent", self._render())
        # Stamped, it caches like any other.
        self._render()
        _, reads = self._reads_on_render()
        self.assertEqual(reads, 0)

    def test_a_different_threshold_is_a_different_key(self):
        for _ in range(3):
            _write_cluster_profile(self.base, f"cluster-p-{_}-us", "p", str(_), "us")
        _age(self.base)
        self.assertIn("  - cluster-p-0-us", self._render({agent_roster.SUMMARY_THRESHOLD_ENV: "9"}))
        self.assertIn("counted here rather than named", self._render({agent_roster.SUMMARY_THRESHOLD_ENV: "1"}))

    def test_a_failed_discovery_is_not_cached(self):
        from unittest import mock

        with mock.patch.object(agent_roster, "discover", return_value=None):
            self.assertIsNone(self._render())
        self.assertIn("- platform: Fleet work.", self._render())

    def test_an_explicit_base_is_part_of_the_key(self):
        self._render()
        with TemporaryDirectory() as other:
            base = Path(other) / "profiles"
            (base / "tse").mkdir(parents=True)
            (base / "tse" / "CAPABILITIES.md").write_text("Support.")
            _age(base)
            self.assertIn("- tse: Support.", _render_roster(base, {}))


class TestSummaryThreshold(unittest.TestCase):
    def _threshold(self, value):
        from unittest import mock

        with mock.patch.dict(os.environ, {agent_roster.SUMMARY_THRESHOLD_ENV: value}):
            return agent_roster.summary_threshold()

    def test_unset_is_the_default(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(agent_roster.SUMMARY_THRESHOLD_ENV, None)
            self.assertEqual(agent_roster.summary_threshold(), agent_roster.SUMMARY_THRESHOLD_DEFAULT)

    def test_a_whole_number_is_read(self):
        self.assertEqual(self._threshold("5"), 5)
        self.assertEqual(self._threshold(" 0 "), 0)

    def test_anything_else_is_the_default(self):
        default = agent_roster.SUMMARY_THRESHOLD_DEFAULT
        for value in ("", "-1", "2.5", "many", "${KUBEAGENTS_ROSTER_SUMMARY_THRESHOLD}"):
            self.assertEqual(self._threshold(value), default, value)


class TestSummarizedGroup(unittest.TestCase):
    """A shared-role group past the threshold is counted, not named."""

    def _fleet(self, tmp, clusters):
        base = Path(tmp) / "profiles"
        (base / "platform").mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet work.")
        for project, cluster, location in clusters:
            name = agent_roster._profile_name(project, cluster, location)
            _write_cluster_profile(base, name, project, cluster, location)
        return base

    def test_counts_by_project_and_location_with_the_role_and_the_hint(self):
        clusters = [("p1", "a", "us"), ("p1", "b", "us"), ("p2", "c", "eu")]
        with TemporaryDirectory() as tmp:
            out = _render_roster(self._fleet(tmp, clusters), {agent_roster.SUMMARY_THRESHOLD_ENV: "2"})
        self.assertIn(
            "The following 3 agents share one role and are counted here rather than named:\n"
            "  - by project: p1 (2), p2 (1)\n"
            "  - by location: us (2), eu (1)\n"
            f"  Shared role: {CLUSTER_ROLE.strip()}\n"
            f"{agent_roster.SUMMARY_HINT}",
            out,
        )
        self.assertNotIn("cluster-p1-a-us", out)
        self.assertIn("- platform: Fleet work.", out)

    def test_at_the_threshold_the_names_stay(self):
        clusters = [("p1", "a", "us"), ("p1", "b", "us")]
        with TemporaryDirectory() as tmp:
            out = _render_roster(self._fleet(tmp, clusters), {agent_roster.SUMMARY_THRESHOLD_ENV: "2"})
        self.assertIn("  - cluster-p1-a-us (the a cluster agent; project p1, location us)", out)
        self.assertNotIn("counted here", out)

    def test_the_default_threshold_names_twenty(self):
        clusters = [("p", f"c{i:02d}", "us") for i in range(20)]
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp, clusters)
            out = _render_roster(base, {})
            self.assertIn("The following 20 agents share one role — pick the one", out)
            _write_cluster_profile(base, "cluster-p-c20-us", "p", "c20", "us")
            out = _render_roster(base, {})
        self.assertIn("The following 21 agents share one role and are counted", out)

    def test_the_named_cap_counts_the_rest(self):
        clusters = [(f"p{i:02d}", f"c{i:02d}", "us") for i in range(25)]
        with TemporaryDirectory() as tmp:
            out = _render_roster(self._fleet(tmp, clusters), {agent_roster.SUMMARY_THRESHOLD_ENV: "1"})
        self.assertIn("p19 (1), and 5 more projects (5 agents)", out)
        self.assertNotIn("p20 (1)", out)
        self.assertIn("  - by location: us (25)", out)

    def test_an_unstamped_member_is_counted_as_such(self):
        with TemporaryDirectory() as tmp:
            base = self._fleet(tmp, [("p", "a", "us"), ("p", "b", "us")])
            (base / "tse-1").mkdir()
            (base / "tse-1" / "CAPABILITIES.md").write_text(CLUSTER_ROLE)
            out = _render_roster(base, {agent_roster.SUMMARY_THRESHOLD_ENV: "0"})
        self.assertIn("  - by project: p (2), (unstamped) (1)", out)

    def test_a_distinct_specialist_and_the_sections_are_unchanged(self):
        clusters = [("p", "a", "us"), ("p", "b", "us")]
        env = {
            agent_roster.SUMMARY_THRESHOLD_ENV: "1",
            "GKE_CLUSTER_NAME": "a", "GKE_PROJECT_ID": "p", "GKE_LOCATION": "us",
        }
        with TemporaryDirectory() as tmp:
            out = _render_roster(self._fleet(tmp, clusters), env)
        self.assertIn("- platform: Fleet work.", out)
        self.assertIn(
            "- cluster=a, project=p, location=us, assignee=cluster-p-a-us", out,
            "the host line still names its assignee under a counted block",
        )


class TestFilter(unittest.TestCase):
    """format_matches hands back the names a counted block withheld."""

    def setUp(self):
        self.agents = [
            {"name": "platform", "responsibilities": "Fleet work."},
            *(
                {
                    "name": agent_roster._profile_name(p, c, l),
                    "responsibilities": CLUSTER_ROLE,
                    "cluster_cluster": c, "cluster_project": p, "cluster_location": l,
                }
                for p, c, l in (("proj-one", "seeded-a", "us-central1"),
                                ("proj-one", "seeded-b", "us-central1"),
                                ("proj-two", "Seeded-C", "europe-west1"))
            ),
        ]

    def test_matches_a_cluster_name(self):
        out = agent_roster.format_matches(self.agents, "seeded-a")
        self.assertTrue(out.startswith('Agents matching "seeded-a" (1 of 1 matched, 4 on the roster):\n'))
        self.assertIn(
            "- cluster-proj-one-seeded-a-us-central1 (the seeded-a cluster agent; "
            f"project proj-one, location us-central1): {CLUSTER_ROLE}",
            out,
        )
        self.assertNotIn("seeded-b", out)
        self.assertNotIn("platform", out)

    def test_matches_project_location_and_assignee_id_case_insensitively(self):
        self.assertIn("2 of 2 matched", agent_roster.format_matches(self.agents, "PROJ-ONE"))
        self.assertIn("1 of 1 matched", agent_roster.format_matches(self.agents, "europe"))
        self.assertIn("1 of 1 matched", agent_roster.format_matches(self.agents, "seeded-c"))
        self.assertIn("1 of 1 matched", agent_roster.format_matches(self.agents, " platform "))

    def test_matches_are_grouped_never_summarized(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {agent_roster.SUMMARY_THRESHOLD_ENV: "0"}):
            out = agent_roster.format_matches(self.agents, "us-central1")
        self.assertIn("The following 2 agents share one role — pick the one whose cluster you need:", out)
        self.assertIn("  - cluster-proj-one-seeded-a-us-central1 (the seeded-a cluster agent", out)
        self.assertNotIn("counted here", out)

    def test_no_match_says_so_and_points_at_platform(self):
        out = agent_roster.format_matches(self.agents, "nope")
        self.assertEqual(out, agent_roster.FILTER_NONE.format(query="nope", total=4))
        self.assertIn("route the request to `platform`", out)

    def test_the_cap_counts_the_rest(self):
        agents = [
            {"name": f"cluster-p-c{i:03d}-us", "responsibilities": CLUSTER_ROLE,
             "cluster_cluster": f"c{i:03d}", "cluster_project": "p", "cluster_location": "us"}
            for i in range(60)
        ]
        out = agent_roster.format_matches(agents, "p")
        self.assertIn("(50 of 60 matched, 60 on the roster)", out)
        self.assertTrue(out.endswith("... and 10 more; narrow the filter."))
        self.assertIn("cluster-p-c049-us", out)
        self.assertNotIn("cluster-p-c050-us", out)

    def test_unknown_in_unknown_out(self):
        self.assertIsNone(agent_roster.format_matches(None, "x"))

    def test_a_no_match_from_a_degraded_walk_is_unread_not_absent(self):
        out = agent_roster.format_matches(self.agents, "nope", degraded=True)
        self.assertEqual(out, agent_roster.FILTER_UNREAD.format(query="nope", total=4))
        self.assertNotIn("has no cluster agent yet", out)
        # A match is a match whatever the walk skipped elsewhere.
        self.assertIn("1 of 1 matched", agent_roster.format_matches(self.agents, "seeded-a", degraded=True))

    def test_render_filtered_reports_a_skipped_profile_as_unread(self):
        # The one profile the filter is looking for is the one the walk could
        # not read: the answer must not state that the cluster has no agent.
        from unittest import mock

        _forget()
        real = Path.is_dir

        def flaky(path):
            if path.name == "cluster-p-b-us":
                raise OSError(13, "EACCES")
            return real(path)

        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            base.mkdir()
            _write_cluster_profile(base, "cluster-p-a-us", "p", "a", "us")
            _write_cluster_profile(base, "cluster-p-b-us", "p", "b", "us")
            _age(base)
            with mock.patch.object(Path, "is_dir", autospec=True, side_effect=flaky):
                degraded = agent_roster.render_filtered("b", base)
            clean = agent_roster.render_filtered("b", base)
        _forget()
        self.assertEqual(degraded, agent_roster.FILTER_UNREAD.format(query="b", total=1))
        self.assertIn("cluster-p-b-us (the b cluster agent", clean)

    def test_render_filtered_reads_the_cached_tree(self):
        _forget()
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            base.mkdir()
            _write_cluster_profile(base, "cluster-p-a-us", "p", "a", "us")
            _write_cluster_profile(base, "cluster-p-b-us", "p", "b", "us")
            _age(base)
            _render_roster(base, {})
            with _counting_reads() as counts:
                out = agent_roster.render_filtered("b", base)
            self.assertEqual(counts["reads"].call_count, 0)
        _forget()
        self.assertIn("cluster-p-b-us (the b cluster agent", out)
        self.assertNotIn("cluster-p-a-us", out)


class TestRouterEnvCarriesTheRosterInputs(unittest.TestCase):
    """list_agents renders what the injected block renders only if its env block passes the inputs."""

    def test_router_env_block_names_every_variable_render_reads(self):
        import yaml

        config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
        env = config["mcp_servers"]["router"]["env"]
        needed = {agent_roster.HOST_CLUSTER_ENV, agent_roster.HOST_LOCATION_ENV,
                  agent_roster.HOST_PROJECT_ENV, agent_roster.SUMMARY_THRESHOLD_ENV}
        reader = agent_roster._scope_reader()
        self.assertIsNotNone(reader)
        needed |= {reader.SCOPE_FILE_ENV, *reader.MANAGEMENT_PROJECT_ENVS}
        for name in sorted(needed):
            self.assertEqual(env.get(name), "${" + name + "}", name)


if __name__ == "__main__":
    unittest.main()
