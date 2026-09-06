# Reporting a Problem with kube-agents

Shared reference for the Platform and Cluster agents. Read it when a user asks how to report a bug,
a feature request, or a question **about kube-agents (Kage) itself** — the harness, this agent, the
operator, the docs — as opposed to a problem with their own cluster or workloads.

Two paths. Give the second one whenever the user has not said they can open an issue, because the
audience that asks this question most often cannot: an enterprise-managed GitHub account cannot open
issues, comment, or fork on a repository outside its own enterprise, and GitHub reports that as a
restriction on the target repository rather than on the account.

- **GitHub issue tracker:** <https://github.com/gke-labs/kube-agents/issues> — for anyone whose
  GitHub account can open one.
- **Public feedback form:** <https://gke-labs.github.io/kube-agents/feedback> — needs no GitHub or
  Google account. Always give this short link, never the underlying Google Forms URL; the form can
  be recreated behind the same link.

What a submission needs, either way:

- a one-line summary, which becomes the issue title;
- what happened;
- what was expected instead;
- the kube-agents version in use.

A form submission becomes a public issue on `gke-labs/kube-agents`, opened by the `kube-agents-bot`
account and labelled `external-feedback`. Tell the user that before they submit: everything they
type is public the moment it is filed, apart from the optional follow-up email, which stays in the
form's own response store.

Because it is public, do not put cluster names, project ids, resource names, log excerpts, or any
value read from a Secret into a report or a draft of one. The exception is text the user typed
themselves and asked you to include; even then, say what it will expose. You do not submit the form
on the user's behalf — hand them the link and what to include, and let them file it.
