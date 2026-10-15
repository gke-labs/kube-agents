#!/usr/bin/env python3
"""Request a human reviewer on a pull request: once the AI review is green, or at the bot's third round.

`.github/workflows/auto_request_review.yml` used to run
`necojackarc/auto-request-review` on `pull_request_target`, which pinged a human
the moment a pull request opened -- minutes before `kube-agents-bot` posted its
read, and on most pull requests before the author had addressed a single
finding. The reviewer is now requested from the bot's verdict instead: the
`AI Review` check run going `success`, or, once per pull request, completing grey after the bot has
reviewed three distinct commits (`HANDOFF_ROUNDS`; the comment above `AI_REVIEW_BOT_LOGIN` has the
measurement). The first request the workflow makes on a pull request leaves one hand-off comment, on either of
its paths; a request run by hand announces nothing, since its comment would land under the
maintainer's own login and never be recognised as the hand-off.

That trigger is why this script exists rather than the action. The action reads
`context.payload.pull_request`, which a `check_run` event does not carry, and
`check_run.pull_requests` is empty for pull requests from forks -- which is
every pull request in this repository. `pull_request_review` is not a way out
either: on a fork pull request its `GITHUB_TOKEN` is read-only and `permissions:`
cannot raise it, so it cannot request a reviewer at all.

The reviewer *selection* below is a port of the action's `src/reviewer.js` at the
pinned v0.13.0, reading `.github/auto_request_review.yml` in the action's format
plus one key of this script's own, `options.robot_accounts`. Where the port had
a choice it copies the action, including minimatch's rule that `*` and
`**` do not match a path segment beginning with a dot. Anything the port does
not implement raises rather than guessing -- see `validate_config` and
`glob_to_regex`.

Six things the action never did. A verdict counts as "already reviewed" only
from someone whose approval finishes the pull request: an `OWNERS` approver for
the changed files (`applicable_approvers`), since only that approval can produce
the `approved` label, or, when the author's own approval already covers every
changed file (`author_approves`), anyone whose review Prow takes for `lgtm`
(`applicable_reviewers`); an approval from anyone else used to suppress the
auto-assign for good. The same test decides who is asked: a pull request the
author does not self-approve draws an approver alone, so a non-approver in the
pool is only ever asked for the one label still missing. And `/request-review`
(`--react-to`) is a person saying "ask someone anyway", so it skips the verdict
check, and when it still declines -- draft, closed, someone already requested --
it says so with a 😕 reaction on the comment and a warning annotation on the run,
where before it exited green having done nothing. And `options.robot_accounts`
names robots that review under an ordinary user account: their verdicts never
count, and a review request outstanding to one of them is not a reviewer
already asked. And the bot's third round: a grey check at its third distinct reviewed
commit (`HANDOFF_ROUNDS`) requests a reviewer anyway, once, and the first request on a pull
request leaves one hand-off comment (`HANDOFF_MARKER`), the marker being what stops a later grey
round asking again.

Run: python3 scripts/request_reviewers.py --pr 728 --dry-run
Test: cd scripts && python3 -m unittest test_request_reviewers
"""

import argparse
import json
import os
import pathlib
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

from github_api import API_ROOT, PER_PAGE, GitHubAPI, log

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
APPROVED_STATE = "APPROVED"
# Robots that review under an ordinary user account rather than a GitHub App,
# so `user.type` reads "User" and nothing else tells them from a person. The
# roster config lists them under this option and their reviews never count as
# a verdict either way: one re-reviews every push and files its follow-ups as
# COMMENTED, so a CHANGES_REQUESTED it once filed would otherwise stand for the
# life of the pull request and the check-run path would never request a human.
# A review request outstanding to one of them is not a reviewer already asked.
ROBOT_ACCOUNTS_OPTION = "robot_accounts"
# What a robot entry has to look like to be a login at all: a letter or digit,
# then letters, digits, hyphens or underscores, at most this many characters.
# Both comparisons the robot list feeds are exact against the `login` field
# GitHub returns, so an entry an editor decorated (`@kyber775`,
# `kyber775[bot]`, a trailing space, `org/kyber775`) would pass a non-empty
# check and then match nothing, silently: the validator refuses those. It does
# not restate GitHub's account-creation rules for what comes after the first
# character -- Enterprise Managed Users carry `handle_shortcode`, older
# accounts carry underscores of their own, and the rules have changed over
# the years -- because refusing a login GitHub actually issued is the worse
# error: the robot could not be listed at all.
GITHUB_LOGIN_MAX_LENGTH = 39
GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# Prow's OWNERS files, read from the checkout the workflow runs in -- the
# default branch, which is also where Prow reads them. `approvers:` covers the
# directory's subtree; a `filters:` entry covers the paths its regex matches,
# relative to the OWNERS file's directory; `no_parent_owners` stops the walk
# once a file has matched something at that level. `OWNERS_ALIASES` expands
# names in either list. Root OWNERS approvers and `hack/OWNERS`'s eval-crew
# filter are the two shapes this repository has today.
OWNERS_FILENAME = "OWNERS"
OWNERS_ALIASES_FILENAME = "OWNERS_ALIASES"
DEFAULT_OWNERS_ROOT = "."
# The two lists an OWNERS file keeps, walked separately as Prow walks them:
# `approvers` can set `approved`, and both lists can set `lgtm`.
OWNERS_APPROVERS_KEY = "approvers"
OWNERS_REVIEWERS_KEY = "reviewers"

# Reactions on the `/request-review` comment: 👀 when a reviewer was requested
# for it, 😕 when the request was declined. Without the second, a declined
# override looked identical to the workflow never firing.
REACTION_ACKNOWLEDGED = "eyes"
REACTION_DECLINED = "confused"

# The third round summons a human whatever the check's colour. Measured on
# 2026-10-08 over every pull request the bot reviewed since 2026-09-21
# (gke-labs/kube-agents-bot#191): 38% of pull requests went five rounds or
# more, those took 86% of the review spend, the first human review arrived a
# median 11 hours and two rounds after round three, and once a human had
# reviewed, two thirds of pull requests saw no further round. The gate that
# waited for green kept the human away from exactly the pull requests that
# loop, because on 54% of third rounds the check is grey -- a High on code the
# last fix added, or the description note -- and an agent author answers a grey
# check with another fix and another `/review`. So a grey check at the bot's
# third distinct commit requests a reviewer too, once, and says so on the pull
# request. Three, not one: nearly half of pull requests finish in two rounds,
# and the first review is where the defects the author is about to fix sit.
#
# What counts as a round is a commit the bot has reviewed -- its reviews
# listed on the pull request, distinct by `commit_id`, so a `/review` on an
# unchanged commit (a re-cut) is not one. The deciding entry has to be a read:
# its summary ends in the bot's `ai-review:` tally with an outcome of
# `findings` or `clean`. The row a push gets carries the previous title and no
# tally, and a broken, conflicted or superseded entry carries another outcome;
# none of those is a round, and none clears the gate at any count.
AI_REVIEW_BOT_LOGIN = "kube-agents-bot[bot]"
AI_REVIEW_TALLY_PREFIX = "ai-review:"
AI_REVIEW_READ_OUTCOMES = frozenset({"findings", "clean"})
HANDOFF_ROUNDS = 3
# Every request this workflow makes is announced in one comment carrying this
# marker, and the marker is what makes the third-round request fire once per
# pull request: GitHub clears a review request as soon as the reviewer files
# any review, a `COMMENTED` one included, and `already_reviewed_reason` does
# not count those, so without it every later grey round would ask again.
HANDOFF_MARKER = "<!-- auto-request-review:handoff -->"
# The hand-off is posted with the job's GITHUB_TOKEN, so its author is always
# this login; a comment anyone else opens with the marker is not a hand-off,
# or the author's agent pasting one it saw elsewhere would switch the
# third-round rule off for the pull request.
HANDOFF_AUTHOR = "github-actions[bot]"
# Only a run under Actions posts the hand-off: its comment lands under the
# login above and is recognised later. A maintainer running the script by hand
# posts under their own login, which `handed_off` would never count, so that
# run announces nothing and says so, and the workflow's next request posts the
# comment that counts.
ACTIONS_ENV = "GITHUB_ACTIONS"

# A declined override is written where the person who typed it will see it:
# a warning annotation on the workflow run (the `::warning::` command goes to
# stdout) and the job's step summary, when Actions provides one.
WORKFLOW_WARNING_PREFIX = "::warning::"
STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

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
    """Refuse a config using a feature this port does not implement, or a
    robot_accounts entry that is not a list of bare GitHub logins."""
    robots = (config.get("options") or {}).get(ROBOT_ACCOUNTS_OPTION)
    if robots is not None:
        if not isinstance(robots, list):
            raise ValueError(f"options.{ROBOT_ACCOUNTS_OPTION} must be a list of GitHub logins, got {robots!r}")
        for login in robots:
            # A mapping (`- login: kyber775`) or a nested list is a shape mistake;
            # quoting is no remedy for it, so the message says the shape.
            if isinstance(login, (dict, list)):
                raise ValueError(
                    f"options.{ROBOT_ACCOUNTS_OPTION} entry {login!r} is a {type(login).__name__}, not a "
                    "login: write each entry as a bare login on its own line, `- kyber775`, "
                    "not a mapping or a list"
                )
            # Every other non-text value is a scalar yaml.safe_load coerced: an
            # all-digit login to an int, a date-shaped one to a date, `yes`, `no`,
            # `on`, `off`, `true` or `false` to a bool, `null`, `~` or an empty item
            # to None. Each is a valid login once quoted, and `007` would not
            # survive str() of the int, so the remedy is named rather than guessed.
            if not isinstance(login, str):
                raise ValueError(
                    f"options.{ROBOT_ACCOUNTS_OPTION} entry {login!r} was read by YAML as "
                    f"{type(login).__name__}, not text: quote a login YAML would otherwise read "
                    "as a number, a date, a boolean or null (`12345`, `2024-01-01`, `no`, "
                    "`null`), or remove an empty entry"
                )
            if not is_github_login(login):
                raise ValueError(
                    f"options.{ROBOT_ACCOUNTS_OPTION} entry {login!r} is not a bare GitHub login "
                    f"(a letter or digit, then letters, digits, hyphens or underscores, at most "
                    f"{GITHUB_LOGIN_MAX_LENGTH} characters, as GitHub shows them)"
                )
    for name, is_used in UNSUPPORTED_CONFIG.items():
        if is_used(config):
            raise ValueError(
                f"{name} is set, and scripts/request_reviewers.py does not implement it. "
                "Implement it here (and test it) before putting it in the config."
            )

    for pattern in (config.get("files") or {}):
        glob_to_regex(pattern)


def is_github_login(login):
    """Whether the text `login` could be a GitHub login: the length as a length, the shape as a pattern.

    Text only; validate_config has already refused anything YAML read as another type.
    """
    return len(login) <= GITHUB_LOGIN_MAX_LENGTH and GITHUB_LOGIN_RE.fullmatch(login) is not None


def robot_accounts(config):
    """The logins the roster lists as robots, lower-cased for comparison."""
    return frozenset(login.lower() for login in (config.get("options") or {}).get(ROBOT_ACCOUNTS_OPTION) or [])


# --------------------------------------------------------------------------- #
# OWNERS -- who can produce the `approved` and `lgtm` labels for the changed files
# --------------------------------------------------------------------------- #


def _read_yaml(path):
    """The mapping in `path`, or an empty one when the file is absent or empty."""
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} does not parse to a mapping")
    return loaded


def load_owners_aliases(root):
    """`OWNERS_ALIASES` as {alias: [logins]}, lower-cased like GitHub logins."""
    aliases = _read_yaml(pathlib.Path(root) / OWNERS_ALIASES_FILENAME).get("aliases") or {}
    return {
        str(name).lower(): [str(login).lower() for login in (members or [])]
        for name, members in aliases.items()
    }


def _expand_aliases(names, aliases):
    expanded = set()
    for name in names or []:
        name = str(name).lower()
        expanded.update(aliases.get(name, [name]))
    return expanded


def _owners_entries(owners_file, aliases, key):
    """The rules one OWNERS file declares under `key` (`approvers` or `reviewers`).

    Returns `(entries, no_parent_owners)`, each entry a `(regex, logins)` pair
    where a `None` regex applies to every path under the directory. As in
    Prow, a file with `filters:` is read as a filtered file and a top-level
    list beside them is ignored. `labels` and the rest of Prow's schema are
    ignored: the two lists are what decide the two merge labels.
    """
    owners = _read_yaml(owners_file)
    entries = []

    filters = owners.get("filters") or {}
    if filters:
        for pattern, rules in filters.items():
            logins = _expand_aliases((rules or {}).get(key), aliases)
            if logins:
                entries.append((re.compile(str(pattern)), logins))
    else:
        logins = _expand_aliases(owners.get(key), aliases)
        if logins:
            entries.append((None, logins))

    no_parent_owners = bool((owners.get("options") or {}).get("no_parent_owners"))
    return entries, no_parent_owners


def _logins_by_file(changed_files, root, key):
    """Prow's walk over one OWNERS list, as {changed file: logins}.

    `entriesForFile` in Prow's `repoowners` package: from the file's directory
    up to the repository root, collecting the logins of each OWNERS file whose
    rules cover the path, and stopping at a level that sets `no_parent_owners`
    once the file has collected any login at that level or below. Each list
    is walked on its own, so the stop applies to the list being read: a
    filtered file that names approvers and no reviewers stops the approver
    walk and lets the reviewer walk fall through to the parent.
    """
    root = pathlib.Path(root)
    aliases = load_owners_aliases(root)
    cache = {}
    by_file = {}

    for changed in changed_files:
        path = pathlib.PurePosixPath(changed)
        directory = path.parent
        collected = set()
        while True:
            if directory not in cache:
                cache[directory] = _owners_entries(root / directory / OWNERS_FILENAME, aliases, key)
            entries, no_parent_owners = cache[directory]

            relative = str(path.relative_to(directory))
            for regex, names in entries:
                if regex is None or regex.search(relative):
                    collected.update(names)

            if (collected and no_parent_owners) or directory == pathlib.PurePosixPath("."):
                break
            directory = directory.parent
        by_file[changed] = collected

    return by_file


def _applicable_logins(changed_files, root, key):
    """The union of `_logins_by_file` over the change."""
    return set().union(*_logins_by_file(changed_files, root, key).values())


def applicable_approvers(changed_files, root=DEFAULT_OWNERS_ROOT):
    """Every login whose approval clears some part of this change, lower-cased.

    The `approvers` walk: `hack/eval/presubmit-cases.txt` gets eval-crew alone
    while `hack/eval/nightly-cases.txt`, which matches nothing under `hack/`,
    falls through to the root. The union across files is the set whose verdict
    can produce the `approved` label on this pull request; nobody else's
    approval can, however real their review was.
    """
    return _applicable_logins(changed_files, root, OWNERS_APPROVERS_KEY)


def applicable_reviewers(changed_files, root=DEFAULT_OWNERS_ROOT):
    """Every login under `reviewers` for some part of this change, lower-cased.

    With `applicable_approvers`, the set whose "Approve" review Prow turns into
    `lgtm`; on its own it is nobody's `approved`.
    """
    return _applicable_logins(changed_files, root, OWNERS_REVIEWERS_KEY)


def approvers_by_file(changed_files, root=DEFAULT_OWNERS_ROOT):
    """`applicable_approvers` kept per changed file: one walk answers both the
    union and `self_approval_covers`."""
    return _logins_by_file(changed_files, root, OWNERS_APPROVERS_KEY)


def self_approval_covers(by_file, author):
    """Whether the author's implicit self-approval covers every changed file.

    Prow's `approved` is per file, so an approver's own pull request opens
    approved only where they are an approver: a root approver's README change
    is, the roster half of their mixed change is not. A pull request this
    returns True for needs `lgtm` alone, which anyone Prow lists as a reviewer
    can give; any other still needs an approver, so only an approver is asked.
    No changed files is no coverage: nothing is approved on open.
    """
    if not by_file:
        return False
    author = author.lower()
    return all(author in approvers for approvers in by_file.values())


def author_approves(changed_files, author, root=DEFAULT_OWNERS_ROOT):
    """`self_approval_covers` over a fresh walk of `root`."""
    return self_approval_covers(approvers_by_file(changed_files, root), author)


# --------------------------------------------------------------------------- #
# Glob matching -- the minimatch subset the config uses
# --------------------------------------------------------------------------- #


def glob_to_regex(pattern):
    """Translate a minimatch glob to a regex, dotfile rule included.

    minimatch runs with its defaults in the action, so `*` and `**` never match
    a path segment that starts with a dot. That is not a detail: `"**"` is the
    catch-all entry in this repository's config, and it does *not* cover
    `.github/workflows/...`. A dotfile path reaches a reviewer only through a
    literal entry naming it, or through the defaults when no glob matches.
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


def select_reviewers(config, changed_files, author, rng=random, restrict_to=None):
    """The full selection: globs, then defaults as fallback, then sampling.

    `restrict_to`, a set of lower-cased logins, narrows the pool before the
    draw: the OWNERS approvers for the change, when the author's own approval
    does not cover it, so that a non-approver in the pool is never asked for
    an `lgtm` that leaves `approved` outstanding with nobody asked. A pool the
    restriction empties is a config shape rather than a pull-request one --
    `main` declines before the draw when the approver set itself is empty, as
    it is for a pull request with no changed files -- and asking someone who
    can `lgtm` beats asking nobody, so it is kept whole and the log says so.
    """
    reviewers = reviewers_by_changed_files(config, changed_files, author)

    if not reviewers:
        reviewers = default_reviewers(config, author)
        if reviewers:
            log("No glob matched; falling back to the default reviewers")

    if restrict_to is not None and reviewers:
        narrowed = [name for name in reviewers if name.lower() in restrict_to]
        if narrowed:
            dropped = [name for name in reviewers if name not in narrowed]
            if dropped:
                log(f"Not drawing {', '.join(dropped)}: not an OWNERS approver for the change")
            reviewers = narrowed
        else:
            log(f"The pool is {', '.join(reviewers)} and none of them is an OWNERS approver for the change; drawing from it anyway")

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


def skip_reason(pull_request, config):
    """Why this pull request can take no reviewer request at all, or None.

    Every branch here is a *skip*, not a failure: the workflow fires on each
    completed `AI Review` check, so re-running on a pull request that has
    already been handed to a human is the normal case, not an error. These
    hold for `/request-review` too -- re-asking when someone is already
    requested is wrong however the run was started -- which is why the
    verdict check is a separate function that path leaves out.
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

    # A request outstanding to a listed robot is nobody asked: the robot answers
    # it and GitHub clears it, and nothing re-fires after that.
    robots = robot_accounts(config)
    requested = [
        user["login"] for user in pull_request.get("requested_reviewers") or [] if user["login"].lower() not in robots
    ]
    requested += [f"team:{team['slug']}" for team in pull_request.get("requested_teams") or []]
    if requested:
        return f"review is already requested from {', '.join(requested)}"

    return None


def already_reviewed_reason(pull_request, reviews, approvers, robots=frozenset()):
    """Why a verdict already on the pull request makes a request redundant, or None.

    Only a verdict from another person counts. Replying to a review thread
    files a `COMMENTED` review under the replier's name, and AGENTS.md tells
    authors to answer every finding before running `/review` -- so counting
    those would mean the pull requests that follow the process are exactly the
    ones that never get a reviewer.

    Each person's *latest* verdict is the one that counts, as GitHub itself
    reads them: the reviews list keeps every review ever filed, so someone who
    requested changes and later approved has both on record, and only the
    approval still means anything. A dismissed review comes back `DISMISSED`
    and so drops out on its own.

    An `APPROVED` counts only from one of `approvers`, the logins whose
    approval finishes this pull request: the OWNERS approvers for the changed
    files, since theirs is the approval that produces the `approved` label,
    widened by `main` to the OWNERS reviewers as well when the author's own
    approval already covers the change and `lgtm` is all it still needs.
    Anyone else's leaves the pull request unable to merge with nobody asked --
    three open pull requests sat that way behind one colleague's approvals.
    A `CHANGES_REQUESTED` counts from anyone: whoever
    filed it, the author owes them a reply, and requesting a fresh reviewer
    over an open objection is noise rather than progress. Bot reviews never
    count either way, and neither do reviews from the logins in `robots`: the
    roster's `options.robot_accounts`, robots that review under a user
    account and so read as `User` to the API.
    """
    author = ((pull_request.get("user") or {}).get("login") or "").lower()
    approvers = {login.lower() for login in approvers}
    robots = {login.lower() for login in robots}

    latest = {}
    for review in sorted(reviews, key=lambda review: review.get("submitted_at") or ""):
        user = review.get("user") or {}
        login = (user.get("login") or "").lower()
        if user.get("type") == "Bot" or login in robots or login == author or review.get("state") not in HUMAN_VERDICT_STATES:
            continue
        latest[login] = review

    humans = [
        review["user"]["login"]
        for login, review in latest.items()
        if review["state"] != APPROVED_STATE or login in approvers
    ]
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


def ai_review_tally(check_run):
    """The bot's tally on a finished `AI Review` entry, or None when it has none.

    The last non-empty line of the entry's summary, `ai-review: {...}`, as the
    bot documents it (gke-labs/kube-agents-bot, docs/design.md, "A tally a
    workflow can read"). An entry with no such line read nothing: the row a
    push gets, or a spinner. Anything that does not parse reads as no tally
    rather than as a guess at one.
    """
    summary = ((check_run or {}).get("output") or {}).get("summary") or ""
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if not lines or not lines[-1].startswith(AI_REVIEW_TALLY_PREFIX):
        return None
    try:
        parsed = json.loads(lines[-1][len(AI_REVIEW_TALLY_PREFIX) :])
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("outcome"), str):
        return None
    return parsed


def reviewed_commits(reviews, *head_shas):
    """The distinct commits the bot has reviewed on a pull request.

    `reviews` is the pull request's reviews listing; only the bot's own count,
    by `commit_id`, so a re-cut of a commit already read is the same round. The
    `head_shas` given are counted too: the deciding entry's commit is a round
    even when its review has not reached the listing by the time the check
    completes.
    """
    commits = {sha for sha in head_shas if sha}
    for review in reviews:
        user = review.get("user") or {}
        if user.get("login") == AI_REVIEW_BOT_LOGIN and review.get("commit_id"):
            commits.add(review["commit_id"])
    return commits


def handed_off(comments):
    """Whether this workflow has already announced a reviewer on the pull request.

    The marker has to open the comment and the comment has to be the
    workflow's own (`HANDOFF_AUTHOR`): a reply quoting the hand-off carries the
    same bytes further down, and anyone can type the marker, which renders as
    nothing.
    """
    return any(
        ((comment.get("user") or {}).get("login") == HANDOFF_AUTHOR)
        and (comment.get("body") or "").lstrip().startswith(HANDOFF_MARKER)
        for comment in comments
    )


def handoff_comment(users, teams, head_sha, check_run, why):
    """The one comment a request leaves behind, for the author and the reviewer.

    No line may start with a slash: Prow reads a command at the start of any
    line of any comment, and the author's agent reads the whole thing as the
    rule it is to follow from here.
    """
    names = [f"@{login}" for login in users] + [f"team {slug}" for slug in teams]
    title = ((check_run or {}).get("output") or {}).get("title")
    verdict = f'; the `{AI_REVIEW_CHECK_NAME}` check reads "{title}"' if title else ""
    return (
        f"{HANDOFF_MARKER}\n"
        f"Handed to {', '.join(names)} at `{head_sha[:7]}`: {why}{verdict}.\n\n"
        "The reviewer decides from here. Author: if a 🔴 High is open, push the fix and say so in its "
        "thread; otherwise reply in each thread. Then wait. A further `/review` spends a round nobody "
        "asked for; the reviewer's reply, or a `/review` they type, is what resumes you."
    )


def ai_review_block_reason(check_run, author_is_bot, rounds=None, already_handed_off=False):
    """Why the AI review does not clear this pull request, or None.

    A bot cannot read its own findings and comment `/review`, so a pull request
    Dependabot opened passes on any completed conclusion. A human author has to
    get it to `success`, or comment `/request-review` to override -- or reach
    the bot's third round: with `rounds` given, a grey entry that is a read
    (`ai_review_tally`) clears the gate once `rounds` is `HANDOFF_ROUNDS` or
    more and no hand-off has been announced yet. Without `rounds` the rule is
    the first one alone.
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
    reason = f"{AI_REVIEW_CHECK_NAME} concluded {conclusion}{detail}, not success"
    if rounds is None:
        return reason

    tally = ai_review_tally(check_run)
    is_read = tally is not None and tally.get("outcome") in AI_REVIEW_READ_OUTCOMES
    if is_read and rounds >= HANDOFF_ROUNDS and not already_handed_off:
        log(
            f"{AI_REVIEW_CHECK_NAME} concluded {conclusion}{detail} at the bot's round {rounds}; "
            "requesting a human whatever the colour"
        )
        return None
    if already_handed_off:
        return f"{reason}, and a reviewer was already handed this pull request"
    if not is_read:
        return f"{reason}, and the deciding entry is not a review the bot finished"
    return f"{reason} (round {rounds} of the {HANDOFF_ROUNDS} before a human is asked anyway)"


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
        help="only request a reviewer once the AI Review check passed, or the bot has reviewed three commits",
    )
    parser.add_argument(
        "--owners-root",
        default=DEFAULT_OWNERS_ROOT,
        help="checkout whose OWNERS files decide who counts as a reviewer (default: the working directory)",
    )
    parser.add_argument(
        "--react-to",
        type=int,
        help=(
            "issue comment id of the /request-review that asked for this: a person overriding, "
            "so a verdict already on the pull request does not stop the request; "
            f"reacts {REACTION_ACKNOWLEDGED} when one is made and {REACTION_DECLINED} when it is declined"
        ),
    )
    parser.add_argument("--seed", type=int, help="seed the reviewer sampling, for reproducible runs")
    parser.add_argument("--dry-run", action="store_true", help="print what would be requested or reacted")
    return parser.parse_args(argv)


def react(api, args, content):
    """Put `content` on the `/request-review` comment, if this run has one."""
    if not args.react_to:
        return
    if args.dry_run:
        log(f"--dry-run: would react {content} to comment {args.react_to}")
        return
    api.post(f"/repos/{args.repo}/issues/comments/{args.react_to}/reactions", {"content": content})


def decline(api, args, reason):
    """Record that no reviewer was requested, and why.

    On the check-run trigger that is a log line and nothing more: it is the
    common case, dozens of times a day. On `/request-review` it is a person
    being told no, so it also goes on the run as a warning annotation and in
    the step summary, and the comment gets a 😕 -- an override that exits
    green with no trace is indistinguishable from one that never ran.
    """
    message = f"Not requesting a reviewer: {reason}"
    log(message)
    if not args.react_to:
        return
    print(f"{WORKFLOW_WARNING_PREFIX}{message}", flush=True)
    summary_path = os.environ.get(STEP_SUMMARY_ENV)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(f"{message}\n")
    react(api, args, REACTION_DECLINED)


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    config = load_config(args.config)
    api = GitHubAPI(args.repo, token, user_agent="kube-agents-request-reviewers")

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

    reason = skip_reason(pull_request, config)
    if reason:
        decline(api, args, reason)
        return 0

    changed_files = [
        entry["filename"] for entry in api.get_all(f"/repos/{args.repo}/pulls/{number}/files")
    ]

    by_file = approvers_by_file(changed_files, args.owners_root)
    approvers = set().union(*by_file.values())
    log(f"OWNERS approvers for the changed files: {', '.join(sorted(approvers)) or 'none'}")
    # Whether the pull request opened with `approved` already on it (#1075)
    # decides both what counts as reviewed and who is drawn: needing lgtm
    # alone, anyone Prow takes an lgtm from will do; otherwise only an
    # approver's review finishes it, so only an approver is asked.
    self_approved = self_approval_covers(by_file, author)
    if self_approved:
        log(f"{author}'s own approval covers every changed file; lgtm is all it needs")
    else:
        log(f"{author}'s own approval does not cover the change; only an approver is drawn")
    # Nobody to narrow to -- a pull request with no changed files, where the
    # walk returns nothing and nothing is self-approved. Narrowing to an empty
    # set would fall back to the whole pool and could hand a non-approver an
    # `approved` nobody can give, so nobody is asked, on either path.
    if not self_approved and not approvers:
        decline(api, args, "no OWNERS approver covers the change")
        return 0

    reviews = None
    comments = None

    def pull_request_comments():
        nonlocal comments
        if comments is None:
            comments = api.get_all(f"/repos/{args.repo}/issues/{number}/comments")
        return comments

    # `/request-review` is a person who has read the pull request saying "ask
    # someone anyway", so a verdict already on it does not decide for them.
    if not args.react_to:
        counting = approvers
        if self_approved:
            counting = approvers | applicable_reviewers(changed_files, args.owners_root)
        robots = robot_accounts(config)
        # Named in the run log because the list can only be checked for shape,
        # not identity: a well-formed login that names no account sits inert,
        # and this line is what shows which logins were in effect.
        log(f"Robot accounts, whose reviews never count: {', '.join(sorted(robots)) or 'none'}")
        reviews = api.get_all(f"/repos/{args.repo}/pulls/{number}/reviews")
        reason = already_reviewed_reason(pull_request, reviews, counting, robots)
        if reason:
            decline(api, args, reason)
            return 0

    deciding = None
    why = "a person asked for a reviewer with /request-review" if args.react_to else "a maintainer ran the request by hand"
    if args.require_ai_review_pass:
        deciding = gate_check_run(api, pull_request, triggering_check_run)
        author_is_bot = pull_request["user"].get("type") == "Bot"
        reason = ai_review_block_reason(deciding, author_is_bot)
        why = "the check is green" if (deciding or {}).get("conclusion") == "success" else "the author is a bot"
        if reason and not author_is_bot and deciding is not None and deciding.get("status") == "completed":
            # Only now, and only on the grey path: the comments listing, which
            # the green path needs only at the announce (the reviews listing is
            # already in hand unless `--react-to` skipped it). A spinner or a
            # head with no entry cannot clear whatever the count. The deciding
            # entry's own commit counts as reviewed.
            if reviews is None:
                reviews = api.get_all(f"/repos/{args.repo}/pulls/{number}/reviews")
            rounds = len(reviewed_commits(reviews, deciding.get("head_sha")))
            reason = ai_review_block_reason(
                deciding, author_is_bot, rounds=rounds, already_handed_off=handed_off(pull_request_comments())
            )
            why = f"the bot has reviewed {rounds} commits and the check is still not green"
        if reason:
            decline(api, args, reason)
            return 0

    rng = random.Random(args.seed) if args.seed is not None else random
    reviewers = select_reviewers(config, changed_files, author, rng=rng, restrict_to=None if self_approved else approvers)

    if not reviewers:
        decline(api, args, "no reviewer matched")
        return 0

    users, teams = split_teams(reviewers)
    log(f"Requesting review from {', '.join(reviewers)}")

    # The listing decides only decoration here, so it cannot cost the request:
    # on the grey path it was already read, and fatally, to decide the gate.
    try:
        already_announced = handed_off(pull_request_comments())
    except Exception as exc:  # noqa: BLE001 - any API error; a duplicate comment is the worse-case cost
        log(f"could not list the comments on #{number} to look for an earlier hand-off: {exc}; announcing anyway")
        already_announced = False
    announce = not already_announced
    if announce and os.environ.get(ACTIONS_ENV) != "true":
        log(
            f"not announcing the hand-off on #{number}: not running as the workflow, so the comment "
            "would land under another login and never be recognised; the workflow's next request posts it"
        )
        announce = False
    if announce and deciding is None:
        # The override path consulted no entry; the comment still names what
        # the check reads, if it reads anything, for the reviewer it summons.
        # Decoration: a failure here costs the clause, never the request.
        try:
            deciding = gate_check_run(api, pull_request, triggering_check_run)
        except Exception as exc:  # noqa: BLE001 - any API error; the request still goes out
            log(f"could not read the head's {AI_REVIEW_CHECK_NAME} entry for the hand-off: {exc}")
            deciding = None
    if args.dry_run:
        log(f"--dry-run: would POST reviewers={users} team_reviewers={teams} to #{number}")
        if announce:
            log(f"--dry-run: would post the hand-off comment on #{number}")
        react(api, args, REACTION_ACKNOWLEDGED)
        return 0

    api.post(
        f"/repos/{args.repo}/pulls/{number}/requested_reviewers",
        {"reviewers": users, "team_reviewers": teams},
    )
    # Once per pull request, whichever path requested: the marker is the
    # record the third-round rule reads, and the text is the author's stop.
    if announce:
        # The commit named is the one the quoted verdict is on: the deciding
        # entry's, which is the head except in the seconds between a push and
        # the bot's row for it. The request already went out: a comment that
        # fails to post is logged, never a red run, and the next request on
        # the pull request posts it.
        at = (deciding or {}).get("head_sha") or pull_request["head"]["sha"]
        try:
            api.post(
                f"/repos/{args.repo}/issues/{number}/comments",
                {"body": handoff_comment(users, teams, at, deciding, why)},
            )
        except Exception as exc:  # noqa: BLE001 - any API error; the request stands
            log(f"{WORKFLOW_WARNING_PREFIX}requested a reviewer on #{number} but could not post the hand-off comment: {exc}")
    react(api, args, REACTION_ACKNOWLEDGED)

    return 0


if __name__ == "__main__":
    sys.exit(main())
