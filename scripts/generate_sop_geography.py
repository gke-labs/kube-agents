#!/usr/bin/env python3
"""Recompute the SOP line numbers each governance cron prompt cites.

Every audit prompt in ``agents/platform/cron/jobs.json`` tells its worker how
long the SOP is ("all N lines of it") and where the checks section sits
("section 2, lines A-B"). The numbers are what stops a model reading the
first screen and reporting a clean fleet it never looked at, and they rot the
moment the SOP is edited. ``test_cron_prompts_cite_the_real_sop_geography`` in
the fleet-audit suite fails on a stale pin; this script is what makes it
current again, so an SOP edit is followed by ``make docs-generate`` rather than
by measuring the file by hand.

Only the digits move. ``jobs.json`` is hand-maintained and does not survive a
JSON round-trip (key order, one-line prompts, the em dashes), so the roster is
never parsed and re-serialised: each prompt line is located in the raw text and
the two pins are rewritten in place by substitution. Every other byte of the
file is left as it was, and the result is parsed once to prove it is still the
same roster with different numbers.

The section span is measured the way the test measures it: a ``### <n>. ``
heading outside a fenced block opens the section, and it runs to the line
before the next ``### `` heading outside a fence, or to the end of the file. A
``### `` inside a fence is a shell comment or a JSON fragment, and counting it
would shift every span after it in the direction that makes a stale pin look
right. A fence is what CommonMark calls one, in the grammar the fleet-audit
harness's ``strip_fenced_blocks`` parses; the test reads its fences through
that function, and this script carries the same rule because it imports
nothing outside the standard library. The section number and the spelled-out
check count stay authored: the test checks both against the SOP, and a
generator that wrote them would be grading its own work.

Usage::

    python3 scripts/generate_sop_geography.py            # rewrite stale pins
    python3 scripts/generate_sop_geography.py --check    # exit 1 if any is stale

Standard library only, deliberately: this runs in CI and in a bare clone.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROSTER = REPO / "agents/platform/cron/jobs.json"
SOP_DIR = REPO / "agents/platform/governance"

# The three things a prompt says about its SOP. The first names the file the
# other two describe, in the profile-home form the prompt uses; the patterns
# are the geography test's, so what this writes is what that reads. A prompt
# that names two governance files is refused rather than measured: the pins
# describe one of them, and neither this script nor the test can tell which.
SOP_REF_RE = re.compile(r"governance/([A-Za-z0-9_-]+\.md)")
TOTAL_RE = re.compile(r"all (\d+) lines of it")
SPAN_RE = re.compile(r"are section (\d+), lines (\d+)-(\d+)")
# A prompt is one line of the roster, so a line carrying the key and an SOP
# reference is the line to rewrite. Anchoring on the key keeps a stray mention
# of an SOP in some other field from being edited.
PROMPT_LINE_RE = re.compile(r'^\s*"prompt":\s*"')
# What the line rule must reach: every parsed prompt that carries a pin. A
# pinned prompt written in a shape the rule does not match (a job object on
# one line, a space before the colon) would otherwise be neither rewritten nor
# compared, and ``--check`` would print ``ok`` over its stale numbers.
PROMPT_KEY_SHAPE = '"prompt": "'
# A fence is CommonMark's, as `strip_fenced_blocks` in the fleet-audit
# harness parses it: it opens on a run of three or more backticks or tildes
# indented at most three spaces, and closes on a run of the same character at
# least as long, indented at most three spaces, with nothing else on the line.
# A toggle on any line starting with ``` reads the inner fence of a
# four-backtick block as its closer, a four-space-indented run as a delimiter
# and a tilde fence as prose, and each exposes lines a heading scan then counts.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_MAX_INDENT = 3
# How much of a refused prompt line an error quotes: enough to identify the job, not the whole prompt.
ERROR_EXCERPT_CHARS = 120
SECTION_HEADING = "### "


def outside_fences(lines: list[str]):
    """Yield ``(1-indexed line number, text)`` for lines outside fenced blocks.

    The delimiters themselves are inside. An unterminated fence runs to the
    end of the file, as it does in every Markdown renderer.
    """
    fence_char = ""
    fence_len = 0
    for number, line in enumerate(lines, start=1):
        if fence_char:
            run = line.rstrip().lstrip(" ")
            indent = len(line.rstrip()) - len(run)
            if indent <= FENCE_MAX_INDENT and set(run) == {fence_char} and len(run) >= fence_len:
                fence_char = ""
                fence_len = 0
            continue
        opened = FENCE_OPEN_RE.match(line)
        if opened:
            fence_char = opened.group(1)[0]
            fence_len = len(opened.group(1))
            continue
        yield number, line


def section_span(lines: list[str], section: str) -> tuple[int, int]:
    """Return the first and last line of ``### <section>. `` in ``lines``.

    Exactly one heading must carry the number; zero or two is an SOP the prompt
    cannot cite, and the caller reports it rather than picking one.
    """
    starts = [n for n, line in outside_fences(lines) if line.startswith(SECTION_HEADING)]
    heading = f"{SECTION_HEADING}{section}. "
    where = [n for n in starts if lines[n - 1].startswith(heading)]
    if len(where) != 1:
        raise ValueError(f"{len(where)} sections headed {heading!r}; a prompt cites one")
    after = [n for n in starts if n > where[0]]
    return where[0], (after[0] - 1) if after else len(lines)


def rewrite_prompt_line(line: str, sop_dir: Path) -> str:
    """Return ``line`` with its two pins recomputed from the SOP it names.

    A prompt line with neither pin is returned unchanged: a prompt that pins
    nothing has nothing to regenerate, and inventing a pin for it is the
    geography test's decision, not this script's. A prompt with one pin but
    not the other, or with a pin and no SOP reference to measure it against,
    is refused: the test fails that roster, and a generator that printed
    ``ok`` over it would be the second gate disagreeing with the first. A
    prompt naming two governance files is refused too, although the test
    passes it as long as one of them is the stream's SOP: measuring the first
    one mentioned would write the other file's numbers into the prompt, and
    ``ok`` over the wrong digits is worse than a refusal the author can read.
    """
    names = sorted(set(SOP_REF_RE.findall(line)))
    total = TOTAL_RE.search(line)
    span = SPAN_RE.search(line)
    if total is None and span is None:
        return line
    if total is None or span is None:
        missing = "length" if total is None else "checks-section span"
        raise ValueError(f"a prompt pins its SOP but states no {missing}: {line.strip()[:ERROR_EXCERPT_CHARS]}")
    if not names:
        raise ValueError(f"a prompt pins an SOP it does not name: {line.strip()[:ERROR_EXCERPT_CHARS]}")
    if len(names) > 1:
        raise ValueError(
            f"a prompt names {len(names)} governance files ({', '.join(names)}) and pins one of them; "
            f"name only the SOP the pins describe: {line.strip()[:ERROR_EXCERPT_CHARS]}"
        )
    sop = sop_dir / names[0]
    if not sop.is_file():
        raise ValueError(f"prompt cites {names[0]}, which is not in {sop_dir}")
    lines = sop.read_text(encoding="utf-8").splitlines()
    section = span.group(1)
    try:
        first, last = section_span(lines, section)
    except ValueError as exc:
        raise ValueError(f"{sop.name}: {exc}") from exc
    line = TOTAL_RE.sub(f"all {len(lines)} lines of it", line, count=1)
    return SPAN_RE.sub(f"are section {section}, lines {first}-{last}", line, count=1)


def rewrite_roster(text: str, sop_dir: Path = SOP_DIR) -> str:
    """Return the roster text with every governance prompt's pins current.

    The text is processed line by line and only prompt lines are touched, so
    the returned string differs from ``text`` in digits inside those lines and
    nowhere else. Two checks follow. The original is parsed and every prompt
    that carries a pin is counted against the pinned lines the line rule
    reached: a pinned prompt in a shape the rule does not match is refused,
    since ``--check`` would otherwise print ``ok`` over numbers it never read.
    Then the result is parsed and compared field by field against the
    original, prompts excepted, so a substitution that broke the JSON or
    reached another field fails here rather than at the next ``hermes cron``.
    """
    out = []
    reached = 0
    for line in text.split("\n"):
        if PROMPT_LINE_RE.match(line):
            reached += _pins_an_sop(line)
            line = rewrite_prompt_line(line, sop_dir)
        out.append(line)
    new_text = "\n".join(out)
    roster = json.loads(text)
    pinned = sum(_pins_an_sop(job.get("prompt", "")) for job in roster["jobs"])
    if reached != pinned:
        raise ValueError(
            f"{pinned} prompt(s) pin an SOP but {reached} open a line of their own with "
            f"{PROMPT_KEY_SHAPE!r}; the generator rewrites only those, so it cannot vouch for the rest"
        )
    if _without_prompts(roster) != _without_prompts(json.loads(new_text)):
        raise ValueError("rewriting the pins changed something other than a prompt")
    return new_text


def _pins_an_sop(prompt: str) -> bool:
    """Whether ``prompt`` carries either pin.

    The same answer for a raw roster line and for the parsed prompt, since
    neither pattern contains a character JSON escapes.
    """
    return bool(TOTAL_RE.search(prompt) or SPAN_RE.search(prompt))


def _without_prompts(roster: dict) -> list[dict]:
    return [{k: v for k, v in job.items() if k != "prompt"} for job in roster["jobs"]]


def stale_prompts(text: str, new_text: str) -> list[str]:
    """The ids of the jobs whose prompt the rewrite changed."""
    before = {job["id"]: job.get("prompt", "") for job in json.loads(text)["jobs"]}
    after = {job["id"]: job.get("prompt", "") for job in json.loads(new_text)["jobs"]}
    return [job_id for job_id, prompt in before.items() if after[job_id] != prompt]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any prompt's pins are stale",
    )
    args = ap.parse_args(argv)

    text = ROSTER.read_text(encoding="utf-8")
    try:
        new_text = rewrite_roster(text)
    except ValueError as exc:
        print(f"{ROSTER.relative_to(REPO)}: {exc}", file=sys.stderr)
        return 1
    rel = ROSTER.relative_to(REPO)
    stale = stale_prompts(text, new_text)
    if not stale:
        print(f"  ok       {rel} [sop-geography]")
        return 0
    for job_id in stale:
        print(f"  {'STALE' if args.check else 'updated'}    {rel} [sop-geography: {job_id}]")
    if args.check:
        print(
            "\nStale SOP line numbers. Run `make docs-generate` and commit the result.",
            file=sys.stderr,
        )
        return 1
    ROSTER.write_text(new_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
