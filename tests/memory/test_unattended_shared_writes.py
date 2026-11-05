#!/usr/bin/env python3
"""A session with no user identity can write shared memory, and is told how.

Cron runs and the k8s event watcher have always been allowed to write — they
are not read-only, and shared is a scope that needs no identity. In three
months not one of them called the tool, because everything they were shown
pointed the other way: ``scope`` defaulted to ``personal``, which is the single
scope such a session is refused, and the schema gated ``shared`` on a fact "the
user states" when there is no user in the room.

Nothing here grants a new capability. It stops the schema and the default from
contradicting the permission the session already has, so the tests are about
what an unattended agent is *shown* and what its first, unqualified call does.

Two other populations are asserted alongside every unattended one, because a
change that widened to either would leak one person's facts to everyone — a
worse bug than the one being fixed. The attributed DM is the obvious one. The
group thread is the trap: it has no ``_user_tag`` either, so anything keyed on
that alone treats a room full of named people as an empty room.

Standalone: plain asserts, no pytest. See ``test_recall_reporting.py`` for how
to run it.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_unattended_shared_writes.py
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERMES = os.environ.get("HERMES_ROOT") or "/opt/hermes"
if os.path.isdir(_HERMES):
    sys.path.insert(0, _HERMES)
sys.path.insert(0, os.path.join(_REPO, "agents", "chat", "plugins", "memory"))

try:
    from . import _stubs  # noqa: F401
except (ImportError, ValueError):
    import _stubs  # type: ignore # noqa: F401

from kube_agents_memory import (  # noqa: E402
    NO_IDENTITY_NOTICE,
    SHARED_SESSION_NOTICE,
    SHARED_TAG,
    KubeAgentsMemoryProvider,
)
from kube_agents_memory.prompts import tool_schemas  # noqa: E402


def provider(*, user_tag="", unattended=None):
    """A writable provider in one of the three live states, over a stub client.

    ``unattended`` defaults to "whatever matches ``user_tag``" so the common two
    cases stay one argument; pass ``unattended=False`` with no tag to build the
    group thread, which is the state that has no identity and is still full of
    people.
    """
    if unattended is None:
        unattended = not user_tag
    assert not (user_tag and unattended), "an attributed session is not unattended"
    p = KubeAgentsMemoryProvider()
    p._read_only = False
    p._user_tag = user_tag
    p._unattended = unattended
    if user_tag:
        p._personal_disabled_reason = ""
    else:
        p._personal_disabled_reason = (
            NO_IDENTITY_NOTICE if unattended else SHARED_SESSION_NOTICE
        )
    retained = {}

    class StubClient:
        def aretain_batch(self, **kw):
            retained.update(kw)
            return SimpleNamespace(id="doc-1")

        def arecall(self, **kw):
            retained["recall"] = kw
            return SimpleNamespace(results=[])

    p._hindsight = SimpleNamespace(
        _bank_id="kube-agents-memory",
        _budget="low",
        _recall_max_tokens=4096,
        _recall_types=["observation"],
        _run_hindsight_operation=lambda op: op(StubClient()),
    )
    return p, retained


def _write_scope(p):
    for s in p.get_tool_schemas():
        if s["name"] == "memory_retain":
            return s["parameters"]["properties"]["scope"]
    raise AssertionError("memory_retain is not advertised")


class TestUnattendedSharedWrites(unittest.TestCase):
    def test_an_unattended_write_with_no_scope_reaches_shared_memory(self):
        """The whole bug, in one call: the shape an agent writes without thinking.

        The old default was 'personal', so this exact call returned an error in
        every cron and event session there has ever been.
        """
        p, retained = provider()
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "Dataplane V2 is off on staging-cluster-1, so NetworkPolicies are inert."
        }))
        self.assertEqual(r.get("result"), "Stored in shared memory.", r)
        self.assertEqual(retained["items"][0]["tags"], [SHARED_TAG], retained)

    def test_an_attributed_write_with_no_scope_is_still_personal(self):
        """The guardrail on the line above: DMs must not start writing to everyone."""
        p, retained = provider(user_tag="user:alice")
        r = json.loads(p.handle_tool_call("memory_retain", {"content": "Alice prefers dry runs."}))
        self.assertEqual(r.get("result"), "Stored in personal memory.", r)
        self.assertEqual(retained["items"][0]["tags"], ["user:alice"], retained)

    def test_an_unqualified_write_in_a_group_thread_is_refused_not_published(self):
        """The mirror image of the headline test, and the reason it is keyed on
        `_unattended` rather than on `_user_tag` being empty.

        A space has no user tag and is not empty: every fact in it belongs to one
        of several named people. Defaulting the write to shared here would take
        "remember I'm on call next week" from one participant and publish it to
        every user of the install, with nothing said out loud about it.
        """
        p, retained = provider(unattended=False)
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "Alice is on call for networking next week.",
        }))
        self.assertIn("error", r, r)
        self.assertEqual(r["error"], SHARED_SESSION_NOTICE, r)
        self.assertFalse(retained, retained)

    def test_a_group_thread_can_still_write_shared_when_it_says_so(self):
        """Refusing the default must not amount to refusing the scope. A team-wide
        fact stated in a space is exactly what a deliberate shared write is for."""
        p, retained = provider(unattended=False)
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "Releases are cut on Tuesdays.", "scope": "shared",
        }))
        self.assertEqual(r.get("result"), "Stored in shared memory.", r)
        self.assertEqual(retained["items"][0]["tags"], [SHARED_TAG], retained)

    def test_a_group_thread_keeps_personal_in_its_schema(self):
        """Dropping 'personal' from the enum would remove the only way to say "this
        is one person's" — and with it the refusal that keeps the fact unpublished.
        The space is told, in the description, that personal is the default."""
        scope = _write_scope(provider(unattended=False)[0])
        self.assertEqual(scope["enum"], ["personal", "shared"], scope)
        self.assertNotIn("no user identity", scope["description"], scope)
        self.assertNotIn("nobody is present", scope["description"], scope)

    def test_asking_for_personal_without_an_identity_still_fails_loudly(self):
        """Defaulting to shared must not become 'silently reroute a personal write'.

        An explicit ``personal`` is a statement that this belongs to one person;
        quietly publishing it to every user instead would be a disclosure bug.
        """
        p, retained = provider()
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "secret", "scope": "personal",
        }))
        self.assertIn("error", r, r)
        self.assertIn("no user identity", r["error"], r)
        self.assertFalse(retained, retained)

    def test_the_unattended_schema_offers_shared_and_only_shared(self):
        """A scope the session is refused has no business in the enum."""
        scope = _write_scope(provider()[0])
        self.assertEqual(scope["enum"], ["shared"], scope)
        self.assertIn("only option", scope["description"], scope)
        # And the attributed session keeps both, or personal memory just vanished.
        self.assertEqual(_write_scope(provider(user_tag="user:x")[0])["enum"], ["personal", "shared"])

    def test_every_variant_carries_the_test_for_what_belongs(self):
        """The old wording ('a fact the user states') excluded the unattended case
        by construction. Whatever replaces it has to reach all three."""
        for p in (provider()[0], provider(unattended=False)[0], provider(user_tag="user:alice")[0]):
            d = _write_scope(p)["description"]
            self.assertIn("could not find out for itself", d, d)
            # The two exclusions are the point; a description that keeps only the
            # invitation would fill the corpus with stale state and self-echo.
            self.assertIn("query that instead", d, d)
            self.assertIn("conclusion you reached this session", d, d)

    def test_a_session_with_no_identity_is_not_told_capture_is_automatic(self):
        """It is not, for either of them — `_auto_retain` is off without a user tag,
        so the DM wording ('captured automatically at the end of a session') is a
        false reassurance in both. What follows it has to differ, though: the tool
        is the only route in for cron, and the last thing a space needs is
        encouragement to record what a named person just said."""
        def _retain_description(p):
            return next(s for s in p.get_tool_schemas() if s["name"] == "memory_retain")["description"]

        unattended = _retain_description(provider()[0])
        self.assertIn("only way anything you learn here is kept", unattended, unattended)
        self.assertNotIn("captured automatically at the end of a session", unattended, unattended)

        space = _retain_description(provider(unattended=False)[0])
        self.assertNotIn("only way anything you learn here is kept", space, space)
        self.assertIn("belongs to the whole team", space, space)

        self.assertIn("captured automatically", _retain_description(provider(user_tag="user:alice")[0]))

    def test_an_unqualified_write_in_an_unattributable_interactive_dm_is_refused_not_published(self):
        """When an A2A or interactive DM session loses its route or user_id, a human
        is still speaking (`_unattended = False`, `_personal_disabled_reason = NO_IDENTITY_NOTICE`).
        An unqualified write must default to `personal` and be refused rather than
        silently publishing the human's personal fact to `scope:shared`.
        """
        from unittest.mock import patch
        from kube_agents_memory import client as kam_client

        p = KubeAgentsMemoryProvider()
        retained = {}

        class StubClient:
            def aretain_batch(self, **kw):
                retained.update(kw)
                return SimpleNamespace(id="doc-1")

        stub_hs = SimpleNamespace(
            _bank_id="kube-agents-memory",
            _budget="low",
            _recall_max_tokens=4096,
            _recall_types=["observation"],
            _run_hindsight_operation=lambda op: op(StubClient()),
        )
        with patch.object(kam_client, "load_hindsight", return_value=stub_hs), \
             patch.object(kam_client, "apply_scoping"), \
             patch.object(kam_client, "ensure_bank"):
            p.initialize("a2a-ctx-unrouted")
        self.assertFalse(p._unattended)
        self.assertEqual(p._personal_disabled_reason, NO_IDENTITY_NOTICE)
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "My personal GKE cluster is dev-1.",
        }))
        self.assertIn("error", r, r)
        self.assertEqual(r["error"], NO_IDENTITY_NOTICE, r)
        self.assertFalse(retained, retained)

    def test_a_read_only_profile_is_unaffected_in_every_state(self):
        """Specialists stay barred; none of this reaches them."""
        for has_identity, unattended in ((True, False), (False, True), (False, False)):
            names = [
                s["name"] for s in tool_schemas(
                    read_only=True, has_identity=has_identity, unattended=unattended,
                )
            ]
            self.assertNotIn("memory_retain", names, names)

    def test_decision_memory_gitops_prs_opt_out_stores_shared_in_hindsight_without_pr(self):
        """When DECISION_MEMORY_GITOPS_PRS=false, shared writes skip Curator PR emission
        and store directly in Hindsight (`kube-agents-memory` under `scope:shared`).
        """
        from unittest.mock import patch
        from kube_agents_memory import client as kam_client

        p, retained = provider(unattended=False)
        with patch.dict(
            os.environ,
            {
                "NATS_URL": "nats://localhost:4222",
                "DECISION_MEMORY_GITOPS_PRS": "false",
            },
        ), patch.object(kam_client, "_resolve_curator_bin", return_value="/usr/local/bin/curator"), \
           patch("subprocess.run") as run_mock:
            r = json.loads(
                p.handle_tool_call(
                    "memory_retain",
                    {"content": "Keep shared rules in Hindsight without opening GitHub PRs.", "scope": "shared"},
                )
            )
            run_mock.assert_not_called()
            self.assertEqual(r.get("result"), "Stored in shared memory.", r)
            self.assertEqual(retained["items"][0]["tags"], [SHARED_TAG], retained)

    def test_handoff_checkpoint_stores_under_checkpoint_strategy_without_gitops_pr(self):
        """Iteration-budget handoff checkpoints store directly in Hindsight under
        CHECKPOINT_STRATEGY with `kind:handoff` and do not open a GitOps PR.
        """
        from unittest.mock import patch
        from kube_agents_memory import client as kam_client
        from kube_agents_memory.config_schema import CHECKPOINT_STRATEGY

        p, retained = provider(unattended=True)
        p._session_id = "sess-42"
        with patch.dict(
            os.environ,
            {
                "NATS_URL": "nats://localhost:4222",
                "DECISION_MEMORY_GITOPS_PRS": "true",
            },
        ), patch.object(kam_client, "emit_shared_decision") as emit_mock:
            r = json.loads(
                p.handle_tool_call(
                    "memory_retain",
                    {
                        "content": "Verified: checkout OOM is not caused by HPA. Remaining: inspect Redis pool.",
                        "kind": "handoff",
                    },
                )
            )
            emit_mock.assert_not_called()
            self.assertEqual(r.get("kind"), "handoff", r)
            self.assertEqual(retained["items"][0]["strategy"], CHECKPOINT_STRATEGY, retained)
            self.assertIn("kind:handoff", retained["items"][0]["tags"], retained)
            self.assertIn("session:sess-42", retained["items"][0]["tags"], retained)

    def test_recall_since_hours_filters_older_timestamps(self):
        """Shift-handover recall with `since_hours` filters out memories older than the lookback window."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from kube_agents_memory import client as kam_client

        p, _ = provider(unattended=True)
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(hours=2)).isoformat()
        stale_ts = (now - timedelta(hours=36)).isoformat()
        fake_results = [
            SimpleNamespace(text="Recent checkout handoff note", occurred_start=recent_ts),
            SimpleNamespace(text="Old staging alert from two days ago", occurred_start=stale_ts),
        ]
        with patch.object(kam_client, "recall", return_value=fake_results), \
             patch.object(kam_client, "recall_decisions", return_value=[]):
            r = json.loads(
                p.handle_tool_call(
                    "memory_recall",
                    {"query": "shift handover", "scope": "shared", "since_hours": 12},
                )
            )
            self.assertEqual(r["status"], "found", r)
            self.assertEqual(r["matches"], 1, r)
            self.assertEqual(r["searched"].get("since_hours"), 12, r)
            self.assertIn("Recent checkout handoff note", r["result"], r)
            self.assertNotIn("Old staging alert", r["result"], r)

    def test_external_doc_loader_ingests_okf_runbooks_and_blueprints(self):
        """load_external_doc_records ingests all OKF record types (runbook, cluster-blueprint,
        tenancy-model, metric-definition) as well as ADRs and RFCs.
        """
        import importlib.util
        from pathlib import Path
        import tempfile

        script_path = (
            Path(__file__).resolve().parents[2]
            / "agents/platform/skills/decision-memory/scripts/decision_memory.py"
        )
        spec = importlib.util.spec_from_file_location("decision_memory_mod", script_path)
        dm = importlib.util.module_from_spec(spec)
        sys.modules["decision_memory_mod"] = dm
        spec.loader.exec_module(dm)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "knowledge/runbooks").mkdir(parents=True)
            (repo / "knowledge/cluster-blueprints").mkdir(parents=True)
            (repo / "docs/adr").mkdir(parents=True)

            (repo / "knowledge/runbooks/0003-coredns-latency.md").write_text(
                "---\ntype: runbook\ntitle: CoreDNS High Latency Triage\nstatus: active\n---\n\nCheck upstream DNS.\n"
            )
            (repo / "knowledge/cluster-blueprints/0001-regional-gke.md").write_text(
                "# Regional GKE Production Blueprint\n\nUse 3 zones.\n"
            )
            (repo / "docs/adr/0012-postgres-failover.md").write_text(
                "# ADR 12: Regional Postgres Failover\n\nKeep standby replica.\n"
            )

            recs = dm.load_external_doc_records(repo)
            by_id = {r.id: r for r in recs}
            self.assertIn("RUN-0003", by_id)
            self.assertEqual(by_id["RUN-0003"].type, "runbook")
            self.assertIn("BP-0001", by_id)
            self.assertEqual(by_id["BP-0001"].type, "cluster-blueprint")
            self.assertIn("ADR-0012", by_id)
            self.assertEqual(by_id["ADR-0012"].type, "decision")


if __name__ == "__main__":
    unittest.main()
