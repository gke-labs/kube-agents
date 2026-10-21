"""
Entry 26 of docs/designs/upgrade-failure-catalogue.md: a known issue in an add-on at the target
version. A GKE patch can carry a provider bug, published on the known-issues page with the patch that fixes it, and a
fleet that upgrades every cluster at once meets the bug everywhere at once. The rule has two
halves.

The inventory (`inventory(read)`): the DaemonSets and Deployments in ADDON_NAMESPACES, from
the reads the report already makes (the Deployments come with the PDB read, the DaemonSets
with the namespaced read after it), each container's image name and tag. The report writes
it to each member's JSON as `addons` and prints it as a table; a DaemonSet read that failed
leaves the Deployments and is said in the note.

The matrix: `known_issues.json` beside the skill's top-level modules, read through the module
loader as the removal table is (`pkgutil.get_data`; `__file__` is a path the daily watch's
sandbox lacks). It carries the known-issues page's rows whose identified versions name a
minor from 1.32 up (the JSON's `source_note` states the rule): each with the page's cells
verbatim, a machine reading of its versions (`gke_versions`: an exact version; a `from`/`to`
range, `to_exclusive` when the fixed build is the range's end; a whole `minor`, bounded by
`from` and `to` inside it; `through_minor` for "X and earlier"; `from_minor` for "X and
later"; `all_versions`), the add-on image it is in where the page names one the inventory
carries, and the page anchor. One risk when the cluster's control-plane version is in an
issue's versions and the add-on it names is in the inventory (at a listed tag, or at any tag
when the issue lists none; an issue naming no add-on matches by version alone); one more when
the member's target version would land on one. A target given without its build
(`MAJOR.MINOR.PATCH`, which the API reads as the patch's newest listed build) is graded against
every listed build of the patch the member's upgrade can land on, read as `target-patch-valid`
reads it (`target_patch_valid.target_builds`: the channel's list, or `validNodeVersions`
when the pools alone move): an issue that affects all of them is the target's risk or open note
as for a full version, one that affects some is a note naming the affected builds, and a patch
no list its move reads carries a build of is a note and no grade; a server config that was not
read, or that does not carry the channel, is an `unknown` naming it, never the note. The grade is
the matched spec's: a match
through a spec with no upper bound (`all_versions`, `from_minor`, `from` with no `to`), which
stands on every later version until the page changes, is a note on the member naming the
open issue, not a risk, and is not counted among the matches; a match through a bounded spec
is the risk. The table is data with a date: when
`generated` is more than STALE_AFTER_DAYS before the instant, the rule files a risk that the
table is stale rather than staying silent on a clean matrix; the newest version the table
knows is in the note and not graded. Never a blocker: `CAN_BLOCK` is False.
"""

import json
import pkgutil
import re
from datetime import date

from readiness_rules import (
    CONTEXT_ADDONS,
    CONTEXT_AT,
    CONTEXT_LOCATION,
    CONTEXT_TARGET_TEXT,
    KIND_DAEMONSET,
    KIND_DEPLOYMENT,
    LIST_SEPARATOR,
    NAMESPACED_READ_KINDS,
    LOCATION_READ_SERVER_CONFIG,
    containers,
    image_repository_and_tag,
    new_result,
    pod_template_spec,
    project_read,
    read_error,
    read_items,
)
from readiness_rules.target_patch_valid import (
    BUILD_SEPARATOR,
    CHANNEL_KEY,
    CURRENT_MASTER_VERSION_KEY,
    NO_CHANNEL_LABEL,
    NO_CHANNEL_VALUES,
    RELEASE_CHANNEL_KEY,
    SERVER_CONFIG_NOT_A_DICT,
    VALID_NODE_VERSIONS_KEY,
    channel_roster,
    control_plane_moves,
    parse_version,
    target_builds,
)

RULE_ID = "known-issues-matrix"
ENTRY = 26
CAN_BLOCK = False

# Where GKE's managed add-ons run: the system namespace and Managed Service for Prometheus's.
ADDON_NAMESPACES = ("kube-system", "gmp-system")
ADDON_KINDS = (KIND_DAEMONSET, KIND_DEPLOYMENT)
DAEMONSET_READ_KIND = NAMESPACED_READ_KINDS[0]  # `daemonset`, the read the DaemonSets come with
# The table: beside the skill's top-level modules, read through the loader of one of them
# (the shape tables, which read the removal table the same way), so the daily watch's
# sandbox, whose loader serves the skill's data files beside its modules, finds it too.
TABLE_NAME = "known_issues.json"
TABLE_MODULE = "upgrade_shape_tables"
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
# A version spec in `gke_versions`: a string is an exact version; a dict carries any of these.
RANGE_FROM_KEY = "from"
RANGE_TO_KEY = "to"
RANGE_TO_EXCLUSIVE_KEY = "to_exclusive"
MINOR_KEY = "minor"
THROUGH_MINOR_KEY = "through_minor"
FROM_MINOR_KEY = "from_minor"
ALL_VERSIONS_KEY = "all_versions"
MINOR_RE = re.compile(r"^(\d+)\.(\d+)$")
MINOR_PARTS = 2
# How old the table may be before a clean matrix is not trusted: the provider publishes a
# patch roughly weekly per channel, and a month without a regeneration spans several.
STALE_AFTER_DAYS = 30
KIND_STALE_AGE = "stale-age"
KIND_RUNNING = "running"
KIND_TARGET = "target"
ANY_TAG = "any tag"
ADDON_TABLE_PAIR_FORMAT = "{image} {tag}"
ADDON_TABLE_NONE = "none"
STALE_AGE_FORMAT = "known-issues table is stale: generated {generated}, {age} days ago, past {limit} days; regenerate it from the GKE known-issues page before trusting a clean matrix"
WITH_ADDON_FORMAT = "with {addon} {tag}"
NO_ADDON_TEXT = "(the page names no add-on)"
# What the page gives for a fix: a version text, or one of these, which the finding says plainly.
NO_FIX_VALUES = ("TBD", "none listed")
FIX_TEXT_FORMAT = "fixed in {fixed_in}"
NO_FIX_TEXT = "no fix published"
TARGET_ADVICE = "target a fixed patch or hold"
TARGET_ADVICE_NO_FIX = "hold"
RUNNING_FORMAT = "known issue {id} ({summary}) applies to GKE {version} {addon_text}; {fix_text}: {url}"
TARGET_FORMAT = "target {version} lands on known issue {id} ({summary}) {addon_text}; {fix_text}; {advice}: {url}"
# A build-less target against the builds of its patch the channel lists.
GRADE_RISK = "risk"
GRADE_OPEN = "open"
TARGET_BUILDS_SEPARATOR = ", "
SHORT_TARGET_NO_BUILDS_NOTE = "target {target} names no build the {channel} channel lists at {location}, so the table was not read for it; a target with its build is graded"
SHORT_TARGET_NO_BUILDS_POOL_MOVE_NOTE = "target {target} names no build the {channel} channel or validNodeVersions lists at {location} for its pool move, so the table was not read for it; a target with its build is graded"
# The server config the builds are read from: a failed read, or a channel it does not carry, is an unknown, never the note.
SERVER_CONFIG_UNREAD_REASON = "server config at {location} not read ({error}), so whether the {channel} channel still lists a build of target {target} is unknown; a target with its build is graded"
CHANNEL_NOT_IN_CONFIG_REASON = "channel {channel} is not in the server config at {location}, so whether it lists a build of target {target} is unknown; a target with its build is graded"
PARTIAL_TARGET_NOTE_FORMAT = "target {target} is graded against the {n} listed build(s) of its patch ({builds}); known issue {id} ({summary}) affects {affected} of them, so whether the upgrade lands on it depends on the build picked; a target with its build says which: {url}"
NOTE_FORMAT = "{addons} add-on(s) inventoried in {namespaces}; {matches} known issue(s) match GKE {master} or target {target} ({open} open issue(s) with no published fix noted); table generated {generated} ({issues} issue(s), newest known version {newest})"
OPEN_NOTE_FORMAT = "{n} open known issue(s) with no published fix on GKE {version}, noted and not graded as risks: {ids}; each is on {url}"
OPEN_IDS_SEPARATOR = ", "
DAEMONSETS_NOT_READ_REASON = "the DaemonSets were not read ({error}), so the add-on inventory lists Deployments only"
NO_LOADER_DATA = "the module loader served no data"
TABLE_UNREADABLE_REASON = "known-issues table {name} could not be read beside {module} ({error})"
TABLE_SHAPE_REASON = "known-issues table {name} is not an object with a generated date, a source and an issues list"
GENERATED_UNPARSABLE_REASON = "known-issues table {name} carries no readable generated date ({generated!r})"
NO_TARGET = "none"
NO_MASTER = "none"
NO_NEWEST = "none"
# The table once per process, keyed by what it was read as: the report grades every member
# in one process, and the table does not change under it.
_TABLE_CACHE: dict[tuple[str, str], dict] = {}


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


def load_table(name: str = TABLE_NAME, module: str = TABLE_MODULE) -> tuple[dict | None, str | None]:
    """The table, read through `module`'s loader (the file beside it on a checkout, the shipped
    copy under the daily watch) once per process; (None, why) when it cannot be read or is not
    the table's shape."""
    cached = _TABLE_CACHE.get((module, name))
    if cached is not None:
        return cached, None
    try:
        raw = pkgutil.get_data(module, name)
        if raw is None:
            raise OSError(NO_LOADER_DATA)
        table = json.loads(raw)
    except (OSError, ValueError, ImportError) as e:
        return None, TABLE_UNREADABLE_REASON.format(name=name, module=module, error=e)
    if not isinstance(table, dict) or not isinstance(table.get(TABLE_ISSUES_KEY), list) or not table.get(TABLE_GENERATED_KEY) or not table.get(TABLE_SOURCE_KEY):
        return None, TABLE_SHAPE_REASON.format(name=name)
    _TABLE_CACHE[(module, name)] = table
    return table, None


def _minor(text) -> tuple | None:
    m = MINOR_RE.match(text.strip()) if isinstance(text, str) else None
    return tuple(int(part) for part in m.groups()) if m else None


def version_matches(version_text, spec) -> bool:
    """Whether `version_text` is the exact version `spec` names, or inside the set a dict spec
    spells: `from`/`to` bounds (inclusive, `to` exclusive with `to_exclusive`), a `minor` the
    version has to be in, `through_minor` (its minor at most this), `from_minor` (at least this),
    or `all_versions`. A dict naming none of them, or a bound that does not parse, matches
    nothing."""
    if isinstance(spec, str):
        return version_text == spec
    if not isinstance(spec, dict):
        return False
    version = parse_version(version_text)
    if version is None:
        return False
    if spec.get(ALL_VERSIONS_KEY) is True:
        return True
    bounded = False
    for key, test in ((MINOR_KEY, lambda minor: version[:MINOR_PARTS] == minor), (THROUGH_MINOR_KEY, lambda minor: version[:MINOR_PARTS] <= minor), (FROM_MINOR_KEY, lambda minor: version[:MINOR_PARTS] >= minor)):
        if key in spec:
            minor = _minor(spec.get(key))
            if minor is None or not test(minor):
                return False
            bounded = True
    if RANGE_FROM_KEY in spec:
        low = parse_version(spec.get(RANGE_FROM_KEY))
        if low is None or version < low:
            return False
        bounded = True
    if RANGE_TO_KEY in spec:
        high = parse_version(spec.get(RANGE_TO_KEY))
        if high is None or (version >= high if spec.get(RANGE_TO_EXCLUSIVE_KEY) else version > high):
            return False
        bounded = True
    return bounded


def spec_is_open(spec) -> bool:
    """Whether a version spec has no upper bound: every version, a minor and later, or a build and
    later with no `to` and no minor to end it. A match through one is a note, not a risk."""
    if not isinstance(spec, dict):
        return False
    return spec.get(ALL_VERSIONS_KEY) is True or FROM_MINOR_KEY in spec or (RANGE_FROM_KEY in spec and RANGE_TO_KEY not in spec and MINOR_KEY not in spec)


def target_candidates(target_text, cluster: dict, context) -> tuple[list[str] | None, str | None, str | None]:
    """(the builds a target names, a note, an unknown): itself for a full version; for a build-less
    one, the listed builds of its patch the member's upgrade can land on, as `target-patch-valid`
    reads them (`target_builds`: the channel's list, validMasterVersions on no channel, or
    validNodeVersions when the pools alone move), newest first; (None, a note, None) when no list
    its move reads carries one; (None, None, why) when the server config was not read or does not
    carry the channel, which is an unknown and never a note."""
    if not isinstance(target_text, str) or not target_text:
        return None, None, None
    if BUILD_SEPARATOR in target_text:
        return [target_text], None, None
    location = str(context.get(CONTEXT_LOCATION) or cluster.get("location") or "")
    channel = str((cluster.get(RELEASE_CHANNEL_KEY) or {}).get(CHANNEL_KEY) or "")
    channel_label = channel if channel not in NO_CHANNEL_VALUES else NO_CHANNEL_LABEL
    server_config, error = project_read(context, LOCATION_READ_SERVER_CONFIG)
    if error is None and not isinstance(server_config, dict):
        error = SERVER_CONFIG_NOT_A_DICT.format(type=type(server_config).__name__)
    if error is not None:
        return None, None, SERVER_CONFIG_UNREAD_REASON.format(location=location, error=error, channel=channel_label, target=target_text)
    roster, _ = channel_roster(server_config, channel)
    if roster is None:
        return None, None, CHANNEL_NOT_IN_CONFIG_REASON.format(channel=channel_label, location=location, target=target_text)
    node_versions = list(server_config.get(VALID_NODE_VERSIONS_KEY) or [])
    moves_control_plane = control_plane_moves(target_text, cluster.get(CURRENT_MASTER_VERSION_KEY), roster)
    builds = target_builds(target_text, roster, node_versions, moves_control_plane)
    if builds:
        return builds, None, None
    note = SHORT_TARGET_NO_BUILDS_NOTE if moves_control_plane else SHORT_TARGET_NO_BUILDS_POOL_MOVE_NOTE
    return None, note.format(target=target_text, channel=channel_label, location=location), None


def _grade(version, specs) -> str | None:
    """How an issue's specs read a version: None when none matches, GRADE_OPEN when every matching
    spec has no upper bound, GRADE_RISK otherwise (a bounded spec that matches names a fix)."""
    matched = [spec for spec in specs if version_matches(version, spec)]
    if not matched:
        return None
    return GRADE_OPEN if all(spec_is_open(spec) for spec in matched) else GRADE_RISK


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


def _issue_finding(kind: str, issue: dict, version: str, tag: str) -> dict:
    return {"kind": kind, "issue": issue.get(ISSUE_ID_KEY), "summary": issue.get(ISSUE_SUMMARY_KEY), "version": version, "addon": issue.get(ISSUE_ADDON_KEY) or None, "tag": tag, "fixed_in": issue.get(ISSUE_FIXED_IN_KEY), "url": issue.get(ISSUE_URL_KEY)}


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
        out["unknown"].append(GENERATED_UNPARSABLE_REASON.format(name=TABLE_NAME, generated=generated_text))
        return out
    daemonset_error = read_error(read, DAEMONSET_READ_KIND)
    if daemonset_error:
        out["unknown"].append(DAEMONSETS_NOT_READ_REASON.format(error=daemonset_error))
    addons = context.get(CONTEXT_ADDONS) if isinstance(context.get(CONTEXT_ADDONS), list) else inventory(read)  # the report inventories once per member and hands it in
    at = context[CONTEXT_AT]
    age_days = (at.date() - generated).days
    if age_days > STALE_AFTER_DAYS:
        out["risks"].append({"kind": KIND_STALE_AGE, "generated": generated_text, "age_days": age_days, "limit_days": STALE_AFTER_DAYS})
    master = cluster.get("currentMasterVersion")
    target_text = context.get(CONTEXT_TARGET_TEXT)
    builds, builds_note, builds_unknown = target_candidates(target_text, cluster, context)
    if builds_unknown:
        out["unknown"].append(builds_unknown)
    matches = 0
    open_issues = {master: [], target_text: []}  # the note-graded matches, per version
    partial_notes = []
    for issue in table.get(TABLE_ISSUES_KEY) or []:
        if not isinstance(issue, dict):
            continue
        tag = addon_present(issue, addons)
        if tag is None:
            continue
        specs = issue.get(ISSUE_VERSIONS_KEY) or []
        for kind, version in ((KIND_RUNNING, master), (KIND_TARGET, target_text)):
            if not version or (kind == KIND_TARGET and version == master):
                continue
            if kind == KIND_TARGET and builds is None:
                continue  # a build-less target with no listed build to grade: noted, or filed as unknown, once
            candidates = builds if kind == KIND_TARGET else [version]
            graded = [_grade(build, specs) for build in candidates]
            affected = [build for build, grade in zip(candidates, graded) if grade]
            if not affected:
                continue
            if len(affected) < len(graded):  # a build-less target, some of whose builds the issue affects
                partial_notes.append(PARTIAL_TARGET_NOTE_FORMAT.format(target=version, n=len(graded), builds=TARGET_BUILDS_SEPARATOR.join(candidates), id=issue.get(ISSUE_ID_KEY), summary=issue.get(ISSUE_SUMMARY_KEY), affected=TARGET_BUILDS_SEPARATOR.join(affected), url=issue.get(ISSUE_URL_KEY)))
                continue
            if GRADE_RISK not in graded:  # every build matched through open specs alone
                open_issues[version].append(str(issue.get(ISSUE_ID_KEY)))
                continue
            out["risks"].append(_issue_finding(kind, issue, version, tag))
            matches += 1
    open_count = sum(len(ids) for ids in open_issues.values())
    out["notes"].append(NOTE_FORMAT.format(addons=len(addons), namespaces=LIST_SEPARATOR.join(ADDON_NAMESPACES), matches=matches, open=open_count, master=master or NO_MASTER, target=target_text or NO_TARGET, generated=generated_text, issues=len(table.get(TABLE_ISSUES_KEY) or []), newest=table.get(TABLE_NEWEST_VERSION_KEY) or NO_NEWEST))
    for version, ids in open_issues.items():
        if ids:
            out["notes"].append(OPEN_NOTE_FORMAT.format(n=len(ids), version=version, ids=OPEN_IDS_SEPARATOR.join(ids), url=table.get(TABLE_SOURCE_KEY)))
    out["notes"].extend(partial_notes)
    if builds_note:
        out["notes"].append(builds_note)
    return out


def describe(finding: dict) -> str:
    kind = finding.get("kind")
    if kind == KIND_STALE_AGE:
        return STALE_AGE_FORMAT.format(generated=finding["generated"], age=finding["age_days"], limit=finding["limit_days"])
    addon_text = WITH_ADDON_FORMAT.format(addon=finding["addon"], tag=finding["tag"]) if finding.get("addon") else NO_ADDON_TEXT
    no_fix = str(finding.get("fixed_in") or "") in NO_FIX_VALUES or not finding.get("fixed_in")
    fix_text = NO_FIX_TEXT if no_fix else FIX_TEXT_FORMAT.format(fixed_in=finding["fixed_in"])
    template = TARGET_FORMAT if kind == KIND_TARGET else RUNNING_FORMAT
    return template.format(id=finding["issue"], summary=finding["summary"], version=finding["version"], addon_text=addon_text, fix_text=fix_text, advice=TARGET_ADVICE_NO_FIX if no_fix else TARGET_ADVICE, url=finding["url"])
