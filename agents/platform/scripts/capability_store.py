#!/usr/bin/env python3
"""capability_store.py - Runtime-owned criteria for capabilities on the delivery vehicle.

A capability's procedure (its SKILL.md or governance SOP) is image-owned and
replaced on every pod start. The thresholds, exclusions and scopes that
procedure reads are not: they live here, under
``$HERMES_HOME/capabilities/<name>/``, seeded from the image template and merged
across starts by ``profile_scaffold.py`` so a value the operator tuned survives
a restart and an upgrade. See docs/designs/capability-delivery-vehicle.md.

Three files per capability:

  criteria.json         the tunable values; the only file the agent edits
  criteria.schema.json  image-owned; what keys exist, their types and defaults
  learning.json         per-key policy for edits the agent makes on its own

plus ``changelog.jsonl``, one line per accepted change, which nothing rewrites.

Every write goes through ``apply_changes`` so the policy is enforced in code
rather than in a prompt: a key marked ``never`` cannot be changed here at all,
a key marked ``propose`` needs an operator's confirmation on the call, and a key
marked ``autonomous`` needs only a reason. The validator is a deliberately small
subset of JSON Schema (type, enum, minimum, maximum, items.type,
additionalProperties) so this module stays on the standard library like the
scaffolder that seeds it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CAPABILITIES_DIR_ENV = "KUBE_AGENTS_CAPABILITIES_DIR"
CAPABILITIES_DIRNAME = "capabilities"
CRITERIA_FILENAME = "criteria.json"
SCHEMA_FILENAME = "criteria.schema.json"
LEARNING_FILENAME = "learning.json"
CHANGELOG_FILENAME = "changelog.jsonl"

# Keys the store maintains itself. The validator ignores them and the policy
# never applies to them; the merge treats them as volume state.
REVISION_KEY = "_revision"
UPDATED_AT_KEY = "_updated_at"
STATE_KEYS = frozenset({REVISION_KEY, UPDATED_AT_KEY})

POLICY_AUTONOMOUS = "autonomous"
POLICY_PROPOSE = "propose"
POLICY_NEVER = "never"
POLICIES = (POLICY_AUTONOMOUS, POLICY_PROPOSE, POLICY_NEVER)
DEFAULT_POLICY = POLICY_PROPOSE

# How a changelog entry says the write was authorised: an operator confirmed
# it, or the key's policy let the agent write it alone.
MODE_CONFIRMED = "confirmed"
MODE_AUTONOMOUS = POLICY_AUTONOMOUS
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SCRATCH_SUFFIX = ".tmp"

# Bounds on what one call may carry, so a runaway model cannot fill the volume
# through this path.
MAX_CHANGES_PER_CALL = 32
MAX_REASON_CHARS = 2000
MAX_HISTORY_ENTRIES = 50

# JSON Schema type names this validator understands, mapped to Python.
SCHEMA_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class CapabilityError(ValueError):
    """A request the store refuses; the message is for the caller to relay."""


@dataclass
class Capability:
    name: str
    root: Path
    criteria: dict[str, Any]
    schema: dict[str, Any]
    learning: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.root / self.name

    def policy_for(self, key: str) -> str:
        keys = self.learning.get("keys")
        if isinstance(keys, dict) and isinstance(keys.get(key), str):
            return keys[key]
        default = self.learning.get("default")
        return default if isinstance(default, str) else DEFAULT_POLICY

    def defaults(self) -> dict[str, Any]:
        props = self.schema.get("properties")
        if not isinstance(props, dict):
            return {}
        return {k: v["default"] for k, v in props.items() if isinstance(v, dict) and "default" in v}


def capabilities_root(hermes_home: Path | str | None = None) -> Path:
    """Where this profile's capabilities live.

    The env override exists for tests and for a process whose HERMES_HOME is not
    the profile home; otherwise the store sits beside the profile's cron/ and
    skills/, which is what profile_scaffold.py seeds.
    """
    override = os.environ.get(CAPABILITIES_DIR_ENV)
    if override:
        return Path(override)
    home = Path(hermes_home) if hermes_home else Path(os.environ.get("HERMES_HOME", "/opt/data"))
    return home / CAPABILITIES_DIRNAME


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CapabilityError(f"{path.name} is unreadable or not JSON: {exc}") from exc


def _write_json_atomic(path: Path, payload: Any) -> None:
    scratch = path.with_name(path.name + SCRATCH_SUFFIX)
    scratch.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(scratch, path)


def list_capabilities(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / CRITERIA_FILENAME).is_file())


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith(".") or name != Path(name).name:
        raise CapabilityError(f"invalid capability name {name!r}")
    return name


def load(root: Path, name: str) -> Capability:
    name = _validate_name(name)
    directory = root / name
    criteria = _read_json(directory / CRITERIA_FILENAME)
    if criteria is None:
        known = ", ".join(list_capabilities(root)) or "none"
        raise CapabilityError(f"unknown capability {name!r}; known: {known}")
    if not isinstance(criteria, dict):
        raise CapabilityError(f"{name}/{CRITERIA_FILENAME} must hold a JSON object")
    schema = _read_json(directory / SCHEMA_FILENAME)
    learning = _read_json(directory / LEARNING_FILENAME)
    return Capability(
        name=name,
        root=root,
        criteria=criteria,
        schema=schema if isinstance(schema, dict) else {},
        learning=learning if isinstance(learning, dict) else {},
    )


def validate(schema: dict[str, Any], criteria: dict[str, Any]) -> list[str]:
    """Return every way `criteria` violates `schema`; empty means valid."""
    errors: list[str] = []
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    closed = schema.get("additionalProperties") is False
    for key, value in criteria.items():
        if key in STATE_KEYS:
            continue
        spec = props.get(key)
        if spec is None:
            if closed:
                errors.append(f"{key}: not a key this capability defines")
            continue
        if not isinstance(spec, dict):
            continue
        errors.extend(f"{key}: {e}" for e in _validate_value(spec, value))
    for key in schema.get("required", []) if isinstance(schema.get("required"), list) else []:
        if key not in criteria:
            errors.append(f"{key}: required")
    return errors


def _validate_value(spec: dict[str, Any], value: Any) -> list[str]:
    errors: list[str] = []
    expected = spec.get("type")
    if isinstance(expected, str) and expected in SCHEMA_TYPES:
        ok = isinstance(value, SCHEMA_TYPES[expected])
        # bool is an int in Python; a schema asking for an integer did not ask for a flag.
        if expected in ("integer", "number") and isinstance(value, bool):
            ok = False
        if not ok:
            return [f"expected {expected}, got {type(value).__name__}"]
    enum = spec.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"must be one of {enum}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(spec.get("minimum"), (int, float)) and value < spec["minimum"]:
            errors.append(f"must be >= {spec['minimum']}")
        if isinstance(spec.get("maximum"), (int, float)) and value > spec["maximum"]:
            errors.append(f"must be <= {spec['maximum']}")
    if isinstance(value, list):
        items = spec.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                errors.extend(f"[{i}] {e}" for e in _validate_value(items, item))
        if isinstance(spec.get("maxItems"), int) and len(value) > spec["maxItems"]:
            errors.append(f"must hold at most {spec['maxItems']} items")
    if isinstance(value, str) and isinstance(spec.get("maxLength"), int) and len(value) > spec["maxLength"]:
        errors.append(f"must be at most {spec['maxLength']} characters")
    return errors


def effective_criteria(cap: Capability) -> dict[str, Any]:
    """Schema defaults with the stored values over them, state keys excluded."""
    out = dict(cap.defaults())
    out.update({k: v for k, v in cap.criteria.items() if k not in STATE_KEYS})
    return out


def describe(cap: Capability) -> dict[str, Any]:
    """What `get` returns: values, revision, and per-key policy and description."""
    props = cap.schema.get("properties")
    props = props if isinstance(props, dict) else {}
    values = effective_criteria(cap)
    keys = {}
    for key in sorted(set(values) | set(props)):
        spec = props.get(key) if isinstance(props.get(key), dict) else {}
        keys[key] = {
            "value": values.get(key),
            "default": spec.get("default"),
            "policy": cap.policy_for(key),
            "description": spec.get("description", ""),
        }
    return {
        "capability": cap.name,
        "revision": cap.criteria.get(REVISION_KEY, 0),
        "updated_at": cap.criteria.get(UPDATED_AT_KEY),
        "criteria": values,
        "keys": keys,
        "policy_default": cap.learning.get("default", DEFAULT_POLICY),
    }


def apply_changes(
    root: Path,
    name: str,
    changes: dict[str, Any],
    *,
    reason: str,
    confirmed_by: str = "",
    actor: str = "agent",
) -> dict[str, Any]:
    """Validate, authorise, and write `changes` onto a capability's criteria.

    Returns the changelog entry that was appended. Raises CapabilityError with
    a message the caller can relay verbatim when anything is refused; nothing is
    written in that case.
    """
    cap = load(root, name)
    if not isinstance(changes, dict) or not changes:
        raise CapabilityError("changes must be a non-empty object of key -> new value")
    if len(changes) > MAX_CHANGES_PER_CALL:
        raise CapabilityError(f"at most {MAX_CHANGES_PER_CALL} keys per call")
    reason = (reason or "").strip()
    if not reason:
        raise CapabilityError("reason is required: say what prompted the change")
    if len(reason) > MAX_REASON_CHARS:
        raise CapabilityError(f"reason must be at most {MAX_REASON_CHARS} characters")
    confirmed_by = (confirmed_by or "").strip()

    # Unknown keys first, so a typo is reported as a typo rather than as a
    # policy refusal on a key that does not exist.
    props = cap.schema.get("properties")
    if cap.schema.get("additionalProperties") is False and isinstance(props, dict):
        unknown = [k for k in changes if k not in props and k not in STATE_KEYS]
        if unknown:
            raise CapabilityError(
                f"invalid: {', '.join(unknown)}: not a key this capability defines; "
                f"defined keys: {', '.join(sorted(props))}"
            )

    refused: list[str] = []
    for key in changes:
        if key in STATE_KEYS:
            refused.append(f"{key}: maintained by the store, not settable")
            continue
        policy = cap.policy_for(key)
        if policy == POLICY_NEVER:
            refused.append(f"{key}: policy is never — this key changes through review, not here")
        elif policy == POLICY_PROPOSE and not confirmed_by:
            refused.append(
                f"{key}: policy is propose — show the operator the before/after and call again "
                f"with confirmed_by naming who agreed"
            )
        elif policy not in POLICIES:
            refused.append(f"{key}: learning policy {policy!r} is not one of {list(POLICIES)}")
    if refused:
        raise CapabilityError("refused: " + "; ".join(refused))

    before = effective_criteria(cap)
    candidate = {k: v for k, v in cap.criteria.items() if k not in STATE_KEYS}
    candidate.update(changes)
    errors = validate(cap.schema, candidate)
    if errors:
        raise CapabilityError("invalid: " + "; ".join(errors))

    revision = cap.criteria.get(REVISION_KEY)
    revision = revision + 1 if isinstance(revision, int) and not isinstance(revision, bool) else 1
    now = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    stored = dict(candidate)
    stored[REVISION_KEY] = revision
    stored[UPDATED_AT_KEY] = now

    entry = {
        "ts": now,
        "capability": cap.name,
        "revision": revision,
        "actor": actor,
        "mode": MODE_CONFIRMED if confirmed_by else MODE_AUTONOMOUS,
        "confirmed_by": confirmed_by,
        "reason": reason,
        "changes": {k: {"before": before.get(k), "after": v} for k, v in changes.items()},
    }
    directory = cap.directory
    _write_json_atomic(directory / CRITERIA_FILENAME, stored)
    with (directory / CHANGELOG_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def history(root: Path, name: str, limit: int = MAX_HISTORY_ENTRIES) -> list[dict[str, Any]]:
    cap = load(root, name)
    path = cap.directory / CHANGELOG_FILENAME
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries[-limit:]


def main(argv: list[str]) -> int:
    """Tiny CLI for an operator on the gateway: list, get, history."""
    root = capabilities_root()
    if len(argv) < 2 or argv[1] not in ("list", "get", "history"):
        print("usage: capability_store.py list | get <name> | history <name>", file=sys.stderr)
        return 2
    try:
        if argv[1] == "list":
            print(json.dumps(list_capabilities(root), indent=2))
        elif argv[1] == "get":
            print(json.dumps(describe(load(root, argv[2])), indent=2, sort_keys=True))
        else:
            print(json.dumps(history(root, argv[2]), indent=2, sort_keys=True))
    except (CapabilityError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
