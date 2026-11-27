"""Hardened YAML 1.2 + JSON manifest parser and typed structural differ."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import yaml

from .types import ResourceRef

# Resource & safety bounds
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_DOCS_PER_FILE = 200
MAX_NESTING_DEPTH = 32
MAX_NODES_PER_FILE = 25_000
MAX_INT_BITS = 1024
MAX_RECOVERY_ALIASES = 64

# Unicode BiDi / Trojan-Source overrides, zero-width characters, and YAML 1.1/1.2 line-break splitters
_HOSTILE_UNICODE = frozenset(
    {
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\u200e",  # LEFT-TO-RIGHT MARK
        "\u200f",  # RIGHT-TO-LEFT MARK
        "\u061c",  # ARABIC LETTER MARK
        "\u2028",  # LINE SEPARATOR (YAML vs JSON/Go line-break desync)
        "\u2029",  # PARAGRAPH SEPARATOR (YAML vs JSON/Go line-break desync)
        "\u202a",  # LRE
        "\u202b",  # RLE
        "\u202c",  # PDF
        "\u202d",  # LRO
        "\u202e",  # RLO
        "\u2066",  # LRI
        "\u2067",  # RLI
        "\u2068",  # FSI
        "\u2069",  # PDI
    }
)

# Kubernetes API group and version syntax
_API_GROUP_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
_API_VERSION_TOKEN_RE = re.compile(r"^v[0-9]+[a-z0-9]*$")

# YAML 1.2 Core Schema scalar syntax
_YAML12_NULL_TOKENS = frozenset({"", "null", "Null", "NULL", "~"})
_YAML12_TRUE_TOKENS = frozenset({"true", "True", "TRUE"})
_YAML12_FALSE_TOKENS = frozenset({"false", "False", "FALSE"})
_YAML12_INT_DEC_RE = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$")
_YAML12_INT_OCT_RE = re.compile(r"^[-+]?0o[0-7]+$")
_YAML12_INT_HEX_RE = re.compile(r"^[-+]?0x[0-9a-fA-F]+$")
_YAML12_AMBIGUOUS_OCTAL_RE = re.compile(r"^[-+]?0[0-9]+$")
_YAML12_FLOAT_RE = re.compile(
    r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
_YAML_DOC_SPLIT_RE = re.compile(r"(?m)^---(?=[ \t]*(?:#.*)?$)[ \t]*(?:#.*)?\r?\n?")

# Kubernetes lists whose dict items are matched by unique `name` during diffing
_NAMED_LIST_KEYS = frozenset(
    {
        "containers",
        "initContainers",
        "ephemeralContainers",
        "volumes",
        "volumeMounts",
        "env",
        "ports",
        "imagePullSecrets",
        "volumeClaimTemplates",
    }
)

_LIST_ENVELOPE_ALLOWED_KEYS = frozenset({"apiVersion", "kind", "metadata", "items"})
MAX_LIST_NESTING_DEPTH = 4
_LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"
_REDACTED_PREFIX = "<redacted-secret:"
_NAN_SENTINEL = object()


class ManifestParseError(ValueError):
    """Raised when a manifest is malformed, hostile, or violates parser limits."""

    def __init__(self, message: str, *, code: str = "MANIFEST_PARSE_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedResource:
    """One parsed, normalized Kubernetes manifest document."""

    ref: ResourceRef
    obj: dict[str, Any]
    sanitized: dict[str, Any]


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing a single YAML or JSON file."""

    resources: tuple[ParsedResource, ...] = ()
    is_k8s_manifest: bool = True
    is_template: bool = False
    is_kustomize: bool = False
    error: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class FieldDiff:
    """One leaf-level structural change between before and after K8s objects."""

    tokens: tuple[str, ...]
    op: str  # "add" | "remove" | "replace"
    before: Any = None
    after: Any = None

    @property
    def dotted(self) -> str:
        return ".".join(self.tokens)


def _find_string_safety_violation(text: str, *, context: str = "manifest") -> tuple[str, str] | None:
    """Return `(message, code)` for the first disallowed character in `text`, or `None`."""
    for idx, ch in enumerate(text):
        codepoint = ord(ch)
        if codepoint == 0:
            return f"{context} contains NUL character at offset {idx}", "BINARY_OR_CONTROL_CHAR"
        if codepoint < 0x20 and ch not in ("\t", "\n", "\r"):
            return f"{context} contains C0 control character U+{codepoint:04X} at offset {idx}", "CONTROL_CHARACTER"
        if 0x7F <= codepoint <= 0x9F:
            return (
                f"{context} contains DEL/C1 control character U+{codepoint:04X} at offset {idx}",
                "CONTROL_CHARACTER",
            )
        if 0xD800 <= codepoint <= 0xDFFF:
            return f"{context} contains lone UTF-16 surrogate U+{codepoint:04X} at offset {idx}", "INVALID_UTF8"
        if ch == "\ufeff" or ch in _HOSTILE_UNICODE:
            return (
                f"{context} contains disallowed Unicode control/BiDi character U+{codepoint:04X} at offset {idx}",
                "HOSTILE_UNICODE",
            )
    return None


def _decode_raw_utf8(raw: bytes) -> str:
    """Enforce size limit, reject raw NUL bytes, and decode UTF-8 (stripping one leading BOM)."""
    if len(raw) > MAX_FILE_BYTES:
        raise ManifestParseError(
            f"manifest exceeds maximum size of {MAX_FILE_BYTES} bytes",
            code="MANIFEST_TOO_LARGE",
        )
    if b"\x00" in raw:
        raise ManifestParseError("manifest contains NUL byte", code="BINARY_OR_CONTROL_CHAR")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestParseError(f"manifest is not valid UTF-8: {exc}", code="INVALID_UTF8") from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _is_ident_start(ch: str) -> bool:
    return ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ch == "_"


def _has_helm_delimiters(s: str) -> bool:
    """Linear-time check for any `{{ ... }}` Go/Helm template action."""
    pos = s.find("{{")
    return pos != -1 and s.find("}}", pos + 2) != -1


def _has_unescaped_envsubst(s: str) -> bool:
    """Linear-time check for unescaped `${VAR...}` while ignoring Flux-escaped `$${VAR}`."""
    n = len(s)
    idx = 0
    while True:
        brace = s.find("${", idx)
        if brace == -1:
            return False
        if brace + 2 >= n or not _is_ident_start(s[brace + 2]):
            idx = brace + 2
            continue
        # Count contiguous `$` characters immediately preceding `{` (including `s[brace]`).
        # An odd count means the final `$` is unescaped (`${VAR}`, `$$${VAR}`);
        # an even count means Flux-escaped literal `$$` (`$${HOSTNAME}`).
        dollars = 1
        scan = brace - 1
        while scan >= 0 and s[scan] == "$":
            dollars += 1
            scan -= 1
        if dollars % 2 == 1:
            return s.find("}", brace + 3) != -1
        idx = brace + 2


def _string_has_template_syntax(s: str) -> bool:
    return _has_helm_delimiters(s) or _has_unescaped_envsubst(s)


def _strip_unquoted_yaml_comment(line: str) -> str:
    """Strip an unquoted `# ...` YAML comment from a single line.

    In YAML 1.2, `#` starts a comment only when outside quotes and at start-of-line
    or preceded by whitespace (` ` or `\\t`).
    """
    in_single = False
    in_double = False
    idx = 0
    n = len(line)
    while idx < n:
        ch = line[idx]
        if in_single:
            if ch == "'":
                if idx + 1 < n and line[idx + 1] == "'":
                    idx += 2
                    continue
                in_single = False
        elif in_double:
            if ch == "\\":
                idx += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "#" and (idx == 0 or line[idx - 1] in (" ", "\t")):
                return line[:idx]
        idx += 1
    return line


def has_template_syntax(text: str) -> bool:
    """Return True if non-comment text contains Helm/Go templates or unescaped envsubst markers."""
    uncommented = "\n".join(_strip_unquoted_yaml_comment(line) for line in text.splitlines())
    return _has_helm_delimiters(uncommented) or _has_unescaped_envsubst(uncommented)


def _inspect_decoded_value(value: Any) -> tuple[bool, str, str]:
    """Validate decoded scalars and strings without discarding valid resource dicts on string errors.

    Returns `(saw_template, first_string_err_msg, first_string_err_code)` and raises
    `ManifestParseError` on structural/numeric range violations.
    """
    templated = False
    first_err = ""
    first_code = ""
    stack: list[Any] = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if not first_err:
                violation = _find_string_safety_violation(cur, context="decoded manifest string")
                if violation is not None:
                    first_err, first_code = violation
            if not templated and _string_has_template_syntax(cur):
                templated = True
        elif isinstance(cur, bool) or cur is None:
            continue
        elif isinstance(cur, int):
            if cur.bit_length() > MAX_INT_BITS:
                raise ManifestParseError(
                    f"integer exceeds {MAX_INT_BITS}-bit safety bound",
                    code="NUMERIC_VALUE_OUT_OF_RANGE",
                )
        elif isinstance(cur, float):
            if not math.isfinite(cur):
                raise ManifestParseError(
                    f"non-finite floating-point value {cur!r} is not allowed",
                    code="NON_FINITE_NUMBER",
                )
        elif isinstance(cur, dict):
            for k, v in cur.items():
                if not isinstance(k, str):
                    raise ManifestParseError(
                        f"mapping key {k!r} ({type(k).__name__}) must be a string",
                        code="YAML_NON_STRING_KEY",
                    )
                if not first_err:
                    violation = _find_string_safety_violation(k, context="decoded mapping key")
                    if violation is not None:
                        first_err, first_code = violation
                if not templated and _string_has_template_syntax(k):
                    templated = True
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
        else:
            raise ManifestParseError(
                f"unsupported decoded manifest value type {type(cur).__name__}",
                code="UNSUPPORTED_SCALAR_TYPE",
            )
    return templated, first_err, first_code


_BaseYAMLLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _StrictYAMLLoader(_BaseYAMLLoader):
    """YAML 1.2-strict SafeLoader rejecting duplicate keys, merge keys, non-string keys, and YAML 1.1 traps."""


class _RecoveryYAMLLoader(_BaseYAMLLoader):
    """Bounded anti-masking YAML 1.2 loader used only AFTER strict validation records an error.

    Expands bounded anchors/aliases (`<= MAX_RECOVERY_ALIASES`) and resolves duplicate
    string keys with Kubernetes/Go last-key-wins semantics so an attacker can never mask
    a `high`/`critical` resource behind `&anchor`, `<<`, or duplicate keys.
    """


def _configure_yaml12_resolvers(loader_cls: type, *, allow_merge: bool = False) -> None:
    loader_cls.yaml_implicit_resolvers = {
        k: [
            (tag, regexp)
            for tag, regexp in v
            if tag
            not in (
                "tag:yaml.org,2002:null",
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:float",
                "tag:yaml.org,2002:timestamp",
                "tag:yaml.org,2002:merge",
                "tag:yaml.org,2002:value",
            )
        ]
        for k, v in _BaseYAMLLoader.yaml_implicit_resolvers.items()
    }
    loader_cls.add_implicit_resolver(
        "tag:yaml.org,2002:null",
        re.compile(r"^(?:|null|Null|NULL|~)$"),
        ["", "n", "N", "~"],
    )
    loader_cls.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    loader_cls.add_implicit_resolver(
        "tag:yaml.org,2002:int",
        re.compile(r"^(?:[-+]?[0-9]+|[-+]?0o[0-7]+|[-+]?0x[0-9a-fA-F]+)$"),
        list("-+0123456789"),
    )
    loader_cls.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        _YAML12_FLOAT_RE,
        list("-+0123456789."),
    )
    if allow_merge:
        loader_cls.add_implicit_resolver(
            "tag:yaml.org,2002:merge",
            re.compile(r"^<<$"),
            ["<"],
        )


_configure_yaml12_resolvers(_StrictYAMLLoader, allow_merge=False)
_configure_yaml12_resolvers(_RecoveryYAMLLoader, allow_merge=True)


def _construct_strict_null(loader: Any, node: yaml.Node) -> None:
    if not isinstance(node, yaml.ScalarNode) or node.value not in _YAML12_NULL_TOKENS:
        raise ManifestParseError(
            f"line {node.start_mark.line + 1}: invalid YAML 1.2 null literal {getattr(node, 'value', node)!r}",
            code="YAML_SYNTAX_ERROR",
        )
    return None


def _construct_strict_bool(loader: Any, node: yaml.Node) -> bool:
    if not isinstance(node, yaml.ScalarNode):
        raise ManifestParseError(f"line {node.start_mark.line + 1}: expected scalar boolean", code="YAML_SYNTAX_ERROR")
    if node.value in _YAML12_TRUE_TOKENS:
        return True
    if node.value in _YAML12_FALSE_TOKENS:
        return False
    raise ManifestParseError(
        f"line {node.start_mark.line + 1}: invalid YAML 1.2 boolean literal {node.value!r}",
        code="YAML_SYNTAX_ERROR",
    )


def _parse_limited_int(raw_val: str, base: int, line_no: int) -> int:
    digits = raw_val.lstrip("+-")
    if base in (8, 16):
        digits = digits[2:]
    bits_per_char = 4 if base == 16 else (3 if base == 8 else 4)
    if len(digits.lstrip("0")) * bits_per_char > MAX_INT_BITS + 8:
        raise ManifestParseError(
            f"line {line_no}: integer literal exceeds {MAX_INT_BITS}-bit safety bound",
            code="NUMERIC_VALUE_OUT_OF_RANGE",
        )
    val = int(raw_val, base)
    if val.bit_length() > MAX_INT_BITS:
        raise ManifestParseError(
            f"line {line_no}: integer literal exceeds {MAX_INT_BITS}-bit safety bound",
            code="NUMERIC_VALUE_OUT_OF_RANGE",
        )
    return val


def _construct_strict_int(loader: Any, node: yaml.Node) -> int:
    if not isinstance(node, yaml.ScalarNode):
        raise ManifestParseError(f"line {node.start_mark.line + 1}: expected scalar integer", code="YAML_SYNTAX_ERROR")
    raw = node.value
    line_no = node.start_mark.line + 1
    if _YAML12_AMBIGUOUS_OCTAL_RE.fullmatch(raw):
        raise ManifestParseError(
            f"line {line_no}: ambiguous leading-zero integer {raw!r} (use 0o... for octal or remove leading zero)",
            code="YAML_AMBIGUOUS_OCTAL",
        )
    if _YAML12_INT_DEC_RE.fullmatch(raw):
        return _parse_limited_int(raw, 10, line_no)
    if _YAML12_INT_OCT_RE.fullmatch(raw):
        sign = -1 if raw.startswith("-") else 1
        body = raw.lstrip("+-")[2:]
        return sign * _parse_limited_int(body, 8, line_no)
    if _YAML12_INT_HEX_RE.fullmatch(raw):
        sign = -1 if raw.startswith("-") else 1
        body = raw.lstrip("+-")[2:]
        return sign * _parse_limited_int(body, 16, line_no)
    raise ManifestParseError(
        f"line {line_no}: invalid YAML 1.2 integer literal {raw!r}",
        code="YAML_SYNTAX_ERROR",
    )


def _construct_strict_float(loader: Any, node: yaml.Node) -> float:
    if not isinstance(node, yaml.ScalarNode):
        raise ManifestParseError(f"line {node.start_mark.line + 1}: expected scalar float", code="YAML_SYNTAX_ERROR")
    raw = node.value
    line_no = node.start_mark.line + 1
    if not _YAML12_FLOAT_RE.fullmatch(raw):
        raise ManifestParseError(
            f"line {line_no}: invalid YAML 1.2 float literal {raw!r}",
            code="YAML_SYNTAX_ERROR",
        )
    low = raw.lower()
    if "nan" in low or "inf" in low:
        raise ManifestParseError(
            f"line {line_no}: non-finite YAML float literal {raw!r} is not allowed",
            code="NON_FINITE_NUMBER",
        )
    val = float(raw)
    if not math.isfinite(val):
        raise ManifestParseError(
            f"line {line_no}: float literal {raw!r} overflows to non-finite value",
            code="NON_FINITE_NUMBER",
        )
    return val


def _typed_key_sig(key: Any) -> tuple[type, Any]:
    if isinstance(key, float) and math.isnan(key):
        return (float, _NAN_SENTINEL)
    return (type(key), key)


def _construct_strict_mapping(loader: _StrictYAMLLoader, node: yaml.Node, deep: bool = False) -> dict[str, Any]:
    if not isinstance(node, yaml.MappingNode):
        raise ManifestParseError("expected YAML mapping node")
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        line_no = key_node.start_mark.line + 1
        if key_node.tag == "tag:yaml.org,2002:merge" or (
            isinstance(key_node, yaml.ScalarNode) and key_node.value == "<<" and key_node.style is None
        ):
            raise ManifestParseError(
                f"line {line_no}: YAML merge keys (`<<`) are not supported",
                code="YAML_MERGE_KEY",
            )
        if not isinstance(key_node, yaml.ScalarNode):
            raise ManifestParseError(
                f"line {line_no}: non-scalar YAML mapping key",
                code="YAML_COMPLEX_KEY",
            )
        key = loader.construct_object(key_node, deep=deep)
        if key == "<<":
            raise ManifestParseError(
                f"line {line_no}: YAML merge key (`<<`) is not supported",
                code="YAML_MERGE_KEY",
            )
        if not isinstance(key, str):
            raise ManifestParseError(
                f"line {line_no}: non-string YAML mapping key {key!r} ({type(key).__name__})",
                code="YAML_NON_STRING_KEY",
            )
        if key in seen:
            raise ManifestParseError(
                f"line {line_no}: duplicate YAML key {key!r}",
                code="YAML_DUPLICATE_KEY",
            )
        seen.add(key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


def _construct_recovery_mapping(loader: _RecoveryYAMLLoader, node: yaml.Node, deep: bool = False) -> dict[str, Any]:
    if not isinstance(node, yaml.MappingNode):
        raise ManifestParseError("expected YAML mapping node")
    loader.flatten_mapping(node)
    if len(node.value) > MAX_NODES_PER_FILE:
        raise ManifestParseError(
            f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
            code="YAML_TOO_MANY_NODES",
        )
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        line_no = key_node.start_mark.line + 1
        if not isinstance(key_node, yaml.ScalarNode):
            raise ManifestParseError(
                f"line {line_no}: non-scalar YAML mapping key",
                code="YAML_COMPLEX_KEY",
            )
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ManifestParseError(
                f"line {line_no}: non-string YAML mapping key {key!r} ({type(key).__name__})",
                code="YAML_NON_STRING_KEY",
            )
        # Last-key-wins matches Go `sigs.k8s.io/yaml` / `encoding/json` unmarshaling
        result[key] = loader.construct_object(value_node, deep=True)
    return result


for _cls in (_StrictYAMLLoader, _RecoveryYAMLLoader):
    _cls.add_constructor("tag:yaml.org,2002:null", _construct_strict_null)
    _cls.add_constructor("tag:yaml.org,2002:bool", _construct_strict_bool)
    _cls.add_constructor("tag:yaml.org,2002:int", _construct_strict_int)
    _cls.add_constructor("tag:yaml.org,2002:float", _construct_strict_float)

_StrictYAMLLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
)
_RecoveryYAMLLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_recovery_mapping,
)


_ALLOWED_YAML_EXPLICIT_TAGS = frozenset(
    {
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:map",
    }
)
_ALLOWED_RECOVERY_YAML_TAGS = _ALLOWED_YAML_EXPLICIT_TAGS | frozenset({"tag:yaml.org,2002:merge"})


def _check_yaml_tag(ev: Any, *, allow_merge: bool = False) -> None:
    tag = getattr(ev, "tag", None)
    allowed = _ALLOWED_RECOVERY_YAML_TAGS if allow_merge else _ALLOWED_YAML_EXPLICIT_TAGS
    if tag and tag not in allowed:
        raise ManifestParseError(
            f"line {ev.start_mark.line + 1}: unsupported YAML tag {tag!r}",
            code="YAML_UNSUPPORTED_TAG",
        )


def _scan_yaml_events(
    text: str,
    *,
    max_docs: int = MAX_DOCS_PER_FILE,
    max_nodes: int = MAX_NODES_PER_FILE,
) -> tuple[int, int]:
    """Validate YAML event stream for directives, anchors/aliases, tags, and depth/node caps."""
    depth = 0
    nodes = 0
    docs = 0
    try:
        for ev in yaml.parse(text, Loader=_StrictYAMLLoader):
            if isinstance(ev, yaml.DocumentStartEvent):
                if ev.version is not None and ev.version != (1, 2):
                    raise ManifestParseError(
                        f"line {ev.start_mark.line + 1}: unsupported %YAML directive version {ev.version!r}",
                        code="YAML_UNSUPPORTED_DIRECTIVE",
                    )
                if ev.tags:
                    raise ManifestParseError(
                        f"line {ev.start_mark.line + 1}: custom %TAG directives are not supported",
                        code="YAML_UNSUPPORTED_DIRECTIVE",
                    )
                docs += 1
                if docs > max_docs:
                    raise ManifestParseError(
                        f"manifest exceeds maximum of {MAX_DOCS_PER_FILE} documents",
                        code="TOO_MANY_DOCUMENTS",
                    )
            elif isinstance(ev, yaml.AliasEvent):
                raise ManifestParseError(
                    f"line {ev.start_mark.line + 1}: YAML alias `*{ev.anchor}` is not supported",
                    code="YAML_ANCHOR_OR_ALIAS",
                )
            elif isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                _check_yaml_tag(ev)
                if ev.anchor:
                    raise ManifestParseError(
                        f"line {ev.start_mark.line + 1}: YAML anchor `&{ev.anchor}` is not supported",
                        code="YAML_ANCHOR_OR_ALIAS",
                    )
                depth += 1
                nodes += 1
                if depth > MAX_NESTING_DEPTH:
                    raise ManifestParseError(
                        f"manifest exceeds maximum nesting depth ({MAX_NESTING_DEPTH})",
                        code="YAML_NESTING_TOO_DEEP",
                    )
                if nodes > max_nodes:
                    raise ManifestParseError(
                        f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                        code="YAML_TOO_MANY_NODES",
                    )
            elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth = max(0, depth - 1)
            elif isinstance(ev, yaml.ScalarEvent):
                _check_yaml_tag(ev)
                if ev.anchor:
                    raise ManifestParseError(
                        f"line {ev.start_mark.line + 1}: YAML anchor `&{ev.anchor}` is not supported",
                        code="YAML_ANCHOR_OR_ALIAS",
                    )
                nodes += 1
                if nodes > max_nodes:
                    raise ManifestParseError(
                        f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                        code="YAML_TOO_MANY_NODES",
                    )
    except ManifestParseError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ManifestParseError(f"YAML syntax error: {exc}", code="YAML_SYNTAX_ERROR") from exc
    return nodes, docs


def _scan_yaml_events_recovery(
    text: str,
    *,
    max_docs: int = MAX_DOCS_PER_FILE,
    max_nodes: int = MAX_NODES_PER_FILE,
    max_aliases: int = MAX_RECOVERY_ALIASES,
) -> tuple[int, int]:
    """Bounded recovery pre-scan allowing `<= max_aliases` aliases before `_RecoveryYAMLLoader`."""
    depth = 0
    nodes = 0
    docs = 0
    aliases = 0
    for ev in yaml.parse(text, Loader=_RecoveryYAMLLoader):
        if isinstance(ev, yaml.DocumentStartEvent):
            if ev.tags:
                raise ManifestParseError(
                    f"line {ev.start_mark.line + 1}: custom %TAG directives are not supported",
                    code="YAML_UNSUPPORTED_DIRECTIVE",
                )
            docs += 1
            if docs > max_docs:
                raise ManifestParseError(
                    f"manifest exceeds maximum of {MAX_DOCS_PER_FILE} documents",
                    code="TOO_MANY_DOCUMENTS",
                )
        elif isinstance(ev, yaml.AliasEvent):
            aliases += 1
            nodes += 1
            if aliases > max_aliases:
                raise ManifestParseError(
                    f"manifest exceeds maximum recovery alias count ({max_aliases})",
                    code="YAML_ANCHOR_OR_ALIAS",
                )
            if nodes > max_nodes:
                raise ManifestParseError(
                    f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                    code="YAML_TOO_MANY_NODES",
                )
        elif isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            _check_yaml_tag(ev, allow_merge=True)
            depth += 1
            nodes += 1
            if depth > MAX_NESTING_DEPTH:
                raise ManifestParseError(
                    f"manifest exceeds maximum nesting depth ({MAX_NESTING_DEPTH})",
                    code="YAML_NESTING_TOO_DEEP",
                )
            if nodes > max_nodes:
                raise ManifestParseError(
                    f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                    code="YAML_TOO_MANY_NODES",
                )
        elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth = max(0, depth - 1)
        elif isinstance(ev, yaml.ScalarEvent):
            _check_yaml_tag(ev, allow_merge=True)
            nodes += 1
            if nodes > max_nodes:
                raise ManifestParseError(
                    f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                    code="YAML_TOO_MANY_NODES",
                )
    return nodes, docs


def _check_expanded_limits(
    value: Any,
    *,
    too_many_code: str = "JSON_TOO_MANY_NODES",
    too_deep_code: str = "JSON_NESTING_TOO_DEEP",
    max_nodes: int = MAX_NODES_PER_FILE,
) -> int:
    """Enforce tree-expanded node and depth bounds (blocks alias DAG / Billion Laughs bombs)."""
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        cur, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ManifestParseError(
                f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                code=too_many_code,
            )
        if depth > MAX_NESTING_DEPTH:
            raise ManifestParseError(
                f"manifest exceeds maximum nesting depth ({MAX_NESTING_DEPTH})",
                code=too_deep_code,
            )
        if isinstance(cur, dict):
            for v in cur.values():
                stack.append((v, depth + 1))
        elif isinstance(cur, list):
            for item in cur:
                stack.append((item, depth + 1))
    return nodes


def _recover_valid_yaml_docs(text: str) -> tuple[list[Any], bool]:
    """Bounded anti-masking YAML recovery across single-doc and multi-doc streams.

    Runs only after strict validation has already set `first_error` / `first_error_code`,
    recovering valid Kubernetes documents across BiDi/control characters, bounded anchors/aliases
    (`<= MAX_RECOVERY_ALIASES` + expanded `MAX_NODES_PER_FILE` cap so Billion Laughs stays blocked),
    merge keys, duplicate string keys, or broken sibling `---` documents.
    """
    raw_chunks = _YAML_DOC_SPLIT_RE.split(text)
    if len(raw_chunks) > MAX_DOCS_PER_FILE + 1:
        return [], False
    chunks = raw_chunks if len(raw_chunks) > 1 else [text]
    recovered: list[Any] = []
    used_nodes = 0
    used_docs = 0
    saw_decoded_template = False
    for chunk in chunks:
        if not chunk or not chunk.strip():
            continue
        if used_docs >= MAX_DOCS_PER_FILE or used_nodes >= MAX_NODES_PER_FILE:
            break
        try:
            _, c_docs = _scan_yaml_events_recovery(
                chunk,
                max_docs=MAX_DOCS_PER_FILE - used_docs,
                max_nodes=MAX_NODES_PER_FILE - used_nodes,
            )
            loaded_docs = list(yaml.load_all(chunk, Loader=_RecoveryYAMLLoader))
            expanded_chunk_nodes = 0
            for doc in loaded_docs:
                expanded_chunk_nodes += _check_expanded_limits(
                    doc,
                    too_many_code="YAML_TOO_MANY_NODES",
                    too_deep_code="YAML_NESTING_TOO_DEEP",
                    max_nodes=MAX_NODES_PER_FILE - used_nodes - expanded_chunk_nodes,
                )
                doc_tmpl, _, _ = _inspect_decoded_value(doc)
                if doc_tmpl:
                    saw_decoded_template = True
        except (ManifestParseError, yaml.YAMLError, RecursionError, ValueError, TypeError):
            continue
        used_nodes += expanded_chunk_nodes
        used_docs += c_docs
        recovered.extend(loaded_docs)
    return recovered, saw_decoded_template


def _split_api_version(api_version: str) -> tuple[str, str]:
    """Split and validate Kubernetes `apiVersion` into `(group, version)`."""
    if not api_version or any(ch.isspace() for ch in api_version):
        raise ManifestParseError(f"invalid Kubernetes apiVersion {api_version!r}", code="INVALID_APIVERSION")
    if "/" in api_version:
        parts = api_version.split("/")
        if (
            len(parts) != 2
            or not _API_GROUP_RE.fullmatch(parts[0])
            or not _API_VERSION_TOKEN_RE.fullmatch(parts[1])
        ):
            raise ManifestParseError(f"malformed Kubernetes apiVersion {api_version!r}", code="INVALID_APIVERSION")
        return parts[0], parts[1]
    if api_version != "v1":
        raise ManifestParseError(
            f"invalid core Kubernetes apiVersion {api_version!r} (expected 'v1' or '<group>/<version>')",
            code="INVALID_APIVERSION",
        )
    return "", api_version


def _redact_secret_value(val: Any) -> str:
    """Deterministic one-way token so Secret value rotations remain diffable without cleartext."""
    payload = f"{type(val).__qualname__}:{val!r}".encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{_REDACTED_PREFIX}{digest}>"


def _redact_secret_object(obj: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a core `Secret` with `data`, `stringData`, and `last-applied-configuration` redacted."""
    clean = copy.deepcopy(obj)
    for secret_field in ("data", "stringData"):
        val = clean.get(secret_field)
        if isinstance(val, dict):
            clean[secret_field] = {
                k: (v if k == "$patch" else _redact_secret_value(v))
                for k, v in val.items()
            }
        elif val is not None:
            clean[secret_field] = _redact_secret_value(val)
    meta = clean.get("metadata")
    if isinstance(meta, dict):
        annos = meta.get("annotations")
        if isinstance(annos, dict) and _LAST_APPLIED_ANNOTATION in annos:
            annos[_LAST_APPLIED_ANNOTATION] = _redact_secret_value(annos[_LAST_APPLIED_ANNOTATION])
    return clean


def _is_list_envelope(doc: dict[str, Any], kind: str) -> bool:
    """Distinguish Kubernetes `List`/`*List` envelopes from named CRDs whose kind ends in `List`."""
    if not (kind == "List" or kind.endswith("List")):
        return False
    if not isinstance(doc.get("items"), list):
        return False
    meta = doc.get("metadata")
    if isinstance(meta, dict):
        raw_name = meta.get("name")
        raw_gen = meta.get("generateName")
        if (isinstance(raw_name, str) and raw_name.strip()) or (isinstance(raw_gen, str) and raw_gen.strip()):
            return False
    return True


def _extract_resources(
    doc: Any,
    out: list[ParsedResource],
    *,
    depth: int = 0,
    default_api_version: str = "",
    default_kind: str = "",
    recovery_mode: bool = False,
) -> tuple[bool, bool, str, str]:
    """Extract Kubernetes resources from a decoded doc.

    Returns `(is_k8s_object, saw_kustomize, first_error_msg, first_error_code)`.
    Never aborts before inspecting remaining items in a `List` envelope.
    """
    if doc is None:
        return True, False, "", ""
    if not isinstance(doc, dict):
        return False, False, "", ""

    raw_api = doc.get("apiVersion", default_api_version if default_api_version else None)
    raw_kind = doc.get("kind", default_kind if default_kind else None)
    if not isinstance(raw_api, str) or not isinstance(raw_kind, str):
        return False, False, "", ""

    api_version = raw_api.strip()
    kind = raw_kind.strip()
    if not api_version or not kind:
        return False, False, "", ""

    api_group = api_version.partition("/")[0]
    if api_group == "kustomize.config.k8s.io" or (
        api_group != "kustomize.toolkit.fluxcd.io" and kind in ("Kustomization", "Component")
    ):
        return True, True, "", ""

    group, version = _split_api_version(api_version)

    # Unpack Kubernetes List / *List envelopes
    if _is_list_envelope(doc, kind):
        if depth > MAX_LIST_NESTING_DEPTH:
            raise ManifestParseError("nested Kubernetes List exceeds depth limit", code="LIST_TOO_DEEP")
        extra_keys = set(doc.keys()) - _LIST_ENVELOPE_ALLOWED_KEYS
        first_err = ""
        first_code = ""
        if extra_keys:
            first_err = f"Kubernetes {kind} contains unexpected top-level fields: {sorted(extra_keys)}"
            first_code = "INVALID_LIST_ITEM"
        inferred_kind = kind[:-4] if (kind != "List" and len(kind) > 4 and kind.endswith("List")) else ""
        inferred_api = api_version if inferred_kind else ""
        saw_kust = False
        for item in doc["items"]:
            if len(out) >= MAX_DOCS_PER_FILE:
                raise ManifestParseError(
                    f"manifest exceeds maximum of {MAX_DOCS_PER_FILE} resources",
                    code="TOO_MANY_DOCUMENTS",
                )
            try:
                ok, is_kust, sub_err, sub_code = _extract_resources(
                    item,
                    out,
                    depth=depth + 1,
                    default_api_version=inferred_api,
                    default_kind=inferred_kind,
                    recovery_mode=recovery_mode,
                )
            except ManifestParseError as exc:
                if not first_err:
                    first_err, first_code = str(exc), exc.code
                continue
            if is_kust:
                saw_kust = True
            if sub_err and not first_err:
                first_err, first_code = sub_err, sub_code
            if not ok and not first_err:
                first_err = "Kubernetes List contains non-resource item"
                first_code = "INVALID_LIST_ITEM"
        return True, saw_kust, first_err, first_code

    meta = doc.get("metadata")
    if meta is not None and not isinstance(meta, dict):
        raise ManifestParseError("Kubernetes metadata must be a mapping", code="INVALID_K8S_METADATA")
    meta_dict = meta if isinstance(meta, dict) else {}
    raw_name = meta_dict.get("name", "")
    raw_ns = meta_dict.get("namespace", "")
    if raw_name is not None and not isinstance(raw_name, str):
        raise ManifestParseError("Kubernetes metadata.name must be a string", code="INVALID_K8S_METADATA")
    if raw_ns is not None and not isinstance(raw_ns, str):
        raise ManifestParseError("Kubernetes metadata.namespace must be a string", code="INVALID_K8S_METADATA")
    raw_gen = meta_dict.get("generateName", "")
    if raw_gen is not None and not isinstance(raw_gen, str):
        raise ManifestParseError("Kubernetes metadata.generateName must be a string", code="INVALID_K8S_METADATA")
    name = (raw_name or "").strip()
    gen_name = (raw_gen or "").strip()
    namespace = (raw_ns or "").strip()
    if not name:
        if gen_name:
            raise ManifestParseError(
                "Kubernetes manifest uses metadata.generateName without a deterministic metadata.name",
                code="INVALID_K8S_METADATA",
            )
        return False, False, "", ""

    normalized = dict(doc)
    normalized["apiVersion"] = api_version
    normalized["kind"] = kind

    ref = ResourceRef(
        group=group,
        version=version,
        kind=kind,
        namespace=namespace,
        name=name,
    )
    if group == "" and kind == "Secret":
        redacted = _redact_secret_object(normalized)
        out.append(ParsedResource(ref=ref, obj=redacted, sanitized=redacted))
    else:
        out.append(ParsedResource(ref=ref, obj=normalized, sanitized=normalized))
    return True, False, "", ""


def _reject_json_constant(token: str) -> Any:
    raise ManifestParseError(f"non-standard JSON constant {token!r} is not allowed", code="JSON_INVALID_CONSTANT")


def _parse_json_float(token: str) -> float:
    val = float(token)
    if not math.isfinite(val):
        raise ManifestParseError(
            f"JSON number {token!r} overflows to non-finite float",
            code="JSON_INVALID_CONSTANT",
        )
    return val


def _parse_json_int(token: str) -> int:
    digits = token.lstrip("-")
    if len(digits.lstrip("0")) * 4 > MAX_INT_BITS + 8:
        raise ManifestParseError(
            f"JSON integer exceeds {MAX_INT_BITS}-bit safety bound",
            code="NUMERIC_VALUE_OUT_OF_RANGE",
        )
    val = int(token)
    if val.bit_length() > MAX_INT_BITS:
        raise ManifestParseError(
            f"JSON integer exceeds {MAX_INT_BITS}-bit safety bound",
            code="NUMERIC_VALUE_OUT_OF_RANGE",
        )
    return val


def _check_json_limits(value: Any) -> None:
    _check_expanded_limits(
        value,
        too_many_code="JSON_TOO_MANY_NODES",
        too_deep_code="JSON_NESTING_TOO_DEEP",
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise ManifestParseError(f"duplicate JSON key {key!r}", code="JSON_DUPLICATE_KEY")
        out[key] = val
    return out


def parse_manifest_bytes(raw: bytes | None, *, is_json: bool = False) -> ParseOutcome:
    """Parse a YAML or JSON file into normalized Kubernetes resources.

    Returns a `ParseOutcome` distinguishing:
    - valid Kubernetes manifests (`resources` populated, `is_k8s_manifest=True`)
    - Kustomize resources (`is_kustomize=True`, still extracting sibling K8s docs)
    - unrendered templates (`is_template=True`, still parsing any valid embedded resources
      so template markers in a benign field or sibling doc never mask a critical/high change)
    - valid non-Kubernetes YAML/JSON (`is_k8s_manifest=False`, `error=""`)
    - syntax/security/size errors (`error` and `error_code` set, still preserving valid
      resources via bounded anti-masking recovery across BiDi escapes, duplicate keys, and
      bounded YAML anchors/aliases so `critical`/`high` changes never downgrade to `unknown`)
    """
    if raw is None or not raw.strip():
        return ParseOutcome(resources=(), is_k8s_manifest=True)
    try:
        text = _decode_raw_utf8(raw)
    except ManifestParseError as exc:
        return ParseOutcome(is_k8s_manifest=False, error=str(exc), error_code=exc.code)

    raw_templated = has_template_syntax(text)
    templated = False
    first_error = ""
    first_error_code = ""
    used_recovery = False

    violation = _find_string_safety_violation(text, context="manifest")
    if violation is not None:
        first_error, first_error_code = violation

    docs: list[Any] = []
    if is_json:
        loaded: Any = None
        have_loaded = False
        try:
            loaded = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
                parse_float=_parse_json_float,
                parse_int=_parse_json_int,
            )
            _check_json_limits(loaded)
            have_loaded = True
        except ManifestParseError as exc:
            if not first_error:
                first_error = str(exc)
                first_error_code = exc.code
            if exc.code == "JSON_DUPLICATE_KEY":
                # Bounded anti-masking fallback: preserve JSON_DUPLICATE_KEY while extracting K8s objects
                try:
                    loaded = json.loads(
                        text,
                        parse_constant=_reject_json_constant,
                        parse_float=_parse_json_float,
                        parse_int=_parse_json_int,
                    )
                    _check_json_limits(loaded)
                    have_loaded = True
                    used_recovery = True
                except (ManifestParseError, ValueError, RecursionError):
                    have_loaded = False
        except (ValueError, RecursionError) as exc:
            if raw_templated and not first_error:
                return ParseOutcome(is_k8s_manifest=False, is_template=True)
            if not first_error:
                first_error = f"JSON syntax error: {exc}"
                first_error_code = "JSON_SYNTAX_ERROR"

        if not have_loaded:
            return ParseOutcome(
                is_k8s_manifest=False,
                is_template=raw_templated,
                error=first_error,
                error_code=first_error_code,
            )

        try:
            dec_tmpl, str_err, str_code = _inspect_decoded_value(loaded)
            if dec_tmpl:
                templated = True
            if str_err and not first_error:
                first_error, first_error_code = str_err, str_code
        except ManifestParseError as exc:
            if not first_error:
                first_error, first_error_code = str(exc), exc.code
            return ParseOutcome(
                is_k8s_manifest=False,
                is_template=templated or raw_templated,
                error=first_error,
                error_code=first_error_code,
            )

        if isinstance(loaded, list):
            if len(loaded) > MAX_DOCS_PER_FILE:
                return ParseOutcome(
                    is_k8s_manifest=False,
                    is_template=templated,
                    error=first_error or f"JSON array exceeds maximum of {MAX_DOCS_PER_FILE} documents",
                    error_code=first_error_code or "TOO_MANY_DOCUMENTS",
                )
            docs = loaded
        else:
            docs = [loaded]
    else:
        strict_succeeded = False
        try:
            _scan_yaml_events(text)
            docs = list(yaml.load_all(text, Loader=_StrictYAMLLoader))
            for d in docs:
                dec_tmpl, str_err, str_code = _inspect_decoded_value(d)
                if dec_tmpl:
                    templated = True
                if str_err and not first_error:
                    first_error, first_error_code = str_err, str_code
            strict_succeeded = True
        except ManifestParseError as exc:
            if not first_error:
                first_error = str(exc)
                first_error_code = exc.code
        except (yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
            if not first_error:
                first_error = f"YAML syntax error: {exc}"
                first_error_code = "YAML_SYNTAX_ERROR"

        if not strict_succeeded:
            if raw_templated:
                templated = True
            used_recovery = True
            docs, rec_templated = _recover_valid_yaml_docs(text)
            if rec_templated:
                templated = True
            if templated and first_error_code == "YAML_SYNTAX_ERROR":
                first_error = ""
                first_error_code = ""
                if not docs:
                    return ParseOutcome(is_k8s_manifest=False, is_template=True)
            elif not docs:
                return ParseOutcome(
                    is_k8s_manifest=False,
                    is_template=templated,
                    error=first_error,
                    error_code=first_error_code,
                )

    resources: list[ParsedResource] = []
    non_empty_docs = [d for d in docs if d is not None]
    if not non_empty_docs:
        if first_error:
            return ParseOutcome(
                resources=(),
                is_k8s_manifest=False,
                is_template=templated,
                error=first_error,
                error_code=first_error_code,
            )
        return ParseOutcome(resources=(), is_k8s_manifest=not templated, is_template=templated)

    saw_k8s = False
    saw_non_k8s = False
    saw_kustomize = False
    for doc in non_empty_docs:
        if len(resources) >= MAX_DOCS_PER_FILE:
            if not first_error:
                first_error = f"manifest exceeds maximum of {MAX_DOCS_PER_FILE} resources"
                first_error_code = "TOO_MANY_DOCUMENTS"
            break
        try:
            is_k8s, is_kust, sub_err, sub_code = _extract_resources(
                doc,
                resources,
                recovery_mode=bool(first_error or used_recovery),
            )
        except ManifestParseError as exc:
            if not first_error:
                first_error = str(exc)
                first_error_code = exc.code
            continue
        if sub_err and not first_error:
            first_error = sub_err
            first_error_code = sub_code
        if is_kust:
            saw_kustomize = True
            continue
        if is_k8s:
            saw_k8s = True
        else:
            saw_non_k8s = True

    if first_error:
        return ParseOutcome(
            resources=tuple(resources),
            is_k8s_manifest=False,
            is_template=templated,
            is_kustomize=saw_kustomize,
            error=first_error,
            error_code=first_error_code,
        )
    if saw_kustomize:
        return ParseOutcome(
            resources=tuple(resources),
            is_k8s_manifest=False,
            is_template=templated,
            is_kustomize=True,
        )
    if saw_non_k8s and saw_k8s:
        return ParseOutcome(
            resources=tuple(resources),
            is_k8s_manifest=False,
            is_template=templated,
            error="multi-document file mixes Kubernetes resources with non-Kubernetes or incomplete documents",
            error_code="MIXED_OR_INCOMPLETE_MANIFEST",
        )
    if saw_non_k8s:
        return ParseOutcome(resources=(), is_k8s_manifest=False, is_template=templated)
    return ParseOutcome(resources=tuple(resources), is_k8s_manifest=not templated, is_template=templated)


def _strict_equal(a: Any, b: Any) -> bool:
    """Typed equality where `True != 1`, `False != 0`, `{1: x} != {True: x}`, and `NaN == NaN`."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, dict):
        if len(a) != len(b):
            return False
        if all(type(k) is str for k in a) and all(type(k) is str for k in b):
            if a.keys() != b.keys():
                return False
            return all(_strict_equal(a[k], b[k]) for k in a)
        b_typed = {_typed_key_sig(k): v for k, v in b.items()}
        if len(b_typed) != len(b):
            return False
        for k, av in a.items():
            sig = _typed_key_sig(k)
            if sig not in b_typed or not _strict_equal(av, b_typed[sig]):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(_strict_equal(x, y) for x, y in zip(a, b))
    return bool(a == b)


def _expand_leaves(path: tuple[str, ...], val: Any, op: str, out: list[FieldDiff]) -> None:
    """Expand a composite dict/list being added or removed into leaf FieldDiffs."""
    if isinstance(val, dict):
        if not val:
            out.append(
                FieldDiff(
                    tokens=path,
                    op=op,
                    before=val if op == "remove" else None,
                    after=val if op == "add" else None,
                )
            )
            return
        for k in sorted(val.keys(), key=lambda x: (type(x).__qualname__, str(x))):
            _expand_leaves(path + (str(k),), val[k], op, out)
        return
    if isinstance(val, list):
        if not val:
            out.append(
                FieldDiff(
                    tokens=path,
                    op=op,
                    before=val if op == "remove" else None,
                    after=val if op == "add" else None,
                )
            )
            return
        for idx, item in enumerate(val):
            _expand_leaves(path + (str(idx),), item, op, out)
        return
    out.append(
        FieldDiff(
            tokens=path,
            op=op,
            before=val if op == "remove" else None,
            after=val if op == "add" else None,
        )
    )


def _named_list_item_name(item: Any, parent_key: str) -> str | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if name is None and parent_key == "volumeClaimTemplates":
        meta = item.get("metadata")
        if isinstance(meta, dict):
            name = meta.get("name")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        return None
    return name


def _named_list_map(items: list[Any], parent_key: str = "") -> tuple[dict[str, dict[str, Any]], list[str]] | None:
    """If every element is a dict with a unique trimmed non-empty `name`, return `(name -> dict, ordered_names)`."""
    mapping: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        name = _named_list_item_name(item, parent_key)
        if name is None or name in mapping:
            return None
        mapping[name] = item
        order.append(name)
    return mapping, order


def _diff_values(path: tuple[str, ...], before: Any, after: Any, out: list[FieldDiff]) -> None:
    if _strict_equal(before, after):
        return
    if before is not None and after is None:
        if isinstance(before, (dict, list)):
            _expand_leaves(path, before, "remove", out)
        else:
            out.append(FieldDiff(tokens=path, op="remove", before=before, after=None))
        return
    if before is None and after is not None:
        if isinstance(after, (dict, list)):
            _expand_leaves(path, after, "add", out)
        else:
            out.append(FieldDiff(tokens=path, op="add", before=None, after=after))
        return

    if isinstance(before, dict) and isinstance(after, dict):
        if all(type(k) is str for k in before) and all(type(k) is str for k in after):
            for k in sorted(set(before.keys()) | set(after.keys())):
                if k not in before:
                    _expand_leaves(path + (k,), after[k], "add", out)
                elif k not in after:
                    _expand_leaves(path + (k,), before[k], "remove", out)
                else:
                    _diff_values(path + (k,), before[k], after[k], out)
            return
        b_sigs = {_typed_key_sig(k): (str(k), v) for k, v in before.items()}
        a_sigs = {_typed_key_sig(k): (str(k), v) for k, v in after.items()}
        for sig in sorted(set(b_sigs.keys()) | set(a_sigs.keys()), key=lambda s: (s[0].__qualname__, str(s[1]))):
            if sig not in b_sigs:
                ks, av = a_sigs[sig]
                _expand_leaves(path + (ks,), av, "add", out)
            elif sig not in a_sigs:
                ks, bv = b_sigs[sig]
                _expand_leaves(path + (ks,), bv, "remove", out)
            else:
                ks, bv = b_sigs[sig]
                _, av = a_sigs[sig]
                _diff_values(path + (ks,), bv, av, out)
        return

    if isinstance(before, list) and isinstance(after, list):
        parent_key = path[-1] if path else ""
        if parent_key in _NAMED_LIST_KEYS:
            b_res = _named_list_map(before, parent_key)
            a_res = _named_list_map(after, parent_key)
            if b_res is not None and a_res is not None:
                bmap, b_order = b_res
                amap, a_order = a_res
                for name in sorted(set(bmap.keys()) | set(amap.keys())):
                    if name not in bmap:
                        _expand_leaves(path + (name,), amap[name], "add", out)
                    elif name not in amap:
                        _expand_leaves(path + (name,), bmap[name], "remove", out)
                    else:
                        _diff_values(path + (name,), bmap[name], amap[name], out)
                common_before = [n for n in b_order if n in amap]
                common_after = [n for n in a_order if n in bmap]
                if common_before != common_after:
                    out.append(
                        FieldDiff(
                            tokens=path + ("$order",),
                            op="replace",
                            before=b_order,
                            after=a_order,
                        )
                    )
                return
        max_len = max(len(before), len(after))
        for idx in range(max_len):
            idx_s = str(idx)
            if idx >= len(before):
                _expand_leaves(path + (idx_s,), after[idx], "add", out)
            elif idx >= len(after):
                _expand_leaves(path + (idx_s,), before[idx], "remove", out)
            else:
                _diff_values(path + (idx_s,), before[idx], after[idx], out)
        return

    if isinstance(before, (dict, list)) or isinstance(after, (dict, list)):
        if isinstance(before, (dict, list)):
            _expand_leaves(path, before, "remove", out)
        else:
            out.append(FieldDiff(tokens=path, op="remove", before=before, after=None))
        if isinstance(after, (dict, list)):
            _expand_leaves(path, after, "add", out)
        else:
            out.append(FieldDiff(tokens=path, op="add", before=None, after=after))
        return

    out.append(FieldDiff(tokens=path, op="replace", before=before, after=after))


def diff_objects(before: dict[str, Any], after: dict[str, Any]) -> list[FieldDiff]:
    """Return sorted, deterministic leaf-level structural diffs between two K8s dicts."""
    out: list[FieldDiff] = []
    _diff_values((), before, after, out)
    return out
