"""collector_run: the one owner of the collectors' subprocess plumbing, and the
check that no collector grows a copy of it back."""

import ast
import copy
import functools
import hashlib
import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import collector_run as cr  # noqa: E402
import credential_proxy_client as client  # noqa: E402
import command_policy  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
# Every script under a skill's scripts/ tree, nested packages included.
SKILL_SCRIPTS = sorted(
    p for p in REPO.glob("agents/platform/skills/*/scripts/**/*.py") if not p.name.startswith("test_")
)
# The collectors that bind the shared names, with the timeout constant each binds.
COLLECTORS = {
    "collect": "DEFAULT_TIMEOUT_S",
    "fleet_waste": "DEFAULT_TIMEOUT_S",
    "fleet_stockout": "DEFAULT_TIMEOUT_S",
    "fleet_drift": "DEFAULT_TIMEOUT_S",
    "fleet_health": "DEFAULT_TIMEOUT_S",
    "patch_readiness": "DEFAULT_TIMEOUT_S",
    "upgrade_retrospective": "GCLOUD_TIMEOUT_S",
    "compute_fleet_audit": "DEFAULT_TIMEOUT_S",
}
OWNED_NAMES = ("Run", "default_run", "_text", "run_and_gate", "paged_dump", "Discovery")
# collect.py keeps its own run_and_gate: it adds the kubeconfig env and the
# `.items` gate, a different contract from the JSON gate here.
LOCAL_EXCEPTIONS = {("collect.py", "run_and_gate")}
TIMED_OUT = subprocess.TimeoutExpired(["gcloud"], 1, output=b"partial", stderr=b"WARNING: zone did not respond")


def load_collector(name):
    for path in SKILL_SCRIPTS:
        if path.stem == name:
            sys.path.insert(0, str(path.parent))
            try:
                return __import__(name)
            finally:
                sys.path.pop(0)
    raise AssertionError(f"no collector named {name}")


def _is_collector_run_attr(node, attr=None):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "collector_run" and (attr is None or node.attr == attr))


def binds_shared(value):
    """True for the two admitted binding shapes: `collector_run.<name>` and
    `functools.partial(collector_run.default_run, ...)`; any other call is a copy."""
    if _is_collector_run_attr(value):
        return True
    if isinstance(value, ast.Call) and value.args and _is_collector_run_attr(value.args[0], "default_run"):
        callee = value.func
        return ((isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
                 and callee.value.id == "functools" and callee.attr == "partial")
                or (isinstance(callee, ast.Name) and callee.id == "partial"))
    return False


def copies_in(paths, root=REPO):
    """Skill scripts that define an owned name: a `def`, a `class`, or an
    assignment such as `Run = namedtuple(...)` that does not bind the shared one."""
    copies = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in OWNED_NAMES:
                if (path.name, node.name) not in LOCAL_EXCEPTIONS:
                    copies.setdefault(where, []).append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in OWNED_NAMES and not binds_shared(node.value):
                        copies.setdefault(where, []).append(f"{target.id} = ...")
    return copies


def strays_in(paths, root=REPO):
    """Places that reach collector_run other than a module-level `import collector_run`
    and the module-level binding lines: a direct `collector_run.<name>` use, an aliased
    import, or a `from collector_run import ...` anywhere."""
    stray = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(root))
        allowed = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                allowed.update(id(n) for n in ast.walk(node) if isinstance(n, ast.Attribute))
            if isinstance(node, ast.Import) and all(a.name == "collector_run" and a.asname is None for a in node.names):
                allowed.add(id(node))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "collector_run" and id(node) not in allowed):
                stray.setdefault(where, []).append(f"{node.lineno}:{node.attr}")
            elif isinstance(node, ast.Import) and id(node) not in allowed and any(a.name == "collector_run" for a in node.names):
                stray.setdefault(where, []).append(f"{node.lineno}:import collector_run outside the module-level plain import")
            elif isinstance(node, ast.ImportFrom) and node.module == "collector_run":
                stray.setdefault(where, []).append(f"{node.lineno}:from collector_run import ...")
    return stray


def bound_timeout_name(mod):
    """The constant the collector's `default_run = functools.partial(...)` line names."""
    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "default_run" for x in node.targets):
            for kw in getattr(node.value, "keywords", []):
                if kw.arg == "timeout" and isinstance(kw.value, ast.Name):
                    return kw.value.id
    return None


def paged_dump_over(legacy_run=None):
    """A `paged_dump` for a collector test whose fake `run` still answers the
    one-shot `kubectl get a,b,c -A -o json` dump. The real `collector_run.paged_dump`
    runs and issues its `--raw` page argvs; each page is answered from the
    fake's dump, asked for once per dump (so a fake counting calls sees one, as
    before), split by kind, with the `kind` and `apiVersion` a raw list omits
    removed so the stamping is exercised. A fake answering the raw argv itself
    (a `--raw` in argv) is left alone. Patched over the collector's own
    `paged_dump` binding by its test module; `run` is the fake the test passed."""

    def paged_dump(resources, *, run, query=None, **run_kwargs):
        resources = list(resources)
        legacy_argv = ["kubectl", "get", ",".join(resources), "-A", "-o", "json"]
        answered = {}

        def raw_run(argv, **kwargs):
            if "dump" not in answered:
                answered["dump"] = run(legacy_argv, **kwargs)
            legacy = answered["dump"]
            try:
                parsed = json.loads(legacy.stdout) if legacy.rc == 0 and legacy.stdout.strip() else None
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
                return legacy._replace(argv=argv)
            path = argv[3].partition("?")[0]
            listed = next(k for k in cr.LIST_KINDS.values() if k.path == path)
            items = []
            for item in parsed["items"]:
                # A fake's item with no `kind` belongs to a single-kind read,
                # as a raw list's items do.
                if isinstance(item, dict) and (item.get("kind") == listed.kind or (len(resources) == 1 and "kind" not in item)):
                    item = copy.deepcopy(item)
                    item.pop("kind", None)
                    item.pop("apiVersion", None)
                    items.append(item)
            return cr.Run(argv, 0, json.dumps({"metadata": {}, "items": items}), legacy.stderr, legacy.duration_s)

        return cr.paged_dump(resources, run=raw_run, query=query, **run_kwargs)

    return paged_dump


def page(items, continue_token="", managed=False):
    """One raw list page as the API server returns it: no `kind` on the items,
    `managedFields` on each when asked, and a continue token in the metadata."""
    out = []
    for name in items:
        metadata = {"name": name, "namespace": "ns"}
        if managed:
            metadata["managedFields"] = [{"manager": "kubectl", "operation": "Apply"}]
        out.append({"metadata": metadata, "spec": {}})
    metadata = {"resourceVersion": "1"}
    if continue_token:
        metadata["continue"] = continue_token
    return json.dumps({"kind": "PodList", "apiVersion": "v1", "metadata": metadata, "items": out})


class PagedReadTest(unittest.TestCase):
    """`paged_dump`: one kind at a time, one page at a time, merged into the
    `List` shape the collectors parse, and closed on any page that fails."""

    def scripted(self, replies):
        """A `run` that answers each `--raw` argv from `replies` by its path and
        continue token, recording the argvs it was asked."""
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            path, _, query = argv[3].partition("?")
            params = dict(p.split("=", 1) for p in query.split("&"))
            key = (path, params.get("continue", ""))
            reply = replies[key]
            return reply if isinstance(reply, cr.Run) else cr.Run(argv, 0, reply, "", 0.01)

        return run, calls

    def test_pages_follow_continue_and_merge_in_order(self):
        run, calls = self.scripted({
            ("/api/v1/pods", ""): page(["a", "b"], continue_token="t1"),
            ("/api/v1/pods", "t1"): page(["c"], continue_token="t2"),
            ("/api/v1/pods", "t2"): page(["d"]),
        })
        parsed, result = cr.paged_dump(["pods"], run=run, env={"KUBECONFIG": "/k"})
        self.assertEqual([i["metadata"]["name"] for i in parsed["items"]], ["a", "b", "c", "d"])
        self.assertEqual(("List", "v1"), (parsed["kind"], parsed["apiVersion"]))
        self.assertEqual(
            [argv[3] for argv, _ in calls],
            ["/api/v1/pods?limit=500", "/api/v1/pods?limit=500&continue=t1", "/api/v1/pods?limit=500&continue=t2"],
        )
        # `env` and the rest of the caller's run keywords reach every page.
        self.assertTrue(all(kwargs == {"env": {"KUBECONFIG": "/k"}} for _, kwargs in calls))
        self.assertEqual((0, ["kubectl", "get", "--raw", "/api/v1/pods?limit=500"]), (result.rc, result.argv))
        self.assertIn("4 items in 3 pages; sha256 ", result.stdout)

    def test_items_are_stamped_with_kind_and_api_version_and_lose_managed_fields(self):
        run, _ = self.scripted({
            ("/apis/apps/v1/deployments", ""): page(["web"], managed=True),
            ("/api/v1/pods", ""): page(["web-1"], managed=True),
        })
        parsed, _ = cr.paged_dump(["deploy", "pod"], run=run)
        kinds = [(i["kind"], i["apiVersion"]) for i in parsed["items"]]
        self.assertEqual([("Deployment", "apps/v1"), ("Pod", "v1")], kinds)
        self.assertTrue(all("managedFields" not in i["metadata"] for i in parsed["items"]))

    def test_an_item_that_names_its_own_kind_keeps_it(self):
        run, _ = self.scripted({
            ("/api/v1/pods", ""): json.dumps({"items": [{"kind": "Pod", "apiVersion": "v1", "metadata": {"name": "x"}}]}),
        })
        parsed, _ = cr.paged_dump(["pods"], run=run)
        self.assertEqual([("Pod", "v1")], [(i["kind"], i["apiVersion"]) for i in parsed["items"]])

    def test_a_failed_middle_page_closes_the_whole_dump(self):
        cut = cr.Run(["kubectl"], 74, '{"items": [{"metadata": {"name": "c"', "credential proxy output truncated at 8388608 bytes (kept 8388608 bytes of stdout; exit 74)", 0.5)
        run, calls = self.scripted({
            ("/apis/apps/v1/deployments", ""): page(["web"]),
            ("/api/v1/pods", ""): page(["a"], continue_token="t1"),
            ("/api/v1/pods", "t1"): cut,
            ("/api/v1/pods", "t2"): page(["z"]),
        })
        parsed, result = cr.paged_dump(["deployments", "pods"], run=run)
        self.assertIsNone(parsed)
        self.assertEqual(74, result.rc)
        self.assertIn("credential proxy output truncated", result.stderr)
        # Nothing after the failed page is read.
        self.assertEqual(3, len(calls))

    def test_a_cut_page_from_an_older_shim_closes_the_dump_too(self):
        # rc 0 with a partial payload: the JSON gate, not the exit code, catches it.
        run, _ = self.scripted({("/api/v1/pods", ""): '{"items": [{"metadata": {"name": "a'})
        parsed, result = cr.paged_dump(["pods"], run=run)
        self.assertIsNone(parsed)
        self.assertEqual(0, result.rc)

    def test_a_page_that_is_not_a_list_closes_the_dump(self):
        for body in ('{"kind": "Status", "code": 403}', "[]", '"x"', "", "   "):
            with self.subTest(body=body):
                run, _ = self.scripted({("/api/v1/pods", ""): body})
                parsed, result = cr.paged_dump(["pods"], run=run)
                self.assertIsNone(parsed)
                if body.strip():
                    self.assertIn(cr.PAGE_NOT_A_LIST_TEXT, result.stderr)

    def test_a_repeated_continue_token_ends_the_read_closed(self):
        run, calls = self.scripted({
            ("/api/v1/pods", ""): page(["a"], continue_token="t1"),
            ("/api/v1/pods", "t1"): page(["b"], continue_token="t1"),
        })
        parsed, result = cr.paged_dump(["pods"], run=run)
        self.assertIsNone(parsed)
        self.assertEqual(cr.PAGE_LOOP_TEXT, result.stderr)
        self.assertEqual(2, len(calls))

    def test_the_page_ceiling_ends_an_endless_server_closed(self):
        counter = {"n": 0}

        def run(argv, **kwargs):
            counter["n"] += 1
            return cr.Run(argv, 0, page(["x"], continue_token=f"t{counter['n']}"), "", 0.0)

        with mock.patch.object(cr, "MAX_PAGES", 5):
            parsed, result = cr.paged_dump(["pods"], run=run)
        self.assertIsNone(parsed)
        self.assertEqual(cr.PAGE_CEILING_TEXT, result.stderr)
        self.assertEqual(5, counter["n"])

    def test_the_summary_digest_follows_the_content(self):
        one, _ = self.scripted({("/api/v1/pods", ""): page(["a"])})
        same, _ = self.scripted({("/api/v1/pods", ""): page(["a"])})
        other, _ = self.scripted({("/api/v1/pods", ""): page(["b"])})
        digest = lambda run: cr.paged_dump(["pods"], run=run)[1].stdout.rsplit(" ", 1)[1]  # noqa: E731
        self.assertEqual(digest(one), digest(same))
        self.assertNotEqual(digest(one), digest(other))
        self.assertEqual(hashlib.sha256(page(["a"]).encode()).hexdigest(), digest(one))

    def test_a_query_rides_on_every_page_and_nodes_page_smaller(self):
        self.assertEqual(
            ["kubectl", "get", "--raw", "/api/v1/events?limit=500&fieldSelector=type%3DWarning&continue=T"],
            cr.list_argv("events", continue_token="T", query={"fieldSelector": "type=Warning"}),
        )
        self.assertEqual("/api/v1/nodes?limit=100", cr.list_argv("nodes")[3])
        self.assertEqual("/api/v1/nodes?limit=7", cr.list_argv("no", limit=7)[3])

    def test_a_query_rides_on_a_continue_page(self):
        run, calls = self.scripted({
            ("/api/v1/events", ""): page(["e1"], continue_token="t1"),
            ("/api/v1/events", "t1"): page(["e2"]),
        })
        parsed, _ = cr.paged_dump(["events"], run=run, query={"fieldSelector": "type=Warning"})
        self.assertEqual(["e1", "e2"], [i["metadata"]["name"] for i in parsed["items"]])
        self.assertEqual(
            ["/api/v1/events?limit=500&fieldSelector=type%3DWarning",
             "/api/v1/events?limit=500&fieldSelector=type%3DWarning&continue=t1"],
            [argv[3] for argv, _ in calls],
        )

    def test_every_kind_list_a_collector_pages_resolves(self):
        """The collectors' kind lists are constants; a misspelt one fails here,
        not inside a sweep."""
        lists = []
        for name in ("collect", "fleet_waste", "fleet_stockout", "upgrade_retrospective"):
            mod = load_collector(name)
            if name == "collect":
                lists += [mod.DUMP_COMMAND_KINDS, mod.COMPLIANCE_DUMP_KINDS, mod.RBAC_DUMP_KINDS,
                          mod.NETPOL_DUMP_KINDS, mod.SERVICE_DUMP_KINDS, mod.SERVICEACCOUNT_DUMP_KINDS]
            elif name == "fleet_waste":
                lists.append(mod.CLUSTER_DUMP_KINDS)
            elif name == "fleet_stockout":
                lists.append(",".join(mod.PAGED_DUMP_KINDS))
            else:
                lists += [kinds for _, kinds, _ in mod.KUBECTL_READS]
        report = load_collector("fleet_upgrade_report")
        lists += [report.KUBECTL_RESOURCES, report.KUBECTL_WEBHOOK_RESOURCES,
                  *(resources for _, resources, mode in report.LATER_READS if mode != report.READ_MODE_NODE_JSONPATH)]
        self.assertTrue(lists)
        for kinds in lists:
            for kind in kinds.split(","):
                with self.subTest(kind=kind):
                    self.assertIn(cr.canonical_resource(kind), cr.LIST_KINDS)

    def test_the_collectors_spellings_resolve_and_unknown_kinds_do_not(self):
        for spelled, canonical in (("deploy", "deployments"), ("sts", "statefulsets"), ("ds", "daemonsets"),
                                   ("cronjob", "cronjobs"), ("pod", "pods"), ("pdb", "poddisruptionbudgets"),
                                   ("svc", "services"), ("ns", "namespaces"), ("sa", "serviceaccounts"),
                                   ("pvc", "persistentvolumeclaims"), ("pv", "persistentvolumes"), ("hpa", "horizontalpodautoscalers"),
                                   ("netpol", "networkpolicies"), ("resourcequota", "resourcequotas"), ("limitrange", "limitranges"),
                                   ("ingress", "ingresses"), ("storageclass", "storageclasses"), ("node", "nodes"),
                                   ("daemonset", "daemonsets"), ("namespace", "namespaces"), ("networkpolicy", "networkpolicies"),
                                   ("statefulset", "statefulsets")):
            self.assertEqual(canonical, cr.canonical_resource(spelled))
        for unknown in ("secrets", "secret", "computeclasses", "gcp", "ccnp", "rootsyncs", "configmaps"):
            with self.subTest(unknown=unknown):
                with self.assertRaises(ValueError):
                    cr.canonical_resource(unknown)

    def test_the_command_text_names_each_kinds_first_page(self):
        # A chain exports the kubeconfig once, in a subshell, so every read
        # runs against the audited cluster when the line is pasted unchanged,
        # the pasting shell keeps its own context, and the path is spelled
        # once however many kinds the chain reads (the published command cap).
        self.assertEqual(
            "( export KUBECONFIG=/k/c; kubectl get --raw '/apis/apps/v1/deployments?limit=500'; kubectl get --raw '/api/v1/pods?limit=500' )",
            cr.paged_command_text(["deploy", "pods"], kubeconfig="/k/c"),
        )
        # One kind is one simple command, and a leading assignment scopes to it.
        self.assertEqual(
            "KUBECONFIG=/k/c kubectl get --raw '/api/v1/pods?limit=500'",
            cr.paged_command_text(["pods"], kubeconfig="/k/c"),
        )
        self.assertEqual(
            "kubectl get --raw '/apis/apps/v1/deployments?limit=500'; kubectl get --raw '/api/v1/pods?limit=500'",
            cr.paged_command_text(["deploy", "pods"]),
        )
        # A path the shell would split is quoted, in either form.
        self.assertEqual(
            "( export KUBECONFIG='/k dir/c'; kubectl get --raw '/apis/apps/v1/deployments?limit=500'; kubectl get --raw '/api/v1/pods?limit=500' )",
            cr.paged_command_text(["deploy", "pods"], kubeconfig="/k dir/c"),
        )

    def test_the_recorded_chain_runs_every_read_under_the_kubeconfig_and_leaves_the_shell_alone(self):
        """The chain as bash runs it: a fake kubectl prints the KUBECONFIG it saw, each read
        sees the recorded path, and the caller's own KUBECONFIG is what it was after."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "kubectl")
            with open(fake, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\necho \"$KUBECONFIG\"\n")
            os.chmod(fake, 0o755)
            line = cr.paged_command_text(["deploy", "pods", "nodes"], kubeconfig="/k/c")
            proc = subprocess.run(
                ["bash", "-c", line + "; echo \"after=$KUBECONFIG\""],
                capture_output=True, text=True, check=True,
                env={"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "KUBECONFIG": "outer"},
            )
        self.assertEqual(["/k/c", "/k/c", "/k/c", "after=outer"], proc.stdout.split())

    def test_the_legacy_dump_adapter_feeds_the_real_paging(self):
        calls = []

        def legacy(argv, **kwargs):
            calls.append(argv)
            items = [
                {"kind": "Deployment", "apiVersion": "apps/v1", "metadata": {"name": "web", "managedFields": [1]}},
                {"kind": "Pod", "apiVersion": "v1", "metadata": {"name": "web-1"}},
                {"kind": "ComputeClass", "metadata": {"name": "cc"}},
            ]
            return cr.Run(argv, 0, json.dumps({"kind": "List", "items": items}), "", 0.0)

        parsed, result = paged_dump_over()(["deploy", "pods"], run=legacy, env={"A": "b"})
        self.assertEqual([["kubectl", "get", "deploy,pods", "-A", "-o", "json"]], calls)
        self.assertEqual([("Deployment", "web"), ("Pod", "web-1")], [(i["kind"], i["metadata"]["name"]) for i in parsed["items"]])
        self.assertNotIn("managedFields", parsed["items"][0]["metadata"])
        self.assertEqual("--raw", result.argv[2])


class PagedReadsAreWithinThePolicyTest(unittest.TestCase):
    """The table here and the credential proxy's allowlist are one set, and
    every argv a paged read issues passes the policy as it stands."""

    def test_the_kind_table_is_the_policy_allowlist(self):
        self.assertEqual({k.path for k in cr.LIST_KINDS.values()}, command_policy.KUBECTL_RAW_COLLECTIONS)

    def test_every_page_argv_is_admitted(self):
        for resource in cr.LIST_KINDS:
            for argv in (
                cr.list_argv(resource),
                cr.list_argv(resource, continue_token="eyJ2IjoibWV0YS5rOHMuaW8vdjEiLCJydiI6MX0"),
                cr.list_argv(resource, query={"fieldSelector": "type=Warning", "labelSelector": "a.b/c!=d,e in (f,g)"}),
            ):
                with self.subTest(argv=argv):
                    decision = command_policy.evaluate(argv)
                    self.assertTrue(decision.allowed, decision.message)

    def test_secrets_are_in_neither_table(self):
        self.assertNotIn("secrets", cr.LIST_KINDS)
        self.assertNotIn("secrets", cr.RESOURCE_ALIASES.values())
        self.assertFalse(any(path.endswith("/secrets") for path in command_policy.KUBECTL_RAW_COLLECTIONS))

    def test_every_alias_names_a_listed_kind(self):
        for alias, canonical in cr.RESOURCE_ALIASES.items():
            self.assertIn(canonical, cr.LIST_KINDS, alias)


class DefaultRunTest(unittest.TestCase):
    def test_a_timeout_that_wrote_output_returns_text(self):
        with mock.patch.object(subprocess, "run", side_effect=TIMED_OUT):
            result = cr.default_run(["gcloud"], timeout=1)
        self.assertEqual((result.rc, result.stdout, result.stderr), (cr.TIMEOUT_RC, "partial", "WARNING: zone did not respond"))

    def test_a_timeout_that_wrote_nothing_returns_empty_text(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["gcloud"], 1)):
            result = cr.default_run(["gcloud"], timeout=1)
        self.assertEqual((result.rc, result.stdout, result.stderr), (cr.TIMEOUT_RC, "", ""))

    def test_a_completed_run_passes_its_text_through(self):
        done = subprocess.CompletedProcess(["gcloud"], 3, stdout="[]", stderr="warn")
        with mock.patch.object(subprocess, "run", return_value=done):
            result = cr.default_run(["gcloud"], timeout=5)
        self.assertEqual((result.rc, result.stdout, result.stderr), (3, "[]", "warn"))

    def test_a_missing_binary_is_a_failed_run_not_a_raise(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError(2, "No such file", "gcloud")):
            result = cr.default_run(["gcloud"], timeout=5)
        self.assertEqual((result.rc, result.stdout), (-1, ""))
        self.assertIn("No such file", result.stderr)

    def test_env_and_timeout_reach_the_child(self):
        done = subprocess.CompletedProcess(["gcloud"], 0, stdout="", stderr="")
        given = {"KUBECONFIG": "/k"}
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            cr.default_run(["gcloud"], env=given, timeout=7)
        self.assertEqual(run.call_args.kwargs["env"], {"KUBECONFIG": "/k", cr.SHIM_RETRY_DEADLINE_ENV: "0"})
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(given, {"KUBECONFIG": "/k"}, "the caller's dict was written to")

    def test_the_timeout_has_no_default(self):
        with self.assertRaises(TypeError):
            cr.default_run(["gcloud"])


class ShimDeadlineTest(unittest.TestCase):
    """Every child `default_run` starts is told the shim's retry deadline, derived from the
    cap: a read with no `env` inherits the process environment plus the variable, a read
    handed one gets its dict plus the variable, and a caller that named one keeps it."""

    NAME = cr.SHIM_RETRY_DEADLINE_ENV
    DONE = subprocess.CompletedProcess(["gcloud"], 0, stdout="", stderr="")

    def child_env_of(self, **kwargs):
        with mock.patch.object(subprocess, "run", return_value=self.DONE) as run:
            cr.default_run(["gcloud"], **kwargs)
        return run.call_args.kwargs["env"]

    def test_the_variable_is_the_shim_s(self):
        self.assertEqual(client.RETRY_DEADLINE_ENV, cr.SHIM_RETRY_DEADLINE_ENV)

    def test_the_deadline_is_the_cap_less_the_gcloud_run_bound_and_never_negative(self):
        self.assertEqual(300, cr.BROKER_GCLOUD_RUN_S)
        self.assertEqual([0, 0, 0, 100], [cr.shim_retry_deadline(t) for t in (60, 240, 300, 400)])
        self.assertEqual(cr.shim_retry_deadline(400), cr.shim_retry_deadline(400.9), "a float cap is read as its whole seconds")

    def test_a_read_with_no_env_inherits_the_process_environment_plus_the_deadline(self):
        with mock.patch.dict(os.environ, {"COLLECTOR_TEST_MARK": "1"}):
            os.environ.pop(self.NAME, None)
            env = self.child_env_of(timeout=120)
            self.assertNotIn(self.NAME, os.environ, "the process environment was written to")
            self.assertEqual({**os.environ, self.NAME: "0"}, env)
        self.assertEqual("1", env["COLLECTOR_TEST_MARK"])

    def test_a_read_handed_an_env_gets_it_plus_the_deadline(self):
        env = self.child_env_of(env={"KUBECONFIG": "/k", "PATH": "/bin"}, timeout=400)
        self.assertEqual({"KUBECONFIG": "/k", "PATH": "/bin", self.NAME: "100"}, env)

    def test_a_caller_s_own_deadline_is_kept(self):
        self.assertEqual("20", self.child_env_of(env={self.NAME: "20"}, timeout=60)[self.NAME])
        with mock.patch.dict(os.environ, {self.NAME: "20"}):
            self.assertEqual("20", self.child_env_of(timeout=60)[self.NAME])

    def test_no_retry_fits_under_the_deadline_of_any_collector_s_cap(self):
        shortest = client.BROKER_ADMISSION_WAIT_SECONDS + client.RETRY_JITTER_FLOOR * client.RETRY_BACKOFF_BASE_SECONDS
        for name, timeout_const in COLLECTORS.items():
            with self.subTest(collector=name):
                deadline = float(cr.shim_retry_deadline(getattr(load_collector(name), timeout_const)))
                self.assertGreater(shortest, deadline)
                self.assertIsNone(client.retry_wait(1, 0.0, None, random.Random(1), lambda: 0.0, deadline=deadline))
                self.assertIsNone(client.retry_wait(1, 0.0, "5", random.Random(1), lambda: 0.0, deadline=deadline), "the gate's shortest wait fits")


class RunAndGateTest(unittest.TestCase):
    def run_of(self, rc, stdout="", stderr=""):
        return cr.Run(["gcloud"], rc, stdout, stderr, 0.0)

    def test_a_failed_command_gates_closed(self):
        parsed, result = cr.run_and_gate(["gcloud"], run=lambda argv, **_: self.run_of(1, "[]"))
        self.assertEqual((parsed, result.rc), (None, 1))

    def test_empty_and_non_json_output_gate_closed(self):
        for stdout in ("", "   ", "not json"):
            parsed, _ = cr.run_and_gate(["gcloud"], run=lambda argv, **_: self.run_of(0, stdout))
            self.assertIsNone(parsed, stdout)

    def test_json_output_is_parsed(self):
        parsed, _ = cr.run_and_gate(["gcloud"], run=lambda argv, **_: self.run_of(0, '{"a": [1]}'))
        self.assertEqual(parsed, {"a": [1]})

    def test_env_is_passed_only_when_given(self):
        calls = []

        def run(argv, **kw):
            calls.append(kw)
            return self.run_of(0, "[]")

        cr.run_and_gate(["gcloud"], run=run)
        cr.run_and_gate(["gcloud"], run=run, env={"A": "1"})
        self.assertEqual(calls, [{}, {"env": {"A": "1"}}])


class CollectorsBindTheSharedNamesTest(unittest.TestCase):
    """Each collector addresses the shared plumbing through its own module name,
    so its tests and fakes keep working, and binds its own timeout."""

    def test_every_collector_binds_run_and_default_run(self):
        for name, timeout_const in COLLECTORS.items():
            with self.subTest(collector=name):
                mod = load_collector(name)
                self.assertIs(mod.Run, cr.Run)
                self.assertIsInstance(mod.default_run, functools.partial)
                self.assertIs(mod.default_run.func, cr.default_run)
                self.assertEqual(mod.default_run.keywords, {"timeout": getattr(mod, timeout_const)})
                self.assertEqual(bound_timeout_name(mod), timeout_const)
                if hasattr(mod, "Discovery"):
                    self.assertIs(mod.Discovery, cr.Discovery)
                if name != "collect" and hasattr(mod, "run_and_gate"):
                    self.assertIs(mod.run_and_gate, cr.run_and_gate)

    def test_no_skill_script_defines_a_copy_of_the_shared_names(self):
        self.assertTrue(any(p.parent.name != "scripts" for p in SKILL_SCRIPTS), "the scan does not reach nested packages")
        self.assertEqual({}, copies_in(SKILL_SCRIPTS), "these skill scripts grew a copy of a name collector_run owns")

    def test_collectors_reach_the_shared_names_only_through_their_bindings(self):
        """A body that reaches the module directly skips the collector's binding, so
        a test that patches `<collector>.default_run` would run the real subprocess
        and still pass."""
        self.assertEqual({}, strays_in(SKILL_SCRIPTS), "collector_run reached outside the module-level import and bindings")

    def test_the_guards_refuse_the_shapes_they_exist_for(self):
        """One planted file per hole: a copy in a nested package, a namedtuple copy,
        a wrapped binding that is not `functools.partial`, a from-import alias
        inside a function, and an aliased module import."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            nested = root / "skill" / "scripts" / "rules"
            nested.mkdir(parents=True)
            (nested / "deep.py").write_text("def default_run(argv, *, timeout=1):\n    return None\n")
            (root / "skill" / "scripts" / "tuple.py").write_text(
                "import collections\nRun = collections.namedtuple('Run', 'argv rc stdout stderr duration_s')\n")
            (root / "skill" / "scripts" / "fromimp.py").write_text(
                "def go(argv):\n    from collector_run import default_run as _r\n    return _r(argv, timeout=1)\n")
            (root / "skill" / "scripts" / "alias.py").write_text(
                "import collector_run as cr\n\n\ndef go(argv):\n    return cr.default_run(argv, timeout=1)\n")
            (root / "skill" / "scripts" / "wrapped.py").write_text(
                "import collector_run\ndefault_run = retrying(collector_run.default_run)\n")
            (root / "skill" / "scripts" / "fine.py").write_text(
                "import collector_run\nRun = collector_run.Run\n"
                "default_run = functools.partial(collector_run.default_run, timeout=5)\n")
            paths = sorted(root.glob("skill/scripts/**/*.py"))
            self.assertEqual(
                {"skill/scripts/rules/deep.py": ["default_run"], "skill/scripts/tuple.py": ["Run = ..."],
                 "skill/scripts/wrapped.py": ["default_run = ..."]},
                copies_in(paths, root=root))
            self.assertEqual(
                {"skill/scripts/alias.py": ["1:import collector_run outside the module-level plain import"],
                 "skill/scripts/fromimp.py": ["2:from collector_run import ..."]},
                strays_in(paths, root=root))


if __name__ == "__main__":
    unittest.main()
