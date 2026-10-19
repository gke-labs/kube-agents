"""Who a bench case's bugs go to first: its effective owner.

The rule (bench/CONTRIBUTING.md, "Owner"): a case's `owner:` in its
`bench/tasks/<id>/task.yaml` names the person whose bugs it is first. When the
field is absent, or names someone who is not in the root `OWNERS` file
(approvers or reviewers), the case falls back to its domain's `owner:` in
`docs/designs/domains.yaml`. The literal `maintainers` named nobody and is no
longer accepted.

`scripts/validate_bench_cases.py` imports this to lint the field, and the eval
dashboard (`scripts/eval_dashboard/`) can import it to route a nightly red to a
person. Stdlib-only for the same reason as `eval_rosters.py`: the dashboard's
collector runs with no third-party imports, so the two files are line-scanned.
`scripts/test_case_owner.py` asserts the scans agree with a YAML parse of the
real files.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# Relative to a checkout, so effective_owner_of_case can read another one.
OWNERS_PATH = pathlib.Path("OWNERS")
DOMAINS_PATH = pathlib.Path("docs") / "designs" / "domains.yaml"
TASKS_PATH = pathlib.Path("bench") / "tasks"
TASK_FILENAME = "task.yaml"
OWNERS_FILE = REPO_ROOT / OWNERS_PATH
DOMAINS_FILE = REPO_ROOT / DOMAINS_PATH

# The value that used to mean "the OWNERS approvers". It named nobody, so the
# validator rejects it and this module never treats it as a person.
RETIRED_OWNER = "maintainers"
# The two root OWNERS lists whose members may own a case.
OWNERS_LIST_KEYS = ("approvers", "reviewers")

_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
_LIST_ITEM = re.compile(r"^\s*-\s*([^\s#]+)")
_DOMAIN_SLUG = re.compile(r"^\s*-\s*slug:\s*([A-Za-z0-9_-]+)")
_DOMAIN_OWNER = re.compile(r"^\s+owner:\s*([^\s#]+)")
_TASK_FIELD = r"^{key}:\s*([^\s#]+)"
_QUOTES = "'\""


def parse_root_owners(text: str) -> frozenset[str]:
    """Lower-cased logins in the OWNERS text's `approvers:` and `reviewers:` lists.

    Lower-cased because GitHub logins are case-insensitive, as
    scripts/request_reviewers.py and scripts/test_ownership.py read them.
    """
    logins: set[str] = set()
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key = _TOP_LEVEL_KEY.match(line)
        if key:
            current = key.group(1)
            continue
        item = _LIST_ITEM.match(line)
        if item and current in OWNERS_LIST_KEYS:
            logins.add(item.group(1).strip(_QUOTES).lower())
    return frozenset(logins)


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


def load_root_owners(path: pathlib.Path = OWNERS_FILE) -> frozenset[str]:
    return parse_root_owners(path.read_text())


def load_domain_owners(path: pathlib.Path = DOMAINS_FILE) -> dict[str, str]:
    return parse_domain_owners(path.read_text())


def is_retired_owner(owner: object) -> bool:
    return isinstance(owner, str) and owner.strip().lower() == RETIRED_OWNER


def is_listed_owner(owner: object, root_owners: frozenset[str]) -> bool:
    """True when `owner` is a login in the root OWNERS lists (case-insensitive)."""
    return isinstance(owner, str) and owner.strip().lower() in root_owners


def effective_owner(
    case_owner: object,
    domain: object,
    *,
    root_owners: frozenset[str],
    domain_owners: dict[str, str],
) -> str | None:
    """The case's owner when it is a person in OWNERS, else its domain's owner.

    None only when neither resolves: no valid case owner and a domain that
    domains.yaml does not define (the validator rejects such a case).
    """
    if is_listed_owner(case_owner, root_owners):
        return str(case_owner).strip()
    if isinstance(domain, str):
        return domain_owners.get(domain)
    return None


def _task_field(text: str, key: str) -> str | None:
    found = re.search(_TASK_FIELD.format(key=re.escape(key)), text, re.MULTILINE)
    return found.group(1).strip(_QUOTES) if found else None


def effective_owner_of_case(name: str, repo_root: pathlib.Path = REPO_ROOT) -> str | None:
    """`effective_owner` for bench/tasks/<name>/task.yaml on the checkout at `repo_root`."""
    text = (repo_root / TASKS_PATH / name / TASK_FILENAME).read_text()
    return effective_owner(
        _task_field(text, "owner"),
        _task_field(text, "domain"),
        root_owners=load_root_owners(repo_root / OWNERS_PATH),
        domain_owners=load_domain_owners(repo_root / DOMAINS_PATH),
    )
