"""Who a bench case's bugs go to first: its effective owner.

The rule (bench/CONTRIBUTING.md, "Ownership"): a case's `owner:` in its
`bench/tasks/<id>/task.yaml` names the person whose bugs it is first, any
GitHub login. When the field is absent, the case belongs to its domain's
`owner:` in `docs/designs/domains.yaml`. The literal `maintainers` named
nobody and is no longer accepted.

This module answers offline, from the checkout alone, so it cannot know
whether a login can be assigned an issue. That check belongs to whatever
files the bug, and is its contract with this module: assign
`effective_owner()`, and when GitHub refuses the assignee (not an
organisation member or collaborator), file to `domain_owner()` instead.

`scripts/validate_bench_cases.py` imports this to lint the field, and the eval
dashboard (`scripts/eval_dashboard/`) can import it to route a nightly red to a
person. Stdlib-only for the same reason as `eval_rosters.py`: the dashboard's
collector runs with no third-party imports, so domains.yaml is line-scanned.
`scripts/test_case_owner.py` asserts the scan agrees with a YAML parse of the
real file.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# Relative to a checkout, so effective_owner_of_case can read another one.
DOMAINS_PATH = pathlib.Path("docs") / "designs" / "domains.yaml"
TASKS_PATH = pathlib.Path("bench") / "tasks"
TASK_FILENAME = "task.yaml"
DOMAINS_FILE = REPO_ROOT / DOMAINS_PATH

# The value that used to mean "the OWNERS approvers". It named nobody, so the
# validator rejects it and this module never treats it as a person.
RETIRED_OWNER = "maintainers"
# GitHub's own login rule: alphanumerics and single interior hyphens, at most
# 39 characters, so a stray e-mail address or a display name is rejected
# before a reviewer has to spot it.
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")

_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
_DOMAIN_SLUG = re.compile(r"^\s*-\s*slug:\s*([A-Za-z0-9_-]+)")
_DOMAIN_OWNER = re.compile(r"^\s+owner:\s*([^\s#]+)")
# [ \t]*, not \s*: a blank `owner:` must not capture the next line.
_TASK_FIELD = r"^{key}:[ \t]*([^\s#]+)"
_QUOTES = "'\""
# A case id as the dashboard reads it from a build log: no slash, no leading dot.
_CASE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


def parse_domain_owners(text: str) -> dict[str, str]:
    """Domain slug to its `owner:` login, from domains.yaml text."""
    owners: dict[str, str] = {}
    slug = None
    for line in text.splitlines():
        if _TOP_LEVEL_KEY.match(line):
            slug = None  # a new top-level key ends the domains list
            continue
        found = _DOMAIN_SLUG.match(line)
        if found:
            slug = found.group(1)
            continue
        found = _DOMAIN_OWNER.match(line)
        if found and slug and slug not in owners:
            owners[slug] = found.group(1).strip(_QUOTES)
    return owners


def load_domain_owners(path: pathlib.Path = DOMAINS_FILE) -> dict[str, str]:
    return parse_domain_owners(path.read_text())


def is_retired_owner(owner: object) -> bool:
    return isinstance(owner, str) and owner.strip().lower() == RETIRED_OWNER


def is_person_login(owner: object) -> bool:
    """True when `owner` could name a GitHub user: a login, and not `maintainers`."""
    return isinstance(owner, str) and bool(GITHUB_LOGIN.fullmatch(owner)) and not is_retired_owner(owner)


def domain_owner(domain: object, domain_owners: dict[str, str]) -> str | None:
    """The domain's owner: the fallback when a case names no one, or GitHub refuses its owner."""
    return domain_owners.get(domain) if isinstance(domain, str) else None


def effective_owner(case_owner: object, domain: object, *, domain_owners: dict[str, str]) -> str | None:
    """The case's owner when it names a person, else its domain's owner.

    Not checked here: whether GitHub will accept the login as an assignee.
    A filer that gets a refusal must retry with `domain_owner()` (module
    docstring). None only when neither resolves: no case owner and a domain
    that domains.yaml does not define (the validator rejects such a case).
    """
    if is_person_login(case_owner):
        return str(case_owner)
    return domain_owner(domain, domain_owners)


def task_field(text: str, key: str) -> str | None:
    """The plain `key: value` at the top of task.yaml text, as the filer reads it.

    The validator rejects a case where this disagrees with its YAML read, so
    on a linted case the two are the same.
    """
    found = re.search(_TASK_FIELD.format(key=re.escape(key)), text, re.MULTILINE)
    return found.group(1).strip(_QUOTES) if found else None


def effective_owner_of_case(name: str, repo_root: pathlib.Path = REPO_ROOT) -> str | None:
    """`effective_owner` for bench/tasks/<name>/task.yaml on the checkout at `repo_root`.

    None for a case that does not exist there (a run of a since-renamed case)
    or a name that is not a case id, never a crash or a read outside
    bench/tasks. A checkout with no readable domains.yaml still raises.
    """
    if not isinstance(name, str) or not _CASE_NAME.fullmatch(name):
        return None
    try:
        text = (repo_root / TASKS_PATH / name / TASK_FILENAME).read_text()
    except OSError:
        return None
    return effective_owner(
        task_field(text, "owner"),
        task_field(text, "domain"),
        domain_owners=load_domain_owners(repo_root / DOMAINS_PATH),
    )
