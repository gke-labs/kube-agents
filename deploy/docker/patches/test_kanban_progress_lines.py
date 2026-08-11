"""Unit tests for the kanban progress lines installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

import apply_kanban_progress_lines as applier
from kanban_handoff_clip import ELLIPSIS
from kanban_progress_lines import DEFAULT_NOTE_LIMIT, progress_note

RUN_URL = "https://github.com/gke-agentic/adamparco-infra/actions/runs/9912345678"


class ProgressNoteTest(unittest.TestCase):
    def test_a_deliberate_note_is_delivered(self):
        self.assertEqual(
            progress_note({"note": "Scanned 3 of 7 clusters; no drift so far."}),
            "Scanned 3 of 7 clusters; no drift so far.",
        )

    def test_an_auto_heartbeat_is_silent(self):
        # The ~2,100 heartbeat rows on the live board are all payload=None.
        # This empty return is the whole reason widening TERMINAL_KINDS does
        # not turn every tool call into a chat message.
        self.assertEqual(progress_note(None), "")

    def test_a_payload_without_a_note_is_silent(self):
        self.assertEqual(progress_note({}), "")
        self.assertEqual(progress_note({"stage": "scanning"}), "")
        self.assertEqual(progress_note({"note": None}), "")

    def test_a_blank_note_is_silent(self):
        self.assertEqual(progress_note({"note": ""}), "")
        self.assertEqual(progress_note({"note": "   \n "}), "")

    def test_a_non_mapping_payload_is_silent(self):
        for payload in ("a bare string", 42, ["note", "x"], object()):
            with self.subTest(payload=payload):
                self.assertEqual(progress_note(payload), "")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(progress_note({"note": "  working  \n"}), "working")

    def test_an_overlong_note_is_clipped_on_a_token_boundary(self):
        note = " ".join(f"step{i}" for i in range(200))
        clipped = progress_note({"note": note})
        self.assertLessEqual(len(clipped), DEFAULT_NOTE_LIMIT)
        self.assertTrue(clipped.endswith(ELLIPSIS))
        body = clipped[: -len(ELLIPSIS)]
        for token in body.split():
            self.assertIn(token, note.split(), f"token {token!r} was cut")

    def test_a_url_is_dropped_rather_than_severed(self):
        note = ("filler " * 60) + RUN_URL
        clipped = progress_note({"note": note})
        self.assertLessEqual(len(clipped), DEFAULT_NOTE_LIMIT)
        # Either the whole link or none of it — never a prefix that 404s.
        self.assertNotIn("https://", clipped)

    def test_a_note_that_ends_in_a_url_within_budget_keeps_it_whole(self):
        note = "Kicked off the rollout: " + RUN_URL
        self.assertLess(len(note), DEFAULT_NOTE_LIMIT)
        self.assertIn(RUN_URL, progress_note({"note": note}))

    def test_the_limit_is_honoured_at_every_width(self):
        note = "Reconciling the fleet inventory across every managed cluster. " * 10
        for limit in range(1, 320):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(progress_note({"note": note}, limit)), limit)

    def test_the_default_limit_is_a_ping_not_a_report(self):
        # Deliberately far below the completion handoff's 1200: a worker with
        # more than this to say should be completing the card, not pinging it.
        self.assertLessEqual(DEFAULT_NOTE_LIMIT, 500)


# --- the applier's own safety net -------------------------------------------
#
# The inserted branch ends in `continue`, which is only legal where the anchor
# actually sits: inside the notifier's per-event loop. If upstream ever moves
# that elif chain out of a loop, the anchor would still match and the patch
# would still apply — and ast.parse() would still say the file is fine, because
# a misplaced `continue` is a compile-time error, not a syntax one. The build
# would ship an image whose gateway raises on import.

_KINDS_ANCHOR_HOST = "class Watcher:\n    def run(self):\n"
_RENDER_ANCHOR_HOST_TAIL = (
    "            if True:\n"
    "                if True:\n"
    "                    if True:\n"
    '                        if kind == "completed":\n'
    '                            msg = "done"\n'
)
_RENDER_ANCHOR_BODY = '                            msg = "status"\n'


def _watchers_source(loop: bool) -> str:
    """A stand-in kanban_watchers.py carrying both anchors at their real indents.

    ``loop=False`` puts the elif chain at the same indentation but outside any
    loop — the shape the applier has to reject.
    """
    outer = (
        "    for d in deliveries:\n        for ev in d:\n"
        if loop
        else "    if deliveries:\n        if deliveries:\n"
    )
    return (
        _KINDS_ANCHOR_HOST
        + applier.KINDS_ANCHOR
        + "\n\ndef notify(deliveries, kind, board_tag, tag):\n"
        + outer
        + _RENDER_ANCHOR_HOST_TAIL
        + applier.RENDER_ANCHOR
        + _RENDER_ANCHOR_BODY
    )


_TOOLS_SOURCE = (
    "HEARTBEAT = {\n"
    + applier.SCHEMA_ANCHOR
    + '    "parameters": {\n'
    + '        "properties": {\n'
    + applier.NOTE_ANCHOR
    + "        },\n"
    + "    },\n"
    + "}\n"
)


class ApplierTest(unittest.TestCase):
    def _tree(self, loop: bool) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "gateway").mkdir()
        (root / "tools").mkdir()
        (root / "gateway" / "kanban_watchers.py").write_text(_watchers_source(loop))
        (root / "tools" / "kanban_tools.py").write_text(_TOOLS_SOURCE)
        return root

    def test_it_patches_a_tree_that_matches_the_shipped_image(self):
        root = self._tree(loop=True)
        applier.apply(root)
        patched = (root / "gateway" / "kanban_watchers.py").read_text()
        self.assertIn('elif kind == "heartbeat":', patched)
        self.assertIn('"block_loop_detected", "heartbeat")', patched)
        self.assertIn("from gateway.kanban_progress_lines import progress_note", patched)
        # _WAKE_KINDS is untouched by design; the point of a progress line is
        # that it costs no LLM turn.
        self.assertIn("A one-line progress update", (root / "tools" / "kanban_tools.py").read_text())

    def test_a_continue_outside_a_loop_fails_the_build(self):
        root = self._tree(loop=False)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("no longer parses", str(caught.exception))
        # ast.parse would have waved this through, which is why it is gone.
        ast.parse(_watchers_source(loop=False))

    def test_a_drifted_anchor_fails_the_build(self):
        root = self._tree(loop=True)
        path = root / "gateway" / "kanban_watchers.py"
        path.write_text(path.read_text().replace(applier.RENDER_ANCHOR, ""))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("found 0", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
