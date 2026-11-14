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
# A rule naming anything else makes the file invalid.
KNOWN_FIX_TYPES = frozenset({"no-pdb"})
# What a person calls each of them, for text kube-agents writes; one per known type.
FIX_TYPE_NAMES = {"no-pdb": "disruption-budget fixes"}
WILDCARD = "*"
# The label on a fix pull request the customer's pipeline may merge on its own
# (`KUBEAGENTS_AUTOMERGE=on`). A merged pull request carrying it is one that
# merged without a human review, which is what the step-down acts on.
AUTOMERGE_LABEL = "kube-agents:auto-merge"
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

# `fullmatch`, never `match`: `$` also matches before a trailing newline, so a
# block scalar reading `prod\n` would pass and then match nothing.
_FIX_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A GKE label value: lowercase letters, digits, `_` and `-`, at most 63.
_ENV_RE = re.compile(r"[a-z0-9_-]{1,63}")
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
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        raise RulesError(f"line {key_node.start_mark.line + 1}: merge keys (`<<`) are not supported")
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
    if not isinstance(fix, str) or not (_FIX_RE.fullmatch(fix) or (wildcard_ok and fix == WILDCARD)):
        hint = "" if wildcard_ok else " (`*` is allowed only in deny rules)"
        raise RulesError(f"{where}.fix: expected a fix type such as `no-pdb`{hint}")
    envs = data["environments"]
    if not isinstance(envs, list) or not envs:
        raise RulesError(f"{where}.environments: expected a non-empty list")
    for index, env in enumerate(envs):
        if not isinstance(env, str) or not (_ENV_RE.fullmatch(env) or (wildcard_ok and env == WILDCARD)):
            hint = "" if wildcard_ok else " (`*` is allowed only in deny rules)"
            raise RulesError(
                f"{where}.environments[{index}]: expected an environment label value such as `dev`{hint}"
            )
    # Refused, not warned: a misspelt deny rule kept but never matching would
    # leave the allow rules beside it in force. A file written for a newer
    # kube-agents is refused by an older one, and a refused file allows nothing.
    if fix != WILDCARD and fix not in KNOWN_FIX_TYPES:
        known = ", ".join(f"`{name}`" for name in sorted(KNOWN_FIX_TYPES))
        raise RulesError(f"{where}.fix: `{fix}` is not a fix kube-agents opens (known: {known})")
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
    except ValueError as exc:
        # A key too large to print (a YAML 1.1 base-60 integer past Python's
        # 4300-digit str limit) fails while the error message is built.
        return Rules(status=INVALID, error=f"not a rules file ({exc})")
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
        if len(raw) > MAX_FILE_BYTES:
            return Rules(status=INVALID, error=f"larger than {MAX_FILE_BYTES} bytes")
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
        # The parser refuses a rule naming a fix type kube-agents does not know;
        # this guard keeps a hand-built Rules from matching one either.
        if rule.fix in KNOWN_FIX_TYPES and rule.matches(fix_type, environment):
            return Decision(True, f"allowed by automerge.allow[{index}]")
    return Decision(False, f"no allow rule names {fix_type} in {environment}")


def with_deny(text: str, fix_type: str, environment: str, note: str = "") -> str:
    """The file's text with one more deny rule, for `fix_type` in `environment`.

    An edit of the text, never a re-render: the file is the customer's, and a
    pull request that rewrote every comment and quote in it to add one rule
    would hide the rule. The rule goes at the end of `automerge.deny`, which
    is created (and `automerge` with it) when absent. `note` becomes a comment above it in a block-style
    file. Raises `RulesError` for a file this cannot read or a layout it cannot
    add to, and checks its own output: the result must parse, with exactly
    this rule added.
    """
    return _with_rule(text, "deny", fix_type, environment, note)


def with_allow(text: str, fix_type: str, environment: str, note: str = "") -> str:
    """`with_deny`, for an allow rule; `automerge` is created too when absent."""
    return _with_rule(text, "allow", fix_type, environment, note)


def new_file(fix_type: str, environment: str) -> str:
    """A rules file that allows exactly `fix_type` in `environment`."""
    text = (
        "# Which kube-agents fixes your pipeline may merge without a human review\n"
        "# https://gke-labs.github.io/kube-agents/reference/automation-rules/\n"
        "# Nothing else merges unattended.\n"
        "version: 1\n"
    )
    return with_allow(text, fix_type, environment)


def _with_rule(text: str, section: str, fix_type: str, environment: str, note: str) -> str:
    import yaml

    before = parse(text)
    if before.status != LOADED:
        raise RulesError(f"{RULES_FILE} does not parse; not edited")
    fix_q, env_q = json.dumps(fix_type), json.dumps(environment)
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    automerge_key = next((k for k, v in root.value if k.value == "automerge"), None)
    automerge = next((v for k, v in root.value if k.value == "automerge"), None)
    entries = automerge.value if isinstance(automerge, yaml.MappingNode) else []
    rules = next((v for k, v in entries if k.value == section), None)
    rules_key = next((k for k, v in entries if k.value == section), None)
    flow_item = f"{{fix: {fix_q}, environments: [{env_q}]}}"
    nl = "\r\n" if "\r\n" in text else "\n"
    comment = f"# {' '.join(note.split())}" if note else ""

    def block_item(pad: str) -> str:
        head = f"{pad}{comment}{nl}" if comment else ""
        return f"{head}{pad}- fix: {fix_q}{nl}{pad}  environments: [{env_q}]{nl}"

    if automerge_key is None:
        if root.flow_style:
            raise RulesError(f"cannot add `automerge` to a flow-style {RULES_FILE}")
        out = _insert_after_line(text, _exact_end(root), f"automerge:{nl}  {section}:{nl}" + block_item("    "))
    elif not isinstance(automerge, yaml.MappingNode):
        # `automerge:` with nothing after it; anything else fails the final check.
        pad = " " * (automerge_key.start_mark.column + 2)
        out = _insert_after_line(text, automerge_key.end_mark.index, f"{pad}{section}:{nl}" + block_item(pad + "  "))
    elif isinstance(rules, yaml.SequenceNode) and rules.flow_style:
        close = rules.end_mark.index - 1
        insert = flow_item if not rules.value or _ends_in_comma(text, close) else f", {flow_item}"
        out = text[:close] + insert + text[close:]
    elif isinstance(rules, yaml.SequenceNode) and rules.value:
        # The sequence's own mark is its first `-`; the first rule's mark is its
        # first key, which may sit on the next line after a bare `-`.
        out = _insert_after_line(text, _exact_end(rules), block_item(" " * rules.start_mark.column))
    elif automerge.flow_style:
        if rules is not None:
            raise RulesError(f"`automerge.{section}` is not a list")
        close = automerge.end_mark.index - 1
        sep = ", " if automerge.value and not _ends_in_comma(text, close) else ""
        out = text[:close] + f"{sep}{section}: [{flow_item}]" + text[close:]
    elif rules_key is not None:
        # `deny:` with nothing after it. `deny: ~` or `deny: null` fall to the
        # final check: the items below them would not parse.
        pad = " " * (rules_key.start_mark.column + 2)
        out = _insert_after_line(text, rules_key.end_mark.index, block_item(pad))
    else:
        pad = " " * automerge.value[0][0].start_mark.column
        out = _insert_after_line(text, _exact_end(automerge), f"{pad}{section}:{nl}" + block_item(pad + "  "))
    edited = parse(out)
    # Exactly one rule more, nothing moved: an alias's node carries its anchor's
    # marks, so a position found from it can land inside another list.
    what = "an allow" if section == "allow" else "a deny"
    try:
        # The rule on its own: a bad fix type or environment says so, while a
        # splice that does not re-parse is a layout this cannot add to.
        added = _parse_rule(
            {"fix": fix_type, "environments": [environment]},
            f"automerge.{section}",
            wildcard_ok=section == "deny",
            warnings=[],
        )
    except RulesError as exc:
        raise RulesError(f"could not add {what} rule to {RULES_FILE}: {exc}") from exc
    if section == "allow":
        expected = (before.allow + (added,), before.deny)
    else:
        expected = (before.allow, before.deny + (added,))
    if edited.status != LOADED or (edited.allow, edited.deny) != expected:
        raise RulesError(f"could not add {what} rule to this layout of {RULES_FILE}")
    if section == "allow" and not decide(edited, fix_type, environment).allowed:
        # A deny already covers it, so the edit would allow nothing.
        raise RulesError(f"{RULES_FILE} already denies {fix_type} in {environment}; an allow rule would change nothing")
    return out


def _ends_in_comma(text: str, close: int) -> bool:
    """Whether a flow collection closing at `close` already ends in `,` (YAML allows it)."""
    return text[:close].rstrip().endswith(",")


def _exact_end(node: Any) -> int:
    """Where `node`'s last character is: a block collection's own end mark runs on past comments."""
    import yaml

    while isinstance(node, yaml.CollectionNode) and not node.flow_style and node.value:
        last = node.value[-1]
        node = last[1] if isinstance(last, tuple) else last
    return node.end_mark.index


def _insert_after_line(text: str, index: int, lines: str) -> str:
    end = text.find("\n", index)
    if end < 0:
        return text + ("\r\n" if lines.endswith("\r\n") else "\n") + lines
    return text[: end + 1] + lines + text[end + 1 :]


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
