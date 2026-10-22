"""A workload's install requirements come from a read guide, never from search snippets.

Asked for a cluster with Agent Substrate installed, the Platform Agent never
called the Developer Knowledge MCP though SOUL §3 item 4.1 named it, and built
the install from `web_search` snippets, a fork's chart included
(gke-labs/kube-agents#2935, #3697). The MCP's tools are deferred, and a worker
that stops at `search_documents` sees chunks, not guides.

The rule lives in prose and nothing executes it, so these tests pin the phrases
it is made of: a rewording that keeps the rule keeps them; one that drops the
load-search-read order or the stop fails here before it fails a dev-lane run.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_install_requirements_lookup.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SOUL = REPO_ROOT / "agents/platform/SOUL.md"
MIN_FILE_CHARS = 1000

# Item 4.1, from its lead to item 4.2's.
ITEM_LEAD = "**Plan from the workload, not the template.**"
NEXT_ITEM = "\n    2. **"
# The dispatch order, as the worker sees the tool names.
LOAD = 'tool_search("developer_knowledge")'
SEARCH = "mcp__developer_knowledge__search_documents"
READ = "mcp__developer_knowledge__get_documents(names=[<the chunk's parent>])"
CHUNK_IS_NOT_SOURCE = "A search chunk points at the source; it is not the source."
SNIPPETS_ARE_NOT_REQUIREMENTS = "its snippets are never the requirements"
MISSING_IS_A_STOP = "When no guide can be read, the requirements are missing, which is a stop, not a gap to fill"
BLOCK = 'kanban_block(kind="needs_input")'


def _item_4_1() -> str:
    soul = PLATFORM_SOUL.read_text(encoding="utf-8")
    if len(soul) < MIN_FILE_CHARS:
        raise AssertionError(f"{PLATFORM_SOUL} read back suspiciously short ({len(soul)} chars)")
    start = soul.index(ITEM_LEAD)
    return soul[start : soul.index(NEXT_ITEM, start)]


class InstallRequirementsLookupTest(unittest.TestCase):
    longMessage = False

    def test_item_loads_searches_then_reads_the_whole_page(self):
        item = _item_4_1()
        positions = []
        for phrase in (LOAD, SEARCH, READ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, item, f"§3 item 4.1 lost {phrase!r}")
            positions.append(item.find(phrase))
        self.assertEqual(sorted(positions), positions, "§3 item 4.1 no longer names load, search, read in that order")

    def test_item_says_a_chunk_or_snippet_is_not_the_requirements(self):
        item = _item_4_1()
        for phrase in (CHUNK_IS_NOT_SOURCE, SNIPPETS_ARE_NOT_REQUIREMENTS):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, item, f"§3 item 4.1 lost {phrase!r}")

    def test_an_unreadable_guide_is_a_stop(self):
        item = _item_4_1()
        self.assertIn(MISSING_IS_A_STOP, item, "§3 item 4.1 no longer makes an unreadable guide a stop")
        self.assertIn(BLOCK, item[item.index(MISSING_IS_A_STOP) :], "§3 item 4.1's stop no longer names kanban_block")


if __name__ == "__main__":
    unittest.main()
