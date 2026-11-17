"""Unit tests for apply_api_platform_hint.py.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier's contract against a miniature PLATFORM_HINTS table with the
api_server entry as v0.21.3 ships it.
"""

import tempfile
import unittest
from pathlib import Path

from apply_api_platform_hint import MARKER, PROMPT_BUILDER_RELATIVE, RENDERING_ANCHOR, apply

# The api_server entry's opening, as shipped (the media sentences follow on the same string).
HINTS_STUB = '''PLATFORM_HINTS = {
    "api_server": (
        "You're responding through an API server. The rendering layer is unknown — assume plain text. No markdown "
        "formatting (no asterisks, bullets, headers, code fences). Treat this like a conversation, not a document. "
        "Keep responses brief and natural. File/media delivery: images referenced as MEDIA:/absolute/path tags "
        "are inlined as base64 data URLs."
    ),
}
'''


def stage(source=HINTS_STUB):
    root = Path(tempfile.mkdtemp())
    path = root / PROMPT_BUILDER_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(source)
    return root


class ApplierTest(unittest.TestCase):
    def test_the_rendering_claim_is_replaced_and_the_media_sentences_stay(self):
        root = stage()
        apply(root)
        patched = (root / PROMPT_BUILDER_RELATIVE).read_text()
        self.assertIn(MARKER, patched)
        self.assertNotIn("assume plain text", patched)
        self.assertNotIn("no asterisks, bullets, headers, code fences", patched)
        self.assertIn("pipe tables all render", patched)
        self.assertIn("Keep responses brief and natural. File/media delivery:", patched, "upstream's media sentences stay")
        compile(patched, "prompt_builder.py", "exec")

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_is_refused(self):
        root = stage(HINTS_STUB.replace("assume plain text", "assume plain-text"))
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertNotIn(MARKER, (root / PROMPT_BUILDER_RELATIVE).read_text())
        self.assertIn(RENDERING_ANCHOR.split("\n")[1], HINTS_STUB, "the stub carries the anchor's second line")


if __name__ == "__main__":
    unittest.main()
