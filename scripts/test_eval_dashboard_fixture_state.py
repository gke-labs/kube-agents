"""fixture_state.py scans every pool project's seeded fleet and never fails
the bot for what it finds.

The scan is driven end to end: the REAL hack/fleet-kubeconfigs.sh and
hack/fleet-fixture-state.py run per project, with `gcloud` and `kubectl`
stubbed on PATH the way tests/test_fleet_fixture_state.py stubs them -- a
JSON "world" the stubs answer from, so each test names only its own defect.
What is pinned:

* the project list is gitops_repo_for_project() in hack/ci-deploy.sh, a
  commented-out arm is not a project, and a checkout without the function
  is a repository bug (exit 1);
* a healthy fleet scans as every role `healthy`, and the document carries
  the scan time, the per-project and overall counts, and the previous
  scan's drift map;
* a fixture out of shape is `drifted` with the assertion and what was
  observed; a role the runner could not publish is `not_checked` with the
  runner's own warning; a read that failed is `not_checked` with its reason;
* every read runs as the project's seeded-fleet reader, pre-flighted with
  one token mint, and a project whose reader cannot be impersonated is
  "not checked" with gcloud's words and no runner is started;
* a missing kubectl or gcloud, a runner that fails, and a runner that hangs
  past its ceiling are each "not checked" with a reason, exit 0;
* the workflow wires the job the header describes: its own cron, its own
  job guarded by `github.event.schedule`, the SDK components it needs, and
  the adjudicator reading the published document.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from eval_dashboard import fixture_state

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci-health.yml"
CATALOG = REPO / "bench" / "tf" / "fleet" / "fixtures.json"
CI_DEPLOY = REPO / "hack" / "ci-deploy.sh"

UTC = timezone.utc
NOW = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)
PROJECT = "kube-agents-evals-2"
OTHER = "kube-agents-evals-3"

# The gcloud stub: the calls hack/fleet-kubeconfigs.sh and
# hack/fleet-fixture-state.py make, answered from the world. A cluster's
# kubeconfig names its project in the server URL so the kubectl stub can
# answer per project.
_STUB_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("gcloud " + " ".join(args) + "\n")
world = json.load(open(os.environ["STUB_WORLD"]))
def flag(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
if args[:2] == ["auth", "print-access-token"]:
    sa = flag("--impersonate-service-account") or ""
    if sa in world.get("cannot_impersonate", []):
        print("WARNING: This command is using service account impersonation.", file=sys.stderr)
        print(f"ERROR: (gcloud.auth.print-access-token) PERMISSION_DENIED: Failed to impersonate [{sa}]. Make sure the account has the \"roles/iam.serviceAccountTokenCreator\" role.", file=sys.stderr)
        print("- '@type': type.googleapis.com/google.rpc.ErrorInfo", file=sys.stderr)
        sys.exit(1)
    print("WARNING: This command is using service account impersonation.", file=sys.stderr)
    print("ya29.stubtoken")
    sys.exit(0)
project = flag("--project") or ""
if args[:3] == ["container", "clusters", "list"]:
    time.sleep(float(os.environ.get("STUB_LIST_SLEEP", "0")))
    clusters = world.get("clusters", {}).get(project)
    if clusters is None:
        print(f"ERROR: (gcloud.container.clusters.list) Permission denied on project {project}", file=sys.stderr)
        sys.exit(1)
    for name, location in clusters:
        print(f"{name}\t{location}")
    sys.exit(0)
if args[:3] == ["container", "clusters", "get-credentials"]:
    name = args[3]
    with open(os.environ["KUBECONFIG"], "w") as fh:
        fh.write(f"""apiVersion: v1
kind: Config
current-context: c
clusters:
- name: {name}
  cluster:
    server: https://{name}.{project}.stub
    certificate-authority-data: Y2E=
contexts:
- name: c
  context:
    cluster: {name}
    user: u
users:
- name: u
  user:
    exec:
      command: gke-gcloud-auth-plugin
""")
    sys.exit(0)
if args[:3] == ["container", "clusters", "describe"]:
    doc = world.get("describe", {}).get(project, {}).get(args[3])
    if doc is None:
        print(f"ERROR: (gcloud.container.clusters.describe) Not found: {args[3]}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(doc))
    sys.exit(0)
if args[:2] == ["container", "get-server-config"]:
    print(json.dumps(world.get("server_config", {})))
    sys.exit(0)
print("unexpected gcloud call: " + " ".join(args), file=sys.stderr)
sys.exit(64)
'''

# The kubectl stub: `config view` for the runner's token rewrite, `get` for
# the presence probes (`-o name`) and the state reads (`-o json`). The
# project is read off the kubeconfig's server URL; answers come from
# world["kubectl"] with world["kubectl_overrides"][project] on top.
_STUB_KUBECTL = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("kubectl " + " ".join(args) + "\n")
world = json.load(open(os.environ["STUB_WORLD"]))
kubeconfig = os.environ.get("KUBECONFIG")
for a in args:
    if a.startswith("--kubeconfig="):
        kubeconfig = a.split("=", 1)[1]
server = ""
if kubeconfig and os.path.exists(kubeconfig):
    m = re.search(r"server: (\S+)", open(kubeconfig).read())
    server = m.group(1) if m else ""
project = server.rsplit("/", 1)[-1].split(".")[1] if server.count(".") >= 2 else ""
if args[:1] == ["config"]:
    print(server if "server" in " ".join(args) else "Y2E=")
    sys.exit(0)
if project in world.get("unreachable", []):
    print("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout", file=sys.stderr)
    sys.exit(1)
i = args.index("get")
kind = args[i + 1]
name = None
selector = None
rest = args[i + 2:]
j = 0
while j < len(rest):
    a = rest[j]
    if a == "-l":
        selector = rest[j + 1]; j += 2
    elif a in ("-n", "-o"):
        j += 2
    elif a.startswith("-"):
        j += 1
    else:
        name = a; j += 1
key = f"{kind}/{name}" if name else f"{kind}?{selector or ''}"
answers = dict(world.get("kubectl", {}))
answers.update(world.get("kubectl_overrides", {}).get(project, {}))
if key not in answers or answers[key] is None:
    if name:
        print(f'Error from server (NotFound): {kind} "{name}" not found', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"items": []}))
    sys.exit(0)
print(json.dumps(answers[key]))
'''


def _pod(*, restarts, last_reason, phase="Running"):
    status = {"restartCount": restarts, "lastState": {}}
    if last_reason:
        status["lastState"] = {"terminated": {"reason": last_reason, "exitCode": 137}}
    return {"status": {"phase": phase, "containerStatuses": [status]}}


def _pods(*pods):
    return {"items": list(pods)}


def _future(days=30):
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cluster_b():
    return {
        "currentMasterVersion": "1.33.4-gke.1134000",
        "releaseChannel": {"channel": "REGULAR"},
        # The no-surge pool as `clusters describe` reports it; the
        # readiness-surge-blocked role asserts on maxUnavailable here.
        "nodePools": [{"name": "default-pool", "upgradeSettings": {"maxSurge": 1}}, {"name": "no-surge-pool", "upgradeSettings": {"maxUnavailable": 1}}],
        "maintenancePolicy": {"window": {"maintenanceExclusions": {"hold-the-minor-lag": {"startTime": "2026-09-01T00:00:00Z", "endTime": _future(), "maintenanceExclusionOptions": {"scope": "NO_MINOR_UPGRADES"}}}}},
    }


def healthy_world(*projects):
    """Every role planted and in its designed state on each project."""
    trio = [["seeded-a", "us-central1-a"], ["seeded-b", "us-central1-a"], ["seeded-c", "us-central1-a"]]
    return {
        "clusters": {project: trio for project in projects},
        "kubectl": {
            "namespace/seeded-debug": {"metadata": {"name": "seeded-debug"}},
            "namespace/seeded-capacity": {"metadata": {"name": "seeded-capacity"}},
            "namespace/seeded-reliability": {"metadata": {"name": "seeded-reliability"}},
            "deployment/payments-api": {"status": {"readyReplicas": 0}},
            "pod?app=payments-api": _pods(_pod(restarts=3, last_reason="OOMKilled")),
            "horizontalpodautoscaler/inference-server": {"status": {}},
            "deployment/inference-server": {"status": {"readyReplicas": 1, "replicas": 3}},
            "pod?app=inference-server": _pods(_pod(restarts=0, last_reason=None), _pod(restarts=0, last_reason=None, phase="Pending")),
            "deployment/checkout-gateway": {"status": {"readyReplicas": 2, "replicas": 2}},
            "poddisruptionbudget?": {"items": []},
            "clusterrolebinding/debug-binding": {"roleRef": {"name": "cluster-admin"}, "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": "seeded-security"}]},
            "node?cloud.google.com/gke-nodepool=idle-batch-pool": {"items": [{"spec": {"taints": [{"key": "seeded-role", "value": "idle-batch", "effect": "NoSchedule"}]}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]},
            # seeded-b's upgrade-readiness trio: the pool that cannot surge,
            # the workload pinned to it, and the fail-closed webhook. The
            # first two gate on being up and Running respectively, because
            # the finding is what a drain WOULD do, which is only meaningful
            # while they are healthy.
            "namespace/seeded-upgrade": {"metadata": {"name": "seeded-upgrade"}},
            "poddisruptionbudget/pinned-batch-runner": {"spec": {"maxUnavailable": 0}, "status": {"disruptionsAllowed": 0, "currentHealthy": 1}},
            "node?cloud.google.com/gke-nodepool=no-surge-pool": {"items": [{"spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]},
            "deployment/pinned-batch-runner": {
                "spec": {"template": {"spec": {"nodeSelector": {"seeded-role": "no-surge"}}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=pinned-batch-runner": _pods(_pod(restarts=0, last_reason=None)),
            "validatingwebhookconfiguration/seeded-fail-closed-gate": {"webhooks": [{"name": "gate.seeded.invalid", "failurePolicy": "Fail", "timeoutSeconds": 30, "clientConfig": {"service": {"name": "nonexistent-admission-gate"}}}]},
        },
        "describe": {project: {"seeded-b": _cluster_b(), "seeded-c": {"currentMasterVersion": "1.34.1-gke.1"}} for project in projects},
        "server_config": {"channels": [{"channel": "REGULAR", "defaultVersion": "1.34.1-gke.1"}]},
    }


class ScanHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("gcloud", _STUB_GCLOUD), ("kubectl", _STUB_KUBECTL)):
            stub = self.bin / name
            stub.write_text(body)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self.world_path = self.tmp / "world.json"
        self.workdir = self.tmp / "work"
        self.roles = fixture_state.catalog_roles(CATALOG)

    def environ(self, **extra):
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_WORLD": str(self.world_path),
            "TMPDIR": str(self.tmp),
            "HOME": os.environ.get("HOME", str(self.tmp)),
        }
        env.update(extra)
        return env

    def scan(self, world, projects=(PROJECT,), prior=None, impersonate=True, timeout=120, **extra_env):
        self.world_path.write_text(json.dumps(world))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            doc = fixture_state.scan(
                list(projects),
                self.roles,
                self.workdir,
                prior=prior,
                now=NOW,
                workers=2,
                project_timeout=timeout,
                impersonate=impersonate,
                environ=self.environ(**extra_env),
            )
        return doc, err.getvalue()

    def calls(self):
        return self.log.read_text().splitlines()

    def states(self, doc, project=PROJECT):
        return {role: entry["state"] for role, entry in doc["projects"][project]["roles"].items()}


class ProjectList(unittest.TestCase):
    def test_the_repo_mapping_names_the_pool(self):
        projects = fixture_state.pool_projects(CI_DEPLOY.read_text())
        self.assertEqual(projects[:3], ["kube-agents-evals", "kube-agents-evals-2", "kube-agents-evals-3"])
        self.assertEqual(len(projects), len(set(projects)))
        self.assertGreaterEqual(len(projects), 30)

    def test_a_commented_out_arm_is_not_a_project_and_no_function_is_an_error(self):
        text = 'gitops_repo_for_project() {\n  case "$1" in\n    kube-agents-evals) echo "x" ;;\n    # kube-agents-evals-9) echo "y" ;;\n    *) return 1 ;;\n  esac\n}\n'
        self.assertEqual(fixture_state.pool_projects(text), ["kube-agents-evals"])
        with self.assertRaises(ValueError):
            fixture_state.pool_projects("echo nothing here")


class HealthyScan(ScanHarness):
    def test_every_role_is_healthy_and_the_document_carries_the_counts(self):
        doc, err = self.scan(healthy_world(PROJECT))
        self.assertEqual(doc["scanned_at"], "2026-09-14T20:00:00+00:00")
        self.assertEqual(set(self.states(doc).values()), {"healthy"})
        self.assertEqual(set(self.states(doc)), set(self.roles))
        entry = doc["projects"][PROJECT]
        self.assertEqual(entry["summary"], {"healthy": 11, "drifted": 0, "not_checked": 0})
        self.assertEqual(entry["reader"], "seeded-fleet-reader@kube-agents-evals-2.iam.gserviceaccount.com")
        self.assertNotIn("error", entry)
        self.assertEqual(doc["summary"], {"projects": 1, "checked": 1, "drifted_projects": 0, "healthy": 11, "drifted": 0, "not_checked": 0})
        self.assertEqual(doc["previous"], {"scanned_at": None, "drifted": {}})
        self.assertEqual(err, "")

    def test_every_read_runs_as_the_projects_reader(self):
        self.scan(healthy_world(PROJECT))
        calls = self.calls()
        reader = "seeded-fleet-reader@kube-agents-evals-2.iam.gserviceaccount.com"
        self.assertEqual(calls[0], f"gcloud auth print-access-token --impersonate-service-account={reader}", "the pre-flight mint comes first")
        # The runner minted as the reader for the rewrite, and the state
        # script's describes ran under the impersonation property (which
        # the stub cannot see, but the kubeconfig rewrite it can: every
        # kubectl read after the rewrite went through the exec credential,
        # whose plugin is hack/fleet-reader-credential.sh naming the reader).
        self.assertGreaterEqual(sum(1 for c in calls if c.startswith("gcloud auth print-access-token")), 2)
        kubeconfig = (self.workdir / PROJECT / "fleet" / "crashloop-workload.kubeconfig").read_text()
        self.assertIn("fleet-reader-credential.sh", kubeconfig)
        self.assertIn(reader, kubeconfig)

    def test_the_previous_scans_drift_map_rides_along(self):
        prior = {"scanned_at": "2026-09-14T19:00:00+00:00", "projects": {PROJECT: {"roles": {"no-pdb-workload": {"state": "drifted", "detail": ["x"]}, "crashloop-workload": {"state": "healthy", "detail": []}}}}}
        doc, _ = self.scan(healthy_world(PROJECT), prior=prior)
        self.assertEqual(doc["previous"], {"scanned_at": "2026-09-14T19:00:00+00:00", "drifted": {PROJECT: ["no-pdb-workload"]}})
        self.assertEqual(fixture_state.previous_drift_map(doc), {PROJECT: ["no-pdb-workload"]})

    def test_without_impersonation_the_reader_is_not_minted(self):
        doc, _ = self.scan(healthy_world(PROJECT), impersonate=False)
        self.assertIsNone(doc["projects"][PROJECT]["reader"])
        self.assertFalse(any("print-access-token" in c for c in self.calls()))
        self.assertEqual(set(self.states(doc).values()), {"healthy"})


class Drift(ScanHarness):
    def test_a_fixture_out_of_shape_is_drifted_with_the_assertion_and_what_was_seen(self):
        world = healthy_world(PROJECT)
        world["kubectl_overrides"] = {PROJECT: {"deployment/checkout-gateway": {"status": {"readyReplicas": 0, "replicas": 2}}}}
        doc, err = self.scan(world)
        states = self.states(doc)
        self.assertEqual(states["no-pdb-workload"], "drifted")
        self.assertEqual({s for r, s in states.items() if r != "no-pdb-workload"}, {"healthy"})
        detail = doc["projects"][PROJECT]["roles"]["no-pdb-workload"]["detail"]
        self.assertTrue(detail[0].startswith("deployment/checkout-gateway status.readyReplicas eq 2: observed 0"), detail)
        self.assertEqual(fixture_state.drift_map(doc), {PROJECT: ["no-pdb-workload"]})
        self.assertEqual(fixture_state.read_map(doc), {PROJECT: sorted(self.roles)}, "a drifted role was read too")
        self.assertEqual(fixture_state.drift_detail(doc, PROJECT, "no-pdb-workload"), detail)
        self.assertEqual(doc["summary"]["drifted_projects"], 1)
        self.assertIn(f"{PROJECT}: drifted: no-pdb-workload", err)

    def test_two_projects_scan_independently(self):
        world = healthy_world(PROJECT, OTHER)
        world["kubectl_overrides"] = {OTHER: {"pod?app=payments-api": _pods(_pod(restarts=0, last_reason=None))}}
        doc, _ = self.scan(world, projects=(PROJECT, OTHER))
        self.assertEqual(set(self.states(doc, PROJECT).values()), {"healthy"})
        self.assertEqual(self.states(doc, OTHER)["crashloop-workload"], "drifted")
        self.assertEqual(doc["summary"], {"projects": 2, "checked": 2, "drifted_projects": 1, "healthy": 21, "drifted": 1, "not_checked": 0})


class NotChecked(ScanHarness):
    def test_a_reader_that_cannot_be_impersonated_stops_before_the_runner(self):
        world = healthy_world(PROJECT)
        world["cannot_impersonate"] = ["seeded-fleet-reader@kube-agents-evals-2.iam.gserviceaccount.com"]
        doc, err = self.scan(world)
        entry = doc["projects"][PROJECT]
        self.assertEqual(set(self.states(doc).values()), {"not_checked"})
        self.assertIn("cannot mint a token as seeded-fleet-reader@kube-agents-evals-2.iam.gserviceaccount.com", entry["error"])
        self.assertIn("roles/iam.serviceAccountTokenCreator", entry["error"])
        self.assertIn("PERMISSION_DENIED", entry["error"], "gcloud's ERROR line, not the YAML dump after it")
        self.assertEqual(len(self.calls()), 1, "no runner, no state check")
        self.assertEqual(doc["summary"]["checked"], 0)
        self.assertEqual(fixture_state.not_checked_reason(doc), entry["error"])
        self.assertIn(f"{PROJECT}: not checked", err)

    def test_a_project_the_reader_cannot_list_is_not_checked_with_the_runners_warning(self):
        world = healthy_world(PROJECT)
        world["clusters"] = {}
        doc, _ = self.scan(world)
        states = self.states(doc)
        self.assertEqual(set(states.values()), {"not_checked"})
        detail = doc["projects"][PROJECT]["roles"]["crashloop-workload"]["detail"]
        self.assertIn("could not list clusters", detail[0])
        self.assertNotIn("error", doc["projects"][PROJECT], "the runner ran and said why; that is not a scan error")

    def test_an_unplanted_role_carries_the_runners_warning_naming_it(self):
        world = healthy_world(PROJECT)
        world["kubectl_overrides"] = {PROJECT: {"clusterrolebinding/debug-binding": None}}
        doc, _ = self.scan(world)
        states = self.states(doc)
        self.assertEqual(states["rbac-overgrant"], "not_checked")
        self.assertEqual({s for r, s in states.items() if r != "rbac-overgrant"}, {"healthy"})
        detail = doc["projects"][PROJECT]["roles"]["rbac-overgrant"]["detail"][0]
        self.assertIn("fixture role 'rbac-overgrant'", detail)
        self.assertIn("never planted", detail)

    def test_a_read_that_fails_is_not_checked_not_drift(self):
        world = healthy_world(PROJECT)
        world["describe"][PROJECT].pop("seeded-c")
        doc, _ = self.scan(world)
        states = self.states(doc)
        self.assertEqual(states["drift-outlier"], "not_checked")
        self.assertIn("clusters describe seeded-c failed", doc["projects"][PROJECT]["roles"]["drift-outlier"]["detail"][0])
        self.assertEqual(doc["summary"]["checked"], 1, "six roles were read; the project counts as checked")

    def test_a_missing_binary_checks_nothing_and_exits_cleanly(self):
        world = healthy_world(PROJECT)
        self.world_path.write_text(json.dumps(world))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            doc = fixture_state.scan([PROJECT, OTHER], self.roles, self.workdir, now=NOW, which=lambda name: None if name == "kubectl" else "/usr/bin/x", environ=self.environ())
        for project in (PROJECT, OTHER):
            self.assertEqual(set(self.states(doc, project).values()), {"not_checked"})
            self.assertEqual(doc["projects"][project]["error"], "kubectl is not on PATH, so nothing was checked")
        self.assertEqual(self.calls(), [], "nothing ran")
        self.assertIn("kubectl is not on PATH", err.getvalue())
        self.assertEqual(fixture_state.checked_projects(doc), 0)
        self.assertEqual(fixture_state.read_map(doc), {})

    def test_a_runner_that_hangs_is_not_checked_within_the_ceiling(self):
        doc, _ = self.scan(healthy_world(PROJECT), timeout=2, STUB_LIST_SLEEP="10")
        self.assertEqual(set(self.states(doc).values()), {"not_checked"})
        self.assertEqual(doc["projects"][PROJECT]["error"], "hack/fleet-kubeconfigs.sh did not finish within 2s")

    def test_a_runner_that_fails_is_not_checked_with_its_last_line(self):
        world = healthy_world(PROJECT)
        self.world_path.write_text(json.dumps(world))
        missing = self.tmp / "no-such-catalog.json"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entry = fixture_state.scan_project(PROJECT, self.roles, self.workdir, 60, catalog=missing, environ=self.environ())
        self.assertIn("hack/fleet-kubeconfigs.sh failed (1)", entry["error"])
        self.assertIn("fleet fixture catalog not found", entry["error"])


class EntryPoint(ScanHarness):
    def run_main(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = fixture_state.main(argv)
        return rc, err.getvalue()

    def test_main_writes_the_document_and_reads_the_prior(self):
        self.world_path.write_text(json.dumps(healthy_world(PROJECT)))
        prior = self.tmp / "prior.json"
        prior.write_text(json.dumps({"scanned_at": "2026-09-14T19:00:00+00:00", "projects": {PROJECT: {"roles": {"idle-nodepool": {"state": "drifted", "detail": []}}}}}))
        out = self.tmp / "fixture-state.json"
        env = self.environ()
        with unittest.mock.patch.dict(os.environ, env):
            rc, err = self.run_main(["--out", str(out), "--prior", str(prior), "--projects", PROJECT, "--workdir", str(self.workdir), "--now", NOW.isoformat(), "--workers", "1"])
        self.assertEqual(rc, 0, err)
        doc = json.loads(out.read_text())
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["previous"]["drifted"], {PROJECT: ["idle-nodepool"]})
        self.assertIn("1 of 1 pool projects checked", err)

    def test_a_checkout_without_the_mapping_is_a_repository_bug(self):
        script = self.tmp / "ci-deploy.sh"
        script.write_text("#!/bin/bash\necho no mapping\n")
        rc, err = self.run_main(["--out", str(self.tmp / "o.json"), "--ci-deploy-script", str(script)])
        self.assertEqual(rc, 1)
        self.assertIn("no gitops_repo_for_project()", err)


class Workflow(unittest.TestCase):
    """ci-health.yml wires the scan the way docs/ci-health.md says."""

    def setUp(self):
        import yaml

        self.text = WORKFLOW.read_text()
        self.doc = yaml.safe_load(self.text)
        self.jobs = self.doc["jobs"]

    def test_two_crons_and_each_run_goes_to_one_job(self):
        on = self.doc.get("on") or self.doc.get(True)
        self.assertEqual([entry["cron"] for entry in on["schedule"]], ["*/15 * * * *", "0 * * * *"])
        self.assertIn("github.event.schedule == '*/15 * * * *'", self.jobs["refresh-and-adjudicate"]["if"])
        self.assertIn("github.event.schedule == '0 * * * *'", self.jobs["fixture-state-scan"]["if"])
        self.assertIn("inputs.fixture_state_scan", self.jobs["fixture-state-scan"]["if"])
        self.assertEqual(on["workflow_dispatch"]["inputs"]["fixture_state_scan"]["type"], "boolean")

    def test_the_scan_job_is_guarded_least_privileged_and_queued_apart(self):
        job = self.jobs["fixture-state-scan"]
        self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"])
        self.assertIn("github.ref == 'refs/heads/main'", job["if"])
        self.assertEqual(job["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(job["concurrency"]["group"], "ci-health-fixture-state-scan")
        self.assertEqual(self.jobs["refresh-and-adjudicate"]["concurrency"]["group"], "ci-health")
        self.assertNotIn("concurrency", self.doc, "queued per job, so a scan never holds a tick")
        checkout = job["steps"][0]
        self.assertFalse(checkout["with"]["persist-credentials"])

    def test_the_scan_job_installs_what_the_scripts_shell_out_to_and_publishes(self):
        steps = self.jobs["fixture-state-scan"]["steps"]
        sdk = next(step for step in steps if "setup-gcloud" in step.get("uses", ""))
        self.assertEqual(sorted(sdk["with"]["install_components"].split(",")), ["gke-gcloud-auth-plugin", "kubectl"])
        auth = next(step for step in steps if "google-github-actions/auth" in step.get("uses", ""))
        self.assertEqual(auth["with"]["service_account"], "${{ env.CI_HEALTH_SA }}")
        scan = next(step for step in steps if "fixture_state.py" in step.get("run", ""))
        self.assertIn("--prior work/fixture-state-prior.json", scan["run"])
        self.assertIn('--workers "$FIXTURE_STATE_WORKERS"', scan["run"])
        upload = next(step for step in steps if "cp work/fixture-state.json" in step.get("run", ""))
        self.assertIn('"$DASHBOARD_BUCKET/fixture-state.json"', upload["run"])
        import re

        job_text = self.text[self.text.index("  fixture-state-scan:") :]
        for line in re.findall(r"^\s*uses: .*$", job_text, re.MULTILINE):
            self.assertRegex(line, r"@[0-9a-f]{40} # v\d", f"{line.strip()} is not pinned to a SHA with its version")

    def test_the_upload_steps_summary_script_runs(self):
        """The inline python that prints the scan's summary is shell-quoted
        with single quotes, so a double quote inside an f-string expression
        must not be escaped with a backslash (a SyntaxError on 3.12 that
        would red the job after the document was already published)."""
        import subprocess
        import sys

        steps = self.jobs["fixture-state-scan"]["steps"]
        upload = next(step for step in steps if "cp work/fixture-state.json" in step.get("run", ""))
        run = upload["run"]
        code = run[run.index("python3 -c '") + len("python3 -c '") :]
        code = code[: code.index("\n'")]
        compile(code, "<upload step>", "exec")
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / "work"
            work.mkdir()
            document = {
                "summary": {"checked": 1, "projects": 2, "drifted_projects": 1, "healthy": 6, "drifted": 1, "not_checked": 7},
                "projects": {
                    "p1": {"roles": {"r1": {"state": "drifted"}, "r2": {"state": "healthy"}}},
                    "p2": {"roles": {"r1": {"state": "not_checked"}}, "error": "cannot mint a token"},
                },
            }
            (work / "fixture-state.json").write_text(json.dumps(document), encoding="utf-8")
            proc = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 of 2 projects checked; drifted on 1", proc.stdout)
        self.assertIn("p1: drifted: r1", proc.stdout)
        self.assertIn("p2: not checked: cannot mint a token", proc.stdout)

    def test_the_tick_reads_the_published_scan(self):
        steps = self.jobs["refresh-and-adjudicate"]["steps"]
        fetch = next(step for step in steps if "fixture-state.json" in step.get("run", "") and "health-prev.json" in step["run"])
        self.assertIn('gsutil -q cp "$DASHBOARD_BUCKET/fixture-state.json" work/fixture-state.json || true', fetch["run"])
        adjudicate = next(step for step in steps if step.get("name") == "Adjudicate")
        self.assertIn("--fixture-state work/fixture-state.json", adjudicate["run"])


if __name__ == "__main__":
    import unittest.mock

    unittest.main()
