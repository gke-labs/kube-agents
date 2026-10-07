---
name: gcp-networking-fabric-audit
description: Audits VPC subnet IPAM capacity, Cloud NAT ephemeral port exhaustion, Private Service Connect routing, and Cloud Armor WAF policies.
---

# Task

Audit Google Cloud VPC subnet IPAM allocation headroom, Cloud NAT ephemeral port capacity, Private Service Connect (PSC) reachability, and Cloud Armor WAF policies, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Networking Inspection

Follow the authoritative SOP at `governance/gcp_networking_fabric_sop.md` to execute the five diagnostic checks across target GCP projects:

- `subnet-ip-exhaustion`
- `cloud-nat-exhaustion`
- `psc-routing-deadlock`
- `mtu-packet-fragmentation`
- `cloud-armor-false-positive`

Helper runner (`networking_audit.py`): resolves the SOP §1 project scope itself and runs two sweeps, chosen with `--check {all,psc-routing-deadlock,subnet-ip-exhaustion}` (default `all`):

- `subnet-ip-exhaustion`: required for SOP check 2.1. Measures Pod ranges from GKE's utilization fields and primary ranges as a lower bound from VM NICs, internal addresses and forwarding rules; skips proxy-only, PSC and NAT subnets. Writes `<project>/<region>/<subnet>` scope entries, findings, and `<project>/UNENUMERATED_SUBNETS` skipped entries; merge them as SOP 2.1 says.
- `psc-routing-deadlock`: optional. Flags PSC forwarding rules in `REJECTED` or `CLOSED` state on `project/<id>` entries.

```bash
python3 ./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --check subnet-ip-exhaustion --output /opt/data/scratch/networking_subnets.json
```

## 2. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
