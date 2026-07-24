#!/usr/bin/env python3
"""OKF validator (kube-agents Phase 0, 06 §5).

Validates an Operational Knowledge Framework tree: every `*.md` entry must carry YAML frontmatter
with a non-empty `type`, and every relative markdown link must resolve to an existing file.

Usage:
    python3 local-dev/okf-validate.py [KNOWLEDGE_DIR]

Default KNOWLEDGE_DIR: examples/gitops-repo/knowledge

Exit code 0 = all good; 1 = one or more violations (prints them). No third-party deps.
"""
from __future__ import annotations

import os
import re
import sys

# Canonical starting types (06 §5). `type` is an OPEN convention, so unknown types are a note, not
# an error — only a missing/empty `type` fails.
CANONICAL_TYPES = {
    "index",
    "cluster-blueprint",
    "tenancy-model",
    "runbook",
    "metric-definition",
    "escalation",
    "observation",
}

LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")  # [text](target), skip images ![...]()
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm


def is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "#"))


def check_file(path: str, root: str, errors: list[str], notes: list[str]) -> None:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rel = os.path.relpath(path, root)

    fm = parse_frontmatter(text)
    if fm is None:
        errors.append(f"{rel}: missing YAML frontmatter (--- block)")
    elif not fm.get("type"):
        errors.append(f"{rel}: frontmatter missing non-empty `type`")
    elif fm["type"] not in CANONICAL_TYPES:
        notes.append(f"{rel}: non-canonical type '{fm['type']}' (allowed — open convention)")

    for target in LINK_RE.findall(text):
        if is_external(target):
            continue
        # Resolve the path portion of a CommonMark inline link. Handle an optional link title
        # `[t](path.md "Title")` / `path.md 'Title'` and the angle-bracket form `[t](<path.md>)`
        # before stripping any `#anchor` — else the title/brackets are treated as part of the path
        # and a valid link is falsely reported broken.
        link_path = target.strip()
        if link_path.startswith("<") and ">" in link_path:
            link_path = link_path[1 : link_path.index(">")]
        else:
            parts = link_path.split(None, 1)  # path is the first whitespace-delimited token
            link_path = parts[0] if parts else ""
        link_path = link_path.split("#", 1)[0].strip()
        if not link_path:
            continue  # pure anchor
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), link_path))
        if not os.path.exists(resolved):
            errors.append(f"{rel}: broken link -> {target}")


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "examples/gitops-repo/knowledge"
    if not os.path.isdir(root):
        print(f"ERROR: knowledge dir not found: {root}", file=sys.stderr)
        return 1

    md_files = [
        os.path.join(dirpath, name)
        for dirpath, _, filenames in os.walk(root)
        for name in filenames
        if name.endswith(".md")
    ]
    if not md_files:
        print(f"ERROR: no markdown entries under {root}", file=sys.stderr)
        return 1

    errors: list[str] = []
    notes: list[str] = []
    for path in sorted(md_files):
        check_file(path, root, errors, notes)

    for note in notes:
        print(f"note: {note}")
    if errors:
        print(f"\nOKF validation FAILED ({len(errors)} issue(s)) in {root}:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"OKF validation PASSED: {len(md_files)} entr(y/ies) in {root} — all typed, links resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
