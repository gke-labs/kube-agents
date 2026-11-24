"""The shell command-line reader the bench's verifiers share.

A worker's or an agent's terminal command is read the way bash reads it --
comments off, quotes honoured, substitutions folded into their words, and
operators only outside quotes -- and then split into one argv per command.
The ``worker_commands`` verifier (``verifiers``) and the GitHub-write
attribution (``github_writes``) both read calls through it, so the two
cannot disagree on where a command starts. It lives in its own module
because ``verifiers`` imports ``github_writes``.

A NUL character in a line stays in its word. The worker's terminal tool
passes the line inside one argument of a shell's ``-c`` flag (read in
hermes-agent v2026.9.14), and an argument cannot hold a NUL character, so no
shell reads such a line. The reader does not model a shell that drops the
character.

The agent images copy this one file beside
``agents/platform/scripts/worker_transcript.py``, which loads it by path. So
it imports only the standard library and nothing from this package. That
module's secret-read detector reads command lines with
``_shell_words_and_spans``, ``_ShellOperator``, ``_is_redirect`` and
``_substitution_commands``, so a change to one of them also changes which
tool results the detector withholds. The second read of ``_shell_argvs``
(``_commands_in_turn``) is not one of them.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable

# A command line is read as the shell reads it: tokenised first, quotes honoured, so a quoted
# path is one word and a quoted sentence is one word too (which is what tells a run of a script
# from a mention of it), and only then split into segments at the operator tokens the shell
# starts a new command on. A bare token (outside quotes) made of these characters alone is such
# an operator; a quoted word that spells one (`';'`, `">"`) is text.
_SHELL_OPERATOR_CHARS = frozenset("&|;()")
# The characters whose bare runs the shell reads as one operator token (`&&`, `>>`, `2>&1`'s
# `>&`, `|&`); `(` and `)` are operators of one character each.
_SHELL_OPERATOR_RUN_CHARS = frozenset("&|;<>")
_SHELL_PARENS = frozenset("()")
# The reader puts its marks inside the line's own text, between two of this character. A mark
# character that the line itself holds is written as the literal mark, so each mark character
# in the marked text opens or closes a mark that the reader wrote, and no text of the line reads
# as an operator mark or a substitution mark.
_MARK_CHAR = "\x00"
_LITERAL_MARK = "\x00N\x00"
_OPERATOR_MARK = "\x00O{index}\x00"
_OPERATOR_MARK_RE = re.compile(r"^\x00O(\d+)\x00$")
_SHELL_PROGRAMS_WITH_COMMAND_STRING = ("sh", "bash", "zsh", "dash")
# `eval` runs its words, joined by spaces, as a command line of its own (`eval "git push ..."`).
_SHELL_EVAL = "eval"
# `eval` joins its words with one space before the shell reads them again.
_SHELL_EVAL_WORD_SEPARATOR = " "
# The limits of :func:`_shell_argvs` for one typed line. They are the numbers of the secret-read
# detector in agents/platform/scripts/worker_transcript.py, which reads the same lines. A command
# line inside a line (after an `eval`, behind a shell's `-c` flag, in a substitution) is one
# level; the typed line is level 0, and a command line that a person types nests a few levels.
# The budget is in characters: each command line that the reader reads costs its length and
# `_READ_COST_CHARS` more. Without it, a substitution inside an `eval` word, which is read as a
# span of the line and again in the line that `eval` runs, doubles the reading at each level.
_MAX_COMMAND_LINE_NESTING = 16
_MAX_READ_CHARS_FACTOR = 6
_MAX_READ_CHARS_FLOOR = 65536
_READ_COST_CHARS = 16
_NESTING_LIMIT_TEXT = f"more than {_MAX_COMMAND_LINE_NESTING} levels of command lines inside the line"
_READ_LIMIT_TEXT = f"more reading than {_MAX_READ_CHARS_FACTOR} x the line's length + {_MAX_READ_CHARS_FLOOR} characters"
# The second read (:func:`_commands_in_turn`) knows two forms that the first read's quote model
# does not: a heredoc and an ANSI-C quote. A call with neither, which the first read does not
# refuse, is read the same by both reads.
_HEREDOC_OPERATOR = "<<"
_HERE_STRING_OPERATOR = "<<<"
# `<<-` strips the leading tabs of the body lines and of the delimiter line.
_HEREDOC_TAB_STRIP = "-"
_HEREDOC_TAB = "\t"
_HEREDOC_BLANKS = " \t"
# `(( x << 2 ))` at the start of a command line is an arithmetic command: its `<<` is a shift.
# Behind another command on the line (`x=1; (( y = x << 2 ))`) the reader takes it as a heredoc.
_ARITHMETIC_COMMAND_OPENER = "(("
_ANSI_C_QUOTE_OPENER = "$'"
# The depth of a substitution opener is its count of this character (`$((` has two).
_SUBSTITUTION_DEPTH_CHAR = "("
# What joins the words of a command line that the reader writes.
_WORD_SEPARATOR = " "
# What an ANSI-C quote's escapes spell, for the ones a command line uses; another escape keeps
# its backslash, as the first read keeps it.
_ANSI_C_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r"}
# A single quote inside a single-quoted word: close the quote, an escaped quote, open it again.
_SINGLE_QUOTE_IN_SINGLE_QUOTES = "'\\''"
# A heredoc body on a line that has one of these as a word can be a command line for that
# shell, so it is read as commands, not as text. The test is on the words of the whole line, not
# on the command that takes the heredoc.
_HEREDOC_SHELL_PROGRAMS = frozenset((*_SHELL_PROGRAMS_WITH_COMMAND_STRING, "ssh"))
# bash runs the substitutions of a heredoc body line when the delimiter has no quote, whatever
# quotes stand around them. The ones that the reader did not see as spans of the line are read
# as words of the null command.
_BODY_SUBSTITUTION_OPENERS = ("$((", "$(")
_NULL_COMMAND = ":"
# A shell program is unwrapped only where the shell would run it: as the segment's command word,
# or behind words that hand their argv on unchanged -- `NAME=value` assignments, these wrappers,
# their flags and a duration or count such as `timeout 600`'s. A shell word behind `echo` or
# `printf` is text the command prints, wherever the `echo` stands (`nohup echo bash -c ...` is
# the wrapper running `echo`), and the string after its `-c` is not run.
_SHELL_EXEC_WRAPPERS = frozenset(("timeout", "nohup", "env", "time", "nice", "ionice", "stdbuf", "setsid", "sudo", "exec", "command", "xargs", "watch", "flock", "chroot"))
# A wrapper that takes a positional ahead of the command it runs (`flock <lock> cmd`,
# `chroot <root> cmd`): that one word is the wrapper's own, whatever it looks like.
_SHELL_WRAPPER_POSITIONALS = {"flock": 1, "chroot": 1}
_SHELL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# The interpreters a case's script is run through, and the project runners that hand the words
# after `run` to one (`uv run`, `poetry run`, `pipenv run`); `-m <stem>` is read after one of these.
_INTERPRETER_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)?)?|pypy3?|uv|poetry|pipenv)$")
_SHELL_COMMENT_CHAR = "#"
_SHELL_QUOTE_CHARS = frozenset("'\"")
_SHELL_SINGLE_QUOTE = "'"
_SHELL_DOUBLE_QUOTE = '"'
_SHELL_ESCAPE_CHAR = "\\"
_SHELL_LINE_END = "\n"
# A `#` opens a comment only where a word opens: at the start of the line, after whitespace or
# after an operator character. Bash then drops the rest of the line, operators included.
_SHELL_WORD_OPENERS = frozenset(" \t\n\r\f\v<>") | _SHELL_OPERATOR_CHARS
# Bash's reserved words: a command word stands behind them (`if python3 ...`, `do python3 ...`).
_SHELL_RESERVED_WORDS = frozenset(("if", "then", "else", "elif", "while", "until", "do", "{", "!"))
# A redirect operator, as the tokeniser cuts it (`>`, `>>`, `>&`, `&>`, `<<<`; a leading fd number
# is a word of its own); one with nothing after it (`>#log`, once the comment is off) is a line
# bash refuses. Matched against bare operator tokens only: a quoted `">"` is text.
_SHELL_REDIRECT_RE = re.compile(r"^(?:\d*[<>]{1,3}&?|&>>?)$")
# A substitution opens a span inside a word that the shell runs for its text, not a subshell:
# `$(`...`)`, `$((`...`))`, `<(`...`)`, `>(`...`)` and a backtick pair. Outside single quotes
# each is folded into its enclosing word before the line is tokenised, so `r_$(date +%s).json`
# stays one word while a bare `(` keeps opening a subshell; the commands inside a span (all but
# the arithmetic `$((`...`))`) are then read as commands of their own, as the shell runs them.
_SHELL_SUBSTITUTION_OPENERS = ("$((", "$(", "<(", ">(")
# Inside double quotes bash still runs `$(`...`)`, `$((`...`))` and backticks; `<(` and `>(` are
# text there.
_SHELL_QUOTED_SUBSTITUTION_OPENERS = ("$((", "$(")
_SHELL_ARITHMETIC_OPENER = "$(("
_SHELL_BACKTICK = "`"
_SUBSTITUTION_MARK = "\x00S{index}\x00"
# The marks a word can hold: a literal mark, or a substitution mark with its index.
_WORD_MARK_RE = re.compile(r"\x00(?:N|S(\d+))\x00")
# After an exec wrapper, only the wrapper's own words are transparent: a flag, the word after a
# flag (its value, as in `-u hermes` or `-s KILL`, unless that word is a flag itself; which flags take one is not tabled), a
# duration or count (`timeout 600`, `timeout infinity`), an expansion the reader never resolves
# (`timeout $T`, `"${TIMEOUT:-600}"`) and `--`, which ends its options. A redirect and its target
# stand anywhere ahead of the command (`nohup > log python3 ...`). The first other word is the
# command the wrapper runs.
_SHELL_FLAG_PREFIX = "-"
_SHELL_OPTIONS_END = "--"
_SHELL_EXPANSION_PREFIX = "$"
_SHELL_COUNT_RE = re.compile(r"^(?:\d+(?:\.\d+)?[smhd]?|infinity)$")
# The structural grep exemption (`_without_grep_patterns`): the programs it reads, the short
# options that take a value (in the next word or glued, `-A 2` or `-A2`; `-e` gives a pattern and
# `-f` a file of patterns, so with either no positional word is the pattern), the long forms of
# those two when the value is glued with `=` (any other long option is read as a flag, so a
# separate value of one is graded as the pattern and the real pattern stays on the row), the one
# place its output may go, and the row hermes renders for a collapsed chain.
_GREP_PROGRAMS = frozenset(("grep", "egrep", "fgrep"))
_GREP_SHORT_VALUE_OPTIONS = frozenset("efABCmdD")
_GREP_PATTERN_OPTION = "e"
_GREP_PATTERN_FILE_OPTION = "f"
_GREP_LONG_PATTERN_PREFIX = "--regexp="
_GREP_LONG_PATTERN_FILE_PREFIX = "--file="
_GREP_LONG_OPTION_PREFIX = "--"
_GREP_STDOUT_REDIRECT = ">"
_GREP_OUTPUT_SINK = "/dev/null"
# A `#` where a word opens starts a comment, which the words drop; a card log row joins the next
# typed line onto it with a space, so a comment there would hide that line from the grading.
_COMMENT_CHAR = "#"
_COLLAPSED_CHAIN_RE = re.compile(r"\s\+\s\d+\scommand(?:s|\(s\))?\s*$")
# A backslash before a blank in a stored typed call: an escaped space or tab, or what the pod's
# scrub leaves of an escaped backslash at a line end (`worker_transcript.fold_shell_command`).
_ESCAPED_BLANK_RE = re.compile(r"\\[ \t]")
# A heredoc operator anywhere in a typed call's text (`<<` or `<<-`, not the here-string `<<<`).
# Read on the raw text, quotes and substitutions included, so a quoted `<<` or a shift in an
# arithmetic span counts too: the caller only keeps such a call on its card log row.
_HEREDOC_OPERATOR_RE = re.compile(r"(?<!<)<<(?!<)")
# The checks below read the line as the shell reader keeps it, quotes in place and line
# continuations gone, so a quote or a backslash that ends a name in bash ends it here too.
# A word that reads the last argument back (`$_`, `${_}`), or a variable through another
# (`${!NAME}`, which can name `_` or a variable an assignment set).
_READ_BACK_RE = re.compile(r"\$(?:_(?![A-Za-z0-9_])|\{_(?![A-Za-z0-9_])|\{!)")
# The one braced expansion a grep row may hold: a plain name, or a default that assigns
# nothing (`${HOME}`, `${DIR:-.}`). Any other `${…}` can assign (`${X:=…}`, `${A[i]=…}`,
# `${!X:=…}`), so it is refused rather than parsed.
_BRACED_EXPANSION_OPEN = "${"
_PLAIN_BRACED_EXPANSION_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:\}|:?-)")


class ShellReadLimitError(ValueError):
    """A command line is past a limit of :func:`_shell_argvs`: its command lines nest deeper than
    ``_MAX_COMMAND_LINE_NESTING``, or they need more reading than the line's budget. The reader
    gives no argvs for such a line, so a check that reads argvs cannot be evaluated on it."""


def _read_shell_line(command: str) -> tuple[str, list[str], list[str]]:
    """The command line with its comments removed, every substitution span the shell would run
    replaced by a marker inside its word, and every bare operator token replaced by a marker of
    its own; the spans and the operators in order. One pass over the raw line, with one quote
    model: a ``'`` or ``"`` outside quotes opens a quoted string that the other character does
    not close (``"don't"`` is one quoted word), an escaped character is kept with its escape
    and never opens a word, and a ``\\``-newline pair, a line continuation, is removed as bash
    removes it. A ``#`` outside quotes that opens a word (at the start of the line,
    after whitespace or after an operator character) comments out the rest of its line,
    operators included, so nothing after it runs; a ``#`` inside a word (``readiness-#2.json``,
    ``$#``), inside quotes (``'#1.json'``) or escaped is part of the word. A substitution
    (``$(``...``)``, ``$((``...``))``, ``<(``...``)``, ``>(``...``)``, a backtick pair) outside
    single quotes is consumed whole, quotes and escapes inside it honoured, so
    ``r_$(date +%s).json`` stays one word and the ``)`` that closes it never opens a word for a
    ``#`` after it; inside double quotes only the forms bash runs there are folded. An operator
    (``&&``, ``;``, ``|``, ``>``, ``2>&1``'s ``>&``, ``(``, a bare newline, which starts a new
    command as ``;`` does) is one only outside quotes, which is
    known here and not after the quotes come off: a quoted ``">"`` or ``';'`` stays a word of
    text. A span left open at the line's end is kept as it stands. Read before the quotes come
    off, because afterwards a quoted ``#1.json`` and a bare one look the same. A mark character
    of the line itself is written as the literal mark (:func:`_with_literal_marks`)."""
    spans: list[str] = []
    operators: list[str] = []
    out: list[str] = []
    i = 0
    quote: str | None = None
    previous = _SHELL_LINE_END
    while i < len(command):
        char = command[i]
        if quote is None and char == _SHELL_COMMENT_CHAR and previous in _SHELL_WORD_OPENERS:
            end = command.find(_SHELL_LINE_END, i)
            if end < 0:
                break
            i = end
            continue
        if char == _SHELL_ESCAPE_CHAR and quote != _SHELL_SINGLE_QUOTE and i + 1 == len(command):
            # A continuation whose next line the row does not carry: a worker log keeps one row
            # per displayed line, so `audit_report.py start \` is read as the words before it.
            break
        if char == _SHELL_ESCAPE_CHAR and quote != _SHELL_SINGLE_QUOTE and i + 1 < len(command):
            if command[i + 1] == _SHELL_LINE_END:
                # A line continuation: bash removes the pair before it reads a word, so a
                # command typed across continued lines is the same line.
                i += 2
                continue
            out.append(_with_literal_marks(command[i:i + 2]))
            previous = _SHELL_ESCAPE_CHAR
            i += 2
            continue
        if char == quote:
            quote = None
        elif quote is None and char in _SHELL_QUOTE_CHARS:
            quote = char
        else:
            openers = () if quote == _SHELL_SINGLE_QUOTE else _SHELL_QUOTED_SUBSTITUTION_OPENERS if quote == _SHELL_DOUBLE_QUOTE else _SHELL_SUBSTITUTION_OPENERS
            opener = next((o for o in openers if command.startswith(o, i)), None)
            close = None
            if opener is not None:
                close = _substitution_end(command, i + len(opener), opener.count("("))
            elif char == _SHELL_BACKTICK and quote != _SHELL_SINGLE_QUOTE:
                close = _backtick_end(command, i + 1)
            if opener is not None or (char == _SHELL_BACKTICK and quote != _SHELL_SINGLE_QUOTE):
                if close is None:
                    out.append(_with_literal_marks(command[i:]))
                    break
                spans.append(command[i:close])
                out.append(_SUBSTITUTION_MARK.format(index=len(spans) - 1))
                previous = _SUBSTITUTION_MARK[-1]
                i = close
                continue
            if quote is None and (char in _SHELL_OPERATOR_RUN_CHARS or char in _SHELL_PARENS or char == _SHELL_LINE_END):
                # A bare newline starts a new command as `;` does, so it is an operator too.
                end = i + 1
                if char in _SHELL_OPERATOR_RUN_CHARS:
                    while end < len(command) and command[end] in _SHELL_OPERATOR_RUN_CHARS:
                        end += 1
                operators.append(command[i:end])
                out.append(" " + _OPERATOR_MARK.format(index=len(operators) - 1) + " ")
                previous = command[end - 1]
                i = end
                continue
        out.append(_with_literal_marks(char))
        previous = char
        i += 1
    return "".join(out), spans, operators


def _with_literal_marks(text: str) -> str:
    """``text`` as :func:`_read_shell_line` keeps the line's own text: each mark character is
    the literal mark."""
    return text.replace(_MARK_CHAR, _LITERAL_MARK)


def _backtick_end(command: str, start: int) -> int | None:
    """The index just past the backtick that closes a span opened before ``start``, escapes
    honoured; None when the line ends first."""
    j = start
    while j < len(command):
        if command[j] == _SHELL_ESCAPE_CHAR:
            j += 2
            continue
        if command[j] == _SHELL_BACKTICK:
            return j + 1
        j += 1
    return None


def _substitution_commands(span: str) -> str | None:
    """The command line a substitution span runs: the text inside its opener and closer, or None
    for the arithmetic form, which runs no command."""
    if span.startswith(_SHELL_ARITHMETIC_OPENER):
        return None
    if span.startswith(_SHELL_BACKTICK):
        return span[1:-1]
    opener = next(o for o in _SHELL_SUBSTITUTION_OPENERS if span.startswith(o))
    return span[len(opener):-1]


def _substitution_end(command: str, start: int, depth: int) -> int | None:
    """The index just past the ``)`` that closes a span opened with ``depth`` parentheses before
    ``start``, quotes and escapes inside the span honoured; None when the line ends first."""
    inner_quote: str | None = None
    j = start
    while j < len(command) and depth > 0:
        c = command[j]
        if c == _SHELL_ESCAPE_CHAR and inner_quote != _SHELL_SINGLE_QUOTE:
            j += 2
            continue
        if inner_quote is not None:
            if c == inner_quote:
                inner_quote = None
        elif c in _SHELL_QUOTE_CHARS:
            inner_quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        j += 1
    return None if depth > 0 else j


def _unfold_marks(word: str, spans: list[str]) -> str:
    """``word`` with each substitution mark replaced by its span and each literal mark by the
    mark character. One pass from the left, so the text of a span is not read for marks."""
    return _WORD_MARK_RE.sub(lambda m: _MARK_CHAR if m.group(1) is None else spans[int(m.group(1))], word)


class _ShellOperator(str):
    """A bare operator token (``&&``, ``;``, ``|``, ``>``, ``(``): one the scan read outside
    quotes. It compares equal to its text; a quoted word that spells the same characters is a
    plain ``str`` and is text."""


def _shell_words_and_spans(command: str) -> tuple[list[str], list[str]]:
    """The command line as the shell's words, quotes honoured and removed, bare operators
    (``&&``, ``||``, ``;``, ``|``, ``&``, ``(``, ``)``, the redirects) as :class:`_ShellOperator`
    words of their own, and the substitution spans the line carries; a line whose quotes do not
    balance, or with a redirect that has no target, yields no words. bash refuses such a line
    and runs nothing of it when the line is one command; a call of more than one command is
    read a command at a time by :func:`_shell_argvs`, which keeps the commands before the one
    that is refused. Comments come off, substitutions fold into their words and operators are
    marked first, in one pass (:func:`_read_shell_line`), so the lexer splits on whitespace
    alone and its own ``#`` commenter is off, as in ``shlex.split``: it would cut a word at a
    ``#`` inside it (``readiness-#2.json``), which bash does not."""
    command, spans, operators = _read_shell_line(command)
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw = list(lexer)
    except ValueError:
        return [], []
    words: list[str] = []
    for word in raw:
        mark = _OPERATOR_MARK_RE.match(word)
        words.append(_ShellOperator(operators[int(mark.group(1))]) if mark else _unfold_marks(word, spans))
    # A redirect that ends a segment with no target (`>#log` once its comment is off) is a
    # command bash refuses, so the line yields no words.
    for index, word in enumerate(words):
        if _is_redirect(word) and (index + 1 == len(words) or isinstance(words[index + 1], _ShellOperator)):
            return [], []
    return words, spans


def _is_redirect(word: str) -> bool:
    return isinstance(word, _ShellOperator) and bool(_SHELL_REDIRECT_RE.match(word))


def _is_control_operator(word: str) -> bool:
    return isinstance(word, _ShellOperator) and (word == _SHELL_LINE_END or set(word) <= _SHELL_OPERATOR_CHARS)


def _commands_in_turn(command: str) -> tuple[list[str], bool, int | None]:
    """The call as the complete commands bash reads from it one after the other, each as text
    that :func:`_shell_words_and_spans` reads; whether the call holds one of the two forms
    below; and the index of the first command with a substitution that does not close here, or
    None. bash reads a call a command at a time: it runs the commands before one that it
    refuses, and nothing from that one on. A command ends at a newline outside quotes and
    substitutions (the quote model of :func:`_read_shell_line`), with two forms that model does
    not know:

    - An ANSI-C quote (``$'...'``), in which ``\\'`` does not close the quote, is written as
      one single-quoted word.
    - A quote in a heredoc body opens nothing. The body lines are not shell text, but each one
      is still a command of its own here when the reader can split it, as the first read takes
      it; a line that the reader cannot split is dropped. In a body whose delimiter has no
      quote, bash runs the substitutions of each line whatever quotes stand around them, so
      those are read too (:func:`_heredoc_body_commands`). The delimiter line of such a body is
      dropped. A body on a line with a shell word (``_HEREDOC_SHELL_PROGRAMS``) can be that
      shell's command lines: it stays in the call as commands, delimiter line included. After
      a body line with a substitution that does not close, the body lines stay whole.

    A substitution that does not close in this model can close in bash: the two forms are not
    modelled inside a substitution. The reader cannot say where such a command ends, so the
    caller takes it and the rest of the call as not read.
    """
    commands: list[str] = []
    out: list[str] = []
    delimiters: list[tuple[str, bool, bool]] = []
    special = False
    open_from: int | None = None
    quote: str | None = None
    previous = _SHELL_LINE_END
    i = 0
    while i < len(command):
        char = command[i]
        if quote is None and char == _SHELL_COMMENT_CHAR and previous in _SHELL_WORD_OPENERS:
            end = command.find(_SHELL_LINE_END, i)
            end = len(command) if end < 0 else end
            out.append(command[i:end])
            i = end
            continue
        if char == _SHELL_ESCAPE_CHAR and quote != _SHELL_SINGLE_QUOTE:
            out.append(command[i:i + 2])
            previous = _SHELL_ESCAPE_CHAR
            i += 2
            continue
        if quote is None and command.startswith(_ANSI_C_QUOTE_OPENER, i):
            end = _ansi_c_quote_end(command, i + len(_ANSI_C_QUOTE_OPENER))
            if end is None:
                out.append(command[i:])
                break
            out.append(_single_quoted(_ansi_c_text(command[i + len(_ANSI_C_QUOTE_OPENER):end - 1])))
            special = True
            previous = _SHELL_SINGLE_QUOTE
            i = end
            continue
        if char == quote:
            quote = None
        elif quote is None and char in _SHELL_QUOTE_CHARS:
            quote = char
        else:
            openers = () if quote == _SHELL_SINGLE_QUOTE else _SHELL_QUOTED_SUBSTITUTION_OPENERS if quote == _SHELL_DOUBLE_QUOTE else _SHELL_SUBSTITUTION_OPENERS
            opener = next((o for o in openers if command.startswith(o, i)), None)
            if opener is not None or (char == _SHELL_BACKTICK and quote != _SHELL_SINGLE_QUOTE):
                close = _backtick_end(command, i + 1) if opener is None else _substitution_end(command, i + len(opener), opener.count(_SUBSTITUTION_DEPTH_CHAR))
                if close is None:
                    open_from = len(commands) if open_from is None else open_from
                    out.append(command[i:])
                    break
                out.append(command[i:close])
                previous = command[close - 1]
                i = close
                continue
            if quote is None and command.startswith(_HERE_STRING_OPERATOR, i):
                out.append(_HERE_STRING_OPERATOR)
                previous = char
                i += len(_HERE_STRING_OPERATOR)
                continue
            if quote is None and command.startswith(_HEREDOC_OPERATOR, i) and not "".join(out).lstrip().startswith(_ARITHMETIC_COMMAND_OPENER):
                end, delimiter, quoted, tabs = _heredoc_delimiter(command, i + len(_HEREDOC_OPERATOR))
                if delimiter is not None:
                    delimiters.append((delimiter, quoted, tabs))
                    special = True
                    out.append(command[i:end])
                    previous = command[end - 1]
                    i = end
                    continue
            if quote is None and char == _SHELL_LINE_END:
                line = "".join(out)
                commands.append(line)
                out = []
                previous = _SHELL_LINE_END
                i += 1
                if delimiters and not _HEREDOC_SHELL_PROGRAMS & {os.path.basename(word) for word in _read_shell_line(line)[0].split()}:
                    for delimiter, quoted, tabs in delimiters:
                        while i < len(command):
                            end = command.find(_SHELL_LINE_END, i)
                            end = len(command) if end < 0 else end
                            body_line = command[i:end]
                            i = end + 1
                            if (body_line.lstrip(_HEREDOC_TAB) if tabs else body_line) == delimiter:
                                break
                            if open_from is not None:
                                # Not read from the open substitution on: the lines stay whole.
                                commands.append(body_line)
                                continue
                            body_commands, is_open = _heredoc_body_commands(body_line, quoted)
                            if is_open:
                                open_from = len(commands)
                            commands.extend(body_commands)
                delimiters = []
                continue
        out.append(char)
        previous = char
        i += 1
    commands.append("".join(out))
    return commands, special, open_from


def _ansi_c_quote_end(command: str, start: int) -> int | None:
    """The index just past the ``'`` that closes an ANSI-C quote opened before ``start``, a
    backslash escaping the character after it; None when the line ends first."""
    j = start
    while j < len(command):
        if command[j] == _SHELL_ESCAPE_CHAR:
            j += 2
            continue
        if command[j] == _SHELL_SINGLE_QUOTE:
            return j + 1
        j += 1
    return None


def _ansi_c_text(body: str) -> str:
    """The text an ANSI-C quote's ``body`` spells, for the escapes of ``_ANSI_C_ESCAPES``."""
    out: list[str] = []
    j = 0
    while j < len(body):
        if body[j] == _SHELL_ESCAPE_CHAR and j + 1 < len(body) and body[j + 1] in _ANSI_C_ESCAPES:
            out.append(_ANSI_C_ESCAPES[body[j + 1]])
            j += 2
            continue
        out.append(body[j])
        j += 1
    return "".join(out)


def _single_quoted(text: str) -> str:
    """``text`` as one single-quoted shell word."""
    return _SHELL_SINGLE_QUOTE + text.replace(_SHELL_SINGLE_QUOTE, _SINGLE_QUOTE_IN_SINGLE_QUOTES) + _SHELL_SINGLE_QUOTE


def _heredoc_body_commands(line: str, quoted: bool) -> tuple[list[str], bool]:
    """The command lines that one heredoc body line gives the second read, and whether a
    substitution of the line does not close on it. The line itself, when the reader can split
    it (the first read takes a body line as a command). In a body whose delimiter has no quote
    (``quoted`` is false), also the null command with the substitutions that bash runs in the
    line and that the reader did not see as its spans, for example behind a quote."""
    words, seen = _shell_words_and_spans(line)
    commands = [line] if words else []
    if quoted:
        return commands, False
    spans = _body_line_substitutions(line)
    if spans is None:
        return [line], True
    if spans and spans != seen:
        commands.append(_WORD_SEPARATOR.join([_NULL_COMMAND, *spans]))
    return commands, False


def _body_line_substitutions(line: str) -> list[str] | None:
    """Each command substitution and backtick pair that bash runs in a heredoc body line whose
    delimiter has no quote. Quotes are text in a body, so they hide none. None when one does
    not close on the line."""
    spans: list[str] = []
    j = 0
    while j < len(line):
        if line[j] == _SHELL_ESCAPE_CHAR:
            j += 2
            continue
        opener = next((o for o in _BODY_SUBSTITUTION_OPENERS if line.startswith(o, j)), None)
        if opener is None and line[j] != _SHELL_BACKTICK:
            j += 1
            continue
        close = _backtick_end(line, j + 1) if opener is None else _substitution_end(line, j + len(opener), opener.count(_SUBSTITUTION_DEPTH_CHAR))
        if close is None:
            return None
        spans.append(line[j:close])
        j = close
    return spans


def _heredoc_delimiter(command: str, start: int) -> tuple[int, str | None, bool, bool]:
    """The index just past the delimiter word of a heredoc whose ``<<`` ends before ``start``;
    the word with its quotes off, or None when no whole word follows; whether the word had a
    quote or an escape, which makes the body plain text; and whether the operator is ``<<-``,
    which strips leading tabs."""
    j = start
    tabs = command.startswith(_HEREDOC_TAB_STRIP, j)
    if tabs:
        j += 1
    while j < len(command) and command[j] in _HEREDOC_BLANKS:
        j += 1
    word: list[str] = []
    quoted = False
    quote: str | None = None
    while j < len(command):
        char = command[j]
        if quote is not None:
            if char == quote:
                quote = None
            elif char == _SHELL_LINE_END:
                break
            else:
                word.append(char)
        elif char in _SHELL_QUOTE_CHARS:
            quote = char
            quoted = True
        elif char == _SHELL_ESCAPE_CHAR and j + 1 < len(command):
            quoted = True
            j += 1
            word.append(command[j])
        elif char in _SHELL_WORD_OPENERS:
            break
        else:
            word.append(char)
        j += 1
    return j, "".join(word) if word and quote is None else None, quoted, tabs


def _second_read_commands(command: str, words: list[str]) -> tuple[list[str], int | None] | None:
    """The commands of the second read of ``command`` and the index of the first one with a
    substitution that does not close (:func:`_commands_in_turn`), or None when the first read,
    which gave ``words``, is all that the line needs. The first read can differ from what bash
    runs in two ways that the second read knows: it gave no words for a line that has text, or
    the line holds a heredoc operator or an ANSI-C quote opener. In such a line a substitution
    that does not close also counts, because one of the two forms can be inside it. For
    another line the two reads have one quote model and give the same argvs."""
    refused = not words and bool(_read_shell_line(command)[0].strip())
    if not refused and _HEREDOC_OPERATOR not in command and _ANSI_C_QUOTE_OPENER not in command:
        return None
    commands, special, open_from = _commands_in_turn(command)
    return (commands, open_from) if refused or special or open_from is not None else None


def _shell_argvs_and_unread(command: str) -> tuple[list[list[str]], list[str]]:
    """:func:`_shell_argvs` of ``command``, and the parts of it that the reader did not read:
    a command that the reader refuses (quotes that do not balance, a redirect with no target,
    a substitution that does not close in a line with a heredoc or an ANSI-C quote) with each
    command after it, at the top of the call and in each command line inside it. The reader
    cannot tell if bash refuses such a part too, so it does not say that nothing ran there."""
    unread: list[str] = []
    argvs = _shell_argvs(command, _unread=unread)
    return argvs, unread


def _shell_argvs_first_read(command: str) -> list[list[str]]:
    """:func:`_shell_argvs` of ``command`` with the first read alone, at each level: for text
    that is not a shell call (an ``execute_code`` program), which a caller reads as before the
    second read."""
    return _shell_argvs(command, _in_turn=False)


def _shell_argvs(
    command: str, _nesting: int = 0, _budget: list[int] | None = None, _unread: list[str] | None = None, _in_turn: bool = True
) -> list[list[str]]:
    """Each segment of a command line as an argv: comments come off and the line is tokenised
    first, so a quoted path is one word and a quoted sentence one word whatever it contains,
    then split where the shell starts a new command. A shell program anywhere on a segment
    (``bash -c``, under ``timeout``, ``nohup`` or an ``env``) carries a command line in the word
    after its ``-c``-style flag, which is read the same way and yields its own argvs; so does
    each command or process substitution and each backtick pair on the line (``OUT=$(python3
    ... --readiness)``, ``cat <(...)``), whose commands the shell runs, and the words after an
    ``eval``, joined by spaces as ``eval`` joins them. A line whose command lines nest deeper
    than ``_MAX_COMMAND_LINE_NESTING`` levels, or need more reading than the line's budget,
    raises :class:`ShellReadLimitError`; ``_nesting``, ``_budget``, ``_unread`` and ``_in_turn``
    are the reader's own.

    The line is read two times when the first read can miss what bash runs
    (:func:`_second_read_commands`). The first read takes the line whole. The second takes it a
    command at a time (:func:`_commands_in_turn`) and stops at the first command that the
    reader refuses; that command and the rest go to ``_unread``. The result is the argvs of the
    first read and each argv that only the second read gives, so a line never has fewer argvs
    than the first read gives it. The second read costs budget too, at each level that needs
    it, so a line can reach the budget sooner than with one read."""
    if _budget is None:
        _budget = [_MAX_READ_CHARS_FACTOR * len(command) + _MAX_READ_CHARS_FLOOR]
    if _nesting > _MAX_COMMAND_LINE_NESTING:
        raise ShellReadLimitError(_NESTING_LIMIT_TEXT)
    _budget[0] -= len(command) + _READ_COST_CHARS
    if _budget[0] < 0:
        raise ShellReadLimitError(_READ_LIMIT_TEXT)
    words, spans = _shell_words_and_spans(command)
    second = _second_read_commands(command, words) if _in_turn else None
    if second is None:
        return _words_argvs(words, spans, _nesting, _budget, _unread, _in_turn)
    commands, open_from = second
    # The first read's inner lines are read again by the second read, so its unread parts are
    # not kept: the second read reports them.
    argvs = _words_argvs(words, spans, _nesting, _budget, None, _in_turn)
    _budget[0] -= len(command) + _READ_COST_CHARS
    if _budget[0] < 0:
        raise ShellReadLimitError(_READ_LIMIT_TEXT)
    first = list(argvs)
    for index, line in enumerate(commands):
        words, spans = _shell_words_and_spans(line)
        if index == open_from or not words:
            if index == open_from or _read_shell_line(line)[0].strip():
                if _unread is not None:
                    _unread.append(_SHELL_LINE_END.join(commands[index:]))
                break
            continue
        for argv in _words_argvs(words, spans, _nesting, _budget, _unread, _in_turn):
            if argv in first:
                first.remove(argv)
            else:
                argvs.append(argv)
    return argvs


def _words_argvs(words: list[str], spans: list[str], nesting: int, budget: list[int], unread: list[str] | None, in_turn: bool) -> list[list[str]]:
    """The argvs of one read of a line (:func:`_shell_argvs`): its own segments, then the
    command lines inside it."""
    argvs: list[list[str]] = []
    argv: list[str] = []
    for word in [*words, _ShellOperator(";")]:
        if _is_control_operator(word):
            if argv:
                argvs.append(argv)
            argv = []
            continue
        argv.append(word)
    for argv in list(argvs):
        index = _command_word_index(argv)
        if index is not None and argv[index] == _SHELL_EVAL:
            argvs.extend(_shell_argvs(_SHELL_EVAL_WORD_SEPARATOR.join(argv[index + 1:]), nesting + 1, budget, unread, in_turn))
            continue
        if index is None or os.path.basename(argv[index]) not in _SHELL_PROGRAMS_WITH_COMMAND_STRING:
            continue
        for flag_index in range(index + 1, len(argv) - 1):
            flag = argv[flag_index]
            if flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]:
                argvs.extend(_shell_argvs(argv[flag_index + 1], nesting + 1, budget, unread, in_turn))
                break
    for span in spans:
        inner = _substitution_commands(span)
        if inner is not None:
            argvs.extend(_shell_argvs(inner, nesting + 1, budget, unread, in_turn))
    return argvs


def _command_word_index(
    argv: list[str], script: str | None = None, matches: Callable[[list[str], int], bool] | None = None
) -> int | None:
    """The index of the word the shell runs as this argv's command, when it is one the reader
    knows: a shell program, an interpreter, ``eval``, ``script`` itself, or a word at which
    ``matches(argv, index)`` holds (the GitHub-write attribution asks for the commands that
    create a branch or a pull request this way). Leading ``NAME=value``
    assignments and reserved words are skipped, and so is a redirect with its target, which bash
    takes anywhere on the line. After an exec wrapper (``timeout``, ``sudo``, ``env`` ...) only
    the wrapper's own words are transparent: a flag, the word after a flag (its value, ``-u user``
    or ``-s KILL``, unless that word is a flag itself, ``sudo -E -u``; which flags take one is not tabled, so the word after a bare flag is read as
    its value even when it is a command, the one over-credit this leaves), a duration or count
    (``timeout 600``), an expansion the reader never resolves (``timeout $T``), ``--``, which
    ends its options, and the one positional ``flock`` and ``chroot`` take ahead of their command
    (``_SHELL_WRAPPER_POSITIONALS``). The first word that is none of those is the command the wrapper runs: a
    shell, an interpreter or the script is returned, and any other word (``ls``, ``echo``,
    ``find``, ``grep``) is a command of its own, behind a wrapper as at the front of the argv,
    and None is returned: nothing it names as an argument runs."""
    after_wrapper = False
    takes_value = False
    positionals = 0
    for index, word in enumerate(argv):
        base = os.path.basename(word)
        if matches is not None and matches(argv, index):
            return index
        if base in _SHELL_PROGRAMS_WITH_COMMAND_STRING or _INTERPRETER_RE.match(base) or (script and base == script) or word == _SHELL_EVAL:
            return index
        if _SHELL_ASSIGNMENT_RE.match(word) or word in _SHELL_RESERVED_WORDS:
            takes_value = False
            continue
        if base in _SHELL_EXEC_WRAPPERS:
            after_wrapper, takes_value = True, False
            positionals = _SHELL_WRAPPER_POSITIONALS.get(base, 0)
            continue
        if _is_redirect(word) or (word.isdigit() and index + 1 < len(argv) and _is_redirect(argv[index + 1])):
            # A redirect takes the next word as its target; a bare fd number ahead of one is its own.
            takes_value = not word.isdigit()
            continue
        if after_wrapper and word.startswith(_SHELL_FLAG_PREFIX):
            # A flag in its own right, never the previous flag's value (`sudo -E -u hermes`); `--`
            # ends the options and takes nothing.
            takes_value = word != _SHELL_OPTIONS_END
            continue
        if takes_value:
            takes_value = False
            continue
        if not after_wrapper:
            return None
        if _SHELL_COUNT_RE.match(word) or word.startswith(_SHELL_EXPANSION_PREFIX):
            continue
        if positionals:
            positionals -= 1
            continue
        return None
    return None


def _typed_call_may_join(command: str) -> bool:
    """Whether a typed command, as the pod stores it, may hold more than one line, which a card
    log row joins with a space: a newline outside quotes once the ends are trimmed, a backslash
    before a blank (an escaped blank, or what the pod's scrub leaves of an escaped backslash at
    a line end once it folds the continuation), or a line the shell reader cannot split."""
    if _ESCAPED_BLANK_RE.search(command):
        return True
    words, _ = _shell_words_and_spans(command.strip())
    if not words:
        return bool(command.strip())
    return any(isinstance(word, _ShellOperator) and word == _SHELL_LINE_END for word in words)


def _holds_heredoc(command: str) -> bool:
    """Whether a typed call's text holds a heredoc operator. The argv reader splits a heredoc
    body into one argv for each line, so a line written into a file would read as a run."""
    return bool(_HEREDOC_OPERATOR_RE.search(command))


def _can_read_a_word_back(line: str) -> bool:
    """Whether ``line``, as :func:`_read_shell_line` keeps it, can read a removed word back: an
    ANSI-C quote, a read of the last argument or of a variable through another, or a braced
    expansion other than a plain name or a default that assigns nothing."""
    if _has_ansi_c_quote(line) or _READ_BACK_RE.search(line):
        return True
    start = line.find(_BRACED_EXPANSION_OPEN)
    while start != -1:
        if not _PLAIN_BRACED_EXPANSION_RE.match(line, start):
            return True
        start = line.find(_BRACED_EXPANSION_OPEN, start + 1)
    return False


def _has_ansi_c_quote(command: str) -> bool:
    """Whether the line opens an ANSI-C quote (``$'…'``) outside other quotes: the shell reader
    keeps its escapes undecoded, so ``$'\\x24_'`` would not read as ``$_``."""
    quote: str | None = None
    i = 0
    while i < len(command):
        char = command[i]
        if char == _SHELL_ESCAPE_CHAR and quote != _SHELL_SINGLE_QUOTE:
            i += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
        elif char == _SHELL_EXPANSION_PREFIX and command.startswith(_SHELL_SINGLE_QUOTE, i + 1):
            return True
        elif char in _SHELL_QUOTE_CHARS:
            quote = char
        i += 1
    return False


def _without_grep_patterns(command: str) -> str | None:
    """The command line as its shell words joined by spaces, with the grep's pattern argument
    removed (the first positional word, or each ``-e``/``--regexp=`` value when one is given);
    None when the line is not one such grep. The line is a ``grep``, ``egrep`` or ``fgrep`` named
    bare (no path, wrapper, assignment or reserved word ahead) and nothing else: no control
    operator (``;``, ``&``, ``&&``, ``||``, ``|``, a newline, a parenthesis), so no later command
    typed on the line can read the removed word back from the shell; no redirect but ``>`` to ``/dev/null``; no
    comment, since a card log row joins the next typed line onto it; and not a collapsed chain
    (`` + N command(s)``); and no substitution anywhere on it. Options end at the first operand.
    Every other word stays: the options, the file operands and any text the rendering joined
    onto the row. Quotes come off, as the shell reads the line; a line bash refuses yields None.
    A card log row that joined a second typed line onto the grep is one row here, so the line
    is also refused when a later word could read the removed word back
    (:func:`_can_read_a_word_back`)."""
    if _COLLAPSED_CHAIN_RE.search(command):
        return None
    # The reader keeps every quoted, escaped or in-word `#` and a span's text whole, so a `#` it
    # did not hand back opened a comment.
    kept, kept_spans, _ = _read_shell_line(command)
    if kept.count(_COMMENT_CHAR) + sum(span.count(_COMMENT_CHAR) for span in kept_spans) < command.count(_COMMENT_CHAR):
        return None
    words, spans = _shell_words_and_spans(command)
    if not words or isinstance(words[0], _ShellOperator) or words[0] not in _GREP_PROGRAMS:
        return None
    # A substitution anywhere runs a command, and can run one the removed word set up
    # (`grep "${X:=<cmd>}" $(eval $X)`).
    if spans:
        return None
    # The grep alone: a later command could read the removed word back from the shell (`$_`,
    # `declare -p _`, `jobs` behind a `&`), and a pipe hands it the grep's output.
    if any(_is_control_operator(word) for word in words):
        return None
    end = len(words)
    patterns: list[int] = []
    positionals: list[int] = []
    given = False
    options_done = False
    i = 1
    while i < end:
        word = words[i]
        if isinstance(word, _ShellOperator):
            # `>` to /dev/null is the one operator the grep's segment may hold; `>|` and every
            # other redirect are refused.
            if word != _GREP_STDOUT_REDIRECT or i + 1 >= end or words[i + 1] != _GREP_OUTPUT_SINK:
                return None
            i += 2
            continue
        if options_done or word == "-" or not word.startswith(_SHELL_FLAG_PREFIX):
            # Options end at the first operand, as POSIX grep reads them, so an `-e` after a file
            # operand is graded rather than removed.
            positionals.append(i)
            options_done = True
        elif word == _SHELL_OPTIONS_END:
            options_done = True
        elif word.startswith(_GREP_LONG_OPTION_PREFIX):
            if word.startswith(_GREP_LONG_PATTERN_PREFIX):
                patterns.append(i)
            given = given or word.startswith((_GREP_LONG_PATTERN_PREFIX, _GREP_LONG_PATTERN_FILE_PREFIX))
        else:
            for j, flag in enumerate(word[1:], start=1):
                if flag not in _GREP_SHORT_VALUE_OPTIONS:
                    continue
                given = given or flag in (_GREP_PATTERN_OPTION, _GREP_PATTERN_FILE_OPTION)
                if word[j + 1:]:
                    value = i
                elif i + 1 < end and not isinstance(words[i + 1], _ShellOperator):
                    value = i + 1
                    i += 1
                else:
                    break
                if flag == _GREP_PATTERN_OPTION:
                    patterns.append(value)
                break
        i += 1
    removed = set(patterns) if given else set(positionals[:1])
    if not removed:
        return None
    # A card log row joins the next typed line onto the grep, so a word after it can be a
    # command that reads the removed word back: from a variable the line assigned, as the last
    # argument (`$_`), or through an ANSI-C quote whose escapes spell either. Such a row is
    # graded whole (#5331).
    if _can_read_a_word_back(kept):
        return None
    return " ".join(word for k, word in enumerate(words) if k not in removed)
