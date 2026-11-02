#!/usr/bin/env python3
"""Keep docs-site Markdown tables wrapping prose cells while preserving inline-code nowrap.

Why this test exists: ``docs/site/src/styles/theme.css`` wraps Markdown tables in
``display: block; width: max-content; min-width: 100%; max-width: 100%; overflow-x: auto``.
When bare ``td:first-child`` / ``th:first-child`` carried ``white-space: nowrap``, any
table with a prose first column (for example ``Completion guardrails`` on
``concepts/platform-agent.md`` or the prerequisite tables on ``install/prerequisites.md``)
stretched column 1 past the 55rem content column, pushed every remaining column off-screen,
and collapsed wrapped prose columns to their single-word minimum width — rendering tall
empty row boxes in the visible viewport.

Inline ``<code>`` inside table cells still needs ``white-space: nowrap`` so tool and skill
identifiers never split mid-token at underscores or hyphens.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THEME_CSS = REPO / "docs" / "site" / "src" / "styles" / "theme.css"
SITE_SRC_DIR = REPO / "docs" / "site" / "src"

COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
WRAP_DECL_RE = re.compile(
    r"(?:^|;)\s*(white-space|text-wrap|text-wrap-mode)\s*:\s*([^;!]+)",
    re.IGNORECASE,
)
NON_WRAPPING_WHITE_SPACE_TOKENS = frozenset({"nowrap", "pre"})
FUNCTIONAL_MATCH_RE = re.compile(r":(?:is|where)\(", re.IGNORECASE)
NOT_PSEUDO_RE = re.compile(r":not\(", re.IGNORECASE)
LEADING_TYPE_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)(?=$|[:.\[#])")
ASTRO_GLOBAL_STYLE_RE = re.compile(
    r"<style\b[^>]*\bis:global\b[^>]*>(.*?)</style>",
    re.IGNORECASE | re.DOTALL,
)
MDX_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
MDX_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
CLASS_OR_ID_TOKEN_RE = re.compile(r"([.#][a-zA-Z0-9_-]+)")


def _find_matching_delim(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Return index of matching ``close_ch`` for ``open_ch`` at ``open_idx``, respecting quotes."""
    depth = 0
    quote: str | None = None
    idx = open_idx
    while idx < len(text):
        ch = text[idx]
        if quote is not None:
            if ch == "\\":
                idx += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return idx
        idx += 1
    return -1


def split_top_level_commas(text: str) -> list[str]:
    """Split a CSS selector list on commas outside ``(...)``, ``[...]``, and quotes."""
    parts: list[str] = []
    start = 0
    paren_depth = 0
    bracket_depth = 0
    quote: str | None = None
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if quote is not None:
            if ch == "\\":
                idx += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif ch == "," and paren_depth == 0 and bracket_depth == 0:
                piece = text[start:idx].strip()
                if piece:
                    parts.append(piece)
                start = idx + 1
        idx += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def split_compound_selectors(selector: str) -> list[str]:
    """Split a single selector branch into ancestor-to-subject compound selectors."""
    compounds: list[str] = []
    start = 0
    paren_depth = 0
    bracket_depth = 0
    quote: str | None = None
    idx = 0
    while idx < len(selector):
        ch = selector[idx]
        if quote is not None:
            if ch == "\\":
                idx += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif paren_depth == 0 and bracket_depth == 0 and (ch.isspace() or ch in ">+~"):
                piece = selector[start:idx].strip()
                if piece:
                    compounds.append(piece)
                idx += 1
                while idx < len(selector) and (selector[idx].isspace() or selector[idx] in ">+~"):
                    idx += 1
                start = idx
                continue
        idx += 1
    tail = selector[start:].strip()
    if tail:
        compounds.append(tail)
    return compounds


def _strip_parenthesized_and_bracketed(compound: str) -> str:
    """Remove ``(...)`` and ``[...]`` bodies so top-level classes/IDs/pseudo-elements can be inspected."""
    out: list[str] = []
    paren_depth = 0
    bracket_depth = 0
    quote: str | None = None
    idx = 0
    while idx < len(compound):
        ch = compound[idx]
        if quote is not None:
            if ch == "\\":
                idx += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif paren_depth == 0 and bracket_depth == 0:
                out.append(ch)
        idx += 1
    return "".join(out)


def _has_top_level_pseudo_element(compound: str) -> bool:
    """Return True if ``compound`` carries a ``::`` pseudo-element outside ``(...)`` / ``[...]``."""
    return "::" in _strip_parenthesized_and_bracketed(compound)


def _has_non_markdown_class_or_id(compounds: list[str]) -> bool:
    """Return True if any compound requires a class or ID absent from Starlight Markdown table DOM."""
    for compound in compounds:
        bare = _strip_parenthesized_and_bracketed(compound)
        for token in CLASS_OR_ID_TOKEN_RE.findall(bare):
            if token != ".sl-markdown-content":
                return True
    return False


def _compound_type(compound: str) -> str | None:
    """Return lower-cased element type of ``compound`` (e.g. ``'td'`` for ``'td:first-child'``)."""
    match = LEADING_TYPE_RE.match(compound)
    return match.group(1).lower() if match else None


def _is_print_only_media(at_header: str) -> bool:
    """Return True when an ``@media`` header applies exclusively to ``print`` media."""
    match = re.match(r"^@media\b(.*)$", at_header, re.IGNORECASE | re.DOTALL)
    if not match:
        return False
    queries = split_top_level_commas(match.group(1))
    return bool(queries) and all(
        re.match(r"^(?:only\s+)?print\b", q.strip(), re.IGNORECASE) for q in queries
    )


def strip_not_pseudos(selector: str) -> str:
    """Replace ``:not(...)`` bodies so negated tokens never match positively."""
    result = selector
    while True:
        match = NOT_PSEUDO_RE.search(result)
        if not match:
            return result
        open_paren = match.end() - 1
        close_paren = _find_matching_delim(result, open_paren, "(", ")")
        if close_paren == -1:
            return result
        result = result[: match.start()] + ":nth-not" + result[close_paren + 1 :]


def expand_selector_branches(selector: str) -> list[str]:
    """Expand ``:is(...)`` and ``:where(...)`` into flat selector alternatives."""
    sanitized = strip_not_pseudos(selector)
    pending = [sanitized]
    expanded: list[str] = []
    while pending:
        current = pending.pop(0)
        match = FUNCTIONAL_MATCH_RE.search(current)
        if not match:
            expanded.append(re.sub(r"\s+", " ", current).strip())
            continue
        open_paren = match.end() - 1
        close_paren = _find_matching_delim(current, open_paren, "(", ")")
        if close_paren == -1:
            expanded.append(re.sub(r"\s+", " ", current).strip())
            continue
        inner_branches = split_top_level_commas(current[open_paren + 1 : close_paren])
        prefix = current[: match.start()]
        suffix = current[close_paren + 1 :]
        if not inner_branches:
            pending.append(prefix + suffix)
        else:
            for branch in inner_branches:
                pending.append(f"{prefix}{branch}{suffix}")
    return expanded


def iter_css_rules(css_text: str) -> list[tuple[list[str], str]]:
    """Return ``(selectors, declarations)`` in screen cascade order, supporting ``@``-rules and nesting."""
    cleaned = COMMENT_RE.sub("", css_text)
    rules: list[tuple[list[str], str]] = []

    def walk_block(segment: str, parent_selectors: list[str] | None) -> None:
        direct_decls: list[str] = []
        child_rules_start_index = len(rules)
        stmt_start = 0
        idx = 0
        quote: str | None = None
        paren_depth = 0
        bracket_depth = 0

        while idx < len(segment):
            ch = segment[idx]
            if quote is not None:
                if ch == "\\":
                    idx += 2
                    continue
                if ch == quote:
                    quote = None
                idx += 1
                continue

            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif ch == ";" and paren_depth == 0 and bracket_depth == 0:
                stmt = segment[stmt_start:idx].strip()
                if parent_selectors is not None and stmt and not stmt.startswith("@"):
                    direct_decls.append(stmt)
                stmt_start = idx + 1
            elif ch == "{" and paren_depth == 0 and bracket_depth == 0:
                header = segment[stmt_start:idx].strip()
                close_brace = _find_matching_delim(segment, idx, "{", "}")
                body = segment[idx + 1 :] if close_brace == -1 else segment[idx + 1 : close_brace]
                if header.startswith("@"):
                    if not _is_print_only_media(header):
                        walk_block(body, parent_selectors)
                else:
                    raw_selectors = split_top_level_commas(header)
                    if parent_selectors is None:
                        resolved = raw_selectors
                    else:
                        resolved = [
                            child.replace("&", parent) if "&" in child else f"{parent} {child}"
                            for parent in parent_selectors
                            for child in raw_selectors
                        ]
                    if resolved:
                        walk_block(body, resolved)
                if close_brace == -1:
                    stmt_start = len(segment)
                    break
                idx = close_brace + 1
                stmt_start = idx
                continue
            idx += 1

        trailing = segment[stmt_start:].strip()
        if parent_selectors is not None and trailing and not trailing.startswith("@"):
            direct_decls.append(trailing)
        if parent_selectors is not None and direct_decls:
            rules.insert(child_rules_start_index, (parent_selectors, "; ".join(direct_decls)))

    walk_block(cleaned, parent_selectors=None)
    return rules


def _resolve_rule_wrap_state(body: str) -> tuple[bool, str] | None:
    """Return ``(is_non_wrapping, rendered_decl)`` for the last wrap declaration in ``body``."""
    state: tuple[bool, str] | None = None
    for match in WRAP_DECL_RE.finditer(body):
        prop = match.group(1).strip().lower()
        val = match.group(2).strip().lower()
        tokens = set(val.split())
        if prop == "white-space":
            non_wrap = bool(tokens & NON_WRAPPING_WHITE_SPACE_TOKENS)
        else:
            non_wrap = "nowrap" in tokens
        state = (non_wrap, f"{prop}: {val}")
    return state


def _is_unrestricted_markdown_ancestor(compound: str) -> bool:
    """Return True if ``compound`` matches general Starlight Markdown table/code ancestry."""
    low = compound.lower()
    return (
        low in ("*", ":nth-not", "*:nth-not", "table", "tbody", "thead", "tr", "td", "th")
        or compound == ".sl-markdown-content"
        or low == "table:nth-not"
        or low == ".sl-markdown-content:nth-not"
    )


def find_table_css_violations(css_text: str) -> tuple[bool, list[str]]:
    """Return ``(has_table_code_nowrap, non_wrapping_cell_selectors)``."""
    has_table_code_nowrap = False
    bad_cell_branches: dict[str, str] = {}

    for selectors, body in iter_css_rules(css_text):
        wrap_state = _resolve_rule_wrap_state(body)
        if wrap_state is None:
            continue
        is_non_wrapping, rendered_decl = wrap_state

        for selector in selectors:
            branches = expand_selector_branches(selector)
            for branch in branches:
                compounds = split_compound_selectors(branch)
                if not compounds:
                    continue
                subject = compounds[-1]
                if _has_top_level_pseudo_element(subject):
                    continue
                if _has_non_markdown_class_or_id(compounds):
                    continue

                subject_type = _compound_type(subject)
                ancestor_types = {_compound_type(c) for c in compounds[:-1]}
                subject_is_bare_code = subject.lower() in ("code", "code:nth-not")
                ancestors_unrestricted = all(
                    _is_unrestricted_markdown_ancestor(c) for c in compounds[:-1]
                )

                is_unrestricted_table_code = (
                    subject_is_bare_code
                    and "table" in ancestor_types
                    and "pre" not in ancestor_types
                    and ancestors_unrestricted
                )
                is_broad_markdown_code = (
                    subject_is_bare_code
                    and "pre" not in ancestor_types
                    and ancestors_unrestricted
                )
                if is_unrestricted_table_code and is_non_wrapping and "nowrap" in rendered_decl:
                    has_table_code_nowrap = True
                elif is_broad_markdown_code and not is_non_wrapping:
                    has_table_code_nowrap = False

                if subject_type in ("td", "th"):
                    if is_non_wrapping:
                        bad_cell_branches[branch] = f"{selector} {{ {rendered_decl} }}"
                    else:
                        bad_cell_branches.pop(branch, None)

    seen_messages: list[str] = []
    for msg in bad_cell_branches.values():
        if msg not in seen_messages:
            seen_messages.append(msg)
    return has_table_code_nowrap, seen_messages


def iter_site_global_stylesheets(site_src_dir: Path) -> list[tuple[str, str]]:
    """Yield ``(label, css_text)`` for ``*.css``, ``<style is:global>`` in ``*.astro``, and ``<style>`` in ``*.mdx``."""
    sources: list[tuple[str, str]] = []
    for css_path in sorted(site_src_dir.rglob("*.css")):
        sources.append((str(css_path.relative_to(REPO)), css_path.read_text(encoding="utf-8")))
    for astro_path in sorted(site_src_dir.rglob("*.astro")):
        text = astro_path.read_text(encoding="utf-8")
        for idx, match in enumerate(ASTRO_GLOBAL_STYLE_RE.finditer(text)):
            sources.append((f"{astro_path.relative_to(REPO)}#global-style[{idx}]", match.group(1)))
    for mdx_path in sorted(site_src_dir.rglob("*.mdx")):
        text = MDX_FENCED_CODE_RE.sub("", mdx_path.read_text(encoding="utf-8"))
        for idx, match in enumerate(MDX_STYLE_RE.finditer(text)):
            sources.append((f"{mdx_path.relative_to(REPO)}#style[{idx}]", match.group(1)))
    return sources


class DocsSiteTableCssTest(unittest.TestCase):
    def test_theme_css_keeps_inline_code_nowrap_without_bare_cell_nowrap(self) -> None:
        self.assertTrue(THEME_CSS.is_file(), f"Missing docs stylesheet: {THEME_CSS}")
        has_code_nowrap, bad_cells = find_table_css_violations(
            THEME_CSS.read_text(encoding="utf-8")
        )
        self.assertTrue(
            has_code_nowrap,
            "theme.css must keep `white-space: nowrap` on inline `code` inside `.sl-markdown-content table` "
            "so tool and skill identifiers do not break mid-token.",
        )
        self.assertEqual(
            bad_cells,
            [],
            "Bare table cells (`td`/`th`, including `:first-child`) must not force non-wrapping `white-space`; "
            f"prose columns overflow the content width and render blank boxes: {bad_cells}",
        )

    def test_no_site_global_style_source_forces_nowrap_on_markdown_table_cells(self) -> None:
        sources = iter_site_global_stylesheets(SITE_SRC_DIR)
        self.assertGreaterEqual(len(sources), 1)
        offenders: dict[str, list[str]] = {}
        for label, css_text in sources:
            _, bad_cells = find_table_css_violations(css_text)
            if bad_cells:
                offenders[label] = bad_cells
        self.assertEqual(offenders, {})

    def test_detector_catches_pre_fix_first_child_nowrap_inside_media_or_top_level(self) -> None:
        pre_fix_css = """
        @layer starlight, theme;
        @import "./tokens.css";
        /* comment with td:first-child { white-space: nowrap; } ignored */
        .sl-markdown-content table:where(:not(.not-content *)) code {
          white-space: nowrap;
        }
        .sl-markdown-content table:where(:not(.not-content *)) td:first-child,
        .sl-markdown-content table:where(:not(.not-content *)) th:first-child {
          white-space: nowrap;
        }
        @media (min-width: 50rem) {
          .sl-markdown-content table td {
            white-space: pre !important;
          }
        }
        """
        has_code_nowrap, bad_cells = find_table_css_violations(pre_fix_css)
        self.assertTrue(has_code_nowrap)
        self.assertEqual(
            bad_cells,
            [
                ".sl-markdown-content table:where(:not(.not-content *)) td:first-child { white-space: nowrap }",
                ".sl-markdown-content table:where(:not(.not-content *)) th:first-child { white-space: nowrap }",
                ".sl-markdown-content table td { white-space: pre }",
            ],
        )

    def test_detector_catches_is_where_pseudos_nested_css_and_text_wrap(self) -> None:
        nested_and_pseudo_css = """
        .sl-markdown-content :is(table, .data-table) code {
          white-space: nowrap;
        }
        /* Allowed: subject is `code`, even when `td:first-child` is an ancestor */
        .sl-markdown-content table td:first-child code {
          white-space: nowrap;
        }
        /* Allowed: pseudo-element ::after, not cell prose */
        .sl-markdown-content table td::after {
          white-space: nowrap;
        }
        /* Allowed: opted-out `.not-content` and custom component class tables */
        .not-content table td:first-child,
        .ka-report th {
          white-space: nowrap;
        }
        /* Allowed: print-only stylesheet wraps long code tokens for paper */
        @media print {
          .sl-markdown-content table code {
            white-space: pre-wrap;
          }
        }
        .sl-markdown-content table:where(:not(.not-content *)) :is(td, th):first-child {
          white-space: collapse nowrap;
        }
        .sl-markdown-content table {
          & td:nth-child(1) {
            text-wrap: nowrap;
          }
        }
        """
        has_code_nowrap, bad_cells = find_table_css_violations(nested_and_pseudo_css)
        self.assertTrue(has_code_nowrap)
        self.assertEqual(
            bad_cells,
            [
                ".sl-markdown-content table:where(:not(.not-content *)) :is(td, th):first-child { white-space: collapse nowrap }",
                ".sl-markdown-content table td:nth-child(1) { text-wrap: nowrap }",
            ],
        )

    def test_detector_flags_missing_or_later_overridden_inline_code_nowrap(self) -> None:
        css_without_code_rule = """
        .sl-markdown-content table th,
        .sl-markdown-content table td {
          padding: 0.6rem 0.75rem;
        }
        .sl-markdown-content table pre code {
          white-space: pre;
        }
        """
        has_code_nowrap, bad_cells = find_table_css_violations(css_without_code_rule)
        self.assertFalse(has_code_nowrap)
        self.assertEqual(bad_cells, [])

        overridden_code_rule = """
        .sl-markdown-content table code {
          white-space: nowrap;
        }
        .sl-markdown-content :not(pre) > code {
          white-space: normal;
        }
        """
        has_code_nowrap_after_override, _ = find_table_css_violations(overridden_code_rule)
        self.assertFalse(has_code_nowrap_after_override)

        class_scoped_code_override = """
        .sl-markdown-content table code {
          white-space: nowrap;
        }
        .sl-markdown-content table td.wrap-code code,
        .not-content table code {
          white-space: normal;
        }
        """
        has_code_nowrap_with_scoped_override, _ = find_table_css_violations(
            class_scoped_code_override
        )
        self.assertTrue(has_code_nowrap_with_scoped_override)


if __name__ == "__main__":
    unittest.main()
