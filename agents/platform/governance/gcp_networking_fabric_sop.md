# SOP: GCP Networking Fabric & VPC IPAM Audit (Daily Governance)

**Purpose:** Sweep all managed VPC networks, subnets, Cloud NAT gateways, Private Service Connect (PSC) endpoints, Cloud Armor security policies and firewall rules across target GCP projects for subnet IP exhaustion, NAT port allocation saturation, PSC routing deadlocks, MTU fragmentation mismatches, Cloud Armor policy anomalies, and management ports open to the whole internet. The question this audit answers for a platform admin is: _which subnets are running out of secondary IP ranges for GKE Pods, where are Cloud NAT gateways dropping connections due to port exhaustion, and which VPCs have MTU mismatches causing packet fragmentation?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gcp-networking-fabric-audit`, schedule `0 8 * * *` (daily 08:00 UTC).

**Data sources:** `gcloud compute networks ...`, `gcloud compute routers ...`, `gcloud compute forwarding-rules ...`, `gcloud compute security-policies ...`, `gcloud compute backend-services list`, `gcloud compute firewall-rules list`, `gcloud compute instances list`, `gcloud compute addresses list` and `gcloud container clusters list`, run once per project in the resolved project scope (§1). The collector, `networking_audit.py` (§2), runs all of them and writes the run manifest.

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

**Resolve the project scope first.** The scope is the host project (`gcloud config get-value project`) plus every project `gcloud projects list --format="value(projectId)"` returns. Run every collection command once per project, passing `--project` explicitly — the ambient default silently audits one project and reports the result as a fleet sweep. The scope is what the agent's identity can read, so an operator narrows it by narrowing the IAM grant. A listing that exits non-zero, or that returns without the host project, cannot say how many other projects exist: sweep the projects you have and add one `scope.skipped` entry, `{"cluster": "project/UNENUMERATED_PROJECTS", "reason": "<the listing's rc and stderr excerpt, or the host project it omitted>"}`, so the run publishes as partial rather than as the whole fleet. A run narrowed on request — someone asks for one project, or the collector is given `--project-id` or `MONITORED_PROJECT_IDS` — records the same entry with the reason `scope narrowed to <projects> on request`: it read no other project, and without the entry `finish` resolves every ledger finding on a project the run never looked at. A project where the API this audit reads is disabled (`SERVICE_DISABLED`, `accessNotConfigured`, `has not been used in project`) holds nothing to audit and counts as empty, not skipped: recording it as a loss would pin every run partial for as long as the project exists. That holds only when the refusal names this project — its id, or the number `gcloud projects describe <project> --format='value(projectNumber)'` prints. A refusal naming another project, such as the credential's quota project, says nothing about this one: record it in `scope.skipped` as a failed read.

The collector (§2) applies all of this itself: it resolves the scope, leaves out a project whose own Compute Engine API is off, and hands back `project/UNENUMERATED_PROJECTS` as a `gate-failed` target carrying the reason. The rules above are for a target you fall back on by hand.

- A target is either a subnet, named `<project>/<region>/<subnet>`, which owes `subnet-ip-exhaustion` alone, or a project, named `project/<project-id>`, which owes the other five checks. Record `{name, location, project, checks_run}` into `scope.clusters` for each.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string, **and the sweep continues** — one project's permission error never decides the outcome for the rest of the fleet. If a target is partially readable, record the refusal in its `limitations` string.

### 2. Diagnostic checks roster

**Run the collector after §0's `start` and before evaluating any check below by hand.** `finish` refuses a manifest that finished before `start` did, as an earlier run's.

```bash
python3 ./skills/gcp-networking-fabric-audit/scripts/networking_audit.py --output /opt/data/scratch/manifest_gcp-networking-fabric-audit.json && python3 ./skills/fleet-audit/scripts/audit_report.py draft --audit gcp-networking-fabric-audit --manifest-file /opt/data/scratch/manifest_gcp-networking-fabric-audit.json --out /opt/data/scratch/findings_gcp-networking-fabric-audit.json
```

`draft` writes the findings document from the manifest: the scope, `checks_run`, and one finding for each candidate with its evidence. Write each `recommendation` and `remediation` in that file, then give it to `finish` as `--findings-file`. Never give the manifest as `--findings-file`. The collector resolves §1's scope itself and sweeps every project in it once; pass `--project-id <id>` only to scope a run to one project, which records the narrowing as `project/UNENUMERATED_PROJECTS`. Read the manifest before doing anything else:

- Every entry in `manifest.clusters` is one target, carrying one `outcome`. `"collected"` means the collector read that target. A check whose read failed is in the target's `checks_unevaluated`, and its `limitations` names that check. Do not list that check in `checks_run` or `checks_not_applicable`. Do not run the other checks again by hand, except a rule that `limitations` names as undecided (§2.6). `"gate-failed"` means a read the whole target depends on failed: put it in `scope.skipped` with its `error` as the reason. `project/UNENUMERATED_PROJECTS`, `<project>/UNENUMERATED_SUBNETS` (a project whose subnets could not be listed) and `<project>/UNREAD_SUBNET_USAGE` (a project whose usage reads failed but which owns no subnet entry to name them on) are always `gate-failed` and go in `scope.skipped` the same way. You may instead check a `gate-failed` project target by hand with this section's commands; then record what you ran in its `checks_run` and say in its `limitations` that the collector could not read it.
- A manifest with a top-level `error` read no target at all: do not call `finish`, and report the error as your one-line summary. The collector exits non-zero for it.
- For a `"collected"` target, copy its `commands` list into that target's `checks_run` — minus any entry whose `check` the same target lists in `checks_not_applicable` — and copy its `checks_not_applicable` and `limitations` verbatim. A target owes only the checks its own shape carries, so do not add `checks_not_applicable` entries saying a subnet owes no project-level check or the reverse.
- Every entry in a `"collected"` target's `candidates` is a verified finding: `check`, `object`, `severity`, `excerpt`, `impact` and the `command` that produced it are already computed, and `finish` overwrites your `evidence` with the collector's. What is still yours to write is the `title`, the `recommendation`, and for a `kind: manifest` remediation the file itself (§3).
- Pass `--manifest-file /opt/data/scratch/manifest_gcp-networking-fabric-audit.json` to `finish` (§5) so it cross-checks your `checks_run` against what the collector ran. This stream's `finish` requires it, or `--no-collector-manifest` with a reason, which reports the run partial.

#### 2.1 Subnet primary and secondary IP range exhaustion (`subnet-ip-exhaustion`)

- **Severity**: `critical`
- **Command**: the collector's subnet sweep, recorded on each subnet target as its `subnets list`, `container clusters list`, `instances list`, `addresses list` and `forwarding-rules list` reads joined with `&&`. `gcloud compute networks subnets list-usable` returns ranges but no usage, so it cannot answer this check.
- **What it measures**: Pod (secondary) ranges from GKE's own utilization fields in `gcloud container clusters list` (`defaultPodIpv4RangeUtilization`, each node pool's `podIpv4RangeUtilization`, `additionalPodRangesConfig`), which count allocated per-node blocks, the unit that runs out. Primary ranges as a lower bound: the unique internal IPs held by VM NICs, reserved internal addresses and forwarding rules, plus the 4 addresses GCP reserves in every range; serverless connectors and Google-managed endpoints are not counted. It skips proxy-only, PSC and NAT subnets (a `purpose` other than `PRIVATE` or `PRIVATE_RFC_1918`): Google manages their allocations and no read can count them, so they appear nowhere in the document — not measured, not as a gap. A Shared VPC host subnet is measured against the clusters and VMs of every project in scope, not only the host's, and a Pod range whose host subnet could not be listed is still reported, as an entry whose `limitations` says so.
- **Condition**: Subnet primary or secondary Pod IP range has < 15% available IP address capacity remaining.
- **Gaps**: a subnet target's `limitations` names the read of that subnet's own project that failed, such as Pod ranges not read; copy it with the target. A Pod range whose host subnet was not listed is still reported, on an entry whose `limitations` says only its Pod ranges were measured.
- **Remediation**: Expand subnet CIDR or allocate additional secondary IP range in Terraform VPC definition. The collector computes the finding; write it `kind: manual`, and promote it to `kind: manifest` per §3 only once you have found the Terraform file that defines the subnet.

#### 2.2 Cloud NAT gateway port allocation saturation (`cloud-nat-exhaustion`)

- **Discovery:** `gcloud compute routers list --project=$PROJECT --format=json` — binds `$ROUTER` and `$REGION`, and each entry of the router's `nats[]` binds a `$NAT`; run the command below once per gateway. A router with no `nats` entry is a BGP router, not a NAT gateway: skip it rather than reading its empty mapping as a gateway without IPs. A project with no NAT router has nothing to inspect: record the check in `checks_run` with the discovery command, not in `limitations`.
- **Command:** `gcloud compute routers get-nat-mapping-info $ROUTER --nat-name=$NAT --region=$REGION --project=$PROJECT --format=json`, corroborated by `routers list` (each NAT's `natIpAllocateOption`/`maxPortsPerVm`) and `routers get-status` (`result.natStatus[].autoAllocatedNatIps`).
- **`--nat-name` is not optional.** Unfiltered, `get-nat-mapping-info` returns every VM behind every gateway on the router. Compared against each gateway's own ceiling in turn, that measures one gateway's VMs against another's limit: a VM drawing 4096 ports from a dynamic gateway reads as 6400% of a static gateway's 64. Read once per gateway.
- **Flag when:** a NAT gateway is `AUTO_ONLY` with no auto-allocated external IP at all, or — **only where `enableDynamicPortAllocation` is on** — any VM's `interfaceNatMappings[].numTotalNatPorts` is `>= 80%` of that NAT's `maxPortsPerVm`. **A NAT that never overrode the field has no field:** `routers list` omits `maxPortsPerVm`, and the ceiling is then GCP's default of 65536. Use the default rather than passing over the gateway, which reads as clearing it.
- **Never measure a static gateway's ports.** With dynamic port allocation off, Cloud NAT reserves each VM exactly `minPortsPerVm`, so `numTotalNatPorts` _is_ the ceiling, the ratio is the constant 1.0, and every VM behind every stock gateway clears the 80% bar. Flagging on it reports `critical` port exhaustion fleet-wide, daily, on an install with no exhaustion anywhere. A static gateway that has genuinely run out shows up as a VM with no mapping at all — indistinguishable here from a VM that is simply idle, so report nothing rather than inventing a ratio.
- **Do NOT flag:** a `MANUAL` NAT IP allocation that still has addresses assigned; a VM under 80% of its port ceiling; any VM behind a gateway with dynamic port allocation off.
- **Severity:** `critical`.
- **Impact:** "VMs that exhaust their NAT port allocation see new outbound connections silently fail, which for a GKE node means pods lose egress with no error at the workload layer."
- **Remediation:** `kind: manifest`. Raise `maxPortsPerVm`, or add NAT IP addresses to the Cloud Router specification in Terraform. A port finding only ever names a dynamic-allocation gateway, so `minPortsPerVm` is never the ceiling that was breached.

#### 2.3 Private Service Connect endpoint routing deadlock (`psc-routing-deadlock`)

- **Command:** `gcloud compute forwarding-rules list --project=$PROJECT --format=json`
- **List unfiltered and select in the check.** `--filter="target:ServiceAttachment"` asks gcloud's `:` operator to match a plural, differently-cased substring inside a URL, and the check re-tests `serviceAttachments` in the target anyway. The filter therefore buys nothing, and if its semantics ever shift it returns an empty list, which reads as `CLEAN` rather than as an error — a blind check that reports the same word as a healthy one.
- **Flag when:** a forwarding rule targeting a Private Service Connect service attachment carries `pscConnectionStatus: REJECTED` or `pscConnectionStatus: CLOSED`.
- **Do NOT flag:** a PSC forwarding rule in `ACCEPTED` status; a forwarding rule whose target is not a service attachment at all.
- **Severity:** `major`.
- **Impact:** "Traffic aimed at this Private Service Connect endpoint cannot reach its target service; consumers see connection failures with no signal at the VPC layer."
- **Remediation:** `kind: manual`. Repair the target service attachment reference or update the forwarding rule's routing in Terraform — the correct target is a fact about the producer service this audit cannot read.

#### 2.4 VPC network MTU packet fragmentation mismatch (`mtu-packet-fragmentation`)

- **Command:** `gcloud compute networks list --project=$PROJECT --format=json`
- **Flag when:** two networks are joined by an `ACTIVE` VPC peering and their MTUs differ. This is a mismatch between two peered networks, never an absolute threshold — a single network's own MTU (1460, 1500, or otherwise) is a choice, not a defect, and packets only fragment where two different choices meet at a peering.
- **A missing `mtu` key means 1460, not unknown.** `networks list` omits the field on every network still at the default, so treating absence as unreadable skips the pair and leaves the check unable to fire on the mismatch that actually happens: a default network peered with one raised to 8896. Both sides would have to have been overridden, to different values, before that reading saw anything at all.
- **Do NOT flag:** a peering that is not `ACTIVE`; two peered networks at the same MTU, whatever the shared value is; a network with no peerings at all; a peering whose other end is not in this listing — a VPC in another project is genuinely unread, and defaulting it to 1460 would invent a mismatch.
- **Severity:** `major`.
- **Impact:** "Packets crossing this peering at the larger MTU get fragmented or dropped, which shows up as intermittent, hard-to-diagnose latency and retransmits rather than a clean failure."
- **Remediation:** `kind: manual`. Align both networks' MTU to the smaller of the two, or to 1500 if the larger side can be raised — either changes a network's core configuration, which this audit does not have enough context to propose automatically.

#### 2.5 Cloud Armor security policy evaluation anomalies (`cloud-armor-false-positive`)

- **Command:** `gcloud compute security-policies list --project=$PROJECT --format=json`, cross-referenced against `gcloud compute backend-services list --project=$PROJECT --format=json` to find which policies protect a production-looking backend.
- **Flag when:** a security policy attached to at least one production-looking backend service carries a rule in `preview` mode (excluding GCP's implicit default rule at priority `2147483647`), or the policy has two or more rules sharing one `priority`.
- **Do NOT flag:** a policy attached only to backends whose name has a token equal to a non-production token (`test`, `staging`, `stage`, `dev`, `sandbox`, `qa`), after the name is split on each character that is not a letter or a digit and the digits at the end of each token are removed (`api-dev2` is non-production; `device-gateway` is not); a policy attached to no backend service at all, which governs no traffic; the implicit default rule's own priority collision with itself. The production-backend condition governs both limbs above, the priority collision as much as the preview rule — an unenforced policy is a housekeeping note, not a finding this stream publishes.
- **Severity:** `minor`.
- **Impact:** "A preview-mode rule on a production backend logs matches without enforcing them, so the WAF looks like it is protecting traffic it is only observing; conflicting priorities make the effective policy unpredictable."
- **Remediation:** `kind: manual`. Take the validated rule out of preview mode and resolve the conflicting priorities — which of two colliding rules should win is a policy-intent judgment this audit cannot make.

#### 2.6 Management ports open to the whole internet (`firewall-world-open-ingress`)

- **Command:** `gcloud compute firewall-rules list --project=$PROJECT --format=json`, cross-referenced against `gcloud compute instances list --project=$PROJECT '--format=json(name,selfLink,zone,status,networkInterfaces,tags,serviceAccounts)'` to find which instances the rule can actually reach, and against `gcloud compute forwarding-rules list --project=$PROJECT --format=json` to find the load balancers the rule names. The subnet sweep and this check share one `instances list` read per project. If the `forwarding-rules list` read fails, the project target's `limitations` says that the load-balancer path was not measured. This check reads only the firewall rules of the network. Hierarchical firewall policies apply first and can deny what a rule allows. A network firewall policy can allow what no rule allows. This check reads neither policy, and the excerpt says so.
- **Flag when:** an enabled `INGRESS` rule names `0.0.0.0/0` or `::/0` in `sourceRanges`, its `allowed` block covers a management port over TCP, and at least one instance on that network holds an external IP inside the rule's target scope. Only a live instance counts: `RUNNING`, `STAGING`, `PROVISIONING` or `REPAIRING`. A stopped instance can keep a static IP but accepts no connection. The collector does the test for each external address of the instance on the rule's network. Internet traffic arrives only at an external address, so internal addresses do not count. If the allow has `destinationRanges`, the address must be in one of them. The management ports are remote access (22, 3389), the control plane's own (2379, 2380, 10250), and the datastores (1433, 3306, 5432, 6379, 9200, 27017) — ports whose service admits a caller on a credential, so opening one to the internet makes that credential the whole perimeter.
- **The instance read is not optional, for the reason §2.5 refuses to flag an unattached Cloud Armor policy.** A firewall opens a port on the instances it targets. An instance with no external address gets no internet traffic directly. Such a rule is a latent misconfiguration, not the exposure this check's impact describes, and publishing it as one would be a `critical` finding nobody can reproduce. An external passthrough Network Load Balancer is the exception: it keeps its own IP as the packet destination, so its backends get internet traffic with or without an external address. The collector decides this path only where the allow's `destinationRanges` name the IP of an external passthrough forwarding rule as a single address (`/32` or `/128`), and the forwarding rule carries the port and sends to a backend service, a target pool or a target instance, not to a proxy. That is the shape a GKE `LoadBalancer` Service writes. The excerpt then names the forwarding rule and its IP. The collector does not read the load balancer's backends, so it names each live instance in the rule's target, and the excerpt says so. Other load-balancer paths are a blind spot.
- **The instance count is a floor, and the excerpt says "visible to the audit identity" because of it.** `compute instances list` does not return GKE Autopilot node VMs to a caller holding project `roles/compute.viewer`: they answer 404 rather than 403, with no deny policy, IAM condition or org policy involved, and the grant that does return them is Owner. Understating a `critical` finding's blast radius costs nothing; what the blindness can cost is a finding, when a rule scoped by `targetTags` matches only invisible nodes and is dropped below as governing no traffic. A _target-scoped_ rule is undecided when no visible instance carries its target. A rule without a target is undecided when it reaches no visible instance and its network holds a GKE Autopilot cluster: the collector finds those networks in the subnet sweep's `clusters list` read. A rule without a target on a network with no Autopilot cluster stays clear. The collector names each undecided rule in the project target's `limitations`, which holds the run partial. That sentence is the one place on a `collected` target where a check is yours to confirm by hand, for example where the tag names an Autopilot cluster's node pool. A rule whose visible target instances are all stopped, all without an external address, or all blocked by a DENY is decided for those instances, and the collector does not flag it. Invisible Autopilot nodes that carry the same target stay a blind spot.
- **An `allowed` entry with no `ports` key opens every port, not none.** That is the API's encoding — it is how `default-allow-internal` and every `gke-<hash>-all` rule are written — so reading absence as "names no ports" lets the rule that opens all 65535 read cleaner than the one that names 22.
- **Do NOT flag:** a rule opening only web ports (80, 443, 8080) — GKE writes one `k8s-fw-<hash>` rule per LoadBalancer Service, opening exactly that to `0.0.0.0/0` because the Service asked it to, and admitting the web ports reports every LoadBalancer in the fleet as `critical` daily; a rule whose `sourceRanges` are all narrower than the whole internet; a disabled rule or an `EGRESS` one; a rule scoped by `targetTags` or `targetServiceAccounts` that no instance holding an external IP carries; a port that an `INGRESS` DENY blocks on an instance — precedence makes the allow unreachable there, and a hardened VPC often puts a blanket deny under narrow allows. A DENY blocks a port on an instance when it has higher or equal priority (a lower or equal number: a deny wins a tie), is on the same network, has a world source range in the family of the external address (`::/0` alone does not block traffic to an IPv4 address), has a target that includes the instance (no target, a tag the instance carries, or its service account), and has `destinationRanges` that contain that external address, if it has any. The collector decides this for each external address of the instance on the rule's network. The instance read carries the tags, the service accounts and the addresses, so the collector decides this for each instance. An instance where a DENY blocks every port the allow opens, on every external address, is not reachable through that allow. The excerpt names only the ports that are reachable on the reported instances. Also do NOT flag an IPv6-only allow on an instance with no external IPv6 address: the allow reaches only an external IPv6 address.
- **Severity:** `critical`.
- **Impact:** "Any host on the internet can open a TCP connection to these ports on the instances named, so whatever authenticates on the far side of the port is the entire perimeter."
- **Remediation:** `kind: manifest`. Declare a Config Connector `ComputeFirewall` for the rule in the reconciled GCP directory, with `spec.sourceRanges` narrowed to `35.235.240.0/20` — the IAP TCP-forwarding range — in place of the world range, and everything else copied from the live rule. Config Connector acquires an existing rule of the same name rather than failing on it, so the object adopts the rule the finding names instead of creating a second one. Narrow rather than delete: `gcloud compute ssh --tunnel-through-iap` keeps working through the IAP range, so the fix closes the internet without removing the access path, and a manifest that deletes the rule outright leaves nothing for a reviewer to compare against. **Do not propose private nodes as the fix**, however obviously it presents itself when every instance in the excerpt is a GKE node holding a public address. Taking the external IPs away is the larger correct change and the wrong one to derive from this finding: a private node reaches Artifact Registry only through Cloud NAT, so on a project whose `cloud-nat-exhaustion` scope entry recorded no NAT gateway (only the `routers list` read, no `get-status`) — §2.2's own read establishes this, at no extra cost — flipping `enablePrivateNodes` stops every image pull in the fleet. Name it in `recommendation.rationale` as the alternative considered and rejected, with the router count as the reason.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/vpc/subnets.tf`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gcp-networking-fabric-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §2 required and, where §2 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

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
            "command": "gcloud compute networks subnets list --project=proj-1 '--format=json(name,region,ipCidrRange,secondaryIpRanges,purpose,selfLink)' && gcloud container clusters list --project=proj-1 '--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools,autopilot)' && gcloud compute instances list --project=proj-1 '--format=json(name,selfLink,zone,status,networkInterfaces,tags,serviceAccounts)' && gcloud compute addresses list --project=proj-1 --filter=addressType=INTERNAL '--format=json(address,subnetwork)' && gcloud compute forwarding-rules list --project=proj-1 '--format=json(IPAddress,subnetwork)'"
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
            "command": "gcloud compute routers list --project=proj-1 --format=json && gcloud compute routers get-status nat-router --region=us-central1 --project=proj-1 --format=json"
          },
          {
            "check": "psc-routing-deadlock",
            "command": "gcloud compute forwarding-rules list --project=proj-1 --format=json"
          },
          {
            "check": "mtu-packet-fragmentation",
            "command": "gcloud compute networks list --project=proj-1 --format=json"
          },
          {
            "check": "cloud-armor-false-positive",
            "command": "gcloud compute security-policies list --project=proj-1 --format=json && gcloud compute backend-services list --project=proj-1 --format=json"
          },
          {
            "check": "firewall-world-open-ingress",
            "command": "gcloud compute firewall-rules list --project=proj-1 --format=json && gcloud compute instances list --project=proj-1 '--format=json(name,selfLink,zone,status,networkInterfaces,tags,serviceAccounts)' && gcloud compute forwarding-rules list --project=proj-1 --format=json"
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
        "command": "gcloud container clusters list --project=proj-1 '--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools,autopilot)'",
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
  --manifest-file /opt/data/scratch/manifest_gcp-networking-fabric-audit.json \
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
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git and forge write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, and authorization headers must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`Subnet/<name>`, `Router/<region>/<name>`), never an ephemeral execution timestamp.
- **Never emit a manifest that directly deletes a network or subnet.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
