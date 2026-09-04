#!/usr/bin/env python3
"""Unit tests for the self-improvement ledger viewer.

Run: cd scripts && python3 -m unittest test_selfimprove_ledger_view

Every test here is cluster-free: the viewer's `--file` path takes a ledger
somebody has already pulled down, which is the same door the renderers come in
through. Nothing below shells out to kubectl.

Two classes of failure are worth more than the rest. The first is a misaligned
table, which is the whole reason this tool exists over `kubectl | jq` -- and it
breaks silently, because a colour code or a hyperlink occupies bytes and no
columns, so the width arithmetic is right up until it is measuring an escape
sequence. Several tests below assert that every rendered line of a table has
the same *visible* width, which is the only statement of correctness that
catches that. The second is the viewer quietly disagreeing with the loop: the
occurrence counts and the gate verdicts are the loop's own functions, reused,
and `TestReusesTheLoopsOwnMaths` is what says so.
"""

import contextlib
import datetime as _dt
import io
import json
import os
import pathlib
import re
import tempfile
import time
import unittest
from unittest import mock

import selfimprove_ledger_view as view

NOW = _dt.datetime(2026, 8, 23, 20, 0, 0, tzinfo=_dt.timezone.utc)


def iso(hours_ago):
    return (NOW - _dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def finding(fingerprint, severity, title, **overrides):
    entry = {
        "fingerprint": fingerprint,
        "signal": "errors",
        "severity": severity,
        "title": title,
        "location": "k8s-operator/internal/controller/platformagent_controller.go:1090",
        "summary": "a summary",
        "evidence": "some evidence",
        "proposed_fix": "a fix",
        "confidence": "high",
        "user_impact": "an impact",
        "first_seen": iso(6),
        "last_seen": iso(1),
        "revision": "aa3b7aa1111111111111111111111111111111",
        "sightings": [{"at": iso(3), "count": 4}, {"at": iso(1), "count": 5}],
        "promotions": [],
    }
    entry.update(overrides)
    return entry


def ledger(**overrides):
    document = {
        "version": 1,
        "findings": {
            "aaaa000000000000": finding("aaaa000000000000", "medium", "A medium finding"),
            "bbbb111111111111": finding(
                "bbbb111111111111",
                "critical",
                "A critical finding",
                signal="latency",
                promotions=[
                    {
                        "at": iso(2),
                        "url": "https://github.com/gke-agentic/kube-agents/pull/160",
                        "revision": "aa3b7aa",
                    }
                ],
            ),
            "cccc222222222222": finding(
                "cccc222222222222", "low", "A low finding", signal="inefficiency"
            ),
        },
        "runs": [
            {"at": iso(3), "revision": "62cdf89", "outcome": "ok", "findings": 4, "promoted": 2, "filed": 1, "note": ""},
            {"at": iso(1), "revision": "aa3b7aa", "outcome": "ok", "findings": 5, "promoted": 1, "filed": 0, "note": ""},
        ],
    }
    document.update(overrides)
    return document


GATE = {
    "maxPullRequestsPerDay": 3,
    "cooldownHours": 24,
    "rules": [
        {"severity": "critical", "minOccurrencesPerDay": 1},
        {"severity": "medium", "minOccurrencesPerDay": 2},
    ],
}


@contextlib.contextmanager
def ledger_file(document):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(document, handle)
        path = handle.name
    try:
        yield path
    finally:
        os.unlink(path)


@contextlib.contextmanager
def local_zone(name):
    """Run the block as though the reader's machine were in `name`.

    Some of what `stamp` has to survive depends on which side of UTC the reader
    is on, and a test that only fails in Los Angeles is a test that passes here.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


#: One OSC 8 hyperlink, in the exact form this file emits for its own links, so
#: a terminal has no way to tell it from one the viewer meant.
INJECTED_LINK = "\x1b]8;;https://evil.example/pwn\x1b\\click here\x1b]8;;\x1b\\"


def run_main(argv):
    """`main` with stdout captured, returning (exit code, text)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = view.main(argv)
    return code, buffer.getvalue()


def table_blocks(text):
    """The report's tables, as runs of consecutive bordered lines.

    Grouped rather than lumped together because a report holds several tables
    with different columns, and only lines from the same table have any reason
    to agree on a width.
    """
    blocks, current = [], []
    for line in text.splitlines():
        if line and line[0] in "┌│├└+|":
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


# --------------------------------------------------------------------------


class TestWidthMeasurement(unittest.TestCase):
    """`plain` is the only thing standing between colour and a broken table."""

    def test_strips_sgr_colour(self):
        self.assertEqual(view.plain("\033[1;31mred\033[0m"), "red")

    def test_strips_osc_8_hyperlinks(self):
        linked = "\x1b]8;;https://example.com/a/very/long/url\x1b\\text\x1b]8;;\x1b\\"
        self.assertEqual(view.plain(linked), "text")

    def test_a_hyperlinked_cell_measures_as_its_text_not_its_url(self):
        palette = view.Palette(True)
        cell = view.hyperlink("short", "https://github.com/o/r/pull/12345", palette)
        self.assertGreater(len(cell), 30)
        self.assertEqual(len(view.plain(cell)), len("short"))

    def test_padding_uses_visible_width(self):
        palette = view.Palette(True)
        padded = view._pad(palette("ab", "red"), 6, "l")
        self.assertEqual(len(view.plain(padded)), 6)


class TestScrubbingUntrustedText(unittest.TestCase):
    """Every field this report draws is text an agent read in production logs.

    A log line holds whatever reached it, so a `summary` can carry the OSC 8
    introducer this file uses for its own links and a `title` can carry the CSI
    sequence for "erase display". `plain` is no defence: it strips the two forms
    the viewer emits, which means an injected one measures zero columns wide, so
    the table both misaligns and passes the assertions above that exist to catch
    misalignment. `--color never` is no defence either -- it gates what this file
    emits and says nothing about what the ledger holds.
    """

    def test_an_osc_8_introducer_loses_its_escape_and_keeps_its_text(self):
        scrubbed = view.scrub(INJECTED_LINK)
        self.assertNotIn("\x1b", scrubbed)
        self.assertIn("evil.example", scrubbed)

    def test_an_erase_display_sequence_is_defused_in_place(self):
        self.assertEqual(view.scrub("A\x1b[2Jbcd"), "A[2Jbcd")

    def test_the_single_byte_c1_introducer_goes_too(self):
        r"""0x9b is CSI to a terminal that decodes the C1 range, so `\x9b2J` is
        the instruction `\x1b[2J` spells in three bytes, and a filter looking
        only for ESC walks straight past it."""
        self.assertEqual(view.scrub("A\x9b2Jbcd"), "A2Jbcd")

    def test_a_bidirectional_override_is_removed(self):
        """U+202E reverses the run after it, so a location can display a path it
        does not contain -- the Trojan Source trick, and a `path:line` is exactly
        the string a reader takes at face value."""
        self.assertEqual(view.scrub("main\u202egpj.exe"), "maingpj.exe")

    def test_a_tab_becomes_a_space_rather_than_jumping_a_column(self):
        self.assertEqual(view.scrub("a\tb"), "a b")

    def test_a_newline_survives_because_a_cell_stacks_paragraphs_on_it(self):
        self.assertEqual(view.scrub("a\nb"), "a\nb")

    def test_a_non_string_is_returned_as_it_came(self):
        for value in (5, 1.5, True, None):
            self.assertIs(view.scrub(value), value)

    def test_scrub_document_reaches_nested_values_and_keys(self):
        """Keys as well as values: the findings map is keyed by fingerprint and
        the gate's verdicts are looked up by the `fingerprint` field, so
        scrubbing one and not the other would quietly stop them matching."""
        document = {"aa\x1b[2Jbb": [{"t": "x\x1b[31my"}, 4]}
        self.assertEqual(view.scrub_document(document), {"aa[2Jbb": [{"t": "x[31my"}, 4]})

    def test_only_an_http_url_is_made_clickable(self):
        """A promotion's `url` is a ledger field like every other, and OSC 8 does
        not care what it wraps: a terminal handed `file:///` or a scheme the
        desktop has registered may pass it to the operating system on a click."""
        palette = view.Palette(True)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "vscode://x/y"):
            self.assertEqual(view.hyperlink("label", url, palette), "label", url)
        self.assertIn(
            "\x1b]8;;https://example.com", view.hyperlink("label", "https://example.com", palette)
        )

    # ----------------------------------------------------------------------
    # The boundary, exercised through `main`: sanitising happens once where the
    # document arrives, not at each of the dozen call sites that draw a field.

    def injected(self):
        """A ledger carrying an escape sequence in every field the report draws."""
        document = ledger()
        entry = document["findings"]["aaaa000000000000"]
        entry["title"] = "A title\x1b[2J"
        entry["summary"] = INJECTED_LINK
        entry["evidence"] = INJECTED_LINK
        entry["signal"] = "errors\x1b[31m"
        entry["confidence"] = "high\x9b2J"
        entry["location"] = "k8s-operator/cmd/main.go:1\u202e"
        entry["promotions"] = [{"at": iso(1), "url": INJECTED_LINK}]
        document["runs"][0]["note"] = INJECTED_LINK
        document["runs"][0]["outcome"] = "ok\x1b[5m"
        return document

    def test_no_escape_from_the_ledger_survives_into_the_report(self):
        with ledger_file(self.injected()) as path:
            code, text = run_main(["--file", path, "--color", "never", "--width", "170"])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)

    def test_the_defused_text_is_still_on_the_page(self):
        """Only the control character is removed. Deleting the whole sequence
        would leave a reader looking at doctored text with nothing to say so."""
        with ledger_file(self.injected()) as path:
            _, text = run_main(["--file", path, "--color", "never", "--width", "170"])
        self.assertIn("evil.example", text)

    def assertNoInjectedLink(self, text):
        """No OSC 8 hyperlink in `text` points anywhere the ledger asked it to.

        Matched on the host rather than the whole URL because the label a
        promotion is drawn with is clipped to the column, and the clipped form
        is what the pre-existing code made clickable.
        """
        targets = [url for _, url in re.findall(r"\x1b\]8;([^;]*);([^\x1b]*)\x1b", text)]
        self.assertEqual([url for url in targets if "evil.example" in url], [])

    def test_a_ledger_hyperlink_never_becomes_the_reports_hyperlink(self):
        """With colour on the viewer emits OSC 8 itself, which is the one
        sequence a terminal cannot tell from an injected one."""
        with ledger_file(self.injected()) as path:
            _, text = run_main(["--file", path, "--color", "always", "--width", "170"])
        self.assertNoInjectedLink(text)

    def test_the_detail_view_is_behind_the_same_boundary(self):
        with ledger_file(self.injected()) as path:
            code, text = run_main(["--file", path, "--color", "always", "--detail", "aaaa"])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[2J", text)
        self.assertNoInjectedLink(text)

    def test_json_still_carries_exactly_what_the_configmap_holds(self):
        """`--json` is deliberately outside the boundary: `json.dumps` escapes a
        control character rather than emitting it, so that path is already inert,
        and somebody piping the document into `jq` wants the bytes as stored."""
        document = self.injected()
        with ledger_file(document) as path:
            _, text = run_main(["--file", path, "--json"])
        self.assertNotIn("\x1b", text)
        self.assertEqual(
            json.loads(text)["findings"]["aaaa000000000000"]["summary"],
            document["findings"]["aaaa000000000000"]["summary"],
        )


class TestTableRendering(unittest.TestCase):
    def render(self, columns, rows, width=100, colour=True, box=None):
        return view.render_table(
            columns, rows, view.Palette(colour), width, box or view.BOX_UNICODE
        )

    def test_every_line_has_the_same_visible_width(self):
        columns = [view.Column("A"), view.Column("B", wrap=True, min_width=10)]
        rows = [
            [("x", "red"), ("a much longer cell that will certainly need wrapping", "dim")],
            [("yy", None), ("short", None)],
        ]
        widths = {len(view.plain(line)) for line in self.render(columns, rows, width=60)}
        self.assertEqual(len(widths), 1, "table lines disagree on width: %s" % sorted(widths))

    def test_colour_does_not_change_the_layout(self):
        columns = [view.Column("A"), view.Column("B", wrap=True)]
        rows = [[("x", "red"), ("some text here", "green")]]
        coloured = [view.plain(line) for line in self.render(columns, rows)]
        self.assertEqual(coloured, self.render(columns, rows, colour=False))

    def test_a_hyperlink_does_not_change_the_layout(self):
        columns = [view.Column("PR"), view.Column("T", wrap=True, min_width=20)]
        url = "https://github.com/gke-agentic/kube-agents/pull/160"
        with_link = self.render(columns, [[("o/r#160", "blue", url), ("a title", None)]])
        without = self.render(columns, [[("o/r#160", "blue"), ("a title", None)]])
        self.assertEqual([view.plain(l) for l in with_link], [view.plain(l) for l in without])

    def test_a_link_is_emitted_once_and_never_on_a_blank_continuation_line(self):
        """Regression: the OSC sequence was wrapped around the empty padding
        lines a taller neighbouring column produces, which every terminal
        renders as visible escape litter and no reader can click."""
        columns = [view.Column("PR"), view.Column("T", wrap=True, min_width=12)]
        url = "https://github.com/gke-agentic/kube-agents/pull/160"
        rows = [[("o/r#160", "blue", url), ("a title long enough to wrap over several lines", None)]]
        lines = self.render(columns, rows, width=44)
        self.assertGreater(sum(1 for line in lines if "o/r#160" in view.plain(line)), 0)
        self.assertEqual(sum(line.count("\x1b]8;;" + url) for line in lines), 1)

    def test_per_line_styles_colour_each_paragraph_of_one_cell(self):
        columns = [view.Column("C", wrap=True, min_width=30)]
        rows = [[("title\nlocation\nverdict", None, None, {1: "cyan", 2: "green"})]]
        lines = self.render(columns, rows, width=40)
        body = [l for l in lines if "location" in view.plain(l) or "verdict" in view.plain(l)]
        self.assertTrue(any(view.STYLES["cyan"] in l for l in body))
        self.assertTrue(any(view.STYLES["green"] in l for l in body))
        title = [l for l in lines if "title" in view.plain(l)][0]
        self.assertNotIn(view.STYLES["cyan"], title)

    def test_a_per_paragraph_link_reaches_a_cell_no_whole_cell_link_could(self):
        """The FINDING cell always stacks a title over a location, so it never
        has the single-line form a whole-cell URL requires. The per-paragraph
        map is the only way its location is ever clickable."""
        columns = [view.Column("C", wrap=True, min_width=40)]
        url = "https://github.com/o/r/blob/abc/x.go#L1"
        cell = ("a title\nx.go:1", None, None, {1: "cyan"}, {1: url})
        lines = self.render(columns, [[cell]], width=50)
        self.assertEqual(sum(line.count("\x1b]8;;" + url) for line in lines), 1)
        # The whole-cell form on the same cell renders no link at all.
        plain_cell = ("a title\nx.go:1", None, url)
        self.assertEqual(
            sum(l.count("\x1b]8;;" + url) for l in self.render(columns, [[plain_cell]], width=50)),
            0,
        )

    def test_a_wrapped_per_paragraph_link_is_drawn_on_every_row_it_spans(self):
        """A location is longer than the column at any ordinary width, so
        dropping wrapped links left the table with none. Each row it spans is
        linked, and a shared `id=` makes a terminal treat them as one.

        The label and the destination name the same file. That is the property
        the location cell exists for, and the fixture used to assert it with a
        path the URL did not contain, which is the bug it should have caught.
        """
        columns = [view.Column("C", wrap=True, min_width=12)]
        path = "some/quite/long/path/that/will/wrap.go"
        url = "https://github.com/o/r/blob/abc/%s#L1" % path
        cell = ("t\n%s:1" % path, None, None, None, {1: url})
        lines = self.render(columns, [[cell]], width=24)
        opens = [m for line in lines for m in re.findall(r"\x1b\]8;([^;]*);([^\x1b]*)\x1b", line)]
        self.assertGreater(len(opens), 1, "the paragraph should have wrapped")
        self.assertEqual({url}, {u for _, u in opens if u})
        ids = {p for p, u in opens if u}
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids.pop().startswith("id="))
        # Reassembled across the rows it wrapped over: borders and padding out,
        # and what is left has to be the path the URL points at.
        visible = re.sub(r"[│|\s]", "", "".join(view.plain(line) for line in lines))
        self.assertIn(url.split("/blob/abc/", 1)[1].split("#", 1)[0], visible)

    def test_two_wrapped_links_in_one_table_do_not_share_an_id(self):
        """A shared `id=` means "one hyperlink", so reusing it across two
        findings would fuse two destinations into one."""
        columns = [view.Column("C", wrap=True, min_width=12)]
        rows = [
            [("t\nsome/quite/long/path/one.go:1", None, None, None, {1: "https://x/a"})],
            [("t\nsome/quite/long/path/two.go:2", None, None, None, {1: "https://x/b"})],
        ]
        lines = self.render(columns, rows, width=24)
        ids = {m for line in lines for m in re.findall(r"\x1b\]8;(id=[^;]*);[^\x1b]", line)}
        self.assertEqual(len(ids), 2)

    def test_blank_padding_lines_are_never_linked(self):
        """A short cell beside a tall one is padded with blank lines; a link on
        one is invisible and unclickable."""
        columns = [view.Column("A", wrap=True, min_width=12), view.Column("B", wrap=True, min_width=12)]
        rows = [[("x.go:1", None, None, None, {0: "https://x/a"}), ("many words that wrap over rows", None)]]
        for line in self.render(columns, rows, width=32):
            if "x.go" not in view.plain(line):
                self.assertNotIn("\x1b]8;", line)

    def test_a_per_paragraph_link_does_not_change_the_layout(self):
        columns = [view.Column("C", wrap=True, min_width=40)]
        url = "https://github.com/o/r/blob/abc/x.go#L1"
        linked = self.render(columns, [[("a title\nx.go:1", None, None, None, {1: url})]])
        bare = self.render(columns, [[("a title\nx.go:1", None)]])
        self.assertEqual([view.plain(l) for l in linked], [view.plain(l) for l in bare])

    def test_long_unbroken_text_is_broken_rather_than_overflowing(self):
        columns = [view.Column("P", wrap=True, min_width=10)]
        rows = [[("a/very/long/path/with/no/spaces/at/all/in/it/anywhere.go:1090", None)]]
        widths = {len(view.plain(line)) for line in self.render(columns, rows, width=30)}
        self.assertEqual(len(widths), 1)

    def test_ascii_mode_emits_no_box_drawing_characters(self):
        columns = [view.Column("A"), view.Column("B")]
        lines = self.render(columns, [[("x", None), ("y", None)]], box=view.BOX_ASCII)
        self.assertFalse(any(ch in "".join(lines) for ch in "─│┌┬┐├┼┤└┴┘"))

    def test_a_narrow_terminal_keeps_the_minimum_rather_than_collapsing(self):
        columns = [view.Column("LONG COLUMN NAME"), view.Column("B", wrap=True, min_width=14)]
        rows = [[("a value", None), ("some wrapping text goes here", None)]]
        lines = self.render(columns, rows, width=20)
        self.assertEqual(len({len(view.plain(l)) for l in lines}), 1)
        self.assertIn("some", "".join(view.plain(l) for l in lines))


class TestRowSeparation(unittest.TestCase):
    """`--rows`. A findings row is a four- or five-line stack with only its
    first line filled in outside the FINDING column, so without a separator
    there is nothing to say where one record stops and the next starts."""

    COLUMNS = [view.Column("A"), view.Column("B", wrap=True, min_width=12)]
    ROWS = [
        [("1", None), ("a cell long enough to wrap over several lines", None)],
        [("2", None), ("another one that also wraps", None)],
    ]

    def render(self, separator, colour=False, width=32):
        return view.render_table(
            self.COLUMNS, self.ROWS, view.Palette(colour), width, view.BOX_UNICODE, separator
        )

    def test_a_separator_costs_exactly_one_line_per_extra_row(self):
        """The whole point of the flag is that it does not make the table
        bigger than it has to be: two rows buy one separator, not two."""
        compact = len(self.render("none"))
        self.assertEqual(len(self.render("blank")), compact + 1)
        self.assertEqual(len(self.render("rule")), compact + 1)

    def test_no_separator_is_added_above_the_first_row_or_below_the_last(self):
        for separator in ("blank", "rule"):
            lines = [view.plain(l) for l in self.render(separator)]
            body = lines[3:-1]
            self.assertTrue(body[0].strip("│ ").startswith("1"), separator)
            self.assertIn("another", " ".join(body[-1:] + body[-2:]), separator)

    def test_every_line_still_has_the_same_visible_width(self):
        """A separator built by hand rather than from the resolved widths is
        the way this breaks: the table stays readable and stops lining up."""
        for separator in ("none", "blank", "rule"):
            widths = {len(view.plain(line)) for line in self.render(separator)}
            self.assertEqual(len(widths), 1, "%s: %s" % (separator, sorted(widths)))

    def test_a_blank_separator_keeps_the_borders_and_empties_the_cells(self):
        added = [
            view.plain(l) for l in self.render("blank") if l not in self.render("none")
        ]
        spacer = [l for l in added if not l.strip("│ ")]
        self.assertEqual(len(spacer), 1)
        self.assertTrue(spacer[0].startswith("│") and spacer[0].endswith("│"))

    def test_a_rule_separator_reuses_the_header_rule(self):
        lines = [view.plain(l) for l in self.render("rule")]
        self.assertEqual(lines.count(lines[2]), 2, "the mid rule should appear twice")

    def test_a_coloured_run_keeps_the_dim_borders_on_the_spacer(self):
        """Blanking a rendered rule would have taken its escapes with it, so
        the spacer is built from the resolved widths instead."""
        added = [l for l in self.render("blank", colour=True) if not view.plain(l).strip("│ ")]
        self.assertEqual(len(added), 1)
        self.assertIn(view.STYLES["dim"], added[0])

    def test_one_line_tall_rows_default_to_no_separator(self):
        """`render_runs` and the header tables pass no separator at all."""
        lines = view.render_table(
            [view.Column("A")], [[("x", None)], [("y", None)]],
            view.Palette(False), 20, view.BOX_UNICODE,
        )
        self.assertEqual(len(lines), 6)

    def test_the_flag_names_map_to_what_render_table_takes(self):
        self.assertEqual(view.row_separator("spaced"), "blank")
        self.assertEqual(view.row_separator("ruled"), "rule")
        self.assertEqual(view.row_separator("compact"), "none")
        self.assertEqual(view.row_separator("nonsense"), "none")


class TestColourSelection(unittest.TestCase):
    def test_explicit_flags_win(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertTrue(view.want_colour("always"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(view.want_colour("never"))

    def test_no_color_beats_a_tty(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}, clear=True):
            self.assertFalse(view.want_colour("auto", tty))

    def test_dumb_terminal_is_not_coloured(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            self.assertFalse(view.want_colour("auto", tty))

    def test_auto_follows_the_tty(self):
        for isatty, expected in ((True, True), (False, False)):
            stream = mock.Mock()
            stream.isatty.return_value = isatty
            with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
                self.assertIs(view.want_colour("auto", stream), expected)

    def test_a_disabled_palette_emits_no_escapes(self):
        palette = view.Palette(False)
        self.assertEqual(palette("text", "red"), "text")
        self.assertEqual(view.hyperlink("text", "https://example.com", palette), "text")


class TestFormatting(unittest.TestCase):
    def test_pr_ref_shortens_a_github_pull_request(self):
        self.assertEqual(
            view.pr_ref("https://github.com/gke-agentic/kube-agents/pull/160"),
            "gke-agentic/kube-agents#160",
        )

    def test_pr_ref_tolerates_a_trailing_slash(self):
        self.assertEqual(view.pr_ref("https://github.com/o/r/pull/1/"), "o/r#1")

    def test_pr_ref_leaves_anything_else_alone(self):
        for other in ("https://github.com/o/r/issues/5", "not a url", ""):
            self.assertEqual(view.pr_ref(other), other)

    def test_parse_iso_accepts_the_ledgers_own_format(self):
        self.assertEqual(view.parse_iso("2026-08-23T17:07:51Z").year, 2026)

    def test_parse_iso_assumes_utc_for_a_naive_stamp(self):
        self.assertEqual(view.parse_iso("2026-08-23T17:07:51").tzinfo, _dt.timezone.utc)

    def test_parse_iso_returns_none_rather_than_raising(self):
        for junk in ("", "   ", "yesterday", None, 17, {}):
            self.assertIsNone(view.parse_iso(junk))

    def test_humanise_delta_scales(self):
        self.assertEqual(view.humanise_delta(45), "45s")
        self.assertEqual(view.humanise_delta(600), "10m")
        self.assertEqual(view.humanise_delta(3600), "1h")
        self.assertEqual(view.humanise_delta(3600 + 120), "1h02m")
        self.assertEqual(view.humanise_delta(86400 * 2), "2d")
        self.assertEqual(view.humanise_delta(86400 * 2 + 3600 * 5), "2d5h")

    def test_ago_reads_forwards_and_backwards(self):
        self.assertEqual(view.ago(NOW - _dt.timedelta(hours=2), NOW), "2h ago")
        self.assertEqual(view.ago(NOW + _dt.timedelta(minutes=30), NOW), "in 30m")
        self.assertEqual(view.ago(None, NOW), "never")

    def test_stamp_in_utc_is_stable_regardless_of_the_readers_zone(self):
        self.assertEqual(view.stamp(view.parse_iso("2026-08-23T17:07:51Z"), True), "2026-08-23 17:07 UTC")
        self.assertEqual(view.stamp(None, True), "-")

    def test_stamp_local_is_lower_case_am_pm(self):
        rendered = view.stamp(view.parse_iso("2026-08-23T17:07:51Z"), False)
        self.assertNotIn("AM", rendered)
        self.assertNotIn("PM", rendered)
        self.assertTrue(rendered.startswith("2026-08-2"))

    def test_compact_count_shortens_the_big_ones(self):
        self.assertEqual(view.compact_count(9), "9")
        self.assertEqual(view.compact_count(999), "999")
        self.assertEqual(view.compact_count(6400), "6.4k")
        self.assertEqual(view.compact_count(2_500_000), "2.5M")

    def test_clip_only_shortens_what_is_too_long(self):
        self.assertEqual(view.clip("short", 20), "short")
        clipped = view.clip("x" * 50, 10)
        self.assertEqual(len(clipped), 10)
        self.assertTrue(clipped.endswith("…"))

    def test_meter_is_always_the_requested_width(self):
        for fraction in (-1.0, 0.0, 0.33, 1.0, 4.2):
            self.assertEqual(len(view.meter(fraction, 18)), 18)

    def test_short_rev_handles_a_missing_revision(self):
        self.assertEqual(view.short_rev("aa3b7aa1111111"), "aa3b7aa")
        self.assertEqual(view.short_rev(None), "-")
        self.assertEqual(view.short_rev(""), "-")


class TestLoading(unittest.TestCase):
    def test_reads_a_bare_ledger(self):
        with ledger_file(ledger()) as path:
            document, raw = view.load_from_file(path)
        self.assertEqual(len(document["findings"]), 3)
        self.assertIn("findings", raw)

    def test_reads_a_whole_configmap(self):
        """`kubectl get cm -o json > x.json` is the shorter command, so it is
        the likelier thing to find in a file."""
        wrapped = {
            "kind": "ConfigMap",
            "metadata": {"name": view.DEFAULT_CONFIGMAP},
            "data": {view.LEDGER_KEY: json.dumps(ledger())},
        }
        with ledger_file(wrapped) as path:
            document, raw = view.load_from_file(path)
        self.assertEqual(len(document["findings"]), 3)
        # The raw text is the inner ledger, not the envelope: the size meter
        # measures the ledger against LEDGER_MAX_BYTES, and measuring the
        # ConfigMap's own JSON would overstate it.
        self.assertNotIn("ConfigMap", raw)

    def test_a_configmap_without_the_ledger_key_is_not_mistaken_for_one(self):
        with ledger_file({"data": {"other.json": "{}"}}) as path:
            document, _ = view.load_from_file(path)
        self.assertNotIn("findings", document)

    def test_cronjob_env_flattens_the_container_spec(self):
        cronjob = {
            "spec": {
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "env": [
                                            {"name": "SELFIMPROVE_MODE", "value": "fork"},
                                            {"name": "FROM_SECRET", "valueFrom": {}},
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
        env = view.cronjob_env(cronjob)
        self.assertEqual(env["SELFIMPROVE_MODE"], "fork")
        self.assertNotIn("FROM_SECRET", env)

    def test_cronjob_env_tolerates_a_shape_it_does_not_recognise(self):
        for junk in (None, {}, {"spec": {}}, {"spec": {"jobTemplate": "nonsense"}}):
            self.assertEqual(view.cronjob_env(junk), {})

    def test_parse_gate_tolerates_missing_and_malformed_json(self):
        self.assertEqual(view.parse_gate({}), {})
        self.assertEqual(view.parse_gate({"SELFIMPROVE_GATE": "not json"}), {})
        self.assertEqual(view.parse_gate({"SELFIMPROVE_GATE": "[1,2]"}), {})
        self.assertEqual(view.parse_gate({"SELFIMPROVE_GATE": json.dumps(GATE)}), GATE)

    def test_kubectl_missing_is_an_error_that_names_the_way_out(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(view.LoadError) as caught:
                view.kubectl_json(["get", "cm"], None)
        self.assertIn("--file", str(caught.exception))

    def test_a_configmap_without_the_key_says_the_loop_has_not_run(self):
        with mock.patch.object(view, "kubectl_json", return_value={"data": {}}):
            with self.assertRaises(view.LoadError) as caught:
                view.load_from_cluster("ns", "cm", None)
        self.assertIn("has not completed a run", str(caught.exception))

    def test_a_missing_cronjob_is_not_an_error(self):
        with mock.patch.object(view, "kubectl_json", side_effect=view.LoadError("nope")):
            self.assertIsNone(view.load_cronjob("ns", "cj", None))


class TestReusesTheLoopsOwnMaths(unittest.TestCase):
    """The counts and the gate come from `selfimprove_ledger`, not from here.

    Reimplementing either would give the viewer a second opinion about the same
    ledger, and the two would drift the first time either changed.
    """

    def setUp(self):
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")

    def test_occurrences_counts_runs_and_reported_counts_claims(self):
        entry = finding("dddd", "medium", "t")
        self.assertEqual(view.occurrences(entry, NOW), 2)
        self.assertEqual(view.reported(entry, NOW), 9)

    def test_a_sighting_outside_the_window_stops_counting(self):
        entry = finding("dddd", "medium", "t", sightings=[{"at": iso(48), "count": 3}])
        self.assertEqual(view.occurrences(entry, NOW), 0)

    def test_gate_verdicts_cover_every_finding(self):
        verdicts = view.gate_verdicts(ledger(), GATE, NOW)
        self.assertEqual(set(verdicts), set(ledger()["findings"]))

    def test_the_cooldown_holds_a_recently_promoted_finding(self):
        verdicts = view.gate_verdicts(ledger(), GATE, NOW)
        self.assertIn("cooldown", verdicts["bbbb111111111111"])

    def test_a_severity_with_no_rule_is_held_and_says_so(self):
        verdicts = view.gate_verdicts(ledger(), GATE, NOW)
        self.assertIn("no promotion rule", verdicts["cccc222222222222"])

    def test_a_finding_that_clears_its_rule_is_promoted(self):
        verdicts = view.gate_verdicts(ledger(), GATE, NOW)
        self.assertTrue(verdicts["aaaa000000000000"].startswith("promoted"))

    def test_no_gate_means_no_verdicts_rather_than_a_guess(self):
        self.assertEqual(view.gate_verdicts(ledger(), {}, NOW), {})

    def test_a_malformed_gate_holds_everything_rather_than_taking_the_report_down(self):
        """`rules` is a string, so no rule can match anything.

        Every finding gets a verdict saying so. The distinction worth keeping is
        against `{}`, which is what an *absent* gate returns and which renders no
        verdict column at all -- a malformed one is a configuration error the
        reader should see, not a gate that isn't there.
        """
        verdicts = view.gate_verdicts(ledger(), {"rules": "not a list"}, NOW)
        self.assertEqual(sorted(verdicts), ["aaaa000000000000", "bbbb111111111111", "cccc222222222222"])
        self.assertTrue(all(v.startswith("held: no promotion rule") for v in verdicts.values()))

    def test_verdict_styles_separate_the_three_outcomes(self):
        self.assertEqual(view.verdict_style("promoted: medium at 2 occurrence(s)"), "green")
        self.assertEqual(view.verdict_style("held: the filing turn refused this permanently (x)"), "magenta")
        self.assertEqual(view.verdict_style("held: the day's budget is spent"), "yellow")

    def test_counts_degrade_to_none_when_the_module_is_absent(self):
        with mock.patch.object(view, "ledger_mod", None):
            self.assertIsNone(view.occurrences(finding("d", "low", "t"), NOW))
            self.assertIsNone(view.reported(finding("d", "low", "t"), NOW))
            self.assertEqual(view.gate_verdicts(ledger(), GATE, NOW), {})


class TestFindingSelection(unittest.TestCase):
    def render(self, document=None, sort="severity", severity=None, signal=None):
        document = document if document is not None else ledger()
        return view.render_findings(
            document, {}, NOW, view.Palette(False), 160, view.BOX_UNICODE, sort, severity, signal
        )

    def test_worst_first_by_default(self):
        _, entries = self.render()
        self.assertEqual([e["severity"] for e in entries], ["critical", "medium", "low"])

    def test_sorting_by_last_seen_puts_the_freshest_first(self):
        document = ledger()
        document["findings"]["cccc222222222222"]["last_seen"] = iso(0)
        document["findings"]["aaaa000000000000"]["last_seen"] = iso(9)
        _, entries = self.render(document, sort="last")
        self.assertEqual(entries[0]["fingerprint"], "cccc222222222222")

    def test_sorting_by_first_seen_puts_the_oldest_first(self):
        document = ledger()
        document["findings"]["cccc222222222222"]["first_seen"] = iso(99)
        _, entries = self.render(document, sort="first")
        self.assertEqual(entries[0]["fingerprint"], "cccc222222222222")

    def test_a_severity_floor_hides_everything_below_it(self):
        _, entries = self.render(severity="medium")
        self.assertEqual([e["severity"] for e in entries], ["critical", "medium"])

    def test_a_signal_filter_is_case_insensitive(self):
        _, entries = self.render(signal="LATENCY")
        self.assertEqual([e["fingerprint"] for e in entries], ["bbbb111111111111"])

    def test_a_filter_that_matches_nothing_says_so_rather_than_drawing_an_empty_table(self):
        lines, entries = self.render(signal="nonexistent")
        self.assertEqual(entries, [])
        self.assertIn("no findings match", "".join(lines))

    def test_an_unknown_severity_sorts_last_rather_than_raising(self):
        document = ledger()
        document["findings"]["aaaa000000000000"]["severity"] = "catastrophic"
        _, entries = self.render(document)
        self.assertEqual(entries[-1]["fingerprint"], "aaaa000000000000")

    def test_a_severity_floor_of_low_hides_nothing_at_all(self):
        """`--severity` is documented as a floor and `low` is the bottom of the
        scale, so `--severity low` has to drop nothing. An unrecognised severity
        ranked past `low` for the sort, the filter reused that rank, and the
        findings nobody can triage at a glance were the ones it silently hid."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["severity"] = "catastrophic"
        _, entries = self.render(document, severity="low")
        self.assertEqual(len(entries), 3)

    def test_an_unknown_severity_filters_as_the_bottom_of_the_scale(self):
        """Ranked with `low` for the floor, and still last in the sort: the two
        are different questions about the same unrecognised word."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["severity"] = "catastrophic"
        _, kept = self.render(document, severity="low")
        _, dropped = self.render(document, severity="medium")
        self.assertIn("aaaa000000000000", [e["fingerprint"] for e in kept])
        self.assertNotIn("aaaa000000000000", [e["fingerprint"] for e in dropped])

    def test_a_permanent_refusal_shows_with_no_gate_to_replay(self):
        """`refused` is a decision a filing turn already made and wrote into the
        ledger, not a simulation of one -- and there is no gate to replay under
        `--file`, under `--no-cronjob`, or on an install whose CronJob has been
        removed. A row that omits it reads as an ordinary live finding the loop
        is still working on."""
        document = ledger()
        document["findings"]["cccc222222222222"]["refused"] = {
            "at": iso(2),
            "reason": "touches the gate",
            "revision": "abc1234",
        }
        text = "".join(self.render(document)[0])
        self.assertIn("refused permanently", text)
        self.assertIn("touches the gate", text)

    def test_a_refusal_with_no_reason_recorded_still_says_it_was_refused(self):
        document = ledger()
        document["findings"]["cccc222222222222"]["refused"] = {"at": iso(2)}
        self.assertIn("no reason recorded", "".join(self.render(document)[0]))

    def test_a_gate_verdict_is_not_displaced_by_the_intrinsic_one(self):
        """Where there is a gate, its wording is the richer of the two -- it
        knows why the finding is held, not only that it was once refused."""
        document = ledger()
        document["findings"]["cccc222222222222"]["refused"] = {"at": iso(2), "reason": "the gate"}
        lines, _ = view.render_findings(
            document,
            {"cccc222222222222": "held: no promotion rule for low"},
            NOW,
            view.Palette(False),
            160,
            view.BOX_UNICODE,
            "severity",
            None,
            None,
        )
        self.assertIn("no promotion rule", "".join(lines))
        self.assertNotIn("refused permanently", "".join(lines))

    def test_findings_may_be_a_list_as_well_as_a_dict(self):
        document = ledger(findings=list(ledger()["findings"].values()))
        self.assertEqual(len(view.sorted_findings(document)), 3)

    def test_match_finding_takes_a_row_number(self):
        _, entries = self.render()
        self.assertIs(view.match_finding(entries, "2"), entries[1])

    def test_match_finding_takes_a_fingerprint_prefix(self):
        _, entries = self.render()
        self.assertEqual(view.match_finding(entries, "cccc")["fingerprint"], "cccc222222222222")

    def test_match_finding_is_case_insensitive_on_the_fingerprint(self):
        _, entries = self.render()
        self.assertEqual(view.match_finding(entries, "CCCC2")["fingerprint"], "cccc222222222222")

    def test_match_finding_refuses_an_ambiguous_prefix(self):
        entries = [finding("ab11", "low", "one"), finding("ab22", "low", "two")]
        self.assertIsNone(view.match_finding(entries, "ab"))

    def test_match_finding_rejects_a_row_number_out_of_range(self):
        _, entries = self.render()
        self.assertIsNone(view.match_finding(entries, "0"))
        self.assertIsNone(view.match_finding(entries, "99"))


class TestPromotions(unittest.TestCase):
    def test_every_promotion_is_collected_newest_first(self):
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = [
            {"at": iso(20), "url": "https://github.com/o/r/pull/1"},
            {"at": iso(0), "url": "https://github.com/o/r/pull/9"},
        ]
        pairs = view.collect_promotions(view.sorted_findings(document))
        self.assertEqual([p["url"].rsplit("/", 1)[-1] for p, _ in pairs], ["9", "160", "1"])

    def test_a_promotion_without_a_url_is_kept_and_labelled(self):
        """`record_promotion(confirmed=False)`: a filing turn that charged the
        budget without printing a link is precisely the row somebody has to go
        and look for by hand, so hiding it would hide the problem."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = [{"at": iso(1), "unconfirmed": True}]
        pairs = view.collect_promotions(view.sorted_findings(document))
        lines = view.render_promotions(pairs, NOW, view.Palette(False), 160, view.BOX_UNICODE, True)
        text = "".join(lines)
        self.assertIn("no URL recorded", text)
        self.assertIn("unconfirmed", text)

    def test_promotions_are_ordered_on_the_instant_not_on_the_text(self):
        """`to_iso` writes `...Z`, which happens to sort correctly as a string;
        the same instant written `+00:00` by whoever last edited the ConfigMap
        does not. `19:00:00-01:00` is an hour after `19:00:00Z` and sorts an hour
        before it, because `-` is below `Z`."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = [
            {"at": "2026-08-23T19:00:00Z", "url": "https://github.com/o/r/pull/1"}
        ]
        document["findings"]["cccc222222222222"]["promotions"] = [
            {"at": "2026-08-23T19:00:00-01:00", "url": "https://github.com/o/r/pull/2"}
        ]
        pairs = view.collect_promotions(view.sorted_findings(document))
        self.assertEqual([p["url"].rsplit("/", 1)[-1] for p, _ in pairs], ["2", "1", "160"])

    def test_a_promotion_nobody_can_date_sorts_last_rather_than_raising(self):
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = [
            {"at": "whenever", "url": "https://github.com/o/r/pull/1"}
        ]
        pairs = view.collect_promotions(view.sorted_findings(document))
        self.assertEqual([p["url"].rsplit("/", 1)[-1] for p, _ in pairs], ["160", "1"])

    def test_a_scalar_promotions_field_is_skipped_rather_than_iterated(self):
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = "none"
        pairs = view.collect_promotions(view.sorted_findings(document))
        self.assertEqual([p["url"].rsplit("/", 1)[-1] for p, _ in pairs], ["160"])

    def test_the_promotion_table_stays_aligned(self):
        pairs = view.collect_promotions(view.sorted_findings(ledger()))
        lines = view.render_promotions(pairs, NOW, view.Palette(True), 120, view.BOX_UNICODE, True)
        self.assertEqual(len({len(view.plain(line)) for line in lines}), 1)


class TestHeader(unittest.TestCase):
    def header(self, document=None, **kwargs):
        document = document if document is not None else ledger()
        raw = json.dumps(document)
        defaults = dict(
            source="file",
            namespace="kubeagents-system",
            name=view.DEFAULT_CONFIGMAP,
            cronjob=None,
            env={},
            gate={},
            utc=True,
        )
        defaults.update(kwargs)
        return view.render_header(
            document,
            raw,
            defaults["source"],
            defaults["namespace"],
            defaults["name"],
            defaults["cronjob"],
            defaults["env"],
            defaults["gate"],
            NOW,
            view.Palette(False),
            defaults["utc"],
        )

    def test_the_first_line_leads_with_the_last_run_and_the_run_count(self):
        """The two questions anyone opens the ledger with, in the first place
        the eye lands. Asserted rather than left to drift because it is the
        one piece of the layout that was specified."""
        lead = self.header()[0]
        self.assertTrue(lead.startswith("last run"))
        self.assertIn("2026-08-23 19:00 UTC", lead)
        self.assertIn("1h ago", lead)
        self.assertIn("2 runs recorded", lead)

    def test_the_lead_line_reports_the_last_runs_outcome(self):
        document = ledger()
        document["runs"][-1]["outcome"] = "killed"
        self.assertIn("killed", self.header(document)[0])

    def test_a_ledger_with_no_runs_says_never_rather_than_a_dash(self):
        lead = self.header(ledger(runs=[]))[0]
        self.assertIn("never", lead)
        self.assertIn("0 runs recorded", lead)

    def test_one_run_is_singular(self):
        document = ledger()
        document["runs"] = document["runs"][:1]
        self.assertIn("1 run recorded", self.header(document)[0])

    def test_the_pull_request_count_does_not_claim_to_be_all_time(self):
        """`prune` deletes a finding a month after its last sighting and keeps
        only the ten most recent promotions on the ones that survive, so the
        number goes down as well as up and an install filing for a year reports
        a fraction of it. What it counts is what the document still holds."""
        text = "\n".join(self.header())
        self.assertIn("1 pull request(s) the ledger still lists", text)
        self.assertNotIn("all time", text)

    def test_a_promotion_with_no_url_is_counted_out_loud(self):
        """`record_promotion(confirmed=False)` writes a record for a filing turn
        that charged the budget without printing a link, so a promotion record is
        not by itself evidence of a pull request."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = [
            {"at": iso(1), "unconfirmed": True}
        ]
        self.assertIn("(1 unconfirmed)", "\n".join(self.header(document)))

    def test_a_file_source_does_not_claim_a_configmap(self):
        self.assertNotIn("configmap", "\n".join(self.header()))

    def test_a_cluster_source_names_the_configmap(self):
        text = "\n".join(self.header(source="gke_p_r_c"))
        self.assertIn("kubeagents-system/%s" % view.DEFAULT_CONFIGMAP, text)

    def test_report_only_mode_does_not_name_a_target_repository(self):
        text = "\n".join(
            self.header(env={"SELFIMPROVE_MODE": "report-only", "SELFIMPROVE_FORK_REPO": "o/r"})
        )
        self.assertIn("report-only", text)
        self.assertNotIn("o/r", text)

    def test_fork_mode_names_the_target_and_the_base(self):
        text = "\n".join(
            self.header(
                env={
                    "SELFIMPROVE_MODE": "fork",
                    "SELFIMPROVE_FORK_REPO": "gke-agentic/kube-agents",
                    "SELFIMPROVE_BASE_BRANCH": "self-improvement-live",
                }
            )
        )
        self.assertIn("gke-agentic/kube-agents", text)
        self.assertIn("self-improvement-live", text)

    def test_a_suspended_cronjob_is_called_out(self):
        cronjob = {"spec": {"schedule": "0 * * * *", "suspend": True}, "status": {}}
        text = "\n".join(self.header(cronjob=cronjob))
        self.assertIn("SUSPENDED", text)

    def test_an_active_cronjob_shows_its_schedule(self):
        cronjob = {
            "spec": {"schedule": "0 * * * *"},
            "status": {"lastScheduleTime": iso(0.05)},
        }
        text = "\n".join(self.header(cronjob=cronjob))
        self.assertIn("0 * * * *", text)
        self.assertIn("active", text)

    def test_the_budget_line_counts_the_promotions_in_the_window(self):
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        text = "\n".join(self.header(gate=GATE))
        self.assertIn("1 of 3 pull requests", text)
        self.assertIn("24h cooldown", text)

    def test_the_budget_line_reports_what_the_gate_will_enforce(self):
        """The gate's own reading of its own numbers, not the raw ones. YAML
        spells infinity `.inf` and both knobs have an intent an operator might
        reach for it to express, so the gate clamps rather than rejects -- and a
        header printing the raw value told a maintainer nothing would ever
        re-file while the install re-filed every day."""
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        text = "\n".join(
            self.header(
                gate={"maxPullRequestsPerDay": float("inf"), "cooldownHours": float("inf")}
            )
        )
        self.assertIn("1 of %d pull requests" % view.ledger_mod.MAX_GATE_COUNT, text)
        self.assertIn("%gh cooldown" % view.ledger_mod.COUNT_WINDOW_HOURS, text)

    def test_a_quoted_budget_renders_rather_than_taking_the_report_down(self):
        """Comparing the spend against `"3"` raised TypeError out of the header,
        which lost the whole report to a quoting mistake in the gate."""
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        text = "\n".join(self.header(gate={"maxPullRequestsPerDay": "3", "cooldownHours": "24"}))
        self.assertIn("1 of 3 pull requests", text)

    def test_a_spent_budget_is_reported_as_spent(self):
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        text = "\n".join(self.header(gate={"maxPullRequestsPerDay": 1, "cooldownHours": 24}))
        self.assertIn("1 of 1 pull requests", text)

    def test_the_size_meter_measures_the_ledger_against_the_cap(self):
        text = "\n".join(self.header())
        self.assertIn("of 768 KiB", text)


class TestRuns(unittest.TestCase):
    def render(self, document=None, limit=10):
        document = document if document is not None else ledger()
        return view.render_runs(document, limit, NOW, view.Palette(True), 140, view.BOX_UNICODE, True)

    def test_newest_run_first(self):
        rows = [view.plain(line) for line in self.render()]
        body = [r for r in rows if "2026-08-23" in r]
        self.assertIn("19:00 UTC", body[0])
        self.assertIn("17:00 UTC", body[1])

    def test_the_table_stays_aligned(self):
        self.assertEqual(len({len(view.plain(line)) for line in self.render()}), 1)

    def test_an_empty_history_says_so_rather_than_drawing_a_table(self):
        self.assertIn("no runs recorded yet", "".join(self.render(ledger(runs=[]))))

    def test_a_limit_shows_the_newest_and_says_how_many_it_hid(self):
        document = ledger(runs=[dict(ledger()["runs"][0], at=iso(h)) for h in range(20)])
        lines = self.render(document, limit=3)
        self.assertIn("17 older run(s) not shown", "".join(lines))

    def test_zero_means_all(self):
        document = ledger(runs=[dict(ledger()["runs"][0], at=iso(h)) for h in range(20)])
        self.assertNotIn("older run(s)", "".join(self.render(document, limit=0)))

    def test_an_unknown_outcome_is_not_styled_as_a_success(self):
        document = ledger()
        document["runs"][-1]["outcome"] = "something-new"
        joined = "".join(self.render(document))
        self.assertIn(view.STYLES["yellow"] + "something-new", joined)


class TestDetail(unittest.TestCase):
    def detail(self, entry, verdict=""):
        return "\n".join(view.render_detail(entry, verdict, NOW, view.Palette(False), 100, True))

    def test_shows_the_fields_the_table_has_to_leave_out(self):
        text = self.detail(finding("aaaa", "high", "A title"))
        for expected in ("a summary", "some evidence", "a fix", "an impact", "aaaa"):
            self.assertIn(expected, text)

    def test_shows_the_full_location_the_table_clips(self):
        long_location = "some/very/long/path.go:10 and " + "x" * 300
        text = self.detail(finding("aaaa", "high", "t", location=long_location))
        self.assertIn("x" * 40, text)

    def test_shows_every_pull_request(self):
        entry = finding(
            "aaaa",
            "high",
            "t",
            promotions=[
                {"at": iso(5), "url": "https://github.com/o/r/pull/1"},
                {"at": iso(1), "url": "https://github.com/o/r/pull/2"},
            ],
        )
        text = self.detail(entry)
        self.assertIn("pull/1", text)
        self.assertIn("pull/2", text)

    def test_shows_a_permanent_refusal_and_its_reason(self):
        entry = finding(
            "aaaa", "high", "t", refused={"at": iso(2), "reason": "touches the gate", "revision": "abc1234def"}
        )
        text = self.detail(entry)
        self.assertIn("touches the gate", text)
        self.assertIn("abc1234", text)

    def test_omits_a_block_it_has_nothing_for(self):
        entry = finding("aaaa", "high", "t", evidence="", proposed_fix="")
        text = self.detail(entry)
        self.assertNotIn("evidence", text)
        self.assertNotIn("proposed fix", text)


#: Stands in for the repository's top-level entries. Real runs derive this from
#: the checkout the script ships in; pinning it here keeps the tests from
#: changing meaning the next time a top-level directory is added or removed.
ROOTS = frozenset({"agents", "k8s-operator", "charts", "docs", "scripts", "images.json"})
REPO = "gke-agentic/kube-agents"


class TestLocationParsing(unittest.TestCase):
    def refs(self, location, roots=ROOTS):
        return view.location_refs(location, roots)

    def test_a_bare_path_and_line(self):
        self.assertEqual(
            self.refs("k8s-operator/cmd/main.go:108"), [("k8s-operator/cmd/main.go", "108")]
        )

    def test_a_path_with_no_line(self):
        self.assertEqual(self.refs("images.json"), [("images.json", None)])

    def test_a_second_reference_given_as_a_bare_line_number(self):
        """The live ledger writes `...controller.go:1090 (...) and :1162 (...)`,
        so a bare line number attaches to the path before it."""
        location = "k8s-operator/internal/controller/platformagent_controller.go:1090 (a) and :1162 (b)"
        self.assertEqual(
            self.refs(location),
            [
                ("k8s-operator/internal/controller/platformagent_controller.go", "1090"),
                ("k8s-operator/internal/controller/platformagent_controller.go", "1162"),
            ],
        )

    def test_code_in_the_prose_does_not_detach_a_following_bare_line(self):
        """Regression: the backticked `r.Status().Update(...)` between the two
        references parses as a dotted path, and treating it as a foreign one
        cost `:1162` its link."""
        location = (
            "k8s-operator/internal/controller/platformagent_controller.go:1090 "
            "(`return newPhase, r.Status().Update(ctx, agent)`) and :1162 (the other)"
        )
        self.assertEqual(len(self.refs(location)), 2)

    def test_a_real_path_in_another_repository_does_detach_it(self):
        self.assertEqual(self.refs("agent/foo.py:10 and :20"), [])

    def test_a_line_range_is_kept_whole(self):
        self.assertEqual(
            self.refs("charts/kube-agents/templates/self-improvement.yaml:611-612"),
            [("charts/kube-agents/templates/self-improvement.yaml", "611-612")],
        )

    def test_another_repositorys_paths_are_not_linked(self):
        """`agent/anthropic_adapter.py` is the Hermes harness. A kube-agents
        blob URL for it 404s, which reads as a stale finding rather than a bad
        link."""
        self.assertEqual(self.refs("agent/anthropic_adapter.py:136 (_is_claude_model)"), [])

    def test_prose_that_only_looks_like_a_path_is_rejected(self):
        for text in ("e.g. the handler", "hermes v2026.8.13 took 1.5s", "see Note: 4"):
            self.assertEqual(self.refs(text), [], text)

    def test_a_url_in_the_prose_is_not_mined_for_paths(self):
        self.assertEqual(self.refs("see https://github.com/o/r/blob/main/x.py here"), [])

    def test_repeats_are_collapsed(self):
        self.assertEqual(len(self.refs("docs/a.md:1 and docs/a.md:1")), 1)

    def test_no_roots_means_no_references(self):
        self.assertEqual(self.refs("k8s-operator/cmd/main.go:108", frozenset()), [])


class TestBlobUrls(unittest.TestCase):
    def test_pins_the_revision_the_finding_was_made_against(self):
        self.assertEqual(
            view.blob_url(REPO, "abc123", "k8s-operator/cmd/main.go", "108"),
            "https://github.com/gke-agentic/kube-agents/blob/abc123/k8s-operator/cmd/main.go#L108",
        )

    def test_a_range_repeats_the_L_the_way_github_spells_it(self):
        self.assertTrue(view.blob_url(REPO, "abc", "x.py", "48-54").endswith("#L48-L54"))

    def test_no_line_means_no_anchor(self):
        self.assertTrue(view.blob_url(REPO, "abc", "x.py").endswith("/x.py"))

    def test_a_rendered_document_asks_for_the_source_view(self):
        """GitHub's rendered Markdown has no gutter, so `#L12` lands nowhere."""
        self.assertEqual(
            view.blob_url(REPO, "abc123", "docs/designs/self-improvement.md", "12"),
            "https://github.com/gke-agentic/kube-agents/blob/abc123"
            "/docs/designs/self-improvement.md?plain=1#L12",
        )
        self.assertTrue(
            view.blob_url(REPO, "abc", "a/b/SKILL.MD", "3").endswith("?plain=1#L3")
        )
        self.assertTrue(
            view.blob_url(REPO, "abc", "notes.rst", "4-9").endswith("?plain=1#L4-L9")
        )

    def test_source_files_are_left_alone(self):
        for path in ("x.py", "k8s-operator/cmd/main.go", "a.yaml", "Makefile"):
            with self.subTest(path=path):
                self.assertNotIn("plain=1", view.blob_url(REPO, "abc", path, "1"))

    def test_a_document_with_no_line_keeps_the_rendered_preview(self):
        """Nothing to anchor to, so the readable page is the better landing."""
        self.assertEqual(
            view.blob_url(REPO, "abc", "docs/README.md"),
            "https://github.com/gke-agentic/kube-agents/blob/abc/docs/README.md",
        )

    def test_the_query_precedes_the_fragment(self):
        """`#L12?plain=1` would make the anchor part of the fragment and die."""
        url = view.blob_url(REPO, "abc", "x.md", "12")
        self.assertLess(url.index("?plain=1"), url.index("#L12"))

    def test_a_missing_ingredient_yields_no_url(self):
        self.assertEqual(view.blob_url("", "abc", "x.py", "1"), "")
        self.assertEqual(view.blob_url(REPO, "", "x.py", "1"), "")
        self.assertEqual(view.blob_url(REPO, "abc", "", "1"), "")

    def test_a_finding_with_no_revision_is_not_linked(self):
        entry = finding("aaaa", "high", "t", revision="")
        self.assertEqual(view.location_links(entry, REPO, ROOTS), [])

    def test_a_traversal_in_the_revision_is_not_linked(self):
        """A browser resolves the `..` away, so the link leaves the repository
        while the label beside it goes on naming a file inside it: the reader
        sees `agents/x.py:12` over a link to somebody else's repository."""
        self.assertEqual(
            view.blob_url(REPO, "../../../../attacker/repo/blob/main", "agents/x.py", "12"), ""
        )

    def test_a_traversal_in_the_path_is_not_linked(self):
        """The path is cut out of a location string the investigating agent
        wrote, which is prose derived from production logs."""
        self.assertEqual(view.blob_url(REPO, "abc123", "../../attacker/x.py", "1"), "")
        self.assertEqual(view.blob_url(REPO, "abc123", "agents/../../x.py", "1"), "")

    def test_a_repo_that_is_not_two_plain_segments_is_not_linked(self):
        for bad in ("o", "o/r/extra", "o/r#x", "o/r?x=1", "https://evil.example/o/r", "o/.."):
            self.assertEqual(view.blob_url(bad, "abc", "x.py", "1"), "", bad)

    def test_a_branch_name_is_still_a_revision_the_loop_writes(self):
        """The runner falls back to a bare ref when it cannot resolve a SHA and
        marks a dirty tree `-dirty`, so a hex-only rule would drop both and take
        every link on those runs with it."""
        self.assertTrue(view.blob_url(REPO, "main", "x.py", "1").endswith("/blob/main/x.py#L1"))
        self.assertIn("/blob/abc123-dirty/", view.blob_url(REPO, "abc123-dirty", "x.py", "1"))

    def test_an_anchor_that_is_not_a_line_number_is_dropped_rather_than_appended(self):
        url = view.blob_url(REPO, "abc123", "x.py", "1 onerror=alert(1)")
        self.assertTrue(url.endswith("/x.py"), url)


class TestRepoToplevel(unittest.TestCase):
    def test_derives_the_set_from_the_checkout_this_script_ships_in(self):
        roots = view.repo_toplevel()
        self.assertIn("k8s-operator", roots)
        self.assertIn("agents", roots)
        self.assertNotIn(".git", roots)
        self.assertNotIn("agent", roots)

    def test_a_directory_that_is_not_a_checkout_switches_linking_off(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(view.repo_toplevel(empty), frozenset())


class TestTargetRepo(unittest.TestCase):
    def test_the_source_repository_wins_over_the_push_target(self):
        """A blob URL has to resolve, which is what `SELFIMPROVE_SOURCE_REPO`
        names: the repository the runner fetched the stamped revision from."""
        env = {
            "SELFIMPROVE_MODE": "upstream",
            "SELFIMPROVE_SOURCE_REPO": "src/repo",
            "SELFIMPROVE_FORK_REPO": "fork/repo",
            "SELFIMPROVE_UPSTREAM_REPO": "up/repo",
        }
        self.assertEqual(view.target_repo(env), "src/repo")

    def test_the_source_repository_wins_under_report_only_too(self):
        env = {
            "SELFIMPROVE_MODE": "report-only",
            "SELFIMPROVE_SOURCE_REPO": "src/repo",
            "SELFIMPROVE_UPSTREAM_REPO": "up/repo",
        }
        self.assertEqual(view.target_repo(env), "src/repo")

    def test_a_blank_source_repository_falls_back(self):
        """An install whose CronJob predates the variable renders it empty
        rather than omitting it, and "" is not an answer."""
        env = {
            "SELFIMPROVE_MODE": "fork",
            "SELFIMPROVE_SOURCE_REPO": "",
            "SELFIMPROVE_FORK_REPO": "fork/repo",
            "SELFIMPROVE_UPSTREAM_REPO": "up/repo",
        }
        self.assertEqual(view.target_repo(env), "fork/repo")

    def test_fork_mode_resolves_against_the_fork(self):
        env = {
            "SELFIMPROVE_MODE": "fork",
            "SELFIMPROVE_FORK_REPO": "fork/repo",
            "SELFIMPROVE_UPSTREAM_REPO": "up/repo",
        }
        self.assertEqual(view.target_repo(env), "fork/repo")

    def test_report_only_resolves_against_upstream(self):
        """Nothing is pushed to a fork under report-only, so the revision is
        only findable upstream."""
        env = {
            "SELFIMPROVE_MODE": "report-only",
            "SELFIMPROVE_FORK_REPO": "fork/repo",
            "SELFIMPROVE_UPSTREAM_REPO": "up/repo",
        }
        self.assertEqual(view.target_repo(env), "up/repo")

    def test_no_configuration_yields_no_repo(self):
        self.assertEqual(view.target_repo({}), "")


class TestLocationLinksInTheReport(unittest.TestCase):
    def test_the_findings_table_links_the_location(self):
        table, _ = view.render_findings(
            ledger(), {}, NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, REPO, ROOTS,
        )
        text = "\n".join(table)
        self.assertIn(
            "\x1b]8;;https://github.com/gke-agentic/kube-agents/blob/"
            "aa3b7aa1111111111111111111111111111111/"
            "k8s-operator/internal/controller/platformagent_controller.go#L1090",
            text,
        )

    def test_the_table_never_links_a_file_its_label_does_not_name(self):
        """The label is the first location, clipped; the URL was computed over
        all of them. `agent/anthropic_adapter.py:42 and
        agents/selfimprove/scripts/selfimprove_ledger.py:9` printed the first --
        the Hermes harness, and deliberately unlinkable -- over a link to the
        second, so the one column whose job is saying where to look named one
        file and went to another."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["location"] = (
            "agent/anthropic_adapter.py:42 and agents/selfimprove/scripts/selfimprove_ledger.py:9"
        )
        table, _ = view.render_findings(
            document, {}, NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, REPO, ROOTS,
        )
        text = "\n".join(table)
        self.assertIn("agent/anthropic_adapter.py:42", view.plain(text))
        self.assertNotIn("selfimprove_ledger.py", text)

    def test_a_second_location_does_not_capture_the_first_ones_link(self):
        """Both are linkable here, so the label is the first and so is the URL."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["location"] = (
            "k8s-operator/cmd/main.go:108 and scripts/generate_docs.py:4"
        )
        table, _ = view.render_findings(
            document, {}, NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, REPO, ROOTS,
        )
        text = "\n".join(table)
        self.assertIn("main.go#L108", text)
        self.assertNotIn("generate_docs.py#L4", text)

    def test_no_repo_means_no_links_and_the_same_text(self):
        args = (ledger(), {}, NOW, view.Palette(True), 200, view.BOX_UNICODE, "severity", None, None)
        linked, _ = view.render_findings(*args, REPO, ROOTS)
        bare, _ = view.render_findings(*args)
        self.assertNotIn("\x1b]8;;", "\n".join(bare))
        self.assertEqual([view.plain(l) for l in linked], [view.plain(l) for l in bare])

    def test_detail_lists_every_reference_separately(self):
        entry = finding(
            "aaaa",
            "high",
            "t",
            location="k8s-operator/cmd/main.go:108 and agents/platform/scripts/x.py:4",
        )
        text = "\n".join(
            view.render_detail(entry, "", NOW, view.Palette(True), 100, True, REPO, ROOTS)
        )
        self.assertIn("open", view.plain(text))
        self.assertIn("main.go#L108", text)
        self.assertIn("x.py#L4", text)

    def test_detail_omits_the_block_when_nothing_is_linkable(self):
        entry = finding("aaaa", "high", "t", location="agent/anthropic_adapter.py:136")
        text = "\n".join(
            view.render_detail(entry, "", NOW, view.Palette(True), 100, True, REPO, ROOTS)
        )
        self.assertNotIn("\x1b]8;;", text)
        self.assertIn("anthropic_adapter", view.plain(text))


#: The pull request's base on an install whose base is itself a fork, which is
#: the configuration that makes bare references ambiguous. `ledger()` records
#: one promotion against it, #160, so that number is the vouched one throughout
#: and every other number is one the prose read somewhere.
PR_REPO = "gke-agentic/kube-agents"

#: What PR_REPO was forked from -- where the project's numbers are assigned.
PARENT_REPO = "gke-labs/kube-agents"


def refs(document=None, parent=PARENT_REPO):
    return view.Refs(PR_REPO, view.filed_pull_requests(document or ledger()), parent)


class TestPullRequestRepo(unittest.TestCase):
    """Where a `#123` is resolved, which is the pull request's base and not the
    repository the source was read from. The two differ under `mode: fork`."""

    def test_upstream_mode_uses_the_upstream(self):
        env = {
            "SELFIMPROVE_MODE": "upstream",
            "SELFIMPROVE_UPSTREAM_REPO": "up/repo",
            "SELFIMPROVE_SOURCE_REPO": "up/repo",
            "SELFIMPROVE_FORK_REPO": "fork/repo",
        }
        self.assertEqual(view.pull_request_repo(env), "up/repo")

    def test_fork_mode_uses_the_fork(self):
        """The chart sets SELFIMPROVE_UPSTREAM_REPO to the pull request's base,
        and under `mode: fork` that base is the fork -- while SOURCE_REPO stays
        on the upstream, because the revision under investigation is upstream's.
        Reading the source repository here would send `#12` to a number that
        belongs to somebody else."""
        env = {
            "SELFIMPROVE_MODE": "fork",
            "SELFIMPROVE_UPSTREAM_REPO": "fork/repo",
            "SELFIMPROVE_SOURCE_REPO": "up/repo",
            "SELFIMPROVE_FORK_REPO": "fork/repo",
        }
        self.assertEqual(view.pull_request_repo(env), "fork/repo")
        self.assertEqual(view.target_repo(env), "up/repo")

    def test_no_cronjob_means_no_repo(self):
        self.assertEqual(view.pull_request_repo({}), "")


class TestIssueUrls(unittest.TestCase):
    def test_builds_a_pull_url(self):
        self.assertEqual(
            view.issue_url("o/r", "874"), "https://github.com/o/r/pull/874"
        )

    def test_pull_is_the_right_form_for_an_issue_too(self):
        """GitHub 302s /pull/N to /issues/N when N is an issue, so one form
        covers both and the view needs no way to tell them apart."""
        self.assertTrue(view.issue_url("o/r", "1").endswith("/pull/1"))

    def test_refuses_a_repo_that_is_not_two_plain_segments(self):
        for repo in ("", "o", "o/r/x", "o/../r", "o/r?x=1", "o/r#f"):
            self.assertEqual(view.issue_url(repo, "1"), "", repo)

    def test_finds_the_first_reference(self):
        self.assertEqual(
            view.Refs("o/r").first("fixed in #874, see also #875"),
            "https://github.com/o/r/pull/874",
        )

    def test_no_reference_and_no_repo_give_no_url(self):
        self.assertEqual(view.Refs("o/r").first("nothing here"), "")
        self.assertEqual(view.Refs().first("fixed in #874"), "")

    def test_what_is_not_a_reference(self):
        """`#` is common in prose the agent writes. A number glued to a word
        before it, or letters after it, is something else."""
        for text in ("colour ##874", "issue# 874", "#874x", "abc#874"):
            self.assertEqual(view.Refs("o/r").first(text), "", text)


class TestFiledPullRequests(unittest.TestCase):
    """Which numbers the ledger can vouch for as the loop's own."""

    def test_reads_the_promotion_urls(self):
        self.assertEqual(view.filed_pull_requests(ledger()), frozenset({(PR_REPO, "160")}))

    def test_a_ledger_with_no_promotions_vouches_for_nothing(self):
        document = ledger()
        for entry in document["findings"].values():
            entry["promotions"] = []
        self.assertEqual(view.filed_pull_requests(document), frozenset())

    def test_junk_does_not_raise(self):
        for document in (
            {},
            {"findings": None},
            {"findings": [finding("aaaa", "high", "t")]},
            {"findings": {"a": "not a dict"}},
            {"findings": {"a": {"promotions": ["not a dict", {}, {"url": None}]}}},
            {"findings": {"a": {"promotions": [{"url": "https://example.com/x"}]}}},
        ):
            self.assertEqual(view.filed_pull_requests(document), frozenset(), document)

    def test_a_list_shaped_findings_block_is_read_too(self):
        promoted = finding(
            "aaaa", "high", "t",
            promotions=[{"url": "https://github.com/o/r/pull/7", "at": iso(1)}],
        )
        self.assertEqual(
            view.filed_pull_requests({"findings": [promoted]}), frozenset({("o/r", "7")})
        )


class TestForkParent(unittest.TestCase):
    """The one network call, and every way it is allowed to come back empty."""

    def gh(self, **kwargs):
        return mock.patch.object(view.subprocess, "run", **kwargs)

    def test_returns_what_gh_reports(self):
        with self.gh(return_value=mock.Mock(returncode=0, stdout=PARENT_REPO + "\n")) as run:
            self.assertEqual(view.fork_parent(PR_REPO), PARENT_REPO)
        self.assertEqual(run.call_args[0][0][:2], ["gh", "api"])
        self.assertIn("repos/" + PR_REPO, run.call_args[0][0])

    def test_a_repo_that_is_not_a_fork_has_no_parent(self):
        with self.gh(return_value=mock.Mock(returncode=0, stdout="\n")):
            self.assertEqual(view.fork_parent(PARENT_REPO), "")

    def test_gh_failing_is_not_an_error(self):
        """Unauthenticated, offline, deleted repository -- all the same answer,
        which is the behaviour the view had before any of this existed."""
        with self.gh(return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(view.fork_parent(PR_REPO), "")

    def test_gh_missing_or_hanging_is_not_an_error(self):
        for exc in (FileNotFoundError(), OSError(), view.subprocess.TimeoutExpired("gh", 10)):
            with self.gh(side_effect=exc):
                self.assertEqual(view.fork_parent(PR_REPO), "", exc)

    def test_an_answer_naming_the_repo_itself_is_dropped(self):
        with self.gh(return_value=mock.Mock(returncode=0, stdout=PR_REPO)):
            self.assertEqual(view.fork_parent(PR_REPO), "")

    def test_an_unsafe_answer_is_dropped(self):
        """`gh --jq` prints whatever the field holds, and the result goes into a
        URL path."""
        for answer in ("o/../r", "o", "o/r/x", "o/r?x=1"):
            with self.gh(return_value=mock.Mock(returncode=0, stdout=answer)):
                self.assertEqual(view.fork_parent(PR_REPO), "", answer)

    def test_no_repo_means_no_call(self):
        for repo in ("", "not-a-repo", "o/../r"):
            with self.gh(side_effect=AssertionError("should not shell out")):
                self.assertEqual(view.fork_parent(repo), "", repo)


class TestWhichRepoABareReferenceGoesTo(unittest.TestCase):
    """The resolution order, on the install where the three answers differ.

    `#160` is in `ledger()`'s promotions, so the loop opened it against the base.
    `#874` is not, so it came out of the project's history -- a squash-merge
    subject or a warning in the source -- and belongs to the fork parent.
    """

    def test_a_number_the_loop_filed_goes_to_the_base(self):
        self.assertEqual(
            refs().first("already filed as #160"),
            "https://github.com/gke-agentic/kube-agents/pull/160",
        )

    def test_a_number_read_out_of_the_history_goes_to_the_fork_parent(self):
        self.assertEqual(
            refs().first("SKIPPED: fixed in #874"),
            "https://github.com/gke-labs/kube-agents/pull/874",
        )

    def test_a_qualified_reference_goes_where_it_says(self):
        for text, want in (
            ("see gke-labs/kube-agents#12", "https://github.com/gke-labs/kube-agents/pull/12"),
            ("see o/r#160", "https://github.com/o/r/pull/160"),
        ):
            self.assertEqual(refs().first(text), want, text)

    def test_a_qualified_reference_cannot_walk_out_of_its_repository(self):
        self.assertEqual(refs().first("see o/../r#1"), "")

    def test_with_no_parent_known_everything_goes_to_the_base(self):
        """`gh` absent or unauthenticated, and an install that files against the
        project itself. Both land here, and both are right for it."""
        without = refs(parent="")
        self.assertEqual(
            without.first("SKIPPED: fixed in #874"),
            "https://github.com/gke-agentic/kube-agents/pull/874",
        )
        self.assertEqual(
            without.first("already filed as #160"),
            "https://github.com/gke-agentic/kube-agents/pull/160",
        )

    def test_no_refs_resolves_nothing_bare(self):
        """`--file` on a ledger with no install behind it. A bare number has no
        repository to go to, but a qualified one brought its own and does not
        need the resolver to have been told anything."""
        for text in ("fixed in #874", "already filed as #160"):
            self.assertEqual(view.NO_REFS.first(text), "", text)
        self.assertEqual(view.NO_REFS.first("see o/r#1"), "https://github.com/o/r/pull/1")


class TestPullRequestReferencesAreLinked(unittest.TestCase):
    REFUSAL = "SKIPPED: already filed as #160"

    def refused_ledger(self):
        document = ledger()
        document["findings"]["aaaa000000000000"]["refused"] = {
            "reason": self.REFUSAL,
            "at": iso(3),
            "revision": "aa3b7aa",
        }
        return document

    def test_the_findings_table_links_the_verdict(self):
        document = self.refused_ledger()
        table, _ = view.render_findings(
            document, {}, NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, REPO, ROOTS, refs(document),
        )
        text = "\n".join(table)
        self.assertIn("https://github.com/gke-agentic/kube-agents/pull/160", text)
        self.assertIn("already filed as #160", view.plain(text))

    def test_a_wrapped_verdict_is_one_link_and_not_several(self):
        """The verdict is far wider than the FINDING column, so it wraps. Each
        row re-opens the hyperlink, and OSC 8's `id=` is what tells the terminal
        they are one link rather than one per line."""
        document = self.refused_ledger()
        table, _ = view.render_findings(
            document, {}, NOW, view.Palette(True), 100, view.BOX_UNICODE,
            "severity", None, None, REPO, ROOTS, refs(document),
        )
        text = "\n".join(table)
        opens = [line for line in table if "/pull/160" in line]
        self.assertGreater(len(opens), 1, "expected the verdict to wrap")
        self.assertIn("\x1b]8;id=", text)

    def test_no_resolver_leaves_the_text_alone(self):
        document = self.refused_ledger()
        args = (
            document, {}, NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, REPO, ROOTS,
        )
        linked, _ = view.render_findings(*args, refs(document))
        bare, _ = view.render_findings(*args)
        self.assertNotIn("/pull/160", "\n".join(bare))
        self.assertEqual([view.plain(l) for l in linked], [view.plain(l) for l in bare])

    def test_a_verdict_naming_no_pull_request_is_not_linked(self):
        table, _ = view.render_findings(
            ledger(), {"aaaa000000000000": "held: 1 occurrence(s) in 24h, rule wants 2"},
            NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, "", ROOTS, refs(),
        )
        self.assertNotIn("/pull/", "\n".join(table))

    def test_the_title_is_never_scanned_for_references(self):
        """A `#12` in a title is as likely to be a hostname suffix or a shell
        comment as a pull request, and the filing skill dictates no vocabulary
        there. Only the verdict, whose wording it does dictate, is linked."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["title"] = "worker#12 restarts"
        table, _ = view.render_findings(
            document, {}, NOW, view.Palette(True), 200, view.BOX_UNICODE,
            "severity", None, None, "", ROOTS, refs(document),
        )
        self.assertNotIn("/pull/12", "\n".join(table))

    def test_detail_links_every_reference_in_the_gate_line(self):
        """Not in a table, so each reference carries its own link rather than
        the paragraph carrying the first one's."""
        entry = finding("aaaa", "high", "t", location="")
        text = "\n".join(
            view.render_detail(
                entry, "held: fixed in #874, superseded by #875", NOW,
                view.Palette(True), 100, True, REPO, ROOTS, refs(),
            )
        )
        self.assertIn("/pull/874", text)
        self.assertIn("/pull/875", text)

    def test_detail_links_the_refusal_reason(self):
        entry = finding(
            "aaaa", "high", "t", location="",
            refused={"reason": self.REFUSAL, "at": iso(3), "revision": "aa3b7aa"},
        )
        text = "\n".join(
            view.render_detail(
                entry, "", NOW, view.Palette(True), 100, True, REPO, ROOTS, refs()
            )
        )
        self.assertIn("/pull/160", text)
        self.assertIn("already filed as #160", view.plain(text))

    def test_detail_sends_a_history_number_to_the_fork_parent(self):
        """The case that started this: the number in the refusal is real, and
        the repository holding it is the one the base was forked from."""
        entry = finding(
            "aaaa", "high", "t", location="",
            refused={"reason": "SKIPPED: fixed in #874", "at": iso(3), "revision": "aa3b7aa"},
        )
        text = "\n".join(
            view.render_detail(
                entry, "", NOW, view.Palette(True), 100, True, REPO, ROOTS, refs()
            )
        )
        self.assertIn("https://github.com/gke-labs/kube-agents/pull/874", text)
        self.assertNotIn("gke-agentic/kube-agents/pull/874", text)

    def test_detail_links_do_not_change_where_lines_wrap(self):
        """Linking runs after `textwrap`, so the escape sequences cannot be
        counted towards the width and push text onto another line."""
        entry = finding("aaaa", "high", "t", location="")
        verdict = "held: the filing turn refused this permanently (SKIPPED: fixed in #874)"
        args = (entry, verdict, NOW, view.Palette(True), 100, True, REPO, ROOTS)
        linked = view.render_detail(*args, refs())
        bare = view.render_detail(*args)
        self.assertIn("/pull/874", "\n".join(linked))
        self.assertEqual([view.plain(l) for l in linked], [view.plain(l) for l in bare])

    def test_a_qualified_reference_survives_the_wrap_at_every_width(self):
        """A reference is recognised only on a line it lands on whole.

        `textwrap` breaks after a hyphen by default and every owner here has
        one, so `gke-labs/kube-agents#874` could arrive as `gke-` plus
        `labs/kube-agents#874` -- which still matches, as a qualified
        reference to a repository called `labs/kube-agents`. That is a live
        link to a 404 rather than a missing one, and the widths it happens at
        are ordinary terminal sizes.
        """
        entry = finding("aaaa", "high", "t", location="")
        verdict = (
            "held: the filing turn refused this permanently "
            "(SKIPPED: already fixed in gke-labs/kube-agents#874)"
        )
        for width in range(40, 104):
            with self.subTest(width=width):
                text = "\n".join(
                    view.render_detail(
                        entry, verdict, NOW, view.Palette(True), width, True,
                        REPO, ROOTS, refs(),
                    )
                )
                self.assertIn("https://github.com/gke-labs/kube-agents/pull/874", text)
                self.assertNotIn("github.com/labs/kube-agents", text)

    def test_colour_off_means_no_escape_sequences_at_all(self):
        entry = finding("aaaa", "high", "t", location="")
        text = "\n".join(
            view.render_detail(
                entry, "held: fixed in #874", NOW, view.Palette(False), 100, True,
                REPO, ROOTS, refs(),
            )
        )
        self.assertNotIn("\x1b", text)
        self.assertIn("fixed in #874", text)


class TestAMalformedLedgerStillRenders(unittest.TestCase):
    """A read-only report is never worth a traceback.

    Every shape below is one a ledger can plausibly hold -- an older schema, a
    `kubectl edit`, a file handed to `--file` -- and each ended the whole report
    rather than the one row it belongs to.
    """

    def test_a_scalar_sightings_field_renders_as_an_unknown_count(self):
        """The loop's own counter iterates `sightings`, and `sightings: 4` is
        not iterable. "?" is what a count this cannot establish already renders
        as when the module itself is missing."""
        entry = finding("dddd333333333333", "medium", "t", sightings=4)
        self.assertIsNone(view.occurrences(entry, NOW))
        self.assertIsNone(view.reported(entry, NOW))

    def test_a_scalar_sightings_field_costs_the_row_only_its_count(self):
        document = ledger()
        document["findings"]["aaaa000000000000"]["sightings"] = 4
        lines, entries = view.render_findings(
            document, {}, NOW, view.Palette(False), 160, view.BOX_UNICODE, "severity", None, None
        )
        self.assertEqual(len(entries), 3)
        self.assertIn("A medium finding", "".join(lines))

    def test_a_scalar_runs_field_reads_as_no_runs(self):
        with ledger_file(ledger(runs="none")) as path:
            code, text = run_main(["--file", path, "--color", "never", "--width", "150"])
        self.assertEqual(code, 0)
        self.assertIn("no runs recorded yet", text)
        self.assertIn("0 runs recorded", text)

    def test_a_timestamp_at_the_start_of_the_calendar_is_printed_as_stored(self):
        """`astimezone` raises rather than clamping at the edges of the
        calendar. `parse_iso` survives the input deliberately -- refusing to
        parse it would lose the row it belongs to -- so the conversion is where
        the guard belongs, and the value is worth seeing: it is almost certainly
        the zero value of something that failed to write a real one."""
        when = view.parse_iso("0001-01-01T00:00:00+05:00")
        self.assertEqual(view.stamp(when, True), "0001-01-01 00:00+05:00")

    def test_a_year_one_timestamp_survives_a_reader_west_of_utc(self):
        """The reported crash: `0001-01-01T00:00:00Z` in any zone behind UTC is
        before `datetime.min`, so it depends on where the reader is sitting."""
        if not hasattr(time, "tzset"):
            self.skipTest("this platform cannot be told which zone it is in")
        with local_zone("America/New_York"):
            self.assertEqual(
                view.stamp(view.parse_iso("0001-01-01T00:00:00Z"), False), "0001-01-01 00:00+00:00"
            )

    def test_a_run_dated_at_the_start_of_the_calendar_does_not_end_the_report(self):
        document = ledger()
        document["runs"][0]["at"] = "0001-01-01T00:00:00+05:00"
        with ledger_file(document) as path:
            code, text = run_main(["--file", path, "--color", "never", "--width", "150"])
        self.assertEqual(code, 0)
        self.assertIn("0001-01-01", text)

    def test_a_scalar_finding_costs_only_its_own_verdict(self):
        """`severity_rank` reads an entry as a mapping, so one scalar in the
        findings map raised where nothing caught it. Widening the `try` around
        the gate would have been worse: it drops every verdict in the ledger for
        one malformed entry."""
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        document = ledger()
        document["findings"]["dddd333333333333"] = "not a finding"
        self.assertEqual(set(view.gate_verdicts(document, GATE, NOW)), set(ledger()["findings"]))

    def test_a_scalar_finding_does_not_cost_the_others_their_verdicts(self):
        """Widening the `try` around the gate would have been the easy fix and
        the worse one: one malformed entry then silently drops every verdict in
        the ledger, and a table with no verdicts looks like a table with a gate
        that holds nothing."""
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        document = ledger()
        document["findings"]["dddd333333333333"] = "not a finding"
        verdicts = view.gate_verdicts(document, GATE, NOW)
        lines, entries = view.render_findings(
            document, verdicts, NOW, view.Palette(False), 160, view.BOX_UNICODE,
            "severity", None, None,
        )
        self.assertEqual(len(entries), 3)
        self.assertIn("cooldown", "".join(lines))

    def test_a_list_shaped_findings_field_with_a_gate_reports_an_unknown_spend(self):
        """`sorted_findings` accepts the list form and the loop's own
        `promotions_today` indexes the mapping one, so the header can be handed
        a ledger the rest of the report renders happily. "?" rather than 0: a
        budget line reading "0 of 3" on a ledger nobody could count is a wrong
        answer, not a missing one."""
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        document = ledger(findings=list(ledger()["findings"].values()))
        text = "\n".join(
            view.render_header(
                document, "{}", "file", "ns", "cm", None, {}, GATE, NOW, view.Palette(False), True
            )
        )
        self.assertIn("? of 3 pull requests", text)

    def test_a_finding_with_no_title_or_location_says_so(self):
        """The fixture above supplies every key, so the placeholders never ran.
        A finding written by an older schema, or by a turn that gave up part-way
        through, is what they are for."""
        document = ledger(
            findings={"dddd333333333333": {"fingerprint": "dddd333333333333", "severity": "high"}}
        )
        with ledger_file(document) as path:
            code, table = run_main(["--file", path, "--color", "never", "--width", "150"])
            _, detail = run_main(["--file", path, "--color", "never", "--detail", "1"])
        self.assertEqual(code, 0)
        self.assertIn("(untitled)", table)
        self.assertIn("(not localised)", detail)


class TestEndToEnd(unittest.TestCase):
    """`main` over `--file`, which is the whole tool minus the kubectl call."""

    def test_the_report_has_every_section(self):
        with ledger_file(ledger()) as path:
            code, text = run_main(["--file", path, "--color", "never", "--width", "150"])
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("last run"))
        for section in ("RUNS", "FINDINGS", "PULL REQUESTS OPENED"):
            self.assertIn(section, text)

    def test_the_pull_request_section_lists_what_the_ledger_holds(self):
        with ledger_file(ledger()) as path:
            _, text = run_main(["--file", path, "--color", "never", "--width", "150"])
        self.assertIn("gke-agentic/kube-agents#160", text)

    def test_the_pull_request_section_is_not_narrowed_by_a_severity_filter(self):
        """A --severity filter narrowing it would hide pull requests still open
        against the findings it hid, and "what has this loop opened" is a fact
        about the install rather than about the current filter."""
        with ledger_file(ledger()) as path:
            _, text = run_main(
                ["--file", path, "--color", "never", "--width", "150", "--severity", "critical"]
            )
        self.assertIn("gke-agentic/kube-agents#160", text)

    def test_rows_reaches_both_tables_and_not_just_the_findings_one(self):
        """The pull-request table wraps its finding titles too, so a `--rows`
        that only threads as far as FINDINGS leaves the second table exactly as
        hard to read as it was."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["promotions"] = [
            {"at": iso(1), "url": "https://github.com/o/r/pull/1"}
        ]

        def sections(style):
            with ledger_file(document) as path:
                _, text = run_main(
                    ["--file", path, "--color", "never", "--width", "150", "--rows", style]
                )
            head, _, tail = text.partition("PULL REQUESTS OPENED")
            return len(head.splitlines()), len(tail.splitlines())

        compact_findings, compact_prs = sections("compact")
        spaced_findings, spaced_prs = sections("spaced")
        self.assertGreater(spaced_findings, compact_findings)
        self.assertGreater(spaced_prs, compact_prs)
        # Two promotions, so one separator: the second table pays the same
        # one-line-per-extra-row price the first one does.
        self.assertEqual(spaced_prs - compact_prs, 1)

    def test_an_empty_pull_request_list_explains_itself(self):
        document = ledger()
        for entry in document["findings"].values():
            entry["promotions"] = []
        with ledger_file(document) as path:
            _, text = run_main(["--file", path, "--color", "never"])
        self.assertIn("none recorded", text)

    def test_no_color_is_honoured_end_to_end(self):
        """Against a ledger that carries escape sequences, not a benign one.
        NO_COLOR gates what this file emits and says nothing about what the
        ledger holds, so a fixture with no escapes in it asserted nothing: the
        report came out clean whether or not any of them were being filtered."""
        document = ledger()
        document["findings"]["aaaa000000000000"]["title"] = "A medium finding\x1b[31m"
        document["runs"][0]["note"] = INJECTED_LINK
        with ledger_file(document) as path:
            with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
                _, text = run_main(["--file", path, "--width", "150"])
        self.assertNotIn("\033[", text)
        self.assertNotIn("\x1b]8;", text)

    def test_ascii_mode_is_pipe_safe(self):
        with ledger_file(ledger()) as path:
            _, text = run_main(["--file", path, "--color", "never", "--ascii", "--width", "150"])
        self.assertFalse(any(ch in text for ch in "─│┌┬┐├┼┤└┴┘"))

    def test_json_prints_the_ledger_and_nothing_else(self):
        with ledger_file(ledger()) as path:
            code, text = run_main(["--file", path, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(text)["findings"]), 3)

    def test_json_unwraps_a_configmap_too(self):
        wrapped = {"data": {view.LEDGER_KEY: json.dumps(ledger())}}
        with ledger_file(wrapped) as path:
            _, text = run_main(["--file", path, "--json"])
        self.assertIn("findings", json.loads(text))

    def test_detail_by_row_number(self):
        with ledger_file(ledger()) as path:
            code, text = run_main(["--file", path, "--color", "never", "--detail", "1"])
        self.assertEqual(code, 0)
        self.assertIn("A critical finding", text)

    def test_detail_by_fingerprint_prefix(self):
        with ledger_file(ledger()) as path:
            code, text = run_main(["--file", path, "--color", "never", "--detail", "cccc"])
        self.assertEqual(code, 0)
        self.assertIn("A low finding", text)

    def test_detail_by_row_number_follows_the_sort(self):
        """The row numbers the table prints are positions in the list `--sort`
        ordered, so `--detail 1` has to open the row the reader has just seen at
        number 1. It rebuilt the list with the default severity sort instead."""
        document = ledger()
        document["findings"]["cccc222222222222"]["last_seen"] = iso(0)
        with ledger_file(document) as path:
            code, text = run_main(
                ["--file", path, "--color", "never", "--sort", "last", "--detail", "1"]
            )
        self.assertEqual(code, 0)
        self.assertIn("A low finding", text)
        self.assertNotIn("A critical finding", text)

    def test_detail_by_row_number_counts_only_the_rows_the_filters_left(self):
        with ledger_file(ledger()) as path:
            code, text = run_main(
                ["--file", path, "--color", "never", "--signal", "inefficiency", "--detail", "1"]
            )
            self.assertEqual(code, 0)
            self.assertIn("A low finding", text)
            # One row survived the filter, so there is no second one to open.
            missing, _ = run_main(
                ["--file", path, "--color", "never", "--signal", "inefficiency", "--detail", "2"]
            )
        self.assertEqual(missing, 1)

    def test_detail_by_row_number_respects_a_severity_floor(self):
        """`--severity critical` leaves one row, so there is no row 2 to open.
        It opened the second finding of the unfiltered ledger, which the reader
        was not looking at and the filter had deliberately hidden."""
        with ledger_file(ledger()) as path:
            code, text = run_main(
                ["--file", path, "--color", "never", "--severity", "critical", "--detail", "1"]
            )
            self.assertEqual(code, 0)
            self.assertIn("A critical finding", text)
            missing, _ = run_main(
                ["--file", path, "--color", "never", "--severity", "critical", "--detail", "2"]
            )
        self.assertEqual(missing, 1)

    def test_detail_by_fingerprint_reaches_a_finding_the_filter_hid(self):
        """Unlike a row number, a fingerprint is a name that outlives the table
        it was read from, so it is looked up across the whole ledger."""
        with ledger_file(ledger()) as path:
            code, text = run_main(
                ["--file", path, "--color", "never", "--severity", "critical", "--detail", "cccc"]
            )
        self.assertEqual(code, 0)
        self.assertIn("A low finding", text)

    def test_the_sort_flag_reaches_the_table(self):
        document = ledger()
        document["findings"]["cccc222222222222"]["last_seen"] = iso(0)
        with ledger_file(document) as path:
            _, text = run_main(
                ["--file", path, "--color", "never", "--width", "170", "--sort", "last"]
            )
        section = text.split("FINDINGS")[1].split("PULL REQUESTS")[0]
        self.assertLess(section.index("A low finding"), section.index("A critical finding"))

    def test_the_signal_flag_reaches_the_table(self):
        with ledger_file(ledger()) as path:
            _, text = run_main(
                ["--file", path, "--color", "never", "--width", "170", "--signal", "latency"]
            )
        section = text.split("FINDINGS")[1].split("PULL REQUESTS")[0]
        self.assertIn("A critical finding", section)
        self.assertNotIn("A low finding", section)

    def test_detail_that_matches_nothing_fails_with_advice(self):
        with ledger_file(ledger()) as path:
            code, _ = run_main(["--file", path, "--detail", "zzzz"])
        self.assertEqual(code, 1)

    def test_a_file_that_is_not_a_ledger_is_rejected(self):
        with ledger_file({"something": "else"}) as path:
            code, _ = run_main(["--file", path])
        self.assertEqual(code, 1)

    def test_a_file_that_is_not_json_is_rejected_rather_than_traced(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("not json at all")
            path = handle.name
        try:
            code, _ = run_main(["--file", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 1)

    def test_a_missing_file_is_rejected_rather_than_traced(self):
        code, _ = run_main(["--file", "/nonexistent/ledger.json"])
        self.assertEqual(code, 1)

    def test_an_empty_ledger_renders_rather_than_crashing(self):
        with ledger_file({"version": 1, "findings": {}, "runs": []}) as path:
            code, text = run_main(["--file", path, "--color", "never", "--width", "150"])
        self.assertEqual(code, 0)
        self.assertIn("no runs recorded yet", text)
        self.assertIn("no findings match", text)

    def test_a_narrow_terminal_still_produces_aligned_tables_that_fit(self):
        """Each table is internally aligned and inside the budget. Different
        tables legitimately settle at different widths -- they have different
        columns -- so this groups the lines by table rather than demanding one
        width across the report."""
        with ledger_file(ledger()) as path:
            _, text = run_main(["--file", path, "--color", "never", "--width", "80"])
        for block in table_blocks(text):
            widths = {len(line) for line in block}
            self.assertEqual(len(widths), 1, "a table's lines disagree on width: %s" % sorted(widths))
            self.assertLessEqual(widths.pop(), 80)

    def test_a_narrow_terminal_says_which_columns_it_dropped(self):
        with ledger_file(ledger()) as path:
            _, text = run_main(["--file", path, "--color", "never", "--width", "80"])
        self.assertIn("dropped to fit 80 columns", text)

    def test_a_wide_terminal_drops_nothing(self):
        with ledger_file(ledger()) as path:
            _, text = run_main(["--file", path, "--color", "never", "--width", "170"])
        self.assertNotIn("dropped to fit", text)
        for header in ("SIGNAL", "CONF", "REPORTED", "REVISION"):
            self.assertIn(header, text)

    def test_the_file_path_never_shells_out(self):
        """`--file` is the offline door; a stray kubectl call on it would make
        the tests depend on a cluster and the tool unusable on a plane."""
        with ledger_file(ledger()) as path:
            with mock.patch("subprocess.run", side_effect=AssertionError("kubectl was called")):
                code, _ = run_main(["--file", path, "--color", "never"])
        self.assertEqual(code, 0)


class TestArgumentSurface(unittest.TestCase):
    def test_the_defaults_match_what_the_chart_installs(self):
        args = view.build_parser().parse_args([])
        self.assertEqual(args.namespace, "kubeagents-system")
        self.assertEqual(args.configmap, "kube-agents-selfimprove-ledger")
        self.assertEqual(args.cronjob, "kube-agents-selfimprove")

    def test_the_namespace_and_configmap_can_come_from_the_environment(self):
        with mock.patch.dict(
            os.environ,
            {"SELFIMPROVE_NAMESPACE": "other", "SELFIMPROVE_LEDGER_CONFIGMAP": "other-cm"},
            clear=False,
        ):
            args = view.build_parser().parse_args([])
        self.assertEqual(args.namespace, "other")
        self.assertEqual(args.configmap, "other-cm")

    def test_the_severity_choices_are_the_ledgers_own(self):
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        self.assertEqual(view.SEVERITY_ORDER, view.ledger_mod.SEVERITIES)

    def test_the_size_cap_matches_the_ledgers_own(self):
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        self.assertEqual(view.FALLBACK_MAX_BYTES, view.ledger_mod.LEDGER_MAX_BYTES)

    def test_the_ledger_key_matches_the_ledgers_own(self):
        if view.ledger_mod is None:
            self.skipTest("selfimprove_ledger is not importable from this checkout")
        self.assertEqual(view.LEDGER_KEY, view.ledger_mod.LEDGER_KEY)

    def test_rows_defaults_to_spaced(self):
        self.assertEqual(view.build_parser().parse_args([]).rows, "spaced")

    def test_the_script_is_executable(self):
        path = pathlib.Path(view.__file__)
        self.assertTrue(os.access(path, os.X_OK), "%s is not executable" % path)


if __name__ == "__main__":
    unittest.main()
