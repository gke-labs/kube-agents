#!/usr/bin/env python3
"""Generate the documentation tables that mirror machine-readable sources.

Several reference tables in this repository restate information that already
exists in a file the code reads: the cron schedule lives in ``jobs.json``, the
skill catalogue lives in each skill's frontmatter, and the provisioning steps
live in the scripts themselves. Maintaining those tables by hand guarantees they
drift. This script regenerates them from the source of truth instead.

There are two kinds of target. Most are a *region* spliced into a hand-written
document (``BLOCKS``). One is a *whole file* written verbatim from its
generator (``FILES``): ``docs/family-roster.txt`` carries no markers, has no
hand-written part, and is replaced in full on every run.

Each generated region is delimited in its target file by::

    <!-- BEGIN GENERATED: <block-id> -->
    <!-- END GENERATED: <block-id> -->

or, in ``.mdx`` files (where MDX rejects HTML comments), by::

    {/* BEGIN GENERATED: <block-id> */}
    {/* END GENERATED: <block-id> */}

Everything outside those markers is hand-written and is never touched. A
whole-file target has no such boundary — nothing in it is hand-written.

Usage::

    python3 scripts/generate_docs.py            # rewrite the generated targets
    python3 scripts/generate_docs.py --check    # exit 1 if anything is stale

``--check`` is what CI runs: if regenerating would change a file, the committed
documentation no longer matches its source.

Standard library only, deliberately -- this must run in CI and in a bare clone
without installing anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The family roster is derived from the same inventory globs the map checker
# reads, so the two cannot disagree about what a family contains. Both modules
# live in scripts/, which is sys.path[0] when either is run as a script.
import check_docs_map

REPO = Path(__file__).resolve().parent.parent

# Two rosters, two profiles. Cron ticking is a property of a running gateway
# and only the `default` (Planning Agent) profile has one, so its roster is the
# only store the gateway thread advances — and it carries `profile-cron-tick`,
# which runs `hermes cron tick` against every named profile with work due. That
# is what makes the Platform Agent's own roster live, so the governance
# watchdogs sit there and run with that profile's persona and toolsets. Both
# files feed the page; a job documented from one roster alone is a job half the
# fleet cannot find.
CRON_ROSTERS = (
    ("Planning Agent", REPO / "agents/chat/defaults/cron/jobs.json"),
    ("Platform Agent", REPO / "agents/platform/cron/jobs.json"),
)
SKILLS_DIR = REPO / "agents/platform/skills"
CLUSTER_SKILLS_DIR = REPO / "agents/cluster/skills"
IMAGES_JSON = REPO / "images.json"

CRON_PAGE = REPO / "docs/site/src/content/docs/reference/cron-jobs.md"
SKILLS_PAGE = REPO / "docs/site/src/content/docs/skills/index.mdx"
IMAGES_PAGE = REPO / "docs/site/src/content/docs/deploy/docker-images.md"
ROSTER_FILE = REPO / "docs/family-roster.txt"

GITHUB_BLOB = "https://github.com/gke-labs/kube-agents/blob/main"

# Editorial grouping for the Platform Agent skill catalogue. Skills missing from
# this map are emitted under "Other" rather than dropped, so a new skill shows up
# as a diff in `--check` instead of silently vanishing from the catalogue. Cluster
# Agent skills are not listed here — they are grouped by persona, not by area (see
# CLUSTER_SKILL_GROUP).
SKILL_GROUPS: dict[str, list[str]] = {
    "Cluster lifecycle": [
        "cluster-agent-lifecycle",
        "gke-cluster-creation",
        "gke-multitenancy",
        "manage-cluster",
    ],
    "Workloads": [
        "gke-app-onboarding",
        "gke-batch-hpc",
        "gke-workload-scaling",
        "gke-workload-security",
        "gke-workload-troubleshooting",
        "workload-rebalancing",
    ],
    "Cost and capacity": [
        "gce-compute-fleet-audit",
        "gke-cluster-autoscaler",
        "gke-compute-classes",
        "gke-cost-analysis",
        "gke-cost-optimization",
        "gke-productionize",
    ],
    "Security and compliance": [
        "gke-backup-dr",
        "gke-platform-security",
    ],
    "Networking and storage": [
        "gcp-networking-fabric-audit",
        "gke-networking",
        "gke-service-networking",
        "gke-storage",
    ],
    "AI and inference": [
        "gke-ai-troubleshooting-handle-disruption-gpu-tpu",
        "gke-ai-troubleshooting-jobset-interruption",
        "gke-ai-troubleshooting-tpu-vbar-oom",
        "gke-golden-path",
        "gke-inference",
        "gke-tpu-dynamic-slices-monitoring",
        "gke-tpu-metrics-monitoring",
    ],
    "Observability": [
        "gke-basics",
        "gke-observability",
        "kube-agents-observability",
    ],
    "Reliability": [
        "gke-reliability",
        "gke-upgrades",
    ],
    "Manifests and remediation": [
        "gke-manifest-generation",
        "submit-suggestion",
    ],
    "Meta": [
        "fleet-audit",
        "github-issue-resolver",
        "inspect-repository",
        "pr-conversation",
        "version-control",
    ],
}

# Cluster Agent skills are single-cluster runtime debugging/operations procedures
# scaffolded into every per-cluster profile. They are listed under one heading
# rather than folded into the groups above: what distinguishes them to a reader is
# which persona runs them, not which area they cover.
CLUSTER_SKILL_GROUP = "Cluster Agent (per-cluster runtime)"

CRON_CADENCE = {
    "40 5 * * *": "Daily 05:40",
    "20 6 * * *": "Daily 06:20",
    "50 6 * * *": "Daily 06:50",
    "50 8 * * *": "Daily 08:50",
    "0 9 * * *": "Daily 09:00",
    "20 9 * * *": "Daily 09:20",
    "0 10 * * *": "Daily 10:00",
    "0 11 * * *": "Daily 11:00",
    "0 12 * * *": "Daily 12:00",
    "0 * * * *": "Hourly",
    "11 * * * *": "Hourly at :11",
    "*/10 * * * *": "Every 10 minutes",
    "*/30 * * * *": "Every 30 minutes",
    "0 21 * * 1-5": "Weekdays 21:00",
    "0 9 * * 0": "Weekly, Sunday 09:00",
    "0 10 * * 0": "Weekly, Sunday 10:00",
    "20 7 * * 1": "Weekly, Monday 07:20",
    "50 7 * * 1": "Weekly, Monday 07:50",
    "20 8 * * 1": "Weekly, Monday 08:20",
    "0 9 1 * *": "Monthly, 1st 09:00",
}


def md_escape(text: str) -> str:
    """Make a string safe to drop into a Markdown table cell.

    Angle brackets are escaped because the skill catalogue is an `.mdx` page:
    MDX reads a bare `<name>` in a description as an unclosed JSX tag and fails
    the site build. `&lt;`/`&gt;` render as literal brackets in plain Markdown
    too, so this is safe for the `.md` targets as well.
    """
    return (
        text.replace("|", "\\|")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
        .strip()
    )


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #


def gen_cron_jobs() -> str:
    rows = [
        "| ID | Profile | Schedule | Cadence | Enabled | Runs |",
        "| -- | ------- | -------- | ------- | :-----: | ---- |",
    ]
    for profile, path in CRON_ROSTERS:
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data
        for job in jobs:
            # A disabled entry on the Platform Agent's roster is a tombstone —
            # an id on its way out, shipped switched off for a release because
            # the start-up merge never prunes, then deleted and named in
            # `--cron-retire` (see `retire_cron_jobs` in `profile_scaffold.py`).
            # The roster carries none today; the filter stays because listing
            # the next one would document a job that cannot run as though an
            # operator could still reach it.
            if profile == "Platform Agent" and not job.get("enabled"):
                continue
            # An interval job has no `expr` — the roster carries `minutes` and a
            # rendered `display` instead. Falling back to `display` keeps those
            # rows from printing an empty cell in both schedule columns.
            schedule = job.get("schedule", {})
            expr = schedule.get("expr", "")
            shown = expr or schedule.get("display", "")
            # The governance jobs put the work in `prompt`; the onboarding and
            # reconcile jobs are self-contained scripts and leave it empty.
            # Naming the script is what stops those rows reading as a job that
            # does nothing.
            prompt = md_escape(job.get("prompt", ""))
            if len(prompt) > 110:
                prompt = prompt[:107].rstrip() + "..."
            if not prompt:
                prompt = f"`{job.get('script', '')}`" if job.get("script") else ""
            rows.append(
                "| `{id}` | {profile} | `{shown}` | {cadence} | {enabled} | "
                "{prompt} |".format(
                    id=job.get("id", ""),
                    profile=profile,
                    shown=shown,
                    # Only a cron expression gets a gloss; an interval schedule
                    # already reads as human text and would just repeat itself.
                    cadence=CRON_CADENCE.get(expr, "—"),
                    enabled="yes" if job.get("enabled") else "no",
                    prompt=prompt,
                )
            )
    return "\n".join(rows)


def read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fields: dict[str, str] = {}
    key: str | None = None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if km:
            key = km.group(1)
            value = km.group(2).strip()
            # Block scalar indicators start a multi-line value on the next line.
            if value in (">", ">-", ">+", "|", "|-", "|+"):
                value = ""
            fields[key] = value
        elif key and line[:1] in (" ", "\t") and line.strip():
            # Continuation line of a multi-line value.
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    # Strip quotes only once values are fully assembled: a quoted value that
    # wraps across lines carries its closing quote on the last line.
    return {k: v.strip("\"'") for k, v in fields.items()}


def _read_skills(skills_dir: Path) -> dict[str, str]:
    """Return {skill name: description} for every SKILL.md under skills_dir."""
    skills: dict[str, str] = {}
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        fm = read_frontmatter(skill_md)
        name = fm.get("name") or skill_md.parent.name
        skills[name] = fm.get("description", "")
    return skills


def gen_skill_catalog() -> str:
    skills = _read_skills(SKILLS_DIR)
    cluster_skills = _read_skills(CLUSTER_SKILLS_DIR)

    grouped = {g: [] for g in SKILL_GROUPS}
    grouped["Other"] = []
    for name in sorted(skills):
        group = next((g for g, members in SKILL_GROUPS.items() if name in members), "Other")
        grouped[group].append(name)
    # Every Cluster Agent skill goes in one persona-scoped group, appended last so
    # the Platform Agent's editorial order above is untouched.
    grouped[CLUSTER_SKILL_GROUP] = sorted(cluster_skills)

    def source_dir(group: str) -> str:
        return (
            "agents/cluster/skills"
            if group == CLUSTER_SKILL_GROUP
            else "agents/platform/skills"
        )

    descriptions = {**skills, **cluster_skills}

    out: list[str] = []
    for group, names in grouped.items():
        if not names:
            continue
        out.append(f"### {group}\n")
        out.append("| Skill | Description |")
        out.append("| ----- | ----------- |")
        for name in names:
            link = f"{GITHUB_BLOB}/{source_dir(group)}/{name}/SKILL.md"
            out.append(f"| [`{name}`]({link}) | {md_escape(descriptions[name])} |")
        out.append("")
    return "\n".join(out).rstrip()


ORIGIN_SECTIONS: list[tuple[str, str, str]] = [
    (
        "first-party",
        "Built and published by this repo",
        "Tagged with the release version; `:latest` on every push to `main`.",
    ),
    (
        "third-party",
        "Pulled by an install, built elsewhere",
        "Pinned here so `make mirror-images` and the install ask for the same version.",
    ),
    (
        "build-time",
        "Base images",
        "Needed only to rebuild the images above from source, not to run an install. "
        "Each is a build arg on its Dockerfile, so a mirrored rebuild passes the copy's reference.",
    ),
]


def image_pin(image: dict) -> str:
    """Describe where an entry's tag comes from, for the Pin column."""
    if "tag" in image:
        return f"`{image['tag']}`"
    if "tagFrom" in image:
        src = image["tagFrom"]
        return f"`{src['key']}` in [`{src['file']}`]({GITHUB_BLOB}/{src['file']})"
    if image.get("tagPolicy") == "release":
        return "release tag"
    return "—"


def gen_container_images() -> str:
    images = json.loads(IMAGES_JSON.read_text(encoding="utf-8"))["images"]

    out: list[str] = []
    for origin, heading, blurb in ORIGIN_SECTIONS:
        entries = [i for i in images if i.get("origin") == origin]
        if not entries:
            continue
        out.append(f"### {heading}\n")
        out.append(f"{blurb}\n")
        out.append("| Image | Upstream reference | Pin | Override | Pulled by |")
        out.append("| ----- | ------------------ | --- | -------- | --------- |")
        for image in entries:
            override = image.get("override") or image.get("buildArg")
            out.append(
                "| `{name}` | `{repository}` | {pin} | {override} | {pulled_by} |".format(
                    name=image["name"],
                    repository=image["repository"],
                    pin=image_pin(image),
                    override=f"`{override}`" if override else "—",
                    pulled_by=md_escape(image.get("pulledBy", "")),
                )
            )
        out.append("")

    return "\n".join(out).rstrip()


def gen_family_roster() -> str:
    """Return the whole contents of the collapsed-family roster file.

    The globs are read out of the map's own inventory rather than listed here,
    so a new family row is rostered the moment it is added. The extraction is
    ``check_docs_map.family_globs`` — the same reader the coverage check uses —
    so the roster cannot cover a different set of rows than the checker
    honours.
    """
    files = check_docs_map.tracked_docs()
    text = check_docs_map.MAP.read_text(encoding="utf-8")

    out = [line.rstrip() for line in ROSTER_HEADER.strip("\n").splitlines()]
    for glob in sorted(check_docs_map.family_globs(text)):
        out.append("")
        out.append(glob)
        for member in sorted(check_docs_map.matches(glob, files)):
            out.append(f"  {member}")
    return "\n".join(out) + "\n"


ROSTER_HEADER = """
# Collapsed-family roster -- generated, do not edit by hand.
# Regenerate with: make docs-generate
#
# The documentation map (docs/README.md, section 4) collapses uniform families
# of documents into a single inventory row whose path cell is a glob. Neither
# check in check_docs_map.py can see a file DELETED from inside such a family:
# the glob still matches the survivors, so the map still reads true while it
# silently describes a document that no longer exists. This file is the
# snapshot that makes the deletion visible -- `make docs-check` fails until it
# is regenerated, and the removed path then shows up as a line in the pull
# request's diff for a reviewer to notice.
#
# It lives outside the map on purpose. The map is this repository's most
# merge-conflict-prone file and a family row deliberately characterises its
# family rather than enumerating it; per-file churn belongs here instead. A
# sorted list still collides when two branches insert into the same gap, so
# .gitattributes hands this file to git's union merge driver -- see the comment
# there. Anything union merge gets wrong shows up as `make docs-check` failing
# on a stale roster, and `make docs-generate` writes the correct file.
"""

BLOCKS = {
    "cron-jobs": (CRON_PAGE, gen_cron_jobs),
    "skill-catalog": (SKILLS_PAGE, gen_skill_catalog),
    "container-images": (IMAGES_PAGE, gen_container_images),
}

# Generated artifacts that are a whole file rather than a region inside a
# hand-written document, written verbatim from their generator.
FILES = {
    "family-roster": (ROSTER_FILE, gen_family_roster),
}


# --------------------------------------------------------------------------- #
# Marker handling
# --------------------------------------------------------------------------- #


def region_markers(path: Path, block_id: str) -> tuple[str, str, str]:
    """Return (begin, end, notice) markers in the comment syntax the file allows.

    MDX rejects HTML comments, so ``.mdx`` files use JSX-style comments.
    """
    if path.suffix == ".mdx":
        return (
            f"{{/* BEGIN GENERATED: {block_id} */}}",
            f"{{/* END GENERATED: {block_id} */}}",
            "{/* Regenerate with: make docs-generate -- do not edit by hand. */}",
        )
    return (
        f"<!-- BEGIN GENERATED: {block_id} -->",
        f"<!-- END GENERATED: {block_id} -->",
        "<!-- Regenerate with: make docs-generate -- do not edit by hand. -->",
    )


def splice(path: Path, block_id: str, body: str) -> tuple[bool, str]:
    """Return (changed, new_text) with the generated region replaced."""
    text = path.read_text(encoding="utf-8")
    begin, end, notice = region_markers(path, block_id)
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end),
        re.S,
    )
    if not pattern.search(text):
        raise SystemExit(
            f"{path.relative_to(REPO)}: missing markers for block '{block_id}'.\n"
            f"  Add:\n    {begin}\n    {end}"
        )
    # Prettier would reflow the compact generated tables, which then fail
    # --check; fence the region off for .md files (the Prettier CI job does
    # not cover .mdx, and MDX rejects HTML comments anyway).
    if path.suffix == ".mdx":
        guard_open = guard_close = ""
    else:
        guard_open = "<!-- prettier-ignore-start -->\n"
        guard_close = "<!-- prettier-ignore-end -->\n"
    replacement = (
        f"{begin}\n"
        f"{notice}\n"
        f"{guard_open}\n"
        f"{body}\n\n"
        f"{guard_close}"
        f"{end}"
    )
    new_text = pattern.sub(lambda _: replacement, text, count=1)
    return new_text != text, new_text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any generated region is out of date",
    )
    args = ap.parse_args()

    # (path, full new contents, changed, block id) for both kinds of target.
    targets: list[tuple[Path, str, bool, str]] = []
    for block_id, (path, generator) in BLOCKS.items():
        changed, new_text = splice(path, block_id, generator())
        targets.append((path, new_text, changed, block_id))
    for block_id, (path, generator) in FILES.items():
        new_text = generator()
        old_text = path.read_text(encoding="utf-8") if path.exists() else None
        targets.append((path, new_text, new_text != old_text, block_id))

    stale: list[str] = []
    for path, new_text, changed, block_id in targets:
        rel = path.relative_to(REPO)
        if not changed:
            print(f"  ok       {rel} [{block_id}]")
            continue
        if args.check:
            stale.append(f"{rel} [{block_id}]")
            print(f"  STALE    {rel} [{block_id}]")
        else:
            path.write_text(new_text, encoding="utf-8")
            print(f"  updated  {rel} [{block_id}]")

    if stale:
        print(
            "\nGenerated documentation is out of date with its source.\n"
            "Run `make docs-generate` and commit the result.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
