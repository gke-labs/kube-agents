"""Group B -- The write path.

B1  No agent principal holds a credential that can cause a production change.
B2  Assent is human or policy. Agents get veto only.
B3  The enforcement substrate is not agent-authorable.
B4  The executor is a governed principal.
B5  What the approver sees is what will be applied.
B6  No self-approval, and no agent satisfies a required review.

Agents are read-only against clusters and propose changes as pull requests, so
the ceiling this group tests is a ceiling on `kubectl` -- not a ceiling on
impact. An invariant set that only governs API calls governs the wrong API,
which is why most of what follows reads workflows and manifests rather than
argv.
"""

from __future__ import annotations

import re
import sys
import unittest

import yaml

from . import _harness as h
from ._harness import command_policy

_WORKFLOWS = sorted(
    # Both extensions: GitHub Actions accepts .yaml too, and a workflow added
    # as .yaml would otherwise escape every assertion over this set silently.
    (h.REPO_ROOT / ".github" / "workflows").glob("*.y*ml")
)

# A `run:` step reaches the pull request's head without the checkout action.
# `git fetch origin pull/N/head` is GitHub's own documented way to do it by
# hand, and a `ref:` assertion on `uses:` steps never sees it.
#
# Three alternatives, and the first carries the rule. `refs/pull` is the
# remote namespace every one of these spellings resolves inside, and no step
# of a workflow that must not have the fork's code has business naming it --
# so it is refused whatever follows, rather than only when `head` or `merge`
# does. The two named refs were the whole pattern until 2026-09-19 and three
# spellings walked past them, each reaching the same code:
# `+refs/pull/${N}/*:refs/remotes/pr/*` with the `pr/head` checkout on the
# next line, a backslash continuation splitting `refs/pull/${N}/` from
# `head`, and `git ls-remote origin "refs/pull/${N}/h*"` for a SHA to fetch
# by. Answering those with three more named refs is the shape that keeps
# losing here; naming the namespace is not.
#
# The second alternative is the short form, which needs no `refs/` prefix at
# all: `git fetch origin pull/N/head` is what GitHub's own documentation
# gives. The third is that short form globbed, which `git ls-remote` accepts
# -- `pull/N/h*` names `refs/pull/N/head` without spelling it -- and which
# stops deliberately short of `pull/N/anything`, because `.../pull/1781` is a
# link to a pull request's web page and a workflow that comments on one has
# every reason to write it down.
#
# What the first alternative concedes is a false red on a step that writes
# `refs/pull/` without fetching it: a comment explaining why this workflow
# does not do that, most likely. That is a line of review on a trigger whose
# job holds a writable token, which is the price this half pays everywhere
# else too.
_PULL_REQUEST_REF = re.compile(
    # `[^\n]*?` rather than a tighter class: the ref is interpolated, and
    # `${{ github.event.number }}` has spaces in it.
    r"refs/pull\b"
    r"|pull/[^\n]*?/(?:head|merge)\b"
    r"|pull/[^\n]*?/[^\s'\"]*\*"
)
# The three spellings of the pull request's head that have turned up in a
# workflow here. They are a backstop rather than the rule: what governs a
# step is the allowlist below, and these three are for the text that carries
# no `${{ ... }}` for that allowlist to read -- a JavaScript property path, a
# ref written into a shell string. Deliberately not extended.
# `github.event.before` and `github.event.pull_request.merge_commit_sha` name
# the same code, `${{ GITHUB.EVENT.AFTER }}` is a second spelling of the third
# alternative because expressions are case-insensitive, and answering each of
# those with one more alternative is the shape the allowlist replaced.
_PULL_REQUEST_HEAD = re.compile(
    r"pull_request\.head|github\.head_ref|github\.event\.after"
)

# Any `${{ ... }}` an interpolated value carries. `re.DOTALL` because `.` is
# otherwise blind to a newline and an expression is allowed to contain one: a
# block scalar keeps `${{ format('{0}',` / `github.event.after) }}` exactly as
# written, newline and all. Without the flag `findall` returned nothing for
# that ref -- so the allowlist loop below never ran, `sub` removed nothing,
# and the whole expression reached the literal scan as text, where
# `github.event.after` contains none of `pull`, `head` or `merge`. One missing
# flag, open in both directions, and the reason the ref half now also refuses
# a `$` left behind in the residue.
_EXPRESSION = re.compile(r"\$\{\{\s*(.+?)\s*\}\}", re.DOTALL)

# An index into a context, which is the other way GitHub spells a property
# access: `github.event.pull_request['head'].sha` and
# `github.event.pull_request.head.sha` are the same expression and only the
# second one looks like it. The subscript has to be a literal string. One
# expression indexed by another is left as it stands, which keeps it off every
# allowlist here, which is the answer this file gives a ref it cannot resolve.
_EXPRESSION_INDEX = re.compile(r"""\[\s*(?:'([^']*)'|"([^"]*)")\s*\]""")

# The expressions a `pull_request_target` checkout ref may name. This is an
# allowlist because the denylist it replaces could not work: it looked for
# `pull_request`, `head` and `merge`, and `${{ github.event.after }}` is the
# pull request's head SHA spelled with none of them. One line in a workflow
# and the whole test went quiet. Listing the safe refs instead means an
# indirection this file cannot resolve -- `env.X`, `steps.X.outputs.Y`, a
# value laundered through `$GITHUB_ENV` -- reads as unsafe rather than as
# innocent text, which is the direction to be wrong in.
#
# One entry, and deliberately: see the test's docstring for what each of the
# three omitted spellings costs, because the three are not one argument.
# `github.ref` and `github.sha` have been synonyms for the entry below since
# GitHub moved this trigger's ref to the default branch on 2025-12-08, so
# leaving them off is a one-spelling rule rather than a safety one.
# `github.base_ref` is the pull request's base branch, which its author picks
# from the branches that already exist here -- a stale unprotected one is not
# the default branch. No fork can write any of the three anyway; that needs
# push access here. So this list is about the author of the pull request
# rather than about the fork, and costs nothing today.
_SAFE_CHECKOUT_EXPRESSIONS = frozenset({"github.event.repository.default_branch"})

# The same treatment for `repository:`, because a ref is resolved *inside* a
# repository and `ref:` alone therefore does not say what gets checked out.
# Absent is the safe case -- the default is the workflow's own repository --
# and `github.repository` is that default spelled out. Why this half refuses a
# literal outright where the `ref:` half tolerates one is argued in the test's
# docstring, which is where the `_SAFE_CHECKOUT_EXPRESSIONS` argument lives
# too; this is the list.
_SAFE_CHECKOUT_REPOSITORIES = frozenset({"github.repository"})

# The expressions any step of a `pull_request_target` workflow may name, in
# its script or anywhere in its `env:`. The refs are the checkout list above,
# because a `git fetch` resolves a ref the same way the action does and there
# is no reason for the two halves to disagree about which refs are safe. The
# rest are not refs at all, and each is here because a carrier in this
# repository names it today. A step authenticates with a token, spelled
# either of the two ways GitHub spells it, and names a remote. A workflow
# that labels or comments on the pull request needs its number, which GitHub
# types as an integer -- the fork chooses which number, not what a number
# is, so it cannot be a ref, a path or a command. `inputs.dry_run` is a
# `workflow_dispatch` boolean, which only somebody who can already push here
# is able to set, and is not the pull request's at all.
#
# The pull request's number is a concession and should be read as one. It is
# the only entry here that is not independent of the pull request -- it
# identifies it -- and with it and the token in the same step, the head is one
# `gh api repos/O/R/pulls/$N --jq .head.sha` away. Three shapes went green
# when this entry was added, measured rather than inferred: that command, `gh
# pr checkout "$PR_NUMBER"`, and `gh pr view --json headRefOid`. All three
# are refused again by the two rules at `_PULL_REQUEST_API`: a `/pulls`
# request is refused as a path, and `gh pr` is an allowlist of two write
# subcommands, so the labelling the carriers do with the number is untouched
# and everything that returns the pull request is not. The concession is made because two
# carriers label and comment with the number, and there is no way to tell that
# use from the other one without reading the program, which is the thing this
# file declines to do. What the number does *not* open is the direct path: `pull/N/head` built out
# of it is refused by `_PULL_REQUEST_REF` over every step, whatever is on this
# list.
#
# Anything else -- another event field, a `steps.X.outputs.Y`, an `env.X`
# that did not resolve -- reads as unsafe, which is the direction the ref
# half is wrong in and for the same reason.
_SAFE_SCRIPT_EXPRESSIONS = _SAFE_CHECKOUT_EXPRESSIONS | {
    "github.repository",
    "github.token",
    "secrets.github_token",
    "github.event.pull_request.number",
    "inputs.dry_run",
}

# The JavaScript half of the same rule. `actions/github-script` hands its
# script the whole webhook payload as `context`, so `context.payload.after` is
# `${{ github.event.after }}` written in a language `_EXPRESSION` cannot see
# and `_SAFE_SCRIPT_EXPRESSIONS` therefore never reads. The accessor is
# optional in this pattern on purpose: a bare `context` is the payload too,
# and a script that serialises it whole and picks a field out of the result
# names no property anywhere a regex could find one.
_SCRIPT_CONTEXT = re.compile(
    r"""\bcontext\b(?:\s*\.\s*(\w+)|\s*\[\s*['"](\w+)['"]\s*\])?"""
)
# The fourth language. The allowlists above read expressions and `context`
# properties, and the rule below reads the payload off disk, but the head is
# also an HTTP call away. `gh pr checkout "$N"` does the fetch and the
# checkout in one word, and `gh api repos/O/R/pulls/$N --jq .head.sha` hands
# back the same SHA the event carries. Neither names a refused expression,
# because the only thing either needs from the event is the number, and the
# number is on the allowlist so that the two carriers that label with it stay
# green.
#
# This was a backstop over three reading subcommands and a `/pulls/` path
# until 2026-09-19, and it lost the way every denylist in this file has lost.
# `gh pr list --json headRefOid` returns the head and `list` was not one of
# the three. `gh api "repos/O/R/pulls?state=all"` is the collection endpoint,
# which carries no trailing slash for `/pulls/` to match. And `gh pr -R
# "$REPO" checkout "$N"` *is* `gh pr checkout` -- the CLI strips flags before
# it resolves the subcommand -- but the pattern wanted the verb immediately
# after `pr`. Adding `list` would have left `status`, `ready --json` and
# whatever GitHub ships next.
#
# So `gh pr` is an allowlist now, walked by `_unsafe_gh_pull_request_commands`
# rather than matched by a regex, because "the first word after any number of
# interposed flags" is a walk and not a pattern. Two subcommands are on it and
# both are writes: `edit`, which `auto-assign-milestone.yml` runs to set a
# milestone, and `comment`, which is the neighbouring write a labelling
# carrier reaches for next and which returns nothing about the head. Every
# other subcommand under `gh pr` -- `checkout`, `diff`, `view`, `list`,
# `status`, and the one after that -- is refused for not being on the list
# rather than for being recognised, which is the direction to be wrong in.
#
# What the walk concedes is written at the function. What the *list* concedes
# is one line of review the day a carrier needs a third subcommand, which is
# the same price `_SAFE_SCRIPT_EXPRESSIONS` charges and the same answer.
_SAFE_GH_PULL_REQUEST_SUBCOMMANDS = frozenset({"edit", "comment"})

# The allowlist one noun further up, and the round-9 half of the same
# argument. Until 2026-09-19 the walk read the first word, skipped every
# invocation whose verb was not `pr`, and so governed one verb of a CLI that
# has about forty. `gh alias set co 'pr checkout'` followed by `gh co "$N"` is
# `gh pr checkout` with the verb renamed, and neither line is a `gh pr` for the
# subcommand allowlist to read -- the first is `alias`, the second is a word
# GitHub never shipped. Adding `alias` by name would leave `gh extension
# install`, `gh repo sync`, and whatever the next release adds; a list of the
# ones that have been seen is the shape that keeps losing here.
#
# So the verb is an allowlist too. Three are on it and each has a carrier or a
# reason. `pr` is `auto-assign-milestone.yml`'s milestone write, and its
# subcommands stay governed by the list above. `issue` is the same object: a
# pull request *is* an issue to GitHub's data model, `gh issue comment 1781`
# comments on this one, and a labelling carrier reaching for it would be doing
# exactly what the `pr` carrier does -- so it is governed by the same
# subcommand allowlist rather than trusted, which is what keeps `gh issue
# view` off it. `api` takes no subcommand at all; what governs it is the path
# it requests, and `_PULL_REQUEST_API` reads that out of the whole script text
# whoever makes the request.
#
# Everything else -- `alias`, `extension`, `repo`, `run`, `release`, `search`,
# `workflow`, and the one after that -- is refused for not being on the list.
# That kills the alias shape permanently rather than by name: a verb this file
# has never heard of is refused *because* it has never heard of it, so the
# rename has nowhere to land. What it concedes is a line of review the day a
# carrier needs a fourth verb, which is the price `_SAFE_SCRIPT_EXPRESSIONS`
# and `_SAFE_GH_PULL_REQUEST_SUBCOMMANDS` both charge, and the same answer.
_SAFE_GH_VERBS = frozenset({"pr", "issue", "api"})

# Which of those verbs the subcommand allowlist is read against. `api` is not
# one: it is a path rather than a verb tree, and reading `repos/...` as a
# subcommand would refuse the live milestone carrier while telling its author
# to allowlist a URL.
_SUBCOMMAND_GOVERNED_GH_VERBS = frozenset({"pr", "issue"})

# `gh` as a command word. The lookbehind refuses a word ending in `gh` and a
# flag spelled `--gh`, and deliberately allows a path prefix, because
# `/usr/bin/gh` is the same program.
#
# The lookahead was `\s` until 2026-09-19, and a quote is what walked past it.
# `'gh' pr checkout "$N"` is what a shell runs when the program name is
# quoted -- quoting a bare word changes nothing about what executes -- and
# `gh'' pr checkout "$N"` is the same trick with the quotes empty. Neither is
# `gh` followed by whitespace, so neither invocation was ever handed to the
# walk at all. The third spelling is not an obfuscation: `await exec.exec('gh',
# ['pr', 'checkout', N])` in an `actions/github-script` body is the ordinary
# way to run a program from a `script:` input, `_step_scripts` already folds
# that input into the text read here, and the character after `gh` is a quote
# followed by a comma.
#
# So the lookahead is whitespace, either quote, or a comma. It costs a false
# red on the two letters `gh` written as prose immediately before a quote or a
# comma inside a step -- "install gh, then run it" -- which is refused because
# the word after it is not an allowlisted verb. That is a line of review on a
# comment in a step of a workflow holding a writable token, which is the
# direction to be wrong in.
_GH_COMMAND = re.compile(r"(?<![\w-])gh(?=[\s'\",])")

# The same walk anchored one word later, and the answer to the thing a regex
# over a program name cannot do. Every rule above keys on the literal word
# `gh`, and a shell has unlimited ways to spell a program: `echo "pr checkout
# $N" | xargs gh` puts the arguments on the far side of a pipe, `"$(command -v
# gh)" pr checkout "$N"` is how four of this repository's own
# `scripts/release/*.sh` name a program and puts a `)` where the lookahead
# wants a quote, `GH=gh` then `"$GH" pr checkout "$N"` moves the name into a
# variable, and `g'h' pr checkout "$N"` executes `gh` while containing the
# substring nowhere. All four were live and green. Identifying a program name
# in shell text is not a thing a regex does, and the next spelling is free.
#
# So this rule is about the *argument shape* instead: the word `pr` followed
# by a subcommand, refused anywhere in a step whatever ran it. The walk is the
# same one `_gh_invocations` uses, started at the `pr` rather than at the
# program, so it reads the subcommand through the same interposed flags and
# the same argv-vector punctuation -- `gh pr -R "$REPO" edit` still resolves to
# `edit`, and `['pr', 'checkout', N]` still resolves to `checkout`. What it is
# held against is `_SAFE_GH_PULL_REQUEST_SUBCOMMANDS`, the list the `gh pr`
# walk already uses, so a subcommand GitHub ships next is refused here for the
# same reason it is refused there.
#
# Read it as a backstop *under* the verb allowlist rather than as the rule.
# The allowlist is what makes a verb or a subcommand nobody has heard of
# refused by default; this catches the invocation the allowlist never sees
# because the program word was unreadable. Keeping both matters: this one
# cannot see `gh api repos/O/R/pulls/$N`, which has no `pr` word in it, and
# the allowlist cannot see any of the four spellings above.
#
# The lookbehind is what keeps the live carriers green: `--pr "$PR_NUMBER"` is
# how `risk_classify.yml` passes the number to its script, and a flag is not a
# command word. The match is case-sensitive because `gh` is -- `gh PR
# checkout` is an unknown command -- which is also what keeps
# `auto-assign-milestone.yml`'s `echo "PR #${PR_NUMBER} was merged"` from
# reading as an invocation.
_GH_PULL_REQUEST_WORD = re.compile(r"(?<![\w-])pr(?=[\s'\",])")

# And what keeps the backstop under the allowlist rather than over it. A rule
# on argument shape alone refuses `./tools/high pr checkout 42`, which runs a
# program in this repository that is not `gh` and cannot reach GitHub, so the
# shape is only half the rule: the other half is that the program word was
# one review could not read. `"$GH"`, `g'h'` and `"$(command -v gh)"` are
# each a name assembled out of something -- a variable, quoting, a
# substitution -- and a name assembled out of something is a name this file
# has already lost. A word made only of the characters a path is made of is a
# name review can read, and for the one such name that reaches GitHub the
# verb allowlist above is the rule.
#
# This is the same trade the arguments make in the other direction. `xargs
# gh` is refused for having no readable arguments while naming the program;
# these are refused for having no readable program while naming the
# arguments. Something has to be readable.
#
# It costs `hub pr checkout` and any other client with `gh`'s argument shape
# and a plain name of its own -- a hole this leaves open deliberately rather
# than one it does not know about, because closing it means a denylist of
# program names, which is the thing this rule exists to stop needing. It is
# also what keeps `is:pr` inside a `gh api search/issues` query green: the
# `:` is not a word character so the lookbehind matches, but `gh` is a name,
# and the query is refused by its path anyway.
_READABLE_PROGRAM = re.compile(r"\A[\w./@-]+\Z")

# What comes off a word before the walk reads it. Quotes were already stripped
# because `gh pr "edit"` runs `edit`; the brackets and the comma are the
# round-9 half, and without them widening the lookahead above buys nothing.
# `exec.exec('gh', ['pr', 'checkout', N])` splits on whitespace into `',`,
# `['pr',`, `'checkout',` -- so the verb reads as `['pr'`, which is not `pr`,
# and the invocation is skipped exactly as it was before. Stripping this
# punctuation off both ends of every word, and dropping whatever that empties,
# is what makes an argument vector read as the command line it is. It is a
# strip rather than a parse, so a word is never split and a quoted string with
# a space in it still arrives as two words; that can only produce more words
# than the CLI sees, never fewer, and an extra word reads as an unrecognised
# subcommand rather than as an allowlisted one.
_GH_WORD_PUNCTUATION = "\"'[],"

# Where the walk stops. Not a shell parser: a separator inside a quoted
# string ends an invocation early, which can lose the tail of one and cannot
# invent one.
_COMMAND_END = re.compile(r"[\n;|&()`]")
# `-R`/`--repo` is the flag worth knowing about by name. The walk drops
# `-`-prefixed words, but a flag's *value* is an ordinary word, so a bare drop
# reads `gh pr -R owner/repo edit` as the subcommand `owner/repo` and refuses a
# legitimate label write -- and tells the reader to allowlist a repository
# name, which is the wrong advice in the wrong place. Skipping the value fixes
# the diagnostic; `--repo=owner/repo` needs no entry here because the `=` form
# is one word and the `-` prefix already drops it. Any other value-taking flag
# left off this set costs a false red, never a false green, which is the
# direction to be wrong in.
#
# An abbreviation needs no entry either, which is worth writing down because
# the same question has a different answer for git. `gh` is built on a parser
# that refuses a prefix outright -- `gh pr edit --rep owner/repo` is `unknown
# flag: --rep`, measured against gh 2.101 -- so there is no `--rep` for the
# walk to mis-read as a subcommand, and no ladder to write here or at the
# `--approve` pattern in B2. Git is the opposite and `_REMOTE_REF_ENUMERATION`
# carries the consequence.
_GH_VALUE_FLAGS = frozenset({"-R", "--repo"})
# A workflow expression holds spaces -- `${{ github.repository }}` is three
# words to `split()` and one word to the runner, which substitutes it before
# the shell ever sees it. Collapsing each to a single token is what makes the
# flag-value skip above land on the value rather than partway through it.
_GH_EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")

# The two halves a regex still answers. `/pulls` as a path segment is the
# same request spelled as a URL, whether `gh api` or `curl` makes it, and it
# is refused whatever follows -- `/pulls/123`, `/pulls?state=all`, `/pulls`
# bare -- because the collection endpoint hands back the same heads the item
# endpoint does. `/pull/1781` is a link to a pull request's web page rather
# than an API path and is not matched.
#
# And the client library, which is that endpoint reached from a `script:`.
# `actions/github-script` hands the step an authenticated Octokit as
# `github`, so `github.rest.pulls.get({...context.repo, pull_number: N})`
# returns the pull request while naming no `gh`, no `/pulls` path and no
# refused expression -- `context.repo` is on `_SAFE_SCRIPT_CONTEXTS` and the
# number is on `_SAFE_SCRIPT_EXPRESSIONS`. The refusal is over the `pulls`
# namespace rather than over its methods, which is the allowlist move one
# noun up: `get`, `list`, `listFiles` and `listCommits` are four ways to ask
# the same endpoint the same question. It concedes a false red on a shell
# path that happens to contain `pulls.` or `/pulls`, which nothing here has.
#
# The web endpoint is the third alternative, and it is the one that needs no
# token at all. `https://github.com/O/R/pull/N.diff` and its `.patch` sibling
# are the fork's changes served off the pull request's *web* page: `curl -fsSL
# ".../pull/${N}.diff" | git apply` puts them on the default-branch checkout
# and `.patch` does it through `git am` with the authorship attached. Neither
# is `/pulls`, so the path rule above never read one; neither names `refs/pull`
# or a `/head` segment, so `_PULL_REQUEST_REF` never did either; and because
# these are public URLs, the request succeeds whether or not the step has the
# token, which makes them the cheapest reach on this list rather than the most
# exotic. The rule is the `pull/` web path *with a diff extension*, so the
# concession the ref half made on purpose stays made -- a bare
# `https://github.com/gke-labs/kube-agents/pull/1781` in a comment is a link
# to a page and a workflow that comments on one has every reason to write it
# down. `[^\n'\"]*?` rather than a tighter class because the number is
# usually interpolated and `${{ github.event.number }}` has spaces in it; it
# stops at a line end and at either quote, so the match cannot wander out of
# the URL it started in.
#
# The fourth is `issues`, closed as a namespace rather than as a list of
# endpoints, and this is the round-10 correction to a comment that was simply
# false. It read `/issues/N/timeline` and `/issues/N/events` and said the two
# "are the only issues endpoints that return a commit rather than a comment, a
# label or a title". `GET /repos/O/R/issues/N` -- the item endpoint, the
# shortest URL in the namespace -- hands back `pull_request: {url, html_url,
# diff_url, patch_url}` for any issue that is a pull request, because a pull
# request *is* an issue to this API. So `curl -sL "$(gh api
# "repos/$REPO/issues/$N" --jq .pull_request.patch_url)" | git am` puts the
# fork's code on the runner while naming no `/pulls`, no `pull/N.diff`, no
# refused expression and an allowlisted `gh` verb. `--jq .pull_request.url`
# gives the `/pulls` URL to pass straight back to `gh api`,
# `github.rest.issues.get` is the same field from a `script:`, and
# `search/issues?q=repo:$REPO+is:pr` returns every open pull request's
# `patch_url` without needing a number at all. Four shapes, one endpoint
# family, and answering them with four more alternatives is the shape that
# keeps losing here.
#
# Closing the namespace is affordable because no carrier in this repository
# uses it: the three `pull_request_target` workflows make exactly two API
# calls between them, `gh api repos/O/R/milestones?state=open` and `gh pr edit
# --milestone`, and neither is an issues path. What it costs is
# `issues.createComment` and `issues.addLabels`, which an earlier round kept
# open as the neighbour a labelling carrier would reach for. That neighbour
# still exists and is `gh issue comment`, which is the CLI, names no API path,
# and survives on the verb-and-subcommand allowlist -- so what this refuses is
# the REST spelling of a write that has a working CLI spelling, which is a
# line of review and a documented alternative rather than a wall. The day a
# carrier needs the REST form, the answer is an entry and a reason, the same
# price every allowlist here charges.
#
# `listEvents` and `listEventsForTimeline` came off the pattern with the two
# endpoint paths: every route to the Octokit issues namespace writes `issues`
# once, including a destructured one, so the method names were alternatives
# that could only ever match text the namespace rule already matched.
_PULL_REQUEST_API = re.compile(
    r"/pulls\b"
    r"|/issues\b"
    r"|\brest['\"]?\s*\]?\s*[.\[]\s*['\"]?(?:pulls|issues)\b"
    r"|\b(?:pulls|issues)\s*[.\[]"
    r"|pull/[^\n'\"]*?\.(?:diff|patch)\b"
)

# Enumerating the remote's refs, which reaches `refs/pull/N/head` without
# spelling any of it. `_PULL_REQUEST_REF` refuses three spellings of the
# namespace and git needs none of them: `refs/pull/N/head` is advertised to a
# plain `git ls-remote origin`, so `git ls-remote origin | grep "/$N/head" |
# cut -f1` reads the head SHA out of the advertisement and `git fetch origin
# "$SHA"` is then a fetch by object name that the server serves. The refspec
# form is the same reach with the filtering moved local: `git fetch origin
# '+refs/*:refs/remotes/all/*'` copies the whole ref space onto the runner,
# including the pull namespace, and `git for-each-ref` reads it back. Both
# were live and green against every rule above.
#
# So the rule is written about reach rather than about the two spellings that
# happened to turn up. Four alternatives. Any `ls-remote` is refused -- the
# command's only job is to list what a remote advertises, and a step of a
# workflow that must not have the fork's code has no question for it. And a
# fetch refspec whose source side globs before the namespace is fixed:
# `refs/*` and `refs/p*` both reach the pull namespace, while `refs/heads/*`
# and `refs/tags/*` name a namespace first and are untouched, which is the
# ordinary mirror fetch and the reason this is not a ban on wildcards.
#
# The other two are the same advertisement asked for without `git`.
# `curl "https://github.com/O/R.git/info/refs?service=git-upload-pack"` is
# the wire protocol underneath `ls-remote`: unauthenticated, and it returns
# the identical list for `grep "/$N/head"` to read. Refusing the command and
# conceding the request it makes would be a rule about which program is on
# the runner rather than about what the step reaches, so the path and the
# service name are alternatives here, either half alone being an answer
# nothing in a workflow has. What it concedes is a URL assembled out of
# pieces, which is the string arithmetic the residual list already carries.
#
# The fifth is a clone rather than a fetch, and it names no refspec at all.
# `git clone --mirror URL m` is documented as setting up a refmap of
# `+refs/*:refs/*`, so it copies `refs/pull/N/head` onto the runner with none
# of the four alternatives above appearing anywhere: no `ls-remote`, no
# `refs/*` written down, no HTTP path. `git for-each-ref` in the clone then
# reads the head SHA out, and a fetch by object name serves it. It was green.
# Only `--mirror` is refused, and every abbreviation of it. Git's option
# parser accepts any unambiguous prefix of a long option -- gitcli(7) says
# long options "may be abbreviated only to their unique prefix" -- and no
# other `git clone` or `git push` long option begins with `--m`, so `git
# clone --mir URL m` sets up the identical refmap. It was green against a
# pattern that wanted the full spelling. So the alternative is the ladder
# from `--m` up, anchored so that the option has to *end* where the ladder
# stops: `--milestone` is what `auto-assign-milestone.yml` writes on this
# very trigger, `--max-count` and `--merges` are what a log step writes, and
# no rung of the ladder ends before the next character of any of them. `--m`
# is the bottom rung because that is the prefix git actually takes for
# `clone` and for `push`, measured against git 2.55 rather than read off an
# option list. The same measurement says `git remote add --m` is ambiguous
# there -- `--master` is the other candidate -- so that one rung is covered
# for a command git would have errored on anyway, which is free.
#
# The same question asked of everything else this file refuses by name: none
# of it abbreviates. `ls-remote` is a subcommand and `git-upload-pack` is a
# service name in a URL, and git abbreviates neither -- there is no `git
# ls-rem`. `--approve` in B2 and `--repo` in `_GH_VALUE_FLAGS` belong to
# `gh`, whose parser refuses a prefix outright (`unknown flag: --appr`,
# measured against gh 2.101), so a ladder there would be a rule about an
# input the CLI already rejects.
#
# `--bare` is the neighbouring flag and copies only the branches an ordinary
# clone would, so refusing it would be a rule about the shape of a clone
# rather than about what the clone reaches, and `git clone --bare` of this
# repository is a thing a release step does.
#
# No carrier here runs either, so what this costs today is nothing, and what
# it costs later is a line of review on a step that wants to survey a remote
# from inside a `pull_request_target` job. What it concedes is a fetch that
# names a safe namespace and then walks into an unsafe one -- there is no such
# refspec, but `git fetch origin && git for-each-ref` against a remote whose
# config already carries a wildcard refmap is not read here, because the
# config is not in this file.
_REMOTE_REF_ENUMERATION = re.compile(
    r"\bls-remote\b"
    r"|refs/[^/\s'\"]*\*"
    r"|/info/refs\b"
    r"|\bgit-upload-pack\b"
    r"|--m(?:i(?:r(?:r(?:o(?:r)?)?)?)?)?(?![\w-])"
)

# The third language the payload is written in. `GITHUB_EVENT_PATH` holds the
# whole webhook event as a file on disk, so a script can read the head SHA out
# of it without naming an expression for `_SAFE_SCRIPT_EXPRESSIONS` to read or
# a `context` property for the rule below -- `jq -r .after "$GITHUB_EVENT_PATH"`
# in a shell, `JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH))` in a
# `script:` input. Both were live and green against the allowlists above, which
# is the same lesson a third time: the rule is about reaching the payload, and
# the payload has more spellings than any one of them covers.
#
# So does the variable. The variable is a convenience: the file is at
# `$RUNNER_TEMP/_github_workflow/event.json` and a script that writes the path
# out reads the same bytes while naming `GITHUB_EVENT_PATH` nowhere. Each
# piece of that path is its own alternative rather than one joined pattern,
# because each alone is already an answer nobody has -- nothing in a workflow
# legitimately names the runner's internal `_github_workflow` directory, and
# an `event.json` a step wrote itself is a file the step could have called
# anything.
#
# `_github_workflow` was spelled out until 2026-09-19, and a glob is what
# walked past it: `jq -r .after "$RUNNER_TEMP"/_github_*/*.json` reads the
# payload with no string arithmetic anywhere, and `_github_*` is not
# `_github_workflow`. The prefix is the alternative now, and `RUNNER_TEMP`
# joins it. That was written up the same day as making the directory half
# complete rather than a spelling, on the reasoning that the payload lives
# under that variable and nowhere else, so a step that has the variable has
# the file. The reasoning holds and the conclusion did not, because a step
# can have the directory without having the variable. `jq -r .after
# /home/runner/work/_temp/*/*.json` is that variable's value on a
# GitHub-hosted Linux runner written out with the `_github_workflow`
# component globbed away: no variable, no `_github`, no `event.json`, and it
# was green. So was the same read from inside a `container:` job, where the
# runner bind-mounts that directory at `/github/workflow` and the path it
# gives the step has no underscore in it at all -- `/github/workflow/*.json`.
# Both literals are alternatives now, `work/_temp` rather than the whole
# absolute path because a self-hosted runner's is `.../_work/_temp` and the
# component is what identifies it.
#
# The directory half is a list of spellings and this comment says so rather
# than claiming otherwise: `D:\a\_temp` is the Windows runner's and is not
# here, because nothing in this repository runs on one and a pattern for a
# platform no carrier uses is a pattern nothing measures. `RUNNER_TEMP`
# costs a step that wanted scratch space, which writes `$(mktemp -d)`
# instead -- and which could not have written `${{ runner.temp }}` either,
# because the expression allowlist refuses that already.
#
# Both `GITHUB_EVENT_PATH` and `RUNNER_TEMP` are also off
# `_SAFE_RUNNER_VARIABLES`, so for either of those spellings the
# runner-variable rule fires first and this one decides nothing. What it
# decides on its own is the spelling that names no variable at all:
# `/home/runner/work/_temp/_github_workflow/event.json` is where
# `GITHUB_EVENT_PATH` points on a GitHub-hosted Linux runner, written out, and
# the directory and the file name are the only things left to read it by. That
# is the shape the mutation table pins this rule with, and it had to be added
# for the purpose. Before it, the table's seven payload-file rows all named
# one of the two variables somewhere and all seven died on the runner-variable
# allowlist instead -- measured by neutering this whole assertion, at which
# every one of them still went red, so the rule could have been deleted
# outright and nothing would have reported it.
#
# The process environment is the last piece, and it is refused as an accessor
# rather than as a name, in each of the two languages a step can be written
# in. `process.env['GITHUB_EVENT_' + 'PATH']` is the path with the string
# split in two, which no pattern over the name can see and which a minifier
# would produce by accident; `process['env'][...]` is the same read with the
# accessor itself indexed, and it walked past a pattern that wanted a literal
# dot. Where the name is split matters for what the row proves rather than for
# what the runner does: `'GITHUB_EVENT_' + 'PATH'` leaves a reserved prefix
# behind for the runner-variable allowlist to refuse, so a row written that way
# pins that allowlist and not this rule, while `'GITHUB' + '_EVENT_PATH'`
# leaves no `GITHUB_`-prefixed word anywhere and the accessor is the only thing
# deciding. `shell: python` is the other language -- `os.environ["GITHUB_EVENT_" +
# "PATH"]` and `os.getenv(...)` are the same reach, and an earlier round
# claimed to handle `shell: python` while reading neither. So the rule is the
# accessor in both: `process.env`, `os.environ`, the name `environ` however
# it got into scope, and `getenv`.
#
# `environ` is refused as a bare word rather than as a word with a subscript
# after it. `from os import environ as e` binds the mapping to a name this
# file cannot predict and then reads it as `e.items()`, so the import is the
# last point at which the accessor is spelled at all, and it was green while
# the pattern wanted a `.` or a `[` next. The bare word costs nothing it was
# not already costing -- a step that writes `environ` and does not read it is
# a step that wrote it for nothing -- and it picks up `/proc/self/environ`,
# which is the same mapping a third way, in the shell.
#
# Refusing the accessor rather than the name is a rule about reach rather
# than about spelling, and it is affordable for a reason specific to these
# inputs: a `script:` is handed everything it needs as `context`, `github`,
# `core` and its own `with:` inputs, and a `run:` step is handed its `env:`
# block as ordinary shell variables. A body that goes to the process
# environment programmatically is going somewhere the step already offered it
# a supported route to. It costs a false red on a script reading an unrelated
# variable, which is a line of review and one `core.getInput` or one `$NAME`
# away from not needing the accessor at all.
_EVENT_PAYLOAD_FILE = re.compile(
    r"GITHUB_EVENT_PATH"
    r"|RUNNER_TEMP"
    r"|work/_temp\b"
    r"|/github/workflow\b"
    r"|_github"
    r"|event\.json"
    r"|\bprocess\s*[.\[]\s*['\"]?env\b"
    r"|\bos\s*[.\[]\s*['\"]?environ\b"
    r"|\benviron\b"
    r"|\bgetenv\b"
)

# `context.repo` is `{owner, repo}` for the repository the workflow lives in,
# which is `github.repository` from the list above in the other language.
# `context.payload`, `context.sha` and `context.ref` are all the pull request
# on this trigger, and are not on it.
_SAFE_SCRIPT_CONTEXTS = frozenset({"repo"})

# And the third language the payload arrives in, after the expression and the
# `context` object: the variables the runner sets itself. A step names none of
# them in its `env:` and they are all there -- `GITHUB_HEAD_REF` is the pull
# request's head branch and `GITHUB_ACTOR` is whoever opened it, so `git fetch
# "https://github.com/$GITHUB_ACTOR/${GITHUB_REPOSITORY#*/}" "$GITHUB_HEAD_REF"`
# is the fork's branch fetched from the fork, with no expression, no `context`,
# no `env:` block and none of the words `pull`, `head` or `merge` reaching any
# pattern that reads for them. Both were green until 2026-09-19.
#
# This is the same allowlist the expressions get, over the same event. It can
# be an allowlist rather than a list of dangerous names because the namespace
# is closed: GitHub reserves the `GITHUB_` and `RUNNER_` prefixes and
# documents every variable it sets, so an unrecognised one is either a
# variable this file has not read about or a variable a step invented under a
# reserved prefix, and both of those are questions rather than answers.
#
# What is on the list is what the base repository decides. `GITHUB_REPOSITORY`
# and its owner and id name the repository the workflow is in, which is this
# one whoever opened the pull request. `GITHUB_REF` and `GITHUB_SHA` are the
# default branch and its head commit on this trigger, since the 2025-12-08
# change the test's docstring dates and cites -- the same value
# `_SAFE_CHECKOUT_EXPRESSIONS` holds, arriving another way. `GITHUB_WORKSPACE`,
# `GITHUB_ENV`, `GITHUB_PATH`, `GITHUB_OUTPUT`, `GITHUB_STATE` and
# `GITHUB_STEP_SUMMARY` are paths the runner owns; the run and job and workflow
# identifiers, the three URLs and the event name are facts about the run; and
# `GITHUB_TOKEN` is the credential this whole test is about, which every
# carrier names and which B2 and the `permissions:` rules below govern
# separately. `RUNNER_OS` and `RUNNER_ARCH` are the machine.
#
# What is off it is everything the pull request decides. `GITHUB_HEAD_REF` is
# the head branch; `GITHUB_BASE_REF` is the base branch, off for the reason
# `github.base_ref` is off `_SAFE_CHECKOUT_EXPRESSIONS`; `GITHUB_ACTOR`,
# `GITHUB_ACTOR_ID` and `GITHUB_TRIGGERING_ACTOR` are its author, whose login
# is the owner half of the fork's clone URL; `GITHUB_EVENT_PATH` and
# `RUNNER_TEMP` are the payload on disk, which `_EVENT_PAYLOAD_FILE` refuses
# for itself. A step that wants any of them is a step to read, which is what a
# red here asks for.
_RUNNER_VARIABLE = re.compile(r"\b(?:GITHUB|RUNNER)_[A-Z0-9_]+\b")
_SAFE_RUNNER_VARIABLES = frozenset({
    "GITHUB_ACTION",
    "GITHUB_ACTIONS",
    "GITHUB_ACTION_PATH",
    "GITHUB_ACTION_REPOSITORY",
    "GITHUB_API_URL",
    "GITHUB_ENV",
    "GITHUB_EVENT_NAME",
    "GITHUB_GRAPHQL_URL",
    "GITHUB_JOB",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_REF_PROTECTED",
    "GITHUB_REF_TYPE",
    "GITHUB_REPOSITORY",
    "GITHUB_REPOSITORY_ID",
    "GITHUB_REPOSITORY_OWNER",
    "GITHUB_REPOSITORY_OWNER_ID",
    "GITHUB_RETENTION_DAYS",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_NUMBER",
    "GITHUB_SERVER_URL",
    "GITHUB_SHA",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_TOKEN",
    "GITHUB_WORKFLOW",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_WORKFLOW_SHA",
    "GITHUB_WORKSPACE",
    "RUNNER_ARCH",
    "RUNNER_DEBUG",
    "RUNNER_NAME",
    "RUNNER_OS",
    "RUNNER_TOOL_CACHE",
})

# The rule above reads names, and a step that reads its whole environment at
# once writes none of them. `A=$(env | grep -i '^github_head_ref=' | cut -d=
# -f2)` puts the pull request's branch in `$A` with `GITHUB_HEAD_REF` spelled
# nowhere the allowlist can see it -- the only spelling on the line is lower
# case, which is not the name the shell would expand and is exactly the name
# `grep -i` matches -- and it was green. So was `env | grep -i
# '^github_actor='`, which is the other half of a clone URL for the fork.
#
# This is `_REMOTE_REF_ENUMERATION`'s shape one language along, and the
# argument is the same one. That rule exists because the ref half of its
# neighbour is a scan for a name, and `git ls-remote` reaches
# `refs/pull/N/head` without naming it; the answer was to refuse the
# enumeration rather than to chase the spellings of the name. Here the
# neighbour is `_RUNNER_VARIABLE`, the enumeration is the environment, and
# the answer is the same: a step that asks for all of it has `GITHUB_HEAD_REF`
# and `GITHUB_ACTOR` whether or not it could spell either.
#
# The reason there was nothing here already is worth writing down, because
# this file removed it on purpose. Until 2026-09-19 the wholesale refusal of
# the step's environment was gated on `_UNREADABLE_SHELL`, a list of the
# shell constructs that defeat the pickup -- `printenv`, `eval`, `env`,
# `declare`, indirect expansion -- and the gate was removed for being a
# denylist over idioms with the interpreters already past it. Removing it was
# right: the refusal it gated is unconditional now, which is strictly more
# than it was. What went with it was not a condition, though. It was the only
# thing in the file that had ever read a dump, and a dump does not reach the
# step's declared `env:` -- which the unconditional refusal reads in full --
# it reaches the runner's own variables, which nothing reads except by name.
# The gate and the enumeration were the same code and only one of them was
# the mistake.
#
# So this is a list of spellings, which is what the deleted rule was, and the
# difference is what it decides. The deleted one said "a step that writes
# `eval` may not have the head in its `env:`", which is a claim about a
# program; this one says "a step may not ask for its environment", which is a
# claim about a request, and the requests are a closed set in a way that the
# ways to read a variable are not: `env` and `printenv` with no operand,
# `declare -p`, `declare -x`, `export -p`, `compgen -e`, a bare `set`. Each
# is anchored on a terminator -- end of line, a pipe, a redirect, a
# semicolon, an `&&`, a closing paren -- because that is what distinguishes
# the dump from the ordinary use: `env FOO=1 cmd` and `env -i PATH=/bin cmd`
# set a variable, `declare -a xs` and `export PATH` and `set -euo pipefail`
# are not reads, and `#!/usr/bin/env bash` is a shebang, so none of them
# match. What it costs is `conda env` at the end of a line and `python -m
# venv env`, which are a line of review, and nothing in any carrier here.
#
# `os.environ`, a bare `environ` and `process.env` are the same request in
# the two other languages a step can be written in, and they are refused at
# `_EVENT_PAYLOAD_FILE` already, because there the same mapping is the thing
# that carries the path to the payload. They are not repeated here: a second
# pattern for a read the file already refuses is a second thing to keep true,
# and the mutation table would credit whichever assertion ran first.
_ENVIRONMENT_ENUMERATION = re.compile(
    r"(?<![\w./-])env(?:[ \t]+-[\w-]+)*[ \t]*(?:$|[|>;&)])"
    r"|(?<![\w./-])printenv(?:[ \t]+-[\w-]+)*[ \t]*(?:$|[|>;&)])"
    r"|(?<![\w./-])(?:declare|typeset)"
    r"(?:[ \t]+-[a-zA-Z]*[px][a-zA-Z]*)?[ \t]*(?:$|[|>;&)])"
    r"|(?<![\w./-])export(?:[ \t]+-p)?[ \t]*(?:$|[|>;&)])"
    r"|(?<![\w./-])set[ \t]*(?:$|[|>;&)])"
    r"|(?<![\w./-])compgen[ \t]+-[\w-]*e\b",
    re.MULTILINE,
)

# A backslash before a newline is not a line break: the shell removes both
# before the command runs. Every pattern here is line-oriented -- `[^\n]*?`
# in the refspec, a walk that stops at `\n` in the `gh` helper -- so
# `refs/pull/${N}/` on one line and `head` on the next is a refspec no
# pattern in this file can see, and wrapping a long command is ordinary
# housekeeping rather than a dodge. The join runs once over the text the
# rules read, so that what is matched is what the shell will run.
_LINE_CONTINUATION = re.compile(r"\\\n")

# Enough passes to settle any chain a workflow would plausibly write.
# `_expand_env` is the one fixed-point loop that reads it -- the `run:` half
# had a second until 2026-09-19 -- and it is capped rather than run to
# exhaustion, so a self-referential pair cannot spin.
_ENV_EXPANSION_LIMIT = 10

#: Every scope a `permissions:` block can name, so that `write-all` expands to
#: what GitHub means by it. Three are read today -- `contents` and `id-token`
#: by B4, `pull-requests` by B2's approval check, all three through this same
#: expansion -- and the list is whole rather than those three, because a
#: shorthand expanded over a partial list would be a quieter way to be wrong
#: than not expanding it at all.
_PERMISSION_SCOPES = frozenset({
    "actions", "attestations", "checks", "contents", "deployments",
    "discussions", "id-token", "issues", "models", "packages", "pages",
    "pull-requests", "repository-projects", "security-events", "statuses",
})
_PERMISSION_SHORTHANDS = {"write-all": "write", "read-all": "read"}


def _permission_scopes(document):
    """Every `permissions:` block in a workflow, as {scope: level} mappings.

    Workflow level and each job's, because GitHub honours either and a job
    inherits the workflow's when it declares none.

    The shorthand is why this is a function rather than a list comprehension
    at each call site. `permissions:` usually takes a mapping, but it also
    takes the bare strings `write-all` and `read-all`, and `write-all` is the
    widest grant a workflow can make -- every scope, `contents` and `id-token`
    among them. Every call site filtered on `isinstance(scope, dict)`, so the
    string fell through the filter and a `pull_request_target` workflow with
    `permissions: write-all` at the top satisfied every assertion about its
    token. Job-level `write-all` was caught, but by `test_B2_...`, which is a
    different test asking a different question -- a neighbour's red is not
    this assertion working. The one site that wants a single job's effective
    grant rather than every block in the file uses `_effective_permissions`;
    the shorthand is the same there, the inheritance rule is not.

    An unknown string yields no grant, which is how `permissions: {}` reads;
    the ones that matter are the two GitHub documents.
    """
    blocks = [document.get("permissions")] + [
        (job or {}).get("permissions")
        for job in (document.get("jobs") or {}).values()
    ]
    for block in blocks:
        yield _permission_mapping(block)


def _permission_mapping(block):
    """One `permissions:` block as a {scope: level} mapping.

    A mapping is itself, the two shorthands expand, and anything else -- a
    missing block, or a string GitHub does not document -- is no grant.
    """
    if isinstance(block, dict):
        return block
    if isinstance(block, str) and block.strip().lower() in _PERMISSION_SHORTHANDS:
        level = _PERMISSION_SHORTHANDS[block.strip().lower()]
        return {scope: level for scope in _PERMISSION_SCOPES}
    return {}


def _effective_permissions(document, job):
    """What a job's token actually carries.

    `_permission_scopes` reads every block in the file and cannot answer this:
    a job inherits the workflow's block only when it declares none of its own,
    and a caller asking "is this job a deploy" needs the one that applies to
    it rather than the union of all of them.
    """
    block = (job or {}).get("permissions")
    if block is None:
        block = document.get("permissions")
    return _permission_mapping(block)


def _env_values(block):
    """The `env:` values of a workflow, job or step, as {NAME: text}.

    Every level is consulted because GitHub merges them, and a ref laundered
    through any one of them is the same ref. Four levels: the workflow, the
    job, the job's `container:`, and the step. The container's was missing
    until 2026-09-19, while this docstring said what it says now -- a
    container `env:` is set in the container every step of the job runs in,
    so it reaches the steps exactly as the job's own does.

    An `env:` that is not a mapping is an expression standing in for the whole
    block -- `env: ${{ fromJSON(...) }}` is valid, and GitHub evaluates it into
    the environment at run time -- and it is returned whole under a
    placeholder name rather than dropped. Dropping it read as "this step
    declares no environment", which is the loudest possible way to be wrong
    here: a block built by `fromJSON` out of `github.event.after` satisfied
    every rule about what a step's environment may carry by carrying nothing
    this function could see. Yielding the text instead puts it in front of the
    expression allowlist, which refuses it for being unresolvable. The name is
    `<env>` because there is no key to use and the name only ever appears in a
    failure message; an angle-bracketed one cannot collide with a variable,
    which `NAME` could.

    This is `_with_inputs`'s rule for a non-mapping `with:`, one field along,
    and for the same reason: the one direction this test does not go is
    failing open on something it cannot parse.
    """
    env = (block or {}).get("env") or {}
    if not isinstance(env, dict):
        return {"<env>": str(env)}
    return {str(k): str(v) for k, v in env.items()}


def _normalise_expression(expression):
    """One expression in the spelling the allowlists are written in.

    Membership of a set of strings answers a question about how an expression
    is spelled, and the allowlists here are asking what it names. GitHub is
    case-insensitive about both the context and the property -- `${{
    GITHUB.EVENT.AFTER }}` is `${{ github.event.after }}` -- and it reads an
    index into a context as the property access it is, so
    `github.event.pull_request['head'].sha` is the head SHA wearing another
    expression's clothes. Both reached the `run:` half as unrecognised text
    and were read as innocent.

    So an index with a literal subscript becomes a property, whitespace around
    a dot goes (the expression parser allows it), and the result is folded to
    lower case. What is still unrecognised after that stays unrecognised,
    which against an allowlist means refused.
    """
    expression = _EXPRESSION_INDEX.sub(
        lambda match: "." + (match.group(1) or match.group(2) or ""), expression
    )
    return re.sub(r"\s*\.\s*", ".", expression).strip().lower()


def _expand_env(text, env):
    """`${{ env.NAME }}` replaced by NAME's value, to a fixed point.

    Iterated rather than single-pass because one `env:` value may name
    another, and a single pass resolves such a chain only when the mapping
    happens to be ordered favourably. The same two declarations written in
    the other order would leave the ref half-expanded and looking innocent,
    which is a difference GitHub does not make. The cap stops a
    self-referential pair from spinning; text that has not settled by then
    keeps its `${{ ... }}`, and the caller refuses what it cannot resolve.

    The match is case-insensitive in both halves, which is what GitHub does:
    `${{ env.REV }}`, `${{ env.rev }}` and `${{ Env.REV }}` are one reference
    to one value. A case-sensitive substitution left the third spelling
    standing, where it was neither resolved here nor picked up by the caller
    as a shell variable, because it is not one. The `run:` half's own pickup
    is case-*sensitive* and stays that way for the opposite reason: `$rev` is
    not `$REV` to a shell, so folding in a value the script cannot be reading
    would be a false red rather than a catch.

    The substitution is total: an unknown name is left alone rather than
    blanked, so a miss cannot quietly turn a suspicious ref into an innocent
    one. The replacement goes through a lambda because `re.sub` reads a string
    replacement as a template, and it parses that template whether or not the
    pattern matches. A value holding a backslash -- a Windows path, a `sed`
    snippet -- would otherwise raise for every ref in scope of the `env:` that
    declared it, including the refs that never mention it.
    """
    for _ in range(_ENV_EXPANSION_LIMIT):
        before = text
        for name, value in env.items():
            text = re.sub(
                r"\$\{\{\s*env\." + re.escape(name) + r"\s*\}\}",
                lambda _match, replacement=value: replacement,
                text,
                flags=re.IGNORECASE,
            )
        if text == before:
            break
    return text


def _join_continuations(text):
    """A script as the shell will see it, with backslash-newlines removed.

    `git fetch origin refs/pull/${N}/\\` on one line and `head` on the next
    is one word to a shell -- it strips the backslash and the newline
    together before the command runs -- and it is two lines to every pattern
    in this file, each of which stops at a newline because the alternative is
    a pattern that matches across a whole script. So the join happens first
    and the patterns read the result: the refspec is spelled out again, and
    the rules that were written against `refs/pull/${N}/head` see it.

    Nothing is inserted in place of the pair, which is what the shell does:
    the continued line keeps whatever indentation it has, so two words
    wrapped across a continuation stay two words and only a split token is
    rejoined. Removing the newline can only lengthen a line, so a pattern
    that matched before the join still matches after it.
    """
    return _LINE_CONTINUATION.sub("", text)


def _invocation_words(text, offset):
    """The command words of the invocation starting at `offset`, and its text.

    Shared by the two walks below, which ask the same question at different
    anchors: `_gh_invocations` starts at the program name, and the pull
    request backstop starts at the `pr` word because the program name is not
    something this file can read. Everything either of them relies on --
    where an invocation ends, which words are flags, which punctuation comes
    off a word -- is the same question in both, so it is one function.

    It is a walk and not a parser, and every place it is wrong is wrong in
    the refusing direction. A flag it does not know takes a value costs a
    false red rather than a false green: the value is read as the next
    command word, is not on an allowlist, and the step is refused. That is
    why the callers' messages quote the whole invocation rather than the word
    they objected to -- the word can be the wrong one. The walk stops at the
    first newline, `;`, `|`, `&`, parenthesis or backtick, so a separator
    inside a quoted string ends it early; that loses the tail of an
    invocation and cannot invent one.

    Punctuation comes off each word and an emptied word is dropped, which is
    what makes a JavaScript argument vector read as the command line it is:
    `exec.exec('gh', ['pr', 'checkout', N])` runs `gh pr checkout` and splits
    into `',`, `['pr',`, `'checkout',`. The argument for the exact set is at
    `_GH_WORD_PUNCTUATION`.
    """
    rest = text[offset:]
    stop = _COMMAND_END.search(rest)
    invocation = (rest[: stop.start()] if stop else rest).strip()
    words = []
    skip_value = False
    stripped = (
        word.strip(_GH_WORD_PUNCTUATION)
        for word in _GH_EXPRESSION.sub("EXPR", invocation).split()
    )
    for word in (word for word in stripped if word):
        if skip_value:
            skip_value = False
            continue
        if word.startswith("-"):
            skip_value = word in _GH_VALUE_FLAGS
            continue
        words.append(word)
    return words, invocation


def _invocation_program(text, offset):
    """The program word of the invocation containing `offset`, if it is one.

    The walk backwards that `_invocation_words` is forwards: from a word in
    the middle of a command to the word that command started with. It stops
    at the same separators, so what it returns is the first word of the same
    segment `_invocation_words` would read to the end of.

    An empty string is an answer, and it is the refusing one: a `pr` with
    nothing before it on its own segment is the far side of a pipe, which is
    where `echo "pr checkout $N" | xargs gh` puts its arguments.
    """
    head = text[:offset]
    starts = [match.end() for match in _COMMAND_END.finditer(head)]
    words = head[starts[-1] :].split() if starts else head.split()
    return words[0] if words else ""


def _gh_invocations(text):
    """Every `gh ...` in `text`, as `(verb, subcommand, invocation)` triples.

    A regex cannot answer this and the last one did not. `gh` takes its flags
    anywhere, so `gh pr -R "$REPO" checkout "$N"` *is* `gh pr checkout` as
    far as the CLI is concerned -- it strips flags before it resolves the
    subcommand -- while "the word immediately after `pr`" is `-R`. Those are
    two different questions and only the second one is about what runs. So
    `_invocation_words` walks the words instead, and this reads the first two
    that survive: the verb, and then the subcommand under it. Everything the
    walk itself concedes is argued there.

    A verb with no subcommand under it reads as the empty string, which is on
    no allowlist, on the same reasoning the ref allowlist refuses an
    expression it cannot resolve. So does an invocation with no *words* at
    all, and that changed on 2026-09-19. It used to be skipped, on the
    reasoning that a bare `gh` runs nothing -- which is true of `gh` alone at
    a prompt and false of `echo "pr checkout $N" | xargs gh`, where the
    arguments arrive on stdin and the walk sees an empty invocation followed
    by a newline. A `gh` this file cannot read the arguments of is now refused
    for that, rather than passed for it. What it costs is a false red on a
    bare `gh` written to print its own help, which nothing here does.

    What is not covered is a `gh` whose program name is unreadable -- through
    a variable, a command substitution, or a quote in the middle of the word.
    Nothing over the *name* can close that, and the answer is a second rule
    over the argument shape rather than a wider pattern here: see
    `_GH_PULL_REQUEST_WORD`.
    """
    for match in _GH_COMMAND.finditer(text):
        words, invocation = _invocation_words(text, match.end())
        yield (
            words[0] if words else "",
            words[1] if len(words) > 1 else "",
            invocation,
        )


def _unsafe_gh_verbs(text):
    """Every `gh ...` in `text` whose verb is not allowlisted.

    The outer half of the pair: `_SAFE_GH_VERBS` is read before the
    subcommand allowlist is, because a verb nobody here recognises has no
    subcommands this file can have an opinion about. `gh alias set co 'pr
    checkout'` is refused as `alias` and the `gh co "$N"` it installs is
    refused as `co`, neither of them for resembling anything.

    A `gh` with no words after it at all -- the bare program name, an
    invocation the walk truncated at a separator before it reached a word, or
    a `gh` reading its argument vector off a pipe -- is yielded with the empty
    string as its verb, and the empty string is not on the list. That is the
    round-10 correction to this docstring, which used to say such a `gh` "runs
    nothing": `echo "pr checkout $N" | xargs gh` runs whatever arrives.
    """
    return sorted({
        f"gh {invocation}"[:120]
        for verb, _, invocation in _gh_invocations(text)
        if verb not in _SAFE_GH_VERBS
    })


def _unsafe_gh_pull_request_commands(text):
    """Every allowlisted-verb `gh` in `text` whose subcommand is not.

    The inner half. Only the verbs in `_SUBCOMMAND_GOVERNED_GH_VERBS` reach
    here: `gh api` takes a path rather than a subcommand and is governed by
    `_PULL_REQUEST_API` instead, and every other verb was already refused by
    `_unsafe_gh_verbs`. What is left is `gh pr` and `gh issue`, which address
    the same object and are held to the same two write subcommands.
    """
    return sorted({
        f"gh {invocation}"[:120]
        for verb, subcommand, invocation in _gh_invocations(text)
        if verb in _SUBCOMMAND_GOVERNED_GH_VERBS
        and subcommand not in _SAFE_GH_PULL_REQUEST_SUBCOMMANDS
    })


def _unsafe_pull_request_subcommands(text):
    """Every `pr <subcommand>` in `text` whose subcommand is not allowlisted.

    The backstop under both allowlists above, and the only one of the three
    that does not care what program is running. `_GH_PULL_REQUEST_WORD` has
    the four spellings of the program name that walked past `_GH_COMMAND` and
    the argument that no fifth one can be ruled out; this is the rule those
    four share, which is that each of them writes `pr checkout` in the sense
    the CLI reads it.

    Held against `_SAFE_GH_PULL_REQUEST_SUBCOMMANDS`, the same list
    `_unsafe_gh_pull_request_commands` uses, through the same walk -- so a
    subcommand GitHub adds tomorrow is refused here for not being on the list,
    exactly as it is there, and the two rules cannot disagree about what `pr`
    may do.

    A `pr` with no word after it is not yielded. Unlike the `gh` case above
    there is nothing to be refused for: `pr` is not a program, so a `pr` at
    the end of a line is a word in a sentence rather than an invocation whose
    arguments went somewhere this file cannot see.

    Neither is a `pr` whose own invocation names a program review can read --
    the argument at `_READABLE_PROGRAM`. What is left is the shape with no
    readable name in front of it, which is what each of the four spellings
    that walked past `_GH_COMMAND` has in common.
    """
    return sorted({
        f"pr {invocation}"[:120]
        for match in _GH_PULL_REQUEST_WORD.finditer(text)
        if not _READABLE_PROGRAM.match(_invocation_program(text, match.start()))
        for words, invocation in [_invocation_words(text, match.end())]
        if words and words[0] not in _SAFE_GH_PULL_REQUEST_SUBCOMMANDS
    })


def _with_inputs(step, name):
    """A step's `with:` values under `name`, matched case-insensitively.

    Case matters here and it is not obvious. The runner hands an input to an
    action as `INPUT_<NAME>`, upper-casing the key, and `core.getInput("ref")`
    looks it up by upper-casing too -- so `Ref:` and `REF:` reach
    `actions/checkout` as the ref it checks out. A `with.get("ref")` reads
    none of them. This is the `uses:` bug one field along: that filter was
    case-sensitive too, `Actions/checkout` walked past it, and the mutation
    row that found it is still in the harness. A list rather than a value
    because both spellings can be present at once, and the rule has to hold
    for each.

    A `with:` that is not a mapping -- `with: ${{ fromJSON(env.CONFIG) }}` --
    is returned whole rather than skipped, so it reaches the allowlist and is
    refused as the unresolvable thing it is. Returning nothing there would
    fail open, which is the one direction this test does not go.

    A null value is `""` and not `"None"`. `ref:` with nothing after it is
    valid YAML and parses to `None`, and `str(None)` is a four-character
    truthy string that satisfies `any(refs)`, carries no expression for the
    allowlist to check, and contains none of `pull`, `head` or `merge`. So a
    bare `ref:` -- which is a checkout with no ref, the case the caller
    refuses `actions/checkout` for -- read as an ordinary literal ref and
    passed, while the honest spelling `ref: ""` reddened. That is the
    fail-open direction reached by the shortest possible diff.
    """
    inputs = (step or {}).get("with")
    if inputs is None:
        return []
    if not isinstance(inputs, dict):
        return [str(inputs)]
    return [
        "" if value is None else str(value)
        for key, value in inputs.items()
        if str(key).lower() == name
    ]


def _step_scripts(step, *scopes):
    """Everything in a step that is a script, as one text.

    `run:` is the obvious one and it is not the only one.
    `actions/github-script` takes JavaScript as its `script:` input and runs
    it in the job, with the same token and the same working directory, so
    `await exec.exec('git', ['fetch', 'origin', head])` written there is the
    identical hazard in a different language -- and a scan of `run:` alone
    reads none of it.

    The rule is over the shape rather than over the action: every `with:`
    value that is a string is folded in. Naming `actions/github-script` would
    make this a rule about that vendor, which is the objection this test
    already makes to a rule about `actions/checkout`, and there are several
    actions that run a script they are handed.

    "Every string" was "every string spanning more than one line", on the
    reasoning that `ref:`, `python-version:` and `fetch-depth:` are one line
    each and nobody writes a multi-line value that is not a program. That is a
    guess about formatting rather than a fact about the value, and it was
    wrong in both directions a program can be written: a `script:` short
    enough to fit on one line is still a program, and a `>-` folded scalar is
    a program that looks multi-line in the file and arrives here as a single
    line, because folding it is what the scalar means. Both walked past
    everything downstream of this function. Reading every string costs those
    scans an ordinary input or two, and what they look for -- a fetch verb, an
    expression that is not on an allowlist, a `context` reference -- is not
    what a version number says.

    A `with:` that is not a mapping is an expression standing in for the whole
    block, and is folded in whole rather than skipped, so that the scans see
    it rather than nothing.

    `shell:` is folded in too, and it is a command line rather than a name.
    The runner builds the step's command by substituting the script's
    temporary file into the value at `{0}`, so `shell: bash -c "gh pr
    checkout $PR_NUMBER && ./ci.sh" {0}` runs a checkout of the pull request
    and the `run:` it decorates can be `true`. Every scan downstream of this
    function reads `run:` and the `with:` values and read no `shell:` at all
    until 2026-09-19, so that step was green with the whole hazard in a field
    nothing opened. Folding the value in costs those scans the word `bash` or
    `python`, which is not a fetch verb, an unrecognised expression or a
    `context` reference.

    `scopes` are the blocks that can set the shell for a step that does not
    set its own: the job and the workflow, each through `defaults.run`. Both
    are read here rather than only where they are written, because a default
    is the step's command line as much as the step's own field is -- a job
    whose `defaults.run.shell` carries the command line above runs it once
    per step, and a rule that read only `steps[*].shell` would be a rule
    about where the author put it. The whole `defaults.run` mapping is folded
    in rather than the `shell` key alone, for the reason `_flatten` reads a
    `container:` block whole: naming the field that matters today is the
    denylist this file keeps replacing, and `working-directory` is a string
    an author wrote too.
    """
    scripts = [str((step or {}).get("run", ""))]
    shell = (step or {}).get("shell")
    if shell is not None:
        scripts.append(str(shell))
    inputs = (step or {}).get("with")
    if isinstance(inputs, dict):
        scripts += [value for value in inputs.values() if isinstance(value, str)]
    elif inputs is not None:
        scripts.append(str(inputs))
    for scope in scopes:
        defaults = (scope or {}).get("defaults")
        run_defaults = defaults.get("run") if isinstance(defaults, dict) else defaults
        if run_defaults is not None:
            scripts += _flatten(run_defaults)
    return "\n".join(scripts)


def _container_blocks(job):
    """A job's `container:` and every one of its `services:`, as (label, map).

    The third place a job runs code, after its steps and the reusable
    workflow it might call, and the one nothing here read until 2026-09-19. A
    job that names `container:` runs every one of its steps inside that
    image, so the image is the code: `image: ghcr.io/${{
    github.event.pull_request.head.repo.full_name }}/runner:latest` pulls
    something the fork pushed and runs the job's own steps in it, and every
    rule over `run:`, `uses:` and `ref:` passes, because the workflow's steps
    are innocent. `services:` is the same thing once per entry -- a service
    container is started before the steps, on the job's network, and the
    image is chosen the same way.

    The two are one function because the shape is identical: an image, an
    `env:`, `options`, `credentials`, `volumes` and `ports`. Only the label
    differs, and that is for the failure message.

    `container: ubuntu:24.04` is the same block as `container: {image:
    ubuntu:24.04}` -- GitHub accepts a bare string as the image -- so the
    string form is normalised rather than skipped. Skipping it would be a
    rule the string form walks past while looking like the form the rule
    reads, which is the shape of every finding this file has had.
    """
    blocks = []
    container = (job or {}).get("container")
    if container is not None:
        blocks.append(("container", _container_mapping(container)))
    services = (job or {}).get("services") or {}
    if isinstance(services, dict):
        blocks += [
            (f"services.{name}", _container_mapping(service))
            for name, service in services.items()
        ]
    elif services is not None:
        blocks.append(("services", _container_mapping(services)))
    return blocks


def _container_mapping(block):
    """One `container:` or `services.<name>:` block as a mapping.

    A string is the image, which is GitHub's shorthand. Anything else that is
    not a mapping -- an expression standing in for the whole block, which is
    valid and which GitHub evaluates at run time -- is returned under the
    same key rather than dropped, so that the expression allowlist sees it
    and refuses it for being unresolvable. This is `_env_values`' rule for a
    non-mapping `env:`, one field along, and for the same reason: the one
    direction this file does not go is failing open on what it cannot parse.
    """
    if isinstance(block, dict):
        return block
    return {"image": "" if block is None else str(block)}


def _flatten(value):
    """Every scalar in a nested YAML value, keys included, as a list of text.

    The block above is read whole rather than field by field. An image, an
    `env:` value, a `--entrypoint` inside `options:`, a `credentials:`
    password: each is a place an expression can go, and naming the fields
    that matter is the denylist this file keeps replacing. Keys are folded in
    with the values because a mapping key is text a workflow author wrote
    too, and an expression is refused wherever it appears.
    """
    if isinstance(value, dict):
        return [
            text
            for key, item in value.items()
            for text in [str(key), *_flatten(item)]
        ]
    if isinstance(value, list):
        return [text for item in value for text in _flatten(item)]
    return [] if value is None else [str(value)]


def _workflows():
    """The workflow set, which is never legitimately empty.

    Four of the five assertions reading this glob answer for an empty set on
    their own: two compare it against a named allowlist and go red when the
    expected names go missing, and the `workflow_run` deploy gate and the
    `pull_request_target` checkout test each carry a non-empty precondition
    over their own filtered subset. The fifth -- B2's "no workflow approves or
    merges a pull request" -- asserts an absence, and an absence is true of
    the empty set.

    That one is not defenceless. Moving `.github/workflows` reds tests in this
    file and elsewhere: C4's SHA-pin sweep keeps its own copy of this glob and
    guards it, and `autopush-deploy.yml` is a registered `_harness.SOURCES`
    entry, so the harness self-check goes red too. But what B2 inherits from
    that is an answer to somebody else's question, and a count of neighbours
    is not a thing to depend on. Three counts in this docstring have been
    wrong and corrected during review -- the last of them by a change in this
    same pull request, which added a precondition and so moved the number it
    had just been corrected to. That is the argument against writing a fourth,
    so this paragraph names the mechanisms and leaves the counting to whoever
    runs it. This is B2 answering its own question, in the place the set is
    built, for the same reason `_harness.text()` raises rather than returning
    an empty string.

    The name is private by convention only -- all five consumers live in this
    module and nothing stops them reading `_WORKFLOWS` directly. What the
    underscore buys is that `_workflows()` reads as the intended route, so a
    new assertion reaching past it looks wrong to a reviewer.
    """
    if not _WORKFLOWS:
        raise AssertionError(
            f"no workflows matched {h.REPO_ROOT / '.github' / 'workflows'}/*.y*ml; "
            "the glob is wrong"
        )
    return _WORKFLOWS


def _workflow_documents():
    """Every workflow, parsed, with YAML 1.1's `on:` -> True quirk normalised."""
    for path in _workflows():
        document = yaml.safe_load(path.read_text())
        if True in document:  # `on:` is the YAML 1.1 boolean `y`/`yes`/`on`
            document["on"] = document.pop(True)
        yield path, document


class B1NoAgentCredentialCausesAProductionChange(unittest.TestCase):
    """B1: not "cannot mutate a cluster" -- cannot cause a change, by any route."""

    def test_B1_kubectl_write_verbs_are_refused(self) -> None:
        """The headline question, in the form a reviewer asks it.

        Read-only is an allowlist rather than a denylist here, deliberately:
        over-blocking kubectl breaks a skill and someone files a bug, while
        under-blocking it against a customer's production cluster is the thing
        the model exists to prevent. These are the verbs a denylist author
        would have had to think of, and the point is that they are refused
        without anyone having thought of them.
        """
        writes = (
            ["kubectl", "delete", "namespace", "prod"],
            ["kubectl", "delete", "pod", "web-0"],
            ["kubectl", "apply", "-f", "manifest.yaml"],
            ["kubectl", "create", "deployment", "web", "--image=nginx"],
            ["kubectl", "patch", "deployment", "web", "-p", "{}"],
            ["kubectl", "replace", "-f", "manifest.yaml"],
            ["kubectl", "edit", "deployment", "web"],
            ["kubectl", "scale", "deployment", "web", "--replicas=0"],
            ["kubectl", "annotate", "pod", "web-0", "a=b"],
            ["kubectl", "label", "pod", "web-0", "a=b"],
            ["kubectl", "set", "image", "deployment/web", "web=nginx:2"],
            ["kubectl", "rollout", "restart", "deployment/web"],
            ["kubectl", "rollout", "undo", "deployment/web"],
            ["kubectl", "drain", "node-1"],
            ["kubectl", "cordon", "node-1"],
            ["kubectl", "uncordon", "node-1"],
            ["kubectl", "taint", "nodes", "node-1", "k=v:NoSchedule"],
            ["kubectl", "exec", "web-0", "--", "sh"],
            ["kubectl", "cp", "web-0:/etc/passwd", "/tmp/p"],
            ["kubectl", "port-forward", "web-0", "8080:80"],
            ["kubectl", "attach", "web-0"],
            ["kubectl", "proxy"],
            ["kubectl", "run", "shell", "--image=busybox"],
            ["kubectl", "debug", "web-0", "--image=busybox"],
            ["kubectl", "certificate", "approve", "csr-1"],
        )
        for argv in writes:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed, f"{argv} reached the cluster")

    def test_B1_ordinary_reads_still_work(self) -> None:
        """A read-only gate nobody can work behind gets switched off.

        `CREDENTIAL_PROXY_ENFORCE_READ_ONLY` is global, unscoped and has no
        expiry, so the cost of a false refusal is not one failed command -- it
        is an operator disabling the whole posture to get through the day. The
        allowlist's coverage is therefore part of the control.
        """
        reads = (
            ["kubectl", "get", "pods", "-n", "prod"],
            ["kubectl", "describe", "node", "node-1"],
            ["kubectl", "logs", "web-0", "-f"],
            ["kubectl", "top", "pods"],
            ["kubectl", "events", "--for", "pod/web-0"],
            ["kubectl", "auth", "can-i", "delete", "pods"],
            ["kubectl", "rollout", "status", "deployment/web"],
            ["kubectl", "rollout", "-n", "prod", "history", "deployment/web"],
            ["kubectl", "api-resources"],
            ["kubectl", "explain", "pod.spec"],
            ["kubectl", "config", "current-context"],
            ["gcloud", "container", "clusters", "list"],
            ["gcloud", "--project", "p", "container", "clusters", "describe", "c"],
            ["gcloud", "container", "clusters", "get-credentials", "c"],
            ["gcloud", "logging", "read", "resource.type=k8s_cluster"],
            ["gcloud", "projects", "get-iam-policy", "p"],
        )
        for argv in reads:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertTrue(decision.allowed, f"{argv}: {decision.message}")

    def test_B1_gcloud_write_commands_are_refused(self) -> None:
        """gcloud's grammar puts the verb neither first nor last.

        `gcloud container clusters get-credentials prod` ends in a cluster
        name, so finding the verb by position would mean encoding gcloud's
        whole command tree. The allowlist of read paths is what makes these
        refusals fall out rather than needing to be enumerated.
        """
        writes = (
            ["gcloud", "container", "clusters", "delete", "prod"],
            ["gcloud", "container", "clusters", "create", "prod"],
            ["gcloud", "container", "clusters", "update", "prod", "--enable-autoscaling"],
            ["gcloud", "projects", "add-iam-policy-binding", "p", "--member=user:x"],
            ["gcloud", "projects", "set-iam-policy", "p", "policy.json"],
            ["gcloud", "iam", "service-accounts", "keys", "create", "k.json"],
            ["gcloud", "compute", "instances", "delete", "vm-1"],
            ["gcloud", "container", "node-pools", "delete", "np-1"],
            ["gcloud", "secrets", "versions", "access", "latest", "--secret=s"],
        )
        for argv in writes:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed, f"{argv} reached the project")

    def test_B1_the_sandbox_image_ships_no_credentialed_cli(self) -> None:
        """The build gate, asserted against the stage graph rather than a grep.

        A gate in a stage the agent image does not derive from is not a gate,
        and the `credential-proxy` stage deliberately reinstalls all four CLIs
        afterwards -- so "the Dockerfile contains this RUN" is not the
        assertion. This walks `FROM` back from the agent target and requires
        the gate on that path.
        """
        source = h.text("dockerfile")

        # Instruction keywords are matched case-sensitively and continuation
        # lines are skipped, because neither is optional here: `from
        # gateway.kanban_handoff_clip import …`, inside a multi-line RUN that
        # patches a plugin, reads as a stage boundary under the obvious
        # case-insensitive regex and silently splits agent-base in two. The
        # first draft of this test passed for that reason.
        stages: dict[str, str] = {}
        parents: dict[str, str] = {}
        current = None
        continued = False
        for line in source.splitlines():
            match = None if continued else re.match(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line)
            continued = line.rstrip().endswith("\\")
            if match:
                parent, name = match.group(1), match.group(2)
                current = name or parent
                stages[current] = ""
                parents[current] = parent
                continue
            if current is not None:
                stages[current] += line + "\n"

        self.assertIn("platform", stages, "the agent build target is gone")

        lineage, cursor = [], "platform"
        while cursor in stages:
            lineage.append(cursor)
            parent = parents[cursor]
            if parent == cursor or parent not in stages:
                break
            cursor = parent

        # Reworded by #913, which moved the real CLIs into deploy/sandbox/: the
        # guard that matters is still the agent image refusing to carry them.
        gate = "unexpected cluster CLI in the agent image"
        gated = [stage for stage in lineage if gate in stages[stage]]
        self.assertTrue(
            gated,
            f"no stage on the agent image's lineage {lineage} asserts the "
            f"absence of credentialed CLIs",
        )
        # The gate has to cover all four, not just the one someone remembered.
        gate_stage = stages[gated[0]]
        for binary in ("gcloud", "kubectl", "gh", "git"):
            with self.subTest(binary=binary):
                self.assertRegex(
                    gate_stage,
                    rf"for binary in[^\n]*\b{binary}\b",
                    f"the build gate does not check for {binary}",
                )

    def test_B1_the_shipped_denylist_refuses_credential_disclosure(self) -> None:
        """Read out of the rendered ConfigMap, not out of the Go constant.

        The constant is what someone wrote; the ConfigMap is what the sidecar
        loads. These are the commands that hand the credential to the caller
        rather than using it, which is the one thing the denylist has always
        been for.
        """
        disclosures = (
            ["gcloud", "auth", "print-access-token"],
            ["gcloud", "auth", "print-identity-token"],
            ["gcloud", "config", "config-helper"],
            ["gh", "auth", "token"],
            ["gh", "auth", "status", "--show-token"],
            ["kubectl", "create", "token", "default"],
            ["kubectl", "config", "view", "--raw"],
            ["git", "credential", "fill"],
            ["gcloud", "auth", "login"],
            ["gcloud", "auth", "activate-service-account", "--key-file=k.json"],
            ["gh", "auth", "login"],
            ["gh", "auth", "refresh"],
            ["gcloud", "components", "install", "alpha"],
            ["gh", "extension", "install", "owner/repo"],
        )
        for argv in disclosures:
            with self.subTest(argv=argv):
                self.assertIsNotNone(
                    h.policy_blocks(argv),
                    f"{argv} is not matched by any shipped denylist rule",
                )

    def test_B1_precondition_the_denylist_governs_gh(self) -> None:
        """Guards the expected-failure below: `gh` rules must still exist."""
        rule_ids = {rule["id"] for rule in h.rendered_policy_rules()}
        self.assertIn("github.credential-replacement", rule_ids)
        self.assertIn("gh", h.credential_proxy.CommandExecutor.ALLOWED_EXECUTABLES)

    def test_B1_the_denylist_refuses_merge_and_approve(self) -> None:
        """The half of B1 that holds today, pinned so it cannot quietly stop.

        github.merge and github.assent ship in the rendered denylist and are
        the only thing standing between the agent's credential and completing
        its own pull request. The first spelling of this file recorded all of
        B1 as one known violation whose cause said "every denial rule matches
        only gh auth or gh extension" -- true when written, closed since --
        and expectedFailure absorbs any subset, so the two live rules were
        covered by nothing: deleting either from the shipped policy left the
        whole suite green.
        """
        for argv in (
            ["gh", "pr", "merge", "1", "--squash"],
            ["gh", "pr", "review", "1", "--approve"],
            ["gh", "pr", "merge", "--auto", "1"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(
                    h.policy_blocks(argv),
                    f"{argv} is permitted by the shipped denylist",
                )

    @h.known_violation("B1", "04_major_requirements.md B1")
    def test_B1_the_agent_cannot_merge_or_approve(self) -> None:
        """KNOWN VIOLATION, narrowed to what still holds: force-push.

        The merge and approve halves closed -- github.merge and github.assent
        ship in the denylist now, pinned by the passing test above. What
        remains open is `git push --force`: the denylist has no rule for it
        (the git verb and lease machinery in the broker constrain *where* git
        may write, not this force flag), so the credential that can rewrite a
        branch a GitOps Application watches is still a production-change
        credential.

        B1 is not "cannot mutate a cluster", it is "cannot cause a production
        change". Rewriting a watched branch is causing one.
        """
        for argv in (
            ["git", "push", "--force", "origin", "main"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(
                    h.policy_blocks(argv),
                    f"{argv} is permitted by the shipped denylist",
                )


class B2AssentIsHumanOrPolicy(unittest.TestCase):
    """B2: gatekeepers may block and may never approve."""

    def test_B2_no_workflow_approves_or_merges_a_pull_request(self) -> None:
        """The structural form of "no model verdict causes a merge".

        A veto is monotone in the safe direction -- successful injection
        against one produces a false block, a nuisance on one pull request
        rather than a breach. That property only holds while nothing in CI can
        assent, so this refuses the mechanisms rather than the intent: the
        merge and approve calls, and GitHub's auto-merge.
        """
        assenting = (
            r"gh\s+pr\s+merge",
            r"gh\s+pr\s+review[^\n]*--approve",
            r"enablePullRequestAutoMerge",
            r"pulls/\$?\{?[^\n]*\}?/reviews",
            r"peter-evans/enable-pull-request-automerge",
            r"pascalgn/automerge-action",
            r"hmarr/auto-approve-action",
        )
        offences = []
        for path in _workflows():
            text = path.read_text()
            for pattern in assenting:
                for match in re.finditer(pattern, text):
                    line = text[: match.start()].count("\n") + 1
                    offences.append(f"{path.name}:{line} {match.group(0)!r}")
        self.assertEqual(
            [],
            offences,
            "a workflow can assent to its own repository's changes",
        )

    def test_B2_no_workflow_grants_a_bot_the_ability_to_approve(self) -> None:
        """`pull-requests: write` is the permission an approval needs.

        Six workflows hold it, and none can give an approval:

        - auto_request_review, which requests reviewers and does not give them.
        - auto-assign-milestone: triggers on `pull_request_target: closed`
          gated on `merged == true`, so it runs only after the merge decision
          has been taken, and its one call is `gh pr edit --milestone`.
        - coverage-comment: `workflow_run`-triggered in the base repository,
          job-gated on `github.repository`, and its one write is posting the
          coverage comment.
        - risk_classify: `pull_request_target` with `permissions: {}` at the
          top, the grant job-scoped, checkout pinned to the default branch,
          and its one write is swapping the `risk:*` label.
        - hold-unresolved-threads: `schedule` plus `pull_request_target:
          labeled`, `permissions: {}` at the top, the grant job-scoped,
          checkout pinned to the default branch, and its writes are the
          `do-not-merge` label and one comment on pull requests with
          unresolved review threads. It withholds a merge; it cannot grant one.
        - ci-health: `schedule` plus `workflow_dispatch`, job-gated on
          `github.repository` and `refs/heads/main`, top-level permissions
          `contents: read` and `id-token: write` only, the grant job-scoped,
          checkout with `persist-credentials: false`, and its pull-request
          write is one comment on a pull request whose smoke run went red,
          edited in place on later runs. It explains a red; it cannot approve,
          label, or merge anything.

        The list is an allowlist of holders, not of intents: the permission is
        a capability, and this asserts membership rather than absence so a
        seventh holder is a red test and a conversation rather than a silent
        addition. Adding a name here means someone read the workflow.
        """
        holders = []
        for path, document in _workflow_documents():
            # `_permission_scopes`, so that `permissions: write-all` -- which
            # is a string rather than a mapping, and grants this scope along
            # with every other -- joins the list rather than slipping past it.
            for scope in _permission_scopes(document):
                if scope.get("pull-requests") == "write":
                    holders.append(path.name)
                    break
        self.assertEqual(
            [
                "auto-assign-milestone.yml",
                "auto_request_review.yml",
                "ci-health.yml",
                "coverage-comment.yml",
                "hold-unresolved-threads.yml",
                "risk_classify.yml",
            ],
            sorted(set(holders)),
            "an unexpected workflow can write to pull requests",
        )


class B3TheSubstrateIsNotAgentAuthorable(unittest.TestCase):
    """B3: the biggest thing the first draft of the invariants missed."""

    def test_B3_precondition_the_customer_gitops_template_still_exists(self) -> None:
        self.assertIn("/clusters/", h.text("codeowners_example"))

    @h.known_violation("B3", "overnight-b/findings.md 2.3")
    def test_B3_the_substrate_paths_are_enumerated_as_code(self) -> None:
        """KNOWN VIOLATION. The human-only path set exists only in prose.

        B3's own test text is "substrate paths enumerated as code. A PR
        touching them takes a different, human-only path than a PR changing a
        replica count." There is no such enumeration anywhere in this
        repository -- not a checker, not a workflow, not a data file. The
        nearest artifact is `examples/gitops-repo/CODEOWNERS.example`, which is
        a template for the *customer's* repository and is not enforced here,
        and `branch-protection.md`, which documents a `review-gate.yml`
        workflow that does not exist.

        Two consequences worth separating. Nothing gates a change to the VAP,
        the operator ClusterRole or the workflows in this repo differently from
        a change to a replica count. And path-based gating would not be enough
        even if it existed -- Kubernetes does not care what directory a
        manifest lives in, so a ClusterRoleBinding committed under
        `clusters/*/namespaces/team-x/` matches the namespace glob and gets
        approved by the wrong humans. The invariant wants the rendered object
        set gated, not the path.
        """
        candidates = [
            h.REPO_ROOT / ".github" / "CODEOWNERS",
            h.REPO_ROOT / "CODEOWNERS",
            h.REPO_ROOT / "docs" / "CODEOWNERS",
            h.REPO_ROOT / ".github" / "workflows" / "review-gate.yml",
        ]
        present = [path for path in candidates if path.is_file()]
        self.assertTrue(
            present,
            "no substrate enumeration and no gate that reads one",
        )

    def test_B3_the_agent_cannot_reach_the_admission_policy_through_kubectl(self) -> None:
        """One half of B3 that *is* enforced, and worth pinning.

        The VAP and the RBAC it guards live in the repository the agent
        proposes into, so the artifact-plane half is open. The API half is not:
        every verb that would edit an admission policy or a NetworkPolicy in
        place is refused by the read-only allowlist.
        """
        for argv in (
            ["kubectl", "delete", "validatingadmissionpolicy", "kube-agents-agent-readonly"],
            ["kubectl", "patch", "validatingadmissionpolicybinding", "b", "-p", "{}"],
            ["kubectl", "delete", "networkpolicy", "platformagent-sandbox-metadata-deny"],
            ["kubectl", "apply", "-f", "clusterrolebinding.yaml"],
            ["kubectl", "delete", "clusterrole", "kubeagents:explorer"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(command_policy.evaluate(argv).allowed)


class B4TheExecutorIsAGovernedPrincipal(unittest.TestCase):
    """B4: CI/CD holds the only production write credential, so it is in scope."""

    def test_B4_every_workflow_run_deploy_gates_on_repository_and_branch(self) -> None:
        """`workflow_run` fires from the default branch with the *triggering* run's context.

        Without all three predicates a fork's completed run, or a run from a
        non-default branch, reaches a job that mints a deployment credential.
        The three are asserted individually so that dropping one -- the
        plausible edit, made while debugging a deploy -- is red.
        """
        consumers = [
            (path, document)
            for path, document in _workflow_documents()
            if "workflow_run" in (document.get("on") or {})
        ]
        self.assertTrue(consumers, "no workflow_run consumers found; the filter is wrong")

        saw_a_deploy = False
        for path, document in consumers:
            for job_name, job in (document.get("jobs") or {}).items():
                condition = str((job or {}).get("if", ""))
                # A job with no permissions block inherits the
                # workflow-level one wholesale, so reading only the job's
                # would let id-token: write hoisted to the top of the file
                # mint the deploy credential past the strict gate. The
                # helper also expands `write-all`, which grants id-token
                # among the rest and is a string -- read raw it used to
                # raise AttributeError here rather than answer the question.
                permissions = _effective_permissions(document, job)
                with self.subTest(workflow=path.name, job=job_name):
                    # Every workflow_run job gates on the repository — the
                    # AGENTS.md fork rule, and the credential half of it.
                    self.assertIn("github.repository ==", condition)
                    # The full three-predicate gate is owed by the jobs that
                    # mint a deploy credential. When this suite was written
                    # every workflow_run consumer was a deploy; main has since
                    # grown non-deploy consumers (a coverage commenter, a
                    # broken-main notifier — which fires on *failure*, so
                    # conclusion == 'success' would be definitionally wrong
                    # there), so the strict form keys on the credential.
                    if permissions.get("id-token") == "write":
                        saw_a_deploy = True
                        chain_conditions = [condition]
                        needs = (job or {}).get("needs")
                        if isinstance(needs, str):
                            needs = [needs]
                        elif not needs:
                            needs = []
                        all_jobs = document.get("jobs") or {}
                        for needed in needs:
                            if needed in all_jobs:
                                chain_conditions.append(str((all_jobs[needed] or {}).get("if", "")))

                        self.assertTrue(
                            any("workflow_run.conclusion == 'success'" in c for c in chain_conditions),
                            f"{path.name}:{job_name} and its prerequisites must gate on workflow_run.conclusion == 'success'",
                        )
                        self.assertTrue(
                            any("workflow_run.head_branch == 'main'" in c for c in chain_conditions),
                            f"{path.name}:{job_name} and its prerequisites must gate on workflow_run.head_branch == 'main'",
                        )
        self.assertTrue(
            saw_a_deploy,
            "no workflow_run job mints a deploy credential any more; the "
            "strict half of this test has gone dead and needs re-pointing",
        )

    def test_B4_no_pull_request_target_workflow_checks_out_the_pull_request(self) -> None:
        """`pull_request_target` runs with the base repository's secrets.

        Checking out the head is how that becomes arbitrary code execution
        with write credentials. A checkout of something *else* is the
        documented-safe pattern — risk_classify pins `ref:` to the default
        branch, so the code that runs is code already merged — and the
        dangerous ingredient is specifically a ref derived from the pull
        request. So every checkout step in such a workflow must carry an
        explicit `ref:` that does not reference the pull request.

        `actions/checkout` carries one obligation more: it must say `ref:` at
        all. That rule was a security rule and is now an explicitness rule,
        and the difference is a dated fact worth getting right. A checkout
        with no `ref:` takes `GITHUB_REF`, and until 2025-12-08 `GITHUB_REF`
        on this trigger was the pull request's *base* branch -- a ref its
        author picks from the branches that already exist here, so a stale
        unprotected one would serve. GitHub's changelog of 2025-11-07
        ("Actions pull_request_target and environment branch protections
        changes") moved it: from 2025-12-08, `GITHUB_REF` for
        `pull_request_target` resolves to the default branch and `GITHUB_SHA`
        to the latest commit on it, which is what the
        events-that-trigger-workflows reference records for this event today.
        So the implicit ref is now the one expression this test allows
        explicitly, and the rule buys review rather than safety: an explicit
        ref is a line somebody can read and check against the list, and a
        default is a line that is not there. Worth keeping, and not worth
        claiming more for. (The Actions *variables* reference page still says
        this trigger takes its ref from the base branch. That page is stale
        against the changelog and is not the citation here.
        `refs/pull/N/merge` is the `pull_request` trigger's default, not this
        one; `risk_classify.yml` and `hold-unresolved-threads.yml` both carry
        the dated note at the checkout step that relies on it.)

        Both filters are asserted non-empty for the same reason B4's
        `workflow_run` gate asserts its own: this test says nothing at all
        about a repository with no `pull_request_target` workflows, or one
        where none of them checks anything out, so it cannot tell "the trigger
        is gone" from "the parse stopped seeing it". Three workflows carry
        the trigger today -- `auto-assign-milestone.yml`,
        `hold-unresolved-threads.yml` and `risk_classify.yml` -- and two of
        them run a checkout; the milestone one runs a single `gh` step and
        checks nothing out. If either count reaches zero the test should be
        read again, not passed by default. This said four and three until
        2026-09-19, which was a grep's answer rather than the parser's:
        `flaky-check-notify.yml` contains the words `pull_request_target` in
        a comment explaining why it does not use the trigger, and its `on:`
        is `workflow_run`. The numbers here are the ones the filter above
        produces, re-measured against it.

        The `run:` half is a stack of rules over every step of the
        workflow, and it is not a proof: a script can reach a ref any number
        of ways and no assertion over YAML will catch all of them. Two of
        them are the shapes everything else here was built out of, and they
        are worth reading in that order. The first is literal. The `refs/pull` namespace is refused wherever it appears,
        along with the two short forms that reach it without the prefix,
        because it is the checkout action's own documented manual equivalent
        and needs no interpolation at all. Its counterpart is further down,
        among the rules read over everything a step can say rather than over
        its script alone: a step that enumerates the remote's refs holds
        `refs/pull/N/head` whether or not it spelled `pull`, because the
        remote advertises the whole namespace to anyone who asks.
        `_REMOTE_REF_ENUMERATION` has the five shapes, what refusing them
        concedes, and the argument about abbreviation that applies to every
        long option named anywhere in this file: git's parser takes any
        unambiguous prefix of a long option, so `--mirror` is refused as a
        ladder from `--m` up; `gh`'s parser takes none, so `--approve` and
        `--repo` are refused and read at their full spellings; and neither
        program abbreviates a *subcommand*, so `ls-remote` and `git-upload-pack`
        need no ladder of their own. The second is the `ref:` half's
        rule one field along: a step may name only the expressions in
        `_SAFE_SCRIPT_EXPRESSIONS`, its `env:` may carry only those, an
        `actions/github-script` body may reach only the `context` properties
        in `_SAFE_SCRIPT_CONTEXTS`, it may name only the runner variables in
        `_SAFE_RUNNER_VARIABLES`, and nothing in it may reach the webhook
        payload as a file or fetch it over HTTP. Those are the five languages
        the payload arrives in here -- an expression, a JavaScript property
        path, a file on disk, a variable the runner set without being asked,
        and an API request -- and the rule is the same in each: name what is
        known to be independent of the pull request, and everything else
        reads as unsafe for not being on a list.

        A sixth thing a job runs is not a language the payload arrives in at
        all: the image. `container:` and each entry of `services:` are read
        at the job level rather than the step level, because a job that
        names one runs every one of its steps inside it, and steps that are
        innocent line by line are innocent inside the fork's image too. See
        `_container_blocks`.

        "Every step" was "every step that fetches" until 2026-09-19, and the
        gate that said so is gone for the reason every other denylist in
        this file went: it was a list of verbs. `fetch`, `checkout`, `clone`
        and `git pull` are four ways to put a fork's code on the runner and
        there are not four -- `git remote add` and `git remote update`,
        `curl .../tarball/... | tar xz`, `pip install "git+https://..."`,
        `docker build "https://...#<sha>"` -- and a verb laundered through
        `env:` is not a verb this file can read at all, which `${{
        env['CMD'] }}` and `os.environ["CMD"].split()` each demonstrated
        while green. The gate was asking "does this step run the fork's
        code", which is a question about programs and about a package
        manager's argument grammar, and a regex over four words cannot
        answer it. What this file can answer is "does this step reach the
        pull request", and that question needs no gate: reaching the payload
        is the thing the rules below already read for. So the condition went
        and the verdicts stayed. It is the same move the environment
        refusal below made one round earlier against the same objection, and
        it is the whole argument of this half -- a denylist of nouns loses,
        and an allowlist of what is reachable wins.

        What that costs is that every step is now read, including the ones
        that do nothing but call an API. A step that carries the pull
        request's head in its `env:` for a label or a log line is refused
        exactly as a fetching one is, because "does the program read this
        variable" is a question about a program. That is a wider cost than
        the gated version's and it is the same kind: a line of review, on a
        trigger where the job holds a writable token, on a step whose author
        can say in one line why the value is there. Measured against the
        carriers, below.

        The second rule was a denylist over three spellings until
        2026-09-19, and it failed the way the `ref:` half's denylist failed,
        which is the argument for the shape rather than a coincidence. Five
        of them, each live and each green: `${{ github.event.before }}` and
        `${{ github.event.pull_request.merge_commit_sha }}`, two more fields
        naming the same commit; `${{ github.event.pull_request['head'].sha
        }}`, which is `pull_request.head` written as an index; `${{
        GITHUB.EVENT.AFTER }}`, because GitHub resolves an expression
        case-insensitively and a Python regex does not; and
        `context.payload.after` inside a `script:` input, which is the same
        field in a language that interpolates nothing at all. Answering those
        with five more alternatives would have left the sixth.
        `_PULL_REQUEST_HEAD` and `_PULL_REQUEST_REF` stay as a backstop over
        text that carries no expression for the allowlist to read.
        `_PULL_REQUEST_HEAD` is deliberately not extended. `_PULL_REQUEST_REF`
        was, once, and in the one direction that is not another noun: it read
        `pull/N/head` and `pull/N/merge`, and three refspecs reaching the
        same code walked past it on 2026-09-19 -- a wildcard over the whole
        namespace with the checkout on the following line, a backslash
        continuation splitting the ref across two lines, and an `ls-remote`
        glob that resolves the head without spelling it. What answered them
        was not three more refs but the namespace they all resolve inside:
        `refs/pull` is refused whatever follows it. The continuation is
        handled where it belongs, at `_join_continuations`, because a
        backslash-newline is not a line break to the shell and every pattern
        here is line-oriented.

        What the allowlist costs is measured rather than assumed, and
        dropping the gate is what made it cost anything. Every step of all
        three carriers is read now, and two expressions had to go on the
        list for them to stay green: `github.event.pull_request.number`,
        which two of them label and comment with, and `inputs.dry_run`, a
        `workflow_dispatch` boolean. Both arguments are at
        `_SAFE_SCRIPT_EXPRESSIONS`. That is the bill in full -- two entries
        and two reasons, paid once, for a rule that no longer depends on
        recognising a verb. The next genuinely safe expression somebody
        needs is a line of review and a line on the list saying why, which
        is the same price and the same answer as the ref allowlist.

        "The step's script" is not the same thing as its `run:`.
        `actions/github-script` takes JavaScript as an input and runs it in
        the job with the same token, so the fetch can be written in the
        `with:` block instead and a scan of `run:` reads nothing at all.
        `_step_scripts` therefore folds in every `with:` value that is a
        string -- by shape rather than by action name, for the same reason
        the `ref:` rule below is not about `actions/checkout`. It folded in
        only the values spanning more than one line until 2026-09-19, on the
        reasoning that a program has newlines in it, which is a guess about
        formatting: a one-line `script:` is a program, and so is a `>-`
        folded scalar, which arrives as a single line because folding is what
        the scalar means.

        Both patterns are read over that script and the step's `env:` values
        together, not over the script alone. Passing an event field through
        `env:` is this repository's house style rather than an exotic dodge
        -- `risk_classify.yml` does exactly that with `PR_NUMBER`, on the
        stated grounds that event fields are attacker-controlled input -- so
        a scan of `run:` by itself reads `$PR_HEAD` and sees nothing.

        Which of those values get folded in is a question that stopped
        deciding anything when the gate went, and saying so is worth more
        than the loop that answers it. The pickup below folds in the values
        the script names, following a chain one hop at a time, and the two
        literal backstops read the result. Those backstops also read every
        environment value singly, because the wholesale refusal below does,
        and the folded text is a subset of the values that refusal already
        walks -- neither pattern can match across the newline the fold joins
        on, so there is no string the pickup puts in front of a pattern that
        the pattern was not going to see anyway. That is an argument, so it
        was also measured: neutering the pickup to an empty mapping leaves
        all twelve round-7 cases, all fifteen round-6 cases and all
        forty-two evasion cases at exactly the verdicts they hold with it.

        It stays because it is cheap and because the wholesale refusal is
        one edit from being narrowed again, and it stays *unpinned*, which
        is the part to write down: no mutation row covers it, and none can
        while no workflow in this repository carries a head in an `env:`
        value. A row that neutered it would be green forever and would be
        theatre. Read it as redundancy that is deliberate rather than as a
        rule -- and if the next round wants it gone, the measurement above
        is the permission slip.

        "Names" has to mean both ways of naming, and expansion has to run
        first. A script can reach an `env:` value as `$REV`, which is a
        shell variable, or as `${{ env.REV }}`, which GitHub substitutes
        before the shell ever starts -- and the second spelling is not a
        shell variable reference, so a pickup keyed on `$NAME` folds in
        nothing and the fetch reads as innocent. The substitution is
        case-insensitive because GitHub's is, which `${{ Env.REV }}` walked
        past; the pickup below is case-sensitive because a shell's variables
        are, and folding in a value `$rev` cannot be reading would be a false
        red rather than a catch. A value can also name
        another value, `REV: ${{ env.A }}`, which is text that matches
        neither the refspec nor the head pattern while `A` never gets
        followed. So the script is expanded, the pickup runs over the
        expansion, the picked-up values are expanded too, and the pickup
        repeats until it stops finding names. Both shapes were live against
        the first version of this half and both have mutation rows now, as do
        `printenv NAME` and the indirect `${!PTR}` -- two more ways to spell a
        read that a pickup keyed on `$NAME` cannot see, and two the rule
        below now refuses without reading them at all. The repeat is capped
        by `_ENV_EXPANSION_LIMIT`, the cap `_expand_env` uses.

        Past that, the answer is refusal rather than more syntax, and what
        is refused is the environment rather than the script. A step whose
        `env:` carries the pull request's head is refused, whether or not
        anything in the script appears to read that value. It has to be.
        `shell: python` makes the read `os.environ["REV"]` and no `$REV` is
        written anywhere; an inline `python3 -c` does the same under the
        default shell; and any program a script starts inherits the whole
        environment without naming a field of it. An earlier version
        enumerated the shell constructs that defeat the pickup --
        `printenv`, indirect expansion, `eval`, `env`, `declare`, `source` --
        and refused only a step that used one of them. The verdict was right
        and the condition on it was a guess: a denylist over idioms, one
        idiom behind by construction, with both interpreters above already
        past it. The condition is gone and the verdict stayed. Same answer an
        unresolvable ref expression gets, one field along: "this file cannot
        tell what this does" reads as unsafe.

        The same reasoning took the fetch gate off this rule a round later,
        which is the only change of substance: the head in a step's
        environment is refused now whatever else the step does, where before
        it was refused only if the step also said `fetch`. A step that reads
        `github.head_ref` to name a label reds today. That is the trade the
        ref allowlist already makes, and a step is a place where a comment
        fits.

        The `ref:` half, by contrast, is an allowlist, and that is the whole
        design. It began as a denylist over three nouns -- `pull_request`,
        `head`, `merge` -- and `${{ github.event.after }}` is the pull
        request's head SHA on every `synchronize`, spelled with none of them.
        One line in a workflow and this test reported `ok`. Naming the safe
        refs instead means anything this file cannot resolve reads as unsafe
        rather than as innocent text: an `env.X` chain, a
        `steps.X.outputs.Y`, a value laundered through `$GITHUB_ENV`. That is
        the direction to be wrong in. The cost is a false red the day
        somebody adds a legitimately safe expression, which is a line of
        review, and the docstring is where they will look.

        The list -- `_SAFE_CHECKOUT_EXPRESSIONS` -- is one entry, and the
        three obvious candidates are off it for two different reasons, only
        one of which is about safety. Since the 2025-12-08 change above,
        `github.ref` and `github.sha` *are* the entry that is on the list:
        the default branch, and the head commit of the default branch.
        Refusing them is a one-spelling rule, which is worth having for its
        own sake -- one way to write a thing is one thing to review -- and it
        is not a tightening; reporting it as one overstates what it bought.
        `github.base_ref` is the omission that stands on its own. It is the
        pull request's base branch, chosen by the pull request's *author*
        from the branches that already exist in this repository, and a stale
        unprotected branch here is not the default branch and is not
        necessarily code anyone has read this year. No fork can write any of
        the three -- that needs push access here -- so this entry is about
        the author of the pull request rather than about the fork the test is
        named for. Both live carriers use the default branch anyway.

        `ref:` is only half of what a checkout resolves. The action looks the
        ref up inside whatever `repository:` says, so the allowlist above
        says nothing on its own: `repository: ${{
        github.event.pull_request.head.repo.full_name }}` with `ref: ${{
        github.event.repository.default_branch }}` is the fork's copy of its
        own default branch, which the fork wrote, and every assertion on the
        ref passes. `repository:` therefore gets its own allowlist,
        `_SAFE_CHECKOUT_REPOSITORIES` -- absent, or `github.repository` --
        and unlike the ref half it refuses a literal outright, including
        this repository's own name. The ref half tolerates literals because
        `main` is an ordinary ref; there is no equivalent reason to write
        out a repository when the expression for it exists, and a literal is
        exactly where a lookalike owner would go unread.

        Both inputs are read through `_with_inputs`, case-insensitively,
        which is not decoration. The runner passes an input to an action as
        `INPUT_<NAME>`, upper-casing the key, and `core.getInput` looks it up
        the same way, so `Ref:` is the ref `actions/checkout` checks out and a
        `with.get("ref")` reads none of it. The same helper maps a YAML null
        to the empty string, because `str(None)` is a truthy `"None"` and a
        bare `ref:` is a checkout with no ref wearing a literal's clothes.
        This is the `uses:` bug one field along -- that filter was
        case-sensitive too until `Actions/checkout` was found walking past it
        -- and the pattern in both is the same: each round of review finds the
        input the last round did not read.

        The rule is over any step that takes a `ref:`, not over
        `actions/checkout`. A third-party checkout action fetches the same
        code, and a rule about one vendor is a rule about that vendor.
        `actions/checkout` keeps the one extra obligation argued at the top
        of this docstring -- it must carry a `ref:` at all -- because it is
        the action whose no-ref behaviour is documented and therefore the one
        this file can say anything about. And a job that calls a reusable
        workflow is refused
        outright: its steps live in a file keyed `workflow_call`, so it is
        not in `consumers` either, and this test would inspect nothing while
        reporting `ok`.

        Not covered, each line re-run against this version of the file on
        2026-09-19 rather than carried forward:

        The API in a spelling none of the rules over it read. Those rules
        are an allowlist of three `gh` verbs, an allowlist of two subcommands
        under the two of them that take one, the same subcommand allowlist
        again anchored on the argument rather than on the program, the
        `/pulls` and `/issues` path segments, the `pull/N.diff` web endpoint,
        and either of those two namespaces on the Octokit client a `script:`
        is handed. Every allowlist among them replaced a denylist that lost,
        and each loss is recorded at the constant. The verb allowlist is the
        sharpest reason: `gh alias set co 'pr checkout'` renames a governed
        verb into an ungoverned one, so no list of dangerous verbs can be
        finished.

        What is left is the call made without naming `pulls` or `issues` and
        without an allowlisted `gh` verb: a GraphQL query for
        `pullRequest(number:)`, or `github.request("GET
        /repos/{owner}/{repo}/pulls/{n}")` spelled with the path in a
        variable. The GraphQL one is the one worth writing down: that
        endpoint is a single URL with no path in it at all, and nothing in
        this file reads a query document.

        A client that is not `gh`, spelled plainly. The argument-shape
        backstop fires on `pr <subcommand>` only where the program word is
        one review cannot read, so `hub pr checkout "$N"` -- a real client,
        with a plain name of its own -- is not refused. That is a hole left
        open rather than one unnoticed: closing it means a denylist of
        program names, which is the thing the allowlists here exist to stop
        needing, and the alternative measured against a live probe was
        refusing `./tools/high pr checkout 42`, a local program that cannot
        reach GitHub at all. The argument is at `_READABLE_PROGRAM`.

        A local composite action. `uses: ./.github/actions/x` moves the
        steps into a file keyed `runs.steps` that nothing here reads, and
        the job's own file shows one innocent line.

        The half of `$GITHUB_ENV` laundering that does not name an
        expression. A step that writes `REV=${{ github.event.after }}` to
        `$GITHUB_ENV` for a later step to read is refused now -- the writing
        step names the field, and every step is read. A step that writes
        what a GraphQL query returned is not, for the same reason the API
        line above is not: the backstop reads four spellings and that is
        not one of them.

        A step that splits the accessor itself, in either language:
        `globalThis['proc'+'ess']['e'+'nv']['GITHUB_EVENT_'+'PATH']`, or
        `getattr(__import__("o"+"s"), "environ")`. `process.env`,
        `process['env']`, `os.environ`, a bare `environ[...]` and `getenv`
        are all refused as of 2026-09-19, and so is the directory the file
        sits in, so this residual now takes string arithmetic over the
        accessor rather than over the variable's name. Narrower is not
        closed.

        A container image that names the fork without an expression.
        `container:` and `services:` are read as of 2026-09-19 and held
        against the expression allowlist, which refuses `image: ghcr.io/${{
        github.event.pull_request.head.repo.full_name }}/x`. A literal
        `image: ghcr.io/somefork/x:latest` is not refused, and cannot be: a
        pinned literal image is how a job gets a toolchain, and the rule that
        refuses a literal `repository:` on a checkout has an expression to
        recommend instead, which this one does not. The same goes for a
        literal `--entrypoint` inside `options:`. What is reviewable here is
        the registry path, which is a line in a diff.

        Read that list as examples rather than as the boundary. An earlier
        version of it was written as though it were complete and did not
        mention `github.event.after`, which turned out to be both live and
        shorter than either path it did name. Entries have come off it as
        well as on: `steps.X.outputs.Y` is an expression like any other and
        the allowlist refuses it for not being recognised, a `script:` that
        builds its own client still writes the word `context` to reach the
        payload, the `$GITHUB_ENV` write above lost its easy half, and the
        API line lost the three shapes a backstop could name. On 2026-09-19
        it lost `$C pr checkout` with `C: gh`, which the argument-shape
        backstop now reads, and the whole of the `issues` namespace, which
        this list described as two endpoints that return a commit -- a claim
        that was simply false, because `GET /repos/O/R/issues/N` hands back
        the pull request's `patch_url` and this repository routes no read
        through that namespace at all.
        """
        consumers = [
            (path, document)
            for path, document in _workflow_documents()
            if "pull_request_target" in (document.get("on") or {})
        ]
        self.assertTrue(
            consumers,
            "no pull_request_target workflows found; the filter is wrong",
        )

        saw_a_checkout = False
        for path, document in consumers:
            with self.subTest(workflow=path.name):
                workflow_env = _env_values(document)
                for job_name, job in (document.get("jobs") or {}).items():
                    # A job that calls a reusable workflow carries no `steps:`
                    # of its own, and the called file is keyed `workflow_call`
                    # rather than `pull_request_target`, so it is not in
                    # `consumers` either. Its checkout would be real and this
                    # test would inspect nothing. Refuse rather than pass: the
                    # rule this enforces is worth more than the convenience.
                    self.assertNotIn(
                        "uses",
                        job or {},
                        f"{path.name}:{job_name} calls a reusable workflow, "
                        "whose steps this test cannot see; inline it, or "
                        "teach this test to follow it",
                    )
                    scope_env = workflow_env | _env_values(job)
                    # The image the steps run in, and the images started
                    # beside them. A job's `container:` is where the job's
                    # code actually is -- the steps are innocent and the
                    # image is the fork's -- and nothing here read it until
                    # 2026-09-19. Read as one text rather than field by
                    # field, and held against the same expression allowlist
                    # the scripts are, so an image, a `--entrypoint` in
                    # `options:` and a `credentials:` password are covered by
                    # the same rule. See `_container_blocks`.
                    for label, block in _container_blocks(job):
                        # The container's `env:` is set in the container, so
                        # every step of the job has it. A service's is not:
                        # it belongs to a process on the job's network, and
                        # it is read below as part of that block's own text.
                        if label == "container":
                            scope_env = scope_env | _env_values(block)
                        container_text = "\n".join(_flatten(block))
                        unsafe_container = sorted({
                            expression
                            for expression in _EXPRESSION.findall(container_text)
                            if _normalise_expression(expression)
                            not in _SAFE_SCRIPT_EXPRESSIONS
                        })
                        self.assertEqual(
                            [],
                            unsafe_container,
                            f"{path.name}:{job_name} names "
                            f"{unsafe_container} in its `{label}:`, which is "
                            "not among the expressions known to be "
                            "independent of the pull request. The image a "
                            "job runs in is the job's code, whatever its "
                            "steps say",
                        )
                        # The literal backstops the scripts get, for text
                        # carrying no expression for the allowlist to read.
                        for pattern, what in (
                            (_PULL_REQUEST_HEAD, "head"),
                            (_PULL_REQUEST_REF, "ref namespace"),
                        ):
                            self.assertIsNone(
                                pattern.search(container_text),
                                f"{path.name}:{job_name} names the pull "
                                f"request's {what} in its `{label}:`",
                            )
                    for step in (job or {}).get("steps") or []:
                        step_env = scope_env | _env_values(step)
                        # GitHub resolves `uses:` case-insensitively, so this
                        # match has to be too. `Actions/checkout` runs the same
                        # action and would otherwise walk past the filter.
                        uses = str((step or {}).get("uses", "")).lower()
                        refs = _with_inputs(step, "ref")
                        if uses.startswith("actions/checkout"):
                            saw_a_checkout = True
                            self.assertTrue(
                                any(refs),
                                "a checkout on pull_request_target with no "
                                "ref: takes GITHUB_REF, which is the default "
                                "branch on this trigger and was the pull "
                                "request's base branch until 2025-12-08. Say "
                                "which ref you mean: an explicit ref is "
                                "reviewable and a default is not",
                            )
                        # Every action that takes a `ref:`, not only
                        # `actions/checkout`. A third-party checkout action
                        # fetches the same code, and naming one vendor turns
                        # the rule into a rule about that vendor.
                        for ref in refs:
                            if not ref:
                                continue
                            resolved = _expand_env(ref, step_env)
                            for expression in _EXPRESSION.findall(resolved):
                                self.assertIn(
                                    _normalise_expression(expression),
                                    _SAFE_CHECKOUT_EXPRESSIONS,
                                    f"{path.name}: the checkout ref names "
                                    f"`{expression}`, which is not one of the "
                                    "refs known to be independent of the pull "
                                    "request",
                                )
                            # Whatever is left once the expressions are gone
                            # is literal text, where `refs/pull/N/head` needs
                            # no interpolation at all.
                            literal = _EXPRESSION.sub("", resolved).lower()
                            # Belt and braces around the loop above: literal
                            # text holding a `$` is an expression
                            # `_EXPRESSION` did not match, and an expression
                            # nothing checked against the allowlist has
                            # reached the three-word scan below as innocent
                            # prose. That is exactly what a missing
                            # `re.DOTALL` did. A regex that misses one should
                            # red here rather than fall through.
                            self.assertNotIn(
                                "$",
                                literal,
                                f"{path.name}: the checkout ref carries an "
                                "expression this test could not parse, so "
                                "nothing checked it against the allowlist",
                            )
                            for fragment in ("pull", "head", "merge"):
                                self.assertNotIn(
                                    fragment,
                                    literal,
                                    f"{path.name}: the checkout ref derives "
                                    "from the pull request",
                                )
                        # A ref is resolved inside a repository, so the
                        # ref allowlist says nothing on its own: the default
                        # branch of the *fork* is code the fork wrote. Same
                        # shape of rule, one input along.
                        for repository in _with_inputs(step, "repository"):
                            resolved = _expand_env(repository, step_env)
                            for expression in _EXPRESSION.findall(resolved):
                                self.assertIn(
                                    _normalise_expression(expression),
                                    _SAFE_CHECKOUT_REPOSITORIES,
                                    f"{path.name}: the checkout names the "
                                    f"repository `{expression}`, which is not "
                                    "known to be this repository -- a safe "
                                    "ref resolved there is somebody else's "
                                    "code",
                                )
                            self.assertEqual(
                                "",
                                _EXPRESSION.sub("", resolved).strip(),
                                f"{path.name}: the checkout names a literal "
                                "repository; say `${{ github.repository }}` "
                                "or leave it out, which is the same thing and "
                                "cannot be a lookalike",
                            )
                        # The step's script, as the shell will see it:
                        # `run:` plus every string `with:` input plus the
                        # `shell:` that runs them and the `defaults.run` the
                        # job and the workflow set, with `${{ env.NAME }}`
                        # substituted and backslash-newlines joined. `run:`
                        # alone is not enough -- see the docstring -- and
                        # neither is the raw text, because GitHub substitutes
                        # an expression before the shell starts and a shell
                        # joins a continued line before it runs one.
                        #
                        # There is no pickup of the `env:` values the script
                        # appears to name, and there has not been since
                        # 2026-09-19. There was one for three rounds, keyed on
                        # `$NAME`, `${NAME}`, `${!NAME}` and `printenv NAME`,
                        # and by the end it decided nothing: the wholesale
                        # refusal below reads every value in the step's
                        # environment whether or not the script names it, and
                        # neither literal backstop matches across the newline
                        # a fold would have joined on, so every string it
                        # folded in was one some rule already read singly.
                        # Measured at the empty mapping in three successive
                        # rounds, and not one verdict moved. A loop that
                        # changes no verdict is a loop a reader has to
                        # disprove, which is a worse cost than the redundancy
                        # was worth.
                        script = _join_continuations(
                            _expand_env(
                                _step_scripts(step, job, document), step_env
                            )
                        )
                        # The `refs/pull` namespace is refused wherever it
                        # appears, and so are the two short forms that reach
                        # it without the prefix: they are literals, they need
                        # no expression, and there is no innocent reason to
                        # write one down in a workflow that must not have the
                        # fork's code. Refused by namespace rather than by
                        # named ref -- see `_PULL_REQUEST_REF` for the three
                        # spellings that taught it the difference.
                        self.assertIsNone(
                            _PULL_REQUEST_REF.search(script),
                            f"{path.name}: a step names the pull request's "
                            "ref namespace, which is the checkout action's "
                            "hazard without the checkout action",
                        )
                        # Everything the step's environment will actually
                        # hold, resolved. Every value rather than the ones
                        # the pickup found: the rules below are about what
                        # the step can reach, and a program reaches all of
                        # its environment without naming a field of it.
                        environment = {
                            name: _join_continuations(
                                _expand_env(value, step_env)
                            )
                            for name, value in step_env.items()
                        }
                        # The ref half's rule, one field along. A step may
                        # name only expressions that are known not to be the
                        # pull request, in its script or in anything its
                        # environment carries, and everything else -- an
                        # event field nobody here has heard of, an `env.X`
                        # that did not resolve -- is refused for being
                        # unrecognised rather than allowed for not matching a
                        # list of nouns.
                        unsafe = sorted({
                            expression
                            for text in [script, *environment.values()]
                            for expression in _EXPRESSION.findall(text)
                            if _normalise_expression(expression)
                            not in _SAFE_SCRIPT_EXPRESSIONS
                        })
                        self.assertEqual(
                            [],
                            unsafe,
                            f"{path.name}: a step names {unsafe}, which "
                            "is not among the expressions "
                            "known to be independent of the pull request. If "
                            "one of them is, add it to "
                            "_SAFE_SCRIPT_EXPRESSIONS and say why",
                        )
                        # And the same rule in JavaScript, which the
                        # expression scan cannot read because a
                        # `github-script` body interpolates nothing: it is
                        # handed the payload as `context` and reaches the
                        # head by property access.
                        reached = sorted({
                            match.group(0).strip()
                            for match in _SCRIPT_CONTEXT.finditer(script)
                            if (match.group(1) or match.group(2) or "").lower()
                            not in _SAFE_SCRIPT_CONTEXTS
                        })
                        self.assertEqual(
                            [],
                            reached,
                            f"{path.name}: a script: input reaches "
                            f"{reached}, which is the webhook "
                            "payload -- the same fields the expression "
                            "allowlist refuses, in the language that runs",
                        )
                        # Belt and braces behind both allowlists, for text
                        # that carries no expression for them to read.
                        self.assertIsNone(
                            _PULL_REQUEST_HEAD.search(script),
                            f"{path.name}: a step names the pull "
                            "request's head, which is the checkout action's "
                            "hazard without the checkout action",
                        )
                        # The environment is refused wholesale rather than
                        # followed. Whether this step's script reads a value
                        # is a question about a program in a language nobody
                        # here parses -- `shell: python` reads it as
                        # `os.environ["REV"]`, an inline `python3 -c` does the
                        # same under bash, and any program a script starts
                        # inherits the lot without naming anything -- so a
                        # step that has the head in its environment at all is
                        # refused. The cost is a false red on a step that
                        # carries the head for a label or a log line, which
                        # is a line of review.
                        carried = sorted(
                            name
                            for name, value in environment.items()
                            if _PULL_REQUEST_HEAD.search(value)
                            or _PULL_REQUEST_REF.search(value)
                        )
                        self.assertEqual(
                            [],
                            carried,
                            f"{path.name}: a step's environment carries "
                            f"the pull request's head as {carried}. Whether "
                            "the script reads it is not a question this test "
                            "can answer, so it does not assume the answer",
                        )
                        # Everything the step can say, as one text: the
                        # three rules below are about a request rather than
                        # about a ref, and a request assembled out of an
                        # `env:` value is the same request.
                        reachable = "\n".join([script, *environment.values()])
                        # The ref namespace reached without being named.
                        # `refs/pull/N/head` is advertised to a plain
                        # `ls-remote` and copied by a `refs/*` refspec, so a
                        # step that enumerates the remote's refs holds the
                        # head whether or not it spelled `pull` anywhere.
                        # Read over `reachable` rather than over the script
                        # and the values the pickup followed, for the reason
                        # the three rules below are: this is a rule about a
                        # command, and a command assembled out of an `env:`
                        # value is the same command -- the wholesale refusal
                        # above reads those values for the *head*, which an
                        # enumeration does not carry. See
                        # `_REMOTE_REF_ENUMERATION` for why the rule is about
                        # that reach rather than about the two spellings that
                        # demonstrated it.
                        self.assertIsNone(
                            _REMOTE_REF_ENUMERATION.search(reachable),
                            f"{path.name}: a step enumerates the remote's "
                            "refs, or copies them whole with `clone "
                            "--mirror` or any prefix of it git accepts, "
                            "which reaches `refs/pull/N/head` "
                            "without the step naming it -- grep the listing "
                            "and the head SHA is a fetch by object name away",
                        )
                        # And the payload over HTTP, which none of the
                        # rules above read either. Refused on the request
                        # rather than on what comes back, for the same reason
                        # the file is: what returns is the head, the diff or
                        # the branch, and there is no innocent one among them.
                        self.assertIsNone(
                            _PULL_REQUEST_API.search(reachable),
                            f"{path.name}: a step reads the pull request "
                            "through the API -- a `/pulls` or `/issues` "
                            "request, either of those namespaces on the "
                            "Octokit client a `script:` input is handed as "
                            "`github`, or the tokenless `pull/N.diff` web "
                            "endpoint. A pull request is an issue to this "
                            "API, so `/issues/N` hands back its `patch_url`; "
                            "the write a labelling step wants is `gh issue "
                            "comment`, which names no path. This is the "
                            "event's own fields fetched over HTTP, in the "
                            "one language none of the rules above read",
                        )
                        # The same request made by the CLI, where the rule
                        # is an allowlist twice over rather than a pattern
                        # over the shapes that have been seen. The outer one
                        # is the verb. The walk read only `gh pr` until
                        # 2026-09-19 and skipped every other verb of the CLI,
                        # so `gh alias set co 'pr checkout'` renamed the verb
                        # out of its sight and `gh co "$N"` then ran the
                        # checkout under a word GitHub never shipped. See
                        # `_SAFE_GH_VERBS` for the three that are on the list
                        # and what each is doing there.
                        unsafe_verbs = _unsafe_gh_verbs(reachable)
                        self.assertEqual(
                            [],
                            unsafe_verbs,
                            f"{path.name}: a step runs {unsafe_verbs}. The "
                            "only `gh` verbs a step of a workflow holding "
                            "this token may run are "
                            f"{sorted(_SAFE_GH_VERBS)}; every other one is "
                            "refused for not being on the list rather than "
                            "for being recognised, because an alias renames "
                            "a verb this file does recognise into one it "
                            "does not. If a new one is safe, add it to "
                            "_SAFE_GH_VERBS and say why",
                        )
                        # And the inner one, the subcommand under a verb that
                        # got past it. `gh pr` is a tree of about twenty and
                        # all but two of them hand back the pull request, its
                        # diff or its head; naming the dangerous ones is a
                        # list that GitHub gets to extend. See
                        # `_SAFE_GH_PULL_REQUEST_SUBCOMMANDS` for what the two
                        # on it are and why the walk that finds them is a
                        # function rather than a regex.
                        unsafe_gh = _unsafe_gh_pull_request_commands(reachable)
                        self.assertEqual(
                            [],
                            unsafe_gh,
                            f"{path.name}: a step runs {unsafe_gh}. The only "
                            "`gh pr` and `gh issue` subcommands a step of a "
                            "workflow holding this token may run are "
                            f"{sorted(_SAFE_GH_PULL_REQUEST_SUBCOMMANDS)}; "
                            "every other one returns the pull request, its "
                            "diff or its head. If a new one is safe, add it "
                            "to _SAFE_GH_PULL_REQUEST_SUBCOMMANDS and say why",
                        )
                        # And the same list once more, anchored on the
                        # argument rather than on the program, because the
                        # program name is the half of a command line a regex
                        # cannot read. `xargs gh`, `"$(command -v gh)"`,
                        # `"$GH"` and `g'h'` are four ways to run the CLI
                        # without writing `gh` where `_GH_COMMAND` looks for
                        # it, all four were green, and the fifth is free. Each
                        # of them still writes `pr` and then a subcommand, so
                        # that is what this reads. See `_GH_PULL_REQUEST_WORD`
                        # for why it is a backstop under the two allowlists
                        # rather than a replacement for them.
                        unsafe_pr = _unsafe_pull_request_subcommands(reachable)
                        self.assertEqual(
                            [],
                            unsafe_pr,
                            f"{path.name}: a step runs {unsafe_pr}. Whatever "
                            "program a step names, `pr` followed by anything "
                            "outside "
                            f"{sorted(_SAFE_GH_PULL_REQUEST_SUBCOMMANDS)} is "
                            "the pull request being read, and the program "
                            "name is the part of a command line this test "
                            "cannot identify",
                        )
                        # And the payload as a file, which is neither an
                        # expression nor a `context` property and so reaches
                        # neither allowlist. Refused on the read rather than
                        # on what is read out of it: every field in that file
                        # arrived from the pull request, and no step in a
                        # workflow that must not have the fork's code has
                        # business in any of them. What counts as the read is
                        # argued at `_EVENT_PAYLOAD_FILE`, and it is ten
                        # patterns rather than one because the variable, each
                        # piece of the two paths it holds -- the runner's and
                        # the one a container job sees it bind-mounted at --
                        # and the process environment that carries it in each
                        # of the two languages a step can be written in are
                        # all ways to the same bytes.
                        # And the variables the runner sets without being
                        # asked, which are the payload again in the one
                        # language a step does not have to write anything to
                        # get: `$GITHUB_HEAD_REF` is the pull request's
                        # branch and `$GITHUB_ACTOR` is its author. An
                        # allowlist, over the environment's names as well as
                        # its values, because a name is where a step would
                        # shadow one. See `_SAFE_RUNNER_VARIABLES` for what
                        # the base repository decides and what the pull
                        # request does.
                        runner_named = sorted({
                            name
                            for text in [reachable, *environment]
                            for name in _RUNNER_VARIABLE.findall(text)
                            if name not in _SAFE_RUNNER_VARIABLES
                        })
                        self.assertEqual(
                            [],
                            runner_named,
                            f"{path.name}: a step names {runner_named}, "
                            "which the runner sets from the pull request "
                            "rather than from this repository. The variables "
                            "a step of a workflow holding this token may "
                            "name are the ones the base repository decides; "
                            "if one of these is safe, add it to "
                            "_SAFE_RUNNER_VARIABLES and say why",
                        )
                        # And the same variables reached without being
                        # named, which the allowlist above cannot see for
                        # the reason `_REMOTE_REF_ENUMERATION` exists: a
                        # scan for a name reads what the step spelled, and a
                        # step that dumps its environment spelled nothing.
                        # See `_ENVIRONMENT_ENUMERATION` for why this is a
                        # rule about a request rather than the denylist over
                        # idioms that came out of this file the same day.
                        self.assertIsNone(
                            _ENVIRONMENT_ENUMERATION.search(reachable),
                            f"{path.name}: a step reads its whole "
                            "environment -- `env`, `printenv`, `declare "
                            "-p`, `export -p`, `compgen -e` or a bare "
                            "`set` -- which hands it `GITHUB_HEAD_REF` and "
                            "`GITHUB_ACTOR` without naming either, so the "
                            "allowlist over the names decides nothing. Name "
                            "the variable you want",
                        )
                        self.assertIsNone(
                            _EVENT_PAYLOAD_FILE.search(reachable),
                            f"{path.name}: a step reaches the webhook "
                            "payload on disk -- `GITHUB_EVENT_PATH`, the "
                            "runner's `$RUNNER_TEMP/_github_workflow/"
                            "event.json` or the `/github/workflow` a "
                            "container job sees that directory at, or the "
                            "process environment that holds the path, "
                            "reached through `process.env`, `os.environ`, "
                            "`environ` or `getenv`. Every field in that "
                            "file came from the pull request, and it is the "
                            "one language neither allowlist reads",
                        )
                # `_permission_scopes` rather than the blocks themselves:
                # `permissions: write-all` is a string, and a filter that
                # read only mappings granted this workflow every scope
                # without either assertion below seeing a thing.
                for scope in _permission_scopes(document):
                    self.assertNotEqual(
                        "write",
                        scope.get("contents"),
                        f"{path.name}: a pull_request_target workflow holds "
                        "contents: write",
                    )
                    self.assertNotEqual(
                        "write",
                        scope.get("id-token"),
                        f"{path.name}: a pull_request_target workflow holds "
                        "id-token: write",
                    )
        self.assertTrue(
            saw_a_checkout,
            "no pull_request_target workflow runs a checkout step any more; "
            "the ref half of this test examined nothing",
        )

    def test_B4_contents_write_is_confined_to_the_release_path(self) -> None:
        """The credential that can push to this repository, and where it lives.

        The release path is the only thing that needs it. Asserting the set
        rather than the absence keeps the grant reviewable: adding a holder is
        a decision someone makes on purpose. The two added on main since this
        suite was written are both that path and both `workflow_dispatch`-only,
        repo-gated, with the grant job-scoped: nightly-pipeline's
        promote-to-staging step, and release-publish's publish job.
        """
        holders = set()
        for path, document in _workflow_documents():
            # Through `_permission_scopes`, which expands `write-all`: the
            # widest grant GitHub offers is spelled as a string, and reading
            # only the mappings would leave the holder set below silent about
            # the one workflow that granted everything.
            for scope in _permission_scopes(document):
                if scope.get("contents") == "write":
                    holders.add(path.name)
        self.assertEqual(
            {
                "rc-create-tag.yml",
                "rc-tag-validated.yml",
                "rc-release-pipeline.yml",
                "nightly-pipeline.yml",
                "release-publish.yml",
            },
            holders,
        )


class B5WhatTheApproverSeesIsWhatWillBeApplied(unittest.TestCase):
    """B5: the invariant we never stated, on the input to the one we did."""

    def setUp(self) -> None:
        scripts = h.REPO_ROOT / "agents/platform/skills/fleet-audit/scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import audit_report

        self.audit_report = audit_report

    def test_B5_precondition_the_renderer_still_sanitises_its_inputs(self) -> None:
        """The markdown-injection hardening that *does* exist, pinned.

        `_ident` flattens newlines and replaces backticks because either one
        ends an inline code span and renders the rest of an attacker's value as
        markup. `_cell` escapes the pipe that would otherwise forge a table
        column. Both are real controls and neither should be lost while the
        expected failure below is being fixed.

        It also pins that all four renderers the expected failure calls still
        EXIST. That belongs in a passing test rather than in the violation: an
        expected failure records any exception, so an AttributeError from a
        renamed renderer would be counted among the twelve while the bidi
        assertions never ran -- and the violation could then never close, since
        an unexpected success cannot fire from a test that always raises.
        `audit_report.py` is a 3000-line module nobody editing it would connect
        to this suite, which is why the anchors in SOURCES back this up.
        """
        self.assertEqual("a'b", self.audit_report._ident("a`b"))
        self.assertEqual("a b", self.audit_report._ident("a\nb"))
        self.assertEqual("a\\|b", self.audit_report._cell("a|b"))
        self.assertEqual("a b", self.audit_report._cell("a\nb"))
        for name in ("_ident", "_cell", "trim_command", "trim_excerpt"):
            with self.subTest(renderer=name):
                self.assertTrue(
                    callable(getattr(self.audit_report, name, None)),
                    f"audit_report.{name} is gone; the B5 violation below calls "
                    "it and would record the AttributeError as its expected "
                    "failure",
                )

    @h.known_violation("B5", "overnight-b/findings.md 2.4")
    def test_B5_rendered_evidence_carries_no_direction_or_width_trickery(self) -> None:
        """KNOWN VIOLATION. Bidi and zero-width characters survive into the body.

        The fleet audit requires every finding to carry "the exact read-only
        command that produced it and a verbatim excerpt of the output". Those
        are attacker-controlled bytes rendered inside a block the system labels
        evidence -- anyone who can write a Pod log can write to it.

        The renderer is careful about markdown: backticks, pipes and newlines
        are all neutralised, and the fence is chosen to be longer than any
        run inside the content. What it does not handle is the class B5 names
        explicitly: `U+202E` reverses the displayed order of everything after
        it, and `U+200B`/`U+FEFF` hide inside an identifier. So a reader can be
        shown `kubectl get pods` for a value that is not that, inside the
        block the document calls evidence.

        This is B5's whole point. B2 makes the human the decision point; without
        B5 that boundary has no integrity requirement on its only input.
        """
        trickery = {
            "‮": "right-to-left override",
            "‭": "left-to-right override",
            "⁦": "left-to-right isolate",
            "​": "zero-width space",
            "﻿": "zero-width no-break space",
            "‎": "left-to-right mark",
        }
        for character, description in trickery.items():
            payload = f"kubectl get pods{character} --all-namespaces"
            for name, function in (
                ("_ident", self.audit_report._ident),
                ("_cell", self.audit_report._cell),
                ("trim_command", self.audit_report.trim_command),
                ("trim_excerpt", self.audit_report.trim_excerpt),
            ):
                with self.subTest(character=description, renderer=name):
                    self.assertNotIn(
                        character,
                        function(payload),
                        f"{name} passes {description} through to the approver",
                    )


class B6NoSelfApproval(unittest.TestCase):
    """B6: an approver must hold authority sufficient to make the change directly."""

    def test_B6_the_gitops_template_names_no_automation_identity(self) -> None:
        """The one thing GitHub can actually enforce.

        Self-approval is blocked on account identity, so two agent identities
        defeat it, and approval *count* can probably be satisfied by a GitHub
        App token -- undocumented, and Minty already mints these. The two rules
        a bot cannot satisfy are CODEOWNERS review and the ruleset
        required-reviewer team rule, because apps are not eligible code owners
        and cannot be team members. So the mechanism has to be a named human
        team containing no automation identity, and this asserts the template
        we hand customers says that.
        """
        rules = [
            line
            for line in h.text("codeowners_example").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        owners = [
            owner for line in rules for owner in re.findall(r"@[\w.-]+(?:/[\w.-]+)?", line)
        ]
        self.assertTrue(owners, "the template names no owners at all")
        for owner in owners:
            with self.subTest(owner=owner):
                self.assertNotIn("[bot]", owner)
                self.assertFalse(
                    owner.endswith("-agent"),
                    "an agent identity is named as a code owner",
                )
                self.assertIn(
                    "/",
                    owner,
                    "a code owner must be a team rather than an individual "
                    "account, so that the required review cannot be satisfied "
                    "by whoever happens to be on call",
                )

    def test_B6_every_guarded_path_in_the_template_has_an_owner(self) -> None:
        """A CODEOWNERS entry that covers nothing is the trap this avoids.

        The six path classes the branch-protection note calls guarded --
        provisioning, agents, namespaces, policy, knowledge and .kube-agents
        -- each need a rule, or the ruleset that requires code-owner review on
        them requires review from nobody. The last two are the declared-intent
        pair: a knowledge/ note can move an audit posture off the ledger, and
        .kube-agents/intent.yaml decides which paths' notes can.

        A class is matched as a whole path segment, not as a substring: the
        `/.kube-agents/` rule contains the letters `agents`, and a substring
        test would let it stand in for the deleted `/clusters/*/agents/` rule.
        """
        text = h.text("codeowners_example")
        rules = [
            line.split()[0].strip("/").split("/")
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for guarded in ("provisioning", "agents", "namespaces", "policy", "knowledge", ".kube-agents"):
            with self.subTest(path=guarded):
                self.assertTrue(
                    any(guarded in segments for segments in rules),
                    f"no CODEOWNERS rule covers {guarded}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
