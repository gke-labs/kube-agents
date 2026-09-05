# SOP: GCP Networking Fabric & VPC IPAM Audit (Daily Governance)

**Purpose:** Sweep all managed VPC networks, subnets, Cloud NAT gateways, Private Service Connect (PSC) endpoints, and Cloud Armor security policies across target GCP projects for subnet IP exhaustion, NAT port allocation saturation, PSC routing deadlocks, MTU fragmentation mismatches, and Cloud Armor policy anomalies. The question this audit answers for a platform admin is: _which subnets are running out of secondary IP ranges for GKE Pods, where are Cloud NAT gateways dropping connections due to port exhaustion, and which VPCs have MTU mismatches causing packet fragmentation?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gcp-networking-fabric-audit`, schedule `0 8 * * *` (daily 08:00 UTC).

**Data sources:** `gcloud compute networks ...`, `gcloud compute routers ...`, `gcloud compute forwarding-rules ...`, and `gcloud compute security-policies ...` across all managed fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gcp-networking-fabric-audit [--repo "<owner>/<repo>"]
```

If multiple repositories are registered in `$GITOPS_STATE_CONFIGMAP` (`managed_repos`), pass `--repo "<owner>/<repo>"` explicitly:

- **Interactive session:** If no `--repo` was specified, prompt the user to choose which repository to target before proceeding.
- **Scheduled / unattended cron:** Iterate over all repositories in `managed_repos` in sequence, executing the audit and running `audit_report.py start` and `audit_report.py finish` for each repository with `--repo "<owner>/<repo>"`.

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gcp-networking-fabric-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gcp-networking-fabric-audit.json", "pending_remediation_requests": [<finding_id>, ...]}`.

If `pending_remediation_requests` is non-empty, inspect each requested finding in the open issue and write the updated manifest or Terraform file to `workspace` at `remediation.path` before proceeding to step 3 (`finish`).

### 1. Enumerate the target fleet

```bash
gcloud compute networks subnets list --format=json
```

- Target every VPC subnet across fleet projects. Record `{name, location, project, checks_run}` into `scope.clusters`, formatting `name` as unique `<project>/<region>/<subnet>` (or project-scoped target `project/<project-id>`).
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

#### 2.1 Subnet primary and secondary IP range exhaustion (`subnet-ip-exhaustion`)

- **Severity**: `critical`
- **Command**: `gcloud compute networks subnets list-usable --project=$PROJECT --format=json`
- **Condition**: Subnet primary or secondary Pod IP range has < 15% available IP address capacity remaining.
- **Remediation**: Expand subnet CIDR or allocate additional secondary IP range in Terraform VPC definition.

#### 2.2 Cloud NAT gateway port allocation saturation (`cloud-nat-exhaustion`)

- **Severity**: `critical`
- **Discovery**: `gcloud compute routers list --project=$PROJECT --format=json` — binds `$ROUTER` and `$REGION`; run the command below once per router whose `nats` list is non-empty. A router with no `nats` entry is a BGP router, not a NAT gateway: skip it rather than reading its empty mapping as a gateway without IPs. A project with no NAT router has nothing to inspect: record the check in `checks_run` with the discovery command, not in `limitations`.
- **Command**: `gcloud compute routers get-nat-mapping-info $ROUTER --region=$REGION --project=$PROJECT --format=json`
- **Condition**: Cloud NAT mapping indicates allocated ports exceed 80% available port capacity per VM or gateway lacks auto-allocated IP addresses.
- **Remediation**: Increase `minPortsPerVm` or add additional NAT IP addresses in Cloud Router specification.

#### 2.3 Private Service Connect endpoint routing deadlock (`psc-routing-deadlock`)

- **Severity**: `major`
- **Command**: `gcloud compute forwarding-rules list --filter="target:ServiceAttachment" --project=$PROJECT --format=json`
- **Condition**: PSC forwarding rule points to rejected or inactive target service attachment.
- **Do NOT flag**: Active PSC forwarding rules in ACCEPTED status.
- **Remediation**: Repair target service attachment reference or update forwarding rule routing in Terraform.

#### 2.4 VPC network MTU packet fragmentation mismatch (`mtu-packet-fragmentation`)

- **Severity**: `major`
- **Command**: `gcloud compute networks list --project=$PROJECT --format=json`
- **Condition**: VPC network MTU is configured below 1500 (e.g. 1460) while jumbo frame processing is enabled or workloads require 1500 MTU.
- **Do NOT flag**: Standard VPC networks operating with default 1460 MTU where workloads do not exchange jumbo frames.
- **Remediation**: Configure VPC MTU to 1500 or adjust workload MSS clamp in network configuration.

#### 2.5 Cloud Armor security policy evaluation anomalies (`cloud-armor-false-positive`)

- **Severity**: `minor`
- **Command**: `gcloud compute security-policies list --project=$PROJECT --format=json`
- **Condition**: Production backend service security policy is in preview mode or contains conflicting rule priorities.
- **Do NOT flag**: Non-production test environments deliberately validating staging rules in preview mode.
- **Remediation**: Enforce validated Cloud Armor security rules and remove conflicting rule definitions.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/vpc/subnets.tf`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gcp-networking-fabric-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gcp-networking-fabric-audit",
  "scope": {
    "clusters": [
      {
        "name": "proj-1/us-central1/gke-pods-subnet",
        "location": "us-central1",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "subnet-ip-exhaustion",
            "command": "gcloud compute networks subnets list-usable --project=proj-1 --format=json"
          }
        ],
        "checks_not_applicable": [
          {
            "check": "cloud-nat-exhaustion",
            "reason": "NAT gateways are configured at the Cloud Router level, not per subnet."
          },
          {
            "check": "psc-routing-deadlock",
            "reason": "Private Service Connect endpoints are project-level resources, not subnet resources."
          },
          {
            "check": "mtu-packet-fragmentation",
            "reason": "VPC network MTU is defined at the VPC level, not per subnet."
          },
          {
            "check": "cloud-armor-false-positive",
            "reason": "Cloud Armor security policies are backend service resources, not subnet resources."
          }
        ]
      },
      {
        "name": "project/proj-1",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "cloud-nat-exhaustion",
            "command": "gcloud compute routers get-nat-mapping-info ROUTER --region=us-central1 --project=proj-1 --format=json"
          },
          {
            "check": "psc-routing-deadlock",
            "command": "gcloud compute forwarding-rules list --filter=\"target:ServiceAttachment\" --project=proj-1 --format=json"
          },
          {
            "check": "mtu-packet-fragmentation",
            "command": "gcloud compute networks list --project=proj-1 --format=json"
          },
          {
            "check": "cloud-armor-false-positive",
            "command": "gcloud compute security-policies list --project=proj-1 --format=json"
          }
        ],
        "checks_not_applicable": [
          {
            "check": "subnet-ip-exhaustion",
            "reason": "Subnet IP capacity is audited per individual subnet scope entry."
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "subnet-ip-exhaustion",
      "severity": "critical",
      "title": "Subnet gke-pods-subnet has < 10% secondary IP addresses remaining",
      "cluster": "proj-1/us-central1/gke-pods-subnet",
      "namespace": "default",
      "object": "Subnet/gke-pods-subnet",
      "impact": "Pod scheduling will fail when secondary IP allocation is exhausted.",
      "evidence": {
        "command": "gcloud compute networks subnets describe gke-pods-subnet --region=us-central1 --project=proj-1 --format=json",
        "excerpt": "ipCidrRange: 10.0.0.0/20"
      },
      "recommendation": {
        "action": "Add an additional secondary IP range to gke-pods-subnet in Terraform.",
        "rationale": "Prevents pod provisioning stockouts during horizontal scaling.",
        "risk": "Requires cluster pod CIDR expansion."
      },
      "remediation": {
        "kind": "manifest",
        "path": "terraform/modules/vpc/subnets.tf"
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gcp-networking-fabric-audit \
  --findings-file /opt/data/scratch/findings_gcp-networking-fabric-audit.json \
  [--repo "<owner>/<repo>"]
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never delete VPC subnets, modify live firewall rules, or tear down NAT gateways directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, and authorization headers must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`Subnet/<name>`, `Router/<name>`), never an ephemeral execution timestamp.
- **Never emit a manifest that directly deletes a network or subnet.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
