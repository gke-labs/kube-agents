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

Optional helper runner:

```bash
./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --output /opt/data/scratch/networking_raw.json
```

## 2. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
