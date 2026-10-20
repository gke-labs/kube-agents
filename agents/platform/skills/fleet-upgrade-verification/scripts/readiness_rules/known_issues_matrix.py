"""
Entry 10 of docs/designs/upgrade-failure-catalogue.md, at patch granularity: a GKE patch can
carry a provider bug in a managed add-on, published in the release notes or the known-issues
page with the patch that fixes it, and a fleet that upgrades every cluster at once meets the
bug everywhere at once. The rule has two halves.

The inventory (`inventory(read)`): the DaemonSets and Deployments in ADDON_NAMESPACES, from
the reads the report already makes (the Deployments come with the PDB read, the DaemonSets
with the namespaced read after it), each container's image name and tag. The report writes
it to each member's JSON as `addons` and prints it as a table; a DaemonSet read that failed
leaves the Deployments and is said in the note.

The matrix: `known_issues.json` beside the scripts, a table of published issues keyed by GKE
version (an exact version, or an inclusive `from`/`to` range, `to_exclusive` when the page
lists the fixed version as the range's last) and by add-on image name, with the fixed
version and the page it was read from. One risk when the cluster's control-plane version is
in an issue's versions and the add-on it names is in the inventory (at a listed tag, or at
any tag when the issue lists none); one more when the member's target version would land on
one. The table is data with a date: when `generated` is more than STALE_AFTER_DAYS before the
instant, or the server config lists a version newer than the table's `newest_gke_version`,
the rule files a risk that the table is stale rather than staying silent on a clean matrix.
Never a blocker: `CAN_BLOCK` is False.
"""

import json
from datetime import date
from pathlib import Path

from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_TARGET_TEXT,
    KIND_DAEMONSET,
    KIND_DEPLOYMENT,
    LIST_SEPARATOR,
    LOCATION_READ_SERVER_CONFIG,
    NAMESPACED_READ_KINDS,
    containers,
    image_repository_and_tag,
    new_result,
    pod_template_spec,
    project_read,
    read_error,
    read_items,
)
from readiness_rules.target_patch_valid import CHANNELS_KEY, VALID_VERSIONS_KEY, parse_version

RULE_ID = "known-issues-matrix"
ENTRY = 10
CAN_BLOCK = False

# Where GKE's managed add-ons run: the system namespace and Managed Service for Prometheus's.
ADDON_NAMESPACES = ("kube-system", "gmp-system")
ADDON_KINDS = (KIND_DAEMONSET, KIND_DEPLOYMENT)
DAEMONSET_READ_KIND = NAMESPACED_READ_KINDS[0]  # `daemonset`, the read the DaemonSets come with
# The table, beside the scripts, and its schema.
TABLE_PATH = Path(__file__).resolve().parents[1] / "known_issues.json"
TABLE_GENERATED_KEY = "generated"
TABLE_SOURCE_KEY = "source"
TABLE_NEWEST_VERSION_KEY = "newest_gke_version"
TABLE_ISSUES_KEY = "issues"
ISSUE_ID_KEY = "id"
ISSUE_VERSIONS_KEY = "gke_versions"
ISSUE_ADDON_KEY = "addon"
ISSUE_ADDON_VERSIONS_KEY = "addon_versions"
ISSUE_SUMMARY_KEY = "summary"
ISSUE_FIXED_IN_KEY = "fixed_in"
ISSUE_URL_KEY = "url"
RANGE_FROM_KEY = "from"
RANGE_TO_KEY = "to"
RANGE_TO_EXCLUSIVE_KEY = "to_exclusive"
# How old the table may be before a clean matrix is not trusted: the provider publishes a
# patch roughly weekly per channel, and a month without a regeneration spans several.
STALE_AFTER_DAYS = 30
DATE_FORMAT = "%Y-%m-%d"
KIND_STALE_AGE = "stale-age"
KIND_STALE_VERSION = "stale-version"
KIND_RUNNING = "running"
KIND_TARGET = "target"
ANY_TAG = "any tag"
ADDON_TABLE_PAIR_FORMAT = "{image} {tag}"
ADDON_TABLE_NONE = "none"
STALE_AGE_FORMAT = "known-issues table is stale: generated {generated}, {age} days ago, past {limit} days; regenerate it from the GKE release notes and known-issues page before trusting a clean matrix"
STALE_VERSION_FORMAT = "known-issues table is stale: generated {generated} before {newest} was listed (the table's newest known version is {table_newest}); regenerate it from the GKE release notes and known-issues page before trusting a clean matrix"
RUNNING_FORMAT = "known issue {id} ({summary}) applies to GKE {version} with {addon} {tag}; fixed in {fixed_in}: {url}"
TARGET_FORMAT = "target {version} lands on known issue {id} ({summary}) with {addon} {tag}; target {fixed_in} or hold: {url}"
NOTE_FORMAT = "{addons} add-on(s) inventoried in {namespaces}; {matches} known issue(s) match GKE {master} or target {target}; table generated {generated} ({issues} issue(s))"
DAEMONSETS_NOT_READ_REASON = "the DaemonSets were not read ({error}), so the add-on inventory lists Deployments only"
TABLE_UNREADABLE_REASON = "known-issues table {path} could not be read ({error})"
TABLE_SHAPE_REASON = "known-issues table {path} is not an object with a generated date, a source and an issues list"
GENERATED_UNPARSABLE_REASON = "known-issues table {path} carries no readable generated date ({generated!r})"
NO_TARGET = "none"
NO_MASTER = "none"


def inventory(read) -> list[dict]:
    """The add-ons the reads returned: one row per container of each DaemonSet and Deployment
    in ADDON_NAMESPACES, with the image's name and tag, sorted and without duplicates."""
    rows = []
    for item in read_items(read):
        if not isinstance(item, dict) or item.get("kind") not in ADDON_KINDS:
            continue
        meta = item.get("metadata") or {}
        namespace = str(meta.get("namespace") or "")
        if namespace not in ADDON_NAMESPACES:
            continue
        spec = pod_template_spec(item) or {}
        for container in containers(spec):
            image = str(container.get("image") or "")
            if not image:
                continue
            name, tag = image_repository_and_tag(image)
            rows.append({"namespace": namespace, "kind": item["kind"], "name": str(meta.get("name") or ""), "container": str(container.get("name") or ""), "image": name, "tag": tag})
    unique = {(r["namespace"], r["kind"], r["name"], r["container"], r["image"], r["tag"]): r for r in rows}
    return [unique[key] for key in sorted(unique)]


def inventory_cell(addons: list[dict]) -> str:
    """`fluent-bit v0.0.422-gke.21, gke-metadata-server ...`: each image name once per tag, for the table."""
    pairs = sorted({(row["image"], row["tag"]) for row in addons})
    return LIST_SEPARATOR.join(ADDON_TABLE_PAIR_FORMAT.format(image=image, tag=tag) for image, tag in pairs) if pairs else ADDON_TABLE_NONE


def load_table(path: Path = TABLE_PATH) -> tuple[dict | None, str | None]:
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, TABLE_UNREADABLE_REASON.format(path=path, error=e)
    if not isinstance(table, dict) or not isinstance(table.get(TABLE_ISSUES_KEY), list) or not table.get(TABLE_GENERATED_KEY) or not table.get(TABLE_SOURCE_KEY):
        return None, TABLE_SHAPE_REASON.format(path=path)
    return table, None


def version_matches(version_text, spec) -> bool:
    """Whether `version_text` is the exact version `spec` names, or inside the range it spells."""
    if isinstance(spec, str):
        return version_text == spec
    if not isinstance(spec, dict):
        return False
    version = parse_version(version_text)
    low = parse_version(spec.get(RANGE_FROM_KEY))
    high = parse_version(spec.get(RANGE_TO_KEY))
    if version is None or low is None or high is None:
        return False
    if version < low:
        return False
    return version < high if spec.get(RANGE_TO_EXCLUSIVE_KEY) else version <= high


def addon_present(issue: dict, addons: list[dict]) -> str | None:
    """The tag the inventory runs the issue's add-on at (ANY_TAG for an issue with no add-on), or None."""
    addon = issue.get(ISSUE_ADDON_KEY)
    if not addon:
        return ANY_TAG
    tags = issue.get(ISSUE_ADDON_VERSIONS_KEY) or []
    for row in addons:
        if row["image"] == addon and (not tags or row["tag"] in tags):
            return row["tag"] or ANY_TAG
    return None


def newest_listed_version(server_config) -> str | None:
    """The newest version any channel's validVersions lists, or None."""
    best, best_text = None, None
    channels = server_config.get(CHANNELS_KEY) if isinstance(server_config, dict) else None
    for entry in channels or []:
        if not isinstance(entry, dict):
            continue
        for candidate in entry.get(VALID_VERSIONS_KEY) or []:
            parsed = parse_version(candidate)
            if parsed is not None and (best is None or parsed > best):
                best, best_text = parsed, candidate
    return best_text


def _issue_finding(kind: str, issue: dict, version: str, tag: str) -> dict:
    return {"kind": kind, "issue": issue.get(ISSUE_ID_KEY), "summary": issue.get(ISSUE_SUMMARY_KEY), "version": version, "addon": issue.get(ISSUE_ADDON_KEY) or ANY_TAG, "tag": tag, "fixed_in": issue.get(ISSUE_FIXED_IN_KEY), "url": issue.get(ISSUE_URL_KEY)}


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    table, error = load_table()
    if error is not None:
        out["unknown"].append(error)
        return out
    generated_text = str(table.get(TABLE_GENERATED_KEY))
    try:
        generated = date.fromisoformat(generated_text)
    except ValueError:
        out["unknown"].append(GENERATED_UNPARSABLE_REASON.format(path=TABLE_PATH, generated=generated_text))
        return out
    daemonset_error = read_error(read, DAEMONSET_READ_KIND)
    if daemonset_error:
        out["unknown"].append(DAEMONSETS_NOT_READ_REASON.format(error=daemonset_error))
    addons = inventory(read)
    at = context[CONTEXT_AT]
    age_days = (at.date() - generated).days
    if age_days > STALE_AFTER_DAYS:
        out["risks"].append({"kind": KIND_STALE_AGE, "generated": generated_text, "age_days": age_days, "limit_days": STALE_AFTER_DAYS})
    server_config, _ = project_read(context, LOCATION_READ_SERVER_CONFIG)
    newest = newest_listed_version(server_config)
    table_newest = table.get(TABLE_NEWEST_VERSION_KEY)
    if newest and table_newest and (parse_version(table_newest) or ()) < (parse_version(newest) or ()):
        out["risks"].append({"kind": KIND_STALE_VERSION, "generated": generated_text, "newest": newest, "table_newest": table_newest})
    master = cluster.get("currentMasterVersion")
    target_text = context.get(CONTEXT_TARGET_TEXT)
    matches = 0
    for issue in table.get(TABLE_ISSUES_KEY) or []:
        if not isinstance(issue, dict):
            continue
        tag = addon_present(issue, addons)
        if tag is None:
            continue
        specs = issue.get(ISSUE_VERSIONS_KEY) or []
        if master and any(version_matches(master, spec) for spec in specs):
            out["risks"].append(_issue_finding(KIND_RUNNING, issue, master, tag))
            matches += 1
        if target_text and target_text != master and any(version_matches(target_text, spec) for spec in specs):
            out["risks"].append(_issue_finding(KIND_TARGET, issue, target_text, tag))
            matches += 1
    out["notes"].append(NOTE_FORMAT.format(addons=len(addons), namespaces=LIST_SEPARATOR.join(ADDON_NAMESPACES), matches=matches, master=master or NO_MASTER, target=target_text or NO_TARGET, generated=generated_text, issues=len(table.get(TABLE_ISSUES_KEY) or [])))
    return out


def describe(finding: dict) -> str:
    kind = finding.get("kind")
    if kind == KIND_STALE_AGE:
        return STALE_AGE_FORMAT.format(generated=finding["generated"], age=finding["age_days"], limit=finding["limit_days"])
    if kind == KIND_STALE_VERSION:
        return STALE_VERSION_FORMAT.format(generated=finding["generated"], newest=finding["newest"], table_newest=finding["table_newest"])
    template = TARGET_FORMAT if kind == KIND_TARGET else RUNNING_FORMAT
    return template.format(id=finding["issue"], summary=finding["summary"], version=finding["version"], addon=finding["addon"], tag=finding["tag"], fixed_in=finding["fixed_in"], url=finding["url"])
