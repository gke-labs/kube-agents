#!/usr/bin/env python3
"""Tests for stall_watch.py: the fleet sweep is faked at the sandbox hop."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stall_watch  # noqa: E402

PROJECT = "proj"
LOCATION = "us-central1"
GATEWAY_SECRET = "Secret storefront/storefront-tls not found."


def completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def finding(namespace, obj, heuristic, detail, stalled_for="20m", stalled_seconds=1200):
    # The shape stall_report.py on main emits per row: no condition message.
    return {
        "object": obj,
        "namespace": namespace,
        "heuristic": heuristic,
        "detail": detail,
        "stalled_for": stalled_for,
        "stalled_seconds": stalled_seconds,
    }


GATEWAY_CONDITION_ROW = finding(
    "storefront", "Gateway/storefront-gateway", "stale-condition", "listeners[https] ResolvedRefs=False InvalidCertificateRef"
)
GATEWAY_SYNC_ROW = finding(
    "storefront",
    "Gateway/storefront-gateway",
    "repeating-warnings",
    f'SYNC x12: failed to translate Gateway "storefront/storefront-gateway": Error GWCER102: {GATEWAY_SECRET}',
    stalled_for="18m",
    stalled_seconds=1080,
)
GATEWAY_ROWS = [GATEWAY_CONDITION_ROW, GATEWAY_SYNC_ROW]
DEPLOYMENT_ROW = finding(
    "checkout",
    "Deployment/checkout-api",
    "dangling-reference",
    "template.spec.containers[0].envFrom[0].configMapRef -> ConfigMap/checkout-feature-flags not found",
)
TIMEOUT = subprocess.TimeoutExpired("gcloud", stall_watch.GET_CREDENTIALS_TIMEOUT_SECONDS)


class Unlisted:
    """A cluster the project lists with a status that is not swept."""

    def __init__(self, status):
        self.status = status


class Located:
    """A cluster in a location other than the default, with its namespaces."""

    def __init__(self, location, namespaces, status="RUNNING"):
        self.location = location
        self.namespaces = namespaces
        self.status = status


class FakeFleet:
    """Answers the sandbox hops for a fleet described as
    {cluster: {namespace: [findings]}}. A namespace mapped to an exception or
    an exit code cannot be read and one mapped to a string returns that text;
    a cluster mapped to an exception cannot be reached; a cluster mapped to
    Unlisted(status) is listed but not swept; a cluster key `name@location`
    or a Located value puts it somewhere other than the default location."""

    def __init__(self, fleet, namespaces_extra=(), listing_stderr="", hidden=()):
        self.listing_stderr = listing_stderr
        self.hidden = set(hidden)
        self.fleet = {}
        for key, spec in fleet.items():
            name, _, location = key.partition(stall_watch.CLUSTER_ID_SEPARATOR)
            if isinstance(spec, Located):
                location, status, namespaces = spec.location, spec.status, spec.namespaces
            else:
                status = spec.status if isinstance(spec, Unlisted) else "RUNNING"
                namespaces = spec
            self.fleet[(name, location or LOCATION)] = (status, namespaces)
        self.namespaces_extra = list(namespaces_extra)
        self.calls = []

    def __call__(self, argv, *, timeout, kubeconfig=None, stdin=None):
        self.calls.append((argv, kubeconfig, stdin))
        if argv[:4] == ["gcloud", "container", "clusters", "list"]:
            body = [{"name": n, "location": l, "status": status} for (n, l), (status, _) in self.fleet.items() if n not in self.hidden]
            return completed(argv, json.dumps(body), stderr=self.listing_stderr)
        if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            name = argv[4]
            location = argv[5].split("=", 1)[1]
            _, namespaces = self.fleet[(name, location)]
            if isinstance(namespaces, Exception):
                raise namespaces
            return completed(argv)
        _, namespaces = self.fleet[self._cluster_from(kubeconfig)]
        if argv[:3] == ["kubectl", "get", "namespaces"]:
            names = list(namespaces) + self.namespaces_extra
            return completed(argv, "".join(f"namespace/{n}\n" for n in names))
        if argv[:3] == [stall_watch.PYTHON_EXECUTABLE, stall_watch.PYTHON_ISOLATED_FLAG, stall_watch.STDIN_SCRIPT_ARG]:
            namespace = argv[argv.index("--namespace") + 1]
            result = namespaces[namespace]
            if isinstance(result, Exception):
                raise result
            if isinstance(result, int):
                return completed(argv, "", returncode=result, stderr="cannot list anything")
            if isinstance(result, str):
                return completed(argv, result)
            return completed(argv, json.dumps({"namespace": namespace, "stalled_resources": len(result), "findings": result}))
        raise AssertionError(f"unexpected sandbox call {argv}")

    def scanned(self):
        return [argv[argv.index("--namespace") + 1] for argv, _, _ in self.calls if argv[:1] == [stall_watch.PYTHON_EXECUTABLE]]

    @staticmethod
    def _cluster_from(kubeconfig):
        slug = Path(kubeconfig).name[len(stall_watch.KUBECONFIG_FILE_PREFIX):-len(stall_watch.KUBECONFIG_FILE_SUFFIX)]
        _, name, location = slug.split(stall_watch.KUBECONFIG_SLUG_SEPARATOR)
        return name, location


def label(name, location=LOCATION):
    return f"`{name}` ({location})"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "stall_watch.json"
        env = {stall_watch.PROJECT_ENVS[0]: PROJECT, "PLATFORM_AGENT_HOME": self.tmp.name}
        for var in (stall_watch.KINDS_ENV, stall_watch.REPORT_SCRIPT_ENV, stall_watch.STATE_PATH_ENV, *stall_watch.PROJECT_ENVS[1:]):
            env[var] = ""
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        p = patch.object(stall_watch.sandbox_exec, "sandbox_enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(stall_watch, "dns_endpoint_args", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def run_tick(self, fleet, **kw):
        fake = FakeFleet(fleet, **kw)
        with patch.object(stall_watch, "run_sandbox", fake):
            lines = stall_watch.tick(self.state, dry_run=False)
        return lines, fake

    def ledger(self):
        return json.loads(self.state.read_text())


class Transitions(Base):
    def test_first_sighting_is_one_bullet_naming_what_it_waits_on(self):
        lines, _ = self.run_tick({"support-eval-cluster": {"storefront": GATEWAY_ROWS, "catalog": []}})
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith(stall_watch.HEADLINE_PREFIX))
        self.assertIn("1 new, 0 cleared", lines[0])
        self.assertIn(stall_watch.NEW_HEADING, text)
        bullets = [l for l in lines if l.startswith("- " + label("support-eval-cluster"))]
        self.assertEqual(len(bullets), 1)
        self.assertIn(f"{label('support-eval-cluster')} / `storefront` — Gateway/storefront-gateway (20m): repeating-warnings", bullets[0])
        self.assertIn(GATEWAY_SECRET, bullets[0].split(stall_watch.DETAIL_JOINER)[0])
        self.assertNotIn(stall_watch.CLEARED_HEADING, text)

    def test_unchanged_stall_is_silent(self):
        fleet = {"c": {"storefront": GATEWAY_ROWS}}
        self.run_tick(fleet)
        lines, _ = self.run_tick(fleet)
        self.assertEqual(lines, [])
        self.assertEqual(len(self.ledger()["stalls"]), 2)

    def test_cleared_stall_is_announced_once(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines)
        self.assertIn("0 new, 1 cleared", lines[0])
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(lines, [])

    def test_a_healthy_fleet_prints_nothing(self):
        lines, _ = self.run_tick({"c": {"catalog": [], "checkout": []}})
        self.assertEqual(lines, [])
        self.assertTrue(self.state.exists())

    def test_a_rising_event_count_is_the_same_row(self):
        sync = lambda n: finding("storefront", "Gateway/storefront-gateway", "repeating-warnings", f"SYNC x{n}: {GATEWAY_SECRET}")
        self.run_tick({"c": {"storefront": [sync(12)]}})
        first = self.ledger()["stalls"]
        lines, _ = self.run_tick({"c": {"storefront": [sync(13)]}})
        self.assertEqual(lines, [])
        second = self.ledger()["stalls"]
        self.assertEqual(list(first), list(second))
        self.assertEqual(list(second.values())[0]["first_seen"], list(first.values())[0]["first_seen"])
        self.assertIn("SYNC x13:", list(second.values())[0]["detail"])

    def test_a_warning_that_recurs_outside_the_window_does_not_flap(self):
        # A repeating-warnings row exists only while the event recurred inside
        # stall_report.py's window, so an hourly warning is absent every other
        # scan; it clears only after two consecutive scans without it.
        only_sync = {"c": {"storefront": [GATEWAY_SYNC_ROW]}}
        quiet = {"c": {"storefront": []}}
        self.run_tick(only_sync)
        self.assertEqual(self.run_tick(quiet)[0], [])
        self.assertEqual(self.ledger()["stalls"][next(iter(self.ledger()["stalls"]))]["missed"], 1)
        self.assertEqual(self.run_tick(only_sync)[0], [])
        self.assertEqual(self.run_tick(quiet)[0], [])
        lines, _ = self.run_tick(quiet)
        self.assertIn(stall_watch.CLEARED_HEADING, lines)
        self.assertEqual(self.ledger()["stalls"], {})

    def test_a_condition_row_clears_on_the_first_scan_without_it(self):
        self.run_tick({"c": {"storefront": [GATEWAY_CONDITION_ROW]}})
        lines, _ = self.run_tick({"c": {"storefront": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines)

    def test_a_row_joining_a_known_object_is_folded_in_silently(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        deadline = finding("checkout", "Deployment/checkout-api", "stale-condition", "Progressing=False ProgressDeadlineExceeded", stalled_for="10m", stalled_seconds=600)
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, deadline]}})
        self.assertEqual(lines, [])
        self.assertEqual(len(self.ledger()["stalls"]), 2)
        lines, _ = self.run_tick({"c": {"checkout": [deadline]}})
        self.assertEqual(lines, [], "one row clearing while the object stays stalled is not a recovery")
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines)

    def test_an_object_with_many_rows_is_one_bullet_with_its_oldest_age(self):
        rows = [
            finding("storefront", "Gateway/storefront-gateway", "stale-condition", d)
            for d in ("Programmed=False Invalid", "Ready=False NotReady", "listeners[https] Ready=False NotReady", "listeners[https] Programmed=False Invalid")
        ]
        rows[0].update(stalled_for="6d0h", stalled_seconds=518400)
        rows[1].update(stalled_for="2d19h", stalled_seconds=241200)
        rows += GATEWAY_ROWS
        lines, _ = self.run_tick({"c": {"storefront": rows}})
        bullets = [l for l in lines if l.startswith("- " + label("c"))]
        self.assertEqual(len(bullets), 1)
        self.assertIn("Gateway/storefront-gateway (6d0h):", bullets[0])
        self.assertIn(GATEWAY_SECRET, bullets[0].split(stall_watch.DETAIL_JOINER)[0])
        self.assertIn("and 3 more", bullets[0])

    def test_a_message_field_when_the_report_carries_one_is_printed_after_the_detail(self):
        # stall_report.py on main emits no `message`; a build that does gets it
        # shown, and the row key does not move when the message does.
        row = dict(GATEWAY_CONDITION_ROW, message="Error GWCER102: " + GATEWAY_SECRET)
        lines, _ = self.run_tick({"c": {"storefront": [row]}})
        self.assertIn(f"InvalidCertificateRef: Error GWCER102: {GATEWAY_SECRET}", "\n".join(lines))
        moved = dict(row, message="Error GWCER102: still not found.", stalled_for="50m", stalled_seconds=3000)
        lines, _ = self.run_tick({"c": {"storefront": [moved]}})
        self.assertEqual(lines, [])
        self.assertEqual(list(self.ledger()["stalls"].values())[0]["stalled_for"], "50m")

    def test_two_clusters_two_kinds_of_stall(self):
        lines, _ = self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"checkout": [DEPLOYMENT_ROW]}})
        text = "\n".join(lines)
        self.assertIn("2 new, 0 cleared (swept 2 clusters, 2 namespaces; 2 stalled objects open)", lines[0])
        self.assertIn(f"{label('a')} / `storefront`", text)
        self.assertIn(f"{label('b')} / `checkout` — Deployment/checkout-api (20m): dangling-reference", text)

    def test_same_named_clusters_in_two_locations_are_two_scopes(self):
        fleet = {"c": {"payments": [DEPLOYMENT_ROW]}, "c@europe-west1": Located("europe-west1", {"payments": []})}
        lines, _ = self.run_tick(fleet)
        self.assertIn("1 new", lines[0])
        lines, _ = self.run_tick(fleet)
        self.assertEqual(lines, [], "the second cluster's empty namespace does not clear the first cluster's row")
        keys = list(self.ledger()["stalls"])
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith(f"c{stall_watch.CLUSTER_ID_SEPARATOR}{LOCATION}{stall_watch.LEDGER_KEY_SEPARATOR}"))


class Gone(Base):
    def test_a_deleted_namespace_clears_its_object_at_once(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS, "catalog": []}})
        lines, _ = self.run_tick({"c": {"catalog": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines)
        self.assertIn("0 new, 1 cleared", lines[0])
        self.assertEqual(self.ledger()["stalls"], {})

    def test_a_deleted_cluster_clears_its_objects_and_its_unreadable_entry(self):
        self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}})
        self.run_tick({"a": TIMEOUT, "b": {"catalog": []}})
        self.assertEqual(list(self.ledger()["unreadable"]), [stall_watch.cluster_id("a", LOCATION)])
        lines, _ = self.run_tick({"b": {"catalog": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines)
        self.assertNotIn(stall_watch.READABLE_AGAIN_HEADING, lines)
        self.assertEqual(self.ledger()["stalls"], {})
        self.assertEqual(self.ledger()["unreadable"], {})

    def test_a_reconciling_cluster_is_swept_and_a_provisioning_one_is_unreadable(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        lines, fake = self.run_tick({"c": Located(LOCATION, {"storefront": GATEWAY_ROWS}, status="RECONCILING")})
        self.assertEqual(lines, [])
        self.assertEqual(fake.scanned(), ["storefront"])
        lines, _ = self.run_tick({"c": Unlisted("PROVISIONING")})
        text = "\n".join(lines)
        self.assertIn(f"- {label('c')}: status=PROVISIONING", text)
        self.assertNotIn(stall_watch.CLEARED_HEADING, text)
        self.assertEqual(len(self.ledger()["stalls"]), 2)
        lines, _ = self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        self.assertEqual(lines[1:], [stall_watch.READABLE_AGAIN_HEADING, f"- {label('c')}"])
        self.assertIn("0 new, 0 cleared", lines[0])


class Unreadable(Base):
    def test_a_cluster_with_only_system_namespaces_counts_as_read(self):
        # Readable again has to fire on a cluster that has nothing to scan, or
        # its entry is stuck and every later failure on it is suppressed.
        self.run_tick({"c": Unlisted("PROVISIONING")})
        lines, _ = self.run_tick({"c": {}}, namespaces_extra=["kube-system"])
        self.assertEqual(lines[1:], [stall_watch.READABLE_AGAIN_HEADING, f"- {label('c')}"])
        self.assertEqual(self.ledger()["unreadable"], {})
        lines, _ = self.run_tick({"c": Unlisted("STOPPING")})
        self.assertIn(f"- {label('c')}: status=STOPPING", "\n".join(lines))

    def test_a_listing_gcloud_calls_incomplete_clears_nothing(self):
        self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}})
        partial = "WARNING: The following zones did not respond: us-central1-a. List results may be incomplete."
        lines, _ = self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}}, listing_stderr=partial, hidden=["a"])
        text = "\n".join(lines)
        self.assertNotIn(stall_watch.CLEARED_HEADING, text)
        self.assertIn("- the cluster listing: incomplete: WARNING: The following zones did not respond", text)
        self.assertEqual(len(self.ledger()["stalls"]), 2)
        lines, _ = self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}})
        self.assertEqual(lines[1:], [stall_watch.READABLE_AGAIN_HEADING, "- the cluster listing"])
        self.assertEqual(len(self.ledger()["stalls"]), 2)
        lines, _ = self.run_tick({"b": {"catalog": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines, "a complete listing without the cluster is a deletion")

    def test_the_sweep_stops_at_its_budget_and_says_what_it_skipped(self):
        fleet = {"c": {"a": [], "b": [dict(DEPLOYMENT_ROW, namespace="b")], "d": []}}
        self.run_tick(fleet)
        over = stall_watch.TICK_BUDGET_SECONDS + 1
        # started, before cluster c, before a, before b (over budget), then whatever else asks
        with patch.object(stall_watch.time, "monotonic", side_effect=[0, 1, 2, over, over, over, over, over]):
            lines, fake = self.run_tick(fleet)
        self.assertEqual(fake.scanned(), ["a"])
        text = "\n".join(lines)
        self.assertIn(f"- the rest of the fleet (sweep budget): sweep budget of {stall_watch.TICK_BUDGET_SECONDS}s exhausted after 1 clusters and 1 namespaces", text)
        self.assertNotIn(stall_watch.CLEARED_HEADING, text)
        self.assertEqual(len(self.ledger()["stalls"]), 1)
        lines, _ = self.run_tick(fleet)
        self.assertEqual(lines[1:], [stall_watch.READABLE_AGAIN_HEADING, "- the rest of the fleet (sweep budget)"])

    def test_a_cluster_the_budget_never_reached_is_unread_not_gone(self):
        fleet = {"a": {"ns": []}, "b": {"ns": []}, "c": {"ns": [dict(DEPLOYMENT_ROW, namespace="ns")]}}
        self.run_tick(fleet)
        over = stall_watch.TICK_BUDGET_SECONDS + 1
        # started, before cluster a, before a/ns, before cluster b (over budget)
        with patch.object(stall_watch.time, "monotonic", side_effect=[0, 1, 2, over, over, over, over, over]):
            lines, fake = self.run_tick(fleet)
        self.assertEqual(fake.scanned(), ["ns"])
        text = "\n".join(lines)
        self.assertNotIn(stall_watch.CLEARED_HEADING, text, "c was listed but never read; its row is unknown, not gone")
        self.assertIn("exhausted after 1 clusters and 1 namespaces", text)
        self.assertEqual(len(self.ledger()["stalls"]), 1)
        self.assertEqual(self.ledger()["unreadable"].keys() - {stall_watch.BUDGET_SCOPE}, set())

    def test_unreachable_cluster_keeps_its_rows_and_is_reported_once(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        lines, _ = self.run_tick({"c": TIMEOUT})
        text = "\n".join(lines)
        self.assertIn(stall_watch.UNREADABLE_HEADING, text)
        self.assertIn(f"- {label('c')}: timed out after 60s", text)
        self.assertIn("swept 0 clusters, 0 namespaces", lines[0])
        self.assertNotIn(stall_watch.CLEARED_HEADING, text)
        self.assertEqual(len(self.ledger()["stalls"]), 2)
        lines, _ = self.run_tick({"c": TIMEOUT})
        self.assertEqual(lines, [])

    def test_cluster_readable_again_is_reported_and_its_rows_can_clear(self):
        self.run_tick({"c": {"storefront": [GATEWAY_CONDITION_ROW]}})
        self.run_tick({"c": TIMEOUT})
        lines, _ = self.run_tick({"c": {"storefront": []}})
        text = "\n".join(lines)
        self.assertIn(stall_watch.READABLE_AGAIN_HEADING, text)
        self.assertIn(stall_watch.CLEARED_HEADING, text)
        self.assertEqual(self.ledger()["unreadable"], {})

    def test_unreadable_namespace_keeps_its_rows(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS, "checkout": [DEPLOYMENT_ROW]}})
        lines, _ = self.run_tick({"c": {"storefront": stall_watch.REPORT_UNREADABLE_EXIT, "checkout": []}})
        text = "\n".join(lines)
        self.assertIn(f"- {label('c')} / `storefront`: no kind could be read", text)
        self.assertIn("Deployment/checkout-api", text)
        self.assertNotIn("Gateway/storefront-gateway", text)
        self.assertIn("swept 1 clusters, 1 namespaces", lines[0])
        self.assertEqual(len(self.ledger()["stalls"]), 2)

    def test_a_scan_timeout_ends_that_clusters_sweep_for_the_tick(self):
        self.run_tick({"c": {"a": [], "b": [], "d": [dict(DEPLOYMENT_ROW, namespace="d")]}})
        scan_timeout = subprocess.TimeoutExpired("python3", stall_watch.NAMESPACE_SCAN_TIMEOUT_SECONDS)
        lines, fake = self.run_tick({"c": {"a": [], "b": scan_timeout, "d": []}})
        self.assertEqual(fake.scanned(), ["a", "b"], "the namespace after the timeout is not scanned")
        text = "\n".join(lines)
        self.assertIn(f"- {label('c')}: namespace b timed out after 300s; the cluster's remaining namespaces were skipped this tick", text)
        self.assertNotIn(stall_watch.CLEARED_HEADING, text, "an unscanned namespace keeps its rows")
        self.assertEqual(len(self.ledger()["stalls"]), 1)

    def test_unparsable_output_marks_one_scope_not_the_sweep(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS, "checkout": [DEPLOYMENT_ROW]}})
        lines, _ = self.run_tick({"c": {"storefront": '{"namespace": "storefront", "find', "checkout": []}})
        text = "\n".join(lines)
        self.assertIn(f"- {label('c')} / `storefront`: stall_report.py returned unparsable output", text)
        self.assertIn(stall_watch.CLEARED_HEADING, text)
        self.assertIsNone(self.ledger()["sweep_error"])

    def test_failed_cluster_list_is_reported_once_and_recovery_once(self):
        def broken(argv, *, timeout, kubeconfig=None, stdin=None):
            return completed(argv, "", returncode=1, stderr="ERROR: (gcloud.auth) reauth required")

        with patch.object(stall_watch, "run_sandbox", broken):
            first = stall_watch.tick(self.state, dry_run=False)
            second = stall_watch.tick(self.state, dry_run=False)
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0].startswith(stall_watch.SWEEP_FAILED_PREFIX))
        self.assertIn("reauth required", first[0])
        self.assertEqual(second, [])
        lines, _ = self.run_tick({"c": {"catalog": []}})
        self.assertEqual(lines, [stall_watch.SWEEP_RECOVERED_LINE])


class Scope(Base):
    def test_system_namespaces_are_skipped_and_the_harness_is_not(self):
        extra = ["kube-system", "gke-managed-cim", "config-management-system", "gmp-public"]
        _, fake = self.run_tick({"c": {"payments": [], "kubeagents-system": []}}, namespaces_extra=extra)
        self.assertEqual(sorted(fake.scanned()), ["kubeagents-system", "payments"])

    def test_a_terminating_namespace_is_still_read_so_its_rows_can_clear(self):
        gone = [dict(r, namespace="old") for r in GATEWAY_ROWS]
        self.run_tick({"c": {"old": gone}})
        self.run_tick({"c": {"old": []}})
        lines, _ = self.run_tick({"c": {"old": []}})
        self.assertIn(stall_watch.CLEARED_HEADING, lines)

    def test_default_kinds_are_passed_and_all_lets_the_script_decide(self):
        _, fake = self.run_tick({"c": {"payments": []}})
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertEqual(scan[scan.index("--kind") + 1], ",".join(stall_watch.DEFAULT_KINDS))
        self.assertNotIn("pods", scan[scan.index("--kind") + 1].split(","))
        with patch.dict(os.environ, {stall_watch.KINDS_ENV: "all"}):
            _, fake = self.run_tick({"c": {"payments": []}})
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertNotIn("--kind", scan)

    def test_the_project_comes_from_the_operators_variable_without_a_gcloud_hop(self):
        with patch.dict(os.environ, {stall_watch.PROJECT_ENVS[0]: "", "GCP_PROJECT_ID": "from-operator"}):
            _, fake = self.run_tick({"c": {"payments": []}})
        argvs = [argv for argv, _, _ in fake.calls]
        self.assertNotIn(["gcloud", "config", "get-value", "project"], argvs)
        self.assertIn("--project=from-operator", argvs[0])

    def test_the_report_script_travels_on_stdin_in_isolated_mode(self):
        _, fake = self.run_tick({"c": {"payments": []}})
        argv, kubeconfig, stdin = next(c for c in fake.calls if c[0][0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertEqual(argv[:3], [stall_watch.PYTHON_EXECUTABLE, stall_watch.PYTHON_ISOLATED_FLAG, stall_watch.STDIN_SCRIPT_ARG])
        expected = (Path(stall_watch.__file__).resolve().parent / stall_watch.LOCAL_REPORT_SCRIPT_NAME).read_text()
        self.assertEqual(stdin, expected)
        self.assertTrue(kubeconfig.endswith(f"{stall_watch.KUBECONFIG_FILE_PREFIX}proj_c_{LOCATION}{stall_watch.KUBECONFIG_FILE_SUFFIX}"))

    def test_isolated_mode_ignores_a_decoy_module_in_the_working_directory(self):
        # The sandbox command runs with the model-owned /opt/data as its cwd.
        # Without -I that directory is first on sys.path and a json.py there is
        # the code that runs; with it the streamed script sees only the stdlib.
        source = stall_watch.report_source()
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / "json.py").write_text("raise SystemExit(99)\n")
            argv = stall_watch.report_argv("payments")
            argv[0] = sys.executable
            isolated = subprocess.run(argv + ["--help"], input=source, capture_output=True, text=True, cwd=cwd)
            naive = subprocess.run([sys.executable, stall_watch.STDIN_SCRIPT_ARG, "--help"], input=source, capture_output=True, text=True, cwd=cwd)
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertIn("--threshold-minutes", isolated.stdout)
        self.assertEqual(naive.returncode, 99, "the decoy is what a non-isolated interpreter would have run")

    def test_production_kubeconfig_path_is_under_hermes_home_with_the_watch_prefix(self):
        with patch.object(stall_watch.sandbox_exec, "sandbox_enabled", return_value=True):
            path = stall_watch.kubeconfig_path("my-proj", "a cluster", "us-central1")
        self.assertEqual(path, f"{stall_watch.SANDBOX_KUBECONFIG_DIR}/{stall_watch.KUBECONFIG_FILE_PREFIX}my-proj_a-cluster_us-central1{stall_watch.KUBECONFIG_FILE_SUFFIX}")
        self.assertFalse(Path(stall_watch.SANDBOX_KUBECONFIG_DIR).exists() and Path(path).exists(), "nothing is created locally for the sandbox path")

    def test_the_report_source_prefers_the_image_copy_and_honours_the_override(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / "image.py"; image.write_text("IMAGE")
            override = Path(d) / "override.py"; override.write_text("OVERRIDE")
            with patch.object(stall_watch, "IMAGE_REPORT_SCRIPT", str(image)):
                self.assertEqual(stall_watch.report_source(), "IMAGE")
                with patch.dict(os.environ, {stall_watch.REPORT_SCRIPT_ENV: str(override)}):
                    self.assertEqual(stall_watch.report_source(), "OVERRIDE")
            with patch.object(stall_watch, "IMAGE_REPORT_SCRIPT", str(Path(d) / "absent.py")):
                self.assertIn("stalled resources", stall_watch.report_source(), "the sibling copy is the fallback")
            with patch.object(stall_watch, "IMAGE_REPORT_SCRIPT", str(Path(d) / "absent.py")), patch.object(stall_watch, "LOCAL_REPORT_SCRIPT_NAME", "nope.py"):
                with self.assertRaises(RuntimeError):
                    stall_watch.report_source()

    def test_only_kubeconfig_and_stdin_cross_into_the_sandbox(self):
        with patch.object(stall_watch.sandbox_exec, "run", return_value=completed([], "[]")) as run:
            stall_watch.run_sandbox(["python3", "-I", "-"], timeout=5, kubeconfig="/k", stdin="print(1)")
        self.assertEqual(run.call_args.kwargs["remote_env"], {"KUBECONFIG": "/k"})
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.kwargs["stdin"], "print(1)")
        self.assertNotIn("principal", run.call_args.kwargs)

    def test_state_is_written_atomically_and_versioned(self):
        self.run_tick({"c": {"payments": []}})
        self.assertFalse(self.state.with_name(self.state.name + stall_watch.STATE_TMP_SUFFIX).exists())
        self.assertEqual(self.ledger()["version"], stall_watch.STATE_SCHEMA_VERSION)

    def test_a_ledger_from_another_version_is_discarded(self):
        self.state.write_text(json.dumps({"version": 1, "stalls": {"x": {}}}))
        self.assertEqual(stall_watch.load_state(self.state)["stalls"], {})


class Output(Base):
    def test_long_sections_are_capped_with_a_count(self):
        rows = [finding("ns", f"Deployment/app-{i}", "generation-lag", "generation 2 observed 1") for i in range(stall_watch.MAX_LISTED_ROWS + 5)]
        lines, _ = self.run_tick({"c": {"ns": rows}})
        self.assertIn("- and 5 more", lines)
        self.assertEqual(sum(1 for l in lines if l.startswith("- " + label("c"))), stall_watch.MAX_LISTED_ROWS)

    def test_main_prints_lines_and_exits_zero(self):
        fake = FakeFleet({"c": {"storefront": GATEWAY_ROWS}})
        out = io.StringIO()
        with patch.object(stall_watch, "run_sandbox", fake), redirect_stdout(out):
            rc = stall_watch.main(["--state", str(self.state)])
        self.assertEqual(rc, 0)
        self.assertIn(stall_watch.NEW_HEADING, out.getvalue())
        out = io.StringIO()
        with patch.object(stall_watch, "run_sandbox", fake), redirect_stdout(out):
            stall_watch.main(["--state", str(self.state)])
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
