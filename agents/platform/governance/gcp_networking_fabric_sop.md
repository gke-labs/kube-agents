# SOP: GCP Networking Fabric & VPC IPAM Audit (Daily Governance)

**Purpose:** Sweep all monitored GCP projects and GKE clusters for VPC IP address exhaustion, Cloud NAT ephemeral port saturation, Private Service Connect (PSC) routing blackholes, and Cloud Armor WAF false positives. The question this audit answers for a platform admin is: _which VPC subnets will block node pool autoscaling, which services are dropping outbound API calls due to NAT limits, and where are cross-project PSC routes dropping traffic?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform fixes for the findings that get promoted.

**Cron:** id `gcp-networking-fabric-audit`, schedule `0 8 * * *` (daily 08:00 UTC).

**Data sources:** Google Cloud Router API (`gcloud compute routers`), VPC subnet IPAM metrics (`gcloud compute networks subnets`), Cloud Armor security logs, and PSC endpoint descriptors.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gcp-networking-fabric-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"...", "findings_path":"/opt/data/scratch/findings_gcp-networking-fabric-audit.json"}`.

### 1. Enumerate the target fleet

```bash
gcloud compute networks subnets list --format=json
```

Record all subnets and clusters into `scope.clusters` with mandatory `checks_run` objects.

### 2. Execute networking fabric inspection

```bash
./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --output /opt/data/scratch/networking_raw.json
```

---

## Section 3: Diagnostic Checks Roster

#### `subnet-ip-exhaustion`

- **Severity**: `critical`
- **Command**: `gcloud compute networks subnets describe $SUBNET --region=$REGION --format=json`
- **Condition**: VPC secondary Pod/Service CIDR range utilization exceeds 85%, threatening GKE node pool scale-up.
- **Remediation**: Provision expanded secondary CIDR ranges in Terraform `google_compute_subnetwork`.

#### `cloud-nat-exhaustion`

- **Severity**: `critical`
- **Command**: `gcloud compute routers nats describe $NAT --router=$ROUTER --region=$REGION --format=json`
- **Condition**: Cloud NAT gateway allocated ephemeral port utilization exceeds 80% (`allocated_ports / total_capacity`).
- **Remediation**: Scale allocated NAT gateway IP addresses or adjust minimum ports per VM in Terraform.

#### `psc-routing-deadlock`

- **Severity**: `major`
- **Command**: `gcloud compute forwarding-rules list --filter="target:serviceAttachments" --format=json`
- **Condition**: Private Service Connect (PSC) endpoint configured but blocked by VPC ingress/egress firewall rules.
- **Remediation**: Generate Terraform `google_compute_firewall` rule permitting TCP traffic to PSC forwarding target.

#### `mtu-packet-fragmentation`

- **Severity**: `major`
- **Command**: `gcloud compute networks describe $VPC --format=json`
- **Condition**: MTU mismatch between VPC network (1460) and container network interfaces (1500) causing silent packet drops on large payloads.
- **Remediation**: Inject TCP MSS clamping or align MTU settings in network DaemonSets.

#### `cloud-armor-false-positive`

- **Severity**: `minor`
- **Command**: `gcloud compute security-policies list --format=json`
- **Condition**: Cloud Armor security policy rate-limiting rules dropping legitimate internal microservice traffic.
- **Remediation**: Tune Cloud Armor rule expression thresholds in Terraform.

---

### 4. Emit findings.json

Write the schema exactly as validated by the helper to `findings_path`:

```json
{
  "audit": "gcp-networking-fabric-audit",
  "scope": {
    "clusters": [
      {
        "name": "vpc-fabric-global",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "subnet-ip-exhaustion",
            "command": "gcloud compute networks subnets describe subnet-1 --region=us-central1"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": []
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gcp-networking-fabric-audit \
  --findings-file /opt/data/scratch/findings_gcp-networking-fabric-audit.json
```

---

## Red Lines

- **Never modify live VPC routes, subnets, or firewalls during this audit.**
- **Never emit a manifest that directly deletes a subnet, router, or PSC attachment.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
- **Never expose internal CIDR topology or secret network configurations in issue bodies.**
