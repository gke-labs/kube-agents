#!/usr/bin/env python3
"""Reject a broken bench case in a second instead of after a cluster lease.

A `task.yaml` mistake costs a full presubmit to discover: provision, deploy,
run the agent, score, read the log. Most of the mistakes are static. A domain
slug that matches no row in `docs/designs/domains.yaml` counts as coverage of
nothing; a fixture role the seeded fleet does not define is a case addressing
a defect that was never planted; a check with no assertion is a check that
cannot fail; a case in no `TASKS` entry never runs at all. None of those needs
a cluster to find.

This module is both the library the CI lint calls
(`scripts/test_task_registration.py`) and the CLI `make bench-case-check`
runs. One implementation, and the lint asserts that `validate_all()` came back
empty rather than checking for a hand-listed set of findings, so the fast
local check and the gating lint cannot drift apart and disagree about what a
valid case is. A rule added here therefore gates the day it is written, with
no second edit anywhere. The CLI runs in no workflow; the lint is what reds a
pull request.

`docs/designs/bench-case-format.md` is the contract these rules enforce, and
`docs/designs/bench-fleet-catalog.md` the fixture half of it.

Usage::

    python3 scripts/validate_bench_cases.py                 # every bench case
    python3 scripts/validate_bench_cases.py path/to/task.yaml [...]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "bench" / "tasks"
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
DOMAINS_FILE = REPO_ROOT / "docs" / "designs" / "domains.yaml"
FIXTURES_FILE = REPO_ROOT / "docs" / "designs" / "fleet-fixtures.yaml"
# The role vocabulary, owned by the catalogue that sits beside the Terraform
# and is resolved at run time by hack/fleet-kubeconfigs.sh. FIXTURES_FILE adds
# the day-N gates and the project-scoped fixtures on top of it; it does not get
# to name a role differently, which fixture_catalog_disagreements() enforces.
ROLE_CATALOG = REPO_ROOT / "bench" / "tf" / "fleet" / "fixtures.json"

# Cases that are neither in TASKS nor nightly-tiered, on purpose, for now.
# Every entry carries its reason; an entry without one should not survive
# review. Delete an entry once its case is registered -- staleness is only
# enforced for cases that no longer exist, because an in-flight branch
# registering a case must not red main the day it merges.
KNOWN_UNREGISTERED = {
    # Provisions its own cluster, so registering it costs every pull request
    # a second multi-minute provision. Whether it belongs in presubmit or
    # nightly is a tier decision nobody has made; this entry is the record
    # that the omission is known rather than accidental.
    "cluster-provision-kanban": "cluster-scoped provisioning task, tier decision pending",
}

# Cases that claim no domain because no row in domains.yaml describes them.
# "Covers nothing, and we checked" is a real answer; an absent field is not,
# because a domain with no case reports as uncovered and a case with no slug
# can stay green for months while the report shows the gap.
KNOWN_NO_DOMAIN = {
    "gpu-stress-test-diagnosis": (
        "a chat-prompted post-incident RCA, not the event-fired autoops triage "
        "that incident-triage names; no domains.yaml row describes it"
    ),
}

# Cases graded by the judge alone. The OutcomeValidity >= 0.7 fallback in
# hack/ci-eval-pr.sh is transitional -- its own header says the fallback is
# dead code to delete once every entry in TASKS carries a spec -- so an entry
# here is a debt with a name on it, not a supported case shape. Each says what
# would close it.
KNOWN_JUDGE_ONLY: dict[str, str] = {}

# Which field of each check type carries the assertion. A check whose type is
# here and none of whose listed fields is populated can only pass, whatever
# the run did.
#
# devops-bench's registry is authoritative and rejects an unknown type at
# spec-load time; this table is the pre-cluster copy. The first three come
# from the pinned devops-bench SHA in bench/pyproject.toml, the last three
# from bench/kube_agents_bench/verifiers.py -- a test re-derives those from
# the entry-point group so a new local verifier fails here rather than
# drifting silently.
#
# For the first three the named field is also required by the verifier's own
# pydantic model, so this rule only moves the rejection earlier -- from after a
# cluster lease to now. The rule earns its keep on the last three, where the
# field is optional and an empty or blank-stringed list is the shape that
# actually ships: a check that reads as an assertion and can only pass.
CHECK_ASSERTIONS: dict[str, tuple[str, ...]] = {
    # Upstream, cluster-reading.
    "resource_property": ("op",),
    "pod_healthy": ("selector",),
    "scaling_complete": ("deployment",),
    # This repository, seeded-fleet-reading: resource_property addressed by
    # fixture role rather than by kubeconfig, so `op` carries the assertion
    # for the same reason. See bench/kube_agents_bench/verifiers.py.
    "fleet_resource_property": ("op",),
    # This repository, run-reading.
    "report_contains": ("required_phrases", "forbidden_phrases", "any_of_phrases"),
    "ledger_issue_contains": ("required_phrases", "forbidden_phrases", "any_of_phrases"),
    "tool_called": ("tool_names",),
}

# Check types that read live cluster state. A case using one is asserting on
# something that has to be there, so it says what: either the seeded-fleet
# roles it depends on, or `fixtures: []` for a case that plants its own state
# (its own Terraform stack, its own namespace) and depends on no fixture.
CLUSTER_READING_TYPES = frozenset(
    {
        "resource_property",
        "pod_healthy",
        "scaling_complete",
        # The one type that reads the seeded fleet specifically, so the
        # `fixtures:` it forces is the list its own fixture_role values
        # resolve against.
        "fleet_resource_property",
    }
)

# Compound nodes assert nothing themselves; their children do.
COMPOUND_TYPES = frozenset({"sequence", "parallel", "all", "any", "none"})

# The entry vocabulary devops-bench's VerificationEntry accepts
# (verification/spec.py). Every rule below is one devops-bench enforces at
# spec-load time and this file enforces before a cluster is leased: a spec that
# fails to parse is not a soft failure, it adds 1.0 to the objective
# denominator with nothing in the numerator and reds the presubmit.
ENTRY_ROLES = frozenset({"objective", "safeguard"})
ENTRY_SEVERITIES = frozenset({"recoverable", "catastrophic"})
# `hold` is in the model's Literal and then rejected by its own validator, so
# it is a documented word that no spec may use.
ENTRY_MODES = frozenset({"converge", "assert"})


def _populated(value: Any) -> bool:
    """Whether an assertion field actually asserts something.

    Truthiness is not enough. `required_phrases: [""]` is a non-empty list, and
    `"" in text` is true of every text there has ever been, so the check passes
    whatever the run did -- the exact shape this rule exists to catch. Same for
    a list of blank strings, and for `required_phrases: ""`.
    """
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_populated(item) for item in value)
    return value is not None and value is not False


class CaseError(Exception):
    """A case file that could not be read at all."""


def _load_yaml(path: pathlib.Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CaseError(f"{path}: could not be parsed as YAML: {exc}") from exc


def known_domains() -> set[str]:
    """Slugs defined in docs/designs/domains.yaml."""
    data = _load_yaml(DOMAINS_FILE) or {}
    return {d["slug"] for d in data.get("domains") or []}


def _catalog_roles() -> dict[str, Any]:
    """Roles in bench/tf/fleet/fixtures.json, which owns the vocabulary."""
    try:
        data = json.loads(ROLE_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseError(f"{ROLE_CATALOG}: could not be parsed as JSON: {exc}") from exc
    return data.get("roles") or {}


def _overlay_fixtures() -> list[dict[str, Any]]:
    """Entries in docs/designs/fleet-fixtures.yaml."""
    data = _load_yaml(FIXTURES_FILE) or {}
    return [f for f in (data.get("fixtures") or []) if isinstance(f, dict)]


def known_fixture_roles() -> set[str]:
    """Every role slug a task.yaml may name.

    bench/tf/fleet/fixtures.json is the vocabulary; the overlay contributes
    only the roles that have no cluster slot, which a catalogue keyed by slot
    cannot hold (orphan-disks is project-scoped). Rejecting a slug is not the
    place to also complain that the two disagree -- that is a repository-level
    fault, not a fault of the case that happened to name the role, so it is
    reported once by fixture_catalog_disagreements() instead of once per case.
    """
    roles = set(_catalog_roles())
    roles |= {f["role"] for f in _overlay_fixtures() if f.get("slot") is None and "role" in f}
    return roles


def fixture_catalog_disagreements() -> list[str]:
    """Ways docs/designs/fleet-fixtures.yaml can drift from the catalogue.

    Both files describe the same planted defects, so the failure to design
    against is the two of them calling one fixture by two names -- which is
    exactly what a task.yaml would then do, naming the overlay's slug in
    `fixtures:` and the catalogue's in a check's `fixture_role:`, in the same
    file, for the same object.
    """
    catalog = _catalog_roles()
    problems = []
    for entry in _overlay_fixtures():
        role, slot = entry.get("role"), entry.get("slot")
        if not isinstance(role, str):
            problems.append(f"fleet-fixtures.yaml: {role!r} is not a role slug")
        elif slot is None:
            if role in catalog:
                problems.append(
                    f"fleet-fixtures.yaml: {role!r} declares no slot, but "
                    "bench/tf/fleet/fixtures.json gives it slot "
                    f"{catalog[role].get('cluster_slot')!r}"
                )
        elif role not in catalog:
            problems.append(
                f"fleet-fixtures.yaml: {role!r} is on slot {slot!r} but "
                "bench/tf/fleet/fixtures.json, which owns the role "
                "vocabulary, does not define it"
            )
        elif catalog[role].get("cluster_slot") != slot:
            problems.append(
                f"fleet-fixtures.yaml puts {role!r} on slot {slot!r}; "
                "bench/tf/fleet/fixtures.json puts it on "
                f"{catalog[role].get('cluster_slot')!r}"
            )
    return problems


def registered_cases() -> set[str] | None:
    """Case names in the TASKS array, commented entries included.

    A commented entry counts: it is registered, pending activation, which is
    how the Phase 2 scenarios wait for the seeded fleet.

    TASKS is read from the script's text rather than by executing it: the
    script provisions clusters and reads secrets, so running it to ask a
    question is not an option. The parse is deliberately narrow -- the
    TASKS=( ... ) block only -- and returns None when it finds nothing, so a
    drifted parse fails loudly rather than calling every case an orphan.
    """
    text = EVAL_SCRIPT.read_text(encoding="utf-8")
    # Multi-line arrays end at a line holding only ")": stopping at the first
    # bare ")" character instead would truncate the block at a parenthesis
    # inside a comment and silently drop every entry below it.
    match = re.search(r"^TASKS=\((.*?)^\)", text, re.M | re.S) or re.search(
        r"^TASKS=\((.*)\)\s*$", text, re.M
    )
    if match is None:
        return None
    return set(re.findall(r"tasks/([A-Za-z0-9_-]+)/task\.yaml", match.group(1)))


def bench_cases() -> dict[str, pathlib.Path]:
    """Every case directory under bench/tasks/, by name."""
    return {p.parent.name: p for p in sorted(TASKS_DIR.glob("*/task.yaml"))}


def _check_assertions(node: Any, where: str, problems: list[str]) -> None:
    """Walk one check subtree, reporting nodes that cannot fail."""
    if not isinstance(node, dict):
        problems.append(f"{where}: check node is not a mapping")
        return

    node_type = node.get("type")
    if not isinstance(node_type, str) or not node_type:
        problems.append(f"{where}: check node has no 'type' discriminator")
        return

    if node_type in COMPOUND_TYPES:
        children = node.get("checks")
        if not isinstance(children, list) or not children:
            problems.append(
                f"{where}: compound check '{node_type}' has no 'checks' members, "
                "so it asserts nothing"
            )
            return
        for index, child in enumerate(children):
            _check_assertions(child, f"{where} > {node_type}[{index}]", problems)
        return

    fields = CHECK_ASSERTIONS.get(node_type)
    if fields is None:
        problems.append(
            f"{where}: unknown check type {node_type!r}; known types are "
            f"{sorted(CHECK_ASSERTIONS) + sorted(COMPOUND_TYPES)}. A new "
            "verifier needs a CHECK_ASSERTIONS entry naming the field that "
            "carries its assertion."
        )
        return

    # Populated rather than present: an empty phrase list, a list of empty
    # strings and an empty tool-name list are all syntactically fine and all
    # make the check unfailable, which is the shape this rule exists to catch.
    # `resource_property`'s `op` is the assertion whatever its value --
    # `absent` and `exists` say something about the match set rather than about
    # a value.
    if not any(_populated(node.get(field)) for field in fields):
        problems.append(
            f"{where}: check '{node_type}' populates none of "
            f"{list(fields)}, so it can only pass"
        )


def _check_types(node: Any, found: set[str]) -> None:
    """Every check type used anywhere in one check subtree."""
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if isinstance(node_type, str):
        found.add(node_type)
    for child in node.get("checks") or []:
        _check_types(child, found)


def _fixture_roles(node: Any, found: set[str]) -> None:
    """Every `fixture_role:` named anywhere in one check subtree."""
    if not isinstance(node, dict):
        return
    role = node.get("fixture_role")
    if isinstance(role, str):
        found.add(role)
    for child in node.get("checks") or []:
        _fixture_roles(child, found)


def _entry_vocabulary(entry: dict[str, Any], where: str, problems: list[str]) -> None:
    """The role/severity/mode/weight rules devops-bench applies at spec load."""
    role = entry.get("role")
    if role not in ENTRY_ROLES:
        problems.append(
            f"{where}: role {role!r} is not one of {sorted(ENTRY_ROLES)}; an "
            "entry says what it is for before it says anything else"
        )

    severity = entry.get("severity")
    if role == "safeguard" and severity is None:
        problems.append(
            f"{where}: a safeguard must declare a severity, "
            f"one of {sorted(ENTRY_SEVERITIES)}"
        )
    elif role == "objective" and severity is not None:
        problems.append(
            f"{where}: severity {severity!r} on an objective; severity says how "
            "bad a safeguard tripping is and an objective cannot trip"
        )
    elif severity is not None and severity not in ENTRY_SEVERITIES:
        problems.append(
            f"{where}: severity {severity!r} is not one of {sorted(ENTRY_SEVERITIES)}"
        )

    mode = entry.get("mode")
    if mode is not None and mode not in ENTRY_MODES:
        problems.append(
            f"{where}: mode {mode!r} is not one of {sorted(ENTRY_MODES)}"
            + ("; 'hold' is declared in the model and rejected by it" if mode == "hold" else "")
        )

    weight = entry.get("weight")
    if weight is not None and (not isinstance(weight, (int, float)) or weight <= 0):
        problems.append(f"{where}: weight {weight!r} must be a number greater than 0")


def validate_case(name: str, path: pathlib.Path, *, registered: set[str] | None) -> list[str]:
    """Every problem with one case file, as reader-facing sentences."""
    problems: list[str] = []
    spec = _load_yaml(path)
    if not isinstance(spec, dict):
        return [f"{path}: does not parse to a mapping"]

    # The id key. devops-bench accepts task_id as an alias for id
    # (tasks/schema.py, from_dict) and prefers id when both are present, so a
    # file carrying both silently loses the task_id value. One spelling here.
    if "task_id" in spec:
        problems.append(
            "uses the deprecated 'task_id:' key; rename it to 'id:' "
            "(devops-bench accepts both and prefers 'id', so this is a "
            "rename with no behaviour change)"
        )
    case_id = spec.get("id") or spec.get("task_id")
    if not case_id:
        problems.append("declares no 'id:'")
    elif str(case_id) != name:
        problems.append(
            f"id {str(case_id)!r} does not match its directory name {name!r}; "
            "the directory name is what TASKS, the results file and every "
            "lint key on"
        )

    # The domain slug. Coverage is counted per domain, so a case with no slug
    # is invisible to the count that decides whether a domain is covered.
    domain = spec.get("domain")
    if domain is None:
        if name not in KNOWN_NO_DOMAIN:
            problems.append(
                "declares no 'domain:'. Claim a slug from "
                "docs/designs/domains.yaml, or add a reviewed KNOWN_NO_DOMAIN "
                "entry in scripts/validate_bench_cases.py saying why no row "
                "describes this case"
            )
    elif not isinstance(domain, str):
        problems.append(f"claims domain {domain!r}, which is not a slug string")
    elif domain not in known_domains():
        problems.append(
            f"claims domain {domain!r}, which docs/designs/domains.yaml does "
            "not define"
        )

    # Fixture roles. Cases address the seeded fleet by role, never by cluster
    # name or project id -- see docs/designs/bench-fleet-catalog.md.
    fixtures = spec.get("fixtures")
    if fixtures is not None:
        if not isinstance(fixtures, list):
            problems.append("'fixtures:' must be a list of role slugs")
        else:
            roles = known_fixture_roles()
            for role in fixtures:
                if not isinstance(role, str):
                    problems.append(
                        f"names fixture role {role!r}, which is not a slug string"
                    )
                elif role not in roles:
                    problems.append(
                        f"names fixture role {role!r}, which neither "
                        "bench/tf/fleet/fixtures.json nor "
                        "docs/designs/fleet-fixtures.yaml defines"
                    )

    # The verification spec.
    entries = spec.get("verification_spec")
    if not entries:
        if name not in KNOWN_JUDGE_ONLY:
            problems.append(
                "carries no 'verification_spec:'. The OutcomeValidity >= 0.7 "
                "fallback in hack/ci-eval-pr.sh is transitional and a "
                "judge-only case cannot fail for the reason it was written; "
                "declare at least one objective naming something the case "
                "planted, or add a reviewed KNOWN_JUDGE_ONLY entry in "
                "scripts/validate_bench_cases.py"
            )
    elif not isinstance(entries, list):
        problems.append("'verification_spec:' must be a list of entries")
    else:
        seen: set[str] = set()
        used_types: set[str] = set()
        used_roles: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                problems.append(f"verification_spec[{index}]: entry is not a mapping")
                continue
            label = entry.get("name")
            where = f"verification_spec[{index}]"
            if not isinstance(label, str) or not label:
                problems.append(f"{where}: entry has no 'name:'")
            else:
                where = f"check {label!r}"
                if label in seen:
                    problems.append(f"{where}: duplicate entry name")
                seen.add(label)
            _entry_vocabulary(entry, where, problems)
            if "check" not in entry:
                problems.append(f"{where}: entry has no 'check:' subtree")
            else:
                _check_assertions(entry["check"], where, problems)
                _check_types(entry["check"], used_types)
                _fixture_roles(entry["check"], used_roles)

        # The two ways a case names a fixture have to be the same name. A
        # check's `fixture_role:` is what the runner resolves to a kubeconfig;
        # `fixtures:` is what a human greps when a cluster is replaced. A case
        # naming one planted defect `crashloop-workload` in one and something
        # else in the other reads as depending on two fixtures and is why the
        # role vocabulary has a single owner -- see fleet-fixtures.yaml's
        # header and fixture_catalog_disagreements().
        if isinstance(fixtures, list):
            undeclared = sorted(used_roles - {f for f in fixtures if isinstance(f, str)})
            for role in undeclared:
                problems.append(
                    f"a check names fixture role {role!r}, which the case's "
                    "own 'fixtures:' list does not declare"
                )

        if fixtures is None and used_types & CLUSTER_READING_TYPES:
            problems.append(
                "reads live cluster state ("
                + ", ".join(sorted(used_types & CLUSTER_READING_TYPES))
                + ") and declares no 'fixtures:'. List the seeded-fleet roles "
                "it depends on, so the fleet owner replacing a cluster can "
                "grep for the cases that go quiet, or declare 'fixtures: []' "
                "for a case that plants its own state"
            )

        # The presubmit decides whether a case has a spec by grepping for a
        # `verification_spec:` line with nothing after it (hack/ci-eval-pr.sh,
        # task_has_spec). A flow-style spec on one line is a valid, loadable
        # spec that the gate cannot see, so the case drops back to the
        # judge-only OutcomeValidity fallback without saying so.
        if not re.search(r"^verification_spec:\s*$", path.read_text(encoding="utf-8"), re.M):
            problems.append(
                "declares its 'verification_spec:' inline rather than as a "
                "block. hack/ci-eval-pr.sh's task_has_spec matches a bare "
                "'verification_spec:' line, so an inline spec runs its checks "
                "and is still graded by the judge-only fallback"
            )

    # Registration. hack/ci-eval-pr.sh runs the cases in TASKS and only those.
    if registered is not None and name not in registered and name not in KNOWN_UNREGISTERED:
        problems.append(
            "is registered nowhere and never runs. Add it to TASKS in "
            "hack/ci-eval-pr.sh (a commented entry counts as registered, "
            "pending activation), or add a reviewed KNOWN_UNREGISTERED entry "
            "in scripts/validate_bench_cases.py with the reason it must not run"
        )

    return problems


def validate_paths(paths: list[pathlib.Path]) -> dict[str, list[str]]:
    """Validate specific task.yaml paths, keyed by their directory name.

    Registration is skipped for a path outside bench/tasks/: a file being
    checked from somewhere else (a fetched pull-request copy, a scratch draft)
    is not expected to be registered yet, and reporting it would drown the
    findings that matter.
    """
    registered = registered_cases()
    out: dict[str, list[str]] = {}
    for path in paths:
        resolved = path.resolve()
        in_tree = resolved.parent.parent == TASKS_DIR
        name = resolved.parent.name if resolved.name == "task.yaml" else resolved.stem
        out[name] = validate_case(name, resolved, registered=registered if in_tree else None)
    return out


def validate_all() -> dict[str, list[str]]:
    """Validate every case under bench/tasks/."""
    registered = registered_cases()
    if registered is None:
        return {
            "<TASKS array>": [
                "could not find a TASKS=( ... ) array in hack/ci-eval-pr.sh -- "
                "the script changed shape and this parse needs updating"
            ]
        }
    if not registered:
        return {
            "<TASKS array>": [
                "the TASKS array in hack/ci-eval-pr.sh parsed to no cases -- "
                "either the array is empty or this parse has drifted"
            ]
        }
    results: dict[str, list[str]] = {}
    for name, path in bench_cases().items():
        # One unreadable file must not hide every other case's findings.
        try:
            results[name] = validate_case(name, path, registered=registered)
        except CaseError as exc:
            results[name] = [str(exc)]
    return results


def stale_allowlist_entries() -> list[str]:
    """Allowlist entries naming a case that no longer exists."""
    existing = bench_cases()
    stale = []
    for label, entries in (
        ("KNOWN_UNREGISTERED", KNOWN_UNREGISTERED),
        ("KNOWN_NO_DOMAIN", KNOWN_NO_DOMAIN),
        ("KNOWN_JUDGE_ONLY", KNOWN_JUDGE_ONLY),
    ):
        stale += [f"{label}: {name}" for name in sorted(entries) if name not in existing]
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        type=pathlib.Path,
        help="task.yaml files to check; defaults to every case under bench/tasks/",
    )
    args = parser.parse_args(argv)

    try:
        results = validate_paths(args.paths) if args.paths else validate_all()
        stale = [] if args.paths else stale_allowlist_entries()
        # Repository-level, so it runs even when a path subset was named: a
        # case that names a drifted role is rejected above, but the drift
        # itself is worth reporting even when no case has hit it yet.
        drift = fixture_catalog_disagreements()
    except CaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    failed = 0
    for name in sorted(results):
        problems = results[name]
        if not problems:
            continue
        failed += 1
        print(f"{name}:")
        for problem in problems:
            print(f"  - {problem}")

    for entry in stale:
        failed += 1
        print(f"stale allowlist entry, delete it: {entry}")

    for entry in drift:
        failed += 1
        print(f"fixture catalogue drift: {entry}")

    if failed:
        print(f"\n{failed} case(s) rejected out of {len(results)} checked.")
        return 1
    print(f"{len(results)} bench case(s) OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
