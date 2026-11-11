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
user sees is `SURFACES` below. Test files never count, and a surface's notes
and pins (`NOT_RENDERED`) do not either.

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
# stylesheet shows only on the rendered page. The two browser UIs an install
# ships, the web console and the A2A console, count whole, bar their tests and
# pins, because their pages and their handlers share files.
SURFACES = (
    "a2a/web/**",
    "a2a/cmd/web-console/**",
    "charts/kube-agents/templates/web-console.yaml",
    "a2a/console/**",
    "a2a/cmd/console/**",
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

# Tests anywhere: a golden file or a fixture changes because a surface did.
# The shared chat cases the chatmsg lint and the conformance tests read. A
# change to them is a test, so it needs no screenshot, but it is exactly what
# the chat job exists to run.
CHAT_CASES = (
    "tests/conformance/chat/**",
    "tests/conformance/chat-ux/**",
)

TEST_PATHS = (
    "**/test_*.py",
    "**/*_test.go",
    "**/*.test.*",
    "**/*.spec.*",
    "**/tests/**",
    "**/testdata/**",
)

# Files inside a SURFACES directory that never render: its notes and its pins.
# Not applied to chat paths, where a `.md` is a SOUL or a message template.
NOT_RENDERED = (
    "**/*.md",
    "**/package.json",
    "**/package-lock.json",
    "**/tsconfig*.json",
    "**/vite.config.*",
    "a2a/web/dev/**",
)

# Glob syntax glob_to_regex does not translate; a pattern using it is refused
# rather than matched literally.
UNSUPPORTED_GLOB_CHARS = "{}[]!"

FRONTMATTER_MARK = "---"
PATHS_KEY = "paths:"
LIST_ITEM_RE = re.compile(r"""^\s+-\s+["']?([^"'\s]+)["']?\s*$""")

# What renders no image, as one alternation so a single left-to-right pass
# takes whichever opens first, the way a renderer reads: an HTML comment, which
# ends at its first `-->` whatever is inside it (or runs to the end, unclosed);
# a fenced block at any indent (list items indent theirs), closed by a line of
# at least the same markers and nothing else, so a "```python" line opens and
# never closes; a code span, closed by a backtick run of its own length before
# a blank line ends the paragraph. An unclosed fence runs to the end of the
# body, as GitHub renders it, so an image after it shows as text.
HIDDEN_RE = re.compile(
    r"<!--.*?(?:-->|\Z)"
    r"|^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*(?:\n.*?^[ \t]*(?P=fence)[`~]*[ \t]*$|.*\Z)"
    r"|(?P<ticks>`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=ticks)(?!`)",
    re.DOTALL | re.MULTILINE,
)
IMAGE_RES = (
    # A Markdown image with a non-empty target; alt text may hold one level of
    # balanced brackets, as CommonMark allows.
    re.compile(r"!\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*[^\s)]+"),
    re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']?\s*[^\s"'>]+""", re.IGNORECASE),  # an HTML image
    re.compile(r"https://github\.com/user-attachments/assets/\S+"),  # a drag-and-drop upload
)
NO_CHANGE_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+No user-visible change\b", re.MULTILINE)

OUTPUT_ENV = "GITHUB_OUTPUT"
EXIT_MISSING = 1


def glob_to_regex(pattern):
    """A glob as Claude Code reads a rule's `paths:`: `**` spans directories, `*` one segment."""
    bad = sorted({c for c in pattern if c in UNSUPPORTED_GLOB_CHARS})
    if bad:
        raise ValueError(f"glob {pattern!r} uses unsupported syntax: {''.join(bad)}")
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
        if not in_paths:
            continue
        item = LIST_ITEM_RE.match(line)
        if item:
            globs.append(item.group(1))
        elif line.startswith((" ", "\t")):
            # An item this reader cannot parse (a trailing comment, say) would
            # otherwise drop out of the list without a word.
            raise ValueError(f"{rules_path}: cannot read paths: entry {line!r}")
        else:
            in_paths = False
    if not globs:
        raise ValueError(f"{rules_path} lists no paths")
    return tuple(globs)


def _matches(path, globs):
    return any(glob_to_regex(g).match(path) for g in globs)


def _any_matches(files, globs):
    return any(_matches(path, globs) for path in files)


def classify(files, chat=None, surfaces=SURFACES):
    """Split changed files into (chat, other surfaces); files a user never sees drop out."""
    chat = chat_globs() if chat is None else chat
    chat_hits, surface_hits = [], []
    for path in files:
        if _matches(path, TEST_PATHS):
            continue
        if _matches(path, chat):
            chat_hits.append(path)
        elif _matches(path, surfaces) and not _matches(path, NOT_RENDERED):
            surface_hits.append(path)
    return chat_hits, surface_hits


def _visible(body):
    """The body as it renders: code and HTML comments show no image."""
    return HIDDEN_RE.sub("", body or "")


def has_image(body):
    text = _visible(body)
    return any(r.search(text) for r in IMAGE_RES)


def no_change_ticked(body):
    return bool(NO_CHANGE_RE.search(_visible(body)))


def verdict(files, body, chat=None):
    """(passed, chat_changed, message) for one pull request."""
    chat_hits, surface_hits = classify(files, chat=chat)
    chat_changed = bool(chat_hits) or _any_matches(files, CHAT_CASES)
    seen = chat_hits + surface_hits
    if not seen:
        return True, chat_changed, "No user-facing path changed."
    listing = "\n".join(f"  {p}" for p in seen)
    if has_image(body):
        return True, chat_changed, f"User-facing paths changed and the body has a screenshot:\n{listing}"
    if no_change_ticked(body):
        return True, chat_changed, f"User-facing paths changed; the author ticked 'No user-visible change':\n{listing}"
    message = (
        "This pull request changes what a user sees, and its body has no screenshot:\n"
        f"{listing}\n"
        "Add at least one screenshot of the new UX under 'UX change' (and a before shot when it "
        "changes existing output), or tick 'No user-visible change' if nothing a user sees moved. "
        "Mechanics: .agents/rules/pre_pr_review.md, 'UX change'."
    )
    return False, chat_changed, message


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
