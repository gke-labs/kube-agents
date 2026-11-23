# fleet-audit persona

The persona of the `fleet-audit` AgentProfile: the parent of the install's fleet-wide scheduled audits and of onboarding's first inventory. It reads no cluster itself. It delegates one `cluster` task per cluster, collects their results, and composes the audit's report with the `fleet-audit` skill's scripts. [`instructions.md`](instructions.md) is that procedure.

It's published as the `persona-fleet-audit` image (`a2a/Dockerfile.persona-fleet-audit`): this directory under `/files`, on the pinned busybox base, as `persona-cluster` and `persona-tse` are.

## The skill

`skills/fleet-audit/` is the persona's own [`SKILL.md`](skills/fleet-audit/SKILL.md) and [`references/findings-document.md`](skills/fleet-audit/references/findings-document.md), a copy of the platform skill's with its one path and three links to references the persona doesn't carry rewritten. `a2a/lib/persona/claudecode/fleet_audit_persona_test.go` holds the copy to its source with only those edits.

The report scripts aren't in the persona: `audit_report.py` alone is over the persona loader's 1 MiB file limit. The worker image copies them from their sources at build time into `/opt/kube-agents/scripts/fleet-audit` (`a2a/Dockerfile.worker`), flat, with the modules they import beside them, and the build fails if one no longer imports from there. So is the compliance audit's collector (`collect.py` and its closure), which a fan-out child runs for its one cluster, with the fleet-upgrade-verification readiness modules its webhook check imports at `/opt/kube-agents/fleet-upgrade-verification/scripts`. `tests/test_worker_fleet_audit_scripts.py` holds both copies to their sources.

## What a run needs that isn't here

- A finish that writes the shadow run's own report and nothing else. Until it exists, a shadow run finishes with `--dry-run`, which writes nothing.
- The profile's opt-in to delegation, which delegation from a profile task requires.
- The report store switch and a writable scratch directory on profile workers.
- A live finish, and `start`'s run record. Without `start`, `finish` treats every repository as unsearched for declared intent and says so as a coverage gap.
