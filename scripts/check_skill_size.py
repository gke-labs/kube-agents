#!/usr/bin/env python3
"""Keep every skill document small enough for ``skill_view`` to return whole.

An agent opens a skill with Hermes' ``skill_view`` tool, and the file it asks
for comes back as the tool's result. Hermes caps one tool result at 100,000
characters (``DEFAULT_RESULT_SIZE_CHARS`` in its ``tools/budget_config.py``),
and all of one turn's results at 200,000. A result over the cap is not
returned: the agent gets a 1,500-character preview and the path of a file the
full text was saved to, and whether it reads that file is up to the model. A
fleet-audit worker that did not read it never learned the audit's first step.

The limit here sits well under the cap, because the result is the document
wrapped in JSON (escaping and the ``linked_files`` listing add a few percent),
a worker may open several references in one turn, and Hermes scales the cap
down to 15% of the context window for a model with less than about 170k
tokens. A document over the limit is split: a short ``SKILL.md`` that says when
to open each part, and the parts under ``references/``.

Checked: every document of every shipped skill, as
``deploy/docker/skill_documents.py`` defines both. Standard library only, so it
runs in CI and in a bare clone.

Usage::

    python3 scripts/check_skill_size.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The image build copies skill_documents.py beside check_skill_commands.py, so it
# lives in deploy/docker rather than here.
SKILL_DOCUMENTS_DIR = REPO / "deploy" / "docker"
sys.path.insert(0, str(SKILL_DOCUMENTS_DIR))

from skill_documents import shipped_skill_documents  # noqa: E402

# Hermes' cap on one tool result (DEFAULT_RESULT_SIZE_CHARS in tools/budget_config.py at the
# pinned tag). The limit below must stay under half of it, for the reasons in the docstring.
HERMES_RESULT_CAP_CHARS = 100_000
# Characters, as Hermes counts the result: len() of the decoded text.
LIMIT_CHARS = 40_000


def oversized(repo: Path, limit: int = LIMIT_CHARS) -> list[tuple[Path, int]]:
    """The documents longer than `limit` characters, with their lengths."""
    found = []
    for doc in shipped_skill_documents(repo):
        size = len(doc.read_text(encoding="utf-8"))
        if size > limit:
            found.append((doc, size))
    return found


def main() -> int:
    docs = shipped_skill_documents(REPO)
    over = oversized(REPO)
    for doc, size in over:
        print(
            f"::error file={doc.relative_to(REPO)}::{size:,} characters, over the "
            f"{LIMIT_CHARS:,}-character limit that keeps it under skill_view's cap. Keep SKILL.md short and move "
            "the detail to references/, one file per step, named from SKILL.md.",
            file=sys.stderr,
        )
    if over:
        return 1
    print(f"Skill sizes OK: {len(docs)} documents, each at most {LIMIT_CHARS:,} characters.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
