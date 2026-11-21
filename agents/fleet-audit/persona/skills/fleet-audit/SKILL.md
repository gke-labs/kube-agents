---
name: fleet-audit
description: Compose a fleet-wide audit's findings document from the per-cluster results your delegated cluster tasks returned, and finish the run with audit_report.py.
---

# fleet-audit, as a profile task

This skill turns what your `cluster` children found into the audit's report. You never inspect a cluster yourself: each child read its own cluster, and their results are your only input.

## The findings document

Write `findings.json` exactly as [`references/findings-document.md`](references/findings-document.md) describes. Each finding names the cluster it came from, as the child that found it named it. A cluster no child covered is a gap, not a clean cluster: list it as not inspected, with the reason.

## Finishing

The scripts are in the worker image, under `/opt/fleet-audit/scripts`:

```
python3 /opt/fleet-audit/scripts/audit_report.py finish --audit <audit id> --findings-file findings.json --no-collector-manifest "<why>" --dry-run
```

- `--audit` is the stream the task's request names.
- An audit that takes a collector manifest needs `--manifest-file`, or `--no-collector-manifest` with a reason. The script says which.
- `--dry-run` writes nothing and opens nothing, and every shadow run (the task parameter `dryRun=true`) uses it. A live run's finish is the scheduled job's own, and isn't described here yet.

Never run `remediate` in a shadow run, and never open an issue or a pull request by any other means.
