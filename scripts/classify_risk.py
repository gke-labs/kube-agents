#!/usr/bin/env python3
"""Classify a pull request's risk tier from its diff, as a non-blocking signal.

Reviewers get no signal about which pull requests are safe to clear quickly:
the only risk information on a pull request is the template's Risk & Rollout
section, author-asserted prose that nothing verifies (#818). This script
computes a `low` / `medium` / `high` tier from the diff itself against the
rules in `.github/risk-rules.yml`, then surfaces it three ways:

  - a `Risk Classification` check run, always concluding `success` -- the tier
    lives in the title, the triggered rules in the summary, and a fenced JSON
    block carries the machine-readable contract for later consumers;
  - a `risk:low|medium|high` label, swapped idempotently;
  - a declared-vs-computed note when the Risk & Rollout section says "low
    risk" and the rules say high -- the same contract reviewers apply to
    Self-Review, where a claim the diff does not support is itself a finding.

The tier is computed from the diff and the rules alone. The pull request's
title, labels, and body are inputs only to the declared-vs-computed check,
never to the tier, so wording cannot buy a lower classification.

`.github/workflows/risk_classify.yml` runs this on `pull_request_target` and
never checks out pull-request code: the diff arrives as text through the API.

Run: python3 scripts/classify_risk.py --pr 812 --dry-run
Test: cd scripts && python3 -m unittest test_classify_risk
"""

import argparse
import json
import os
import re
import sys
import urllib.error

import yaml

# The API plumbing and the minimatch glob subset are request_reviewers.py's;
# both scripts read globs the same way or the two configs drift apart.
import request_reviewers as rr

CHECK_NAME = "Risk Classification"
DEFAULT_RULES_PATH = ".github/risk-rules.yml"

TIER_ORDER = {"low": 0, "medium": 1, "high": 2}
LABEL_PREFIX = "risk:"
LABEL_COLORS = {"low": "0e8a16", "medium": "fbca04", "high": "d93f0b"}

RULE_KEYS = {"id", "tier", "why", "match", "only_match", "all_of", "patch_contains"}
SELECTOR_KEYS = {"match", "only_match", "all_of", "patch_contains"}

# The version of the JSON block in the check-run summary. Bump it when a field
# changes meaning; consumers pin against it.
SCHEMA_VERSION = 1

RISK_SECTION = re.compile(
    r"^##\s*Risk\s*&\s*Rollout\s*$(?P<text>.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# What authors actually write in the section, from the open pull requests the
# heuristic was replayed against: "Low risk, ...", "Risk: none to any running
# system", and a bare leading verdict -- "Low. No runtime code paths" (#770),
# "Moderate, and concentrated in one place" (#733). Deliberately loose in one
# direction: when a section mentions more than one tier, the highest counts as
# the declaration, so a cautious sentence ("high-risk paths are untouched")
# only ever suppresses the mismatch flag, never raises a false one.
DECLARED_WORDS = {
    "low": "low|none|minimal|negligible",
    "medium": "medium|moderate",
    "high": "high",
}
DECLARED = {
    tier: re.compile(
        # "<word> risk" / "<word>-risk", or "risk ... <word>" in one clause,
        # or the section opening with the bare word ("Low." / "Moderate,").
        # The lookahead keeps "High-level overview" from declaring high.
        rf"\b(?:{words})[- ]risk\b"
        rf"|\brisk\b[^.!?\n]{{0,40}}?\b(?:{words})\b"
        rf"|\A[\s>*_-]*(?:\*\*)?(?:{words})(?=[\s.,:;!)])",
        re.IGNORECASE,
    )
    for tier, words in DECLARED_WORDS.items()
}


def log(message):
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def load_rules(path):
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    if not isinstance(config, dict):
        raise ValueError(f"{path} does not parse to a mapping")

    validate_rules(config)
    return config


def validate_rules(config):
    """Refuse a config this script would misread. Silence here becomes a wrong
    tier on every pull request, so anything unrecognised raises."""
    default = config.get("default_tier", "medium")
    if default not in TIER_ORDER:
        raise ValueError(f"default_tier {default!r} is not one of {sorted(TIER_ORDER)}")

    rules = config.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules must be a non-empty list")

    seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(f"rule {rule!r} is not a mapping")

        unknown = set(rule) - RULE_KEYS
        if unknown:
            raise ValueError(f"rule {rule.get('id')!r} has unknown keys: {sorted(unknown)}")

        for key in ("id", "tier", "why"):
            if not rule.get(key):
                raise ValueError(f"rule {rule.get('id')!r} is missing {key!r}")

        if rule["id"] in seen:
            raise ValueError(f"rule id {rule['id']!r} appears twice")
        seen.add(rule["id"])

        if rule["tier"] not in TIER_ORDER:
            raise ValueError(f"rule {rule['id']!r} tier {rule['tier']!r} is not one of {sorted(TIER_ORDER)}")

        selectors = SELECTOR_KEYS & set(rule)
        if not selectors:
            raise ValueError(f"rule {rule['id']!r} has no selector ({sorted(SELECTOR_KEYS)})")

        # `only_match` and `all_of` decide on the whole file set; mixing them
        # with the per-file selectors has no one obvious meaning, so refuse.
        for whole in ("only_match", "all_of"):
            if whole in rule and selectors != {whole}:
                raise ValueError(f"rule {rule['id']!r} combines {whole!r} with other selectors")

        # A low rule scoped to a subset of files would classify a mixed pull
        # request low on the strength of its safe half.
        if rule["tier"] == "low" and "only_match" not in rule:
            raise ValueError(f"low rule {rule['id']!r} must use only_match")

        for key in ("match", "only_match"):
            for pattern in rule.get(key, []):
                rr.glob_to_regex(pattern)
        for group in rule.get("all_of", []):
            if not isinstance(group, list) or not group:
                raise ValueError(f"rule {rule['id']!r} all_of groups must be non-empty lists")
            for pattern in group:
                rr.glob_to_regex(pattern)
        for pattern in rule.get("patch_contains", []):
            re.compile(pattern)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def _matching_paths(globs, paths):
    regexes = [rr.glob_to_regex(pattern) for pattern in globs]
    return [path for path in paths if any(regex.match(path) for regex in regexes)]


def rule_trigger(rule, files):
    """The changed files that trigger `rule`, or None when it does not.

    `files` is the GitHub `pulls/{n}/files` shape: dicts with `filename` and,
    for text diffs the API is willing to inline, `patch`. A file without a
    patch never satisfies `patch_contains`.
    """
    paths = [entry["filename"] for entry in files]

    if "only_match" in rule:
        if paths and len(_matching_paths(rule["only_match"], paths)) == len(paths):
            return paths
        return None

    if "all_of" in rule:
        groups = [_matching_paths(group, paths) for group in rule["all_of"]]
        if all(groups):
            return sorted({path for group in groups for path in group})
        return None

    candidates = files
    if "match" in rule:
        matched = set(_matching_paths(rule["match"], paths))
        candidates = [entry for entry in files if entry["filename"] in matched]
        if not candidates:
            return None

    if "patch_contains" in rule:
        regexes = [re.compile(pattern, re.MULTILINE) for pattern in rule["patch_contains"]]
        candidates = [
            entry
            for entry in candidates
            if any(regex.search(entry.get("patch") or "") for regex in regexes)
        ]
        if not candidates:
            return None

    return [entry["filename"] for entry in candidates]


def classify(config, files):
    """The tier and the rules behind it: {tier, default_applied, rules}."""
    triggered = []
    for rule in config["rules"]:
        hits = rule_trigger(rule, files)
        if hits is not None:
            triggered.append(
                {"id": rule["id"], "tier": rule["tier"], "why": rule["why"], "files": hits}
            )

    if triggered:
        tier = max((entry["tier"] for entry in triggered), key=TIER_ORDER.get)
        default_applied = False
    else:
        tier = config.get("default_tier", "medium")
        default_applied = True

    return {"tier": tier, "default_applied": default_applied, "rules": triggered}


def declared_tier(body):
    """The tier the Risk & Rollout section claims, or None without one."""
    section = RISK_SECTION.search(body or "")
    if not section:
        return None
    text = section.group("text")
    found = [tier for tier, regex in DECLARED.items() if regex.search(text)]
    return max(found, key=TIER_ORDER.get) if found else None


def build_result(config, files, body, pr_number=None, head_sha=None):
    result = classify(config, files)
    declared = declared_tier(body)
    result.update(
        {
            "schema": SCHEMA_VERSION,
            "pr": pr_number,
            "head_sha": head_sha,
            "declared_tier": declared,
            "mismatch": declared == "low" and result["tier"] == "high",
        }
    )
    return result


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def check_run_title(result):
    if result["mismatch"]:
        return f"risk: {result['tier']} — Risk & Rollout declares low"
    count = len(result["rules"])
    detail = "no rule matched" if result["default_applied"] else f"{count} rule{'s' if count != 1 else ''}"
    return f"risk: {result['tier']} ({detail})"


def check_run_summary(result):
    lines = [f"## Risk: {result['tier']}", ""]

    if result["mismatch"]:
        lines += [
            "**The Risk & Rollout section declares low risk; the diff classifies "
            "high.** Read that section against the rules below before anything "
            "else — a claim the diff does not support is itself a finding.",
            "",
        ]

    if result["default_applied"]:
        lines += [
            f"No rule in `{DEFAULT_RULES_PATH}` matched, so the default tier applies. "
            "An unclassified path is not the same thing as a safe one.",
            "",
        ]
    else:
        for entry in result["rules"]:
            files = ", ".join(f"`{path}`" for path in entry["files"][:5])
            more = f" (+{len(entry['files']) - 5} more)" if len(entry["files"]) > 5 else ""
            lines.append(f"- `{entry['id']}` → **{entry['tier']}** — {entry['why']} ({files}{more})")
        lines.append("")

    lines += [
        "This check is signal for the reviewer; it blocks nothing. "
        f"Rules live in `{DEFAULT_RULES_PATH}` (#818).",
        "",
        "```json",
        json.dumps(result, indent=2, sort_keys=True),
        "```",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# GitHub side effects
# --------------------------------------------------------------------------- #


def post_check_run(api, head_sha, result):
    api.post(
        f"/repos/{api.repo}/check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": "success",
            "output": {"title": check_run_title(result), "summary": check_run_summary(result)},
        },
    )


def _tolerate(status, call):
    """Run an API call, swallowing exactly one expected HTTP status."""
    try:
        call()
    except urllib.error.HTTPError as error:
        if error.code != status:
            raise


def sync_labels(api, pr_number, tier, current):
    """Make `risk:{tier}` the one risk label on the pull request."""
    desired = f"{LABEL_PREFIX}{tier}"

    for name in current:
        if name.startswith(LABEL_PREFIX) and name != desired:
            # 404: someone removed it between our read and this call.
            _tolerate(
                404,
                lambda name=name: api.delete(f"/repos/{api.repo}/issues/{pr_number}/labels/{name}"),
            )

    if desired not in current:
        # 422: the label already exists in the repository. Creating it first
        # keeps the colours consistent; adding an unknown label name to an
        # issue would otherwise invent one with a random colour.
        _tolerate(
            422,
            lambda: api.post(
                f"/repos/{api.repo}/labels",
                {"name": desired, "color": LABEL_COLORS[tier], "description": f"computed by {CHECK_NAME} (#818)"},
            ),
        )
        api.post(f"/repos/{api.repo}/issues/{pr_number}/labels", {"labels": [desired]})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int, help="pull request number")
    target.add_argument(
        "--files-json",
        help="offline: a JSON file in the pulls/{n}/files shape; classifies and prints, no API",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "gke-labs/kube-agents"),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--rules", default=DEFAULT_RULES_PATH)
    parser.add_argument("--body-file", help="with --files-json: the pull request body to parse")
    parser.add_argument(
        "--dry-run", action="store_true", help="classify and print, but post no check run or label"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = load_rules(args.rules)

    if args.files_json:
        with open(args.files_json, encoding="utf-8") as handle:
            files = json.load(handle)
        body = ""
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as handle:
                body = handle.read()
        print(json.dumps(build_result(config, files, body), indent=2, sort_keys=True))
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    api = rr.GitHubAPI(args.repo, token)
    pull_request = api.get(f"/repos/{args.repo}/pulls/{args.pr}")
    # The head from the API rather than the event payload: on a `synchronize`
    # the payload's head can be a push behind by the time the job runs, and a
    # check run on a superseded commit is invisible on the pull request.
    head_sha = pull_request["head"]["sha"]
    files = api.get_all(f"/repos/{args.repo}/pulls/{args.pr}/files")

    result = build_result(
        config, files, pull_request.get("body"), pr_number=args.pr, head_sha=head_sha
    )
    log(f"#{args.pr} at {head_sha[:7]}: {check_run_title(result)}")
    print(json.dumps(result, indent=2, sort_keys=True))

    if args.dry_run:
        return 0

    post_check_run(api, head_sha, result)
    sync_labels(api, args.pr, result["tier"], [label["name"] for label in pull_request.get("labels") or []])
    return 0


if __name__ == "__main__":
    sys.exit(main())
