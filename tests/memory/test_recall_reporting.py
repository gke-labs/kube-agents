#!/usr/bin/env python3
"""Regression test for #113: a read must name its outcome.

The defect it locks down: the stock Hindsight tools answer an empty result with
the bare string ``"No relevant memories found."``, which is indistinguishable
from "the store never answered" and reads to a model as "no such record exists".
In the scale test a specialist declared a real ADR "not recorded anywhere
retrievable" while the ADR's text sat in the store it was searching.

Asserts the three outcomes stay distinguishable — ``found`` / ``no_match`` /
``unreachable`` — that each one reports the search it ran, and that the scope
tag filter still narrows correctly, since the fix replaced the stock tool
delegation (which mutated ``_recall_tags`` around the call) with a direct client
call.

Standalone: plain asserts, no pytest. Needs Hermes on the path for
``agent.memory_provider`` and ``tools.registry``; it never reaches a real
Hindsight — the client is stubbed.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_recall_reporting.py

Inside the agent image, Hermes is already importable:

    /opt/hermes/.venv/bin/python3 tests/memory/test_recall_reporting.py
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

from kube_agents_memory import SHARED_TAG, KubeAgentsMemoryProvider  # noqa: E402

PLUGIN_DIR = os.path.join(
    _REPO, "agents", "chat", "plugins", "memory", "kube_agents_memory"
)


def provider(*, results=None, recall_exc=None, reflect_text="", reflect_exc=None):
    """A provider wired to a stub client, plus a dict recording its call kwargs."""
    p = KubeAgentsMemoryProvider()
    p._user_tag = "user:alice"
    calls = {}

    class StubClient:
        def arecall(self, **kw):
            calls["recall"] = kw
            if recall_exc:
                raise recall_exc
            return SimpleNamespace(results=results or [])

        def areflect(self, **kw):
            calls["reflect"] = kw
            if reflect_exc:
                raise reflect_exc
            return SimpleNamespace(text=reflect_text)

    p._hindsight = SimpleNamespace(
        _bank_id="kube-agents-memory",
        _budget="mid",
        _recall_max_tokens=4096,
        _recall_types=["observation"],
        _recall_tags=["user:alice", SHARED_TAG],
        _recall_tags_match="any_strict",
        _run_hindsight_operation=lambda op: op(StubClient()),
    )
    return p, calls


class TestRecallReporting(unittest.TestCase):
    def test_found_reports_matches_and_the_search(self):
        p, calls = provider(results=[SimpleNamespace(text="ADR-2026-081 mandates preemption tolerance.")])
        r = json.loads(p.handle_tool_call("memory_recall", {"query": "ADR-2026-081"}))
        self.assertEqual(r["status"], "found", r)
        self.assertEqual(r["matches"], 1, r)
        self.assertIn("ADR-2026-081 mandates", r["result"], r)
        self.assertEqual(r["searched"]["tags"], ["user:alice", SHARED_TAG], r)
        self.assertEqual(r["searched"]["layer"], ["observation"], r)
        self.assertEqual(calls["recall"]["tags_match"], "any_strict", calls)

    def test_no_match_is_not_nonexistence(self):
        p, calls = provider(results=[])
        r = json.loads(p.handle_tool_call("memory_recall", {"query": "ADR-2026-081", "scope": "shared"}))
        self.assertEqual(r["status"], "no_match", r)
        self.assertEqual(r["matches"], 0, r)
        self.assertIn("not the same as the record not existing", r["result"], r)
        self.assertEqual(r["searched"]["query"], "ADR-2026-081", r)
        self.assertEqual(r["searched"]["tags"], [SHARED_TAG], r)
        self.assertEqual(calls["recall"]["tags"], [SHARED_TAG], calls)

    def test_unreachable_is_a_distinct_outcome(self):
        p, _ = provider(recall_exc=RuntimeError("Cannot connect to host hindsight-api:8888"))
        r = json.loads(p.handle_tool_call("memory_recall", {"query": "ADR-2026-081"}))
        self.assertEqual(r["status"], "unreachable", r)
        self.assertIn("error", r, r)
        self.assertIn("nothing was searched", r["error"], r)
        self.assertEqual(r["searched"]["bank"], "kube-agents-memory", r)

    def test_scope_still_narrows_the_tag_filter(self):
        p, calls = provider(results=[SimpleNamespace(text="x")])
        p.handle_tool_call("memory_recall", {"query": "q", "scope": "personal"})
        self.assertEqual(calls["recall"]["tags"], ["user:alice"], calls)

    def test_reflect_reports_the_same_three_outcomes(self):
        p, calls = provider(reflect_text="   ")
        r = json.loads(p.handle_tool_call("memory_reflect", {"query": "who owns etcd"}))
        self.assertEqual(r["status"], "no_match", r)
        self.assertEqual(calls["reflect"]["tags"], ["user:alice", SHARED_TAG], calls)

        p, _ = provider(reflect_text="Etcd is owned by the storage team.")
        r = json.loads(p.handle_tool_call("memory_reflect", {"query": "who owns etcd"}))
        self.assertTrue(r["status"] == "found" and r["result"].startswith("Etcd"), r)

        p, _ = provider(reflect_exc=RuntimeError("503"))
        r = json.loads(p.handle_tool_call("memory_reflect", {"query": "q"}))
        self.assertEqual(r["status"], "unreachable", r)

    def test_the_stock_read_tool_is_no_longer_delegated_to(self):
        """The stock tool is what conflated the outcomes; nothing may route back.

        Scans every module in the package, not just the entry point — the read path
        lives in `session.py` and `client.py` since the split.
        """
        sources = sorted(f for f in os.listdir(PLUGIN_DIR) if f.endswith(".py"))
        self.assertGreaterEqual(len(sources), 4, sources)
        for name in sources:
            with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn('handle_tool_call("hindsight_', src, f"{name} delegates to the stock tool")
            for line in src.splitlines():
                if "No relevant memories found" in line:
                    # Only survives where it is quoted as the defect being described.
                    self.assertTrue(line.lstrip().startswith(("#", "tools answer", "string")), (name, line))

    def test_the_absence_rule_reaches_the_system_prompt(self):
        """The injected block cannot annotate its own silence, so the prompt does."""
        p, _ = provider()
        self.assertIn("Memory is a search, not an index", p.system_prompt_block())
        p._user_tag = ""  # shared-only variant
        self.assertIn("Memory is a search, not an index", p.system_prompt_block())


if __name__ == "__main__":
    unittest.main()
