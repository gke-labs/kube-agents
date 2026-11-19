"""Set one scalar in a block-style YAML manifest and leave every other byte alone.

This is the first half of the `edit-manifest` verb: a recipe's fix generator
calls `set_scalar` on the manifest it read, and the broker later re-runs the
same generator on the base it reads itself and requires the pull request's head
to match byte for byte. So the editor has two jobs, and they pull the same way:

- be exact and deterministic. Only the edited value's characters change (or,
  in create mode, one line is inserted); comments, key order, indentation,
  quoting of every other value and the trailing newline stay as they were.
- refuse what it cannot edit exactly. It reads a strict subset of YAML and
  raises `RefusedEdit` on anything outside it rather than guessing.

It is stdlib only and does not parse with a YAML library: a full parser would
accept constructs
(anchors, merge keys, tags) whose meaning this editor cannot hold constant.

What the target document may contain: block mappings and block sequences of
plain, single- or double-quoted single-line scalars. Flow collections and
block scalars (`|`, `>`) are skipped as opaque values when they are not on the
edited path, and refused when they are. Anywhere in the target document it
refuses anchors, aliases, tags, merge keys, complex keys, duplicate keys and
multi-line plain or quoted scalars. Anywhere in the file it refuses a byte-order
mark, carriage returns, tabs, control characters, Unicode line separators, Helm
template markers (`{{`), directives and document-end markers, and files larger
than MAX_FILE_BYTES. It never changes `apiVersion`, `kind`, `metadata.name` or
`metadata.namespace`, at the document root or on any nested object that
carries `apiVersion` and `kind` (a List's items).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Mirrors automation_rules.MAX_FILE_BYTES. Not imported, so this module has no
# dependency on its callers; test_yaml_edit.py holds the two equal.
MAX_FILE_BYTES = 64 * 1024

# PyYAML and libyaml/go-yaml refuse an implicit key whose ':' is more than 1024
# characters from its start.
MAX_KEY_CHARS = 1024
# go-yaml reads integers outside int64 as floats.
INT_MIN, INT_MAX = -(2**63), 2**63 - 1

PROTECTED_PATHS = (
    ("apiVersion",),
    ("kind",),
    ("metadata", "name"),
    ("metadata", "namespace"),
)

Value = str | int | bool
PathSegment = str | int


class RefusedEdit(ValueError):
    """The editor cannot make this edit exactly. `reason` is a stable slug."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# --- nodes -------------------------------------------------------------------


@dataclass
class Scalar:
    line: int  # absolute line index in the file
    start: int  # column of the first character of the value
    end: int  # column one past the last character of the value
    style: str  # "plain", "single" or "double"


@dataclass
class Null:
    line: int


@dataclass
class Opaque:
    kind: str  # "flow" or "block-scalar"
    ends_with_block_scalar: bool = False


@dataclass
class Mapping:
    indent: int
    entries: dict = field(default_factory=dict)
    nonstring_keys: bool = False
    last_line: int = -1
    ends_with_block_scalar: bool = False


@dataclass
class Sequence:
    indent: int
    items: list = field(default_factory=list)
    last_line: int = -1
    ends_with_block_scalar: bool = False


Node = Scalar | Null | Opaque | Mapping | Sequence


# --- lexical helpers ---------------------------------------------------------

_DOC_START = re.compile(r"^---(?= |$)")
_DOC_END = re.compile(r"^\.\.\.(?= |$)")
_BLOCK_HEADER = re.compile(r"^[|>](?:[1-9][+-]?|[+-][1-9]?)?(?: +#.*)? *$")
_PLAIN_KEY_COLON = re.compile(r":(?= |$)")
# The body of a double-quoted scalar whose every backslash starts an escape
# YAML (and PyYAML, and go-yaml) accepts.
_VALID_ESCAPES = re.compile(r'(?:[^\\"]|\\(?:[0abtnvfre "/\\N_LP]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}))*')
_INDICATORS = set("-?:,[]{}#&*!|>'\"%@`")
# Scalars YAML 1.1 (PyYAML, and the go-yaml v2 that Kubernetes tooling still
# embeds) resolves to something other than a string: null, bool, int (including
# go-yaml's 0o octal), float, sexagesimal, timestamp, and the value (=) and
# merge (<<) indicators. Matched case-insensitively
# against a plain scalar; a plain key that matches is a non-string key.
_NONSTRING_PLAIN = re.compile(
    r"""^(?:
        ~|null|y|n|yes|no|on|off|true|false
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0[0-7_]+|[-+]?0x[0-9a-f_]+|[-+]?0b[01_]+
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?
        |[-+]?(?:[0-9][0-9_]*)?\.[0-9_]*(?:e[-+]?[0-9]+)?
        |[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?e[-+]?[0-9]+
        |[-+]?\.inf|\.nan
        |[-+]?0o[0-7_]+
        |[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:(?:[Tt]|[ ]+)[0-9].*)?
        |=|<<
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
# The only strings this editor writes unquoted: they start with a letter and
# cannot be read back as anything but the same string.
_SAFE_PLAIN = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9._/-]*$")
_LINE_BREAKS = ("\x85", "\u2028", "\u2029")


def _check_file(text: str) -> None:
    if not isinstance(text, str):
        raise RefusedEdit("bad-input", "manifest text must be str")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise RefusedEdit("control-char", "text is not valid UTF-8") from None
    if size > MAX_FILE_BYTES:
        raise RefusedEdit("too-large", f"larger than {MAX_FILE_BYTES} bytes")
    if "\ufeff" in text:
        raise RefusedEdit("bom", "byte-order mark")
    if "\r" in text:
        raise RefusedEdit("crlf", "carriage return")
    if "\t" in text:
        raise RefusedEdit("tab", "tab character")
    for ch in text:
        o = ord(ch)
        if (o < 0x20 and ch != "\n") or 0x7F <= o <= 0x9F or o in (0xFFFE, 0xFFFF) or ch in _LINE_BREAKS:
            raise RefusedEdit("control-char", f"control character U+{ord(ch):04X}")
    if "{{" in text:
        raise RefusedEdit("template", "Helm template marker")


def _documents(lines: list[str]) -> list[tuple[int, int]]:
    """Line ranges [start, end) of each document, counted the way PyYAML does."""
    markers = []
    for i, line in enumerate(lines):
        if line.startswith("%"):
            raise RefusedEdit("directive", f"line {i + 1}: YAML directive")
        if _DOC_END.match(line):
            raise RefusedEdit("doc-end-marker", f"line {i + 1}: document-end marker")
        if _DOC_START.match(line):
            rest = line[3:].strip()
            if rest and not rest.startswith("#"):
                raise RefusedEdit("unsupported-syntax", f"line {i + 1}: content after ---")
            markers.append(i)
    docs = []
    first_end = markers[0] if markers else len(lines)
    if any(_is_content(lines[j]) for j in range(first_end)):
        docs.append((0, first_end))
    for k, m in enumerate(markers):
        docs.append((m + 1, markers[k + 1] if k + 1 < len(markers) else len(lines)))
    return docs


def _is_content(line: str) -> bool:
    s = line.lstrip(" ")
    return bool(s) and not s.startswith("#")


def _quoted_end(text: str, pos: int) -> int:
    """Index one past the closing quote of the scalar opening at text[pos], or -1."""
    quote = text[pos]
    i = pos + 1
    while i < len(text):
        c = text[i]
        if quote == '"' and c == "\\":
            i += 2
            continue
        if c == quote:
            if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def _rest_is_comment(text: str) -> bool:
    s = text.lstrip(" ")
    return s == "" or (s.startswith("#") and (len(s) == len(text) or text[0] == " "))


def _unquote(raw: str, lineno: int) -> str:
    if raw[0] == "'":
        return raw[1:-1].replace("''", "'")
    if "\\" in raw:
        raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: escape in quoted key")
    return raw[1:-1]


# --- parser ------------------------------------------------------------------


class _Parser:
    """Recursive descent over one document's lines, refusing anything unknown."""

    def __init__(self, lines: list[str], start: int, end: int):
        self.lines = lines
        self.i = start
        self.end = end

    def peek(self) -> int:
        """Index of the next content line, or -1. Does not consume."""
        j = self.i
        while j < self.end:
            if _is_content(self.lines[j]):
                return j
            j += 1
        return -1

    def indent_of(self, j: int) -> int:
        line = self.lines[j]
        return len(line) - len(line.lstrip(" "))

    def is_seq_item(self, j: int) -> bool:
        s = self.lines[j].lstrip(" ")
        return s == "-" or s.startswith("- ")

    def parse_document(self) -> Node:
        j = self.peek()
        if j < 0:
            return Null(-1)
        if self.indent_of(j) != 0 or self.is_seq_item(j):
            raise RefusedEdit("unsupported-syntax", "document root is not a block mapping at column 0")
        root = self.parse_mapping(0, None)
        if self.peek() >= 0:
            raise RefusedEdit("unsupported-syntax", f"line {self.peek() + 1}: content after root mapping")
        return root

    def split_key(self, text: str, col: int, lineno: int):
        """(key, is_plain, rest, rest_col) if text is a `key:` entry, else None."""
        if text[0] in "'\"":
            end = _quoted_end(text, 0)
            if end < 0 or not text[end:].startswith(":") or text[end + 1 : end + 2] not in ("", " "):
                return None
            if end > MAX_KEY_CHARS:
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: key longer than {MAX_KEY_CHARS} characters")
            return _unquote(text[:end], lineno), False, text[end + 1 :], col + end + 1
        m = _PLAIN_KEY_COLON.search(text)
        if not m:
            return None
        hash_at = text.find(" #")
        if hash_at != -1 and hash_at < m.start():
            return None
        key = text[: m.start()]
        if len(key) > MAX_KEY_CHARS:
            raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: key longer than {MAX_KEY_CHARS} characters")
        if text[0] in _INDICATORS or key != key.rstrip(" "):
            raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: key {key!r} is not a simple key")
        if key == "<<":
            raise RefusedEdit("merge-key", f"line {lineno + 1}: merge key")
        return key, True, text[m.end() :], col + m.end()

    def parse_mapping(self, indent: int, first) -> Mapping:
        node = Mapping(indent)
        while True:
            if first is not None:
                col, text, lineno = first
                first = None
            else:
                j = self.peek()
                if j < 0 or self.indent_of(j) < indent:
                    break
                if self.indent_of(j) > indent:
                    raise RefusedEdit("unsupported-syntax", f"line {j + 1}: unexpected indentation")
                if self.is_seq_item(j):
                    raise RefusedEdit("unsupported-syntax", f"line {j + 1}: sequence item inside a mapping")
                col, text, lineno = indent, self.lines[j][indent:], j
                self.i = j + 1
            split = self.split_key(text, col, lineno)
            if split is None:
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: expected `key: value`")
            key, plain, rest, rest_col = split
            if key in node.entries:
                raise RefusedEdit("duplicate-key", f"line {lineno + 1}: duplicate key {key!r}")
            if plain and _NONSTRING_PLAIN.match(key):
                node.nonstring_keys = True
            value = self.parse_value(col, rest, rest_col, lineno, mapping_value=True)
            node.entries[key] = value
            node.ends_with_block_scalar = getattr(value, "ends_with_block_scalar", False)
        node.last_line = self.i - 1
        return node

    def parse_sequence(self, indent: int) -> Sequence:
        node = Sequence(indent)
        while True:
            j = self.peek()
            if j < 0 or self.indent_of(j) != indent or not self.is_seq_item(j):
                break
            self.i = j + 1
            rest = self.lines[j][indent + 1 :]
            body = rest.lstrip(" ")
            col = indent + 1 + len(rest) - len(body)
            if body.startswith("- ") or body == "-":
                raise RefusedEdit("unsupported-syntax", f"line {j + 1}: nested inline sequence")
            item: Node
            if body and body[0] not in "[{#" and self.split_key(body, col, j) is not None:
                item = self.parse_mapping(col, (col, body, j))
            else:
                item = self.parse_value(indent, rest, indent + 1, j, mapping_value=False)
            node.items.append(item)
            node.ends_with_block_scalar = getattr(item, "ends_with_block_scalar", False)
        nxt = self.peek()
        if nxt >= 0 and self.indent_of(nxt) > indent:
            raise RefusedEdit("unsupported-syntax", f"line {nxt + 1}: unexpected indentation")
        node.last_line = self.i - 1
        return node

    def parse_value(self, parent: int, rest: str, rest_col: int, lineno: int, mapping_value: bool) -> Node:
        """The value after `key:` or `-`. `parent` is the key's (or dash's) column."""
        if rest and not rest.startswith(" "):
            raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: no space after indicator")
        body = rest.lstrip(" ")
        start = rest_col + len(rest) - len(body)
        if body == "" or body.startswith("#"):
            j = self.peek()
            if j >= 0 and self.indent_of(j) > parent:
                if self.is_seq_item(j):
                    return self.parse_sequence(self.indent_of(j))
                text = self.lines[j][self.indent_of(j) :]
                if self.split_key(text, self.indent_of(j), j) is None:
                    raise RefusedEdit("multi-line-scalar", f"line {j + 1}: scalar on its own line")
                self.i = j + 1
                return self.parse_mapping(self.indent_of(j), (self.indent_of(j), text, j))
            if mapping_value and j >= 0 and self.indent_of(j) == parent and self.is_seq_item(j):
                return self.parse_sequence(parent)
            return Null(lineno)
        c = body[0]
        if c == "&":
            raise RefusedEdit("anchor", f"line {lineno + 1}: anchor")
        if c == "*":
            raise RefusedEdit("alias", f"line {lineno + 1}: alias")
        if c == "!":
            raise RefusedEdit("tag", f"line {lineno + 1}: tag")
        if c in "|>":
            if not _BLOCK_HEADER.match(body) or any(d in body.split("#")[0] for d in "123456789"):
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: block scalar header")
            content_indent = -1
            leading_blank = 0
            while self.i < self.end:
                line = self.lines[self.i]
                if line.strip(" ") == "":
                    if content_indent < 0:
                        leading_blank = max(leading_blank, len(line))
                    self.i += 1
                elif self.indent_of(self.i) > parent and self.indent_of(self.i) >= max(content_indent, 0):
                    # A line indented less than the first content line ends the
                    # scalar, as it does for a full parser; the caller then
                    # refuses it as unexpected indentation.
                    if content_indent < 0:
                        content_indent = self.indent_of(self.i)
                    self.i += 1
                else:
                    break
            if content_indent >= 0 and leading_blank > content_indent:
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: leading blank line out-indents the block")
            return Opaque("block-scalar", ends_with_block_scalar=True)
        if c in "[{":
            self.skip_flow(lineno, start)
            return Opaque("flow")
        if c in "'\"":
            end = _quoted_end(body, 0)
            if end < 0:
                raise RefusedEdit("multi-line-scalar", f"line {lineno + 1}: quoted scalar spans lines")
            if not _rest_is_comment(body[end:]):
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: text after quoted scalar")
            if c == '"' and not _VALID_ESCAPES.fullmatch(body[1 : end - 1]):
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: invalid escape in double-quoted scalar")
            self.no_continuation(parent, lineno)
            return Scalar(lineno, start, start + end, "single" if c == "'" else "double")
        if c in _INDICATORS and not (c in "-?:" and body[1:2] not in ("", " ")):
            raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: value starts with {c!r}")
        hash_at = body.find(" #")
        value = (body if hash_at < 0 else body[:hash_at]).rstrip(" ")
        if value in ("=", "<<"):
            raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: plain {value!r} has no string reading")
        if _PLAIN_KEY_COLON.search(value):
            raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: `: ` inside a plain value")
        self.no_continuation(parent, lineno)
        return Scalar(lineno, start, start + len(value), "plain")

    def no_continuation(self, parent: int, lineno: int) -> None:
        j = self.peek()
        if j >= 0 and self.indent_of(j) > parent:
            raise RefusedEdit("multi-line-scalar", f"line {j + 1}: scalar continues onto the next line")

    def skip_flow(self, lineno: int, col: int) -> None:
        """Consume and validate a flow collection that is not on the edited path.

        It is opaque to the edit but still checked against a small flow grammar
        (no anchors, aliases, tags, explicit keys, single-pair sequence entries
        or duplicate keys), so an accepted file is one a full parser accepts too.
        """
        tokens, last = _flow_tokens(self.lines, lineno, col, self.end)
        pos = _flow_node(tokens, 0)
        if pos != len(tokens):
            raise RefusedEdit("unsupported-syntax", f"line {tokens[pos][1] + 1}: text after flow collection")
        self.i = max(self.i, last + 1)


_FLOW_INDICATORS = ",[]{}"


def _flow_tokens(lines: list[str], lineno: int, col: int, end: int):
    """Tokens (kind, line, text) of one flow collection, and the line it ends on."""
    tokens = []
    depth = 0
    line_no, pos = lineno, col
    text = lines[line_no]
    while True:
        if pos >= len(text):
            if depth == 0:
                break
            line_no += 1
            if line_no >= end:
                raise RefusedEdit("unsupported-syntax", f"line {lineno + 1}: unclosed flow collection")
            text, pos = lines[line_no], 0
            continue
        ch = text[pos]
        if ch == " ":
            pos += 1
            continue
        if ch == "#" and (pos == 0 or text[pos - 1] == " "):
            if depth == 0:
                break
            pos = len(text)
            continue
        if depth == 0 and tokens:
            raise RefusedEdit("unsupported-syntax", f"line {line_no + 1}: text after flow collection")
        nxt = text[pos + 1 : pos + 2]
        if ch in "[{":
            depth += 1
            tokens.append((ch, line_no, ch))
            pos += 1
        elif ch in "]}":
            depth -= 1
            tokens.append((ch, line_no, ch))
            pos += 1
        elif ch == ",":
            tokens.append((ch, line_no, ch))
            pos += 1
        elif ch == ":" and nxt in ("", " "):
            tokens.append((":", line_no, ch))
            pos += 1
        elif ch in "&*!":
            raise RefusedEdit(
                {"&": "anchor", "*": "alias", "!": "tag"}[ch], f"line {line_no + 1}: inside a flow collection"
            )
        elif ch in "'\"":
            stop = _quoted_end(text, pos)
            if stop < 0:
                raise RefusedEdit("multi-line-scalar", f"line {line_no + 1}: quoted scalar spans lines")
            after = text[stop : stop + 2]
            if after[:1] not in ("", " ", ",", "]", "}") and after not in (":", ": "):
                raise RefusedEdit("unsupported-syntax", f"line {line_no + 1}: text after quoted scalar")
            tokens.append(("scalar", line_no, _unquote(text[pos:stop], line_no)))
            pos = stop
        elif ch in _INDICATORS and not (ch == "-" and nxt not in ("", " ")):
            raise RefusedEdit("unsupported-syntax", f"line {line_no + 1}: {ch!r} in a flow collection")
        else:
            start = pos
            while pos < len(text):
                c = text[pos]
                if c in _FLOW_INDICATORS or (c == "#" and text[pos - 1] == " "):
                    break
                if c == "?":
                    # PyYAML ends a flow plain scalar at '?'; refuse rather than guess.
                    raise RefusedEdit("unsupported-syntax", f"line {line_no + 1}: '?' in a flow scalar")
                if c == ":":
                    after = text[pos + 1 : pos + 2]
                    if after in ("", " "):
                        break
                    if after in _FLOW_INDICATORS:
                        raise RefusedEdit("unsupported-syntax", f"line {line_no + 1}: ':' before a flow indicator")
                pos += 1
            plain = text[start:pos].rstrip(" ")
            if plain in ("=", "<<"):
                raise RefusedEdit("unsupported-syntax", f"line {line_no + 1}: plain {plain!r} has no string reading")
            tokens.append(("scalar", line_no, plain))
    return tokens, line_no


def _flow_node(tokens, i: int) -> int:
    """Validate one flow node starting at tokens[i]; return the index after it."""
    if i >= len(tokens):
        raise RefusedEdit("unsupported-syntax", "flow collection ends early")
    kind, line, _ = tokens[i]
    if kind == "scalar":
        return i + 1
    if kind not in "[{":
        raise RefusedEdit("unsupported-syntax", f"line {line + 1}: expected a value, found {kind!r}")
    close = "]" if kind == "[" else "}"
    keys = set()
    i += 1
    while True:
        if i < len(tokens) and tokens[i][0] == close:
            return i + 1
        if kind == "[":
            i = _flow_node(tokens, i)
        else:
            if i >= len(tokens) or tokens[i][0] != "scalar":
                raise RefusedEdit("unsupported-syntax", f"line {line + 1}: flow mapping key is not a scalar")
            if tokens[i][2] in keys:
                raise RefusedEdit("duplicate-key", f"line {tokens[i][1] + 1}: duplicate key {tokens[i][2]!r}")
            keys.add(tokens[i][2])
            i += 1
            if i < len(tokens) and tokens[i][0] == ":":
                i += 1
                if i < len(tokens) and tokens[i][0] not in (",", close):
                    i = _flow_node(tokens, i)
        if i >= len(tokens):
            raise RefusedEdit("unsupported-syntax", f"line {line + 1}: unclosed flow collection")
        if tokens[i][0] == ",":
            i += 1
            continue
        if tokens[i][0] != close:
            raise RefusedEdit("unsupported-syntax", f"line {tokens[i][1] + 1}: expected ',' or {close!r}")
        return i + 1


# --- rendering ---------------------------------------------------------------


def _render(value: Value, style: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise RefusedEdit("bad-value", "integer outside the signed 64-bit range")
        return str(value)
    if not isinstance(value, str):
        raise RefusedEdit("bad-value", f"unsupported value type {type(value).__name__}")
    if not value.isprintable():
        raise RefusedEdit("bad-value", "value has a line break or non-printable character")
    if "{{" in value:
        raise RefusedEdit("bad-value", "value has a Helm template marker")
    if style == "single":
        return "'" + value.replace("'", "''") + "'"
    if style == "plain" and _SAFE_PLAIN.match(value) and not _NONSTRING_PLAIN.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_rendered(raw: str) -> Value:
    """Read back a value `_render` wrote; used to check the edit round-trips."""
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    if raw.startswith('"'):
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    return raw


# --- public API --------------------------------------------------------------


def _parse_target(lines: list[str], doc_index: int) -> Node:
    docs = _documents(lines)
    if isinstance(doc_index, bool) or not isinstance(doc_index, int) or not 0 <= doc_index < len(docs):
        raise RefusedEdit("doc-index", f"no document {doc_index!r} (file has {len(docs)})")
    # Every document must parse, not just the target: a file is accepted only
    # when the editor understands all of it, so no other document can hide a
    # construct a full YAML parser would read differently.
    roots = [_Parser(lines, start, end).parse_document() for start, end in docs]
    return roots[doc_index]


def _walk(root: Node, path: list[PathSegment]):
    """Return (node, parent) for path; node is None if only the last key is missing."""
    node: Node = root
    parent: Node | None = None
    for depth, seg in enumerate(path):
        where = ".".join(str(s) for s in path[: depth + 1])
        if isinstance(node, Opaque):
            reason = "flow-on-path" if node.kind == "flow" else "block-scalar"
            raise RefusedEdit(reason, f"{where}: {node.kind} on the path")
        if isinstance(seg, bool) or not isinstance(seg, (str, int)):
            raise RefusedEdit("bad-path", f"{where}: path segment must be str or int")
        last = depth == len(path) - 1
        if isinstance(seg, str):
            if not isinstance(node, Mapping):
                raise RefusedEdit("path-missing", f"{where}: parent is not a mapping")
            if node.nonstring_keys:
                raise RefusedEdit("non-string-key", f"{where}: mapping has a key YAML reads as non-string")
            if seg not in node.entries:
                if last:
                    return None, node
                raise RefusedEdit("path-missing", f"{where}: no such key")
            parent, node = node, node.entries[seg]
        else:
            if not isinstance(node, Sequence) or not 0 <= seg < len(node.items):
                raise RefusedEdit("path-missing", f"{where}: no such sequence index")
            parent, node = node, node.items[seg]
    return node, parent


def _refuse_identity_path(root: Node, path: list[PathSegment]) -> None:
    """Refuse an object's identity fields wherever the object sits (e.g. a List's items)."""
    node: Node | None = root
    for depth in range(len(path)):
        is_object = isinstance(node, Mapping) and "apiVersion" in node.entries and "kind" in node.entries
        if is_object and tuple(path[depth:]) in PROTECTED_PATHS:
            raise RefusedEdit("protected-path", ".".join(map(str, path)))
        seg = path[depth]
        if isinstance(node, Mapping) and isinstance(seg, str):
            node = node.entries.get(seg)
        elif isinstance(node, Sequence) and isinstance(seg, int) and not isinstance(seg, bool) and 0 <= seg < len(node.items):
            node = node.items[seg]
        else:
            return


def set_scalar(
    text: str,
    doc_index: int,
    path: list[PathSegment],
    value: Value,
    *,
    create_missing_leaf: bool = False,
) -> str:
    """Return `text` with the scalar at `path` in document `doc_index` set to `value`.

    `path` is a list of mapping keys (str) and sequence indexes (int). The
    existing value must be a single-line plain or quoted scalar; its quoting
    style is kept when the new value can be written in it. With
    `create_missing_leaf`, a missing final key is added as one new
    `key: value` line at the end of its (existing, block) parent mapping.
    Raises RefusedEdit when the edit cannot be made exactly.
    """
    _check_file(text)
    if not isinstance(path, (list, tuple)) or not path:
        raise RefusedEdit("bad-path", "path must be a non-empty list")
    if tuple(path) in PROTECTED_PATHS:
        raise RefusedEdit("protected-path", ".".join(map(str, path)))
    lines = text.split("\n")
    root = _parse_target(lines, doc_index)
    _refuse_identity_path(root, list(path))
    node, parent = _walk(root, list(path))

    if node is None:
        if not create_missing_leaf:
            raise RefusedEdit("path-missing", f"{'.'.join(map(str, path))}: no such key")
        key = path[-1]
        if not _SAFE_KEY.match(key) or _NONSTRING_PLAIN.match(key):
            raise RefusedEdit("bad-path", f"cannot write {key!r} as a plain key")
        if parent.ends_with_block_scalar:
            raise RefusedEdit("block-scalar", "parent mapping ends in a block scalar")
        new_line = " " * parent.indent + key + ": " + _render(value, "plain")
        new_lines = lines[: parent.last_line + 1] + [new_line] + lines[parent.last_line + 1 :]
        changed = parent.last_line + 1
    else:
        if isinstance(node, Opaque):
            reason = "flow-on-path" if node.kind == "flow" else "block-scalar"
            raise RefusedEdit(reason, f"{'.'.join(map(str, path))}: target is a {node.kind}")
        if not isinstance(node, Scalar):
            raise RefusedEdit("not-scalar", f"{'.'.join(map(str, path))}: target is not a scalar value")
        line = lines[node.line]
        new_line = line[: node.start] + _render(value, node.style) + line[node.end :]
        new_lines = list(lines)
        new_lines[node.line] = new_line
        changed = node.line

    result = "\n".join(new_lines)
    _verify(lines, new_lines, changed, create=node is None, doc_index=doc_index, path=path, value=value)
    return result


def _verify(old, new, changed, *, create, doc_index, path, value) -> None:
    """Re-read the result and confirm the edit is exactly the one intended."""
    expected = old[:changed] + old[changed + 1 :] if not create else old
    if new[:changed] + new[changed + 1 :] != expected:
        raise RefusedEdit("internal", "edit touched more than one line")
    result = "\n".join(new)
    _check_file(result)
    node, _ = _walk(_parse_target(new, doc_index), list(path))
    if not isinstance(node, Scalar) or node.line != changed:
        raise RefusedEdit("internal", "edited value does not read back at the target")
    got = _decode_rendered(new[changed][node.start : node.end])
    if got != value or type(got) is not type(value):
        raise RefusedEdit("internal", f"edited value reads back as {got!r}")
