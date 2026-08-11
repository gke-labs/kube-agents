"""A JSON Schema validator covering exactly the keywords `runner/contract/` uses.

Why not `jsonschema`: the conformance suite has to run wherever `make test-python`
runs, and every third-party import in that suite is one more way for a
contributor's first test run to fail for a reason that has nothing to do with
their change. The two schemas here are small and deliberately written against a
small keyword set, so a validator for that set is cheaper than a dependency.

The one property that makes this safe: an unrecognised keyword **raises**. A
validator that silently ignores what it does not understand reports success on a
schema it never checked, which is worse than having no validator -- the schema
would look enforced while enforcing nothing. If a schema starts using `$ref`,
`allOf`, or `patternProperties`, this file must grow first and the failure says
so by name.
"""

from __future__ import annotations

from typing import Any

# Keywords that carry no constraint. Listed rather than pattern-matched on a
# leading "$" so that a genuine "$ref" cannot slip through as an annotation.
_ANNOTATIONS = frozenset(
    {"$schema", "$id", "$comment", "title", "description", "examples", "default"}
)

_CONSTRAINTS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "oneOf",
        "minimum",
        "minLength",
        "minItems",
    }
)

_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class UnsupportedSchemaError(Exception):
    """A schema used a keyword this validator does not implement."""


def _type_matches(value: Any, expected: str) -> bool:
    # bool is a subclass of int in Python and is not a JSON number, so the
    # numeric branches exclude it explicitly. Without that, `true` validates
    # against {"type": "integer"}.
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    known = _TYPES.get(expected)
    if known is None:
        raise UnsupportedSchemaError(f"unknown type name {expected!r}")
    return isinstance(value, known)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return every way ``instance`` violates ``schema``; empty means valid.

    Errors accumulate rather than short-circuit, because a conformance failure
    is more useful as "these four fields are wrong" than as the first one.
    """
    unknown = set(schema) - _ANNOTATIONS - _CONSTRAINTS
    if unknown:
        raise UnsupportedSchemaError(
            f"{path}: schema uses unimplemented keyword(s) {sorted(unknown)}. "
            "Add support to runner/jsonschema_mini.py before using them."
        )

    errors: list[str] = []

    if "oneOf" in schema:
        branches = schema["oneOf"]
        matched = [b for b in branches if not validate(instance, b, path)]
        if len(matched) != 1:
            titles = [b.get("title", "<untitled>") for b in branches]
            errors.append(
                f"{path}: matched {len(matched)} of {len(branches)} oneOf branches "
                f"(expected exactly 1); branches are {titles}"
            )
        # A oneOf schema in these files carries nothing beside oneOf, and
        # reporting a failed branch's inner errors here would be noise: the
        # instance is not meant to satisfy the other six.
        return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    if "type" in schema:
        expected = schema["type"]
        names = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(instance, name) for name in names):
            got = type(instance).__name__
            errors.append(f"{path}: expected type {expected!r}, got {got}")
            # Every remaining keyword assumes the type held, so stop here rather
            # than emit a cascade of consequences of the one real error.
            return errors

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                errors.extend(validate(value, properties[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name!r}")

    if isinstance(instance, list):
        if "items" in schema:
            for index, value in enumerate(instance):
                errors.extend(validate(value, schema["items"], f"{path}[{index}]"))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")

    if isinstance(instance, str) and "minLength" in schema:
        if len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")

    if "minimum" in schema and isinstance(instance, (int, float)):
        if not isinstance(instance, bool) and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} is below minimum {schema['minimum']}")

    return errors
