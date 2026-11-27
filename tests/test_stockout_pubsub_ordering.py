"""The stockout sink is created last and destroyed first behind its publish grant.

Cloud Logging starts exporting the moment a sink exists and keeps exporting for
some minutes after one is deleted. Binding the stockout sink's publish grant
after the sink (by reading `writer_identity` off it) opens an apply-side window
where Cloud Logging exports to an ungranted topic and emails every project
owner (`topic_permission_denied`), and deleting the topic or revoking the grant
immediately after deleting the sink opens a destroy-side `topic_not_found`
window.

`module.gke_cluster` passes the stockout drain, publish grant, and topic into
`create_after` (alongside `drift_pubsub`'s teardown gate) while leaving out
`google_logging_project_sink.stockout_alerts`, which reads `cluster_name`.
Terraform destroys `sink -> cluster -> drain -> grant & topic` with no cycle.
Inside `terraform/examples/full-install/main.tf`, the stockout ordering chain
is:

    google_project_service_identity.stockout_logging
      -> time_sleep.stockout_logging_identity                  (apply-side agent wait)
        -> google_pubsub_topic_iam_member.stockout_sink_writer (grant before sink)
          -> time_sleep.stockout_sink_drain                    (destroy-side drain)
            -> google_logging_project_sink.stockout_alerts     (sink last)

The first link is a `triggers` reference (`google_project_service_identity.stockout_logging[0].id`);
the last three are `depends_on` edges. This suite tokenizes the HCL through
`test_terraform_module_tests` and `test_drift_pubsub_ordering` helpers so
commented-out edges or explanatory comments do not pass or fail the checks.

Run:
  python3 -m unittest tests/test_stockout_pubsub_ordering.py -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_MAIN = REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
COMPOSITION_PROVIDERS = REPO_ROOT / "terraform" / "examples" / "full-install" / "providers.tf"
COMPOSITION_VARIABLES = REPO_ROOT / "terraform" / "examples" / "full-install" / "variables.tf"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tests.test_drift_pubsub_ordering import (
        _block_strings,
        _code_text,
        _depends_on_references,
        _local_references,
        _locals_definitions,
        _reference_text,
        _resource_body,
    )
    from tests.test_terraform_module_tests import (
        _EQUALS,
        _OPEN,
        _OTHER,
        _WORD,
        _blocks,
        _nested_blocks,
        _providers_in,
        _tokens,
    )
except ImportError:  # run from inside tests/
    from test_drift_pubsub_ordering import (
        _block_strings,
        _code_text,
        _depends_on_references,
        _local_references,
        _locals_definitions,
        _reference_text,
        _resource_body,
    )
    from test_terraform_module_tests import (
        _EQUALS,
        _OPEN,
        _OTHER,
        _WORD,
        _blocks,
        _nested_blocks,
        _providers_in,
        _tokens,
    )

_RESOURCE = "resource"
_MODULE = "module"
_DATA = "data"
_LOCALS = "locals"
_LIFECYCLE = "lifecycle"
_POSTCONDITION = "postcondition"
_TIME_PROVIDER = "time"
_CLUSTER_MODULE = "gke_cluster"
_CREATE_AFTER_ATTR = "create_after"
_TRIGGERS_ATTR = "triggers"
_BRACKET_OPEN = "["
_BRACKET_CLOSE = "]"

TOPIC = ("google_pubsub_topic", "stockout_alerts")
SUBSCRIPTION = ("google_pubsub_subscription", "stockout_alerts")
PROJECT_DATA = ("google_project", "stockout")
SERVICE_IDENTITY = ("google_project_service_identity", "stockout_logging")
IDENTITY_WAIT = ("time_sleep", "stockout_logging_identity")
GRANT = ("google_pubsub_topic_iam_member", "stockout_sink_writer")
DRAIN = ("time_sleep", "stockout_sink_drain")
SINK = ("google_logging_project_sink", "stockout_alerts")

AGENT_SUBSCRIBER = ("google_pubsub_subscription_iam_member", "stockout_agent_subscriber")
AGENT_VIEWER = ("google_pubsub_subscription_iam_member", "stockout_agent_viewer")

IDENTITY_ID_REFERENCE = f"{SERVICE_IDENTITY[0]}.{SERVICE_IDENTITY[1]}[0].id"
IDENTITY_DURATION_REFERENCE = "var.stockout_pubsub_logging_identity_propagation_duration"
PROJECT_NUMBER_REFERENCE = f"{_DATA}.{PROJECT_DATA[0]}.{PROJECT_DATA[1]}[0].number"
REQUIRED_APIS_REFERENCE = "google_project_service.required"

TOPIC_ID_REFERENCE = f"{TOPIC[0]}.{TOPIC[1]}[0].id"
DRAIN_ID_REFERENCE = f"{DRAIN[0]}.{DRAIN[1]}[0].id"
GRANT_ID_REFERENCE = f"{GRANT[0]}.{GRANT[1]}[0].id"
SUBSCRIPTION_ID_REFERENCE = f"{SUBSCRIPTION[0]}.{SUBSCRIPTION[1]}[0].id"
SINK_ADDRESS = f"{SINK[0]}.{SINK[1]}"
SINK_ADDRESS_RE = re.compile(rf"(?<![\w.]){re.escape(SINK[0])}\s*\.\s*{re.escape(SINK[1])}(?![\w-])")
SINK_WRITER_IDENTITY_REF = f"{SINK_ADDRESS}[0].writer_identity"
EXPECTED_WRITER_LOCAL = "local.expected_stockout_sink_writer_identity"
POSTCONDITION_CONDITION = f"self.writer_identity=={EXPECTED_WRITER_LOCAL}"
PUBLISHER_ROLE = "roles/pubsub.publisher"
SUBSCRIBER_ROLE = "roles/pubsub.subscriber"
VIEWER_ROLE = "roles/pubsub.viewer"

_UNBOUND_AGENT = (
    "the publish grant will run before GCP can bind the Logging service agent it "
    "names, failing apply with missing service account on projects without one"
)
_MAIL = (
    "Cloud Logging will export to a topic that does not exist or cannot be published "
    "to, and email every project owner"
)

REQUIRED_EDGES = (
    (GRANT, IDENTITY_WAIT, _UNBOUND_AGENT),
    (DRAIN, GRANT, _MAIL),
    (SINK, DRAIN, _MAIL),
)

REQUIRED_VARIABLES = (
    "stockout_pubsub_sink_writer_identity_override",
    "stockout_pubsub_sink_drain_duration",
    "stockout_pubsub_logging_identity_propagation_duration",
)


def _block_body(tokens: list, block_type: str, expected_labels: list[str], where: str) -> list:
    for labels, body in _blocks(tokens, block_type, len(expected_labels)):
        if labels == expected_labels:
            return body
    raise AssertionError(f'{where} declares no {block_type} {" ".join(expected_labels)}')


def _attribute_body(body: list, attr_name: str) -> list | None:
    """Tokens inside `attr_name = { ... }` or `attr_name = [ ... ]` at depth 1."""
    for index in range(len(body) - 2):
        (kind, value), depth = body[index]
        if depth != 1 or (kind, value) != (_WORD, attr_name):
            continue
        if body[index + 1][0][0] != _EQUALS:
            continue
        start_tok, start_depth = body[index + 2]
        if start_tok[0] == _OPEN:
            collected = []
            for tok, d in body[index + 3 :]:
                if d <= start_depth:
                    break
                collected.append((tok, d))
            return collected
        if start_tok[0] == _OTHER and start_tok[1] == _BRACKET_OPEN:
            bracket_depth = 1
            collected = []
            for tok, d in body[index + 3 :]:
                if tok[0] == _OTHER and tok[1] == _BRACKET_OPEN:
                    bracket_depth += 1
                elif tok[0] == _OTHER and tok[1] == _BRACKET_CLOSE:
                    bracket_depth -= 1
                    if bracket_depth == 0:
                        break
                collected.append((tok, d))
            return collected
    return None


def _references_stockout_sink(body: list, tokens: list) -> bool:
    """True when `body` (in code, string interpolations, or reachable locals) names `google_logging_project_sink.stockout_alerts`."""
    if SINK_ADDRESS_RE.search(_reference_text(body)):
        return True
    definitions = _locals_definitions(tokens)
    seen, pending = set(), list(_local_references(body))
    while pending:
        name = pending.pop()
        if name in seen or name not in definitions:
            continue
        seen.add(name)
        value = definitions[name]
        if SINK_ADDRESS_RE.search(_reference_text(value)):
            return True
        pending.extend(_local_references(value))
    return False


class StockoutPubsubOrderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main_tokens = _tokens(COMPOSITION_MAIN.read_text(encoding="utf-8"))
        cls.providers_text = COMPOSITION_PROVIDERS.read_text(encoding="utf-8")
        cls.variables_text = COMPOSITION_VARIABLES.read_text(encoding="utf-8")

    def test_the_sink_is_last_in_the_ordering_chain(self) -> None:
        for (dep_type, dep_name), (target_type, target_name), consequence in REQUIRED_EDGES:
            with self.subTest(dependent=dep_name, target=target_name):
                body = _resource_body(self.main_tokens, dep_type, dep_name, str(COMPOSITION_MAIN))
                refs = _depends_on_references(body)
                self.assertIsNotNone(
                    refs,
                    f"{dep_type}.{dep_name} declares no depends_on -> {target_type}.{target_name}; {consequence}",
                )
                self.assertIn(
                    f"{target_type}.{target_name}",
                    refs or [],
                    f"{dep_type}.{dep_name} must depend_on {target_type}.{target_name}; {consequence}",
                )

    def test_logging_identity_wait_tracks_identity_and_duration(self) -> None:
        wait_body = _resource_body(self.main_tokens, *IDENTITY_WAIT, str(COMPOSITION_MAIN))
        triggers_body = _attribute_body(wait_body, _TRIGGERS_ATTR)
        self.assertIsNotNone(
            triggers_body,
            "time_sleep.stockout_logging_identity must declare a triggers map",
        )
        triggers_code = _code_text(triggers_body or [])
        self.assertIn(IDENTITY_ID_REFERENCE, triggers_code)
        self.assertIn(IDENTITY_DURATION_REFERENCE, triggers_code)

    def test_service_identity_waits_on_required_apis_and_project_lookup_does_not_defer(
        self,
    ) -> None:
        identity_body = _block_body(
            self.main_tokens, _RESOURCE, list(SERVICE_IDENTITY), str(COMPOSITION_MAIN)
        )
        identity_refs = _depends_on_references(identity_body) or []
        self.assertIn(REQUIRED_APIS_REFERENCE, identity_refs)

        project_body = _block_body(
            self.main_tokens, _DATA, list(PROJECT_DATA), str(COMPOSITION_MAIN)
        )
        project_refs = _depends_on_references(project_body) or []
        self.assertNotIn(
            REQUIRED_APIS_REFERENCE,
            project_refs,
            "data.google_project.stockout must not depend on google_project_service.required; "
            "otherwise any API change defers project.number to apply and replaces ForceNew member while the sink stays live",
        )
        self.assertEqual(project_refs, [])

        locals_refs = " ".join(
            _reference_text(body) for _labels, body in _blocks(self.main_tokens, _LOCALS, 0)
        )
        self.assertIn(PROJECT_NUMBER_REFERENCE, locals_refs)

    def test_grant_precedes_sink_and_does_not_read_sink_writer_identity(self) -> None:
        body = _resource_body(self.main_tokens, *GRANT, str(COMPOSITION_MAIN))
        code = _code_text(body)
        refs_text = _reference_text(body)
        self.assertNotIn(SINK_WRITER_IDENTITY_REF, code)
        self.assertNotIn(SINK_WRITER_IDENTITY_REF, refs_text)
        self.assertFalse(
            _references_stockout_sink(body, self.main_tokens),
            f"{GRANT[0]}.{GRANT[1]} must not reference {SINK_ADDRESS} directly, in an interpolation, or via locals",
        )
        self.assertIn(EXPECTED_WRITER_LOCAL, code)
        self.assertIn(TOPIC_ID_REFERENCE, code)
        self.assertIn(PUBLISHER_ROLE, _block_strings(body))

    def test_sink_carries_writer_identity_postcondition(self) -> None:
        sink_body = _resource_body(self.main_tokens, *SINK, str(COMPOSITION_MAIN))
        lifecycle_blocks = _nested_blocks(sink_body, _LIFECYCLE)
        self.assertTrue(lifecycle_blocks, "stockout_alerts sink must declare a lifecycle block")
        postconditions = [
            p_body
            for lc_body in lifecycle_blocks
            for p_body in _nested_blocks(lc_body, _POSTCONDITION)
        ]
        self.assertTrue(postconditions, "stockout_alerts sink must declare a postcondition")
        self.assertTrue(
            any(POSTCONDITION_CONDITION in _code_text(p_body) for p_body in postconditions),
            f"stockout_alerts postcondition must verify {POSTCONDITION_CONDITION}",
        )

    def test_agent_subscription_grants_bind_by_id(self) -> None:
        for (rtype, rname), role in ((AGENT_SUBSCRIBER, SUBSCRIBER_ROLE), (AGENT_VIEWER, VIEWER_ROLE)):
            with self.subTest(grant=rname):
                body = _resource_body(self.main_tokens, rtype, rname, str(COMPOSITION_MAIN))
                self.assertIn(SUBSCRIPTION_ID_REFERENCE, _code_text(body))
                self.assertIn(role, _block_strings(body))

    def test_gke_cluster_teardown_gate_covers_stockout_without_sink_cycle(self) -> None:
        cluster_body = _block_body(self.main_tokens, _MODULE, [_CLUSTER_MODULE], str(COMPOSITION_MAIN))
        create_after_body = _attribute_body(cluster_body, _CREATE_AFTER_ATTR)
        self.assertIsNotNone(
            create_after_body,
            "module.gke_cluster must set create_after to order cluster teardown before ingress",
        )
        create_after_code = _code_text(create_after_body or [])
        for ref in (DRAIN_ID_REFERENCE, GRANT_ID_REFERENCE, TOPIC_ID_REFERENCE):
            self.assertIn(
                ref,
                create_after_code,
                f"module.gke_cluster.create_after must reach {ref} so cluster stops emitting before teardown",
            )
        self.assertFalse(
            _references_stockout_sink(cluster_body, self.main_tokens),
            f"module.{_CLUSTER_MODULE} must not reference {SINK_ADDRESS} directly, inside an interpolation, or via locals",
        )
        depends_refs = _depends_on_references(cluster_body) or []
        for pair in (DRAIN, GRANT, TOPIC, SINK):
            self.assertNotIn(f"{pair[0]}.{pair[1]}", depends_refs)

    def test_time_provider_declared_in_composition_providers(self) -> None:
        self.assertIn(
            _TIME_PROVIDER,
            _providers_in(self.providers_text),
            "providers.tf must declare hashicorp/time in required_providers",
        )

    def test_stockout_override_and_timer_variables_declared(self) -> None:
        for var_name in REQUIRED_VARIABLES:
            self.assertIn(f'variable "{var_name}"', self.variables_text)


class StockoutOrderingHelpersRegressionTest(unittest.TestCase):
    def test_attribute_body_stops_at_map_closing_brace_and_handles_nesting(self) -> None:
        hcl = f"""
resource "{IDENTITY_WAIT[0]}" "{IDENTITY_WAIT[1]}" {{
  {_TRIGGERS_ATTR} = {{
    nested = {{
      duration = {IDENTITY_DURATION_REFERENCE}
    }}
  }}
  depends_on = [{IDENTITY_ID_REFERENCE}]
}}
"""
        wait_body = _resource_body(_tokens(hcl), *IDENTITY_WAIT, "synthetic.tf")
        triggers_body = _attribute_body(wait_body, _TRIGGERS_ATTR)
        self.assertIsNotNone(triggers_body)
        triggers_code = _code_text(triggers_body or [])
        self.assertIn(IDENTITY_DURATION_REFERENCE, triggers_code)
        self.assertNotIn(IDENTITY_ID_REFERENCE, triggers_code)

        list_hcl = f"""
module "{_CLUSTER_MODULE}" {{
  {_CREATE_AFTER_ATTR} = ["]", "[", {DRAIN_ID_REFERENCE}]
  ignored = {TOPIC_ID_REFERENCE}
}}
"""
        cluster_body = _block_body(_tokens(list_hcl), _MODULE, [_CLUSTER_MODULE], "synthetic.tf")
        list_body = _attribute_body(cluster_body, _CREATE_AFTER_ATTR)
        self.assertIsNotNone(list_body)
        list_code = _code_text(list_body or [])
        self.assertIn(DRAIN_ID_REFERENCE, list_code)
        self.assertNotIn(TOPIC_ID_REFERENCE, list_code)

    def test_references_stockout_sink_catches_interpolations_and_local_indirection(self) -> None:
        direct_interp_hcl = f"""
resource "{GRANT[0]}" "{GRANT[1]}" {{
  member = "serviceAccount:${{{SINK_WRITER_IDENTITY_REF}}}"
}}
"""
        direct_tokens = _tokens(direct_interp_hcl)
        direct_grant = _resource_body(direct_tokens, *GRANT, "synthetic.tf")
        self.assertTrue(_references_stockout_sink(direct_grant, direct_tokens))

        local_laundered_hcl = f"""
locals {{
  expected_stockout_sink_writer_identity = "${{{SINK_WRITER_IDENTITY_REF}}}"
}}
resource "{GRANT[0]}" "{GRANT[1]}" {{
  member = {EXPECTED_WRITER_LOCAL}
}}
"""
        local_tokens = _tokens(local_laundered_hcl)
        local_grant = _resource_body(local_tokens, *GRANT, "synthetic.tf")
        self.assertTrue(_references_stockout_sink(local_grant, local_tokens))

        cluster_interp_hcl = f"""
module "{_CLUSTER_MODULE}" {{
  {_CREATE_AFTER_ATTR} = ["${{{SINK_ADDRESS}[0].id}}"]
}}
"""
        cluster_interp_tokens = _tokens(cluster_interp_hcl)
        cluster_body = _block_body(cluster_interp_tokens, _MODULE, [_CLUSTER_MODULE], "synthetic.tf")
        self.assertTrue(_references_stockout_sink(cluster_body, cluster_interp_tokens))

        cluster_local_hcl = f"""
locals {{
  stockout_gate = ["${{{SINK_ADDRESS}[0].id}}"]
}}
module "{_CLUSTER_MODULE}" {{
  {_CREATE_AFTER_ATTR} = local.stockout_gate
}}
"""
        cluster_local_tokens = _tokens(cluster_local_hcl)
        cluster_local_body = _block_body(cluster_local_tokens, _MODULE, [_CLUSTER_MODULE], "synthetic.tf")
        self.assertTrue(_references_stockout_sink(cluster_local_body, cluster_local_tokens))


if __name__ == "__main__":
    unittest.main()
