"""Every sed program in hack/check-image-inventory.sh is one BSD sed runs (#1449).

The one CI job that runs the script, validate.yml, runs it on GNU sed, which
accepts `\\?`, `\\+`, `\\|`, `\\s`, `\\w`, `\\<`, `\\b` and their complements as
extensions to POSIX regular expressions. BSD sed -- /usr/bin/sed on macOS --
reads each as a pattern that matches nothing or as the literal character, and
`sed -n .../p` then prints the wrong lines rather than failing.
image_field_refs carried one `\\?`, so on macOS every `image:` field dropped out
of image_refs while the run stayed green, until #1317 added the first check
that needed one. No job runs this script under BSD sed (the macOS runner in
installer-matrix-test.yml runs the installer scripts only), so this is a
text-level lint over the script's own sed programs rather than a run under
both: it reads each invocation's flags and every expression, resolves the
`readonly` constant a program is built from, and fails on the escapes only GNU
sed accepts -- in every program not invoked with -E for the three -E spells as
bare `?`, `+` and `|`, and in every program for the rest -- and on GNU long
options, which BSD sed rejects outright. What it cannot read it reports: a sed
in command position whose expression it did not find fails
test_every_sed_invocation_is_linted rather than passing.
"""

import pathlib
import re
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_constant, lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# `sed` where a command goes: the start of a line, after `|`, `(`, `$(`, `;`,
# `&` or `{`, or after a keyword or wrapper that runs the word following it.
# The word also turns up in strings, which the coverage check is not about.
_SED_COMMAND_RE = re.compile(r"(?:^|[|(;&{]|\b(?:then|else|do|xargs|exec|env|command)\s)\s*sed\s")

# The word itself, wherever an argument run can follow it. Comment lines are
# blanked before the scan, so a program quoted in prose is not linted.
_SED_WORD_RE = re.compile(r"(?<![\w.-])sed(?=\s)")

# One argument after `sed`: a flag token, a single-quoted word, or a
# double-quoted word with backslash-escaped quotes honoured. The run ends at
# the first thing that is none of these -- a bare operand, a pipe, a `)`.
_SED_ARG_RE = re.compile(r"""\s+(?:(-\S+)|'([^']*)'|"((?:[^"\\]|\\.)*)")""")

# A flag whose next argument is an expression: -e alone or ending a cluster.
_EXPRESSION_FLAG_RE = re.compile(r"^-[A-Za-z]*e$")

# GNU's inline spelling of the same.
_EXPRESSION_INLINE_RE = re.compile(r"^--expression=(.*)$")

# A flag whose next argument is not an expression: -i takes a suffix on BSD
# sed, -f a file whose program this lint cannot read.
_OPERAND_FLAG_RE = re.compile(r"^-[A-Za-z]*[if]$")

# Extended syntax: -E alone or inside a cluster, and GNU's -r spelling.
# `--regexp-extended` is not here because BSD sed has no long options at all;
# every `--` flag is a finding of its own.
_EXTENDED_FLAG_RE = re.compile(r"^-[A-Za-z]*[Er][A-Za-z]*$")
_LONG_OPTION_PREFIX = "--"

# `$NAME` or `${NAME}` inside a program. When NAME is a `readonly` constant of
# the script its value is read through, so moving a pattern into a constant
# does not move it out of the lint's reach; anything else stays as written,
# because a runtime value carries no escape the lint could read.
_VARIABLE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# The value on a `readonly NAME=...` line, one layer of quotes removed.
_CONSTANT_VALUE_RE = re.compile(r"""^readonly [A-Za-z_][A-Za-z0-9_]*=(['"]?)(.*)\1\s*$""")

# The escapes GNU sed accepts in a basic regular expression and BSD sed does
# not: against /usr/bin/sed on macOS 25.6, `\?` matches nothing and `\+` and
# `\|` match the literal character. Under -E the same three operators are the
# bare characters, and the escaped form is the literal on both.
_GNU_ONLY_BASIC_ESCAPES = (r"\?", r"\+", r"\|")

# The escapes with no POSIX spelling in either syntax: the character classes
# and the word anchors. Against the same sed, `\b` matches a literal b and the
# rest match nothing.
_GNU_ONLY_ESCAPES = (r"\s", r"\S", r"\w", r"\W", r"\<", r"\>", r"\b", r"\B")

# The program image_field_refs shipped with before #1449, and the two portable
# spellings of it. The lint has to tell them apart or it passes vacuously.
_PROGRAM_BEFORE_1449 = r's/^[[:space:]]*image:[[:space:]]*"\?\([^"]*\)"\?[[:space:]]*$/\1/p'
_PROGRAM_POSIX_BASIC = r's/^[[:space:]]*image:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p'
_PROGRAM_EXTENDED = r's/^[[:space:]]*image:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/p'

# Invocation shapes the argument reader has to get right, each with the
# programs it must yield; None is an invocation whose program is out of reach.
_ARGUMENT_CASES = (
    ("sed -n 's/a/b/p' \"$file\"", ["s/a/b/p"]),
    ("x=\"$(sed -n 's/a/b/p' <<<\"$y\")\"", ["s/a/b/p"]),
    ("sed -n -e 's/a/b/' -e 's/x\\?/y/p'", ["s/a/b/", "s/x\\?/y/p"]),
    ("sed -ne 's/a/b/' -e 's/c/d/'", ["s/a/b/", "s/c/d/"]),
    ("sed -i '' 's/a\\?/b/' f", ["s/a\\?/b/"]),
    ('sed -n "s/\\"\\?//p"', ['s/\\"\\?//p']),
    ("sed --expression='s/a/b/' f", ["s/a/b/"]),
    ("sed -f prog.sed f", [None]),
    ("sed s/a/b/ f", [None]),
    ("# sed -n 's/a\\?//p' used to be here", []),
)

# Lines where sed is, or is not, in command position.
_COMMAND_POSITION_CASES = (
    ("  sed -n 's/a/b/p'", True),
    ("x=\"$(sed -n 's/a/b/p')\"", True),
    ("cat f | sed -n 's/a/b/p'", True),
    ("{ sed -f prog.sed; }", True),
    ("if x; then sed -f prog.sed; fi", True),
    ("while :; do sed -f prog.sed; done", True),
    ("printf x | xargs sed -f prog.sed", True),
    ('echo "or this sed does not accept the pattern"', False),
    ("# sed -n 's/a/b/p'", False),
)


def _constant_value(name: str, text: str) -> str:
    match = _CONSTANT_VALUE_RE.match(lift_constant(name, text, _SCRIPT))
    assert match is not None, f"{_SCRIPT}: readonly {name}= is not one quoted value"
    return match.group(2)


def _resolve(program: str, text: str) -> str:
    """`program` with each `$NAME` that names a readonly constant replaced by
    the constant's value."""

    def replace(match):
        try:
            return _constant_value(match.group(1), text)
        except AssertionError:
            return match.group(0)

    return _VARIABLE_RE.sub(replace, program)


def _offending_escapes(program: str, extended: bool) -> list:
    """The GNU-only escapes in `program`, for the syntax it runs under."""
    gnu_only = _GNU_ONLY_ESCAPES + (() if extended else _GNU_ONLY_BASIC_ESCAPES)
    return [escape for escape in gnu_only if escape in program]


def _code_text(text: str) -> str:
    """`text` with every comment line blanked, line numbers preserved."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.split("\n"))


def _sed_calls(text: str, constants: str = None) -> list:
    """(line number, flags, extended, resolved program) once per expression of
    every sed invocation in `text`, comment lines excluded; program is None
    for an invocation whose expression the reader could not find. `$NAME` is
    resolved against `constants` -- the whole script when `text` is a piece
    of it."""
    constants = constants or text
    code = _code_text(text)
    calls = []
    for word in _SED_WORD_RE.finditer(code):
        line = code.count("\n", 0, word.start()) + 1
        flags, programs, pending, position = [], [], None, word.end()
        while True:
            arg = _SED_ARG_RE.match(code, position)
            if arg is None:
                break
            position = arg.end()
            flag = arg.group(1)
            if flag is not None:
                flags.append(flag)
                inline = _EXPRESSION_INLINE_RE.match(flag)
                if inline:
                    programs.append(inline.group(1).strip("'\""))
                elif _EXPRESSION_FLAG_RE.match(flag):
                    pending = "expression"
                elif _OPERAND_FLAG_RE.match(flag):
                    pending = "operand"
                continue
            quoted = arg.group(2) if arg.group(2) is not None else arg.group(3)
            if pending == "expression" or (pending is None and not programs):
                programs.append(quoted)
            elif pending is None:
                break  # a quoted operand after the program: the file it reads
            pending = None
        extended = any(_EXTENDED_FLAG_RE.match(flag) for flag in flags)
        for program in programs or [None]:
            resolved = None if program is None else _resolve(program, constants)
            calls.append((line, tuple(flags), extended, resolved))
    return calls


def _code_lines_running_sed(text: str) -> list:
    """Line numbers where `sed` stands in command position outside a comment."""
    return [
        number
        for number, line in enumerate(_code_text(text).split("\n"), start=1)
        if _SED_COMMAND_RE.search(line)
    ]


class SedPortabilityTest(unittest.TestCase):
    def test_sed_programs_use_no_gnu_only_escapes(self):
        text = _SCRIPT.read_text()
        for line, flags, extended, program in _sed_calls(text):
            with self.subTest(line=line):
                self.assertEqual(
                    [flag for flag in flags if flag.startswith(_LONG_OPTION_PREFIX)],
                    [],
                    f"{_SCRIPT}:{line}: BSD sed has no long options; spell the flag short",
                )
                if program is None:
                    continue  # test_every_sed_invocation_is_linted reports it
                self.assertEqual(
                    _offending_escapes(program, extended),
                    [],
                    f"{_SCRIPT}:{line}: sed program uses an escape BSD sed does not read as GNU "
                    f"sed does; use -E, or the POSIX \\{{0,1\\}} form (#1449): {program}",
                )

    def test_every_sed_invocation_is_linted(self):
        """A program the reader cannot find is a program the lint never reads,
        so every code line running sed has to yield one."""
        text = _SCRIPT.read_text()
        readable = {line for line, _, _, program in _sed_calls(text) if program is not None}
        for line in _code_lines_running_sed(text):
            with self.subTest(line=line):
                self.assertIn(
                    line,
                    readable,
                    f"{_SCRIPT}:{line} runs sed but its expression is not a quoted argument "
                    "after the flags (or comes from -f), so the lint cannot read it",
                )
        self.assertTrue(readable, f"{_SCRIPT} has no sed invocation the lint recognises")

    def test_image_field_refs_program_is_read_through_its_constant(self):
        """The pattern lives in IMAGE_FIELD_RE, so the lint has to resolve the
        reference or the constant is a blind spot."""
        text = _SCRIPT.read_text()
        function = lift_function("image_field_refs", text, _SCRIPT)
        self.assertIn("IMAGE_FIELD_RE", function)
        (call,) = _sed_calls(function, text)
        _, _, extended, program = call
        self.assertIsNotNone(program)
        self.assertNotIn("IMAGE_FIELD_RE", program)
        self.assertIn("image:", program)
        self.assertEqual(_offending_escapes(program, extended), [])

    def test_the_lint_tells_the_1449_program_from_the_portable_ones(self):
        self.assertEqual(_offending_escapes(_PROGRAM_BEFORE_1449, extended=False), [r"\?"])
        self.assertEqual(_offending_escapes(_PROGRAM_POSIX_BASIC, extended=False), [])
        self.assertEqual(_offending_escapes(_PROGRAM_EXTENDED, extended=True), [])

    def test_every_expression_of_an_invocation_is_read(self):
        for line, programs in _ARGUMENT_CASES:
            with self.subTest(line=line):
                self.assertEqual([program for _, _, _, program in _sed_calls(line)], programs)

    def test_extended_flag_is_read_alone_or_folded(self):
        for flags, extended in (
            ("-E", True),
            ("-nE", True),
            ("-En", True),
            ("-r", True),
            ("-n", False),
            ("-ne", False),
            ("--regexp-extended", False),
        ):
            with self.subTest(flags=flags):
                self.assertEqual(bool(_EXTENDED_FLAG_RE.match(flags)), extended)

    def test_sed_in_command_position_is_found(self):
        for line, running in _COMMAND_POSITION_CASES:
            with self.subTest(line=line):
                self.assertEqual(_code_lines_running_sed(line) == [1], running)


if __name__ == "__main__":
    unittest.main()
