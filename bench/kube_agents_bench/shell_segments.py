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
- the command inside ``$(...)`` and backticks, wherever it sits: a double-quoted
  string, or the body of a heredoc whose tag is unquoted, which the shell
  expands;
- the script a shell runs from a ``-c`` string (``sh``, ``bash``, ``zsh``,
  ``dash``, ``ksh``) or ``eval``, segmented in turn; the ``-c`` string of a
  ``python`` command, as one segment of Python text;
- what ``kubectl exec``, ``kubectl run`` and ``kubectl debug`` run after ``--``,
  global flags before the verb included;
- the text an ``echo`` or ``printf`` writes to a file a later command in the
  same call runs (quoted or bare arguments; a ``printf`` format with no
  further argument is the text), or that is piped into such a ``tee``;
- the body of a heredoc that is executed: fed to a shell or ``python`` on its
  opener line (the whole line, pipes included), written to a ``.sh``/``.py``
  path, or written (by ``>``, ``tee`` or ``dd of=``) to a path that a later
  command in the same call runs or marks executable. A shell body is segmented
  in turn; a Python body is one segment of Python text. Any other heredoc body
  is content (a manifest, a pull-request body) and is not a command.

A segment starts at its command word, normalised: wrapper words and their
options (``env``, ``sudo -u x``, ``time``, ``timeout 30``, ``nice -n 5``,
``nohup``, ``xargs -n1``, ``command``, ``builtin``, ``exec``, ``if``, ``!``,
``while``, ``until``, ``then``, ``else``, ``elif``, ``do``, ``{``) and leading
``VAR=value`` assignments are stripped; a quoted or backslashed command word
(``"kubectl"``, ``\\kubectl``) reads bare; ``$(which x)``, ``$(command -v x)``
and ``$(type -p x)`` read as ``x``. Shell keywords that close a block (``fi``,
``done``, ``esac``, ``}``) are not commands and yield nothing.

Quoted text does not open a segment of its own, so a pattern anchored on the
command word never sees a quoted argument; a pattern that must float (Python
text in a ``-c`` string) still does. Comments and content heredocs yield
nothing at all.

What is not understood, stated so a case is not written against it: a command
behind a shell variable (``$K scale``), text piped into an interpreter
(``echo ... | sh``) or handed as an argument to one other than a shell or
Python (``perl -e``, ``awk 'BEGIN{system(...)}'``), a here-string's content, a ``printf`` format whose
directives are filled from later arguments, aliases and functions, a script
written in one call and run in a later one, and anything after a quote that
never closes. A content heredoc whose opener line also carries an interpreter
joined by ``&&`` or ``;`` is read as that interpreter's body. The walk is
linear in the call's length.
"""

from __future__ import annotations

import re

# Shell words that precede the command they run. Options are any ``-x``
# tokens; the named options take one value token more; ``timeout`` takes a
# duration after its options.
_WRAPPERS = frozenset(
    {"env", "sudo", "doas", "setsid", "busybox", "chroot", "time", "nohup", "nice", "ionice", "stdbuf",
     "taskset", "watch", "timeout", "xargs", "command", "builtin", "exec",
     "if", "!", "while", "until", "then", "else", "elif", "do", "{"}
)
# kubectl's boolean global flags, which take no value token: a copy of the command
# policy's ``_KUBECTL_BOOLEAN_FLAGS`` (agents/platform/scripts/command_policy.py),
# which the segmenter suite compares it with.
_KUBECTL_BOOLEAN_FLAGS = frozenset(
    {"-h", "--help", "--insecure-skip-tls-verify", "--disable-compression", "--match-server-version", "--warnings-as-errors"}
)
# ``find … -exec <command> {} \;`` runs its argument for every match.
_FIND_EXEC_OPTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_EXEC_END = frozenset({";", "\;", "+"})
# Escapes ``echo -e``, ``printf`` and ``$'…'`` expand before a script is written.
_ESCAPES = (("\\n", "\n"), ("\\t", "\t"), ("\\r", "\r"))
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-T", "-U", "--user", "--group"},
    "doas": {"-u", "-C"},
    "taskset": {"-c", "-p", "--cpu-list"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "watch": {"-n", "-d", "--interval"},
    "xargs": {"-I", "-n", "-L", "-P", "-s", "-d", "-E", "-a", "--max-args", "--max-procs", "--delimiter", "--replace"},
    "env": {"-u", "-C"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
}
_WRAPPER_POSITIONAL = {"timeout": 1, "chroot": 1}
# taskset's first form is a mask before the command (``taskset 0x1 cmd``).
_TASKSET_MASK_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")
# Words that end a block; never a command.
_BLOCK_CLOSERS = frozenset({"fi", "done", "esac", "}", ")"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "su"})
_PYTHONS = re.compile(r"^python[\d.]*$")
_SHELL_RUNNERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "source", "."})
# Short options of an interpreter that take a value token, read before a ``-c``;
# the value may also be attached (``-Werror``, ``-Xdev``).
# per interpreter: a shell's ``-O shopt`` and ``-m`` (monitor) are not Python's
# ``-O`` (optimise, no value) and ``-m module``.
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O"})
_SU_VALUE_OPTIONS = _SHELL_VALUE_OPTIONS | {"-s", "-g", "-G"}
_PYTHON_VALUE_OPTIONS = frozenset({"-W", "-X", "-m"})
_INTERPRETER_LONG_VALUE_OPTIONS = frozenset({"--rcfile", "--init-file"})
# ``echo``/``printf`` options that precede the text they print.
_WRITER_OPTIONS = frozenset({"-e", "-E", "-n", "-v", "-ne", "-en"})
# A chmod mode that adds execute: ``+x``, ``u+x``, ``a=rx``, ``755``, ``0700``.
_CHMOD_EXEC_RE = re.compile(r"[+=][^,]*[xX]|^[0-7]{0,3}[1357]$|^[0-7]*[1357][0-7]{2}$")
_KUBECTL_PASSTHROUGH = frozenset({"exec", "run", "debug"})
# Commands whose quoted arguments become a file's content.
_WRITERS = frozenset({"echo", "printf", "tee"})
_PYTHON_SUFFIX = ".py"
_SHELL_SUFFIXES = (".sh", ".bash")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_WHICH_RE = re.compile(r"^(?:which|command\s+-v|type\s+-p)\s+(\S+)$")
# ``<<`` or ``<<-``, then a tag: quoted, backslashed, or bare; a bare tag is a
# shell word, and only a bare tag's body is expanded.
_HEREDOC_RE = re.compile(r"<<(-?)\s*(?:'([^']*)'|\"([^\"]*)\"|(\\)?([^\s;&|<>()'\"\\]+))")
_REDIRECT_TARGET_RE = re.compile(r"(?:^|[\s'\"])>{1,2}\s*(\S+)")
_ATTACHED_REDIRECT_RE = re.compile(r"^('[^']*'|\"[^\"]*\")>")
_DD_TARGET_RE = re.compile(r"(?:^|\s)of=(\S+)")
# Nesting beyond this is not shell a worker writes; it bounds a pathological
# input rather than any real one.
_MAX_DEPTH = 8
# How a heredoc body is read once it is known to be executed.
_KIND_SHELL = "shell"
_KIND_PYTHON = "python"
# Prefix the walk puts on a Python segment (a ``-c`` string, an executed Python
# heredoc or written script); ``segments`` strips it, ``typed_segments`` reads it.
_PYTHON_MARK = "\x00python\x00"


def segments(command: str) -> list[str]:
    """Every simple command in ``command``, each from its command word on."""
    return [text for _, text in typed_segments(command)]


def typed_segments(command: str) -> list[tuple[str, str]]:
    """``segments`` with each segment's kind: ``"shell"``, or ``"python"`` for Python text."""
    out: list[str] = []
    _walk(command, out, depth=0)
    return [_typed(seg) for seg in out]


def _typed(seg: str) -> tuple[str, str]:
    if seg.startswith(_PYTHON_MARK):
        return _KIND_PYTHON, seg[len(_PYTHON_MARK):]
    return _KIND_SHELL, seg


def _walk(text: str, out: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH or not text:
        return
    pieces, nested, heredocs = _scan(text)
    for inner in nested:
        _walk(inner, out, depth + 1)
    executed = _executed_paths(pieces)
    for piece in pieces:
        run = _find_exec_argv(piece) or _env_split_string(piece)
        if run:
            _walk(run, out, depth + 1)
    for index, piece in enumerate(pieces):
        # A script written by ``echo``/``printf`` (``>`` or ``tee``) and run
        # in the same call: the writer's quoted arguments are the script, or
        # the piece piped into ``tee``.
        target = _writer_target(piece)
        kind = _executed_kind(executed, target) if target else None
        if kind is None:
            continue
        found = _command_word(piece)
        if found is None or found[0].rsplit("/", 1)[-1] not in _WRITERS:
            continue
        source = pieces[index - 1] if found[0].rsplit("/", 1)[-1] == "tee" and index else piece
        _emit_written(source, kind, out, depth)
    for piece in pieces:
        # ``bash <(echo cmd)``: the interpreter runs what the substituted command prints.
        found = _command_word(piece)
        if found is None:
            continue
        base = found[0].rsplit("/", 1)[-1]
        kind = _KIND_SHELL if base in _SHELL_RUNNERS else _KIND_PYTHON if _PYTHONS.match(base) else None
        if kind is None:
            continue
        for arg in found[1]:
            if arg.startswith("<(") and arg.endswith(")"):
                for inner_piece in _scan(arg[2:-1])[0]:
                    _emit_written(inner_piece, kind, out, depth)
    for opener_pieces, body, expands in heredocs:
        if expands:
            inner: list[str] = []
            _collect_nested(body, inner)
            for sub in inner:
                _walk(sub, out, depth + 1)
        kind = _heredoc_kind(opener_pieces, executed)
        if kind == _KIND_SHELL:
            _walk(body, out, depth + 1)
        elif kind == _KIND_PYTHON and body.strip():
            out.append(_PYTHON_MARK + body.strip())
    for piece in pieces:
        _emit(piece, out, depth)


def _emit(piece: str, out: list[str], depth: int) -> None:
    tokens = _tokens(piece)
    index = _command_index(tokens)
    if index is None:
        return
    _, end, word = tokens[index]
    unquoted = _unquote(word)
    inner = _substituted_command(unquoted)
    if inner is not None:
        which = _WHICH_RE.match(inner.strip())
        if not which:
            return
        unquoted = which.group(1)
    if unquoted in _BLOCK_CLOSERS:
        return
    out.append((unquoted + piece[end:]).strip())
    base = unquoted.rsplit("/", 1)[-1]
    rest = tokens[index + 1 :]
    if base in _SHELLS or base == "eval" or _PYTHONS.match(base):
        script = _dash_c_script(base, rest)
        if script is not None:
            if base in _SHELLS or base == "eval":
                _walk(script, out, depth + 1)
            elif script.strip():
                out.append(_PYTHON_MARK + script.strip())
    if base == "kubectl" and _kubectl_verb(rest) in _KUBECTL_PASSTHROUGH:
        for _, token_end, token in rest:
            if token == "--":
                tail = piece[token_end:].strip()
                if tail:
                    _walk(tail, out, depth + 1)
                break


def _kubectl_verb(rest: list[tuple[int, int, str]]) -> str | None:
    """kubectl's first subcommand: the first argument past the global flags and their values
    (a boolean global flag takes none; the table is the policy's ``_KUBECTL_BOOLEAN_FLAGS``)."""
    i = 0
    while i < len(rest):
        token = rest[i][2]
        if token == "--":
            return None
        if token.startswith("-"):
            i += 1
            if token not in _KUBECTL_BOOLEAN_FLAGS and "=" not in token and i < len(rest) and not rest[i][2].startswith("-"):
                i += 1
            continue
        return _unquote(token)
    return None


def _emit_written(piece: str, kind: str, out: list[str], depth: int) -> None:
    """Walk, or keep as text, what a writer piece prints when something runs it."""
    for script in _writer_scripts(piece):
        if kind == _KIND_SHELL:
            _walk(script, out, depth + 1)
        elif script.strip():
            out.append(_PYTHON_MARK + script.strip())


def _writer_scripts(piece: str) -> list[str]:
    """The text ``echo``/``printf``/``tee`` in ``piece`` writes: each quoted argument,
    or the arguments joined as the shell hands them over when any is bare."""
    found = _command_word(piece)
    if found is None:
        return []
    base = found[0].rsplit("/", 1)[-1]
    if base not in _WRITERS or base == "tee":
        return []
    words: list[str] = []
    for _, _, token in _tokens(piece):
        if token.startswith((">", "1>", "2>", "&>")):
            break
        attached = _ATTACHED_REDIRECT_RE.match(token)
        if attached:  # ``'text'>file``: the redirect follows the closing quote
            words.append(attached.group(1))
            break
        words.append(token)
    # Everything after the writer's word and its options; printf's first word is the format.
    while words and words[0].rsplit("/", 1)[-1] != base:
        words.pop(0)
    words = words[1:]
    options: list[str] = []
    while words and words[0] in _WRITER_OPTIONS:
        options.append(words.pop(0))
    if base == "printf" and len(words) > 1:
        words = words[1:]  # the format; with no further argument the format is the text
    expands = base == "printf" or any(o in ("-e", "-ne", "-en") for o in options)
    if any(w[:1] not in "'\"$" for w in words):
        return [_expand(" ".join(_unquote(w) for w in words), expands)]
    return [_expand(_unquote(w), expands or w.startswith("$'")) for w in words if len(w) > 2]


def _expand(text: str, expands: bool) -> str:
    """The text with ``\n``/``\t``/``\r`` escapes turned into the characters ``echo -e``,
    ``printf`` and ``$'…'`` write."""
    if not expands:
        return text
    for escape, char in _ESCAPES:
        text = text.replace(escape, char)
    return text


def _find_exec_argv(piece: str) -> str | None:
    """What a ``find -exec … \;`` runs, as one command string with its quoting kept, or ``None``."""
    if "-exec" not in piece and "-ok" not in piece:  # cheap gate: the piece is tokenised again below
        return None
    tokens = _tokens(piece)
    index = _command_index(tokens)
    if index is None or _unquote(tokens[index][2]).rsplit("/", 1)[-1] != "find":
        return None
    words = [t for _, _, t in tokens[index + 1 :]]
    for k, token in enumerate(words):
        if token in _FIND_EXEC_OPTIONS:
            run = []
            for t in words[k + 1 :]:
                if t in _FIND_EXEC_END:
                    break
                run.append(t)
            return " ".join(run) if run else None
    return None


def _value_options(base: str) -> frozenset[str]:
    """The short options of ``base`` that take a value token."""
    if base == "su":
        return _SU_VALUE_OPTIONS
    if _PYTHONS.match(base):
        return _PYTHON_VALUE_OPTIONS
    return _SHELL_VALUE_OPTIONS


def _env_split_string(piece: str) -> str | None:
    """What GNU ``env -S '<string>'`` splits and runs, or ``None``. ``env`` is a wrapper
    word, so this is read from the raw tokens before the wrapper walk drops it."""
    if "env" not in piece or "-S" not in piece and "--split-string" not in piece:  # cheap gate
        return None
    tokens = _tokens(piece)
    index = _command_index(tokens, keep=frozenset({"env"}))
    if index is None or _unquote(tokens[index][2]).rsplit("/", 1)[-1] != "env":
        return None
    words = [t for _, _, t in tokens[index + 1 :]]
    k = 0
    while k < len(words):
        token = words[k]
        if token in ("-S", "--split-string") and k + 1 < len(words):
            return _unquote(words[k + 1])
        if token.startswith("-S") and len(token) > 2:
            return _unquote(token[2:])
        if token in _WRAPPER_VALUE_OPTIONS["env"]:
            k += 2
            continue
        if not token.startswith("-"):
            return None
        k += 1
    return None


def _dash_c_script(base: str, rest: list[tuple[int, int, str]]) -> str | None:
    """The string a shell or python runs with ``-c``, or everything after ``eval``.

    Options are read as the interpreter reads them: ``--`` ends them (after a
    ``-c`` the next token is the script; before one there is no script), long
    options that take a value consume it, a short value option consumes the
    next token or carries it attached, and only then does a cluster holding
    ``c`` name the script. ``su`` may name a user (and ``-``) before ``-c``."""
    if base == "eval":
        return " ".join(_unquote(t) for _, _, t in rest) if rest else None
    values = _value_options(base)
    user_seen = False
    i = 0
    while i < len(rest):
        token = rest[i][2]
        if token == "--":
            return None
        if token.startswith("--"):
            i += 2 if token in _INTERPRETER_LONG_VALUE_OPTIONS else 1
            continue
        if token.startswith(("-", "+")) and len(token) > 1:
            if token in values:
                i += 2
                continue
            if token[:2] in values and len(token) > 2:
                i += 1
                continue
            if "c" in token[1:] and token[0] == "-":
                k = i + 1
                if k < len(rest) and rest[k][2] == "--":
                    k += 1
                return _unquote(rest[k][2]) if k < len(rest) else None
            # A cluster ending in a value letter (``-eo x``).
            if ("-" + token[-1]) in values:
                i += 2
                continue
            i += 1
            continue
        if base == "su" and (token == "-" or not user_seen):
            user_seen = user_seen or token != "-"
            i += 1
            continue
        return None
    return None


def _command_index(tokens: list[tuple[int, int, str]], keep: frozenset[str] = frozenset()) -> int | None:
    """The index of the command word: assignments and wrapper words (with their options) are
    skipped, a wrapper named in ``keep`` is returned as the command word instead."""
    i = 0
    while i < len(tokens):
        raw = tokens[i][2]
        word = _unquote(raw)
        if _ASSIGNMENT_RE.match(raw) and not raw.startswith(("'", '"')):
            i += 1
            continue
        if word.rsplit("/", 1)[-1] in _WRAPPERS and word.rsplit("/", 1)[-1] not in keep:
            name = word.rsplit("/", 1)[-1]
            i += 1
            if name == "taskset" and i < len(tokens) and _TASKSET_MASK_RE.match(tokens[i][2]):
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
    if token.startswith("\\") and len(token) > 1:
        token = token[1:]
    if token.startswith("$'") and token.endswith("'") and len(token) >= 3:
        return _expand(token[2:-1], True)  # ANSI-C quoting: escapes are characters
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
            elif c in "<>" and piece.startswith(c + "(", i):
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


def _scan(text: str) -> tuple[list[str], list[str], list[tuple[list[str], str, bool]]]:
    """Split ``text`` into simple-command pieces, nested substitution texts and heredocs.

    Returns ``(pieces, nested, heredocs)``; a heredoc is ``(the pieces of its
    opener line, body, whether the shell expands the body)``.
    """
    pieces: list[str] = []
    nested: list[str] = []
    heredocs: list[tuple[list[str], str, bool]] = []
    pending: list[tuple[str, bool, bool]] = []  # (tag, strip leading tabs, expands)
    buf: list[str] = []
    line_first = 0  # index in ``pieces`` of the first piece on the current line
    i, n = 0, len(text)

    def flush() -> None:
        piece = "".join(buf).strip()
        buf.clear()
        if piece:
            pieces.append(piece)

    def end_of_line(pos: int) -> int:
        """Consume the pending heredoc bodies that start at ``pos`` (just after a newline)."""
        nonlocal pending
        current = "".join(buf).strip()
        opener_pieces = pieces[line_first:] + ([current] if current else [])
        for tag, strip_tabs, expands in pending:
            body_lines: list[str] = []
            while pos < n:
                nl = text.find("\n", pos)
                line = text[pos:] if nl < 0 else text[pos:nl]
                pos = n if nl < 0 else nl + 1
                candidate = line.lstrip("\t") if strip_tabs else line
                if candidate.strip() == tag:
                    break
                body_lines.append(candidate if strip_tabs else line)
            heredocs.append((opener_pieces, "\n".join(body_lines), expands))
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
        elif c in "<>" and text.startswith(c + "(", i):
            # Process substitution: ``bash <(echo cmd)`` runs what ``echo`` prints.
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
        elif c == "<" and text.startswith("<<<", i):
            # A here-string: an operand, not a heredoc opener.
            buf.append("<<<")
            i += 3
        elif c == "<" and text.startswith("<<", i):
            m = _HEREDOC_RE.match(text, i)
            if m:
                if m.group(2) is not None:
                    tag, expands = m.group(2), False
                elif m.group(3) is not None:
                    tag, expands = m.group(3), False
                else:
                    tag, expands = m.group(5), m.group(4) is None
                pending.append((tag, m.group(1) == "-", expands))
                buf.append(text[i : m.end()])
                i = m.end()
            else:
                buf.append(text[i : i + 2])
                i += 2
        elif c == "\n":
            if pending:
                i = end_of_line(i + 1)
            else:
                i += 1
            flush()
            line_first = len(pieces)
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
    """Substitutions inside text the shell expands: a double-quoted string, a bare-tag heredoc."""
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


def _command_word(piece: str) -> tuple[str, list[str]] | None:
    """The normalised command word of ``piece`` and its unquoted arguments."""
    tokens = _tokens(piece)
    index = _command_index(tokens)
    if index is None:
        return None
    word = _unquote(tokens[index][2])
    return word, [_unquote(t) for _, _, t in tokens[index + 1 :]]


def _executed_paths(pieces: list[str]) -> dict[str, str]:
    """Each path a piece runs, to how: ``bash p``, ``. p``, ``python p``, ``./p``, ``chmod +x p``.

    An interpreter runs its first positional argument only; a flag's value
    (``--body-file p``) is not a script.
    """
    paths: dict[str, str] = {}
    for piece in pieces:
        found = _command_word(piece)
        if found is None:
            continue
        word, args = found
        base = word.rsplit("/", 1)[-1]
        positional = _interpreter_operands(args, base)
        if word == "chmod":
            positional = [a for a in args if not a.startswith("-")]  # the mode is the first operand
        if base in _SHELL_RUNNERS and positional:
            paths.setdefault(positional[0], _KIND_SHELL)
        elif _PYTHONS.match(base) and positional:
            paths.setdefault(positional[0], _KIND_PYTHON)
        elif word == "chmod" and positional and _CHMOD_EXEC_RE.search(positional[0]):
            for path in positional[1:]:
                paths.setdefault(path, _KIND_SHELL)
        elif word.startswith(("./", "/")):
            paths.setdefault(word, _KIND_SHELL)
            paths.setdefault(word[2:] if word.startswith("./") else word, _KIND_SHELL)
    return paths


def _interpreter_operands(args: list[str], base: str) -> list[str]:
    """An interpreter's operands: option words and their values (``-o pipefail``, ``--rcfile f``,
    ``-W ignore``, attached ``-Werror``) are neither."""
    values = _value_options(base)
    out: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in values or token in _INTERPRETER_LONG_VALUE_OPTIONS:
            i += 2
        elif token.startswith(("-", "+")) and len(token) > 1:
            i += 2 if ("-" + token[-1]) in values and token[:2] not in values else 1
        else:
            out.append(token)
            i += 1
    return out


def _writer_target(piece: str) -> str:
    """The file a writer piece's own redirect, ``tee`` or ``dd of=`` names, read token by token
    so a ``>`` inside the quoted text is not taken for it."""
    tokens = [t for _, _, t in _tokens(piece)]
    found = _command_word(piece)
    if found and found[0].rsplit("/", 1)[-1] == "tee":
        positional = [a for a in found[1] if not a.startswith(("-", ">"))]
        if positional:
            return positional[0]
    for i, token in enumerate(tokens):
        attached = _ATTACHED_REDIRECT_RE.match(token)
        if attached:
            rest = token[attached.end():].lstrip(">")
            return _unquote(rest) if rest else (_unquote(tokens[i + 1]) if i + 1 < len(tokens) else "")
        if token in (">", ">>", "1>", "1>>", "&>"):
            return _unquote(tokens[i + 1]) if i + 1 < len(tokens) else ""
        if token.startswith((">", "1>", "&>")) and not token.startswith(">("):
            return _unquote(token.lstrip("1&>"))
    return _heredoc_target([piece])


def _executed_kind(executed: dict[str, str], target: str) -> str | None:
    """How ``target`` runs, with ``./`` read the same on both sides."""
    plain = target[2:] if target.startswith("./") else target
    return executed.get(plain) or executed.get("./" + plain)


def _heredoc_target(opener_pieces: list[str]) -> str:
    """The file a heredoc's line writes to: a ``>`` redirect, ``tee``, or ``dd of=``."""
    for piece in opener_pieces:
        # ``tee <file> >/dev/null``: tee's file, not the redirect that silences it.
        found = _command_word(piece)
        if found and found[0].rsplit("/", 1)[-1] == "tee":
            positional = [a for a in found[1] if not a.startswith(("-", ">"))]
            if positional:
                return positional[0]
        redirect = _REDIRECT_TARGET_RE.search(piece)
        if redirect:
            return _unquote(redirect.group(1))
        dd = _DD_TARGET_RE.search(piece)
        if dd:
            return _unquote(dd.group(1))
    return ""


def _heredoc_kind(opener_pieces: list[str], executed: dict[str, str]) -> str | None:
    """How a heredoc's body runs, or ``None`` when it is content."""
    for piece in opener_pieces:
        found = _command_word(piece)
        if found is None:
            continue
        base = found[0].rsplit("/", 1)[-1]
        if _PYTHONS.match(base):
            return _KIND_PYTHON
        if base in _SHELL_RUNNERS:
            return _KIND_SHELL
    target = _heredoc_target(opener_pieces)
    if not target:
        return None
    if target.endswith(_PYTHON_SUFFIX):
        return _KIND_PYTHON
    if target.endswith(_SHELL_SUFFIXES):
        return _KIND_SHELL
    return _executed_kind(executed, target)
