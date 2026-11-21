# fleet-audit persona

The persona of the `fleet-audit` AgentProfile (#5623, #5620): the parent of the install's fleet-wide scheduled audits and of onboarding's first inventory. It reads no cluster itself. It delegates one `cluster` task per cluster, collects their results, and composes the audit's report with the `fleet-audit` skill's scripts.

It's published as the `persona-fleet-audit` image (`a2a/Dockerfile.persona-fleet-audit`): this directory under `/files`, on the pinned busybox base, as `persona-cluster` and `persona-tse` are.

## The skill

`skills/fleet-audit/` is the persona's own `SKILL.md` and `references/findings-document.md`, a copy of the platform skill's with its one path and three links to references the persona doesn't carry rewritten. `a2a/lib/persona/claudecode/fleet_audit_persona_test.go` holds the copy to its source with only those edits.

The report scripts aren't in the persona: `audit_report.py` alone is over the persona loader's 1 MiB file limit. The worker image copies them from their sources at build time into `/opt/fleet-audit/scripts` (`a2a/Dockerfile.worker`), flat, with the modules they import beside them, and the build fails if one no longer imports from there. `tests/test_worker_fleet_audit_scripts.py` holds the list to its sources.

## What isn't here yet

- The `--store-only` finish a shadow run writes its report with (#5623's onboarding PR). Until then a shadow finishes with `--dry-run`, which writes nothing.
- `spec.delegates: true` on the profile, which delegation from a profile task will need (#6789). Whichever of #6789 and the onboarding PR lands second sets it.
- The report store switch and scratch directory on profile workers (#6110, and the onboarding PR).
