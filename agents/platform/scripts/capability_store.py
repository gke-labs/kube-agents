#!/usr/bin/env python3
"""capability_store.py - Runtime-owned criteria for capabilities on the delivery vehicle.

A capability's procedure (its SKILL.md or governance SOP) is image-owned and
replaced on every pod start. The thresholds, exclusions and scopes that
procedure reads are not: they live here, under
``$HERMES_HOME/capabilities/<name>/``, seeded from the image template and merged
across starts by ``profile_scaffold.py`` so a value the operator tuned survives
a restart and an upgrade.

Each capability directory holds:

  criteria.json         the tunable values; the only file the agent edits
  criteria.schema.json  image-owned; what keys exist, their types and defaults
  learning.json         image-owned; per-key policy for what the agent may
                        change with and without an operator's confirmation
  changelog.jsonl       one line per accepted change; nothing rewrites it

Every write goes through ``apply_changes`` so the policy is enforced in code
rather than in a prompt. What the code can enforce: a key marked ``never``
cannot be changed here at all, and a key marked ``propose`` is refused unless
the call names who confirmed it. What it cannot: that the named person actually
saw the before/after — that round-trip is the agent's instruction
(agents/platform/AGENTS.md), and the changelog records the name so a reader can
ask. The validator is a deliberately small subset of JSON Schema (type, enum,
minimum, maximum, maxLength, items, maxItems, required, additionalProperties) so
this module stays on the standard library like the scaffolder that seeds it.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_scaffold import write_json_atomic  # noqa: E402

CAPABILITIES_DIR_ENV = "KUBE_AGENTS_CAPABILITIES_DIR"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "/opt/data"
# Where the Platform Agent's store sits relative to the machine home. The tool
# runs with HERMES_HOME already at the profile home; the operator CLI usually
# does not, so it looks here when the bare home holds no capabilities.
PLATFORM_PROFILE_SUBDIR = "profiles/platform"
CAPABILITIES_DIRNAME = "capabilities"
CRITERIA_FILENAME = "criteria.json"
SCHEMA_FILENAME = "criteria.schema.json"
LEARNING_FILENAME = "learning.json"
CHANGELOG_FILENAME = "changelog.jsonl"
# Held for the whole read -> validate -> write of one change. A cron child and
# a kanban worker each run their own platform_control process against the same
# volume, so without it two simultaneous sets would lose one update.
LOCK_FILENAME = ".lock"

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

# Bounds on what one call may carry, so a runaway model cannot fill the volume
# through this path.
MAX_CHANGES_PER_CALL = 32
MAX_REASON_CHARS = 2000
# A name or handle, not a transcript pasted as evidence of who agreed.
MAX_CONFIRMED_BY_CHARS = 200
MAX_HISTORY_ENTRIES = 50

# What the operator CLI at the bottom of this file accepts. Read-only on
# purpose: writes go through the MCP tool so every one carries a policy check
# and a changelog line.
CLI_ACTIONS = ("list", "get", "history")

# The `actor` a changelog entry records when the caller names none.
DEFAULT_ACTOR = "agent"

# JSON Schema type names this validator understands, mapped to Python.
NUMERIC_TYPES = ("integer", "number")
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

    def properties(self) -> dict[str, Any]:
        props = self.schema.get("properties")
        return props if isinstance(props, dict) else {}

    def defaults(self) -> dict[str, Any]:
        return {
            k: v["default"]
            for k, v in self.properties().items()
            if isinstance(v, dict) and "default" in v
        }

    def is_closed(self) -> bool:
        return self.schema.get("additionalProperties") is False


def capabilities_root(hermes_home: Path | str | None = None) -> Path:
    """Where this profile's capabilities live.

    The env override exists for tests and for a process whose HERMES_HOME is not
    the profile home; otherwise the store sits beside the profile's cron/ and
    skills/, which is what profile_scaffold.py seeds.
    """
    override = os.environ.get(CAPABILITIES_DIR_ENV)
    if override:
        return Path(override)
    home = Path(hermes_home) if hermes_home else Path(os.environ.get(HERMES_HOME_ENV, DEFAULT_HERMES_HOME))
    return home / CAPABILITIES_DIRNAME


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CapabilityError(f"{path.name} is unreadable or not JSON: {exc}") from exc


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    with (directory / LOCK_FILENAME).open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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
        if expected in NUMERIC_TYPES and isinstance(value, bool):
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


def stale_keys(cap: Capability) -> list[str]:
    """Stored keys the image-owned schema no longer defines.

    The volume-wins merge keeps a key across an upgrade that dropped it, so the
    schema is what says it is gone. `set` prunes them rather than refusing every
    later write over a key nobody can name any more.
    """
    if not cap.is_closed():
        return []
    props = cap.properties()
    return sorted(k for k in cap.criteria if k not in props and k not in STATE_KEYS)


def invalid_keys(cap: Capability) -> dict[str, str]:
    """Stored values the image-owned schema now rejects, by key.

    A release may tighten a bound after the volume stored a value inside the old
    one. The merge keeps the value; this is what notices. Such a value is not
    effective — the schema default stands in for it — and the next `set` resets
    it and says so, rather than refusing every later write on its account.
    """
    props = cap.properties()
    out: dict[str, str] = {}
    for key, value in cap.criteria.items():
        spec = props.get(key)
        if key in STATE_KEYS or not isinstance(spec, dict):
            continue
        errors = _validate_value(spec, value)
        if errors:
            out[key] = "; ".join(errors)
    return out


def effective_criteria(cap: Capability) -> dict[str, Any]:
    """Schema defaults with the stored values over them; state, stale and invalid keys excluded."""
    out = dict(cap.defaults())
    excluded = set(stale_keys(cap)) | set(invalid_keys(cap))
    out.update({k: v for k, v in cap.criteria.items() if k not in STATE_KEYS and k not in excluded})
    return out


def describe(cap: Capability) -> dict[str, Any]:
    """What `get` returns: values, revision, and per-key policy and description."""
    props = cap.properties()
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
        "stale_keys": stale_keys(cap),
        "invalid_keys": invalid_keys(cap),
    }


def apply_changes(
    root: Path,
    name: str,
    changes: dict[str, Any],
    *,
    reason: str,
    confirmed_by: str = "",
    actor: str = DEFAULT_ACTOR,
) -> dict[str, Any]:
    """Validate, authorise, and write `changes` onto a capability's criteria.

    Returns the changelog entry that was appended. Raises CapabilityError with
    a message the caller can relay verbatim when anything is refused; nothing is
    written in that case. `changes` may arrive JSON-encoded; a model that
    serialises the object as a string should not lose a turn over it.
    """
    name = _validate_name(name)
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except ValueError:
            raise CapabilityError("changes is a string that is not JSON; pass an object of key -> new value")
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
    if len(confirmed_by) > MAX_CONFIRMED_BY_CHARS:
        raise CapabilityError(f"confirmed_by must be at most {MAX_CONFIRMED_BY_CHARS} characters: a name, not a transcript")

    directory = root / name
    if not directory.is_dir():
        # Same message load() gives, before we try to create a lock file in a
        # directory that does not exist.
        load(root, name)
    with _locked(directory):
        cap = load(root, name)
        return _apply_locked(cap, changes, reason, confirmed_by, actor)


def _apply_locked(
    cap: Capability, changes: dict[str, Any], reason: str, confirmed_by: str, actor: str
) -> dict[str, Any]:
    props = cap.properties()
    # Unknown keys first, so a typo is reported as a typo rather than as a
    # policy refusal on a key that does not exist.
    if cap.is_closed():
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
    pruned = stale_keys(cap)
    # Every invalid stored value is recorded, including one this call replaces:
    # `before` carries the default that stood in for it, so the changelog is the
    # only place the raw value survives.
    reset = invalid_keys(cap)
    candidate = {
        k: v for k, v in cap.criteria.items()
        if k not in STATE_KEYS and k not in pruned and k not in reset
    }
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
        "pruned": {k: cap.criteria[k] for k in pruned},
        "reset": {k: {"value": cap.criteria[k], "error": e} for k, e in reset.items()},
    }
    write_json_atomic(cap.directory / CRITERIA_FILENAME, stored, sort_keys=True)
    with (cap.directory / CHANGELOG_FILENAME).open("a", encoding="utf-8") as fh:
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


def cli_root() -> Path:
    """The store the CLI reads: the env override, else HERMES_HOME's, else the platform profile's.

    On the gateway HERMES_HOME is the machine home, whose own `capabilities/`
    does not exist; the Platform Agent's store is one profile down.
    """
    root = capabilities_root()
    if os.environ.get(CAPABILITIES_DIR_ENV) or root.is_dir():
        return root
    fallback = root.parent / PLATFORM_PROFILE_SUBDIR / CAPABILITIES_DIRNAME
    return fallback if fallback.is_dir() else root


def main(argv: list[str]) -> int:
    """Tiny read-only CLI for an operator on the gateway."""
    root = cli_root()
    if len(argv) < 2 or argv[1] not in CLI_ACTIONS:
        print(f"usage: capability_store.py {' | '.join(CLI_ACTIONS)} [<name>]", file=sys.stderr)
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
