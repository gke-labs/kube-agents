# Branch protection / ruleset (review gate — 06 §7, 07 Phase 0)

Branch protection is a GitHub **repository setting**, not code — enabling it is a customer/admin step
when this GitOps repo is forked. This file documents the required ruleset so the review half of Phase 0
acceptance (A3) is satisfied end to end.

## Required ruleset on the default branch

- **Require a pull request before merging** — no direct pushes.
- **Require review from Code Owners** — so edits to guarded paths pull in the teams in
  [`CODEOWNERS.example`](../CODEOWNERS.example) (a template with `@your-org/*` placeholders; copy it to
  `CODEOWNERS` and substitute real teams when forking this repo — GitHub only activates a file named
  `CODEOWNERS`). Guarded globs: `**/provisioning/**`, `**/agents/**`, `**/namespaces/**`,
  `**/policy/**`, and agent config / `SOUL.md`.
- **Require status checks to pass** — the actuation pipeline's validation and the **security review
  gate** (`review-gate.yml`, added Phase 5): any unmitigated high/critical finding blocks merge.
- **Dismiss stale approvals on new commits**; **require branches up to date**; **no bypass** for the
  guarded paths (admins included).

## Why both this and the ValidatingAdmissionPolicy

Defense in depth: human review + CI here catch a bad change **before** merge; the
`policy/vap-agent-readonly.yaml` `ValidatingAdmissionPolicy` rejects it **at apply time even if merged
anyway** (03 §11). Phase 0 acceptance A3 requires both halves.

## Enabling (reference)

```bash
gh api -X PUT repos/<owner>/<repo>/rulesets ...   # or configure in Settings → Rules → Rulesets
```
