#!/usr/bin/env python3
"""Which tier a data.json run belongs to, and the filters every consumer uses.

The collector records three kinds of run under one schema (SCHEMA.md,
``runs[].tier``): the presubmit gate's, one per pull-request build of
``pull-kube-agents-smoke-test``; the nightly periodics', one per build of
``ci-kube-agents-eval-nightly`` (and of ``ci-kube-agents-eval-nightly-writers``
when a night is split across two jobs, ``nightly.py``) against ``main`` with
no pull request; and the GitLab lane's, one per build of
``pull-kube-agents-smoke-test-gitlab``, the forge-grading subset of the
presubmit matrix driven against a pool project's GitLab repository
(``EVAL_FORGE=gitlab``, kube-agents#2394). Every
gate verdict -- the health adjudicator's rules, the "is this red mine?"
classification, the Brief's runs list -- is a statement about the presubmit,
so each of those consumers filters through ``presubmit_runs`` before it
counts anything; the GitLab lane is listed on its own and counts nowhere else. A document written before the field existed carries no
``tier`` at all; that is the presubmit, because nothing else was collected
then. A value this module does not know is no tier of these: a producer that
tags runs some new way must not have them counted as the gate's by default.

Only stdlib, so the consumers that avoid dependencies can import it.
"""

from __future__ import annotations

TIER_PRESUBMIT = "presubmit"
TIER_NIGHTLY = "nightly"
TIER_GITLAB = "gitlab"
TIERS = (TIER_PRESUBMIT, TIER_NIGHTLY, TIER_GITLAB)
# The tiers whose runs feed the per-case record (cases[], the Cases page's
# rates): the GitLab lane feeds it nothing, so it has no rate.
CASE_TIERS = (TIER_PRESUBMIT, TIER_NIGHTLY)

# The key on a data.json run, and what an ABSENT value reads as: the
# presubmit, the only tier that existed before the key did. An unrecognised
# value is kept as written and matches no filter below.
TIER_KEY = "tier"
DEFAULT_TIER = TIER_PRESUBMIT


def run_tier(run) -> str:
    """The tier of one run: its ``tier`` as written, else presubmit when absent."""
    tier = run.get(TIER_KEY) if isinstance(run, dict) else None
    return tier if isinstance(tier, str) and tier else DEFAULT_TIER


def is_presubmit(run) -> bool:
    return run_tier(run) == TIER_PRESUBMIT


def is_nightly(run) -> bool:
    return run_tier(run) == TIER_NIGHTLY


def presubmit_runs(runs) -> list:
    """The gate's runs: everything a gate verdict is allowed to count."""
    return [run for run in runs or [] if is_presubmit(run)]


def nightly_runs(runs) -> list:
    return [run for run in runs or [] if is_nightly(run)]


def is_gitlab(run) -> bool:
    return run_tier(run) == TIER_GITLAB


def gitlab_runs(runs) -> list:
    """The GitLab lane's runs: the Brief's own section, never the gate's."""
    return [run for run in runs or [] if is_gitlab(run)]
