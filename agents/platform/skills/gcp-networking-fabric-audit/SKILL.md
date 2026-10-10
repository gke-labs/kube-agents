---
name: gcp-networking-fabric-audit
description: Audits VPC subnet IPAM capacity, Cloud NAT port exhaustion, Private Service Connect routing, VPC peering MTU mismatches, Cloud Armor WAF policies, and management ports open to the whole internet.
---

# Task

Audit Google Cloud VPC subnet IPAM allocation headroom, Cloud NAT port capacity, Private Service Connect (PSC) reachability, VPC peering MTU mismatches, Cloud Armor WAF policies and world-open management ports, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Open the Run, Then Run the Collector

Open the run first with the `fleet-audit` harness's `start` (SOP §0): `finish` refuses a manifest that finished before `start` did, as an earlier run's. Then run the collector, which resolves the SOP §1 project scope itself and writes the run manifest:

```bash
python3 ./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --output /opt/data/scratch/manifest_gcp-networking-fabric-audit.json && python3 ./skills/fleet-audit/scripts/audit_report.py draft --audit gcp-networking-fabric-audit --manifest-file /opt/data/scratch/manifest_gcp-networking-fabric-audit.json --out /opt/data/scratch/findings_gcp-networking-fabric-audit.json
```

`draft` writes the findings document (`findings_gcp-networking-fabric-audit.json`) from the manifest. Write each `recommendation` and `remediation` in it. The manifest is not a findings document.

## 2. Evaluate Findings Against SOP Checks

Read the manifest and follow `governance/gcp_networking_fabric_sop.md` §2, which owns the copy rules for `commands`, `checks_not_applicable`, `limitations` and `candidates`. All six roster checks are collector-verified; none is yours to hand-run unless a target is `gate-failed` or its `limitations` names a rule as undecided. A check whose read failed is in the target's `checks_unevaluated`, and its `limitations` names that check. Run the collector as the SOP does, without `--check`: that flag narrows a run to the subnet sweep or the project checks, and the manifest it writes leaves the other checks out.

- `subnet-ip-exhaustion`, on each `<project>/<region>/<subnet>` target: Pod ranges from GKE's own utilization fields, primary ranges as a lower bound from VM NICs, internal addresses and forwarding rules.
- `cloud-nat-exhaustion`, `psc-routing-deadlock`, `mtu-packet-fragmentation`, `cloud-armor-false-positive` and `firewall-world-open-ingress`, on each `project/<project>` target.

A manifest with a top-level `error` read no target: do not call `finish`, and report the error.

## 3. Hand Findings to Fleet Audit

Finish the run you opened in step 1 with the `fleet-audit` harness's `finish`, passing `--manifest-file` as the SOP's §5 directs. This stream's `finish` requires that flag, or `--no-collector-manifest` with a reason.
