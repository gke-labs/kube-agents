# How to contribute

We'd love to accept your patches and contributions to this project.

## Sign our Contributor License Agreement

Contributions to this project must be accompanied by a
[Contributor License Agreement](https://cla.developers.google.com/about) (CLA). You (or your
employer) retain the copyright to your contribution; this simply gives us permission to use and
redistribute your contributions as part of the project.

If you or your current employer have already signed the Google CLA (even if it was for a different
project), you probably don't need to do it again. Visit <https://cla.developers.google.com/> to see
your current agreements or to sign a new one.

This project follows
[Google's Open Source Community Guidelines](https://opensource.google/conduct/).

## Code review

All submissions, including from project members, require review through GitHub pull requests.
Nobody merges by hand: Prow squash-merges a pull request once a reviewer's `lgtm` and an `OWNERS`
approver's `approved` are both on it and the required checks are green.
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#how-a-change-merges) has the
mechanics, including what a pull request is waiting for when it sits unmerged.

## Where the rules live

The contributor workflow is written once, next to the code it governs, and the same rules bind
human contributors and AI coding agents:

- [`AGENTS.md`](AGENTS.md) states the rules: branch from a freshly fetched `main`, check whether
  someone is already doing the work, Conventional Commits, the pull request template, the two
  pre-PR review passes, live validation against a real install, and the automated review every
  pull request receives.
- [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md) has the commands behind them:
  the duplicate-work scan, the branch-drift check, the local validation checks and the constraint
  each one exists for, how to poll for and answer the `kube-agents-bot` review, and how to
  resolve its threads.
- [`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md) is the mechanics of the
  self-review, live-validation, and bug-fix recurrence sections the pull request template asks
  for.
- [`docs/testing-map.md`](docs/testing-map.md) says where a new test goes and what runs it.
- [`docs/designs/live-test-lease.md`](docs/designs/live-test-lease.md) is the lease to take before
  mutating an installation your team shares.

## Where to file issues

Bug reports, feature requests, and questions:
<https://github.com/gke-labs/kube-agents/issues>. If your GitHub account cannot open an issue
here, the [feedback form](https://gke-labs.github.io/kube-agents/feedback/) files one on your
behalf; the site's [contributing page](https://gke-labs.github.io/kube-agents/contributing/)
explains when that happens.
