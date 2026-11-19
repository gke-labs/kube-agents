"""No Python file masks private keys with `PRIVATE_KEY_PATTERN.sub`.

The pattern's `sub` reads to the end of the text from each BEGIN line that no
END line follows, so its time is quadratic on a text that repeats a key
header. `AuditRedactor.mask_private_keys` gives the same output in one pass.
This test fails when a Python file writes the slow call again in one of its
plain spellings. It cannot see a call through another name for the pattern.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
# The directories a repository walk never reads: the set the Python test
# discovery guard keeps for the same walk (dependency and tool folders, git's
# store, and `.claude`, where a review command leaves worktrees of other
# branches inside a maintainer's checkout).
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from test_test_discovery import IGNORED_NAMES as _WALK_EXCLUDED_NAMES  # noqa: E402

_PATTERN_NAME = "PRIVATE_KEY_PATTERN"
# The pattern's own `sub` or `subn`, or the pattern given to `re.sub` or `re.subn`.
_SLOW_CALL = re.compile(r"PRIVATE_KEY_PATTERN\s*\.\s*subn?\s*\(|\bre\s*\.\s*subn?\s*\(\s*[\w.]*PRIVATE_KEY_PATTERN\b")
_PYTHON_SUFFIX = ".py"
# The files that called the slow form before; the walk must reach them.
_FILES_THE_WALK_MUST_REACH = (
    "agents/chat/defaults/plugins/common/redactor.py",
    "charts/kube-agents/files/redactor.py",
    "agents/platform/scripts/worker_transcript.py",
)


class PrivateKeyMaskCallersTest(unittest.TestCase):
    def test_no_python_file_calls_the_slow_form(self):
        seen = set()
        slow = []
        for directory, names, files in os.walk(_REPO_ROOT):
            names[:] = [name for name in names if name not in _WALK_EXCLUDED_NAMES]
            for file in files:
                path = pathlib.Path(directory, file)
                # A link may point outside the repository; its target is read where it lives.
                if not file.endswith(_PYTHON_SUFFIX) or path.is_symlink():
                    continue
                relative = path.relative_to(_REPO_ROOT).as_posix()
                seen.add(relative)
                text = path.read_text(encoding="utf-8", errors="replace")
                if _PATTERN_NAME in text and _SLOW_CALL.search(text):
                    slow.append(relative)
        for relative in _FILES_THE_WALK_MUST_REACH:
            self.assertIn(relative, seen)
        self.assertEqual(slow, [], "call AuditRedactor.mask_private_keys in place of PRIVATE_KEY_PATTERN.sub")


if __name__ == "__main__":
    unittest.main()
