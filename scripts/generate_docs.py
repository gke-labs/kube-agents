#!/usr/bin/env python3
"""Generate the documentation tables that mirror machine-readable sources.

Several reference tables in this repository restate information that already
exists in a file the code reads: the cron schedule lives in ``jobs.json``, the
skill catalogue lives in each skill's frontmatter, and the provisioning steps
live in the scripts themselves. Maintaining those tables by hand guarantees they
drift. This script regenerates them from the source of truth instead.

Each target is a *region* spliced into one or more hand-written documents
(``BLOCKS``); a region that several pages carry is rendered once and spliced
into each. A region is delimited in its target file by::

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
    python3 scripts/generate_docs.py crd-fields-agentplugin   # one block only
    python3 scripts/generate_docs.py --block-prefix crd-fields-  # one family

``--check`` is what CI runs: if regenerating would change a file, the committed
documentation no longer matches its source. Naming blocks, or a prefix,
restricts a run to those regions; ``make -C k8s-operator manifests`` uses the
prefix to refresh the CRD field pages beside the CRD YAML.

Standard library only, deliberately -- this must run in CI and in a bare clone
without installing anything. The one tool it calls out to is ``go``: the CRD
field pages are read from the operator's Go API types by
``scripts/gen_crd_docs.go``, itself standard library and run with the
installed toolchain. Without ``go`` on PATH a write run skips those pages and
says so, so the other regions still refresh; ``--check`` fails, since a check
that cannot read the pages cannot vouch for them.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from generate_sop_geography import SOP_REF_RE

REPO = Path(__file__).resolve().parent.parent

# Two rosters, two profiles. Cron ticking is a property of a running gateway
# and only the `default` (Planning Agent) profile has one, so its roster is the
# only store the gateway thread advances — and it carries `profile-cron-tick`,
# which runs `hermes cron tick` against every named profile with work due. That
# is what makes the Platform Agent's own roster live, so the governance
# watchdogs sit there and run with that profile's persona and toolsets. Both
# files feed the page; a job documented from one roster alone is a job half the
# fleet cannot find.
PLATFORM_CRON_ROSTER = REPO / "agents/platform/cron/jobs.json"
CRON_ROSTERS = (
    ("Planning Agent", REPO / "agents/chat/defaults/cron/jobs.json"),
    ("Platform Agent", PLATFORM_CRON_ROSTER),
)
# The one roster entry the site renders in full, as the worked example of the
# job schema. It is generated rather than pasted because the pasted copies went
# stale every time the prompt was reworded, and a guard that compared them to
# the roster was the cost of keeping them by hand.
EXAMPLE_JOB_ID = "compliance-audit"
# The roster file's own indentation, so the example reads as the file spells it.
EXAMPLE_INDENT = 2
# How the helper-jobs table names a roster entry's kind: a `no_agent` script
# that runs no model, a prompt job, or a prompt job with a pre-run script whose
# output goes above the prompt (the weekly brief is the one such entry).
HELPER_KIND_SCRIPT = "script, `{script}`"
HELPER_KIND_PROMPT = "prompt"
HELPER_KIND_PROMPT_AFTER_SCRIPT = "prompt, after `{script}`"
HELPER_JOBS_HEADER = (
    "| Job | Type | Cadence | Name |",
    "| --- | ---- | ------- | ---- |",
)
# The governance page's audit matrix: the SOP each prompt names, the watchdog
# that runs it, its cadence and its name, all read from the roster.
GOVERNANCE_AUDITS_HEADER = (
    "| SOP file | Watchdog | Cadence | Audit |",
    "| -------- | -------- | ------- | ----- |",
)
# What the reference page prints in its cadence column when an expression has
# no gloss: that page shows the expression in its own column already.
NO_CADENCE_GLOSS = "—"
SKILLS_DIR = REPO / "agents/platform/skills"
CLUSTER_SKILLS_DIR = REPO / "agents/cluster/skills"
TSE_SKILLS_DIR = REPO / "agents/tse/skills"
IMAGES_JSON = REPO / "images.json"

CRON_PAGE = REPO / "docs/site/src/content/docs/reference/cron-jobs.md"
WATCHDOGS_PAGE = REPO / "docs/site/src/content/docs/concepts/autonomous-watchdogs.md"
GOVERNANCE_PAGE = REPO / "docs/site/src/content/docs/concepts/governance-sops.md"
SKILLS_CONCEPT_PAGE = REPO / "docs/site/src/content/docs/concepts/skills.md"
SKILLS_PAGE = REPO / "docs/site/src/content/docs/skills/index.mdx"
IMAGES_PAGE = REPO / "docs/site/src/content/docs/deploy/docker-images.md"
README = REPO / "README.md"
# A governance audit is a Platform Agent job that carries the fleet-audit skill;
# that skill's tests hold this set equal to its audit catalogue. The prompt names
# the audit's SOP by a path relative to the profile home.
AUDIT_SKILL = "fleet-audit"
PLATFORM_HOME = "agents/platform"

GITHUB_BLOB = "https://github.com/gke-labs/kube-agents/blob/main"

# The CRD field reference pages, one per kind, read from the operator's API
# types by the Go program beside this script. GOTOOLCHAIN=local runs the
# installed toolchain whatever a go.mod might ask for: the program is standard
# library on an API every supported Go has, so no download starts in CI.
CRD_API_DIR = REPO / "k8s-operator/api/v1alpha1"
CRD_DOCS_GENERATOR = REPO / "scripts/gen_crd_docs.go"
GO_BINARY = "go"
GO_RUN_ENV = {"GOTOOLCHAIN": "local"}
OPERATOR_DOCS_DIR = REPO / "docs/site/src/content/docs/operator"
CRD_KINDS: dict[str, Path] = {
    "PlatformAgent": OPERATOR_DOCS_DIR / "platformagent-fields.md",
    "AgentProfile": OPERATOR_DOCS_DIR / "agentprofile-fields.md",
    "AgentPlugin": OPERATOR_DOCS_DIR / "agentplugin-fields.md",
}
# The narrative guide each field page points its reader at, as a site route.
CRD_GUIDES = {
    "PlatformAgent": "/kube-agents/operator/platformagent-crd/",
    "AgentProfile": "/kube-agents/operator/agentprofile-crd/",
    "AgentPlugin": "/kube-agents/operator/agentplugin-crd/",
}
CRD_BLOCK_PREFIX = "crd-fields-"
# The paragraph above the tables, rendered once here rather than kept by hand
# on each page, so the three pages cannot drift apart. It states what the
# tables leave out: standard metadata, the inside of upstream Kubernetes
# types, every paragraph of a field's documentation after the first other
# than its deprecation notice, and the CEL rules the Constraints column does
# not carry.
CRD_GROUP = "kubeagents.x-k8s.io"
CRD_INTRO = (
    "Every field of the `{kind}` resource's `spec` and `status`, generated from the "
    "operator's Go API types. The standard Kubernetes `apiVersion`, `kind` and "
    "`metadata` are not listed. A field whose type comes from the Kubernetes API "
    "(`corev1`, `metav1`, `networkingv1`, `resource.Quantity`) names that type and is "
    "not expanded; the Kubernetes API reference describes it. Paths read "
    "`parent.child`, with `[]` for each item of a list and `{{}}` for each value of a "
    "map. Each description is the opening paragraph of the field's documentation, "
    "plus its deprecation notice when it has one, and names other fields in their Go "
    "spelling (`MaxSessions` for `maxSessions`); `kubectl explain "
    "{resource}.spec.<field>` (`{resource}.status.<field>` for a row of the Status "
    "table) prints the whole text. The Constraints column lists the "
    "schema bounds on a field: allowed values, ranges, lengths and patterns. The API "
    "server also enforces CEL rules on these fields and on whole sections, which the "
    "column does not show; `kubectl get crd {resource}s.{group} -o yaml` lists them "
    "under `x-kubernetes-validations`, each with the message the API server returns "
    "when a value breaks it. For what the fields do together, read the "
    "[{kind} CRD]({guide}) guide."
)
# The two trees each page carries, with the heading each is rendered under.
CRD_TREES = (("spec", "Spec"), ("status", "Status"))
CRD_TABLE_HEADER = (
    "| Field | Type | Required | Default | Constraints | Description |",
    "| ----- | ---- | -------- | ------- | ----------- | ----------- |",
)
CRD_EMPTY_TREE = "No fields."
REQUIRED_YES = "yes"
REQUIRED_NO = "no"
CONSTRAINT_SEPARATOR = "; "
ENUM_MEMBER_SEPARATOR = ", "
# Entities for the brackets a `.md` page would otherwise read as a tag, and
# the escape GFM reads as a literal pipe inside a table cell.
LT_ENTITY = "&lt;"
GT_ENTITY = "&gt;"
PIPE_ESCAPE = "\\|"
# The validation markers the Constraints column shows: how each reads on the
# page, and the schema keyword controller-gen writes for it, which the parity
# test in test_gen_crd_docs.py reads back from the committed CRD YAML. One
# table for both, so a marker cannot render on a page that the test does not
# know. A marker on the elements of a collection goes through the same table
# under a prefix, `items:` for a list's items (controller-gen's own spelling)
# and `values:` for a map's values (the generator's, since controller-gen has
# none); a marker not listed here is shown as its own name, and the parity
# test fails on it until it is added.
CONSTRAINT_MARKERS: dict[str, tuple[str, str]] = {
    "Enum": ("one of", "enum"),
    "Minimum": ("min", "minimum"),
    "Maximum": ("max", "maximum"),
    "MinLength": ("min length", "minLength"),
    "MaxLength": ("max length", "maxLength"),
    "MinItems": ("min items", "minItems"),
    "MaxItems": ("max items", "maxItems"),
    "MinProperties": ("min properties", "minProperties"),
    "MaxProperties": ("max properties", "maxProperties"),
    "Pattern": ("pattern", "pattern"),
    "MultipleOf": ("multiple of", "multipleOf"),
    "Format": ("format", "format"),
}
# Markers whose keyword controller-gen also writes with no marker present:
# `format: int32`, `int64` or `byte` comes from the Go width of every such
# field. The schema therefore carries these where no row does, so the parity
# test holds a marker to its keyword, never the keyword to a marker.
SCHEMA_IMPLIED_MARKERS = frozenset({"Format"})
# Each collection prefix with the word the page puts before the constraint,
# and the schema keyword controller-gen writes the element's validation
# under, which the parity test reads back.
CONSTRAINT_PREFIXES: dict[str, tuple[str, str]] = {
    "items:": ("items", "items"),
    "values:": ("values", "additionalProperties"),
}
# controller-gen separates enum members with `;`.
ENUM_SEPARATOR = ";"
CODE_SPAN_RE = re.compile(r"`[^`]*`")

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
        "capacity-obtainability",
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
        "gke-workload-identity",
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
        "gke-alert-configuration",
        "gke-basics",
        "gke-observability",
        "kube-agents-observability",
    ],
    "Reliability": [
        "fleet-upgrade-verification",
        "gke-reliability",
        "gke-upgrades",
        "topology-drift",
        "upgrade-readiness-brief",
    ],
    "Manifests and remediation": [
        "gcp-config-connector",
        "gcp-terraform",
        "gitops-helm-chart",
        "gke-manifest-generation",
        "submit-suggestion",
    ],
    "Meta": [
        "fleet-audit",
        "fleet-audit-reports",
        "github-issue-resolver",
        "inspect-repository",
        "kube-agents-feedback",
        "pr-conversation",
        "version-control",
    ],
}

# Cluster Agent skills are single-cluster runtime debugging/operations procedures
# scaffolded into every per-cluster profile. They are listed under one heading
# rather than folded into the groups above: what distinguishes them to a reader is
# which persona runs them, not which area they cover.
CLUSTER_SKILL_GROUP = "Cluster Agent (per-cluster runtime)"
# The GKE Support Agent's skills ship in the image as the optional `tse`
# profile template, scaffolded when KUBE_AGENTS_ENABLED_PROFILES names it. One
# persona-scoped heading, after the Cluster Agent's, for the same reason.
TSE_SKILL_GROUP = "GKE Support Agent (tse)"
# The persona-scoped groups, in catalogue order after the Platform Agent's
# editorial groups, each with the directory its skills are read from and
# linked to. A new persona is one more row here.
PERSONA_SKILL_GROUPS: tuple[tuple[str, Path], ...] = (
    (CLUSTER_SKILL_GROUP, CLUSTER_SKILLS_DIR),
    (TSE_SKILL_GROUP, TSE_SKILLS_DIR),
)

CRON_CADENCE = {
    "40 5 * * *": "Daily 05:40",
    "20 6 * * *": "Daily 06:20",
    "50 6 * * *": "Daily 06:50",
    "50 8 * * *": "Daily 08:50",
    "45 7 * * *": "Daily 07:45",
    "0 8 * * *": "Daily 08:00",
    "10 10 * * *": "Daily 10:10",
    "0 9 * * *": "Daily 09:00",
    "20 9 * * *": "Daily 09:20",
    "0 10 * * *": "Daily 10:00",
    "0 11 * * *": "Daily 11:00",
    "0 12 * * *": "Daily 12:00",
    "35 12 * * *": "Daily 12:35",
    "0 13 * * *": "Daily 13:00",
    "0 * * * *": "Hourly",
    "11 * * * *": "Hourly at :11",
    "25 * * * *": "Hourly at :25",
    "*/10 * * * *": "Every 10 minutes",
    "*/30 * * * *": "Every 30 minutes",
    "0 21 * * 1-5": "Weekdays 21:00",
    "0 6 * * 6": "Weekly, Saturday 06:00",
    "0 7 * * 6": "Weekly, Saturday 07:00",
    "0 9 * * 0": "Weekly, Sunday 09:00",
    "0 10 * * 0": "Weekly, Sunday 10:00",
    "0 18 * * 0": "Weekly, Sunday 18:00",
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


def escape_cell(text: str) -> str:
    """Make prose with inline code safe for a Markdown table cell on a `.md` page.

    Pipes are escaped everywhere, since GFM reads one inside a code span as a
    column break too. Angle brackets are escaped only outside code spans: a
    bare `<name>` in prose is an HTML tag to the renderer and vanishes, while
    inside a code span `&lt;` would show as those four characters.
    """
    parts = CODE_SPAN_RE.split(text)
    spans = CODE_SPAN_RE.findall(text)
    out: list[str] = []
    for index, part in enumerate(parts):
        out.append(part.replace("<", LT_ENTITY).replace(">", GT_ENTITY))
        if index < len(spans):
            out.append(spans[index])
    return "".join(out).replace("|", PIPE_ESCAPE).replace("\n", " ").strip()


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #


def schedule_expr(schedule: dict) -> tuple[str, str]:
    """Return (expr, shown) for a roster entry's schedule.

    An interval job has no ``expr``: the roster carries ``minutes`` and a
    rendered ``display`` instead, so ``shown`` falls back to ``display`` and a
    row never prints an empty schedule.
    """
    expr = schedule.get("expr") or ""
    return expr, expr or schedule.get("display") or ""


def cadence_cell(schedule: dict, fallback: str | None = None) -> str:
    """The ``CRON_CADENCE`` gloss for a schedule, or what a page shows without one.

    Every generator that prints a cadence goes through here so a new schedule
    shape is handled once. ``fallback`` is for a page that already shows the
    expression beside the gloss; the others print the expression itself.
    """
    expr, shown = schedule_expr(schedule)
    gloss = CRON_CADENCE.get(expr)
    if gloss:
        return gloss
    return fallback if fallback is not None else f"`{shown}`"


def load_roster(path: Path) -> list[dict]:
    """Return the job entries of a cron roster, whichever shape the file takes.

    A roster is either a bare list of jobs or an object carrying them under
    ``jobs``. Both generators and the tests read through this one reader so
    the shape rule lives in one place.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["jobs"] if isinstance(data, dict) and "jobs" in data else data


def gen_cron_jobs() -> str:
    rows = [
        "| ID | Profile | Schedule | Cadence | Enabled | Runs |",
        "| -- | ------- | -------- | ------- | :-----: | ---- |",
    ]
    for profile, path in CRON_ROSTERS:
        for job in load_roster(path):
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
            _, shown = schedule_expr(schedule)
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
                    cadence=cadence_cell(schedule, NO_CADENCE_GLOSS),
                    enabled="yes" if job.get("enabled") else "no",
                    prompt=prompt,
                )
            )
    return "\n".join(rows)


def gen_cron_job_example(job_id: str = EXAMPLE_JOB_ID) -> str:
    """Render one Platform Agent roster entry as the fenced JSON the pages show.

    The entry is re-serialised rather than copied out of the file, so the
    example carries the roster's keys in the roster's order and nothing else;
    ``ensure_ascii=False`` keeps the prompt's em dash a dash rather than a
    ``\\u2014`` escape. An id the roster does not carry is a hard failure: a
    page that silently rendered nothing would read as a schema with no
    example, which is the drift this region exists to prevent.
    """
    job = next((j for j in load_roster(PLATFORM_CRON_ROSTER) if j.get("id") == job_id), None)
    if job is None:
        raise SystemExit(
            f"{PLATFORM_CRON_ROSTER.relative_to(REPO)}: no job with id '{job_id}'; "
            "the cron-job-example region has nothing to render."
        )
    rendered = json.dumps(job, indent=EXAMPLE_INDENT, ensure_ascii=False)
    return f"```json\n{rendered}\n```"


def is_audit(job: dict) -> bool:
    """Whether a roster entry is a governance audit: it carries the `fleet-audit` skill.

    The one predicate behind `helper_jobs()` and `governance_audits()`, so the
    two regions partition the enabled roster between them and cannot drift. A
    missing or `null` `skills` reads as no skills, as every other roster reader
    in the tree treats it.
    """
    return AUDIT_SKILL in (job.get("skills") or [])


def helper_jobs() -> list[dict]:
    """The enabled Platform Agent roster entries that are not fleet audits.

    An audit carries the `fleet-audit` skill; everything else on the roster is
    a helper: the pollers, the watches, the collectors and the weekly brief.
    A disabled entry is a tombstone on its way out (see `gen_cron_jobs`) and
    is left out for the same reason.
    """
    return [job for job in load_roster(PLATFORM_CRON_ROSTER) if job.get("enabled") and not is_audit(job)]


def helper_kind(job: dict) -> str:
    script = job.get("script", "")
    if job.get("no_agent"):
        return HELPER_KIND_SCRIPT.format(script=script)
    if script:
        return HELPER_KIND_PROMPT_AFTER_SCRIPT.format(script=script)
    return HELPER_KIND_PROMPT


def gen_cron_helper_jobs() -> str:
    """Render the Platform Agent's non-audit roster as a table, in roster order.

    The watchdogs page used to list these jobs in one paragraph that restated
    `jobs.json` by hand; this region reads the roster so an entry added,
    retired or rescheduled there changes the page on the next `docs-generate`.
    """
    rows = list(HELPER_JOBS_HEADER)
    for job in helper_jobs():
        rows.append(
            "| `{id}` | {kind} | {cadence} | {name} |".format(
                id=job.get("id", ""),
                kind=helper_kind(job),
                cadence=cadence_cell(job.get("schedule", {})),
                name=md_escape(job.get("name") or job.get("id", "")),
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


def group_skills(
    skills: dict[str, str], persona_skills: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    """Return {group: skill names} in catalogue order; empty groups are kept.

    ``skills`` is the Platform Agent's map, sorted into the editorial groups;
    ``persona_skills`` is ``{group: map}`` for the persona-scoped groups, each
    appended after them in ``PERSONA_SKILL_GROUPS`` order so the editorial
    order is untouched.
    """
    grouped = {g: [] for g in SKILL_GROUPS}
    grouped["Other"] = []
    for name in sorted(skills):
        group = next((g for g, members in SKILL_GROUPS.items() if name in members), "Other")
        grouped[group].append(name)
    for group, _ in PERSONA_SKILL_GROUPS:
        grouped[group] = sorted(persona_skills.get(group, {}))
    return grouped


def read_persona_skills() -> dict[str, dict[str, str]]:
    """Return {group: {skill name: description}} for every persona-scoped group."""
    return {group: _read_skills(skills_dir) for group, skills_dir in PERSONA_SKILL_GROUPS}


def gen_skill_catalog() -> str:
    skills = _read_skills(SKILLS_DIR)
    persona_skills = read_persona_skills()
    grouped = group_skills(skills, persona_skills)
    source_dirs = dict(PERSONA_SKILL_GROUPS)

    def source_dir(group: str) -> str:
        return source_dirs.get(group, SKILLS_DIR).relative_to(REPO).as_posix()

    out: list[str] = []
    for group, names in grouped.items():
        if not names:
            continue
        # A name can live in more than one persona directory with its own
        # description in each, so each row reads the map its group came from.
        descriptions = persona_skills.get(group, skills)
        out.append(f"### {group}\n")
        out.append("| Skill | Description |")
        out.append("| ----- | ----------- |")
        for name in names:
            link = f"{GITHUB_BLOB}/{source_dir(group)}/{name}/SKILL.md"
            out.append(f"| [`{name}`]({link}) | {md_escape(descriptions[name])} |")
        out.append("")
    return "\n".join(out).rstrip()


def run_order(audit: dict[str, str]) -> tuple[bool, bool, int, int]:
    """Sort key for the audits: daily ones first, then the rest, each by time of day.

    A schedule with no fixed time of day (an interval, a step, a list) sorts
    after every schedule that has one.
    """
    fields = audit["expr"].split()
    if len(fields) != 5 or not (fields[0].isdecimal() and fields[1].isdecimal()):
        return True, True, 0, 0
    minute, hour, *date = fields
    return date != ["*", "*", "*"], False, int(hour), int(minute)


def governance_audits() -> list[dict[str, str]]:
    """Return the enabled governance audits in the order the README lists them."""
    audits = []
    for job in load_roster(PLATFORM_CRON_ROSTER):
        if not job.get("enabled") or not is_audit(job):
            continue
        sop = SOP_REF_RE.search(job.get("prompt") or "")
        if not sop:
            raise SystemExit(
                f"{PLATFORM_CRON_ROSTER.relative_to(REPO)}: audit '{job.get('id')}' "
                "names no governance SOP in its prompt; the capabilities region "
                "has nothing to link."
            )
        schedule = job.get("schedule") or {}
        audits.append(
            {
                "id": job.get("id", ""),
                "name": job.get("name") or job.get("id", ""),
                "expr": schedule.get("expr") or "",
                "display": schedule.get("display") or "",
                "schedule": schedule,
                "sop": sop.group(0),
            }
        )
    return sorted(audits, key=run_order)


def gen_capabilities() -> str:
    """Render the README's capabilities matrix: the scheduled audits, then the skills.

    It is a short index for a first-time reader, so it names things and links to
    the site pages that describe them rather than repeating those descriptions.
    """
    out = [
        "### Scheduled governance audits\n",
        "| Audit | Schedule (UTC) | Procedure |",
        "| ----- | -------------- | --------- |",
    ]
    for audit in governance_audits():
        cadence = cadence_cell(audit["schedule"])
        link = f"{PLATFORM_HOME}/{audit['sop']}"
        out.append(
            f"| {md_escape(audit['name'])} | {cadence} | [`{Path(audit['sop']).name}`]({link}) |"
        )

    out += [
        "",
        "### Skills by area\n",
        "| Area | Skills |",
        "| ---- | ------ |",
    ]
    grouped = group_skills(_read_skills(SKILLS_DIR), read_persona_skills())
    for group, names in grouped.items():
        if names:
            out.append(f"| {group} | {', '.join(f'`{n}`' for n in names)} |")
    return "\n".join(out)


def gen_governance_audits() -> str:
    """Render the governance page's audit matrix from the roster, in README order.

    The page used to carry the watchdog ids and cadences by hand, one
    subsection per SOP; the check lists stay hand-written beside this region,
    since no machine-readable source counts them the same way for every SOP.
    """
    rows = list(GOVERNANCE_AUDITS_HEADER)
    for audit in governance_audits():
        rows.append(
            "| `{sop}` | `{id}` | {cadence} | {name} |".format(
                sop=Path(audit["sop"]).name,
                id=audit["id"],
                cadence=cadence_cell(audit["schedule"]),
                name=md_escape(audit["name"]),
            )
        )
    return "\n".join(rows)


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


def run_crd_generator(api_dir: Path, kinds: tuple[str, ...]) -> dict:
    """Run ``gen_crd_docs.go`` over ``api_dir`` and return its JSON model.

    A missing ``go`` and a generator that refuses the package are both hard
    failures with the reason: a page silently rendered from nothing would
    pass ``--check`` while saying nothing.
    """
    if shutil.which(GO_BINARY) is None:
        raise SystemExit(
            f"{CRD_DOCS_GENERATOR.relative_to(REPO)}: the CRD field pages are read "
            f"from the Go API types, which needs a Go toolchain; `{GO_BINARY}` is "
            "not on PATH."
        )
    command = [
        GO_BINARY,
        "run",
        str(CRD_DOCS_GENERATOR),
        "-dir",
        str(api_dir),
        "-kinds",
        ",".join(kinds),
    ]
    result = subprocess.run(
        command,
        cwd=REPO,
        env={**os.environ, **GO_RUN_ENV},
        capture_output=True,
        # The model carries the doc comments' non-ASCII (section signs, em
        # dashes) as UTF-8 whatever the locale's codec is.
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"{CRD_DOCS_GENERATOR.relative_to(REPO)} failed "
            f"(exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


@functools.lru_cache(maxsize=1)
def crd_model() -> dict[str, dict]:
    """The committed API package's model, by kind, read once per run."""
    model = run_crd_generator(CRD_API_DIR, tuple(CRD_KINDS))
    return {entry["kind"]: entry for entry in model["kinds"]}


def render_constraint(constraint: dict[str, str]) -> str:
    name, value = constraint["name"], constraint["value"]
    prefix = ""
    for marker_prefix, (word, _) in CONSTRAINT_PREFIXES.items():
        if name.startswith(marker_prefix):
            prefix = f"{word} "
            name = name[len(marker_prefix) :]
            break
    label = CONSTRAINT_MARKERS[name][0] if name in CONSTRAINT_MARKERS else name
    if name == "Enum":
        shown = ENUM_MEMBER_SEPARATOR.join(f"`{member}`" for member in value.split(ENUM_SEPARATOR))
    else:
        shown = f"`{value}`"
    return f"{prefix}{label} {shown}"


def render_crd_row(field: dict) -> str:
    constraints = CONSTRAINT_SEPARATOR.join(render_constraint(c) for c in field["constraints"])
    default = f"`{field['default']}`" if field["default"] else ""
    return "| `{path}` | `{type}` | {required} | {default} | {constraints} | {description} |".format(
        path=field["path"],
        type=field["type"],
        required=REQUIRED_YES if field["required"] else REQUIRED_NO,
        default=escape_cell(default),
        constraints=escape_cell(constraints),
        description=escape_cell(field["description"]),
    )


def gen_crd_fields(kind: str) -> str:
    """Render one kind's page body: the intro paragraph, then a spec and a status table."""
    entry = crd_model()[kind]
    out: list[str] = [
        CRD_INTRO.format(
            kind=kind, resource=kind.lower(), group=CRD_GROUP, guide=CRD_GUIDES[kind]
        ),
        "",
    ]
    for key, heading in CRD_TREES:
        out.append(f"## {heading}\n")
        fields = entry[key]
        if not fields:
            out.append(CRD_EMPTY_TREE)
        else:
            out.extend(CRD_TABLE_HEADER)
            out.extend(render_crd_row(field) for field in fields)
        out.append("")
    return "\n".join(out).rstrip()


# Each region names every file it is spliced into. One generator, one body,
# however many pages carry it: the job example is the same entry on three
# pages, and rendering it once is what keeps the three from disagreeing.
BLOCKS: dict[str, tuple[tuple[Path, ...], Callable[[], str]]] = {
    "cron-jobs": ((CRON_PAGE,), gen_cron_jobs),
    "cron-job-example": (
        (WATCHDOGS_PAGE, SKILLS_CONCEPT_PAGE, CRON_PAGE),
        gen_cron_job_example,
    ),
    "cron-helper-jobs": ((WATCHDOGS_PAGE,), gen_cron_helper_jobs),
    "governance-audits": ((GOVERNANCE_PAGE,), gen_governance_audits),
    "skill-catalog": ((SKILLS_PAGE,), gen_skill_catalog),
    "container-images": ((IMAGES_PAGE,), gen_container_images),
    "capabilities": ((README,), gen_capabilities),
    **{
        f"{CRD_BLOCK_PREFIX}{kind.lower()}": ((page,), functools.partial(gen_crd_fields, kind))
        for kind, page in CRD_KINDS.items()
    },
}

# Generated artifacts that are a whole file rather than a region inside a
# hand-written document, written verbatim from their generator.


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


def splice(path: Path, block_id: str, body: str, text: str | None = None) -> tuple[bool, str]:
    """Return (changed, new_text) with the generated region replaced.

    ``text`` is the document to splice into when the caller already holds it;
    a page carrying several regions is spliced cumulatively, each block into
    the text the previous one produced, so the last result carries them all.
    Left ``None``, the file is read from disk.
    """
    if text is None:
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


Target = tuple[Path, str, bool, str]


def collect_targets(blocks=BLOCKS) -> list[Target]:
    """Return (path, full new contents, changed, block id) for every target.

    A region's generator runs once and the one body is spliced into each of
    the region's files, so two pages carrying the same block cannot differ.
    A file carrying several regions is spliced cumulatively: each block goes
    into the text the previous block left, not into a fresh read of the disk,
    so its last target holds every block and writing the targets in order
    cannot put a stale region back. ``changed`` is per block, so the report
    still names which region moved.
    """
    targets: list[Target] = []
    pending: dict[Path, str] = {}
    for block_id, (paths, generator) in blocks.items():
        body = generator()
        for path in paths:
            changed, new_text = splice(path, block_id, body, pending.get(path))
            pending[path] = new_text
            targets.append((path, new_text, changed, block_id))
    return targets


def display_path(path: Path) -> Path:
    """A path as the report prints it: relative to the repository when inside it."""
    return path.relative_to(REPO) if path.is_relative_to(REPO) else path


def select_blocks(names: list[str], prefixes: list[str] = (), blocks=BLOCKS) -> dict:
    """The blocks a run covers, in registry order.

    No names and no prefixes selects every block. A name must be a registered
    id and a prefix must match at least one, so a typo fails the run rather
    than quietly covering nothing.
    """
    unknown = sorted(set(names) - set(blocks))
    if unknown:
        raise SystemExit(
            f"unknown block id(s): {', '.join(unknown)}; known: {', '.join(blocks)}"
        )
    for prefix in prefixes:
        if not any(block_id.startswith(prefix) for block_id in blocks):
            raise SystemExit(
                f"no block id starts with '{prefix}'; known: {', '.join(blocks)}"
            )
    if not names and not prefixes:
        return dict(blocks)
    return {
        block_id: entry
        for block_id, entry in blocks.items()
        if block_id in names or any(block_id.startswith(prefix) for prefix in prefixes)
    }


def needs_go(block_id: str) -> bool:
    """Whether a block's generator reads the Go API types."""
    return block_id.startswith(CRD_BLOCK_PREFIX)


def main(argv: list[str] | None = None, blocks=BLOCKS) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any generated region is out of date",
    )
    ap.add_argument(
        "--block-prefix",
        action="append",
        default=[],
        metavar="PREFIX",
        help="also cover every block whose id starts with PREFIX (repeatable)",
    )
    ap.add_argument(
        "blocks",
        nargs="*",
        metavar="BLOCK",
        help="block ids to regenerate or check; default: all of them",
    )
    args = ap.parse_args(argv)

    selected = select_blocks(args.blocks, args.block_prefix, blocks)
    # A write run on a machine without Go still refreshes every region that
    # needs none; the CRD pages are reported skipped, not silently left. A
    # block asked for by name is wanted, so it fails as it would in --check.
    if not args.check and not args.blocks and not args.block_prefix and shutil.which(GO_BINARY) is None:
        for block_id in [b for b in selected if needs_go(b)]:
            print(f"  skipped  [{block_id}]: needs `{GO_BINARY}` on PATH; regions that need none are refreshed")
            del selected[block_id]

    targets = collect_targets(selected)

    stale: list[str] = []
    for path, new_text, changed, block_id in targets:
        rel = display_path(path)
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
