"""Tests for the Pub/Sub adapter's suppression mechanisms.

    python3 -m unittest discover -s agentplugins/pubsub-platform/tests -v

Three separate things decide whether an alert becomes an investigation, and all three
fail silently — an over-eager one drops incidents nobody hears about, a slack one turns
one stockout into an investigation per autoscaler retry, and either way the logs look
normal. They are covered here because a live cluster is a slow and expensive place to
find out:

  * the **filter**, which decides whether the alert is about something we can act on
  * the **dedup key**, which decides whether two alerts are the same incident
  * the **registry**, which decides how long that verdict lasts

The payload shapes below are trimmed copies of real GKE autoscaler output collected from
`gke-stockout-alerts-topic`, including the awkward parts: the cluster label lives at a
different depth in different events, and one event shape names no workload at all.

Nothing here touches the network, a cluster, or the gateway.
"""

import importlib.util
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import time
import types
import unittest

_ADAPTER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "files" / "platforms" / "pubsub" / "adapter.py"
)


def _load_adapter_module():
    """Import adapter.py with its gateway dependencies stubbed.

    The adapter imports the Hermes gateway at module scope, which only exists inside the
    agent image. Everything exercised here is pure, so stubs are enough — and keeping the
    tests importable outside the image is what makes them runnable in CI.
    """
    for name in (
        "gateway", "gateway.platforms", "gateway.platforms.base",
        "gateway.config", "gateway.session",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))

    base = sys.modules["gateway.platforms.base"]

    class _Stub:
        pass

    for attr in ("BasePlatformAdapter", "SendResult", "MessageEvent", "MessageType", "ProcessingOutcome"):
        setattr(base, attr, _Stub)
    cfg = sys.modules["gateway.config"]
    cfg.Platform = lambda *a, **k: None
    cfg.PlatformConfig = _Stub
    sys.modules["gateway.session"].build_session_key = lambda *a, **k: "session-key"

    spec = importlib.util.spec_from_file_location("pubsub_adapter", _ADAPTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pubsub_adapter"] = module
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter_module()

ROUTE = "gke_stockout_alerts"
NS = "jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups.0"
CONTROLLER_PATH = f"{NS}.podGroup.samplePod.controller.name"

# The shipped configuration, mirrored from
# agentplugins/gke-stockout-investigator/templates/agentplugin.yaml. If that file changes,
# these tests are the record of what the change is supposed to preserve.
DEDUP_FIELDS = [
    ["resource.labels.project_id", "jsonPayload.resource.labels.project_id"],
    ["resource.labels.location", "jsonPayload.resource.labels.location"],
    ["resource.labels.cluster_name", "jsonPayload.resource.labels.cluster_name"],
    f"{NS}.podGroup.samplePod.namespace",
    f"{NS}.podGroup.samplePod.controller.kind",
    CONTROLLER_PATH,
]
FILTER = (
    f"resource.labels.cluster_name == 'example-cluster' and {CONTROLLER_PATH} != ''"
    f" or jsonPayload.resource.labels.cluster_name == 'example-cluster' and {CONTROLLER_PATH} != ''"
)
ROUTE_CONFIG = {"deduplicate_fields": DEDUP_FIELDS, "filter": FILTER}


def no_scale_up(
    controller="llm-inference-service-56c4b87787",
    kind="ReplicaSet",
    namespace="stockout-scenarios",
    cluster="example-cluster",
    zone="example-region-b",
    reason="no.scale.up.nap.pod.zonal.resources.exceeded",
    nested_labels=False,
):
    """A `noDecisionStatus.noScaleUp` event — what the autoscaler emits organically.

    `zone` and `reason` vary between the retries of ONE incident; that is the whole point
    of test_retries_of_one_incident_are_one_incident.
    """
    labels = {"project_id": "example-project", "location": "example-region", "cluster_name": cluster}
    payload = {
        "jsonPayload": {
            "noDecisionStatus": {
                "noScaleUp": {
                    "unhandledPodGroups": [{
                        "podGroup": {"samplePod": {
                            "namespace": namespace,
                            "controller": {"kind": kind, "name": controller},
                        }},
                        "napFailureReasons": [{"messageId": reason, "parameters": [zone]}],
                    }],
                }
            }
        }
    }
    # Both shapes occur in the wild: some events carry the resource block at the top
    # level, others only under jsonPayload.
    if nested_labels:
        payload["jsonPayload"]["resource"] = {"labels": labels}
    else:
        payload["resource"] = {"labels": labels}
    return payload


def nap_summary(cluster="example-cluster"):
    """A NAP summary event: real, frequent, and names no workload."""
    return {
        "resource": {"labels": {"project_id": "example-project", "location": "example-region",
                                "cluster_name": cluster}},
        "jsonPayload": {"noDecisionStatus": {"noScaleUp": {
            "napFailureReasons": [{"messageId": "no.scale.up.nap.disabled"}],
        }}},
    }


def unrelated_shape():
    """A payload carrying none of the configured dedup fields."""
    return {"jsonPayload": {"somethingElse": {"value": 1}}}


class AdapterTestCase(unittest.TestCase):
    """Gives each test its own registry file and a quiet logger."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.registry = self.tmp / "pubsub_registry.json"

        # __init__ needs a gateway PlatformConfig; none of the code under test uses the
        # instance state it would set up.
        self.adapter = adapter_mod.PubSubAdapter.__new__(adapter_mod.PubSubAdapter)
        self.adapter._get_registry_path = lambda: str(self.registry)

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        os.environ.pop("DISABLE_PUBSUB_DEDUP", None)
        self.addCleanup(os.environ.pop, "DISABLE_PUBSUB_DEDUP", None)

    def is_duplicate(self, payload, config=None):
        return self.adapter._is_duplicate(payload, config or dict(ROUTE_CONFIG), ROUTE)

    def entries(self):
        return json.loads(self.registry.read_text()) if self.registry.exists() else []


class DedupKeyTest(AdapterTestCase):
    """Which alerts count as the same incident."""

    def test_the_same_alert_twice_is_one_incident(self):
        self.assertFalse(self.is_duplicate(no_scale_up()))
        self.assertTrue(self.is_duplicate(no_scale_up()))

    def test_retries_of_one_incident_are_one_incident(self):
        """The regression that mattered: one stockout, many autoscaler retries.

        A single wedged workload produces a stream of events whose failure reason and
        zone differ every time — `zonal.resources.exceeded` in example-region-b, then
        `zonal.failing.predicates` in -c, and so on. Keying on any of that detail turned
        one stockout into an investigation per retry, each one opening its own PR.
        """
        self.assertFalse(self.is_duplicate(no_scale_up(zone="example-region-b", reason="no.scale.up.nap.pod.zonal.resources.exceeded")))
        for zone, reason in (
            ("example-region-c", "no.scale.up.nap.pod.zonal.failing.predicates"),
            ("example-region-d", "no.scale.up.nap.pod.zonal.illegal.config"),
            ("example-region-b", "no.scale.up.nap.pod.zonal.resources.exceeded"),
        ):
            with self.subTest(zone=zone, reason=reason):
                self.assertTrue(self.is_duplicate(no_scale_up(zone=zone, reason=reason)))
        self.assertEqual(len(self.entries()), 1, "one incident must leave one registry entry")

    def test_a_different_workload_is_a_different_incident(self):
        self.assertFalse(self.is_duplicate(no_scale_up(controller="api-server-7d9f")))
        self.assertFalse(self.is_duplicate(no_scale_up(controller="batch-worker-1a2b")))

    def test_a_new_revision_is_a_new_incident(self):
        """The ReplicaSet hash is the pod-template revision, and that distinction earns
        its keep: a remediation that changed the template and STILL cannot schedule is a
        failed fix, not a repeat of the original alert."""
        self.assertFalse(self.is_duplicate(no_scale_up(controller="llm-inference-service-56c4b87787")))
        self.assertFalse(self.is_duplicate(no_scale_up(controller="llm-inference-service-99aa0011")))

    def test_the_same_name_in_another_namespace_is_a_different_incident(self):
        self.assertFalse(self.is_duplicate(no_scale_up(namespace="team-a")))
        self.assertFalse(self.is_duplicate(no_scale_up(namespace="team-b")))

    def test_the_same_name_in_another_cluster_is_a_different_incident(self):
        """Cluster name alone was the key once; two fleets with a `prod` each collided."""
        self.assertFalse(self.is_duplicate(no_scale_up(cluster="example-cluster")))
        self.assertFalse(self.is_duplicate(no_scale_up(cluster="ka-staging")))

    def test_a_deployment_and_a_replicaset_of_the_same_name_differ(self):
        self.assertFalse(self.is_duplicate(no_scale_up(kind="Deployment", controller="svc")))
        self.assertFalse(self.is_duplicate(no_scale_up(kind="ReplicaSet", controller="svc")))


class AlternativePathsTest(AdapterTestCase):
    """One route carries several payload shapes; a field may name alternatives."""

    def test_the_same_incident_matches_across_payload_shapes(self):
        self.assertFalse(self.is_duplicate(no_scale_up(nested_labels=False)))
        self.assertTrue(
            self.is_duplicate(no_scale_up(nested_labels=True)),
            "top-level and jsonPayload-nested resource labels describe the same cluster",
        )

    def test_the_first_non_empty_alternative_wins(self):
        key = self.adapter._dedup_key(
            no_scale_up(nested_labels=True),
            [["resource.labels.cluster_name", "jsonPayload.resource.labels.cluster_name"]],
        )
        self.assertEqual(list(key.values()), ["example-cluster"])

    def test_the_label_is_the_first_path(self):
        """Registry entries stay comparable across restarts only if the label is stable."""
        key = self.adapter._dedup_key(no_scale_up(), [["resource.labels.location", "x.y"]])
        self.assertEqual(list(key), ["resource.labels.location"])


class EmptyKeyTest(AdapterTestCase):
    """A key that identifies nothing must not suppress everything."""

    def test_an_unrecognised_shape_is_not_deduplicated(self):
        self.assertFalse(self.is_duplicate(unrelated_shape()))
        self.assertFalse(self.is_duplicate(unrelated_shape()))
        self.assertEqual(self.entries(), [], "an empty key must not be recorded")

    def test_two_unknown_shape_alerts_do_not_suppress_each_other(self):
        """The failure this guards, stated precisely.

        Without the guard the first all-empty key is *recorded*, and then every later
        alert whose fields also resolve empty matches it — however unrelated the incident
        — and is dropped for the whole window. One event silently stands for all of them.
        """
        self.assertFalse(self.is_duplicate(unrelated_shape()))
        self.assertFalse(
            self.is_duplicate({"jsonPayload": {"anotherShape": {"value": 2}}}),
            "a different incident must not match the first one's empty key",
        )

    def test_an_unrecognised_shape_does_not_suppress_real_alerts(self):
        self.is_duplicate(unrelated_shape())
        self.assertFalse(self.is_duplicate(no_scale_up()), "a real alert must still get through")


class RegistryTest(AdapterTestCase):
    """How long a verdict lasts, and what happens when the file is unusable."""

    def test_the_verdict_expires_with_the_window(self):
        config = {**ROUTE_CONFIG, "deduplicate_window_seconds": 60}
        self.assertFalse(self.is_duplicate(no_scale_up(), config))
        self.assertTrue(self.is_duplicate(no_scale_up(), config))

        aged = self.entries()
        aged[0]["timestamp"] = time.time() - 3600
        self.registry.write_text(json.dumps(aged))

        self.assertFalse(self.is_duplicate(no_scale_up(), config), "the window had passed")

    def test_expired_entries_are_pruned(self):
        self.registry.write_text(json.dumps([
            {"route_name": ROUTE, "timestamp": time.time() - 90000, "field_values": {"a": "old"}},
        ]))
        self.is_duplicate(no_scale_up())
        self.assertEqual(len(self.entries()), 1, "the stale entry should be gone, the new one kept")

    def test_another_route_does_not_suppress_this_one(self):
        self.is_duplicate(no_scale_up())
        entry = self.entries()[0]
        self.registry.write_text(json.dumps([{**entry, "route_name": "some_other_route"}]))
        self.assertFalse(self.is_duplicate(no_scale_up()))

    def test_a_short_window_route_does_not_expire_a_long_window_one(self):
        """One registry file, many routes — and retention used to be the caller's.

        A route with a five-minute window pruned every entry older than five minutes,
        including a daily route's. The daily route then re-investigated an incident it
        had already filed, and nothing in either route's logs said who dropped the
        record. Each entry now expires on the window it was written under.
        """
        long_config = {**ROUTE_CONFIG, "deduplicate_window_seconds": 86400}
        self.assertFalse(self.is_duplicate(no_scale_up(), long_config))

        aged = self.entries()
        aged[0]["timestamp"] = time.time() - 3600  # an hour old: stale to a 5m route
        self.registry.write_text(json.dumps(aged))

        # A different, short-window route passes through and prunes on its way.
        self.adapter._is_duplicate(
            no_scale_up(controller="unrelated-7c1f"),
            {**ROUTE_CONFIG, "deduplicate_window_seconds": 300},
            "some_other_route",
        )

        self.assertTrue(
            self.is_duplicate(no_scale_up(), long_config),
            "the hour-old entry is well inside its own 24h window and must survive",
        )

    def test_an_entry_records_the_window_it_was_written_under(self):
        self.is_duplicate(no_scale_up(), {**ROUTE_CONFIG, "deduplicate_window_seconds": 1234})
        self.assertEqual(self.entries()[0]["window_seconds"], 1234)

    def test_a_legacy_entry_without_a_window_still_expires(self):
        """Entries written before the window was recorded carry none; the calling
        route's own fall back to its window, which is what they were written under."""
        self.registry.write_text(json.dumps([
            {"route_name": ROUTE, "timestamp": time.time() - 600, "field_values": {"a": "old"}},
        ]))
        self.is_duplicate(no_scale_up(), {**ROUTE_CONFIG, "deduplicate_window_seconds": 300})
        self.assertEqual(
            [e["field_values"] for e in self.entries() if e["field_values"] == {"a": "old"}],
            [], "the windowless own-route entry was past this route's window",
        )

    def test_a_corrupt_registry_does_not_drop_the_alert(self):
        self.registry.write_text("{not json")
        self.assertFalse(self.is_duplicate(no_scale_up()), "unreadable state must fail open")

    def test_the_verdict_survives_a_restart(self):
        self.assertFalse(self.is_duplicate(no_scale_up()))
        fresh = adapter_mod.PubSubAdapter.__new__(adapter_mod.PubSubAdapter)
        fresh._get_registry_path = lambda: str(self.registry)
        self.assertTrue(fresh._is_duplicate(no_scale_up(), dict(ROUTE_CONFIG), ROUTE))


class DisableSwitchTest(AdapterTestCase):
    """The operator override, and the reason it is worth a test.

    `DISABLE_PUBSUB_DEDUP=true` was set out-of-band on a live deployment and preserved
    across every reconcile by server-side apply. It is invisible from the CRs, and while
    it was on, one wedged workload opened an investigation per autoscaler retry.
    """

    def test_the_env_var_turns_dedup_off(self):
        os.environ["DISABLE_PUBSUB_DEDUP"] = "true"
        self.assertFalse(self.is_duplicate(no_scale_up()))
        self.assertFalse(self.is_duplicate(no_scale_up()))
        self.assertFalse(self.registry.exists(), "nothing is recorded while dedup is off")

    def test_an_unrecognised_value_leaves_dedup_on(self):
        os.environ["DISABLE_PUBSUB_DEDUP"] = "yes-please"
        self.assertFalse(self.is_duplicate(no_scale_up()))
        self.assertTrue(self.is_duplicate(no_scale_up()))

    def test_no_configured_fields_means_no_dedup(self):
        self.assertFalse(self.is_duplicate(no_scale_up(), {"deduplicate_fields": []}))
        self.assertFalse(self.is_duplicate(no_scale_up(), {"deduplicate_fields": []}))


class FilterTest(AdapterTestCase):
    """The suppression that runs before dedup: is this alert about something actionable?"""

    def test_an_alert_naming_a_workload_passes(self):
        self.assertTrue(self.adapter._eval_filter(FILTER, no_scale_up()))

    def test_the_nested_label_shape_passes_too(self):
        self.assertTrue(self.adapter._eval_filter(FILTER, no_scale_up(nested_labels=True)))

    def test_an_alert_naming_no_workload_is_dropped(self):
        """A NAP summary has nothing to investigate — the skill's first step is to look at
        the workload's pods — and each one used to open its own investigation."""
        self.assertFalse(self.adapter._eval_filter(FILTER, nap_summary()))

    def test_another_cluster_is_dropped(self):
        self.assertFalse(self.adapter._eval_filter(FILTER, no_scale_up(cluster="someone-elses")))

    def test_paths_reach_through_lists(self):
        """`unhandledPodGroups.0.…` must resolve. While the filter's own path walk stopped
        at the first list, every such expression silently evaluated to empty — a filter
        that always sees "" is a filter that means something other than what it says."""
        expr = f"{CONTROLLER_PATH} == 'llm-inference-service-56c4b87787'"
        self.assertTrue(self.adapter._eval_filter(expr, no_scale_up()))

    def test_a_malformed_expression_drops_the_message(self):
        self.assertFalse(self.adapter._eval_filter("this is not an expression", no_scale_up()))


if __name__ == "__main__":
    unittest.main()
