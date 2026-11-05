"""The shell command-line reader the bench's verifiers share.

A worker's or an agent's terminal command is read the way bash reads it --
comments off, quotes honoured, substitutions folded into their words, and
operators only outside quotes -- and then split into one argv per command.
The ``worker_commands`` verifier (``verifiers``) and the GitHub-write
attribution (``github_writes``) both read calls through it, so the two
cannot disagree on where a command starts. It lives in its own module
because ``verifiers`` imports ``github_writes``.
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
_OPERATOR_MARK = "\x00O{index}\x00"
_OPERATOR_MARK_RE = re.compile(r"^\x00O(\d+)\x00$")
_SHELL_PROGRAMS_WITH_COMMAND_STRING = ("sh", "bash", "zsh", "dash")
# `eval` runs its words, joined by spaces, as a command line of its own (`eval "git push ..."`).
_SHELL_EVAL = "eval"
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
_SUBSTITUTION_MARK_RE = re.compile(r"\x00S(\d+)\x00")
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
    off, because afterwards a quoted ``#1.json`` and a bare one look the same."""
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
            out.append(command[i:i + 2])
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
                    out.append(command[i:])
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
        out.append(char)
        previous = char
        i += 1
    return "".join(out), spans, operators


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


def _unfold_substitutions(word: str, spans: list[str]) -> str:
    return _SUBSTITUTION_MARK_RE.sub(lambda m: spans[int(m.group(1))], word)


class _ShellOperator(str):
    """A bare operator token (``&&``, ``;``, ``|``, ``>``, ``(``): one the scan read outside
    quotes. It compares equal to its text; a quoted word that spells the same characters is a
    plain ``str`` and is text."""


def _shell_words_and_spans(command: str) -> tuple[list[str], list[str]]:
    """The command line as the shell's words, quotes honoured and removed, bare operators
    (``&&``, ``||``, ``;``, ``|``, ``&``, ``(``, ``)``, the redirects) as :class:`_ShellOperator`
    words of their own, and the substitution spans the line carries; a line whose quotes do not
    balance is one bash refuses and runs nothing of, so it yields no words and no run is
    credited from it. Comments come off, substitutions fold into their words and operators are
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
        words.append(_ShellOperator(operators[int(mark.group(1))]) if mark else _unfold_substitutions(word, spans))
    # A redirect that ends a segment with no target (`>#log` once its comment is off) is a line
    # bash refuses, so nothing on it runs.
    for index, word in enumerate(words):
        if _is_redirect(word) and (index + 1 == len(words) or isinstance(words[index + 1], _ShellOperator)):
            return [], []
    return words, spans


def _is_redirect(word: str) -> bool:
    return isinstance(word, _ShellOperator) and bool(_SHELL_REDIRECT_RE.match(word))


def _is_control_operator(word: str) -> bool:
    return isinstance(word, _ShellOperator) and (word == _SHELL_LINE_END or set(word) <= _SHELL_OPERATOR_CHARS)


def _shell_argvs(command: str) -> list[list[str]]:
    """Each segment of a command line as an argv: comments come off and the line is tokenised
    first, so a quoted path is one word and a quoted sentence one word whatever it contains,
    then split where the shell starts a new command. A shell program anywhere on a segment
    (``bash -c``, under ``timeout``, ``nohup`` or an ``env``) carries a command line in the word
    after its ``-c``-style flag, which is read the same way and yields its own argvs; so does
    each command or process substitution and each backtick pair on the line (``OUT=$(python3
    ... --readiness)``, ``cat <(...)``), whose commands the shell runs, and the words after an
    ``eval``, joined by spaces as ``eval`` joins them."""
    words, spans = _shell_words_and_spans(command)
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
            argvs.extend(_shell_argvs(" ".join(argv[index + 1:])))
            continue
        if index is None or os.path.basename(argv[index]) not in _SHELL_PROGRAMS_WITH_COMMAND_STRING:
            continue
        for flag_index in range(index + 1, len(argv) - 1):
            flag = argv[flag_index]
            if flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]:
                argvs.extend(_shell_argvs(argv[flag_index + 1]))
                break
    for span in spans:
        inner = _substitution_commands(span)
        if inner is not None:
            argvs.extend(_shell_argvs(inner))
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
