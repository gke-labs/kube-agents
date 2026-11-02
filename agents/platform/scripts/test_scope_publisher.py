"""Unit tests for scope_publisher — the resolved scope the A2A gateway reads.

Run: cd agents/platform/scripts && python3 -m unittest discover -p 'test_scope_publisher.py'

The broker is faked by FakeBroker, which answers only the five argv shapes the broker's
`scope` role admits, token for token, and only for a project or number a shape-2 to shape-5
answer returned earlier in the run (the broker's member window). A command outside that
contract fails the test, so the publisher cannot ship a call the real broker refuses.
"""

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_agent_reconcile as rec  # noqa: E402
import scope_publisher as pub  # noqa: E402

# The shared memberHash vector: lane 4 (the Go gateway) and the operator's ScopeResolved
# check pin the same input bytes and the same hash. A change here is a change to the
# contract, made on both sides at once.
MEMBER_HASH_VECTOR_INPUT = (
    '{"present":true,"projects":["team-b","team-a","team-a"],'
    '"folders":["222222222222","111111111111"],"organizations":[],'
    '"sharedVpcHosts":["host-project-1"],'
    '"exclude":{"projects":["legacy-*","123456789012"],'
    '"clusters":[{"projectId":"team-a","location":"us-central1","clusterName":"c1"}]}}'
)
MEMBER_HASH_VECTOR_CANONICAL = (
    '{"exclude":{"projects":["123456789012","legacy-*"]},'
    '"folders":["111111111111","222222222222"],"maxProjects":100,"metricsScopes":[],'
    '"organizations":[],"projects":["team-a","team-b"],"sharedVpcHosts":["host-project-1"]}'
)
MEMBER_HASH_VECTOR_HASH = "sha256:078c09284502aeccfafc4e819358684d7b3938b6f0e6b8204967114a3eaf8118"
# Two more vectors, written on the Go side; the Go tests of the operator and gateway changes
# pin all three.
MEMBER_HASH_GATEWAY_VECTORS = (
    ('{"present":true,"projects":["p2","p1","p1"],"folders":["123"],"organizations":[],'
     '"sharedVpcHosts":["host-a"],"metricsScopes":["scope-p"],"maxProjects":100,'
     '"exclude":{"projects":["team-*","legacy"],"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"x"}]}}',
     "sha256:973c98df4c5a1430d79cb96f1bb482cb7326fc50bdbeb22a0b1a60d6f811062f"),
    ('{"present":true,"folders":["123"]}',
     "sha256:1fbf7a3528db2af0695f56e453d28d49c4244a7bad037954b85c7f7d69f60cfe"),
)

# The five argv shapes of the broker's `scope` role (the #4119 contract, section 5). "{}" is
# the one argument; every other token is fixed.
SHAPE_PROBE = ("gcloud", "container", "clusters", "list", "--project", "{}", "--format=value(name,location)")
SHAPE_ASSET_SEARCH = ("gcloud", "asset", "search-all-resources", "--scope={}",
                      "--asset-types=container.googleapis.com/Cluster", "--format=json(name,location)")
SHAPE_SHARED_VPC = ("gcloud", "compute", "shared-vpc", "list-associated-resources", "{}", "--format=json(id,type)")
SHAPE_METRICS_SCOPE = ("gcloud", "beta", "monitoring", "metrics-scopes", "describe",
                       "locations/global/metricsScopes/{}", "--format=json(name,monitoredProjects)")
SHAPE_PROJECT_NAME = ("gcloud", "projects", "describe", "{}", "--format=value(projectId)")
SHAPES = (SHAPE_PROBE, SHAPE_ASSET_SEARCH, SHAPE_SHARED_VPC, SHAPE_METRICS_SCOPE, SHAPE_PROJECT_NAME)

DENIED_STDERR = "ERROR: (gcloud.container.clusters.list) ResponseError: code=403, message=PERMISSION_DENIED"
API_DISABLED_STDERR = ("ERROR: (gcloud.container.clusters.list) Kubernetes Engine API has not been used in "
                       "project 1 before or it is disabled. SERVICE_DISABLED")


def _match_shape(argv) -> tuple[tuple, str] | None:
    """(shape, argument) when argv is exactly one contract shape, else None."""
    for shape in SHAPES:
        if len(argv) != len(shape):
            continue
        argument = None
        for token, template in zip(argv, shape):
            if "{}" in template:
                prefix, suffix = template.split("{}")
                if not (token.startswith(prefix) and token.endswith(suffix)) or len(token) <= len(prefix) + len(suffix):
                    break
                argument = token[len(prefix):len(token) - len(suffix)]
            elif token != template:
                break
        else:
            return shape, argument
    return None


class FakeBroker:
    """The broker's /v1/exec for the `scope` caller, as a COMMAND_RUNNER.

    `assets` maps a container (`folders/<n>`) to the project IDs whose clusters the asset
    search returns; `xpn` a Shared VPC host to its service projects; `scopes` a Metrics
    Scope to the project numbers it monitors; `names` a number to its project ID; `probes`
    a project to the stderr its `clusters list` fails with (absent: it lists one cluster).
    `fail` maps an argument to the stderr its shape-2/3/4/5 call fails with; `timeouts`
    holds the arguments whose call times out. Every call is recorded in `calls`.
    """

    def __init__(self, assets=None, xpn=None, scopes=None, names=None, probes=None, fail=None, timeouts=()):
        self.assets, self.xpn, self.scopes = assets or {}, xpn or {}, scopes or {}
        self.names, self.probes, self.fail = names or {}, probes or {}, fail or {}
        self.timeouts = set(timeouts)
        self.window: set[str] = set()
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check, timeout):
        self.calls.append(list(argv))
        matched = _match_shape(argv)
        if matched is None:
            raise AssertionError(f"argv outside the scope role's five shapes: {argv}")
        shape, argument = matched
        if shape in (SHAPE_PROBE, SHAPE_PROJECT_NAME) and argument not in self.window:
            raise AssertionError(f"{argument} was not returned to the scope caller before {argv}")
        if argument in self.timeouts:
            raise subprocess.TimeoutExpired(argv, timeout)
        if argument in self.fail:
            return self._answer(argv, check, "", self.fail[argument], 1)
        if shape == SHAPE_PROBE:
            if argument in self.probes:
                return self._answer(argv, check, "", self.probes[argument], 1)
            return self._answer(argv, check, "prod us-central1\n", "", 0)
        if shape == SHAPE_ASSET_SEARCH:
            projects = self.assets.get(argument, [])
            self.window.update(projects)
            rows = [{"name": f"//container.googleapis.com/projects/{p}/locations/us-central1/clusters/prod",
                     "location": "us-central1"} for p in projects]
            return self._answer(argv, check, json.dumps(rows), "", 0)
        if shape == SHAPE_SHARED_VPC:
            projects = self.xpn.get(argument, [])
            self.window.update(projects)
            return self._answer(argv, check, json.dumps([{"id": p, "type": "PROJECT"} for p in projects]), "", 0)
        if shape == SHAPE_METRICS_SCOPE:
            numbers = self.scopes.get(argument, [])
            self.window.update(numbers)
            monitored = [{"name": f"locations/global/metricsScopes/{argument}/projects/{n}"} for n in numbers]
            return self._answer(argv, check, json.dumps({"name": argument, "monitoredProjects": monitored}), "", 0)
        project = self.names[argument]
        self.window.add(project)
        return self._answer(argv, check, project + "\n", "", 0)

    @staticmethod
    def _answer(argv, check, stdout, stderr, code):
        if check and code:
            raise subprocess.CalledProcessError(code, argv, stdout, stderr)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class MemberHashTest(unittest.TestCase):
    def test_the_shared_vector(self):
        self.assertEqual(pub.member_hash(json.loads(MEMBER_HASH_VECTOR_INPUT)), MEMBER_HASH_VECTOR_HASH)
        # The canonical bytes the hash is taken over, for a Go implementation to compare.
        self.assertEqual(pub.MEMBER_HASH_PREFIX + __import__("hashlib").sha256(
            MEMBER_HASH_VECTOR_CANONICAL.encode()).hexdigest(), MEMBER_HASH_VECTOR_HASH)

    def test_the_gateway_vectors(self):
        for scope, want in MEMBER_HASH_GATEWAY_VECTORS:
            self.assertEqual(pub.member_hash(json.loads(scope)), want, scope)

    def test_exclude_clusters_and_order_do_not_change_the_hash(self):
        declared = json.loads(MEMBER_HASH_VECTOR_INPUT)
        declared["exclude"]["clusters"] = []
        declared["projects"] = ["team-a", "team-b"]
        declared["folders"].reverse()
        self.assertEqual(pub.member_hash(declared), MEMBER_HASH_VECTOR_HASH)

    def test_each_membership_field_changes_the_hash(self):
        base = json.loads(MEMBER_HASH_VECTOR_INPUT)
        edits = (
            ("projects", ["team-a"]),
            ("folders", ["111111111111"]),
            ("organizations", ["333333333333"]),
            ("sharedVpcHosts", []),
            ("metricsScopes", ["scope-project-1"]),
            ("exclude", {"projects": ["legacy-*"]}),
            ("maxProjects", 200),
        )
        for key, value in edits:
            declared = dict(base, **{key: value})
            self.assertNotEqual(pub.member_hash(declared), MEMBER_HASH_VECTOR_HASH, key)

    def test_a_missing_or_malformed_field_reads_as_its_default(self):
        self.assertEqual(pub.member_hash({}), pub.member_hash({"maxProjects": 100, "projects": "team-a",
                                                                "exclude": ["x"]}))


class BrokerRunTest(unittest.TestCase):
    ARGV = list(SHAPE_PROJECT_NAME[:3]) + ["123456789012", SHAPE_PROJECT_NAME[4]]

    def _run(self, completed, check=True):
        with mock.patch.object(pub.subprocess, "run", return_value=completed) as run, \
                mock.patch.dict(pub.os.environ, {"KUBECONFIG": "/tmp/kubeconfig"}), \
                mock.patch.object(pub, "log"):
            try:
                return pub.broker_run(self.ARGV, check=check, timeout=12), run
            except subprocess.CalledProcessError as e:
                return e, run

    def test_it_runs_the_client_with_the_argv_and_without_a_kubeconfig(self):
        result, run = self._run(subprocess.CompletedProcess([], 0, "team-a\n", ""))
        self.assertEqual(result.stdout, "team-a\n")
        args, kwargs = run.call_args
        self.assertEqual(args[0], [sys.executable, str(pub.BROKER_CLIENT_SCRIPT), *self.ARGV])
        self.assertEqual(kwargs["timeout"], 12)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("KUBECONFIG", kwargs["env"])

    def test_a_failure_raises_with_the_broker_stderr_for_the_lookups_to_classify(self):
        error, _ = self._run(subprocess.CompletedProcess([], 1, "", DENIED_STDERR))
        self.assertIsInstance(error, subprocess.CalledProcessError)
        self.assertEqual(rec._classify_list_failure(error.stderr), rec.OUTCOME_DENIED)
        result, _ = self._run(subprocess.CompletedProcess([], 1, "", DENIED_STDERR), check=False)
        self.assertEqual(result.returncode, 1)

    def test_a_policy_refusal_reads_unreachable_whatever_its_text(self):
        refused = subprocess.CompletedProcess([], pub.EXIT_SECURITY_POLICY_BLOCKED, "",
                                              "403 Permission denied: is not enabled for this caller")
        error, _ = self._run(refused)
        self.assertEqual(error.stderr, pub.POLICY_REFUSED_STDERR)
        self.assertEqual(rec._classify_list_failure(error.stderr), rec.OUTCOME_UNREACHABLE)

    def test_a_timeout_propagates_for_the_lookup_to_read_unreachable(self):
        with mock.patch.object(pub.subprocess, "run", side_effect=subprocess.TimeoutExpired(self.ARGV, 12)):
            with self.assertRaises(subprocess.TimeoutExpired):
                pub.broker_run(self.ARGV, check=True, timeout=12)
            with mock.patch.object(rec, "COMMAND_RUNNER", pub.broker_run), mock.patch.object(rec, "log"):
                self.assertEqual(rec._project_id_of("123456789012"), (None, rec.OUTCOME_UNREACHABLE))


class FakeBrokerTest(unittest.TestCase):
    def test_it_refuses_anything_outside_the_five_shapes(self):
        broker = FakeBroker(assets={"folders/1": ["team-a"]})
        refused = (
            ["gcloud", "asset", "search-all-resources", "--scope=folders/1",
             "--asset-types=container.googleapis.com/Cluster", "--format=json(name,location)", "--log-http"],
            ["gcloud", "container", "clusters", "list", "--project=team-a", "--format=value(name,location)"],
            ["gcloud", "config", "get-value", "project"],
            ["kubectl", "get", "pods"],
        )
        for argv in refused:
            with self.assertRaises(AssertionError, msg=argv):
                broker(argv, check=True, timeout=1)

    def test_it_admits_a_probe_only_for_a_member_it_returned(self):
        broker = FakeBroker(assets={"folders/1": ["team-a"]})
        probe = [t.format("team-a") for t in SHAPE_PROBE]
        with self.assertRaises(AssertionError):
            broker(probe, check=True, timeout=1)
        broker([t.format("folders/1") for t in SHAPE_ASSET_SEARCH], check=True, timeout=1)
        self.assertEqual(broker(probe, check=True, timeout=1).returncode, 0)


class DocumentTest(unittest.TestCase):
    def _document(self, rows):
        return {"v": 1, "resolvedAt": "2026-11-02T14:15:03Z", "memberHash": MEMBER_HASH_VECTOR_HASH,
                "state": pub.STATE_OK, "excludedProjects": ["legacy-app"], "projects": rows}

    def test_it_is_compact_and_keeps_the_field_order(self):
        row = {"id": "payments-prod", "via": ["folders/123"], "outcome": "ok", "confirmedAt": "2026-11-02T14:15:01Z"}
        self.assertEqual(
            pub.encode_document(self._document([row])),
            '{"v":1,"resolvedAt":"2026-11-02T14:15:03Z","memberHash":"' + MEMBER_HASH_VECTOR_HASH + '",'
            '"state":"ok","excludedProjects":["legacy-app"],"projects":[{"id":"payments-prod",'
            '"via":["folders/123"],"outcome":"ok","confirmedAt":"2026-11-02T14:15:01Z"}]}')

    def test_past_the_byte_budget_it_publishes_too_large_with_no_rows(self):
        # maxProjects at the CRD maximum, with IDs at the CRD's 30-character maximum, each
        # under two sources: inside the budget.
        two_sources = ["folders/111111111111", "organizations/222222222222"]
        rows = [{"id": f"p{i:029d}", "via": two_sources, "outcome": "ok",
                 "confirmedAt": "2026-11-02T14:15:01Z"} for i in range(5000)]
        text = pub.encode_document(self._document(rows))
        self.assertEqual(json.loads(text)["state"], pub.STATE_OK)
        # Each under all four source kinds: past it.
        for row in rows:
            row["via"] = two_sources + ["sharedVpcHosts/host-project-0000000000000000",
                                        "metricsScopes/scope-project-000000000000000"]
        with mock.patch.object(pub, "log"):
            too_large = json.loads(pub.encode_document(self._document(rows)))
        self.assertEqual(too_large, {"v": 1, "resolvedAt": "2026-11-02T14:15:03Z",
                                     "memberHash": MEMBER_HASH_VECTOR_HASH, "state": pub.STATE_TOO_LARGE,
                                     "excludedProjects": [], "projects": []})

    def test_a_previous_document_keeps_only_well_formed_fields(self):
        raw = json.dumps({"v": 1, "resolvedAt": "t", "state": "ok", "excludedProjects": "legacy-1",
                          "numbers": {"111": "mon-proj-a", "222": 5},
                          "projects": [{"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "extra": 1},
                                       {"id": "team-x", "via": [FOLDER_A]},
                                       {"id": "team-y", "via": FOLDER_A, "outcome": "ok"}],
                          "carried": [{"id": "999", "via": ["metricsScopes/s"], "outcome": "denied", "number": "999"},
                                      {"id": 5, "via": [], "outcome": "ok"}]})
        self.assertEqual(pub.parse_document(raw), {
            "v": 1, "resolvedAt": "t", "state": "ok", "excludedProjects": [],
            "projects": [{"id": "team-a", "via": [FOLDER_A], "outcome": "ok"}],
            "carried": [{"id": "999", "via": ["metricsScopes/s"], "outcome": "denied", "number": "999"}],
            "numbers": {"111": "mon-proj-a"}})

    def test_a_previous_document_this_run_cannot_use_reads_as_none(self):
        good = json.dumps(self._document([]))
        self.assertEqual(pub.parse_document(good)["state"], pub.STATE_OK)
        for raw in (None, "", "not json", "[]", json.dumps(dict(self._document([]), v=2)),
                    json.dumps(dict(self._document([]), projects=None))):
            self.assertIsNone(pub.parse_document(raw), raw)


class KubeApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token = Path(tmp.name) / "token"
        token.write_text("kube-token\n")
        patches = (
            mock.patch.object(pub, "KUBE_TOKEN_FILE", token),
            mock.patch.object(pub, "KUBE_CA_FILE", Path(tmp.name) / "ca.crt"),
            mock.patch.object(pub.ssl, "create_default_context", return_value=None),
            mock.patch.dict(pub.os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"}),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.requests = []

    def _urlopen(self, *answers):
        answers = list(answers)

        def fake(request, timeout, context):
            self.requests.append(request)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return FakeResponse(json.dumps(answer).encode())
        return mock.patch.object(pub.urllib.request, "urlopen", side_effect=fake)

    def test_it_reads_the_previous_document_and_its_resource_version(self):
        doc = {"v": 1, "resolvedAt": "t", "memberHash": "h", "state": "ok", "excludedProjects": [], "projects": []}
        with self._urlopen({"metadata": {"resourceVersion": "42"}, "data": {pub.DATA_KEY: json.dumps(doc)}}):
            self.assertEqual(pub.read_previous("kube-agents", "pa-resolved-scope"), (doc, "42"))
        request = self.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://10.0.0.1:443/api/v1/namespaces/kube-agents/configmaps/pa-resolved-scope")
        self.assertEqual(request.get_header("Authorization"), "Bearer kube-token")

    def test_an_empty_configmap_has_no_previous_document(self):
        with self._urlopen({"metadata": {"resourceVersion": "7"}}):
            self.assertEqual(pub.read_previous("kube-agents", "pa-resolved-scope"), (None, "7"))

    def test_it_merge_patches_the_data_key_at_the_version_it_read(self):
        with self._urlopen({}):
            pub.write_document("kube-agents", "pa-resolved-scope", '{"v":1}', "42")
        request = self.requests[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.get_header("Content-type"), "application/merge-patch+json")
        self.assertEqual(json.loads(request.data), {"data": {pub.DATA_KEY: '{"v":1}'},
                                                    "metadata": {"resourceVersion": "42"}})

    def test_a_conflict_is_raised(self):
        conflict = urllib.error.HTTPError("u", 409, "Conflict", {}, None)
        self.addCleanup(conflict.close)
        with self._urlopen(conflict), self.assertRaises(urllib.error.HTTPError):
            pub.write_document("kube-agents", "pa-resolved-scope", "{}", "42")

    def test_an_ipv6_service_host_is_bracketed(self):
        with mock.patch.dict(pub.os.environ, {"KUBERNETES_SERVICE_HOST": "fd00::1"}), self._urlopen({}):
            pub.read_previous("ns", "cm")
        self.assertTrue(self.requests[0].full_url.startswith("https://[fd00::1]:443/"))


MANAGEMENT = "mgmt-project"
# A listing budget short enough that a hanging lookup outlives it, and the bound past which
# the run is held by that lookup rather than by the budget (only a regression reaches it).
SHORT_BUDGET_SECONDS = 0.2
SYNC_FAILURE_TIMEOUT_SECONDS = 10.0
# Long enough that two commands run in parallel would overlap.
OVERLAP_WINDOW_SECONDS = 0.02
FOLDER_A = "folders/111111111111"
FOLDER_B = "folders/222222222222"
ORG = "organizations/333333333333"
PREVIOUS_TIME = "2026-11-02T14:00:00Z"


def _scope(**fields):
    return dict({"present": True}, **fields)


class BuildDocumentTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(rec, "log"), mock.patch.object(pub, "log")):
            p.start()
            self.addCleanup(p.stop)

    def _build(self, broker, declared, previous=None):
        with mock.patch.object(rec, "COMMAND_RUNNER", broker):
            document = pub.build_document(declared, MANAGEMENT, previous)
        for argv in broker.calls:
            self.assertNotIn(MANAGEMENT, " ".join(argv), "the management project is never named to the broker")
        return document

    @staticmethod
    def _rows(document):
        return {row["id"]: row for row in document["projects"]}

    @staticmethod
    def _probed(broker):
        return sorted(argv[5] for argv in broker.calls if argv[:4] == list(SHAPE_PROBE[:4]))

    def test_container_members_are_probed_and_confirmed(self):
        broker = FakeBroker(assets={FOLDER_A: ["team-a", "team-b"], ORG: ["team-b", "team-c"]})
        document = self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]], organizations=[ORG.split("/")[1]]))
        self.assertEqual(document["state"], pub.STATE_OK)
        self.assertEqual(document["memberHash"], pub.member_hash(_scope(folders=[FOLDER_A.split("/")[1]],
                                                                        organizations=[ORG.split("/")[1]])))
        self.assertEqual(document["projects"], [
            {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": document["resolvedAt"]},
            {"id": "team-b", "via": [FOLDER_A, ORG], "outcome": "ok", "confirmedAt": document["resolvedAt"]},
            {"id": "team-c", "via": [ORG], "outcome": "ok", "confirmedAt": document["resolvedAt"]},
        ])
        self.assertEqual(self._probed(broker), ["team-a", "team-b", "team-c"])

    def test_explicit_and_management_projects_get_no_row_and_no_probe(self):
        broker = FakeBroker(assets={FOLDER_A: ["team-a", "explicit-1", MANAGEMENT]})
        document = self._build(broker, _scope(projects=["explicit-1"], folders=[FOLDER_A.split("/")[1]]))
        self.assertEqual(list(self._rows(document)), ["team-a"])
        self.assertEqual(self._probed(broker), ["team-a"])

    def test_selector_members_by_id_and_by_number(self):
        broker = FakeBroker(xpn={"host-project": ["svc-proj-a"]},
                            scopes={"scope-project": ["111", "222"]},
                            names={"111": "mon-proj-a", "222": "mon-proj-b"})
        document = self._build(broker, _scope(sharedVpcHosts=["host-project"], metricsScopes=["scope-project"]))
        self.assertEqual(self._rows(document), {
            "mon-proj-a": {"id": "mon-proj-a", "via": ["metricsScopes/scope-project"], "outcome": "ok",
                      "confirmedAt": document["resolvedAt"]},
            "mon-proj-b": {"id": "mon-proj-b", "via": ["metricsScopes/scope-project"], "outcome": "ok",
                      "confirmedAt": document["resolvedAt"]},
            "svc-proj-a": {"id": "svc-proj-a", "via": ["sharedVpcHosts/host-project"], "outcome": "ok",
                      "confirmedAt": document["resolvedAt"]},
        })

    def test_a_number_that_cannot_be_named_gets_no_row(self):
        broker = FakeBroker(scopes={"scope-project": ["111"]}, fail={"111": "ERROR: PERMISSION_DENIED"})
        document = self._build(broker, _scope(metricsScopes=["scope-project"]))
        self.assertEqual(document["projects"], [])
        self.assertEqual(self._probed(broker), [])

    def test_exclusions_by_glob_and_by_number_are_recorded(self):
        broker = FakeBroker(assets={FOLDER_A: ["team-a", "legacy-1", MANAGEMENT]},
                            scopes={"scope-project": ["111", "222"]}, names={"111": "mon-proj-a", "222": "mon-proj-b"})
        document = self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]], metricsScopes=["scope-project"],
                                              exclude={"projects": ["legacy-*", "222", "mgmt-*"]}))
        self.assertEqual(list(self._rows(document)), ["mon-proj-a", "team-a"])
        # The entry that matches the management project is not applied, so it is not listed.
        self.assertEqual(document["excludedProjects"], ["legacy-1", "mon-proj-b"])
        self.assertNotIn("legacy-1", self._probed(broker))

    def test_probe_outcomes(self):
        broker = FakeBroker(assets={FOLDER_A: ["p-ok", "p-denied", "p-off", "p-slow", "p-refused"]},
                            probes={"p-denied": DENIED_STDERR, "p-off": API_DISABLED_STDERR,
                                    "p-refused": pub.POLICY_REFUSED_STDERR},
                            timeouts={"p-slow"})
        rows = self._rows(self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]])))
        self.assertEqual({p: r["outcome"] for p, r in rows.items()},
                         {"p-ok": "ok", "p-denied": "denied", "p-off": "api-disabled",
                          "p-slow": "unreachable", "p-refused": "unreachable"})
        self.assertEqual([p for p, r in rows.items() if "confirmedAt" in r], ["p-ok"])

    def test_the_cap_counts_management_and_explicit_first_and_skips_a_whole_container(self):
        # maxProjects 4: the management project and one explicit project take two slots; the
        # first folder's three members would cross the cap, so it is left out whole; the
        # second folder's two fit.
        broker = FakeBroker(assets={FOLDER_A: ["a-1", "a-2", "a-3"], FOLDER_B: ["b-1", "b-2"]})
        document = self._build(broker, _scope(maxProjects=4, projects=["explicit-1"],
                                              folders=[FOLDER_A.split("/")[1], FOLDER_B.split("/")[1]]))
        self.assertEqual(list(self._rows(document)), ["b-1", "b-2"])
        self.assertEqual(self._probed(broker), ["b-1", "b-2"])

    def test_a_failed_source_carries_its_members_forward_unprobed(self):
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": [
            {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME},
            {"id": "team-x", "via": [FOLDER_A], "outcome": "denied"},
        ]}
        broker = FakeBroker(fail={FOLDER_A: "connection reset by peer"})
        document = self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]]), previous)
        self.assertEqual(document["projects"], [
            {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME},
            {"id": "team-x", "via": [FOLDER_A], "outcome": "denied"},
        ])
        self.assertEqual(self._probed(broker), [])

    def test_a_carried_member_a_new_entry_excludes_is_recorded_as_excluded(self):
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": [
            {"id": "legacy-1", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME},
            {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME}]}
        broker = FakeBroker(fail={FOLDER_A: "connection reset by peer"})
        document = self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]],
                                              exclude={"projects": ["legacy-*"]}), previous)
        self.assertEqual(list(self._rows(document)), ["team-a"])
        self.assertEqual(document["excludedProjects"], ["legacy-1"])

    # A number entry in exclude.projects keeps excluding the project it was named to, on every
    # route that reaches the project by ID, after the run that named it.
    NUMBER_ENTRY_SCOPE = {"folders": ["111111111111"], "metricsScopes": ["scope-project"],
                          "exclude": {"projects": ["123456789012"]}}

    def _named_once(self):
        broker = FakeBroker(assets={FOLDER_A: ["foo-project"]}, scopes={"scope-project": ["123456789012"]},
                            names={"123456789012": "foo-project"})
        document = self._build(broker, _scope(**self.NUMBER_ENTRY_SCOPE))
        self.assertEqual((document["projects"], document["excludedProjects"]), ([], ["foo-project"]))
        self.assertEqual(document["numbers"], {"123456789012": "foo-project"})
        return document

    def test_a_number_entry_still_excludes_when_the_naming_call_fails(self):
        previous = self._named_once()
        broker = FakeBroker(assets={FOLDER_A: ["foo-project"]}, scopes={"scope-project": ["123456789012"]},
                            fail={"123456789012": "ERROR: 503 backend unavailable"})
        document = self._build(broker, _scope(**self.NUMBER_ENTRY_SCOPE), previous)
        self.assertEqual((document["projects"], document["excludedProjects"]), ([], ["foo-project"]))
        self.assertNotIn("foo-project", self._probed(broker))

    def test_a_number_entry_still_excludes_when_the_metrics_scope_is_removed(self):
        previous = self._named_once()
        broker = FakeBroker(assets={FOLDER_A: ["foo-project"]})
        declared = _scope(folders=["111111111111"], exclude={"projects": ["123456789012"]})
        for _ in range(2):  # and on the run after, from this run's memo
            previous = self._build(broker, declared, previous)
            self.assertEqual((previous["projects"], previous["excludedProjects"]), ([], ["foo-project"]))

    def test_a_number_entry_added_while_the_metrics_scope_is_down_drops_the_carried_row(self):
        broker = FakeBroker(assets={FOLDER_A: []}, scopes={"scope-project": ["123456789012"]},
                            names={"123456789012": "foo-project"})
        previous = self._build(broker, _scope(metricsScopes=["scope-project"]))
        self.assertEqual([row["id"] for row in previous["projects"]], ["foo-project"])
        broker = FakeBroker(fail={"scope-project": "connection reset by peer"})
        document = self._build(broker, _scope(metricsScopes=["scope-project"],
                                              exclude={"projects": ["123456789012"]}), previous)
        self.assertEqual((document["projects"], document["excludedProjects"]), ([], ["foo-project"]))

    def test_a_number_entry_added_after_the_metrics_scope_stops_reporting_still_excludes(self):
        # The pair outlives the Metrics Scope's report while the project stays in the set
        # through the folder, as the reconcile's row keeps the number.
        broker = FakeBroker(assets={FOLDER_A: ["foo-project"]}, scopes={"scope-project": []})
        for declared in (_scope(folders=["111111111111"], metricsScopes=["scope-project"]),
                         _scope(folders=["111111111111"])):
            # Run 1 named it while not excluded, so it has a row via both sources.
            first = self._build(FakeBroker(assets={FOLDER_A: ["foo-project"]}, scopes={"scope-project": ["123456789012"]},
                                           names={"123456789012": "foo-project"}),
                                _scope(folders=["111111111111"], metricsScopes=["scope-project"]))
            second = self._build(broker, declared, first)
            self.assertEqual(second["numbers"], {"123456789012": "foo-project"})
            third = self._build(FakeBroker(assets={FOLDER_A: ["foo-project"]}, scopes={"scope-project": []}),
                                dict(declared, exclude={"projects": ["123456789012"]}), second)
            self.assertEqual((third["projects"], third["excludedProjects"]), ([], ["foo-project"]), declared)

    def test_a_malformed_previous_carry_does_not_stop_the_run(self):
        for previous in ({"v": 1, "resolvedAt": PREVIOUS_TIME, "projects": [], "carried": 5},
                         {"v": 1, "resolvedAt": PREVIOUS_TIME, "projects": [],
                          "carried": [{"id": 5, "via": [FOLDER_A]}, {"id": ["a"], "via": [FOLDER_A]},
                                      {"id": "x-project", "via": FOLDER_A}],
                          "numbers": ["not", "a", "dict"]}):
            broker = FakeBroker(fail={FOLDER_A: "connection reset by peer"}, assets={FOLDER_B: ["team-b"]})
            document = self._build(broker, _scope(folders=["111111111111", "222222222222"]), previous)
            self.assertEqual(list(self._rows(document)), ["team-b"])

    def test_a_failed_source_counts_its_carried_unnamed_member_against_the_cap(self):
        # maxProjects 3: the management project, then a monitored project no run could name
        # (carried, no row), then a folder of two, which would cross the cap. The next run, with
        # the Metrics Scope down, still counts the unnamed member and keeps the folder out.
        declared = _scope(maxProjects=3, folders=["111111111111"], metricsScopes=["scope-project"])
        broker = FakeBroker(assets={FOLDER_A: ["fa-project", "fb-project"]}, scopes={"scope-project": ["999"]},
                            fail={"999": "ERROR: PERMISSION_DENIED"})
        previous = self._build(broker, declared)
        self.assertEqual(previous["projects"], [])
        self.assertEqual(previous["carried"], [{"id": "999", "via": ["metricsScopes/scope-project"],
                                                "outcome": "denied", "number": "999"}])
        broker = FakeBroker(assets={FOLDER_A: ["fa-project", "fb-project"]},
                            fail={"scope-project": "connection reset by peer"})
        document = self._build(broker, declared, previous)
        self.assertEqual(document["projects"], [])
        self.assertEqual(self._probed(broker), [])

    def test_a_member_also_declared_explicitly_is_carried_not_a_row(self):
        broker = FakeBroker(assets={FOLDER_A: ["explicit-1", "team-a"]})
        document = self._build(broker, _scope(projects=["explicit-1"], folders=["111111111111"]))
        self.assertEqual(list(self._rows(document)), ["team-a"])
        self.assertEqual(document["carried"], [{"id": "explicit-1", "via": ["explicit", FOLDER_A], "outcome": "ok"}])

    def test_when_every_source_fails_the_previous_resolved_at_is_kept(self):
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": [
            {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME}]}
        broker = FakeBroker(fail={FOLDER_A: "caller could not be authenticated",
                                  "host-project": "caller could not be authenticated"})
        document = self._build(broker, _scope(folders=["111111111111"], sharedVpcHosts=["host-project"]), previous)
        self.assertEqual((document["state"], document["resolvedAt"]), (pub.STATE_OK, PREVIOUS_TIME))
        self.assertEqual(document["projects"], previous["projects"])

    def test_when_every_source_fails_with_no_earlier_run_it_publishes_error(self):
        broker = FakeBroker(fail={FOLDER_A: "caller could not be authenticated"})
        declared = _scope(folders=["111111111111"])
        document = self._build(broker, declared)
        self.assertEqual((document["state"], document["projects"], document["memberHash"]),
                         (pub.STATE_ERROR, [], pub.member_hash(declared)))

    def test_every_source_failing_after_an_error_or_too_large_document_stays_error(self):
        broker = FakeBroker(fail={FOLDER_A: "caller could not be authenticated"})
        declared = _scope(folders=["111111111111"])
        first = self._build(broker, declared)
        self.assertEqual(first["state"], pub.STATE_ERROR)
        too_large = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "too-large", "excludedProjects": [], "projects": []}
        for previous in (first, too_large):
            document = self._build(FakeBroker(fail={FOLDER_A: "caller could not be authenticated"}), declared, previous)
            self.assertEqual(document["state"], pub.STATE_ERROR, previous["state"])

    def test_a_number_the_metrics_scope_lists_but_cannot_name_takes_the_naming_outcome(self):
        # The source answered, so this is not a carried row: the naming call's outcome is the
        # row's, as in the reconcile, and confirmedAt does not advance.
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "numbers": {"111": "mon-proj-a"},
                    "projects": [{"id": "mon-proj-a", "via": ["metricsScopes/scope-project"], "outcome": "ok",
                                  "confirmedAt": PREVIOUS_TIME}]}
        broker = FakeBroker(scopes={"scope-project": ["111"]}, fail={"111": "ERROR: PERMISSION_DENIED"})
        document = self._build(broker, _scope(metricsScopes=["scope-project"]), previous)
        self.assertEqual(document["projects"], [{"id": "mon-proj-a", "via": ["metricsScopes/scope-project"],
                                                 "outcome": "denied", "confirmedAt": PREVIOUS_TIME}])
        self.assertEqual(self._probed(broker), [])

    def test_a_number_a_failed_folder_carries_takes_the_naming_outcome_when_the_metrics_scope_answers(self):
        # Carried only when every source in its via failed: here the Metrics Scope answered.
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "numbers": {"111": "mon-proj-a"},
                    "projects": [{"id": "mon-proj-a", "via": [FOLDER_A, "metricsScopes/scope-project"],
                                  "outcome": "ok", "confirmedAt": PREVIOUS_TIME}]}
        broker = FakeBroker(scopes={"scope-project": ["111"]},
                            fail={"111": "ERROR: PERMISSION_DENIED", FOLDER_A: "connection reset by peer"})
        document = self._build(broker, _scope(folders=["111111111111"], metricsScopes=["scope-project"]), previous)
        self.assertEqual(document["projects"], [{"id": "mon-proj-a", "via": [FOLDER_A, "metricsScopes/scope-project"],
                                                 "outcome": "denied", "confirmedAt": PREVIOUS_TIME}])

    def test_a_source_that_answers_empty_restamps_the_document(self):
        # A folder with no clusters today, and a Shared VPC host with no service projects,
        # answered: that is not a failed lookup.
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": []}
        for declared in (_scope(folders=["111111111111"]), _scope(sharedVpcHosts=["host-project"])):
            broker = FakeBroker(assets={FOLDER_A: []}, xpn={"host-project": []})
            document = self._build(broker, declared, previous)
            self.assertEqual((document["state"], document["projects"]), (pub.STATE_OK, []))
            self.assertNotEqual(document["resolvedAt"], PREVIOUS_TIME, declared)

    def test_one_source_that_answers_restamps_the_document(self):
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": []}
        broker = FakeBroker(assets={FOLDER_B: ["team-b"]}, fail={FOLDER_A: "connection reset by peer"})
        document = self._build(broker, _scope(folders=["111111111111", "222222222222"]), previous)
        self.assertNotEqual(document["resolvedAt"], PREVIOUS_TIME)
        self.assertEqual(list(self._rows(document)), ["team-b"])
        # A declaration with no source at all has nothing to fail.
        document = self._build(FakeBroker(), _scope(projects=["explicit-1"]), previous)
        self.assertEqual((document["state"]), pub.STATE_OK)
        self.assertNotEqual(document["resolvedAt"], PREVIOUS_TIME)

    def test_a_listed_member_whose_probe_fails_keeps_its_old_confirmation(self):
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": [
            {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME}]}
        broker = FakeBroker(assets={FOLDER_A: ["team-a"]}, probes={"team-a": DENIED_STDERR})
        document = self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]]), previous)
        self.assertEqual(document["projects"], [
            {"id": "team-a", "via": [FOLDER_A], "outcome": "denied", "confirmedAt": PREVIOUS_TIME}])

    def test_a_project_the_source_no_longer_lists_has_no_row(self):
        previous = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "projects": [
            {"id": "moved-out", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME}]}
        broker = FakeBroker(assets={FOLDER_A: ["team-a"]})
        document = self._build(broker, _scope(folders=[FOLDER_A.split("/")[1]]), previous)
        self.assertEqual(list(self._rows(document)), ["team-a"])

    def test_the_run_has_one_broker_command_in_flight_at_a_time(self):
        broker = FakeBroker(assets={FOLDER_A: ["a-1-project", "a-2-project"], FOLDER_B: ["b-1-project"]},
                            xpn={"host-project": ["svc-proj-a"]}, scopes={"scope-project": ["111", "222"]},
                            names={"111": "mon-proj-a", "222": "mon-proj-b"})
        lock, state = threading.Lock(), {"now": 0, "most": 0}
        answer = broker.__call__

        def counted(argv, *, check, timeout):
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            try:
                time.sleep(OVERLAP_WINDOW_SECONDS)
                return answer(argv, check=check, timeout=timeout)
            finally:
                with lock:
                    state["now"] -= 1

        with mock.patch.object(rec, "COMMAND_RUNNER", counted):
            document = pub.build_document(_scope(folders=["111111111111", "222222222222"], sharedVpcHosts=["host-project"],
                                                 metricsScopes=["scope-project"]), MANAGEMENT, None)
        self.assertEqual(len(document["projects"]), 6)
        self.assertEqual(state["most"], 1)

    def test_a_source_that_hangs_does_not_hold_the_run_past_its_budget(self):
        released = threading.Event()
        self.addCleanup(released.set)
        broker = FakeBroker(assets={FOLDER_A: ["team-a"]})
        answer = broker.__call__

        def hanging(argv, *, check, timeout):
            if argv[3] == f"--scope={FOLDER_A}":
                released.wait(SYNC_FAILURE_TIMEOUT_SECONDS)
            return answer(argv, check=check, timeout=timeout)

        started = time.monotonic()
        with mock.patch.object(rec, "_list_budget_seconds", return_value=SHORT_BUDGET_SECONDS), \
                mock.patch.object(rec, "LIST_GRACE_SECONDS", 0), mock.patch.object(rec, "COMMAND_RUNNER", hanging):
            document = pub.build_document(_scope(folders=[FOLDER_A.split("/")[1]]), MANAGEMENT, None)
        self.assertLess(time.monotonic() - started, SYNC_FAILURE_TIMEOUT_SECONDS)
        # The one source did not answer and no earlier run resolved.
        self.assertEqual((document["state"], document["projects"]), (pub.STATE_ERROR, []))

    def test_a_declaration_without_a_scope_block_resolves_nothing(self):
        broker = FakeBroker()
        document = self._build(broker, {"present": False, "folders": ["111111111111"]})
        self.assertEqual((document["state"], document["projects"], broker.calls), (pub.STATE_OK, [], []))


class MainTest(unittest.TestCase):
    PREVIOUS = {"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok", "excludedProjects": ["legacy-1"], "projects": [
        {"id": "team-a", "via": [FOLDER_A], "outcome": "ok", "confirmedAt": PREVIOUS_TIME}]}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scope_file = Path(tmp.name) / "scope.json"
        self.scope_file.write_text(json.dumps(_scope(folders=["111111111111"])))
        self.env = {"POD_NAMESPACE": "kube-agents", "KUBEAGENTS_RESOLVED_SCOPE_CONFIGMAP": "pa-resolved-scope",
                    "KUBEAGENTS_SCOPE_FILE": str(self.scope_file), "KUBEAGENTS_MANAGEMENT_PROJECT": MANAGEMENT}
        for p in (mock.patch.object(rec, "log"), mock.patch.object(pub, "log"),
                  mock.patch.object(rec, "COMMAND_RUNNER", None)):
            p.start()
            self.addCleanup(p.stop)

    def _main(self, env=None, read=None, write=None, broker=None):
        broker = broker or FakeBroker(assets={FOLDER_A: ["team-a"]})
        with mock.patch.dict(pub.os.environ, env if env is not None else self.env, clear=True), \
                mock.patch.object(pub, "read_previous", **(read or {"return_value": (self.PREVIOUS, "42")})), \
                mock.patch.object(pub, "write_document", **(write or {})) as written, \
                mock.patch.object(pub, "broker_run", broker):
            code = pub.main()
        document = json.loads(written.call_args[0][2]) if written.called else None
        return code, document, written

    def test_a_run_writes_at_the_version_it_read_and_exits_0(self):
        code, document, written = self._main()
        self.assertEqual(code, pub.EXIT_WRITTEN)
        self.assertEqual(written.call_args[0][:2], ("kube-agents", "pa-resolved-scope"))
        self.assertEqual(written.call_args[0][3], "42")
        self.assertEqual(document["state"], pub.STATE_OK)
        self.assertEqual(document["projects"][0]["id"], "team-a")

    def test_rows_that_read_denied_still_exit_0(self):
        code, document, _ = self._main(broker=FakeBroker(assets={FOLDER_A: ["team-a"]},
                                                         probes={"team-a": DENIED_STDERR}))
        self.assertEqual((code, document["projects"][0]["outcome"]), (pub.EXIT_WRITTEN, "denied"))

    def test_an_unreadable_scope_publishes_error_and_keeps_the_previous_rows(self):
        self.scope_file.write_text("{not json")
        self.PREVIOUS = dict(self.PREVIOUS, numbers={"111": "mon-proj-a"},
                             carried=[{"id": "999", "via": ["metricsScopes/scope-project"], "outcome": "denied"}])
        code, document, _ = self._main()
        self.assertEqual((document["numbers"], document["carried"]), (self.PREVIOUS["numbers"], self.PREVIOUS["carried"]))
        self.assertEqual(code, pub.EXIT_WRITTEN)
        self.assertEqual((document["state"], document["memberHash"]), (pub.STATE_ERROR, ""))
        self.assertEqual(document["projects"], self.PREVIOUS["projects"])
        self.assertEqual(document["excludedProjects"], ["legacy-1"])

    def test_an_empty_or_unset_management_project_still_reserves_the_first_cap_slot(self):
        # The reconcile then counts the broker's gcloud config project, which the Job cannot see,
        # so the slot stays reserved: at maxProjects 2 a folder of two crosses the cap whether
        # the management project is named or not; at 3 it fits. Neither case is an error.
        assets = {FOLDER_A: ["team-a", "team-b"]}
        unset = {k: v for k, v in self.env.items() if k != "KUBEAGENTS_MANAGEMENT_PROJECT"}
        for cap, want in ((2, []), (3, ["team-a", "team-b"])):
            self.scope_file.write_text(json.dumps(_scope(maxProjects=cap, folders=["111111111111"])))
            for env in (self.env, dict(self.env, KUBEAGENTS_MANAGEMENT_PROJECT=""), unset):
                code, document, _ = self._main(env=env, broker=FakeBroker(assets=assets))
                self.assertEqual((code, document["state"]), (pub.EXIT_WRITTEN, pub.STATE_OK))
                self.assertEqual([row["id"] for row in document["projects"]], want, (cap, env))

    def test_an_error_run_over_a_malformed_previous_row_writes_and_exits_0(self):
        self.scope_file.write_text("{not json")
        raw = json.dumps({"v": 1, "resolvedAt": PREVIOUS_TIME, "state": "ok",
                          "projects": [{"id": "x-project", "via": [FOLDER_A]}]})
        code, document, _ = self._main(read={"return_value": (pub.parse_document(raw), "42")})
        self.assertEqual((code, document["state"], document["projects"]), (pub.EXIT_WRITTEN, pub.STATE_ERROR, []))

    def test_a_configmap_that_cannot_be_read_or_written_exits_non_zero(self):
        code, _, written = self._main(read={"side_effect": urllib.error.URLError("no route")})
        self.assertEqual(code, pub.EXIT_NOT_WRITTEN)
        written.assert_not_called()
        conflict = urllib.error.HTTPError("u", 409, "Conflict", {}, None)
        self.addCleanup(conflict.close)
        code, _, _ = self._main(write={"side_effect": conflict})
        self.assertEqual(code, pub.EXIT_NOT_WRITTEN)

    def test_without_its_namespace_or_configmap_it_writes_nothing(self):
        env = {k: v for k, v in self.env.items() if k != "POD_NAMESPACE"}
        code, _, written = self._main(env=env)
        self.assertEqual(code, pub.EXIT_NOT_WRITTEN)
        written.assert_not_called()


if __name__ == "__main__":
    unittest.main()
