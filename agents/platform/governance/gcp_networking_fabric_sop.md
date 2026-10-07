# SOP: GCP Networking Fabric & VPC IPAM Audit (Daily Governance)

**Purpose:** Sweep all managed VPC networks, subnets, Cloud NAT gateways, Private Service Connect (PSC) endpoints, and Cloud Armor security policies across target GCP projects for subnet IP exhaustion, NAT port allocation saturation, PSC routing deadlocks, MTU fragmentation mismatches, and Cloud Armor policy anomalies. The question this audit answers for a platform admin is: _which subnets are running out of secondary IP ranges for GKE Pods, where are Cloud NAT gateways dropping connections due to port exhaustion, and which VPCs have MTU mismatches causing packet fragmentation?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gcp-networking-fabric-audit`, schedule `0 8 * * *` (daily 08:00 UTC).

**Data sources:** `gcloud compute networks ...`, `gcloud compute routers ...`, `gcloud compute forwarding-rules ...`, and `gcloud compute security-policies ...`, run once per project in the resolved project scope (§1). Check 2.1 runs through `networking_audit.py`, which also reads `gcloud container clusters list`, `gcloud compute instances list` and `gcloud compute addresses list`.

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

**Resolve the project scope first.** The scope is the host project (`gcloud config get-value project`) plus every project `gcloud projects list --format="value(projectId)"` returns. Run every collection command once per project, passing `--project` explicitly — the ambient default silently audits one project and reports the result as a fleet sweep. The scope is what the agent's identity can read, so an operator narrows it by narrowing the IAM grant. A listing that exits non-zero, or that returns without the host project, cannot say how many other projects exist: sweep the projects you have and add one `scope.skipped` entry, `{"cluster": "project/UNENUMERATED_PROJECTS", "reason": "<the listing's rc and stderr excerpt, or the host project it omitted>"}`, so the run publishes as partial rather than as the whole fleet. A run narrowed on request — someone asks for one project, or a helper script is given `--project-id` or `MONITORED_PROJECT_IDS` — records the same entry with the reason `scope narrowed to <projects> on request`: it read no other project, and without the entry `finish` resolves every ledger finding on a project the run never looked at. A project where the API this audit reads is disabled (`SERVICE_DISABLED`, `accessNotConfigured`, `has not been used in project`) holds nothing to audit and counts as empty, not skipped: recording it as a loss would pin every run partial for as long as the project exists. That holds only when the refusal names this project — its id, or the number `gcloud projects describe <project> --format='value(projectNumber)'` prints. A refusal naming another project, such as the credential's quota project, says nothing about this one: record it in `scope.skipped` as a failed read.

```bash
HOST=$(gcloud config get-value project)
LISTED=$(gcloud projects list --format="value(projectId)")
PROJECTS=$(printf '%s\n' "$HOST" $LISTED | sort -u)
for PROJECT in $PROJECTS; do
  gcloud compute networks subnets list --project="$PROJECT" --format=json
done
```

- Target every VPC subnet across the resolved projects. Record `{name, location, project, checks_run}` into `scope.clusters`, formatting `name` as unique `<project>/<region>/<subnet>` (or project-scoped target `project/<project-id>`). Check 2.1's helper writes the subnet entries for every subnet it can count, and lists none for the proxy-only, PSC and NAT subnets it skips (2.1); you write the `project/<id>` ones.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string, **and the sweep continues** — one project's permission error never decides the outcome for the rest of the fleet. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

Run every check once per project in §1's scope, with `$PROJECT` set to that project for the whole pass, except 2.1: its helper sweeps the whole scope itself in one run. After §1's loop the variable holds the last project listed, so a check run outside a per-project pass reads that one project and reports it as the fleet.

#### 2.1 Subnet primary and secondary IP range exhaustion (`subnet-ip-exhaustion`)

- **Severity**: `critical`
- **Command**: `python3 ./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --check subnet-ip-exhaustion --output /opt/data/scratch/networking_subnets.json` — run it once, not inside a per-project pass: it resolves §1's project scope itself and sweeps every project in it. `gcloud compute networks subnets list-usable` returns ranges but no usage, so it cannot answer this check.
- **What it measures**: Pod (secondary) ranges from GKE's own utilization fields in `gcloud container clusters list` (`defaultPodIpv4RangeUtilization`, each node pool's `podIpv4RangeUtilization`, `additionalPodRangesConfig`), which count allocated per-node blocks, the unit that runs out. Primary ranges as a lower bound: the unique internal IPs held by VM NICs, reserved internal addresses and forwarding rules, plus the 4 addresses GCP reserves in every range; serverless connectors and Google-managed endpoints are not counted. It skips proxy-only, PSC and NAT subnets (a `purpose` other than `PRIVATE` or `PRIVATE_RFC_1918`): Google manages their allocations and no read can count them, so they appear nowhere in the document — not measured, not as a gap. A Shared VPC host subnet is measured against the clusters and VMs of every project in scope, not only the host's, and a Pod range whose host subnet could not be listed is still reported, as an entry whose `limitations` says so.
- **Condition**: Subnet primary or secondary Pod IP range has < 15% available IP address capacity remaining.
- **Merge**: Copy the helper's `<project>/<region>/<subnet>` entries from `scope.clusters`, its `subnet-ip-exhaustion` findings and its `scope.skipped` entries (`<project>/UNENUMERATED_SUBNETS` for a project whose subnets could not be listed, `<project>/UNREAD_SUBNET_USAGE` for a project whose usage reads failed but which owns no subnet entry to name them on) into findings.json unchanged, apart from §3's remediation promotion. Keep each subnet entry's `limitations` — it names the read of that subnet's own project that failed, such as Pod ranges not read. A `project/UNENUMERATED_PROJECTS` row your document already carries is not added twice. Do not put `subnet-ip-exhaustion` on a `project/<id>` entry's `checks_run`; it stays in that entry's `checks_not_applicable`, as §4's example shows. If the helper exits non-zero or writes no file, the check did not run: leave it out of every `checks_run`, and move it from each `project/<id>` entry's `checks_not_applicable` to that entry's `limitations` with the error, so the run publishes as partial. Never record it as run.
- **Remediation**: Expand subnet CIDR or allocate additional secondary IP range in Terraform VPC definition. The helper writes `kind: manual`; promote a finding to `kind: manifest` per §3 only once you have found the Terraform file that defines the subnet.

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
            "command": "gcloud compute networks subnets list --project=proj-1 '--format=json(name,region,ipCidrRange,secondaryIpRanges,purpose,selfLink)' && gcloud container clusters list --project=proj-1 '--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools)' && gcloud compute instances list --project=proj-1 '--format=json(networkInterfaces[].networkIP,networkInterfaces[].subnetwork)' && gcloud compute addresses list --project=proj-1 --filter=addressType=INTERNAL '--format=json(address,subnetwork)' && gcloud compute forwarding-rules list --project=proj-1 '--format=json(IPAddress,subnetwork)'"
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
      "title": "Pod range gke-pods of subnet gke-pods-subnet in us-central1 has 6% available",
      "cluster": "proj-1/us-central1/gke-pods-subnet",
      "namespace": "",
      "object": "SecondaryRange/gke-pods",
      "impact": "GKE cannot add nodes that take their Pod block from gke-pods once it is fully allocated, so autoscaling and surge upgrades on those node pools fail.",
      "evidence": {
        "command": "gcloud container clusters list --project=proj-1 '--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools)'",
        "excerpt": "Pod range gke-pods (10.4.0.0/18): GKE reports 93.8% allocated (cluster prod-1, node pool default-pool); about 4 more /24 node blocks fit"
      },
      "recommendation": {
        "action": "Add an additional Pod range to cluster prod-1 (additionalPodRangesConfig), or lower maxPodsPerNode on new node pools so each node takes a smaller block.",
        "rationale": "GKE allocates one fixed block of the Pod range per node, whatever the node runs.",
        "risk": "An additional range needs unused VPC space that overlaps no other range; maxPodsPerNode applies only to new node pools, so existing pools must be recreated to benefit."
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
# -> {"status":"CLEAN"|"HELD"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
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
