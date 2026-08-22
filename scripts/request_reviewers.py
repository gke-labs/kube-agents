#!/usr/bin/env python3
"""Request a human reviewer on a pull request, once the AI review is green.

`.github/workflows/auto_request_review.yml` used to run
`necojackarc/auto-request-review` on `pull_request_target`, which pinged a human
the moment a pull request opened -- minutes before `kube-agents-bot` posted its
read, and on most pull requests before the author had addressed a single
finding. The reviewer is now requested from the bot's verdict instead: the
`AI Review` check run going `success`.

That trigger is why this script exists rather than the action. The action reads
`context.payload.pull_request`, which a `check_run` event does not carry, and
`check_run.pull_requests` is empty for pull requests from forks -- which is
every pull request in this repository. `pull_request_review` is not a way out
either: on a fork pull request its `GITHUB_TOKEN` is read-only and `permissions:`
cannot raise it, so it cannot request a reviewer at all.

The reviewer *selection* below is a port of the action's `src/reviewer.js` at the
pinned v0.13.0, reading `.github/auto_request_review.yml` unchanged. Where the
port had a choice it copies the action, including minimatch's rule that `*` and
`**` do not match a path segment beginning with a dot. Anything the port does
not implement raises rather than guessing -- see `validate_config` and
`glob_to_regex`.

Run: python3 scripts/request_reviewers.py --pr 728 --dry-run
Test: cd scripts && python3 -m unittest test_request_reviewers
"""

import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

# The `AI Review` check run comes from the kube-agents-bot GitHub App. The name
# alone is not enough of an identity check: any App may post a check run with
# any name, and this one decides who gets pinged.
AI_REVIEW_CHECK_NAME = "AI Review"
AI_REVIEW_APP_ID = 4437198

DEFAULT_CONFIG_PATH = ".github/auto_request_review.yml"
DEFAULT_IGNORED_KEYWORDS = ["DO NOT REVIEW"]

# Review states that mean a person has actually reviewed. `COMMENTED` is not one
# of them: GitHub files a `COMMENTED` review for a reply to a review thread.
HUMAN_VERDICT_STATES = {"APPROVED", "CHANGES_REQUESTED"}

API_ROOT = "https://api.github.com"
PER_PAGE = 100

# Config keys the action supports and this port does not. Silently ignoring one
# would hand the reviewer selection a rule nobody applied, so they are refused.
UNSUPPORTED_CONFIG = {
    "reviewers.per_author": lambda config: "per_author" in (config.get("reviewers") or {}),
    "options.enable_group_assignment": lambda config: bool(
        (config.get("options") or {}).get("enable_group_assignment")
    ),
}

# Glob syntax minimatch accepts and `glob_to_regex` does not translate. `!` is
# only special leading the pattern; the rest are special anywhere.
UNSUPPORTED_GLOB_CHARS = "{}[]()|\\"


def log(message):
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    if not isinstance(config, dict):
        raise ValueError(f"{path} does not parse to a mapping")

    validate_config(config)
    return config


def validate_config(config):
    """Refuse a config using a feature this port does not implement."""
    for name, is_used in UNSUPPORTED_CONFIG.items():
        if is_used(config):
            raise ValueError(
                f"{name} is set, and scripts/request_reviewers.py does not implement it. "
                "Implement it here (and test it) before putting it in the config."
            )

    for pattern in (config.get("files") or {}):
        glob_to_regex(pattern)


# --------------------------------------------------------------------------- #
# Glob matching -- the minimatch subset the config uses
# --------------------------------------------------------------------------- #


def glob_to_regex(pattern):
    """Translate a minimatch glob to a regex, dotfile rule included.

    minimatch runs with its defaults in the action, so `*` and `**` never match
    a path segment that starts with a dot. That is not a detail: `"**"` is the
    catch-all entry in this repository's config, and it does *not* cover
    `.github/workflows/...`, which is why those files have literal entries of
    their own.
    """
    if pattern.startswith("!"):
        raise ValueError(f"negated glob {pattern!r} is not supported")

    bad = sorted({char for char in pattern if char in UNSUPPORTED_GLOB_CHARS})
    if bad:
        raise ValueError(f"glob {pattern!r} uses unsupported syntax: {''.join(bad)}")

    segments = pattern.split("/")
    parts = []

    for index, segment in enumerate(segments):
        last = index == len(segments) - 1

        if segment == "**":
            if last:
                # One or more trailing segments: `k8s-operator/**` matches
                # `k8s-operator/main.go` and `k8s-operator/a/b.go`.
                parts.append(r"(?!\.)[^/]+(?:/(?!\.)[^/]+)*")
            else:
                # Zero or more segments, separator included, so `a/**/b.go`
                # still matches `a/b.go`.
                parts.append(r"(?:(?!\.)[^/]+/)*")
            continue

        parts.append(_segment_regex(segment))
        if not last:
            parts.append("/")

    return re.compile("^" + "".join(parts) + "$")


def _segment_regex(segment):
    body = "".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in segment)
    # A wildcard may not consume a leading dot; a literal dot in the pattern may.
    return r"(?!\.)" + body if segment[:1] in ("*", "?") else body


def matches_any(pattern, paths):
    regex = glob_to_regex(pattern)
    return any(regex.match(path) for path in paths)


# --------------------------------------------------------------------------- #
# Reviewer selection -- a port of the action's src/reviewer.js
# --------------------------------------------------------------------------- #


def _expand_groups(names, config):
    """Replace group names with their members. Single level, as the action does."""
    groups = (config.get("reviewers") or {}).get("groups") or {}
    expanded = []
    for name in names:
        members = groups.get(name)
        expanded.extend(members if isinstance(members, list) else [name])
    return expanded


def _dedupe(names, exclude):
    seen = []
    for name in names:
        if name not in seen and name != exclude:
            seen.append(name)
    return seen


def reviewers_by_changed_files(config, changed_files, author):
    """Reviewers matched by the `files` globs.

    A glob matches when *any* changed file matches it, and with
    `last_files_match_only` the last matching glob replaces everything matched
    before it -- so ordering in the config file is what decides, not
    specificity.
    """
    files = config.get("files") or {}
    last_match_only = bool((config.get("options") or {}).get("last_files_match_only"))

    matched = []
    for pattern, reviewers in files.items():
        if not matches_any(pattern, changed_files):
            continue
        if last_match_only:
            matched.clear()
        matched.extend(reviewers)

    return _dedupe(_expand_groups(matched, config), author)


def default_reviewers(config, author):
    defaults = (config.get("reviewers") or {}).get("defaults")
    if not isinstance(defaults, list):
        return []
    return _dedupe(_expand_groups(defaults, config), author)


def select_reviewers(config, changed_files, author, rng=random):
    """The full selection: globs, then defaults as fallback, then sampling."""
    reviewers = reviewers_by_changed_files(config, changed_files, author)

    if not reviewers:
        reviewers = default_reviewers(config, author)
        if reviewers:
            log("No glob matched; falling back to the default reviewers")

    number = (config.get("options") or {}).get("number_of_reviewers")
    if number is not None and reviewers:
        reviewers = rng.sample(reviewers, min(int(number), len(reviewers)))

    return reviewers


def split_teams(reviewers):
    """`team:` entries are requested as teams, the rest as users."""
    teams = [name[len("team:") :] for name in reviewers if name.startswith("team:")]
    users = [name for name in reviewers if not name.startswith("team:")]
    return users, teams


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def skip_reason(pull_request, reviews, config):
    """Why this pull request should not have a reviewer requested, or None.

    Every branch here is a *skip*, not a failure: the workflow fires on each
    completed `AI Review` check, so re-running on a pull request that has
    already been handed to a human is the normal case, not an error.
    """
    options = config.get("options") or {}

    if pull_request.get("state") != "open":
        return f"the pull request is {pull_request.get('state')}, not open"

    if pull_request.get("draft") and options.get("ignore_draft", True):
        return "the pull request is a draft"

    title = pull_request.get("title") or ""
    for keyword in options.get("ignored_keywords", DEFAULT_IGNORED_KEYWORDS):
        if keyword in title:
            return f"the title contains the ignored keyword {keyword!r}"

    requested = [user["login"] for user in pull_request.get("requested_reviewers") or []]
    requested += [f"team:{team['slug']}" for team in pull_request.get("requested_teams") or []]
    if requested:
        return f"review is already requested from {', '.join(requested)}"

    # Only a verdict from another person counts. Replying to a review thread
    # files a `COMMENTED` review under the replier's name, and AGENTS.md tells
    # authors to answer every finding before running `/review` -- so counting
    # those would mean the pull requests that follow the process are exactly the
    # ones that never get a reviewer.
    author = (pull_request.get("user") or {}).get("login")
    humans = _dedupe(
        [
            review["user"]["login"]
            for review in reviews
            if (review.get("user") or {}).get("type") != "Bot"
            and review.get("state") in HUMAN_VERDICT_STATES
        ],
        exclude=author,
    )
    if humans:
        return f"{', '.join(humans)} already reviewed it"

    return None


def latest_ai_review(check_runs):
    """The most recent `AI Review` check run from the bot, or None.

    `/review` posts a fresh check run rather than updating the old one, so a
    head commit can carry several and only the last one is the verdict.
    """
    mine = [
        run
        for run in check_runs
        if run.get("name") == AI_REVIEW_CHECK_NAME
        and (run.get("app") or {}).get("id") == AI_REVIEW_APP_ID
    ]
    if not mine:
        return None
    return max(mine, key=lambda run: (run.get("started_at") or "", run.get("id") or 0))


def ai_review_block_reason(check_run, author_is_bot):
    """Why the AI review does not clear this pull request, or None.

    A bot cannot read its own findings and comment `/review`, so a pull request
    Dependabot opened passes on any completed conclusion. A human author has to
    get it to `success`, or comment `/request-review` to override.
    """
    if check_run is None:
        return f"there is no {AI_REVIEW_CHECK_NAME} check run on the head commit"

    if check_run.get("status") != "completed":
        return f"{AI_REVIEW_CHECK_NAME} is {check_run.get('status')}"

    conclusion = check_run.get("conclusion")
    if conclusion == "success":
        return None

    if author_is_bot:
        log(f"{AI_REVIEW_CHECK_NAME} concluded {conclusion}; the author is a bot, so it cannot re-run /review")
        return None

    title = (check_run.get("output") or {}).get("title")
    detail = f" ({title})" if title else ""
    return f"{AI_REVIEW_CHECK_NAME} concluded {conclusion}{detail}, not success"


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #


class GitHubAPI:
    def __init__(self, repo, token, root=API_ROOT):
        self.repo = repo
        self.token = token
        self.root = root

    def _request(self, method, path, body=None):
        request = urllib.request.Request(
            f"{self.root}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "kube-agents-request-reviewers",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request) as response:
            payload = response.read()
        return json.loads(payload) if payload else None

    def get(self, path):
        return self._request("GET", path)

    def get_all(self, path):
        """Every page of a list endpoint. A truncated list reads as complete."""
        separator = "&" if "?" in path else "?"
        items = []
        page = 1
        while True:
            batch = self._request("GET", f"{path}{separator}per_page={PER_PAGE}&page={page}")
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items
            page += 1

    def post(self, path, body):
        return self._request("POST", path, body)


def resolve_pull_request(api, head_sha):
    """Find the open pull request the commit `head_sha` belongs to.

    Not `GET /commits/{sha}/pulls`: that endpoint returns nothing for a fork's
    head commit (checked against #734), and every pull request here is a fork.

    Usually the commit is still the head. It is not when the author pushed
    during the review -- the check run carries the commit the bot read, which by
    the time it completes is one behind. Matching on the head alone would drop
    that event, and nothing would retry it: a push does not start another AI
    review. So fall back to the pull request that *contains* the commit, at the
    cost of one extra call per open pull request in a case that is rare.
    """
    open_pull_requests = api.get_all(f"/repos/{api.repo}/pulls?state=open")

    for pull_request in open_pull_requests:
        if pull_request["head"]["sha"] == head_sha:
            return pull_request

    for pull_request in open_pull_requests:
        commits = api.get_all(f"/repos/{api.repo}/pulls/{pull_request['number']}/commits")
        if any(commit["sha"] == head_sha for commit in commits):
            log(
                f"#{pull_request['number']} has moved on to {pull_request['head']['sha'][:7]} "
                f"since {head_sha[:7]} was reviewed"
            )
            return pull_request

    # A force-push during the review leaves the reviewed commit on no branch at
    # all, and there is nothing left to match against.
    return None


def gate_check_run(api, pull_request, triggering):
    """The `AI Review` check run the gate should be decided on.

    The triggering one, unless the head has moved since -- then the current head
    may carry a newer verdict, and a newer verdict wins. If it carries none, the
    stale one still decides: holding out for a review that will never be
    requested is how a pull request goes quiet forever.
    """
    head_sha = pull_request["head"]["sha"]

    if triggering is not None and triggering.get("head_sha") == head_sha:
        return triggering

    check_runs = api.get(f"/repos/{api.repo}/commits/{head_sha}/check-runs")["check_runs"]
    current = latest_ai_review(check_runs)

    if current is None and triggering is not None:
        log(f"No {AI_REVIEW_CHECK_NAME} on the current head; deciding on the one that triggered this run")
        return triggering

    return current


def fetch_ai_review_check_run(api, check_run_id):
    """The triggering check run, refetched and re-identified.

    The workflow's `if:` has already checked the name and the App, but it
    checked an event payload. This reads the same fields back from the API, and
    gets the commit the bot actually reviewed rather than trusting the payload
    for it.
    """
    check_run = api.get(f"/repos/{api.repo}/check-runs/{check_run_id}")

    name = check_run.get("name")
    app_id = (check_run.get("app") or {}).get("id")
    if name != AI_REVIEW_CHECK_NAME or app_id != AI_REVIEW_APP_ID:
        raise ValueError(f"check run {check_run_id} is {name!r} from app {app_id}, not the AI review")

    return check_run


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int, help="pull request number")
    target.add_argument("--head-sha", help="a commit of the pull request to find")
    target.add_argument(
        "--check-run-id",
        type=int,
        help="the AI Review check run that triggered this; supplies the commit and the verdict",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "gke-labs/kube-agents"),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--require-ai-review-pass",
        action="store_true",
        help="only request a reviewer if the AI Review check passed",
    )
    parser.add_argument("--react-to", type=int, help="issue comment id to acknowledge with 👀")
    parser.add_argument("--seed", type=int, help="seed the reviewer sampling, for reproducible runs")
    parser.add_argument("--dry-run", action="store_true", help="print what would be requested")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    config = load_config(args.config)
    api = GitHubAPI(args.repo, token)

    triggering_check_run = None
    if args.check_run_id:
        triggering_check_run = fetch_ai_review_check_run(api, args.check_run_id)

    if args.pr:
        pull_request = api.get(f"/repos/{args.repo}/pulls/{args.pr}")
    else:
        commit = args.head_sha or triggering_check_run["head_sha"]
        pull_request = resolve_pull_request(api, commit)
        if pull_request is None:
            log(f"No open pull request contains {commit}; nothing to do")
            return 0

    number = pull_request["number"]
    author = pull_request["user"]["login"]
    log(f"#{number} by {author}: {pull_request['title']}")

    reviews = api.get_all(f"/repos/{args.repo}/pulls/{number}/reviews")
    reason = skip_reason(pull_request, reviews, config)
    if reason:
        log(f"Not requesting a reviewer: {reason}")
        return 0

    if args.require_ai_review_pass:
        reason = ai_review_block_reason(
            gate_check_run(api, pull_request, triggering_check_run),
            pull_request["user"].get("type") == "Bot",
        )
        if reason:
            log(f"Not requesting a reviewer: {reason}")
            return 0

    changed_files = [
        entry["filename"] for entry in api.get_all(f"/repos/{args.repo}/pulls/{number}/files")
    ]
    rng = random.Random(args.seed) if args.seed is not None else random
    reviewers = select_reviewers(config, changed_files, author, rng=rng)

    if not reviewers:
        log("No reviewer matched; nothing to request")
        return 0

    users, teams = split_teams(reviewers)
    log(f"Requesting review from {', '.join(reviewers)}")

    if args.dry_run:
        log(f"--dry-run: would POST reviewers={users} team_reviewers={teams} to #{number}")
        return 0

    api.post(
        f"/repos/{args.repo}/pulls/{number}/requested_reviewers",
        {"reviewers": users, "team_reviewers": teams},
    )

    if args.react_to:
        api.post(f"/repos/{args.repo}/issues/comments/{args.react_to}/reactions", {"content": "eyes"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
