"""Hardened YAML 1.2 + JSON manifest parser and typed structural differ."""

from __future__ import annotations

import copy
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

# Unicode BiDi / Trojan-Source overrides and zero-width characters
_HOSTILE_UNICODE = frozenset(
    {
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\u200e",  # LEFT-TO-RIGHT MARK
        "\u200f",  # RIGHT-TO-LEFT MARK
        "\u061c",  # ARABIC LETTER MARK
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

# Helm / Go-template syntax markers
_HELM_TEMPLATE_RE = re.compile(
    r"\{\{-?\s*(?:\.[A-Za-z_]|include\b|if\b|else\b|end\b|range\b|with\b|define\b|template\b|required\b|\$)"
)
# Flux / envsubst substitution syntax (e.g. `${CLUSTER_NAME}`, `${VAR:=default}`)
_ENVSUBST_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:[:?+-][^}]*)?\}")

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

_REDACTED_MARKER = "<redacted-secret-value>"


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

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        return (self.ref.group, self.ref.kind, self.ref.namespace, self.ref.name)


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


def _check_text_safety(raw: bytes) -> str:
    """Decode UTF-8 and reject NUL, C0 control chars, and BiDi/zero-width chars."""
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
    for idx, ch in enumerate(text):
        codepoint = ord(ch)
        if codepoint < 0x20 and ch not in ("\t", "\n", "\r"):
            raise ManifestParseError(
                f"manifest contains C0 control character U+{codepoint:04X} at offset {idx}",
                code="CONTROL_CHARACTER",
            )
        if codepoint == 0x7F or ch == "\ufeff" or ch in _HOSTILE_UNICODE:
            raise ManifestParseError(
                f"manifest contains disallowed Unicode control/BiDi character U+{codepoint:04X} at offset {idx}",
                code="HOSTILE_UNICODE",
            )
    return text


def has_template_syntax(text: str) -> bool:
    """Return True if text contains unrendered Helm/Go templates or envsubst markers."""
    if _HELM_TEMPLATE_RE.search(text):
        return True
    # Flux/envsubst `${VAR}` markers outside commented lines
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if _ENVSUBST_RE.search(stripped):
            return True
    return False


_BaseYAMLLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _StrictYAMLLoader(_BaseYAMLLoader):
    """YAML 1.2-strict SafeLoader rejecting duplicate keys, merge keys, and YAML 1.1 traps."""


# Remove YAML 1.1 boolean / sexagesimal / legacy-octal / timestamp resolvers so parsing
# matches YAML 1.2 core schema (only true/false are booleans; Off/NO/dates stay strings).
_StrictYAMLLoader.yaml_implicit_resolvers = {
    k: [
        (tag, regexp)
        for tag, regexp in v
        if tag
        not in (
            "tag:yaml.org,2002:bool",
            "tag:yaml.org,2002:int",
            "tag:yaml.org,2002:float",
            "tag:yaml.org,2002:timestamp",
        )
    ]
    for k, v in _BaseYAMLLoader.yaml_implicit_resolvers.items()
}
_StrictYAMLLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
_StrictYAMLLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)
_StrictYAMLLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+0123456789."),
)

# Explicit YAML tags that construct non-JSON Python objects (bytes, set, datetime, etc.)
_DISALLOWED_YAML_TAGS = frozenset(
    {
        "tag:yaml.org,2002:binary",
        "tag:yaml.org,2002:set",
        "tag:yaml.org,2002:omap",
        "tag:yaml.org,2002:pairs",
        "tag:yaml.org,2002:timestamp",
    }
)


def _typed_key_sig(key: Any) -> tuple[type, Any]:
    if isinstance(key, float) and math.isnan(key):
        return (float, "nan")
    return (type(key), key)


def _construct_strict_mapping(loader: _StrictYAMLLoader, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
    if not isinstance(node, yaml.MappingNode):
        raise ManifestParseError("expected YAML mapping node")
    seen: set[tuple[type, Any]] = set()
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ManifestParseError(
                f"line {key_node.start_mark.line + 1}: YAML merge keys (`<<`) are not supported",
                code="YAML_MERGE_KEY",
            )
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)) and key is not None:
            raise ManifestParseError(
                f"line {key_node.start_mark.line + 1}: non-scalar YAML mapping key",
                code="YAML_COMPLEX_KEY",
            )
        if key == "<<":
            raise ManifestParseError(
                f"line {key_node.start_mark.line + 1}: YAML merge key (`<<`) is not supported",
                code="YAML_MERGE_KEY",
            )
        sig = _typed_key_sig(key)
        if sig in seen or key in result:
            raise ManifestParseError(
                f"line {key_node.start_mark.line + 1}: duplicate YAML key {key!r}",
                code="YAML_DUPLICATE_KEY",
            )
        seen.add(sig)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictYAMLLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
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


def _check_yaml_tag(ev: Any) -> None:
    tag = getattr(ev, "tag", None)
    if tag and tag not in _ALLOWED_YAML_EXPLICIT_TAGS:
        raise ManifestParseError(
            f"line {ev.start_mark.line + 1}: unsupported YAML tag {tag!r}",
            code="YAML_UNSUPPORTED_TAG",
        )


def _scan_yaml_events(text: str) -> None:
    """Validate YAML event stream for anchors/aliases, disallowed tags, and depth/node caps."""
    depth = 0
    nodes = 0
    docs = 0
    try:
        for ev in yaml.parse(text, Loader=_StrictYAMLLoader):
            if isinstance(ev, yaml.DocumentStartEvent):
                docs += 1
                if docs > MAX_DOCS_PER_FILE:
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
                if nodes > MAX_NODES_PER_FILE:
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
                if nodes > MAX_NODES_PER_FILE:
                    raise ManifestParseError(
                        f"manifest exceeds maximum node count ({MAX_NODES_PER_FILE})",
                        code="YAML_TOO_MANY_NODES",
                    )
    except ManifestParseError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ManifestParseError(f"YAML syntax error: {exc}", code="YAML_SYNTAX_ERROR") from exc


def _split_api_version(api_version: str) -> tuple[str, str]:
    """Split and validate Kubernetes `apiVersion` into `(group, version)`."""
    if not api_version or any(ch.isspace() for ch in api_version):
        raise ManifestParseError(f"invalid Kubernetes apiVersion {api_version!r}", code="INVALID_APIVERSION")
    if "/" in api_version:
        parts = api_version.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ManifestParseError(f"malformed Kubernetes apiVersion {api_version!r}", code="INVALID_APIVERSION")
        return parts[0], parts[1]
    return "", api_version


def _sanitize_for_diff(obj: dict[str, Any], group: str, kind: str) -> dict[str, Any]:
    """Return a copy with Secret data/stringData values redacted before diffing."""
    if group == "" and kind == "Secret":
        clean = copy.deepcopy(obj)
        for field in ("data", "stringData"):
            val = clean.get(field)
            if isinstance(val, dict):
                clean[field] = {k: _REDACTED_MARKER for k in val}
            elif val is not None:
                clean[field] = _REDACTED_MARKER
        return clean
    return obj


def _extract_resources(doc: Any, out: list[ParsedResource], *, depth: int = 0) -> tuple[bool, bool]:
    """Extract Kubernetes resources from a decoded doc.

    Returns `(is_k8s_object, is_kustomize_object)`.
    """
    if doc is None:
        return True, False
    if not isinstance(doc, dict):
        return False, False

    raw_api = doc.get("apiVersion")
    raw_kind = doc.get("kind")
    if not isinstance(raw_api, str) or not isinstance(raw_kind, str):
        return False, False

    api_version = raw_api.strip()
    kind = raw_kind.strip()
    if not api_version or not kind:
        return False, False

    group, version = _split_api_version(api_version)
    if group.startswith("kustomize.config.k8s.io") or kind in ("Kustomization", "Component"):
        return True, True

    # Unpack Kubernetes List / *List envelopes
    if (kind == "List" or kind.endswith("List")) and isinstance(doc.get("items"), list):
        if depth > 4:
            raise ManifestParseError("nested Kubernetes List exceeds depth limit", code="LIST_TOO_DEEP")
        for item in doc["items"]:
            if len(out) >= MAX_DOCS_PER_FILE:
                raise ManifestParseError(
                    f"manifest exceeds maximum of {MAX_DOCS_PER_FILE} resources",
                    code="TOO_MANY_DOCUMENTS",
                )
            ok, is_kust = _extract_resources(item, out, depth=depth + 1)
            if is_kust:
                return True, True
            if not ok:
                raise ManifestParseError("Kubernetes List contains non-resource item", code="INVALID_LIST_ITEM")
        return True, False

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
    if not name and not gen_name:
        # Missing name without non-empty generateName -> likely a Kustomize/Helm fragment
        return False, False

    # Normalize top-level apiVersion/kind whitespace so downstream checks and Secret
    # redaction see canonical values even when the input quoted trailing spaces.
    normalized = dict(doc)
    normalized["apiVersion"] = api_version
    normalized["kind"] = kind

    ref = ResourceRef(
        group=group,
        version=version,
        kind=kind,
        namespace=namespace,
        name=name or gen_name,
    )
    sanitized = _sanitize_for_diff(normalized, group, kind)
    out.append(ParsedResource(ref=ref, obj=normalized, sanitized=sanitized))
    return True, False


def _reject_json_constant(token: str) -> Any:
    raise ManifestParseError(f"non-standard JSON constant {token!r} is not allowed", code="JSON_INVALID_CONSTANT")


def _check_json_limits(value: Any, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_NODES_PER_FILE:
        raise ManifestParseError(
            f"JSON exceeds maximum node count ({MAX_NODES_PER_FILE})",
            code="JSON_TOO_MANY_NODES",
        )
    if depth > MAX_NESTING_DEPTH:
        raise ManifestParseError(
            f"JSON exceeds maximum nesting depth ({MAX_NESTING_DEPTH})",
            code="JSON_NESTING_TOO_DEEP",
        )
    if isinstance(value, dict):
        for v in value.values():
            _check_json_limits(v, depth + 1, counter)
    elif isinstance(value, list):
        for item in value:
            _check_json_limits(item, depth + 1, counter)


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
    - Kustomize resources (`is_kustomize=True`)
    - unrendered templates (`is_template=True`, still parsing any valid embedded resources
      so template markers in a benign field can never mask a critical/high K8s change)
    - valid non-Kubernetes YAML/JSON (`is_k8s_manifest=False`, `error=""`)
    - syntax/security/size errors (`error` and `error_code` set)
    """
    if raw is None or not raw.strip():
        return ParseOutcome(resources=(), is_k8s_manifest=True)
    try:
        text = _check_text_safety(raw)
    except ManifestParseError as exc:
        return ParseOutcome(is_k8s_manifest=False, error=str(exc), error_code=exc.code)

    templated = has_template_syntax(text)

    docs: list[Any]
    if is_json:
        try:
            loaded = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
            _check_json_limits(loaded)
        except ManifestParseError as exc:
            return ParseOutcome(is_k8s_manifest=False, is_template=templated, error=str(exc), error_code=exc.code)
        except (ValueError, RecursionError) as exc:
            if templated:
                return ParseOutcome(is_k8s_manifest=False, is_template=True)
            return ParseOutcome(
                is_k8s_manifest=False,
                error=f"JSON syntax error: {exc}",
                error_code="JSON_SYNTAX_ERROR",
            )
        if isinstance(loaded, list):
            if len(loaded) > MAX_DOCS_PER_FILE:
                return ParseOutcome(
                    is_k8s_manifest=False,
                    is_template=templated,
                    error=f"JSON array exceeds maximum of {MAX_DOCS_PER_FILE} documents",
                    error_code="TOO_MANY_DOCUMENTS",
                )
            docs = loaded
        else:
            docs = [loaded]
    else:
        try:
            _scan_yaml_events(text)
            docs = list(yaml.load_all(text, Loader=_StrictYAMLLoader))
        except ManifestParseError as exc:
            if templated and exc.code == "YAML_SYNTAX_ERROR":
                return ParseOutcome(is_k8s_manifest=False, is_template=True)
            return ParseOutcome(is_k8s_manifest=False, is_template=templated, error=str(exc), error_code=exc.code)
        except (yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
            if templated:
                return ParseOutcome(is_k8s_manifest=False, is_template=True)
            return ParseOutcome(
                is_k8s_manifest=False,
                error=f"YAML syntax error: {exc}",
                error_code="YAML_SYNTAX_ERROR",
            )

    resources: list[ParsedResource] = []
    non_empty_docs = [d for d in docs if d is not None]
    if not non_empty_docs:
        return ParseOutcome(resources=(), is_k8s_manifest=not templated, is_template=templated)

    saw_k8s = False
    saw_non_k8s = False
    for doc in non_empty_docs:
        try:
            is_k8s, is_kust = _extract_resources(doc, resources)
        except ManifestParseError as exc:
            return ParseOutcome(
                resources=tuple(resources),
                is_k8s_manifest=False,
                is_template=templated,
                error=str(exc),
                error_code=exc.code,
            )
        if is_kust:
            return ParseOutcome(
                resources=tuple(resources),
                is_k8s_manifest=False,
                is_template=templated,
                is_kustomize=True,
            )
        if is_k8s:
            saw_k8s = True
        else:
            saw_non_k8s = True

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
    """Typed equality where `True != 1`, `False != 0`, and `NaN == NaN`."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, dict):
        if len(a) != len(b) or a.keys() != b.keys():
            return False
        return all(_strict_equal(a[k], b[k]) for k in a)
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
        for k in sorted(val.keys(), key=str):
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


def _named_list_map(items: list[Any]) -> dict[str, dict[str, Any]] | None:
    """If every element is a dict with a unique non-empty string `name`, return name -> dict."""
    mapping: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in mapping:
            return None
        mapping[name] = item
    return mapping


def _diff_values(path: tuple[str, ...], before: Any, after: Any, out: list[FieldDiff]) -> None:
    if _strict_equal(before, after):
        return
    # Expand composite <-> None so emptying a YAML mapping/list to `null`
    # emits leaf removals/additions rather than hiding child keys behind a parent op.
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
        all_keys = sorted(set(before.keys()) | set(after.keys()), key=str)
        for k in all_keys:
            ks = str(k)
            if k not in before:
                _expand_leaves(path + (ks,), after[k], "add", out)
            elif k not in after:
                _expand_leaves(path + (ks,), before[k], "remove", out)
            else:
                _diff_values(path + (ks,), before[k], after[k], out)
        return

    if isinstance(before, list) and isinstance(after, list):
        parent_key = path[-1] if path else ""
        if parent_key in _NAMED_LIST_KEYS:
            bmap = _named_list_map(before)
            amap = _named_list_map(after)
            if bmap is not None and amap is not None:
                for name in sorted(set(bmap.keys()) | set(amap.keys())):
                    if name not in bmap:
                        _expand_leaves(path + (name,), amap[name], "add", out)
                    elif name not in amap:
                        _expand_leaves(path + (name,), bmap[name], "remove", out)
                    else:
                        _diff_values(path + (name,), bmap[name], amap[name], out)
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

    out.append(FieldDiff(tokens=path, op="replace", before=before, after=after))


def diff_objects(before: dict[str, Any], after: dict[str, Any]) -> list[FieldDiff]:
    """Return sorted, deterministic leaf-level structural diffs between two K8s dicts."""
    out: list[FieldDiff] = []
    _diff_values((), before, after, out)
    return out
