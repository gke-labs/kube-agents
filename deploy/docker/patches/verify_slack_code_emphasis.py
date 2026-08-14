#!/usr/bin/env python3
"""Build-time behaviour gate for the Slack code-emphasis patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_code_emphasis.py``. The applier proves the four
anchors matched and that the file still parses; this proves the shipped renderer
actually stops emitting the literal asterisks that card ``t_549d081c`` put in a
user's Slack thread.

The distinction matters because every way this patch can fail is silent. A
sentinel that never gets restored, a style dict that comes back empty, an
emphasis rule that stops pairing across the mask — none of them raise. They all
look exactly like a report that happened to contain no bolded code, and the next
person to notice is the user reading the thread. So this drives the real
``render_blocks`` over the real reported line and asserts on the elements it
returns, rather than grepping for the text the applier just inserted.

``test_slack_code_emphasis.py`` covers the applier against a fixture and cannot
cover any of this: the edit lives inside Hermes' own module, and the unit suite
never sees the file that ships.

The module is loaded by path rather than imported as
``plugins.platforms.slack.block_kit`` so the gate does not depend on the
package's ``__init__`` — and therefore on the Slack SDK — being importable at
build time. ``block_kit.py`` imports only ``re`` and ``typing``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RELATIVE = "plugins/platforms/slack/block_kit.py"

# The list item from card t_549d081c, verbatim. Standard Markdown: a bolded
# code span, which the report-format stanza and the platform persona between
# them actively steer workers towards writing.
REPORTED = "- **`adam-new-cluster`** (us-east4) -> Spawning worker card `t_79d6d3d1`"


def _fail(detail: str) -> "SystemExit":
    return SystemExit(f"slack_code_emphasis verify: {detail}")


def _load(root: Path):
    path = root / RELATIVE
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("block_kit_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text_elements(blocks) -> list:
    """Every leaf element of every rich_text block, in document order."""
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") in ("text", "link"):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blocks)
    return found


def main(root: Path) -> None:
    block_kit = _load(root)

    blocks = block_kit.render_blocks(REPORTED)
    if not blocks:
        raise _fail(f"render_blocks returned {blocks!r} for the reported line")

    elements = _text_elements(blocks)
    texts = [e.get("text", "") for e in elements]

    # 1) The defect itself: no delimiter may survive into a rendered element.
    for text in texts:
        if "**" in text:
            raise _fail(
                "the reported line still renders a literal '**' — the emphasis "
                f"scan is not matching across a masked code span. Got: {elements!r}"
            )

    # 2) The repair: the cluster name is one element, styled bold *and* code.
    #    Asserted positively so a patch that fixed (1) by deleting the markup
    #    rather than honouring it cannot pass.
    match = [e for e in elements if e.get("text") == "adam-new-cluster"]
    if len(match) != 1:
        raise _fail(
            f"expected exactly 1 'adam-new-cluster' element, got {len(match)}: "
            f"{elements!r}"
        )
    style = match[0].get("style") or {}
    if not (style.get("bold") and style.get("code")):
        raise _fail(
            f"'adam-new-cluster' should be styled bold+code, got {style!r}"
        )

    # 3) Code with no emphasis around it is still plain code, so the patch has
    #    not simply started bolding every span it restores.
    plain = [e for e in elements if e.get("text") == "t_79d6d3d1"]
    if len(plain) != 1 or (plain[0].get("style") or {}) != {"code": True}:
        raise _fail(
            f"'t_79d6d3d1' should be styled code-only, got {plain!r}"
        )

    # 4) A code span stays opaque: masking must not have started interpreting
    #    markdown inside one.
    opaque = _text_elements(block_kit.render_blocks("- `a **b** c`"))
    if [e.get("text") for e in opaque] != ["a **b** c"]:
        raise _fail(
            f"markdown inside a code span is no longer opaque: {opaque!r}"
        )

    # 5) Identifier underscores either side of a span must not pair. Masking
    #    hands the emphasis scan one continuous string, and _ITALIC_RE has no
    #    intra-word rule, so without the guard the `_` in `t_549d081c` pairs
    #    with the one in `machine_type` and both are deleted. That corrupts an
    #    identifier silently, which is worse than the defect fixed above, so it
    #    stops the build rather than shipping.
    ident = _text_elements(
        block_kit.render_blocks(
            "- Card t_549d081c: cluster `c` needs machine_type e2"
        )
    )
    rendered = "".join(e.get("text", "") for e in ident)
    if rendered != "Card t_549d081c: cluster c needs machine_type e2":
        raise _fail(
            "intra-word underscores are pairing across a masked code span — "
            f"identifiers are being corrupted. Got: {rendered!r}"
        )
    if any((e.get("style") or {}).get("italic") for e in ident):
        raise _fail(f"spurious italic across a code span: {ident!r}")

    # 6) The guard is intra-word only — genuinely delimited underscore emphasis
    #    still has to work. `\W` here rather than `\w` is the whole point: `_`
    #    is a word character, so a `\w`-based guard degrades `__bold__` to
    #    italic, which this catches.
    #
    #    Every case carries trailing text after the closing delimiter, and that
    #    is not decoration. A guard whose lookahead is wrong cannot fire at the
    #    end of a string, because there is no following character to inspect —
    #    so `- _i_` passes under a broken guard and proves nothing. An earlier
    #    revision of this patch emitted `[^\\W_]` (the set {backslash, W, _})
    #    instead of `[^\W_]`, masked the closing delimiter of every `_i_ x` in
    #    the fleet's output, and sailed through the end-of-string form of this
    #    very check. Keep the trailing text.
    for source, expected_style in (
        ("- __b__ trailing", "bold"),
        ("- _i_ trailing", "italic"),
        ("- a __b__ trailing", "bold"),
        ("- a _i_ trailing", "italic"),
    ):
        got = _text_elements(block_kit.render_blocks(source))
        styled = [e for e in got if (e.get("style") or {}).get(expected_style)]
        if len(styled) != 1:
            raise _fail(
                f"{source!r} should render exactly one {expected_style} "
                f"element, got {got!r}"
            )
        if not any("trailing" in e.get("text", "") for e in got):
            raise _fail(f"{source!r} lost its trailing text: {got!r}")

    # 7) The everyday content of a report — a path, a URL, a wildcard — must
    #    come back exactly as typed. These carry no markup at all, so any
    #    difference is a character the renderer ate, and an identifier a reader
    #    would copy out of Slack and fail to find.
    for plain in (
        "/var/log/kube_agents/agent.log and /var/log/kube_agents/err.log",
        "https://example.com/foo_bar?x=1&y=2#sec_3",
        "export KUBE_AGENTS_HOME=/opt/hermes",
        "kubectl get pods -n * -o wide",
        "e2-standard-8 and n2_standard_16",
        # `*_*` stays whole only while the underscore is visible to the
        # emphasis lookarounds — a guard that masks it lets the stars pair.
        "ls *_*.yaml and match_* files",
    ):
        got = _text_elements(block_kit.render_blocks(f"- {plain}"))
        joined = "".join(e.get("text", "") for e in got)
        if joined != plain:
            raise _fail(
                f"plain text was altered by the renderer: {plain!r} -> {joined!r}"
            )
        if any(e.get("style") for e in got):
            raise _fail(f"plain text picked up a style: {got!r}")

    # 8) Underscores glued to punctuation or to a code span must not pair
    #    either (review finding #2: the first guard only masked alphanumeric-
    #    flanked runs, so `/tmp/_a` could pair with `/tmp/_b` across a span) —
    #    while emphasis whose delimiters touch punctuation must keep working
    #    (the failure mode of both neighbour-rule guards: mask one half of
    #    `"_i_"` and the stranded half pairs with the next stranded half).
    for source in ("- /tmp/_a and `x` and /tmp/_b", "- `c`_prod and `d`_dev"):
        got = _text_elements(block_kit.render_blocks(source))
        joined = "".join(e.get("text", "") for e in got)
        if "_" not in joined or any(
            (e.get("style") or {}).get("italic") for e in got
        ):
            raise _fail(
                f"edge-adjacent underscores paired across a span: {source!r} "
                f"-> {got!r}"
            )
    quoted = _text_elements(block_kit.render_blocks('- "_i_" and "_j_" trailing'))
    italics = [e for e in quoted if (e.get("style") or {}).get("italic")]
    if [e.get("text") for e in italics] != ["i", "j"]:
        raise _fail(
            f"quoted emphasis broke — a guard is stranding delimiters: {quoted!r}"
        )

    # 9) No sentinel may survive into a rendered element. An escaped \x00/\x01
    #    is invisible in a diff and in most terminals, and only shows up as a
    #    mangled glyph in the thread itself.
    leaked = _text_elements(
        block_kit.render_blocks(
            "- Card t_549d081c `chip` machine_type and [a_b](https://ex.com/c_d)"
        )
    )
    for element in leaked:
        if "\x00" in element.get("text", "") or "\x01" in element.get("text", ""):
            raise _fail(f"a sentinel escaped into the output: {element!r}")

    print("slack_code_emphasis verify: ok")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
