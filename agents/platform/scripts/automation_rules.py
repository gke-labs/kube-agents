"""The customer's automation rules file: which fixes their pipeline may merge on its own.

`.kube-agents/automation.yaml` in the GitOps repository lists the kinds of fix
the customer allows to merge without a human, and where. Nothing is allowed
unless a rule names it, a deny rule beats every allow rule, and a file this
module cannot read in full allows nothing. That is the opposite of
`.kube-agents/intent.yaml`, which falls open to the whole tree: a bound that
fails open only reads more notes, while a rules file that fails open would
merge fixes nobody approved.

The file is read only when `KUBEAGENTS_AUTOMERGE` is not `off`
(`kubeagents_switches`). With the switch off, `read_rules` returns before it
touches the repository, so an install that never turned the switch on behaves
the same whether or not the file exists.

The reference page is `docs/site/src/content/docs/reference/automation-rules.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

RULES_FILE = ".kube-agents/automation.yaml"
SCHEMA_VERSION = 1
# Larger than any hand-written rules file; the cap keeps a hostile file from
# costing more than a glance to refuse.
MAX_FILE_BYTES = 64 * 1024
# The kinds of fix kube-agents opens today, by the audit check that finds them.
# A rule naming anything else is kept but can never match, and says so.
KNOWN_FIX_TYPES = frozenset({"no-pdb"})
WILDCARD = "*"
# The cluster resource labels that carry the environment, in the order the
# fleet-drift SOP resolves them (`fleet_drift.ENV_LABEL_KEYS`). Only a label
# counts here: fleet drift also guesses from cluster names, which is fine for
# grouping clusters and not for deciding what merges unattended.
ENV_LABEL_KEYS = ("environment", "env", "stage", "tier")
UNKNOWN_ENVIRONMENT = ""

# The automerge switch values that read the file (`kubeagents_switches`).
READING_SWITCH_VALUES = frozenset({"practice", "on"})

# Statuses, in the order `read_rules` can stop at them.
SWITCH_OFF = "switch-off"
ABSENT = "absent"
INVALID = "invalid"
LOADED = "loaded"

_FIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A GKE label value: lowercase letters, digits, `_` and `-`, at most 63.
_ENV_RE = re.compile(r"^[a-z0-9_-]{1,63}$")
_TOP_KEYS = frozenset({"version", "automerge"})
_AUTOMERGE_KEYS = frozenset({"allow", "deny"})
_RULE_KEYS = frozenset({"fix", "environments"})


@dataclass(frozen=True)
class Rule:
    fix: str
    environments: tuple[str, ...]

    def matches(self, fix_type: str, environment: str) -> bool:
        return self.fix in (WILDCARD, fix_type) and (
            WILDCARD in self.environments or environment in self.environments
        )


@dataclass(frozen=True)
class Rules:
    """A parsed rules file. Anything but `LOADED` allows nothing."""

    status: str
    allow: tuple[Rule, ...] = ()
    deny: tuple[Rule, ...] = ()
    # Why the file was refused (`INVALID`), for the operator's log.
    error: str = ""
    # Rules that parse but can never match, e.g. an unknown fix type.
    warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class RulesError(ValueError):
    pass


class _UniqueKeyLoader:
    """A SafeLoader that refuses a repeated mapping key.

    PyYAML keeps the last of two equal keys silently, so a reviewer reading
    the first `automerge:` would approve a file whose second one is what runs.
    Built lazily because PyYAML is imported lazily.
    """

    _cls: Any = None

    @classmethod
    def get(cls) -> Any:
        if cls._cls is None:
            import yaml

            class Loader(yaml.SafeLoader):
                pass

            def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
                seen: set = set()
                for key_node, _ in node.value:
                    key = loader.construct_object(key_node, deep=deep)
                    if key in seen:
                        raise RulesError(f"line {key_node.start_mark.line + 1}: key `{key}` appears twice")
                    seen.add(key)
                return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

            Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
            cls._cls = Loader
        return cls._cls


def _require_keys(value: Any, where: str, allowed: frozenset, required: frozenset = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise RulesError(f"{where}: expected a mapping")
    unknown = sorted(str(k) for k in value if k not in allowed)
    if unknown:
        raise RulesError(f"{where}: unknown key `{unknown[0]}`")
    missing = sorted(required - value.keys())
    if missing:
        raise RulesError(f"{where}: missing `{missing[0]}`")
    return value


def _parse_rule(raw: Any, where: str, *, wildcard_ok: bool, warnings: list[str]) -> Rule:
    data = _require_keys(raw, where, _RULE_KEYS, _RULE_KEYS)
    fix = data["fix"]
    if not isinstance(fix, str) or not (_FIX_RE.match(fix) or (wildcard_ok and fix == WILDCARD)):
        hint = "" if wildcard_ok else " (`*` is allowed only in deny rules)"
        raise RulesError(f"{where}.fix: expected a fix type such as `no-pdb`{hint}")
    envs = data["environments"]
    if not isinstance(envs, list) or not envs:
        raise RulesError(f"{where}.environments: expected a non-empty list")
    for index, env in enumerate(envs):
        if not isinstance(env, str) or not (_ENV_RE.match(env) or (wildcard_ok and env == WILDCARD)):
            hint = "" if wildcard_ok else " (`*` is allowed only in deny rules)"
            raise RulesError(
                f"{where}.environments[{index}]: expected an environment label value such as `dev`{hint}"
            )
    if fix != WILDCARD and fix not in KNOWN_FIX_TYPES:
        warnings.append(f"{where}.fix: `{fix}` is not a fix kube-agents opens; the rule never matches")
    return Rule(fix=fix, environments=tuple(envs))


def parse(text: str) -> Rules:
    """Parse the file's text. Any error refuses the whole file, never one rule.

    Dropping only the bad rule would leave a deny rule with a typo silently
    out of force while the allow rules beside it still applied.
    """
    import yaml

    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return Rules(status=INVALID, error=f"larger than {MAX_FILE_BYTES} bytes")
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader.get())
    except RulesError as exc:
        return Rules(status=INVALID, error=str(exc))
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        # `ValueError`: an unquoted date `datetime` refuses; `RecursionError`: a
        # flow collection nested past the limit, as in
        # `audit_report.parse_declarations`. `TypeError`: a mapping key that
        # cannot be hashed (`? [a]`), which the duplicate check meets first.
        return Rules(status=INVALID, error=f"not YAML ({exc})")
    warnings: list[str] = []
    try:
        top = _require_keys(data, "file", _TOP_KEYS, frozenset({"version"}))
        version = top["version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise RulesError(f"version: expected {SCHEMA_VERSION}")
        automerge = top.get("automerge")
        if automerge is None:
            return Rules(status=LOADED)
        section = _require_keys(automerge, "automerge", _AUTOMERGE_KEYS)
        parsed: dict[str, tuple[Rule, ...]] = {}
        for key in ("allow", "deny"):
            items = section.get(key)
            if items is None:
                items = []
            if not isinstance(items, list):
                raise RulesError(f"automerge.{key}: expected a list")
            parsed[key] = tuple(
                _parse_rule(raw, f"automerge.{key}[{i}]", wildcard_ok=key == "deny", warnings=warnings)
                for i, raw in enumerate(items)
            )
    except RulesError as exc:
        return Rules(status=INVALID, error=str(exc))
    return Rules(status=LOADED, allow=parsed["allow"], deny=parsed["deny"], warnings=tuple(warnings))


def _symlinked_component(tree: Path, relative: str) -> str | None:
    probe = tree
    for part in PurePosixPath(relative).parts:
        probe = probe / part
        if probe.is_symlink():
            return probe.relative_to(tree).as_posix()
    return None


def load(tree: Path) -> Rules:
    """Read `RULES_FILE` from a checkout of the GitOps repository."""
    # A committed symlink could point the read outside the checkout the rules
    # were reviewed in; `audit_report.read_intent_paths` refuses it the same way.
    linked = _symlinked_component(tree, RULES_FILE)
    if linked is not None:
        return Rules(status=INVALID, error=f"`{linked}` is a symbolic link, which is never followed")
    path = tree / RULES_FILE
    if not path.is_file():
        return Rules(status=ABSENT)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Rules(status=INVALID, error=f"unreadable ({exc})")
    return parse(text)


def read_rules(tree: Path, *, automerge: str | None = None) -> Rules:
    """The rules in force: `SWITCH_OFF` without reading the file unless the switch reads it.

    `automerge` overrides the switch for tests; callers leave it unset.
    """
    if automerge is None:
        # Lazy: `kubeagents_switches` reads a sandbox file; importing this
        # module for `parse` alone needs neither.
        import kubeagents_switches

        automerge = kubeagents_switches.switch("AUTOMERGE")
    if automerge not in READING_SWITCH_VALUES:
        return Rules(status=SWITCH_OFF)
    return load(tree)


def cluster_environment(labels: Mapping[str, Any] | None) -> str:
    """The environment a cluster's resource labels declare, or `UNKNOWN_ENVIRONMENT`.

    The first of `ENV_LABEL_KEYS` that is set wins, compared as written: a GKE
    label value is already lowercase, and folding case here would let `Dev`
    match a rule for `dev` that a reviewer never saw.
    """
    for key in ENV_LABEL_KEYS:
        value = (labels or {}).get(key)
        if isinstance(value, str) and value:
            return value
    return UNKNOWN_ENVIRONMENT


def decide(rules: Rules, fix_type: str, environment: str) -> Decision:
    """Whether the rules let a fix of `fix_type` in `environment` merge unattended."""
    if rules.status == SWITCH_OFF:
        return Decision(False, "KUBEAGENTS_AUTOMERGE is off; the rules file was not read")
    if rules.status == ABSENT:
        return Decision(False, f"no {RULES_FILE}; nothing merges unattended")
    if rules.status != LOADED:
        return Decision(False, f"{RULES_FILE} refused ({rules.error}); nothing merges unattended")
    if not environment:
        return Decision(False, "the cluster declares no environment label")
    for index, rule in enumerate(rules.deny):
        if rule.matches(fix_type, environment):
            return Decision(False, f"denied by automerge.deny[{index}]")
    for index, rule in enumerate(rules.allow):
        if rule.matches(fix_type, environment):
            return Decision(True, f"allowed by automerge.allow[{index}]")
    return Decision(False, f"no allow rule names {fix_type} in {environment}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Check a GitOps checkout's {RULES_FILE}.")
    parser.add_argument("tree", type=Path, help="the checkout's root")
    parser.add_argument("--fix", default="", help="also decide for this fix type")
    parser.add_argument("--environment", default="", help="and this environment")
    args = parser.parse_args(argv)
    rules = read_rules(args.tree)
    out: dict[str, Any] = {
        "status": rules.status,
        "allow": [vars(r) | {"environments": list(r.environments)} for r in rules.allow],
        "deny": [vars(r) | {"environments": list(r.environments)} for r in rules.deny],
    }
    if rules.error:
        out["error"] = rules.error
    if rules.warnings:
        out["warnings"] = list(rules.warnings)
    if args.fix:
        decision = decide(rules, args.fix, args.environment)
        out["decision"] = {"allowed": decision.allowed, "reason": decision.reason}
    print(json.dumps(out, indent=2))
    return 1 if rules.status == INVALID else 0


if __name__ == "__main__":
    sys.exit(main())
