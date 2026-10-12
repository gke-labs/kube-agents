"""The commands inside one terminal call, for pattern checks over worker commands.

A worker's terminal call is a shell script of any shape: several lines, joins,
a heredoc, a command substitution, a string handed to ``sh -c``. A pattern
check that reads the whole call as one string either misses a command behind
some shell syntax or fires on prose that happens to contain a command word.
:func:`segments` reads the call once, the way a shell does, and returns every
simple command in it as its own string, starting at the command word.

What counts as a segment:

- each simple command on a line, split at ``;``, ``&&``, ``||``, ``|``, ``&``
  and parentheses, with a backslash-newline joined to the next line;
- the command inside ``$(...)`` and backticks, wherever it sits, a double-quoted
  string included;
- the script a shell runs from a ``-c`` string (``sh``, ``bash``, ``zsh``,
  ``dash``) or ``eval``, segmented in turn; the ``-c`` string of a ``python``
  command, as one segment of Python text;
- what ``kubectl exec``, ``kubectl run`` and ``kubectl debug`` run after ``--``;
- the lines of a heredoc that is executed: fed to an interpreter on its opener
  line, written to a ``.sh``/``.py`` path, or written to a path that a later
  command in the same call runs. Any other heredoc body is content (a manifest,
  a pull-request body) and is not a command.

What is stripped before the command word: wrapper words and their options
(``env``, ``sudo -u x``, ``time``, ``timeout 30``, ``nice -n 5``, ``nohup``,
``xargs -n1``, ``command``, ``builtin``, ``exec``, ``if``, ``!``, ``while``,
``until``, ``then``, ``else``, ``elif``, ``do``, ``{``) and leading ``VAR=value``
assignments. A command word that is ``$(which x)``, ``$(command -v x)`` or
``$(type -p x)`` reads as ``x``. Shell keywords that close a block (``fi``,
``done``, ``esac``, ``}``) are not commands and yield nothing.

What is not understood, stated so a case is not written against it: a command
behind a shell variable (``$K scale``), text piped into an interpreter
(``echo ... | sh``), a here-string, aliases and functions, and anything after a
quote that never closes. The walk is linear in the call's length.
"""

from __future__ import annotations

import re

# Shell words that precede the command they run, with how many option tokens
# follow: options are any ``-x`` tokens, and the words below take one more
# value token after the named options.
_WRAPPERS = frozenset(
    {"env", "sudo", "time", "nohup", "nice", "timeout", "xargs", "command", "builtin", "exec",
     "if", "!", "while", "until", "then", "else", "elif", "do", "{"}
)
# Options of a wrapper that take a value token of their own.
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-T", "-U"},
    "nice": {"-n"},
    "xargs": {"-I", "-n", "-L", "-P", "-s", "-d", "-E", "-a"},
    "env": {"-u", "-C", "-S"},
    "timeout": {"-s", "-k"},
}
# ``timeout`` takes a duration after its options.
_WRAPPER_POSITIONAL = {"timeout": 1}
# Words that end a block; never a command.
_BLOCK_CLOSERS = frozenset({"fi", "done", "esac", "}", ")", "then", "else", "do"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_PYTHONS = re.compile(r"^python[\d.]*$")
_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "source", "."})
_KUBECTL_PASSTHROUGH = frozenset({"exec", "run", "debug"})
_SCRIPT_SUFFIXES = (".sh", ".py", ".bash")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_WHICH_RE = re.compile(r"^(?:which|command\s+-v|type\s+-p)\s+(\S+)$")
_HEREDOC_RE = re.compile(r"<<(-?)\s*(?:'([^']*)'|\"([^\"]*)\"|\\?([A-Za-z0-9_]+))")
_REDIRECT_TARGET_RE = re.compile(r"(?:^|\s)>{1,2}\s*(\S+)")


def segments(command: str) -> list[str]:
    """Every simple command in ``command``, each from its command word on."""
    out: list[str] = []
    _walk(command, out, depth=0)
    return out


# Nesting beyond this is not shell a worker writes; it bounds a pathological
# input rather than any real one.
_MAX_DEPTH = 8


def _walk(text: str, out: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH or not text:
        return
    pieces, nested, heredocs = _scan(text)
    for inner in nested:
        _walk(inner, out, depth + 1)
    executed = _executed_paths(pieces)
    for opener, target, body in heredocs:
        if _heredoc_is_code(opener, target, executed):
            _walk(body, out, depth + 1)
    for piece in pieces:
        _emit(piece, out, depth)


def _emit(piece: str, out: list[str], depth: int) -> None:
    tokens = _tokens(piece)
    index = _command_index(tokens)
    if index is None:
        return
    start, _, word = tokens[index]
    unquoted = _unquote(word)
    inner = _substituted_command(word)
    if inner is not None:
        which = _WHICH_RE.match(inner.strip())
        if which:
            unquoted = which.group(1)
            segment = unquoted + piece[tokens[index][1]:]
        else:
            return
    else:
        segment = piece[start:]
    if unquoted in _BLOCK_CLOSERS:
        return
    out.append(segment.strip())
    base = unquoted.rsplit("/", 1)[-1]
    rest = tokens[index + 1 :]
    if base in _SHELLS or base == "eval" or _PYTHONS.match(base):
        script = _dash_c_script(base, rest, piece)
        if script is not None:
            if base in _SHELLS or base == "eval":
                _walk(script, out, depth + 1)
            else:
                out.append(script.strip())
    if base == "kubectl" and rest and _unquote(rest[0][2]) in _KUBECTL_PASSTHROUGH:
        for position, (_, end, token) in enumerate(rest):
            if token == "--":
                tail = piece[end:].strip()
                if tail:
                    _walk(tail, out, depth + 1)
                break


def _dash_c_script(base: str, rest: list[tuple[int, int, str]], piece: str) -> str | None:
    if base == "eval":
        return " ".join(_unquote(t) for _, _, t in rest) if rest else None
    for position, (_, _, token) in enumerate(rest):
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            if position + 1 < len(rest):
                return _unquote(rest[position + 1][2])
            return None
        if not token.startswith("-"):
            return None
    return None


def _command_index(tokens: list[tuple[int, int, str]]) -> int | None:
    i = 0
    while i < len(tokens):
        word = _unquote(tokens[i][2])
        if _ASSIGNMENT_RE.match(tokens[i][2]) and not tokens[i][2].startswith(("'", '"')):
            i += 1
            continue
        if word in _WRAPPERS:
            name = word
            i += 1
            values = _WRAPPER_VALUE_OPTIONS.get(name, set())
            while i < len(tokens) and tokens[i][2].startswith("-") and tokens[i][2] != "--":
                option = tokens[i][2]
                i += 1
                if option in values and i < len(tokens):
                    i += 1
            if i < len(tokens) and tokens[i][2] == "--":
                i += 1
            for _ in range(_WRAPPER_POSITIONAL.get(name, 0)):
                if i < len(tokens):
                    i += 1
            continue
        return i
    return None


def _substituted_command(word: str) -> str | None:
    """The inner command when ``word`` is wholly a ``$(...)`` or backtick substitution."""
    if word.startswith("$(") and word.endswith(")"):
        return word[2:-1]
    if len(word) >= 2 and word[0] == "`" and word[-1] == "`":
        return word[1:-1]
    return None


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        body = token[1:-1]
        return body.replace('\\"', '"') if token[0] == '"' else body
    return token


def _tokens(piece: str) -> list[tuple[int, int, str]]:
    """Whitespace-separated words of one simple command, quotes and substitutions kept whole."""
    tokens: list[tuple[int, int, str]] = []
    i, n = 0, len(piece)
    while i < n:
        while i < n and piece[i] in " \t":
            i += 1
        if i >= n:
            break
        start = i
        while i < n and piece[i] not in " \t":
            c = piece[i]
            if c == "'":
                j = piece.find("'", i + 1)
                i = n if j < 0 else j + 1
            elif c == '"':
                i = _skip_double(piece, i)
            elif c == "$" and piece.startswith("$(", i):
                i = _skip_parens(piece, i + 1)
            elif c == "`":
                j = piece.find("`", i + 1)
                i = n if j < 0 else j + 1
            elif c == "\\" and i + 1 < n:
                i += 2
            else:
                i += 1
        tokens.append((start, i, piece[start:i]))
    return tokens


def _skip_double(text: str, i: int) -> int:
    """Index after the double-quoted string opening at ``i`` (unterminated: end of text)."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == '"':
            return i + 1
        elif c == "$" and text.startswith("$(", i):
            i = _skip_parens(text, i + 1)
        elif c == "`":
            j = text.find("`", i + 1)
            i = n if j < 0 else j + 1
        else:
            i += 1
    return n


def _skip_parens(text: str, i: int) -> int:
    """Index after the parenthesised group opening at ``i`` (unterminated: end of text)."""
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif c == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"':
            i = _skip_double(text, i)
        elif c == "\\" and i + 1 < n:
            i += 2
        else:
            i += 1
    return n


def _scan(text: str) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
    """Split ``text`` into simple-command pieces, nested substitution texts and heredocs.

    Returns ``(pieces, nested, heredocs)``; a heredoc is ``(opener piece,
    redirect target or "", body)``.
    """
    pieces: list[str] = []
    nested: list[str] = []
    heredocs: list[tuple[str, str, str]] = []
    pending: list[tuple[str, bool]] = []  # (tag, strip leading tabs)
    buf: list[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        piece = "".join(buf).strip()
        buf.clear()
        if piece:
            pieces.append(piece)

    def end_of_line(pos: int) -> int:
        """Consume heredoc bodies that start at ``pos`` (just after a newline)."""
        nonlocal pending
        opener = "".join(buf).strip() or (pieces[-1] if pieces else "")
        for tag, strip_tabs in pending:
            body_lines: list[str] = []
            while pos < n:
                nl = text.find("\n", pos)
                line = text[pos:] if nl < 0 else text[pos:nl]
                pos = n if nl < 0 else nl + 1
                candidate = line.lstrip("\t") if strip_tabs else line
                if candidate.strip() == tag:
                    break
                body_lines.append(candidate if strip_tabs else line)
            target = _REDIRECT_TARGET_RE.search(opener)
            heredocs.append((opener, _unquote(target.group(1)) if target else "", "\n".join(body_lines)))
        pending = []
        return pos

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            i += 2
        elif c == "\\" and i + 1 < n:
            buf.append(text[i : i + 2])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j + 1
            buf.append(text[i:j])
            i = j
        elif c == '"':
            j = _skip_double(text, i)
            _collect_nested(text[i + 1 : j - 1] if j - 1 > i else "", nested)
            buf.append(text[i:j])
            i = j
        elif c == "$" and text.startswith("$(", i):
            j = _skip_parens(text, i + 1)
            nested.append(text[i + 2 : j - 1] if text[j - 1 : j] == ")" else text[i + 2 : j])
            buf.append(text[i:j])
            i = j
        elif c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                buf.append(text[i:])
                i = n
            else:
                nested.append(text[i + 1 : j])
                buf.append(text[i : j + 1])
                i = j + 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "<" and text.startswith("<<", i) and not text.startswith("<<<", i):
            m = _HEREDOC_RE.match(text, i)
            if m:
                tag = m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4)
                pending.append((tag, m.group(1) == "-"))
                buf.append(text[i : m.end()])
                i = m.end()
            else:
                buf.append(text[i : i + 2])
                i += 2
        elif c == "\n":
            if pending:
                i = end_of_line(i + 1)
                flush()
            else:
                flush()
                i += 1
        elif c == "&" and (text[i - 1 : i] in "<>" or text.startswith("&>", i)):
            # A redirection (``2>&1``, ``&>file``), not a background join.
            buf.append(c)
            i += 1
        elif c in ";|&()":
            if c == "&" and text.startswith("&&", i) or c == "|" and text.startswith("||", i):
                flush()
                i += 2
            elif c == ";" and text.startswith(";;", i):
                flush()
                i += 2
            else:
                flush()
                i += 1
        else:
            buf.append(c)
            i += 1
    if pending:
        end_of_line(n)
    flush()
    return pieces, nested, heredocs


def _collect_nested(inner: str, nested: list[str]) -> None:
    """Substitutions inside a double-quoted string."""
    i, n = 0, len(inner)
    while i < n:
        if inner.startswith("$(", i):
            j = _skip_parens(inner, i + 1)
            nested.append(inner[i + 2 : j - 1] if inner[j - 1 : j] == ")" else inner[i + 2 : j])
            i = j
        elif inner[i] == "`":
            j = inner.find("`", i + 1)
            if j < 0:
                break
            nested.append(inner[i + 1 : j])
            i = j + 1
        elif inner[i] == "\\":
            i += 2
        else:
            i += 1


def _executed_paths(pieces: list[str]) -> set[str]:
    """Paths a piece runs: ``bash p``, ``source p``, ``. p``, ``python p``, ``./p``, ``chmod +x p``."""
    paths: set[str] = set()
    for piece in pieces:
        tokens = _tokens(piece)
        index = _command_index(tokens)
        if index is None:
            continue
        word = _unquote(tokens[index][2])
        base = word.rsplit("/", 1)[-1]
        args = [_unquote(t) for _, _, t in tokens[index + 1 :] if not t.startswith("-")]
        if base in _INTERPRETERS or _PYTHONS.match(base) or word == "chmod":
            paths.update(args)
        elif word.startswith(("./", "/")):
            paths.add(word)
            paths.add(word[2:] if word.startswith("./") else word)
    return paths


def _heredoc_is_code(opener: str, target: str, executed: set[str]) -> bool:
    if target.endswith(_SCRIPT_SUFFIXES) or target in executed or ("./" + target) in executed:
        return True
    for piece in _scan(opener)[0]:
        tokens = _tokens(piece)
        index = _command_index(tokens)
        if index is None:
            continue
        base = _unquote(tokens[index][2]).rsplit("/", 1)[-1]
        if base in _INTERPRETERS or _PYTHONS.match(base):
            return True
    return False
