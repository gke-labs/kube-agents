---
title: Contributing
description: How to contribute to kube-agents, and where to file issues.
---

## Before you begin

### Sign the Contributor License Agreement

Contributions must be accompanied by a [Contributor License Agreement](https://cla.developers.google.com/about) (CLA). You (or your employer) retain copyright to your contribution; the CLA gives us permission to use and redistribute it as part of the project.

If you or your current employer have already signed the Google CLA (even for a different project), you probably don't need to do it again. Check at <https://cla.developers.google.com/>.

### Community guidelines

This project follows [Google's Open Source Community Guidelines](https://opensource.google/conduct/).

## How to contribute

The contributor workflow lives in the repository, next to the code it governs. [`CONTRIBUTING.md`](https://github.com/gke-labs/kube-agents/blob/main/CONTRIBUTING.md) is the entry point; [`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) states the rules every change follows, and [`docs/pull-request-workflow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/pull-request-workflow.md) has the commands. Every pull request is reviewed by an automated reviewer and then by a maintainer; Prow merges it once a reviewer's `lgtm` and an `OWNERS` approver's `approved` are both on it and the required checks are green.

## Where to file issues

Bug reports, feature requests, and questions: [github.com/gke-labs/kube-agents/issues](https://github.com/gke-labs/kube-agents/issues).

If your GitHub account cannot open an issue here, use the [feedback form](/kube-agents/feedback/) instead. It needs no account and files a public issue on your behalf, labelled `external-feedback`. The usual reason is an enterprise-managed GitHub account, which cannot interact with any repository outside its own enterprise; GitHub reports that as a restriction on this repository, but the repository itself is open. How the form works is in [`scripts/feedback_form/README.md`](https://github.com/gke-labs/kube-agents/blob/main/scripts/feedback_form/README.md).

The [`github-repo-watcher` poller](/kube-agents/concepts/autonomous-watchdogs/#pollers-file-cards-watchdogs-deliver-reports) checks open issues every 10 minutes, and the agent may (within tight guardrails) triage or respond to one automatically. Human review still gates any resolution.
