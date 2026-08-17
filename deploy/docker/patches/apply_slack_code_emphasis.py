#!/usr/bin/env python3
"""Let bold and italic survive an inline code span in Slack Block Kit output.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/slack/block_kit.py``.

The bug
-------
``_inline_elements`` turns a run of markdown into ``rich_text`` children — the
elements Slack uses for list items, block quotes and table cells. It tokenized
inline code first and *by splitting the run on it*::

    for m in _INLINE_CODE_RE.finditer(s):
        _walk_links(s[pos:m.start()], style)   # the gap BEFORE the code span
        emit_text(m.group(1), {"code": True})
        pos = m.end()
    _walk_links(s[pos:], style)                # the gap AFTER it

Each gap reaches the emphasis scan as its own string, so a ``**`` that opens
before a code span and closes after it can never pair up: ``_BOLD_RE`` sees
``"**"`` in one call and ``"** (us-east4) …"`` in the next, matches neither, and
``_walk_emphasis`` falls through to ``emit_text(s, …)``, which emits the
delimiters verbatim. The docstring's promise that "unmatched markup is emitted
verbatim … so this never loses characters" is what turns a parse miss into
literal asterisks on screen.

Two standard-Markdown spellings render wrongly as a result:

``**`code`**`` (emphasis wrapping a code span)
    Renders as a literal ``**``, a code chip, then a literal ``**``.

``**bold with `code` inside**`` (a code span inside an emphasis run)
    Renders as literal ``**bold with``, a code chip, then ``inside**``. Nothing
    is bold, because the only place code is detected is the top-level ``walk``
    and ``_walk_emphasis`` never recurses back into it.

Measured on card ``t_549d081c`` (a fleet health check that fanned out to four
Cluster Agents on 2026-08-12). Its ``result`` opened each list item with
``**`adam-new-cluster`** (us-east4) -> …`` and Slack showed the asterisks:
``{"type": "text", "text": "**"}``, ``{"text": "adam-new-cluster", "style":
{"code": true}}``, ``{"type": "text", "text": "** (us-east4) -> …"}``.

Only the ``rich_text`` path is affected. Section blocks go through the adapter's
``format_message`` (``render_blocks(content, mrkdwn_fn=self.format_message)``),
which protects inline code behind placeholders *before* converting ``**x**`` to
``*x*``, so the same markdown in a paragraph has always rendered correctly. That
asymmetry is why this reads as a formatting bug rather than a broken agent: the
identical source renders one way in a paragraph and another in a list item.

The agents cannot route around it. ``agents/platform/SOUL.md`` §0 tells every
worker to "always write standard Markdown (``**bold**``, ``[text](url)``),
never a platform's own dialect: the adapter converts for you". ``**`code`**`` is
standard Markdown, and backticked identifiers are required by the report-format
stanza in ``kanban_report_format.py``, so the two rules together steer workers
straight into this case.

The fix
-------
Mask instead of split. Each inline code span is replaced by a ``\\x00N\\x00``
sentinel and held in a list; links and emphasis then scan one continuous string,
and ``emit_text`` restores the spans as it emits, giving each the style of the
run it sits in. Code stays opaque either way — no markdown is interpreted inside
a span, which was the point of tokenizing it first — but the emphasis scan is no
longer handed a pre-chopped string it cannot match across.

A restored span carries a combined style, ``{"bold": true, "code": true}``,
which is what Slack's own composer emits for bolded code and what the upstream
``code_style = dict(style)`` line was already written to allow — that ``dict``
copy could only ever be empty before this change, because ``walk`` ran only at
the top level with ``{}``.

``_unmask`` handles the one place a code element cannot go: a Slack ``link``
element carries flat ``text``/``url`` strings with no children, so a sentinel
landing there is restored to its original backticked source instead. That also
repairs ``[`code`](url)``, which upstream shredded into three elements because
the code split ran before the link scan.

Handing the emphasis scan one continuous string also un-hides a second defect,
which the split had been suppressing by accident. ``_ITALIC_RE`` has no
intra-word rule — it rejects a delimiter only when it sits next to whitespace or
another ``*``/``_`` — so a bare ``_`` inside a snake_case token opens emphasis.
While each gap was scanned alone a lone underscore usually had nothing to pair
with; across a mask, the ``_`` in ``t_549d081c`` pairs with the one in
``machine_type``, italicising everything between them, swallowing the code chip,
and *deleting* both delimiters. That is a worse failure than the one being fixed
here: literal asterisks are ugly but lossless, whereas this silently corrupts the
card IDs and machine types these reports exist to carry. ``_BOLD_RE``'s ``__``
alternative has the same shape.

So underscore runs that cannot be one half of a real emphasis pair are masked
too, as ``\\x01``, and restored when the element is emitted. Masking is used
rather than tightening ``_ITALIC_RE`` and ``_BOLD_RE`` directly because guarding
only their ``_`` alternatives means splitting each into separately-anchored
branches, which renumbers the capture group ``_walk_emphasis`` reads as
``m.group(1)``. The guard incidentally repairs the same-gap case (two snake_case
tokens with no span between them), which upstream mangles today.

Which runs to mask is decided by pairing them, not by looking at either run's
neighbours alone — that distinction is load-bearing, and review of two earlier
revisions proved it the hard way. A neighbour rule that masks only
alphanumeric-flanked runs leaves ``🚀_x``/``/tmp/_a``-shaped runs free to pair
across the message (review finding #2). Widening either side of the rule then
masks one half of legitimate emphasis and leaves the other half stranded — and
a stranded delimiter does not render harmlessly, it pairs with the *next*
stranded delimiter: mask the quote-adjacent openers in ``"_a_" and "_b_"`` and
the two orphaned closers italicise ``" and "``, deleting characters that both
upstream and the narrower guard rendered fine. Every one-sided mask just moves
the corruption to a different shape of text.

``_mask_underscores`` therefore classifies each run — can it open, can it
close, per CommonMark-style flanking (``_`` never opens or closes against an
alphanumeric on both sides) — pairs openers with closers on a stack, and masks
every run left unpaired. Two invariants fall out. Masked runs are restored
verbatim on emit, so a wrong *mask* decision shows a literal underscore and
never deletes; and every surviving run sits in a plausible open→close pair, so
the emphasis regexes downstream cannot be handed a stranded delimiter.

Deliberately asymmetric: intra-word ``*`` is *not* masked. CommonMark forbids
intra-word ``_`` emphasis and permits intra-word ``*``, upstream already
italicises ``cp a*b c*d`` with no code span involved, and masking ``*`` here
would break legitimate ``a**b**c``. So ``*`` keeps pairing across a masked span,
which makes the code-span case agree with the no-span case, and the two-wildcard
selector ``app=*,tier=*`` still loses its stars in both — pinned in
``test_slack_code_emphasis.py`` so the asymmetry is on the record.

Upstream: not reported. The renderer is Hermes-internal and this directory is
the repository's normal route for a Hermes fix.

Usage::

    python3 apply_slack_code_emphasis.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/block_kit.py"

# Asserted in the built bundle by the Dockerfile, so a patch that silently stops
# applying fails the image build instead of shipping literal asterisks.
BUILD_MARKER = "_CODE_SENTINEL_RE"

# ---------------------------------------------------------------------------
# 1) The comment above the inline regexes still has to describe what happens.
# ---------------------------------------------------------------------------

ORDER_COMMENT = "# Order matters: code first (opaque), then links, then emphasis.\n"

ORDER_COMMENT_PATCHED = (
    "# Order matters: code first (masked opaque, see _CODE_SENTINEL_RE), then\n"
    "# links, then emphasis.\n"
)

# ---------------------------------------------------------------------------
# 2) The sentinel pattern, minted next to the regexes it has to survive.
# ---------------------------------------------------------------------------

STRIKE = '_STRIKE_RE = re.compile(r"~~(.+?)~~")\n'

STRIKE_PATCHED = STRIKE + (
    "# kube-agents patch: what a masked inline-code span leaves behind for the\n"
    "# emphasis scan to step over as one opaque word. NUL cannot occur in a Slack\n"
    "# message, and the digits between the delimiters are neither whitespace nor\n"
    "# `*`/`_`, so _ITALIC_RE's lookarounds still pair around a masked span.\n"
    '_CODE_SENTINEL_RE = re.compile(r"\\x00(\\d+)\\x00")\n'
    "# kube-agents patch: every underscore run in the masked text, handed to\n"
    "# _mask_underscores below. Upstream's emphasis regexes have no intra-word\n"
    "# rule — a delimiter is rejected only next to whitespace or another `*`/`_`\n"
    "# — which was survivable only because splitting on code kept each stray `_`\n"
    "# in a scan of its own. Once the run is continuous, the `_` in `t_549d081c`\n"
    "# pairs with the one in `machine_type` across the span, eating both\n"
    "# delimiters and italicising everything between. Which runs get masked is\n"
    "# decided by pairing them (see _mask_underscores), not by a neighbour\n"
    "# regex: two earlier neighbour rules each traded one corruption for\n"
    "# another, because masking one half of a legitimate pair strands the other\n"
    "# half, and stranded delimiters pair with each other.\n"
    '_US_RUN_RE = re.compile(r"_+")\n'
)

# ---------------------------------------------------------------------------
# 3) The tokenizer itself: emit_text gains a restore step, walk masks.
# ---------------------------------------------------------------------------

TOKENIZER = '''\
    def emit_text(s: str, style: Optional[Dict[str, bool]] = None) -> None:
        if not s:
            return
        el: Dict[str, Any] = {"type": "text", "text": s}
        if style:
            el["style"] = style
        elements.append(el)

    # Tokenize by the highest-priority markers first using a single scan.
    # We recursively split on code, then links, then emphasis to keep spans
    # from overlapping incorrectly.
    def walk(s: str, style: Dict[str, bool]) -> None:
        pos = 0
        # inline code is opaque — no nested styling
        for m in _INLINE_CODE_RE.finditer(s):
            _walk_links(s[pos:m.start()], style)
            code_style = dict(style)
            code_style["code"] = True
            emit_text(m.group(1), code_style or None)
            pos = m.end()
        _walk_links(s[pos:], style)
'''

TOKENIZER_PATCHED = '''\
    def _append(s: str, style: Optional[Dict[str, bool]] = None) -> None:
        if not s:
            return
        # Masked underscores come back here: they were hidden from the
        # emphasis scan, not removed from the message.
        el: Dict[str, Any] = {"type": "text", "text": s.replace("\\x01", "_")}
        if style:
            el["style"] = style
        elements.append(el)

    def emit_text(s: str, style: Optional[Dict[str, bool]] = None) -> None:
        """Emit ``s``, restoring masked code spans as code-styled elements.

        A restored span inherits the style of the run it sits inside, so
        ``**`x`**`` emits one element styled bold *and* code rather than the
        literal asterisks the split-on-code tokenizer used to leave behind.
        """
        if not s:
            return
        pos = 0
        for m in _CODE_SENTINEL_RE.finditer(s):
            _append(s[pos:m.start()], style)
            code_style = dict(style or {})
            code_style["code"] = True
            _append(codes[int(m.group(1))], code_style)
            pos = m.end()
        _append(s[pos:], style)

    # kube-agents patch — see deploy/docker/patches/apply_slack_code_emphasis.py.
    # Inline code is masked rather than split on. Upstream handed the emphasis
    # scan each gap between code spans as a separate string, so a `**` opening
    # before a span and closing after it never paired and both delimiters
    # reached Slack as literal asterisks; card t_549d081c posted
    # "**`adam-new-cluster`** (us-east4) -> …" into a user's thread that way.
    # Masking keeps a span opaque — no markdown is interpreted inside it — while
    # leaving links and emphasis one continuous string to match across. That
    # continuity is also why stray underscores have to be masked: the split
    # used to keep the `_` in `t_549d081c` away from the one in `machine_type`,
    # and without a guard they would now pair across the span between them.
    codes: List[str] = []

    def _mask_code(s: str) -> str:
        def take(m: re.Match) -> str:
            codes.append(m.group(1))
            return f"\\x00{len(codes) - 1}\\x00"

        # A NUL or SOH already in the text would make a sentinel ambiguous.
        # Neither can occur in a Slack message, so dropping them costs nothing.
        masked = _INLINE_CODE_RE.sub(
            take, s.replace("\\x00", "").replace("\\x01", "")
        )
        # Code spans are held aside by now, so this only sees prose — mask the
        # underscores that cannot be real emphasis, before the emphasis scan
        # gets a chance to pair them across the span between them.
        return _mask_underscores(masked)

    def _mask_underscores(s: str) -> str:
        """Mask every underscore run that is not one half of a plausible pair.

        The emphasis regexes will pair ANY surviving opener-shaped run with ANY
        surviving closer-shaped run, however far apart, deleting both — so the
        only safe rule is one that reasons about pairs. A run is classified by
        CommonMark-style flanking (an underscore never opens or closes emphasis
        against an alphanumeric on both sides — that is the interior of
        ``t_549d081c``), openers pair with closers on a stack, and whatever is
        left unpaired is masked. Neighbour-only rules were tried and each one
        traded a corruption for a corruption: masking one half of the
        legitimate pair in ``"_a_" and "_b_"`` leaves two stranded closers that
        italicise ``" and "`` and eat both underscores.

        Masking is lossless — the SOH placeholder is restored to ``_`` on emit
        and in ``_unmask`` — so a wrong decision here shows a literal
        underscore, never a corrupted identifier. A code-span sentinel counts
        as a word character: the split tokenizer kept the underscore in
        ``[code]_prod`` inert, and word status is what keeps it that way now.
        """
        def _bucket(c: str) -> str:
            if not c or c.isspace():
                return "space"
            if c.isalnum() or c == "\\x00":
                return "word"
            return "punct"

        runs = list(_US_RUN_RE.finditer(s))
        if not runs:
            return s
        keep = [False] * len(runs)
        stack: List[int] = []
        for i, m in enumerate(runs):
            prev_c = s[m.start() - 1] if m.start() else ""
            next_c = s[m.end()] if m.end() < len(s) else ""
            # A run touching `*` is already inert: every emphasis lookaround
            # rejects a delimiter next to `*`/`_`, on either side. Masking it
            # would HIDE the underscore from those lookarounds and un-block
            # the neighbouring asterisks — `*_*` is a shell glob, and eating
            # its stars is the corruption this guard exists to prevent. Kept
            # verbatim it blocks them exactly as it does upstream.
            if prev_c == "*" or next_c == "*":
                keep[i] = True
                continue
            prev = _bucket(prev_c)
            nxt = _bucket(next_c)
            # CommonMark flanking, reduced to the three buckets: a run can
            # open when attached to the start of a word (left-flanking, and
            # not right-flanking unless punctuation precedes), close when
            # attached to the end of one. Word-interior runs are neither.
            left_flank = nxt != "space" and (nxt != "punct" or prev != "word")
            right_flank = prev != "space" and (prev != "punct" or nxt != "word")
            can_open = left_flank and (not right_flank or prev == "punct")
            can_close = right_flank and (not left_flank or nxt == "punct")
            if can_close and stack:
                keep[stack.pop()] = True
                keep[i] = True
            elif can_open:
                stack.append(i)
        parts: List[str] = []
        pos = 0
        for i, m in enumerate(runs):
            if keep[i]:
                continue
            parts.append(s[pos:m.start()])
            parts.append("\\x01" * (m.end() - m.start()))
            pos = m.end()
        parts.append(s[pos:])
        return "".join(parts)

    def _unmask(s: str) -> str:
        """Restore masked spans to their original backticked source.

        For the one place a code element cannot go: a Slack ``link`` carries
        flat ``text``/``url`` strings with no child elements, so the backticks
        come back rather than the span being dropped. Masked underscores are
        restored here too — a URL such as ``https://example.com/a_b`` reaches
        this function with its underscore hidden.
        """
        return _CODE_SENTINEL_RE.sub(
            lambda m: f"`{codes[int(m.group(1))]}`", s
        ).replace("\\x01", "_")

    def walk(s: str, style: Dict[str, bool]) -> None:
        _walk_links(_mask_code(s), style)
'''

# ---------------------------------------------------------------------------
# 4) A link's flat text/url get the source restored into them.
# ---------------------------------------------------------------------------

LINK = (
    '            link_el: Dict[str, Any] = '
    '{"type": "link", "url": m.group(2), "text": m.group(1)}\n'
)

LINK_PATCHED = """\
            link_el: Dict[str, Any] = {
                "type": "link",
                "url": _unmask(m.group(2)),
                "text": _unmask(m.group(1)),
            }
"""


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="slack_code_emphasis")
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(ORDER_COMMENT, ORDER_COMMENT_PATCHED)
    patch.substitute(STRIKE, STRIKE_PATCHED)
    patch.substitute(TOKENIZER, TOKENIZER_PATCHED)
    patch.substitute(LINK, LINK_PATCHED)
    patch.commit("4 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
