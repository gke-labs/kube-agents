---
name: gcp-networking-fabric-audit
description: Audits VPC subnet IPAM capacity, Cloud NAT ephemeral port exhaustion, Private Service Connect routing, and Cloud Armor WAF policies.
---

# Task

Audit Google Cloud VPC subnet IPAM allocation headroom, Cloud NAT ephemeral port capacity, Private Service Connect (PSC) firewall reachability, and Cloud Armor WAF policies, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Networking Inspection

Run the profile-relative networking runner to sweep target projects:

```bash
./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --output /opt/data/scratch/networking_raw.json
```

## 2. Evaluate Findings Against SOP Checks

Filter and categorize collected metrics according to `governance/gcp_networking_fabric_sop.md`:

- `subnet-ip-exhaustion`: Flag VPC secondary Pod/Service CIDR ranges with > 85% allocation.
- `cloud-nat-exhaustion`: Flag Cloud NAT gateways with > 80% ephemeral port utilization.
- `psc-routing-deadlock`: Flag PSC endpoints blocked by missing firewall rules.
- `mtu-packet-fragmentation`: Flag MTU mismatches between VPC (1460) and container interfaces.
- `cloud-armor-false-positive`: Flag Cloud Armor rate-limiting rules dropping legitimate internal traffic.

## 3. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
