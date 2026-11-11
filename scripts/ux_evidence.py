#!/usr/bin/env python3
"""Hold a pull request that changes what a user sees to a screenshot in its body.

`.github/workflows/ux-evidence.yml` runs this on every pull request. It reads
the changed files and the pull request body, and fails when a changed file is a
user-facing surface and the body carries neither an image nor the template's
ticked "No user-visible change" box. The rule is `AGENTS.md` "Pull Request
Hygiene"; the mechanics are `.agents/rules/pre_pr_review.md`, "UX change".

The surfaces are two lists. Chat output is the `paths:` frontmatter of
`.agents/rules/chat_ux.md`, the list `review-chat-ux` already scopes itself
by, read here rather than copied so the two cannot drift. Everything else a
user sees is `SURFACES` below. Test files never count: a golden file or a
fixture changes because a surface did.

A chat path also sets `chat=true` in `$GITHUB_OUTPUT`, which is what starts
the workflow's chat-rules job.

Run: git diff --name-only origin/main...HEAD | python3 scripts/ux_evidence.py --body-file body.md
Test: cd scripts && python3 -m unittest test_ux_evidence
"""

import argparse
import os
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CHAT_RULES = REPO_ROOT / ".agents" / "rules" / "chat_ux.md"

# What a user sees outside chat. The docs site counts for its rendering, not
# its prose: a page's words are in the diff already, while a component or a
# stylesheet shows only on the rendered page. The admin console counts whole,
# bar its tests and pins, because its views and its logic share files.
SURFACES = (
    "admin_console/**",
    "docs/site/astro.config.mjs",
    "docs/site/public/**",
    "docs/site/src/assets/**",
    "docs/site/src/components/**",
    "docs/site/src/styles/**",
    "install.sh",
    "upgrade.sh",
    "uninstall.sh",
    "scripts/installer/*.sh",
)

# Paths inside a surface that are not themselves seen.
NOT_SEEN = (
    "**/test_*.py",
    "**/*_test.go",
    "**/tests/**",
    "**/testdata/**",
    "**/*.md",
    "**/requirements*.txt",
)

FRONTMATTER_MARK = "---"
PATHS_KEY = "paths:"
LIST_ITEM_RE = re.compile(r"""^\s+-\s+["']?([^"'\s]+)["']?\s*$""")

COMMENT_RE = re.compile(r"<!--.*?(-->|\Z)", re.DOTALL)
FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
IMAGE_RES = (
    re.compile(r"!\[[^\]]*\]\(\s*\S+"),  # a Markdown image
    re.compile(r"<img\b[^>]*\bsrc\s*=", re.IGNORECASE),  # an HTML image
    re.compile(r"https://github\.com/user-attachments/assets/\S+"),  # a drag-and-drop upload
)
NO_CHANGE_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+No user-visible change\b", re.MULTILINE)

OUTPUT_ENV = "GITHUB_OUTPUT"
EXIT_MISSING = 1


def glob_to_regex(pattern):
    """A glob as Claude Code reads a rule's `paths:`: `**` spans directories, `*` one segment."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def chat_globs(rules_path=CHAT_RULES):
    """The `paths:` list in the chat rules' frontmatter."""
    lines = rules_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_MARK:
        raise ValueError(f"{rules_path} has no frontmatter")
    globs = []
    in_paths = False
    for line in lines[1:]:
        if line.strip() == FRONTMATTER_MARK:
            break
        if line.strip() == PATHS_KEY:
            in_paths = True
            continue
        item = LIST_ITEM_RE.match(line)
        if in_paths and item:
            globs.append(item.group(1))
        elif in_paths and not line.startswith(" "):
            in_paths = False
    if not globs:
        raise ValueError(f"{rules_path} lists no paths")
    return tuple(globs)


def _matches(path, globs):
    return any(glob_to_regex(g).match(path) for g in globs)


def classify(files, chat=None, surfaces=SURFACES):
    """Split changed files into (chat, other surfaces); files a user never sees drop out."""
    chat = chat_globs() if chat is None else chat
    chat_hits, surface_hits = [], []
    for path in files:
        if _matches(path, NOT_SEEN):
            continue
        if _matches(path, chat):
            chat_hits.append(path)
        elif _matches(path, surfaces):
            surface_hits.append(path)
    return chat_hits, surface_hits


def _visible(body):
    """The body as it renders: HTML comments and fenced blocks show no image."""
    return FENCE_RE.sub("", COMMENT_RE.sub("", body or ""))


def has_image(body):
    text = _visible(body)
    return any(r.search(text) for r in IMAGE_RES)


def no_change_ticked(body):
    return bool(NO_CHANGE_RE.search(_visible(body)))


def verdict(files, body, chat=None):
    """(passed, chat_changed, message) for one pull request."""
    chat_hits, surface_hits = classify(files, chat=chat)
    seen = chat_hits + surface_hits
    if not seen:
        return True, False, "No user-facing path changed."
    listing = "\n".join(f"  {p}" for p in seen)
    if has_image(body):
        return True, bool(chat_hits), f"User-facing paths changed and the body has a screenshot:\n{listing}"
    if no_change_ticked(body):
        return True, bool(chat_hits), f"User-facing paths changed; the author ticked 'No user-visible change':\n{listing}"
    message = (
        "This pull request changes what a user sees, and its body has no screenshot:\n"
        f"{listing}\n"
        "Add at least one screenshot of the new UX under 'UX change' (and a before shot when it "
        "changes existing output), or tick 'No user-visible change' if nothing a user sees moved. "
        "Mechanics: .agents/rules/pre_pr_review.md, 'UX change'."
    )
    return False, bool(chat_hits), message


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--body-file", required=True, help="the pull request body")
    parser.add_argument("--files", default="-", help="changed paths, one per line ('-' for stdin)")
    args = parser.parse_args(argv)

    body = pathlib.Path(args.body_file).read_text(encoding="utf-8")
    raw = sys.stdin.read() if args.files == "-" else pathlib.Path(args.files).read_text(encoding="utf-8")
    files = [line.strip() for line in raw.splitlines() if line.strip()]

    passed, chat_changed, message = verdict(files, body)
    print(message)
    output = os.environ.get(OUTPUT_ENV)
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"chat={'true' if chat_changed else 'false'}\n")
    return 0 if passed else EXIT_MISSING


if __name__ == "__main__":
    sys.exit(main())
