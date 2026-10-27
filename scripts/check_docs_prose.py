#!/usr/bin/env python3
"""Fail when a published site page's prose regresses against its recorded ceiling.

``.agents/rules/documentation.md`` ("Write it straight") asks for prose that
leads with the fact, drops the filler and hype words, skips the "not X, it is
Y" epigram, and keeps its length to the task. Nothing in ``make docs-check``
read prose until now, so the site accumulated the opposite one pull request at
a time. This check counts five kinds of finding per page under
``docs/site/src/content/docs/``:

* **banned word** -- a word from ``BANNED_WORDS`` or a phrase from
  ``BANNED_PHRASES``, whole-word and case-insensitive, a phrase also when it
  is wrapped across two lines or spelt with a typographic apostrophe;
  ``leverage`` only as a verb (``leverage the``, ``leverages``,
  ``leveraging``), since the noun is ordinary English;
* **antithesis** -- the two epigram forms, ``is not a X[,:;--] it is Y`` and
  ``not (merely|just|simply) X, but Y``. A plain ``is a X, not a Y`` contrast
  ("a namespaced resource, not cluster-scoped") is a fact and is not flagged;
* **paragraph over ``PARAGRAPH_WORD_LIMIT`` words** -- a paragraph ends at a
  blank line, a heading, a list item, a table row or a ``:::`` directive line
  (a Starlight aside's label or closer);
* **table cell over ``CELL_WORD_LIMIT`` words**;
* **sentence over ``SENTENCE_WORD_LIMIT`` words**.

Only prose is read. Frontmatter, fenced code (backtick and tilde), HTML and
MDX comments, generated regions, MDX ``import`` lines and component tags
(one-line, or an opening tag whose attributes run on to a closing ``>``) are
blanked line for line, so every finding keeps its 1-based line number; a
blockquote's ``>`` prefix is dropped so the quoted text is read as the
paragraphs, list items and table rows it is. Inline code spans and link URLs
are replaced in place so an identifier in backticks never counts as a word or
a banned one; a code span prettier wrapped across lines, and a comment that
opens after text, are matched over the whole paragraph with their line
breaks kept. A long paragraph or sentence is reported at the line it starts
on, with its opening words.

**The ratchet.** ``scripts/docs_prose_ratchet.json`` records, per page and
per kind, the count the page carried when the check arrived. A page with no
entry has a ceiling of zero for every kind, so a new page must be clean. The
pin is two-sided: a count above its ceiling fails with the findings to fix,
and a count below it fails until the entry is lowered or removed, so the
record never says more than the page carries. ``--write-ratchet`` does that
upkeep from the current counts and only ever lowers or removes an entry;
raising one, or adding a page, is a hand edit a reviewer sees in the diff.
An entry for a page that no longer exists fails too.

The check also fails when it finds no site page at all: that would otherwise
turn the gate green with nothing checked, the moment the site root moves.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_prose.py
    python3 scripts/check_docs_prose.py --write-ratchet
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The published site: the only tree the prose rule binds.
SITE_ROOT = REPO / "docs" / "site" / "src" / "content" / "docs"
SITE_SUFFIXES = (".md", ".mdx")

# Per-page, per-kind ceilings; written by --write-ratchet, read by every run.
RATCHET = REPO / "scripts" / "docs_prose_ratchet.json"
RATCHET_INDENT = 2
WRITE_RATCHET_FLAG = "--write-ratchet"

# The rule the findings cite.
RULE = ".agents/rules/documentation.md"

# Structural ceilings. A paragraph a reader can hold in one go, a cell that
# still reads as a table entry, and a sentence with one idea in it.
PARAGRAPH_WORD_LIMIT = 120
CELL_WORD_LIMIT = 120
SENTENCE_WORD_LIMIT = 40

# Fewer pages than this and the run reports an error rather than a clean tree.
PAGE_COUNT_FLOOR = 1

# The finding kinds, in report order. The strings are the ratchet's keys.
KIND_BANNED_WORD = "banned-word"
KIND_ANTITHESIS = "antithesis"
KIND_PARAGRAPH = "long-paragraph"
KIND_CELL = "long-table-cell"
KIND_SENTENCE = "long-sentence"
KINDS = (KIND_BANNED_WORD, KIND_ANTITHESIS, KIND_PARAGRAPH, KIND_CELL, KIND_SENTENCE)

# The words the rule names, and the plain-language equivalents the issue
# added. Whole words, case-insensitive. `leverage` is handled apart because
# only the verb is filler.
BANNED_WORDS = (
    "simply",
    "comprehensive",
    "robust",
    "seamless",
    "seamlessly",
    "powerful",
    "delve",
    "holistic",
    "synergy",
    "utilize",
    "utilizes",
    "utilized",
    "utilizing",
    "facets",
)
BANNED_PHRASES = (
    "it's worth noting",
    "it is worth noting",
    "in today's",
)
# The verb forms of `leverage`: inflected, or the base form followed by a
# determiner or pronoun object. `leverage` as a noun ("gives you leverage")
# does not match.
LEVERAGE_VERB_RE = re.compile(
    r"\b(?:leverages|leveraged|leveraging|leverage\s+(?:the|a|an|this|that|these|those|its|their|your|our|it))\b",
    re.IGNORECASE,
)
# A phrase is matched over a run of consecutive lines, so one prettier wrapped
# (`It is worth` / `noting that`) still hits: the gap between its words is any
# whitespace, which inside a run is at most one line break. An apostrophe may
# be the straight or the typographic one.
PHRASE_GAP = r"\s+"
APOSTROPHE_CLASS = "['’]"
BANNED_PHRASE_PATTERNS = tuple(
    PHRASE_GAP.join(re.escape(word).replace("'", APOSTROPHE_CLASS) for word in phrase.split())
    for phrase in BANNED_PHRASES
)
BANNED_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in BANNED_WORDS) + r")\b"
    r"|\b(?:" + "|".join(BANNED_PHRASE_PATTERNS) + r")\b",
    re.IGNORECASE,
)
BANNED_WORD_RES = (BANNED_RE, LEVERAGE_VERB_RE)

# The two epigram forms. Each clause is bounded so the pattern cannot span a
# whole paragraph: the first allows a short noun phrase between `is not` and
# the separator, the second a short phrase between the qualifier and `but`.
ANTITHESIS_CLAUSE_WORDS = 8
ANTITHESIS_RES = (
    re.compile(
        r"\bis not (?:a|an|the|just a|just an|merely a|merely an|simply a|simply an)\b"
        r"(?:\s+[\w'’-]+){1,%d}\s*[,:;—–-]+\s*it(?:'s|’s| is)\b" % ANTITHESIS_CLAUSE_WORDS,
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnot (?:merely|just|simply)\b(?:\s+[\w'’-]+){1,%d},?\s+but\b" % ANTITHESIS_CLAUSE_WORDS,
        re.IGNORECASE,
    ),
)

# Line shapes that end a paragraph or are not prose at all.
FRONTMATTER_DELIMITER = "---"
FENCE_RE = re.compile(r"^\s*(?P<fence>```+|~~~+)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
# A remark directive container line: a Starlight aside's `:::note[Title]`
# label or its `:::` closer. It bounds the aside's paragraphs like a heading
# and is not a paragraph itself; the label's words stay for the word check.
DIRECTIVE_RE = re.compile(r"^\s*:{3,}")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# A blockquote's `>` prefix (one per nesting level). Dropped before the line
# is classified, so a `>`-only separator is a blank line, a quoted `| … |` row
# is a table row and a quoted paragraph starts at its own capital.
BLOCKQUOTE_RE = re.compile(r"^\s*(?:>\s?)+")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
IMPORT_LINE_RE = re.compile(r"^\s*import\s+.*\sfrom\s+['\"].*['\"];?\s*$")
# A line that is only an MDX/HTML tag (open, close or self-closing), such as
# `<Aside type="note">` or `</Steps>`; prose inside the component stays.
TAG_LINE_RE = re.compile(r"^\s*</?[A-Za-z][\w.-]*(?:\s[^<>]*)?/?>\s*$")
# A tag whose attributes continue on the next lines (`<LinkCard` / `title="…"`
# / `/>`); the span is blanked through the line that closes it. The attribute
# text is not prose for the check in either layout, so a card description
# reads the same whether prettier wrapped it or not.
TAG_OPEN_RE = re.compile(r"^\s*<[A-Za-z][\w.-]*(?:\s[^<>]*)?$")
TAG_CLOSE_RE = re.compile(r"/?>\s*$")
HTML_COMMENT_OPEN = "<!--"
HTML_COMMENT_CLOSE = "-->"
MDX_COMMENT_OPEN = "{/*"
MDX_COMMENT_CLOSE = "*/}"
GENERATED_BEGIN_RE = re.compile(r"BEGIN GENERATED")
GENERATED_END_RE = re.compile(r"END GENERATED")

# In-place replacements that keep the column roughly where it was. The code
# placeholder is capitalised so a sentence that opens with an identifier still
# starts a sentence for the splitter below. A span is matched over the
# paragraph's joined lines, since prettier wraps a long one at the column
# limit; the line breaks inside it are kept so line numbers survive.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
CODE_SPAN_PLACEHOLDER = "Code"
# A comment that opens after prose on its line, closed on the same line or a
# later line of the paragraph (an inline comment cannot cross a blank line);
# one left open runs to the paragraph's end. Matched after code spans, so a
# documented `<!--` in backticks is not an opener.
INLINE_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)|\{/\*.*?(?:\*/\}|\Z)", re.DOTALL)
LINK_URL_RE = re.compile(r"\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
LINK_URL_PLACEHOLDER = "]"
# Bare URLs and autolinks count as one word, not as several split on dots.
# The match stops before trailing punctuation, so the period that ends the
# sentence after a URL is still there for the splitter.
BARE_URL_RE = re.compile(r"<?https?://[^\s<>]*[^\s<>.,;:!?)\]]>?")
BARE_URL_PLACEHOLDER = "url"
# Emphasis markers would otherwise glue to a word and defeat `\b`.
EMPHASIS_RE = re.compile(r"[*_]{1,3}")
# Inline HTML and MDX tags inside a prose line (`<kbd>`, `<br />`).
INLINE_TAG_RE = re.compile(r"</?[A-Za-z][\w.-]*(?:\s[^<>]*)?/?>")

# A word, for the counts: a run of letters, digits and the joiners a word
# carries (`kube-agents`, `don't`, `v1.2`, `a/b`).
WORD_RE = re.compile(r"[\w][\w'’./:-]*")
# Sentence ends: a terminal mark followed by whitespace and an opening
# capital, quote or bracket. A period inside a version or a path is followed
# by a lowercase letter or a digit and does not split; `e.g.` and `i.e.` are
# excused by name, since a code placeholder often follows them.
SENTENCE_END_RE = re.compile(r"(?<=[.!?])(?<!\be\.g\.)(?<!\bi\.e\.)[\"')\]]*\s+(?=[\"'(\[A-Z0-9])")
# Table cell boundary: an unescaped pipe.
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

# How a long paragraph or sentence is named in the report: the count, then
# the opening words so the author can find it without counting.
DETAIL_OPENING_WORDS = 6
LENGTH_DETAIL = "{words} words (limit {limit}): {opening}..."
# Wrapped lines of one paragraph are joined with this, so a sentence's offset
# maps back to its source line.
PARAGRAPH_JOIN = "\n"
# A line the replacement pass emptied: a comment's closing line, or the middle
# of a code span wrapped over three lines. It stands for text the page still
# carries, so it continues the paragraph; the empty string is a source blank
# and ends it.
REPLACED_LINE = " "

# Report wording.
ABOVE_MESSAGE = (
    "{path}: {kind} {count} > ceiling {ceiling} -- fix the prose (rule: " + RULE + "):"
)
BELOW_MESSAGE = (
    "{path}: {kind} {count} < ceiling {ceiling} -- lower the entry in {ratchet}"
    " (python3 scripts/check_docs_prose.py " + WRITE_RATCHET_FLAG + ")"
)
MISSING_PAGE_MESSAGE = "{path}: in {ratchet} but no such page -- drop the entry"
NO_PAGES_ERROR = "no .md/.mdx page found under {root}; the site root moved and this check scans nothing"
BAD_RATCHET_ERROR = "{ratchet}: {detail}"
CLEAN_MESSAGE = "Prose on the {pages} published site pages is within {ratchet}."
WRITTEN_MESSAGE = "Wrote {ratchet}: {entries} page(s) with a non-zero ceiling."
FINDING_LINE = "    {path}:{line}: {detail}"


class Finding:
    """One prose finding: where it is, which kind, and the text or count behind it."""

    __slots__ = ("path", "line", "kind", "detail")

    def __init__(self, path: Path, line: int, kind: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.path!s}:{self.line}, {self.kind!r}, {self.detail!r})"


def keeping_newlines(placeholder: str):
    """A ``re.sub`` replacement: ``placeholder`` plus the match's line breaks."""
    return lambda match: placeholder + PARAGRAPH_JOIN * match.group(0).count(PARAGRAPH_JOIN)


def prose_lines(text: str) -> list[str]:
    """The page's lines with everything that is not prose blanked, line for line.

    Frontmatter, fences, comments, generated regions, import and tag lines
    become empty strings and a blockquote prefix is dropped; code spans and
    inline comments are replaced over each run of prose lines, then link
    URLs, bare URLs, emphasis and inline tags inside the line. The list has one
    entry per source line, so index + 1 is the source line number.
    """
    lines = text.splitlines()
    kept: list[str] = []
    in_frontmatter = False
    fence: str | None = None
    in_comment: str | None = None
    in_generated = False
    in_tag = False
    for index, raw in enumerate(lines):
        if in_tag:
            # Read before the blockquote strip: inside an opening tag a `>` on
            # its own line is the tag's close, never a quote marker.
            if TAG_CLOSE_RE.search(raw):
                in_tag = False
            kept.append("")
            continue
        raw = BLOCKQUOTE_RE.sub("", raw, count=1)
        stripped = raw.strip()
        if index == 0 and stripped == FRONTMATTER_DELIMITER:
            in_frontmatter = True
            kept.append("")
            continue
        if in_frontmatter:
            if stripped == FRONTMATTER_DELIMITER:
                in_frontmatter = False
            kept.append("")
            continue
        if fence is not None:
            match = FENCE_RE.match(raw)
            if match and match.group("fence")[0] == fence[0] and len(match.group("fence")) >= len(fence):
                fence = None
            kept.append("")
            continue
        match = FENCE_RE.match(raw)
        if match:
            fence = match.group("fence")
            kept.append("")
            continue
        if in_comment is not None:
            if in_comment in raw:
                in_comment = None
            kept.append("")
            continue
        if in_generated:
            if GENERATED_END_RE.search(raw):
                in_generated = False
            kept.append("")
            continue
        begin = GENERATED_BEGIN_RE.search(raw)
        if begin:
            # A region opened and closed on one line blanks that line only.
            in_generated = not GENERATED_END_RE.search(raw, begin.end())
            kept.append("")
            continue
        for opener, closer in ((HTML_COMMENT_OPEN, HTML_COMMENT_CLOSE), (MDX_COMMENT_OPEN, MDX_COMMENT_CLOSE)):
            if stripped.startswith(opener):
                if closer not in stripped[len(opener) :]:
                    in_comment = closer
                raw = ""
                break
        if not raw:
            kept.append("")
            continue
        if TAG_OPEN_RE.match(raw):
            in_tag = True
            kept.append("")
            continue
        if IMPORT_LINE_RE.match(raw) or TAG_LINE_RE.match(raw):
            kept.append("")
            continue
        kept.append(raw if stripped else "")
    return [line for run in prose_runs(kept) for line in run]


def prose_runs(kept: list[str]) -> list[list[str]]:
    """``kept`` split at its blank lines, each run's prose replaced in place.

    A run is one paragraph's worth of consecutive lines. Code spans and inline
    comments are replaced over the run joined with ``PARAGRAPH_JOIN``, with
    their line breaks kept, so a span wrapped across two lines is one ``Code``
    and the prose after it stays on its own line; the rest is per line. A line
    the replacement leaves empty comes back as ``REPLACED_LINE``, so the
    paragraph it sits in is not read as two.
    """
    runs: list[list[str]] = []
    run: list[str] = []
    for line in kept + [""]:
        if line:
            run.append(line)
            continue
        if run:
            joined = PARAGRAPH_JOIN.join(run)
            joined = CODE_SPAN_RE.sub(keeping_newlines(CODE_SPAN_PLACEHOLDER), joined)
            joined = INLINE_COMMENT_RE.sub(keeping_newlines(""), joined)
            runs.append(
                [prose_line(part) if part.strip() else REPLACED_LINE for part in joined.split(PARAGRAPH_JOIN)]
            )
            run = []
        runs.append([""])
    runs.pop()  # the sentinel's own blank
    return runs


def prose_line(raw: str) -> str:
    """One prose line with link URLs, bare URLs, inline tags and emphasis replaced."""
    # A `* item` bullet keeps its marker: the emphasis strip below would
    # otherwise turn it into plain text and merge it into the paragraph.
    marker = LIST_ITEM_RE.match(raw)
    prefix, line = (marker.group(0), raw[marker.end() :]) if marker else ("", raw)
    line = LINK_URL_RE.sub(LINK_URL_PLACEHOLDER, line)
    line = BARE_URL_RE.sub(BARE_URL_PLACEHOLDER, line)
    line = INLINE_TAG_RE.sub(" ", line)
    line = EMPHASIS_RE.sub("", line)
    return prefix + line


def word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def is_blank(line: str) -> bool:
    """A source blank line, as opposed to one the replacement pass emptied."""
    return not line.strip() and line != REPLACED_LINE


def paragraphs(lines: list[str]) -> list[tuple[int, str]]:
    """(1-based start line, text) for every prose block.

    A blank line, a heading, a directive line, a list item or a table row ends
    the block before it; a heading, directive line or table row is not a
    paragraph itself, and each list item is a paragraph of its own.
    """
    blocks: list[tuple[int, str]] = []
    start = 0
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            blocks.append((start, PARAGRAPH_JOIN.join(buffer)))
            buffer.clear()

    for index, line in enumerate(lines, start=1):
        if is_blank(line) or HEADING_RE.match(line) or DIRECTIVE_RE.match(line) or TABLE_ROW_RE.match(line):
            flush()
            continue
        if LIST_ITEM_RE.match(line):
            flush()
            start = index
            buffer.append(LIST_ITEM_RE.sub("", line, count=1))
            continue
        if not buffer:
            start = index
        buffer.append(line.strip())
    flush()
    return blocks


def table_cells(lines: list[str]) -> list[tuple[int, str]]:
    """(1-based line, text) for every cell of every table row that is not a rule."""
    cells: list[tuple[int, str]] = []
    for index, line in enumerate(lines, start=1):
        if not TABLE_ROW_RE.match(line) or TABLE_RULE_RE.match(line):
            continue
        inner = line.strip()[1:-1]
        for cell in CELL_SPLIT_RE.split(inner):
            if cell.strip():
                cells.append((index, cell.strip()))
    return cells


def sentences(text: str) -> list[tuple[int, str]]:
    """(offset into ``text``, sentence) for every non-blank sentence."""
    out: list[tuple[int, str]] = []
    offset = 0
    for match in SENTENCE_END_RE.finditer(text):
        out.append((offset, text[offset : match.start()]))
        offset = match.end()
    out.append((offset, text[offset:]))
    return [(o, s) for o, s in out if s.strip()]


def length_detail(text: str, words: int, limit: int) -> str:
    opening = " ".join(WORD_RE.findall(text)[:DETAIL_OPENING_WORDS])
    return LENGTH_DETAIL.format(words=words, limit=limit, opening=opening)


def line_at(start: int, text: str, offset: int) -> int:
    """The source line of ``offset`` in a block that starts on line ``start``."""
    return start + text[:offset].count(PARAGRAPH_JOIN)


def text_findings(path: Path, start: int, text: str, kind: str, limit: int) -> list[Finding]:
    """The antithesis, length and sentence findings of one paragraph or cell."""
    findings: list[Finding] = []
    for pattern in ANTITHESIS_RES:
        for match in pattern.finditer(text):
            findings.append(Finding(path, line_at(start, text, match.start()), KIND_ANTITHESIS, match.group(0)))
    words = word_count(text)
    if words > limit:
        findings.append(Finding(path, start, kind, length_detail(text, words, limit)))
    for offset, sentence in sentences(text):
        words = word_count(sentence)
        if words > SENTENCE_WORD_LIMIT:
            detail = length_detail(sentence, words, SENTENCE_WORD_LIMIT)
            findings.append(Finding(path, line_at(start, text, offset), KIND_SENTENCE, detail))
    return findings


def line_runs(lines: list[str]) -> list[tuple[int, str]]:
    """(1-based start line, text) for every run of consecutive non-blank lines, joined."""
    runs: list[tuple[int, str]] = []
    start = 0
    buffer: list[str] = []
    for index, line in enumerate(lines + [""], start=1):
        if not is_blank(line):
            if not buffer:
                start = index
            buffer.append(line)
        elif buffer:
            runs.append((start, PARAGRAPH_JOIN.join(buffer)))
            buffer = []
    return runs


def banned_findings(path: Path, lines: list[str]) -> list[Finding]:
    """Banned words, phrases and verb forms, matched over each run of lines.

    A phrase wrapped across two lines is found and reported at the line it
    starts on, with the line break in its text read as a space.
    """
    findings: list[Finding] = []
    for start, text in line_runs(lines):
        for pattern in BANNED_WORD_RES:
            for match in pattern.finditer(text):
                detail = " ".join(match.group(0).split())
                findings.append(Finding(path, line_at(start, text, match.start()), KIND_BANNED_WORD, detail))
    return findings


def page_findings(path: Path, text: str) -> list[Finding]:
    """Every finding on one page, in line order."""
    lines = prose_lines(text)
    findings = banned_findings(path, lines)
    for start, block in paragraphs(lines):
        findings.extend(text_findings(path, start, block, KIND_PARAGRAPH, PARAGRAPH_WORD_LIMIT))
    for number, cell in table_cells(lines):
        findings.extend(text_findings(path, number, cell, KIND_CELL, CELL_WORD_LIMIT))
    findings.sort(key=lambda f: (f.line, KINDS.index(f.kind)))
    return findings


def site_pages(root: Path = SITE_ROOT) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in SITE_SUFFIXES and p.is_file())


def page_key(page: Path, root: Path) -> str:
    """How the ratchet names a page: its path relative to the site root, POSIX."""
    return page.relative_to(root).as_posix()


def scan(root: Path = SITE_ROOT) -> dict[str, list[Finding]]:
    """Findings per page key, every page present even when clean."""
    return {page_key(page, root): page_findings(page, page.read_text(encoding="utf-8")) for page in site_pages(root)}


def counts_of(findings: list[Finding]) -> dict[str, int]:
    """Non-zero counts per kind, in KINDS order."""
    counts: dict[str, int] = {}
    for kind in KINDS:
        n = sum(1 for f in findings if f.kind == kind)
        if n:
            counts[kind] = n
    return counts


def current_ratchet(root: Path = SITE_ROOT) -> dict[str, dict[str, int]]:
    """What the ratchet would say if written from the tree as it stands."""
    return {key: counts for key, findings in sorted(scan(root).items()) if (counts := counts_of(findings))}


def load_ratchet(path: Path = RATCHET) -> dict[str, dict[str, int]]:
    """The committed ceilings; an absent file is an empty ratchet."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("top level is not an object")
    for page, kinds in data.items():
        if not isinstance(kinds, dict):
            raise ValueError(f"{page}: value is not an object")
        for kind, count in kinds.items():
            if kind not in KINDS:
                raise ValueError(f"{page}: unknown kind {kind!r}")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f"{page}: {kind} is not a non-negative integer")
    return data


def dump_ratchet(ratchet: dict[str, dict[str, int]]) -> str:
    ordered = {page: {k: ratchet[page][k] for k in KINDS if k in ratchet[page]} for page in sorted(ratchet)}
    return json.dumps(ordered, indent=RATCHET_INDENT) + "\n"


def lowered_ratchet(recorded: dict[str, dict[str, int]], actual: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """``recorded`` with every entry lowered to ``actual`` and none raised or added."""
    result: dict[str, dict[str, int]] = {}
    for page, kinds in recorded.items():
        present = actual.get(page)
        if present is None:
            continue
        kept = {kind: min(ceiling, present.get(kind, 0)) for kind, ceiling in kinds.items()}
        kept = {kind: ceiling for kind, ceiling in kept.items() if ceiling}
        if kept:
            result[page] = kept
    return result


def display_path(path: Path) -> Path:
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def compare(
    findings_by_page: dict[str, list[Finding]],
    recorded: dict[str, dict[str, int]],
    root: Path,
    ratchet_path: Path,
) -> list[str]:
    """The report lines for every page out of step with its ceilings, in page order."""
    report: list[str] = []
    for page in sorted(set(findings_by_page) | set(recorded)):
        findings = findings_by_page.get(page)
        ceilings = recorded.get(page, {})
        shown = (root / page) if findings is not None else Path(page)
        if findings is None:
            report.append(MISSING_PAGE_MESSAGE.format(path=display_path(shown), ratchet=display_path(ratchet_path)))
            continue
        actual = counts_of(findings)
        for kind in KINDS:
            count = actual.get(kind, 0)
            ceiling = ceilings.get(kind, 0)
            if count > ceiling:
                report.append(ABOVE_MESSAGE.format(path=display_path(shown), kind=kind, count=count, ceiling=ceiling))
                for finding in findings:
                    if finding.kind == kind:
                        report.append(
                            FINDING_LINE.format(path=display_path(finding.path), line=finding.line, detail=finding.detail)
                        )
            elif count < ceiling:
                report.append(
                    BELOW_MESSAGE.format(
                        path=display_path(shown),
                        kind=kind,
                        count=count,
                        ceiling=ceiling,
                        ratchet=display_path(ratchet_path),
                    )
                )
    return report


def preconditions(root: Path = SITE_ROOT) -> list[str]:
    """Reasons a run cannot be trusted to have checked anything."""
    errors = []
    if len(site_pages(root)) < PAGE_COUNT_FLOOR:
        errors.append(NO_PAGES_ERROR.format(root=root))
    return errors


def readable_ratchet(ratchet_path: Path) -> dict[str, dict[str, int]] | None:
    """The committed ratchet, or ``None`` after reporting why it cannot be read."""
    try:
        return load_ratchet(ratchet_path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {BAD_RATCHET_ERROR.format(ratchet=display_path(ratchet_path), detail=exc)}")
        return None


def write_ratchet(root: Path = SITE_ROOT, ratchet_path: Path = RATCHET) -> int:
    """Lower or drop the recorded ceilings to today's counts; never raise or add one."""
    recorded = readable_ratchet(ratchet_path)
    if recorded is None:
        return 1
    lowered = lowered_ratchet(recorded, current_ratchet(root))
    ratchet_path.write_text(dump_ratchet(lowered), encoding="utf-8")
    print(WRITTEN_MESSAGE.format(ratchet=display_path(ratchet_path), entries=len(lowered)))
    return 0


def check(root: Path = SITE_ROOT, ratchet_path: Path = RATCHET) -> int:
    recorded = readable_ratchet(ratchet_path)
    if recorded is None:
        return 1
    findings_by_page = scan(root)
    report = compare(findings_by_page, recorded, root, ratchet_path)
    if report:
        for line in report:
            print(line)
        return 1
    print(CLEAN_MESSAGE.format(pages=len(findings_by_page), ratchet=display_path(ratchet_path)))
    return 0


def main(argv: list[str] | None = None, root: Path = SITE_ROOT, ratchet_path: Path = RATCHET) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        WRITE_RATCHET_FLAG,
        action="store_true",
        help="lower or drop ratchet entries to the current counts (never raise or add one)",
    )
    args = parser.parse_args(argv)
    errors = preconditions(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    if args.write_ratchet:
        return write_ratchet(root, ratchet_path)
    return check(root, ratchet_path)


if __name__ == "__main__":
    sys.exit(main())
