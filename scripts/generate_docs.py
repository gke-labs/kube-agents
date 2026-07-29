#!/usr/bin/env python3
"""Generate the documentation tables that mirror machine-readable sources.

Several reference tables in this repository restate information that already
exists in a file the code reads: the cron schedule lives in ``jobs.json``, the
skill catalogue lives in each skill's frontmatter, and the provisioning steps
live in the scripts themselves. Maintaining those tables by hand guarantees they
drift. This script regenerates them from the source of truth instead.

Each generated region is delimited in its target file by::

    <!-- BEGIN GENERATED: <block-id> -->
    <!-- END GENERATED: <block-id> -->

or, in ``.mdx`` files (where MDX rejects HTML comments), by::

    {/* BEGIN GENERATED: <block-id> */}
    {/* END GENERATED: <block-id> */}

Everything outside those markers is hand-written and is never touched.

Usage::

    python3 scripts/generate_docs.py            # rewrite the generated regions
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

REPO = Path(__file__).resolve().parent.parent

CRON_JOBS = REPO / "agents/platform/cron/jobs.json"
SKILLS_DIR = REPO / "agents/platform/skills"
CLUSTER_SKILLS_DIR = REPO / "agents/cluster/skills"
SCRIPTS_DIR = REPO / "k8s-operator/scripts"

CRON_PAGE = REPO / "docs/site/src/content/docs/reference/cron-jobs.md"
SKILLS_PAGE = REPO / "docs/site/src/content/docs/skills/index.mdx"
SCRIPTS_PAGE = SCRIPTS_DIR / "README.md"

GITHUB_BLOB = "https://github.com/gke-labs/kube-agents/blob/main"

# Editorial grouping for the Platform Agent skill catalogue. Skills missing from
# this map are emitted under "Other" rather than dropped, so a new skill shows up
# as a diff in `--check` instead of silently vanishing from the catalogue. Cluster
# Agent skills are not listed here — they are grouped by persona, not by area (see
# CLUSTER_SKILL_GROUP).
SKILL_GROUPS: dict[str, list[str]] = {
    "Cluster lifecycle": [
        "cluster-agent-lifecycle",
        "gke-cluster-creator",
        "gke-cluster-lifecycle",
        "gke-multi-tenancy",
        "manage-cluster",
    ],
    "Workloads": [
        "gke-app-onboarding",
        "workload-rebalancing",
    ],
    "Cost and capacity": [
        "gke-cost-analysis",
        "gke-compute-classes",
        "gke-productionize",
    ],
    "Security and compliance": ["gke-backup-dr"],
    "Networking and storage": ["gke-networking-edge"],
    "AI and inference": ["gke-inference-quickstart"],
    "Observability": ["kube-agents-observability"],
    "Manifests and remediation": ["gke-manifest-generation", "submit-suggestion"],
    "Meta": ["github-issue-resolver"],
}

# Cluster Agent skills are single-cluster runtime debugging/operations procedures
# scaffolded into every per-cluster profile. They are listed under one heading
# rather than folded into the groups above: what distinguishes them to a reader is
# which persona runs them, not which area they cover.
CLUSTER_SKILL_GROUP = "Cluster Agent (per-cluster runtime)"

CRON_CADENCE = {
    "0 9 * * *": "Daily 09:00",
    "0 10 * * *": "Daily 10:00",
    "0 11 * * *": "Daily 11:00",
    "0 12 * * *": "Daily 12:00",
    "0 * * * *": "Hourly",
    "*/30 * * * *": "Every 30 minutes",
    "0 9 * * 0": "Weekly, Sunday 09:00",
    "0 10 * * 0": "Weekly, Sunday 10:00",
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
    data = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
    jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data

    rows = [
        "| ID | Schedule | Cadence | Enabled | Prompt |",
        "| -- | -------- | ------- | :-----: | ------ |",
    ]
    for job in jobs:
        expr = job.get("schedule", {}).get("expr", "")
        prompt = md_escape(job.get("prompt", ""))
        if len(prompt) > 110:
            prompt = prompt[:107].rstrip() + "..."
        rows.append(
            "| `{id}` | `{expr}` | {cadence} | {enabled} | {prompt} |".format(
                id=job.get("id", ""),
                expr=expr,
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


def parse_script_banner(path: Path) -> tuple[str, str]:
    """Return (title, description) from a script's comment banner."""
    lines = path.read_text(encoding="utf-8").splitlines()
    title, desc = "", []
    rules = [i for i, line in enumerate(lines[:24]) if set(line.strip("# ")) == {"="}]
    if len(rules) >= 2:
        between = lines[rules[0] + 1 : rules[1]]
        if between:
            title = between[0].lstrip("# ").strip()
            title = re.sub(r"^[^\w]*Step \d+:\s*", "", title).strip()
    if len(rules) >= 3:
        for line in lines[rules[1] + 1 : rules[2]]:
            desc.append(line.lstrip("#").strip())
    return title, " ".join(d for d in desc if d)


def gen_provisioning_steps() -> str:
    out: list[str] = []
    for kind, heading in (("provision", "Provisioning"), ("teardown", "Teardown")):
        out.append(f"### {heading} steps\n")
        out.append("| # | Script | What it does |")
        out.append("| :-: | ------ | ------------ |")
        for path in sorted(SCRIPTS_DIR.glob(f"{kind}_[0-9][0-9]_*.sh")):
            num = int(re.search(r"_(\d{2})_", path.name).group(1))
            title, desc = parse_script_banner(path)
            summary = md_escape(desc or title)
            out.append(f"| {num} | [`{path.name}`]({path.name}) | **{md_escape(title)}** — {summary} |")
        out.append("")
    return "\n".join(out).rstrip()


BLOCKS = {
    "cron-jobs": (CRON_PAGE, gen_cron_jobs),
    "skill-catalog": (SKILLS_PAGE, gen_skill_catalog),
    "provisioning-steps": (SCRIPTS_PAGE, gen_provisioning_steps),
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

    stale: list[str] = []
    for block_id, (path, generator) in BLOCKS.items():
        changed, new_text = splice(path, block_id, generator())
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
