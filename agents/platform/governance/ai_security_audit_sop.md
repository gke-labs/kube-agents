# SOP: AI Workload Security Audit (Daily Governance)

**Purpose:** A read-only, fleet-wide sweep of every managed GKE cluster for the security properties that are specific to _AI inference and training workloads_ — a model server reachable from the internet, a model repository trusted to execute its own code, model weights the serving process can rewrite, an artifact pulled from an unpinned source, a model-registry credential sitting in plaintext, and a model-server image that changes underneath the cluster. The question this audit answers for a platform admin is: _who can reach my models, what can rewrite them, and where did their weights come from?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying generated manifests for the findings that get promoted.

**Cron:** id `ai-security-audit`, schedule `50 8 * * *` (daily 08:50 UTC). Every other audit stream occupies a `:20` or `:50` slot between 06:20 and 08:20, so this is the first free one: no two streams issue a fleet-wide `kubectl` sweep at the same minute, including on Mondays when the three weekly streams also run.

**Scope boundary — this audit owns the _AI-specific_ security surface and nothing else.** Every check below applies only to a workload the §2 discriminator identifies as an AI workload, and every one of them asks a question no other stream asks. The generic container-hardening questions — privileged containers, host namespaces, hostPath mounts, wildcard RBAC, missing NetworkPolicy, default-ServiceAccount automounting, Workload Identity — belong to the **Security & RBAC Posture Audit** (`compliance-audit`) and are audited there on every workload including the AI ones. Do not re-report them here. Two ledgers carrying one verdict on one object is the failure mode this boundary exists to prevent: the object gets fixed once and reported as resolved twice, or gets fixed in one stream's PR and re-flagged forever by the other.

**Data sources:** `kubectl` read verbs, `gcloud container clusters|node-pools list|describe`, and the `gke` MCP server. **Nothing else** — no BigQuery, no Prometheus/GMP, no Policy Controller / Gatekeeper, no Security Command Center, no Model Armor or Vertex AI API calls, no external model registry, no blueprint, no kanban delegation to Cluster Agents. Every conclusion is derived from live cluster reads you performed in this run. In particular: **this audit does not evaluate the model.** Prompt-injection resistance, jailbreak susceptibility, output filtering quality, and training-data provenance are all real AI risks and none of them are visible to `kubectl`; a stream that guessed at them would publish unfalsifiable findings into a public issue. What is auditable here is the workload's _configuration_, and that is the whole remit.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit ai-security-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/ai-security-audit/org__repo", "findings_path":"/opt/data/scratch/findings_ai-security-audit.json", "pending_remediation_requests":[…]}`. Keep `findings_path` and `workspace` from this call; you write into both.

- `workspace` is the GitOps clone `start` made for you. The audit pod does not begin life inside a checkout, so this is the only tree that exists, and every `remediation.path` in Step 4 is resolved against it — a manifest written elsewhere is one the harness cannot find.
- `issue` is this stream's open ledger issue, or `null` when it has none. Either way you never create it — `finish` owns that.
- `pending_remediation_requests` lists finding ids a repo writer asked for with a `/remediate` comment on the ledger. Write a manifest for each one while you inspect (Step 4), or the promotion fails for want of a file. Each comment is answered once and only once — an acknowledgement listing every target and its outcome, a refusal naming the reason, or both when one comment mixes valid and invalid targets (the reasons the harness emits are: the commenter is not recorded as a collaborator, reported as the `authorAssociation` it saw rather than as a write-access check it never makes; an id absent from the current document; a target that is not a `manifest`; a bare `/remediate` naming nothing; a `/remediate all` that matched nothing promotable; and a command not written at the start of its own line and so never parsed as one) — with a hidden marker keeping yesterday's request from being answered again today. `/remediate all` is accepted too, and expands against this run's manifest findings.
- `start` creates and resets no branch. There is no report branch.

The helper owns every `git`/`gh` operation and renders the ledger issue body and every remediation PR body — **never hand-write an issue or PR body, never run `git commit`, `git push`, `gh issue create`, `gh pr create`, or `gh issue comment` yourself.**

**Never comment on the ledger yourself.** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit: an agent that posts it — including when someone asks for a fix in chat — is authorizing its own pull request. `finish` ignores a `/remediate` from a machine account, so posting one achieves nothing but noise on the issue.

### 1. Enumerate the target fleet

```bash
gcloud container clusters list --format=json
```

- Target every cluster with `status == "RUNNING"`. Record `{name, location, project, checks_run}` into `scope.clusters`.
- **`checks_run` is mandatory on every cluster,** and each entry is an object, never a bare string:

  ```json
  {
    "check": "model-image-floating-tag",
    "command": "kubectl --context gke_<project>_<location>_<cluster> get deploy,sts,ds,cronjob,pod -A -o json"
  }
  ```

  `check` is the backticked slug from the §3 heading that defines it — `inference-endpoint-public`, `weights-mount-writable`, and so on — never the section number and never prose. (`start` prints the full roster of six; the SOP still says what each check _is_.) `command` is the literal invocation you issued on that cluster for that check, with its `--context` and the namespace or resource it targeted. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl` — that is the harness's allowlist, not this stream's roster: `curl` has no legitimate use here, because §1 restricts this audit's data sources to `kubectl` and `gcloud` and the Red Lines forbid any request to a model endpoint. `echo`, `cat`, `python3 -c`, and a call back into `audit_report.py` are all rejected, as is anything under eight characters.

  **Keep this command short — a line or two, not a program.** The ledger renders `checks_run` as a table, and a 700-character `jq` program in a table cell is unreadable even though it publishes in full: the harness gives this field the same 2,000-character allowance as `evidence.command`, precisely so that a re-runnable command is never clipped into one that is not. Legibility is on you. Give the _collection_ command here — the `kubectl get … -o json` that produced the dump this check read, with its `--context`, and never the `cat` of the dump that `$WL` expands to, which the validator rejects as a command that inspected nothing — and put the object-scoped confirm read from §2 in the finding's `evidence.command`, which is allowed 2,000 characters. The two fields answer different questions: `checks_run[].command` proves the check ran against that cluster, `evidence.command` proves the individual finding.

  The validator rejects an unknown slug, a duplicate, a missing or unusable command, the field being absent, and an empty list unless that cluster's `limitations` says why nothing ran: a cluster you could read but ran nothing against is not a clean cluster, it is an audit that did not happen. Anything short of the checks that apply to that cluster makes the run **partial** exactly as a `limitations` note does, so the ledger stays open and nothing is announced as resolved. Append the entry when its check completes, not when you intend to run it.

- **A check the cluster's shape rules out is not a gap — declare it.** Alongside `checks_run`, a cluster may carry `checks_not_applicable` as a list of `{check, reason}`:

  ```json
  {
    "check": "inference-endpoint-public",
    "reason": "Illustrative only — no §3 check on this stream is ruled out by a cluster's shape; read the next two bullets before you use this field."
  }
  ```

  Same slugs as `checks_run`, and the `reason` must say why the check _cannot_ apply here — "N/A" and "not applicable" are rejected; name the property of the cluster that rules it out. Those checks leave the denominator instead of counting as missing, so the cluster reads as complete rather than forever-incomplete, and the ledger can eventually close.

- **A cluster with no AI workloads is the common case, and it is still a fully audited cluster.** Most clusters in a fleet run none. The six checks still _ran_ there: you took the §2 dump and every filter returned nothing. So record **all six** in that cluster's `checks_run` against that cluster's collection command, leave `checks_not_applicable` empty, and do **not** write a `limitations` note. This is the single most important instruction in this section, and the harness enforces half of it: the validator rejects an empty `checks_run` on a cluster you could read, whatever `checks_not_applicable` says, so a document written the other way publishes nothing at all. The other half is yours: a `limitations` string would mark the run `partial`, and on a fleet where nine of ten clusters serve no models the daily stream would be pinned at `partial: true` forever — `resolved: 0`, no stale remediation PR ever retired, the ledger never able to close. "Six checks ran and matched nothing" is the honest record of a cluster that runs no models.
- **Autopilot changes nothing here.** Every §3 check reads a workload spec, and Autopilot admits all six shapes. Do not put anything in `checks_not_applicable` on account of Autopilot.
- **One question decides the scope list.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; name any check that could have run there and did not in that cluster's `limitations`, and any check its shape rules out in `checks_not_applicable`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces both halves.
- A cluster you cannot read goes in `scope.skipped` with a literal reason: `"status=STOPPING"`, `"get-credentials failed: <stderr first line>"`, `"timeout after 30s"`. A skipped cluster is never silently dropped.
- A partial read is **not** a skip. If the dump succeeded but one kind was refused, the cluster stays in `scope.clusters` and the refusal goes in its `limitations`: `"RBAC: cannot list services; check 3.1 not run."`
- Obtain per-cluster credentials into an isolated kubeconfig so clusters cannot bleed into each other:
  ```bash
  export KC="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml"
  KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
  ```
- If **zero** clusters land in `scope.clusters`, do **not** call `finish` — the helper hard-fails on an empty scope. Report the enumeration failure as your one-line summary and stop.

### 2. Collect workload state and identify the AI workloads

Two JSON dumps per cluster answer every check in Step 3. **Do not run a separate full-fleet query per check.**

```bash
set -euo pipefail
KUBECONFIG=$KC kubectl get deploy,sts,ds,cronjob,pod -A -o json > /opt/data/scratch/ai_wl_<cluster>.json
KUBECONFIG=$KC kubectl get svc -A -o json > /opt/data/scratch/ai_svc_<cluster>.json
jq -e '.items|type=="array"' /opt/data/scratch/ai_wl_<cluster>.json > /dev/null
jq -e '.items|type=="array"' /opt/data/scratch/ai_svc_<cluster>.json > /dev/null
```

**The two `jq -e` gates are the load-bearing guard of this whole audit, and they have to be here rather than on the pipelines in §3.** The redirect creates the file before `kubectl` runs, so a `kubectl` that fails — expired credentials, an API-server timeout, RBAC — leaves a zero-byte dump behind. Every §3 check then reads that dump through `$WL`, and `cat` of an empty file succeeds while `jq` over empty input prints nothing and exits `0`: a total collection failure and a genuinely clean cluster produce byte-identical output. `pipefail` does not catch this one, because nothing in the pipeline failed. The gate does: `jq -e` exits non-zero on an absent, empty, truncated, or non-List dump, and `set -e` stops the cluster there. A dump that does not pass both gates is a cluster you could not read — `scope.skipped`, or `limitations` if only one kind was refused — and **never** a cluster with six checks and no findings.

`job` is deliberately excluded while `cronjob` is included: a CronJob-generated Job carries a random name suffix, so a finding on one is announced as fixed and re-announced as new on the next run, and the daily delta this ledger exists to produce becomes noise. Audit the CronJob, which is what a human edits.

Shared setup, evaluated once per cluster. `$WL` reads the dump you just took rather than re-querying, which is what keeps every check below pinned to the cluster whose credentials Step 1 fetched: a bare `kubectl` here carries no `KUBECONFIG=$KC` and would answer from whatever context the pod's default kubeconfig points at, reporting one cluster's contents under another cluster's name. `$PRE` applies the universal suppressions, normalises every workload to `{kind, ns, name, lbl, spec}`, and then keeps only the AI workloads — so each check below is `$WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'| <filter>'`.

```bash
set +e; set -o pipefail
SYS='^(kube-system|kube-public|kube-node-lease|gke-.*|gmp-system|gmp-public|gke-gmp-system|gke-managed-.*|cnrm-system|configconnector-operator-system|krmapihosting-system|istio-system|asm-system|anthos-identity-service|config-management-.*|gatekeeper-system|composer-system)$'
MDL='(^|/)(vllm|sglang|text-generation-inference|tgi|tritonserver|torchserve|tensorflow-serving|kserve|ollama|ray|llama|mlserver|seldon|lorax|aibrix)([-:@/]|$)'
WL='cat /opt/data/scratch/ai_wl_<cluster>.json'
PRE='.items[]
 | select((.metadata.namespace|test($sys)|not)
      and (.kind!="Pod" or ((.metadata.ownerReferences//[])|length)==0)
      and (((.metadata.labels//{})["addonmanager.kubernetes.io/mode"] // (.metadata.annotations//{})["components.gke.io/component-name"])==null))
 | {kind, ns:.metadata.namespace, name:.metadata.name,
    lbl:(.spec.template.metadata.labels // .spec.jobTemplate.spec.template.metadata.labels // .metadata.labels // {}),
    spec:(.spec.template.spec // .spec.jobTemplate.spec.template.spec // .spec)}
 | select(([(.spec.containers//[])[].image // ""] | any(test($mdl)))
       or ([(.spec.containers//[])[] | (.resources.limits//{}) | keys[]]
             | any(test("nvidia\\.com/gpu|google\\.com/tpu"))))'
```

**`set +e; set -o pipefail`, and both halves are deliberate.** `pipefail` is what makes `$WL | jq` fail loudly: `$WL` is a `cat`, and without it the pipeline reports `jq`'s status, so a `cat` that failed still exits `0` and the check's empty output is recorded as "no findings". `-e` is off from here on because these variables live in the same shell as Step 4, where `grep -rl` exits `1` on the expected "this workload has no declaration in the clone" outcome — the outcome §4 calls the common one — and `-e` would abort the audit on a normal result. Both are **per-shell** settings that the `set -euo pipefail` in the dump block above does not carry into a new one: if your harness gives each fenced block its own shell, re-issue this line together with the variables, because a check run without `pipefail` is a check that cannot report its own failure.

**The AI discriminator is the last `select`, and it is deliberately two-pronged:** a container image naming a known inference or serving runtime (`$MDL`), **or** a container requesting an accelerator (`nvidia.com/gpu`, `google.com/tpu`). Either is sufficient. **The TPU prong tests keys of `resources.limits`, so it must spell the extended resource, `google.com/tpu`** — `cloud.google.com/tpu-accelerator` and its siblings are _node labels_ and appear in a `nodeSelector`, never as a resource name, so a prong matching `cloud.google.com/tpu` matched no pod that has ever run on GKE and every TPU inference server in the fleet dropped out of the audit before the first check saw it. The pattern is unanchored, so it still covers a `cloud.`-prefixed spelling if anything ever emits one. The accelerator prong catches the in-house model server whose image is `acme/recommender:v4` and which no name-based pattern could ever match; the image prong catches the CPU-served small model that requests no GPU. A workload matching neither is not this audit's business, however AI-adjacent its name looks — **never widen the discriminator on the strength of a namespace called `ml` or a Deployment called `ai-gateway`.** Naming is not evidence, and a stream that audits by vibe reports findings the owner will not accept.

**Both sides of that `or` are single booleans on purpose.** In `jq`, `false or (empty)` evaluates to `empty`, not to `false` — so writing the accelerator prong as a bare `keys[]` generator inside `or` makes a non-matching workload vanish from the pipeline entirely instead of being rejected, and a workload that vanishes is one this audit silently never examined. Each side is wrapped in `[…] | any(…)` so it always yields exactly one boolean. The same rule applies to every filter in §3: **no bare generator on either side of an `or`.**

**Universal suppressions — every check in this section:** namespaces matching `$SYS`; objects carrying `addonmanager.kubernetes.io/mode` or `components.gke.io/component-name` (the GKE-managed add-ons, several of which — `nvidia-gpu-device-plugin`, `gke-metadata-server` — sit squarely in the accelerator prong's path and are not yours to fix); pods with a non-empty `ownerReferences` — audit the **owning controller**, never the pod, because pod name suffixes are random. `kubeagents-system` is deliberately **not** suppressed: the harness audits itself.

Read workload **templates** (`spec.template.spec`), not live Pods. Templates are what an admin edits, and they are unaffected by admission-time defaulting.

**Evidence discipline.** The dump is the _detector_; a live single-object read is the _confirmer_. For every candidate finding, run the object-scoped command below, capture a trimmed excerpt, and store that exact string in `evidence.command`, with `<kind>`, `<ns>` and `<name>` filled in and every shell variable **expanded** so a human can paste it unchanged — a published command containing an unset shell variable is not re-runnable, which defeats the point of publishing it. If the confirm command fails or the condition no longer holds, **drop the finding — do not soften it.**

```bash
KUBECONFIG=$KC kubectl get <kind> -n <ns> <name> -o yaml
```

**Read `$?` on every §3 filter, mechanically, not as a habit.** Empty stdout is what a clean check and a dead check both look like, so run each one in the shape below rather than reading its output straight:

```bash
OUT=$($WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'| <filter>'); RC=$?
[ "$RC" -eq 0 ] || echo "CHECK ABORTED rc=$RC"   # -> that cluster's limitations, never a clean check
printf '%s\n' "$OUT"
```

`RC=0` with an empty `$OUT` is the **only** reading of "this check matched nothing"; every other combination is a check that did not run. `jq` exits `2` on a bad argument — `--argjson ai "$AI"` in 3.1 when `$AI` came back empty because the workload dump was unreadable, which prints to stderr and leaves stdout empty — `3` when the filter did not compile, and `5` on a runtime error. Treat `5` as the most dangerous of the three: `jq` streams, so it aborted partway and every object after the offender went unexamined, which is not "fewer findings" but "unknown findings". This whole recipe depends on `pipefail` being set in this shell; without it `$?` is `jq`'s status alone and a `$WL` that failed is invisible.

**Never paste a credential into an excerpt.** This audit runs a check (3.5) whose whole subject is a credential in an environment variable, and the excerpt for it must prove the _shape_ and never the _value_: quote the variable's name and the fact that it carries a literal `value:`, and re-read with a projection that omits the value itself (`-o jsonpath='{.spec.template.spec.containers[*].env[*].name}'`). **Project, never trim** — the `-o yaml` confirm read is not a safe place to go looking for that shape, because `kubectl apply` writes the entire applied pod spec back into `metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]` with every `env[].value` verbatim, so the credential appears a second time hundreds of lines away from the `env:` block you were careful about. A Secret's `data:` block, a ServiceAccount token, a kubeconfig, a Hugging Face or model-registry token, and a private key must never reach `evidence.excerpt`. The harness redacts high-confidence credential shapes as a backstop, not as the primary control; the primary control is this paragraph.

**Two more shapes reach the excerpt by accident, and this ledger is a public issue.** 3.2, 3.4 and 3.6 quote a container's `args` or an image reference, and neither field is credential-free: an inference server is routinely started with `--api-key <value>` or `--hf-token <value>` sitting next to the argument being reported, and a plaintext model URL — exactly what 3.4 exists to flag — can carry its own `user:pass@` userinfo. Neither shape is a high-confidence match for the harness's redactor, so nothing catches them after you. Quote only the argument the finding is about, write `(withheld)` in place of the value of any `--*key`, `--*token`, `--*secret` or `--*password` argument, and strip the userinfo out of every URL you reproduce — in `evidence.excerpt`, in `title`, and in `recommendation.action`, which for 3.4 is where the URL is written a second time.

If one check yields more than 25 findings in a single cluster, roll the surplus into one namespace-level finding per namespace: same severity, and a namespace-scoped confirm command. Give the roll-up the scope it covers as its `object` — `Namespace/<ns>`, nothing more — put the workload count in the `title` and the names in `evidence.excerpt`, never in `object`. The individual findings it replaces are not also emitted.

**Finding identity.** **Do not write an `id`.** The harness derives it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. `check` is the backticked slug in the check's heading below; a slug outside this SOP's roster is rejected.

Identity is only as stable as those four fields, so **never** let a timestamp, image tag, pod name, model revision, or replica count into `object` — audit the owning controller, never the pod. One finding per (check, object): three containers in one Deployment trusting remote code are **one** finding listing all three in `evidence.excerpt`. Two findings agreeing on all four fields are one finding, and the harness refuses the document rather than collapsing them silently.

### 3. Checks

#### 3.1 Inference endpoint reachable from the internet (`inference-endpoint-public`)

```bash
AI=$($WL | jq -c --arg sys "$SYS" --arg mdl "$MDL" "[$PRE"' | {ns, lbl}]')
cat /opt/data/scratch/ai_svc_<cluster>.json | jq -r --argjson ai "$AI" '.items[]
 | select(.spec.type=="LoadBalancer"
      and ((.metadata.annotations//{})["networking.gke.io/load-balancer-type"]//"") != "Internal"
      and ((.metadata.annotations//{})["cloud.google.com/load-balancer-type"]//"") != "Internal"
      and (((.spec.selector//{})|length) > 0))
 | . as $s
 | select([$ai[] | select(.ns == $s.metadata.namespace)
             | . as $w | ($s.spec.selector|to_entries|all(. as $e | $w.lbl[$e.key] == $e.value))]
          | any)
 | "\(.metadata.namespace)/Service/\(.metadata.name)"'
```

- **Flag when:** a `Service` of `type: LoadBalancer`, carrying neither internal-LB annotation, selects the pods of an AI workload. The selector must be a genuine subset match against that workload's pod labels in the same namespace — a Service is in scope because of what it routes to, not because of where it sits.
- **Both internal-LB annotations are tested, because GKE has two.** `networking.gke.io/load-balancer-type` is the current spelling and `cloud.google.com/load-balancer-type` is the older one, still honoured by GKE and still in wide use. Testing only the first reports every internal LoadBalancer written the older way as a `critical` model server on the public internet — a false positive on this check is not a harmless one, because it is the check whose whole claim is "anyone can reach this".
- **Never publish the address.** The finding names the Service, not where it answers. `kubectl get svc -o yaml` returns `status.loadBalancer.ingress[].ip`, and this ledger is a public GitHub issue: pasting that address under a `critical` heading that says a model server is internet-reachable and its authentication unknown is an advertisement, not evidence. Keep the confirm read to the spec — `-o jsonpath='{.spec.type}{" "}{.spec.selector}'`, and not `{.metadata.annotations}`, which drags `last-applied-configuration` in with it — and keep the address out of `title`, `object`, `evidence.excerpt`, and `recommendation`.
- **The selector guard is load-bearing.** `(((.spec.selector//{})|length) > 0)` must be evaluated **before** any `to_entries` on the selector: a selector-less Service (a headless Service, or one with hand-managed Endpoints) yields `null`, and `null | to_entries` aborts the whole `jq` program with exit 5. Because `jq` streams, that abort happens partway through — every Service after the offender goes unexamined, so an exposed model server later in the list is silently never reported. That is an order-dependent false all-clear, which is the worst outcome this audit can produce.
- **Do NOT flag:** standard exclusions; `type: ClusterIP` or `NodePort`; a LoadBalancer annotated `networking.gke.io/load-balancer-type: Internal` or `cloud.google.com/load-balancer-type: Internal`; a LoadBalancer selecting a workload the §2 discriminator did not match (that is a general exposure question and it is not this stream's); `type: ExternalName`. **Do not attempt to determine whether the endpoint requires authentication.** Deciding that would mean sending a request to the model, and this audit is read-only against the cluster and never issues inference traffic.
- **Severity:** `critical`. An unauthenticated model server on a public address is simultaneously a data-exfiltration path (the model and, via prompts, whatever context it is given), a compute-theft path (GPU cycles are expensive and inference endpoints are scanned for constantly), and an abuse-attribution path.
- **Impact:** "This model server is reachable from the public internet. Anyone who finds the address can send it inference traffic, consume its accelerator capacity, and probe whatever the model can reach."
- **Remediation:** `kind: manual`, always. The right fix depends on facts this audit cannot read — whether an authenticating proxy already fronts the service, whether the LoadBalancer is deliberate and gated by Cloud Armor, whether the correct answer is an internal LB plus a Gateway. Flipping a Service to `ClusterIP` in a pull request would take a serving endpoint offline the moment it merged, and an audit that causes an outage is an audit that gets switched off. State the options in `recommendation.action` and let the owner choose.

#### 3.2 Model repository trusted to execute its own code (`model-remote-code-trusted`)

```bash
$WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'
 | . as $w | (.spec.containers//[]) + (.spec.initContainers//[]) | .[]
 | select(([(.args//[]),(.command//[])] | flatten
             | any(test("trust[-_]remote[-_]code(?!=(0|false|no))";"i")))
       or ([(.env//[])[] | select(.name|test("TRUST_REMOTE_CODE";"i")) | (.value//"")]
             | any(test("^(1|true|yes)$";"i"))))
 | "\($w.ns)/\($w.kind)/\($w.name) \(.name)"'
```

- **Flag when:** an AI workload's container or init container passes `--trust-remote-code` (or `--trust_remote_code`) in `args`/`command`, or sets `TRUST_REMOTE_CODE` to a truthy value in `env`. Init containers count because a weights-fetch step that trusts remote code executes it with the same ServiceAccount and writes into the same volume the server then loads.
- **The args prong is a lookahead, not a substring match, and both parts of that matter.** Without `(?!=(0|false|no))` the filter also matches `--trust-remote-code=false` and reports the workload that explicitly turned the flag off, which is the reading the next bullet forbids. Keeping the rest of the match unanchored is what still catches the flag inside a `command: ["sh","-c","… --trust-remote-code --model x"]`, and the `"i"` is what keeps `--trust-remote-code=True` a finding rather than a silent pass.
- **Do NOT flag:** standard exclusions; the flag present but explicitly disabled (`TRUST_REMOTE_CODE=false`, `--trust-remote-code=false`); a workload that does not match the §2 discriminator.
- **Severity:** `critical`. This is not a hardening nicety. The flag instructs the loader to execute Python that ships _inside the model repository_, at start-up, with the serving container's identity — its ServiceAccount token, its Workload Identity binding, its network position, and its mounted weights. It converts "we pulled a model" into "we ran that publisher's code", and combined with 3.4 (unpinned source) it converts it into "we run whatever that publisher pushes next".
- **Impact:** "The model loader executes arbitrary code shipped inside the model repository, with this pod's ServiceAccount, network access, and mounted volumes. A compromised or swapped model artifact is remote code execution in this namespace."
- **Remediation:** `kind: manifest` (subject to §4's declaration rule). Rewrite the object's existing declaration with the flag removed from `args`/`command`, or the environment variable removed, and nothing else changed. This is the one check in this audit whose fix is a mechanical single-token deletion from a file the repository already owns, which is why it is the one that can be promoted unattended. State in `note` that a model genuinely requiring custom code will fail to load without the flag, and that the supported path is to vendor the reviewed modelling code into the image rather than to trust the repository at runtime. If the object has no declaration in the clone, the finding degrades to `kind: manual` under §4.

#### 3.3 Model weights mounted writable by the serving process (`weights-mount-writable`)

```bash
$WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'
 | . as $w
 | (.spec.volumes//[]) as $vols
 | (.spec.containers//[])[] | . as $c | (.volumeMounts//[])[]
 | select((.readOnly//false) == false)
 | . as $m
 | ($vols[] | select(.name == $m.name)) as $v
 | select(($v.csi != null and ($v.csi.readOnly//false) == false)
       or ($v.persistentVolumeClaim != null and ($v.persistentVolumeClaim.readOnly//false) == false))
 | "\($w.ns)/\($w.kind)/\($w.name) \($c.name) \($m.name) \($m.mountPath)"'
```

- **Flag when:** a container in an AI workload mounts a CSI volume or a PersistentVolumeClaim read-write — neither the `volumeMount` nor the volume itself sets `readOnly: true`.
- **The volume join is load-bearing.** The filter binds `$m` to the `volumeMount` and then looks up the matching entry in `.spec.volumes` by name. A filter that tests `.csi` or `.persistentVolumeClaim` directly on the `volumeMount` matches **nothing, ever** — those fields live on the volume, not on the mount — and a check that can never fire publishes a permanent all-clear. Keep the join.
- **Do NOT flag:** standard exclusions; a mount with `readOnly: true`; a volume whose own `readOnly` is true (a read-only PVC or CSI volume cannot be written through even a read-write mount); `emptyDir`, `configMap`, `secret`, `downwardAPI`, and `projected` volumes — a scratch directory is not a weights store, and flagging every `emptyDir` would bury the real findings; weights baked into the container image (there is no volume, and the image layer is already immutable).
- **Severity:** `major`. It is a persistence and integrity problem rather than an immediate breach: an attacker who achieves execution in the pod — via 3.2, via a dependency, via the application — can rewrite the weights that every subsequent replica loads, and the change survives the pod that made it. Not `critical`, because it requires a prior foothold.
- **Impact:** "The serving process can overwrite its own model weights. Any code execution in this pod becomes a persistent, replica-wide model swap that outlives the compromised pod and is invisible to an image scanner."
- **Remediation:** `kind: manifest` (subject to §4's declaration rule). Rewrite the object's existing declaration with `readOnly: true` added to that `volumeMount`. Do **not** set `readOnly` on the volume itself — a CSI volume shared with a writer sidecar or an init container that stages the download would break, and the mount-level flag scopes the change to the container that was flagged. State in `note` that a workload which downloads its weights at start-up into the same mount must keep that init container's mount writable, and that only the serving container's mount is being changed.

#### 3.4 Model artifact pulled from an unpinned source (`model-artifact-unpinned-source`)

```bash
$WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'
 | . as $w | (.spec.containers//[]) + (.spec.initContainers//[]) | .[]
 | . as $c
 | ([(.args//[]),(.command//[])] | flatten) as $a
 | ([(.env//[])[] | (.value//"")]) as $e
 | select((($a + $e) | any(test("(^|=)(http|ftp)://")))
       or (($a | any(test("^--model(-id)?(=|$)"))) and ($a | any(test("^--revision(=|$)")) | not)))
 | "\($w.ns)/\($w.kind)/\($w.name) \(.name)"'
```

- **Flag when:** an AI workload's container or init container either (a) names a model artifact over a plaintext `http://` or `ftp://` URL — as a bare argument, as `--flag=<url>`, or as an environment-variable value; or (b) passes `--model` / `--model-id` without a companion `--revision`, which resolves the repository's moving default branch at every pod start.
- **Prong (b) accepts both argument spellings, `--model x` and `--model=x`.** `argparse` treats the two identically and both are common in the wild, so matching only `^--model$` left every workload written the second way unexamined by the one check that decides whether its weights are pinned, while matching only `^--revision$` reported `--revision=<sha>` — a pinned workload — as unpinned. `(=|$)` after each flag name is what makes the two spellings one case; it is not a loosening, because `--models` still fails to match.
- **Do NOT flag:** standard exclusions; a `--model` argument accompanied by `--revision`; a model path that is a local filesystem path already populated by an image layer or a read-only volume (there is no fetch, so there is no source to pin); `https://` URLs to a pinned object; an object-store URI (`gs://`, `s3://`) — object versioning and bucket IAM are the control there and this audit cannot read either, so reporting it would be a guess.
- **Severity:** `major`. On its own it is a supply-chain exposure rather than a live compromise. **Escalate to `critical` in `impact` and severity when the same container is also flagged by 3.2** — an unpinned source plus trusted remote code means the publisher can push new code into your cluster at the next pod restart, with no change to any manifest and nothing for a reviewer to see. Say so explicitly in the `impact` when both hold, and cross-reference the 3.2 finding.
- **Impact:** "The model artifact this container loads is not pinned: the bytes that arrive at the next pod restart are whatever the source serves then. Nothing in the manifest records which model is actually running."
- **Remediation:** `kind: manual`. The correct revision is a fact about the model the owner chose, and this audit cannot read it — inventing a commit SHA or picking the current `main` would pin the workload to whatever happens to be live at 08:50 today, which is not a security improvement, it is a silent version change. Put the exact argument to add in `recommendation.action` (`--revision <sha>`, with the sha to be read from the model repository) and name the `https://` replacement for any `http://` URL — with any `user:pass@` stripped out of it, because `recommendation.action` publishes into the same public issue `evidence.excerpt` does.

#### 3.5 Model or registry credential in a plaintext environment variable (`model-credential-plaintext-env`)

```bash
$WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'
 | . as $w | (.spec.containers//[]) + (.spec.initContainers//[]) | .[]
 | . as $c | (.env//[])[]
 | select((.value//"") != "" and (.valueFrom == null)
      and (.name | test("HF_[A-Z_]*TOKEN|HUGGING_?FACE.*TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|WANDB_API_KEY|(MODEL|REGISTRY|INFERENCE).*(TOKEN|KEY|SECRET|PASSWORD)";"i")))
 | "\($w.ns)/\($w.kind)/\($w.name) \($c.name) \(.name)"'
```

- **Flag when:** an AI workload's container declares an environment variable whose **name** matches a model-registry or model-API credential pattern and which carries a literal `value:` rather than a `valueFrom.secretKeyRef`.
- **Report the name, never the value.** The `object` is the workload and the `evidence.excerpt` names the variable and the fact that it is literal — `HF_TOKEN is set with a literal value: (contents withheld)`. The value must not appear in the excerpt, in the title, in the recommendation, or in the remediation note. Re-read with `-o jsonpath='{.spec.template.spec.containers[*].env[*].name}'` to produce a quotable excerpt that structurally cannot contain the secret. This is the check most likely to put a live credential into a public GitHub issue, and the harness's redaction is a backstop behind this instruction, not a substitute for it.
- **Do NOT flag:** standard exclusions; a variable sourced from `valueFrom.secretKeyRef`, `configMapKeyRef`, or `fieldRef`; an empty `value: ""`; a name matching the pattern that is plainly a non-secret (`HF_TOKEN_PATH`, `OPENAI_API_KEY_FILE`, `MODEL_REGISTRY_KEY_ID` where the value is a visible identifier rather than a secret) — judge the name, and when it is genuinely ambiguous, do not flag: a false positive here costs a reviewer a credential rotation they did not need.
- **Severity:** `major`. The credential is readable by anyone with `get pod` in the namespace, is copied into every pod's environment, appears in `kubectl describe` output, and is committed to the GitOps repository in plaintext if the manifest is stored there.
- **Impact:** "A model-registry credential is embedded in this workload's pod spec in plaintext. It is visible to anyone who can describe the pod or read the manifest in Git, and it is not rotatable without a redeploy."
- **Remediation:** `kind: manual`, always. Moving the value into a Secret means creating a Secret containing the credential — and this audit's remediation path writes files into a Git repository, so generating that manifest would commit the plaintext credential to version control, which is worse than the finding. **Never emit a Secret manifest, and never copy the value anywhere.** `recommendation.action` names the variable, the `secretKeyRef` shape to replace it with, and the fact that the credential must be considered exposed and rotated.

#### 3.6 Model server image on a floating tag (`model-image-floating-tag`)

```bash
$WL | jq -r --arg sys "$SYS" --arg mdl "$MDL" "$PRE"'
 | . as $w | (.spec.containers//[]) + (.spec.initContainers//[]) | .[] | (.image//"") as $img
 | select($img != "" and ($img|test("@sha256:")|not)
      and (($img|test(":(latest|main|master|dev|nightly|stable)$")) or ($img|test(":[^/]*$")|not)))
 | "\($w.ns)/\($w.kind)/\($w.name) \($img)"'
```

- **Flag when:** an AI workload's container or init container image carries no digest and either ends in a mutable tag (`:latest`, `:main`, `:master`, `:dev`, `:nightly`, `:stable`) or has no tag at all. Init containers count for the same reason they do in 3.2 and 3.4: the weights-fetch step is the container most often left on `:latest`, and it is the one that decides what the server then loads.
- **`:[^/]*$` is a tag test, not a colon test, and the `[^/]` is what makes it one.** A tag cannot contain `/`, so a colon followed by no slash before the end of the string is a tag and nothing else is. That is what keeps a registry port out of the answer: `gcr.io:5000/i` has a colon and no tag, the `5000/i` after it contains a `/`, the test fails and the image is correctly flagged as untagged — the same verdict as bare `gcr.io/p/i`. `gcr.io:5000/i:v1` matches on its trailing `:v1` and is correctly left alone.
- **Do NOT flag:** standard exclusions; any image pinned by digest (`@sha256:…`), whatever tag it also carries; a version-like tag (`:24.05-py3`, `:v0.6.2`) — conventionally immutable, and treating every tag as floating would flag most of the fleet and make the check worthless.
- **Severity:** `minor`. Nothing is currently wrong; the workload is simply not reproducible, and the next pod restart may serve a different inference runtime than the one that was reviewed.
- **Impact:** "The image this container runs is not reproducible: a restart can pull different bytes than the ones running now, with no manifest change to review."
- **Remediation:** `kind: manifest` (subject to §4's declaration rule) **only when the digest can be read from the live cluster** — take it from a live Pod of that workload — `kubectl get pod -n <ns> -l <the workload's pod labels> -o jsonpath='{.items[0].status..imageID}'`, the one read in this audit that goes to a Pod rather than a template, and only ever to copy a digest the cluster has already resolved — and rewrite the declaration to `<repo>@sha256:<digest>`. That is a fact read off the running object, not an invented value. The recursive `..` in that jsonpath is deliberate: an init container's resolved digest lives in `initContainerStatuses`, so `containerStatuses` alone leaves a finding on a fetcher image unpinnable and silently degrades it to `manual`. If no Pod is running, or `imageID` carries no digest, the finding is `kind: manual` naming the digest the owner must resolve. **Never guess a digest and never substitute a tag you did not read from the cluster.**

**Dropped deliberately.** Prompt-injection and jailbreak resistance, output filtering, model licence compliance, training-data provenance, GPU driver CVEs, and inference-latency anomalies are all real AI-security concerns and none of them are decidable from a `kubectl` read; a check that guessed would publish an unfalsifiable finding. Model Armor and Cloud Armor coverage are deliberately out of scope: verifying either requires API calls this SOP's data-source rule forbids, and the obvious `gcloud` probe for Model Armor mutates the caller's gcloud configuration, which an unattended daily job must never do. Accelerator cost and idle GPU capacity belong to the Fleet Waste Audit; privileged containers, host namespaces, RBAC, and NetworkPolicy on AI workloads belong to the Security & RBAC Posture Audit, which already audits them there.

### 4. Generate remediation artifacts

- **The declaration rule decides where the file goes, and every branch of it discovers a directory that is already there.** All three of this audit's manifest remediations (3.2, 3.3, 3.6) _change an object that already exists_, so each must go to that object's **existing declaration in the GitOps repo**: locate it (`grep -rl "name: <workload-name>" --include='*.yaml' .` — the bare object name, not the `Kind/name` form the `object` field carries), give that file's repo-relative path as `remediation.path`, and rewrite it as the object's complete desired manifest. Never emit a patch fragment. A file carrying `metadata.name` and a partial `spec` is not valid `kubectl apply` input, and a second file claiming an object the repo already declares is a duplicate resource id that both Config Sync and Argo reject.
- **Narrow by namespace first** (`grep -rl "namespace: <namespace>" --include='*.yaml' .`), then **open the hits and confirm one actually declares that workload on the target cluster.** Do not anchor on `grep "name: <workload>"` alone — `grep` is kind-blind and unanchored, so it also matches `app.kubernetes.io/name:` label lines and any object whose name merely starts with the workload's, and it will happily return a file under another cluster's directory. **If the hits land in more than one directory, or none can be tied to the target cluster, the finding is `kind: manual`.**
- **This audit's workloads are the ones most likely to be Helm-templated, and a templated workload has no declaration to rewrite.** An inference stack installed from a chart appears in the clone as `values.yaml` plus a chart reference, or does not appear at all. Neither is a manifest this audit can rewrite: editing a rendered template is meaningless because the next render overwrites it, and editing `values.yaml` means guessing at a chart's parameter names. **When the workload's declaration is a Helm chart or is absent, the finding is `kind: manual`** — put the exact change in `recommendation.action`, naming the field as it appears in the live object, and say in the recommendation that the change has to be made wherever the chart's values are maintained. Do not write a file. This is expected to be the common outcome on this stream, and a `manual` finding with a precise action is a good finding; a manifest written into a path nothing applies is a fix that merges, closes the finding for one run, and leaves the model exactly as exposed as it was.
- **Never create a new top-level directory, and never write to a path whose parent directory does not already exist in the clone.**
- The path is POSIX and relative to the `workspace` clone from Step 0: no leading `/` or `:`, no `..`, and no glob metacharacters (`*`, `?`, `[`, `]`). The helper rejects all of those outright. A path with no file behind it is handled more gently but is still your mistake: that finding degrades to `kind: manual`, keeps its evidence and recommendation, and the ledger records that the audit named the fix without writing it.
- Write the manifest for every id in `pending_remediation_requests` from Step 0 whose finding still reproduces. A human has already asked for that fix; without the file the promotion fails.
- Head each file with a comment naming the cluster, the check, and the finding id.
- Copy every field you are not changing verbatim from the live object. **Never invent an image digest, a model revision, a credential, or a Secret name** — if the value cannot be read off the live object, the finding is `kind: manual`.
- Writing the file does not open a Pull Request. `finish` opens one automatically only for a `critical` finding whose remediation is a `manifest` and whose branch carries no **live** pull request, at most five per run; the ledger names the ones it withheld. "Live" is the operative word: a PR the harness closed itself as stale is labelled `audit:stale-closed` and the same fix may be promoted again, while a PR a human closed and a PR that merged both stay closed. Everything else waits for a repo writer to comment `/remediate <finding-id>` or `/remediate all` on the ledger issue. **In this audit only 3.2 produces a `critical` manifest**, so a `--trust-remote-code` flag on a workload the repo already declares is the one fix that arrives ready to merge; 3.1 is `critical` but `manual` and is never promotable, and 3.3 and 3.6 are manifests but not `critical`, so they wait to be asked for. A `kind: manual` finding is never promotable — it stays prose in the ledger. Findings whose manifest paths intersect share one PR.
- These files are proposals for human review; do not `kubectl apply` anything, ever.

### 5. Emit findings.json

Write the schema exactly as the helper validates it to the `findings_path` returned in Step 0: `audit` set to `ai-security-audit`; a top-level `findings` array, required even when empty (`[]` on a clean run); `scope.clusters` non-empty, each entry carrying the mandatory `checks_run` list of `{check, command}` objects for the §3 checks that actually ran there, optionally `checks_not_applicable`, and optionally a non-empty `limitations` string; `scope.skipped` complete and disjoint from `scope.clusters`; and, for each finding, `check` (the §3 slug that produced it), `severity`, `title`, `cluster`, `namespace`, `object` (as `Kind/name`), `evidence.command` (the literal confirm command you ran, variables expanded) and `evidence.excerpt` (trimmed to the few lines that prove the finding, and never carrying a credential), `impact`, `recommendation`, and `remediation` — with `remediation.path` present and the file on disk whenever `kind == "manifest"`. No `id`: the harness derives it (§2) from `check`, `cluster`, `namespace` and `object`. Sort findings by severity (`critical`, `major`, `minor`), then cluster, then namespace. A schema violation publishes nothing: `finish` exits 2 and the ledger is untouched. Validate your own JSON before calling it.

Read your `checks_run` and `checks_not_applicable` lists once more before you write. Padding `checks_run` to six because §3 lists six checks is the one entry in this document that converts a partial audit back into a false all-clear — the harness cannot see the check you skipped, so it takes the list at its word. `checks_not_applicable` is the same lie wearing a different field: it removes checks from the denominator, so a slug parked there because you ran out of turns is a coverage gap the ledger will never show. It is published too — every exclusion and its reason render under _Not applicable_, where a reviewer who knows the cluster can call it. On this stream the honest and common answer is six-of-six run against a dump that held no AI workload; the dishonest one is six-of-six run on a cluster you never dumped.

**`recommendation` is required on every finding.** Three sub-fields, all non-empty strings, no exceptions — a `manual` finding that will never become a PR needs it exactly as much as a promotable one. Most of this stream's findings are `manual`, so this field is where most of its value lives.

- `action` — what to do, imperative, one or two sentences.
- `rationale` — why this fix and not the obvious alternative; name the alternative you considered and why you rejected it.
- `risk` — what breaks on apply, and the read-only check to run first.

Worked example, for a 3.2 finding on `serving/vllm-llama`:

```json
"recommendation": {
  "action": "Remove the --trust-remote-code flag from the vllm-llama container's args in its existing declaration, changing nothing else.",
  "rationale": "The flag makes the loader execute Python shipped inside the model repository with this pod's ServiceAccount and network access. The obvious alternative, leaving the flag and restricting the pod instead, is rejected: it treats a code-execution primitive as a blast-radius problem, and the model this workload serves does not require custom modelling code. A model that genuinely does should have that code reviewed and vendored into the image.",
  "risk": "A model whose repository does define custom modelling code will fail to load and the pod will crash-loop on the next restart. Confirm the model loads without it first with kubectl --context prod-us-east logs -n serving deploy/vllm-llama --tail=50, which prints the loader's module resolution at start-up."
}
```

### 6. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit ai-security-audit \
  --findings-file /opt/data/scratch/findings_ai-security-audit.json
```

One JSON line comes back, carrying `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, and `silent_ok`. Exit 2 means the validator rejected the document and nothing was published — fix the document, do not retry blind. Exit 1 is fatal. Exit 0 means it published.

`partial` is `true` when the run could not read the whole fleet: any cluster in `scope.skipped`, or any cluster kept in scope with a `limitations` note. `coverage_gaps` names each one in a sentence. The harness then refuses to draw conclusions from silence: `resolved` comes back `0`, no resolved-delta is posted, no remediation PR is retired as stale, and the ledger issue stays open even at zero findings. A check declared in `checks_not_applicable` is not a gap and does not raise the flag; a model-free cluster needs neither field, because its six checks ran and matched nothing.

**`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling that from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → your entire final response is exactly `[SILENT]`. Otherwise report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** If a person dispatched this job — from a kanban card or straight from chat — someone is waiting on the answer, and `[SILENT]` throws it away. Report the outcome and the ledger URL whatever `silent_ok` says.

What to report in each case:

- `silent_ok: true` — `[SILENT]` on a scheduled run, nothing else and no preamble. On `CLEAN` the ledger issue closed as completed and every open remediation PR for this stream closed with it; on `UPDATED` the ledger was rewritten but nothing moved. Dispatched on demand, say which in one line and give the issue URL.
- `status: "CLEAN"` with `resolved: > 0` — every AI-workload exposure this ledger tracked has been closed. Report the issue URL and the count.
- `status: "CLEAN"` with `partial: true` — nothing reproduced, but the ledger and its PRs stayed open because the coverage was incomplete. One line, the clean result plus the `coverage_gaps`, then stop.
- Any other outcome — reply with **one line**: counts by severity, new vs. resolved, skipped-cluster count if any, remediation PRs opened or closed, and the `issue_url`. Example: `AI Workload Security Audit: 1 critical, 3 major, 2 minor across 2 of 7 clusters (2 new, 1 resolved, 1 remediation PR opened) — <issue_url>`. Say how many clusters actually ran AI checks, not just how many were read: on this stream those are usually very different numbers, and a reader who does not know that will misjudge the fleet.

## Red Lines

- **Read-only against every cluster.** No `apply`, `patch`, `edit`, `delete`, `scale`, `drain`, or `cordon`. **No inference traffic, ever** — never send a prompt, a health probe, or any request to a model endpoint to test whether it is authenticated. Reachability is judged from the Service spec, never by connecting.
- **No hand-written issue or PR bodies, and no direct git/gh calls.** `audit_report.py` owns the ledger issue, the remediation branches, the commits, and every body it renders. Never open a second ledger issue for this stream, never open a remediation PR for a non-`manifest` finding, and never reopen a merged one.
- **No credentials in evidence, and no credential ever written to a file.** A Secret's `data:` block, a ServiceAccount token, a Hugging Face or model-registry token, or a private key never enters an excerpt, a title, a recommendation, or a generated manifest. Check 3.5 reports the _name_ of a plaintext credential variable and never its value, and its remediation never emits a Secret — writing one would commit the credential to Git. The rule covers the shapes that do not announce themselves as credentials too, because the ledger is a public issue: the `user:pass@` inside a model URL, the value of an `--api-key` or `--hf-token` argument sitting beside the argument being quoted, and the external address of the endpoint 3.1 has just labelled internet-reachable.
- **An empty result is a result only when the command that produced it exited `0`.** A dump a failed `kubectl` left at zero bytes, a `jq` that aborted mid-stream, a `--argjson` handed an empty variable — every one of them prints nothing, and a check that reads nothing as clean publishes a false all-clear, which is the worst thing this audit can do. The dump gates and the `$?` recipe in §2 are not optional and their absence is not visible in the output.
- **No commands that mutate the caller's environment.** An unattended daily job must not run `gcloud config set`, change an API endpoint override, or alter the active configuration to reach a service. If a control cannot be verified with a plain read, it is out of scope — see the dropped list in §3.
- **A finding you cannot reproduce is dropped, not softened.** `evidence.command` is the literal command you executed, with every shell variable expanded; if the confirm read fails or the condition has cleared, the finding does not ship.
- **No fabricated values.** Image digests, model revisions, credential names, and Secret references are either read off the live object or left to a human.
- **No bare generator inside an `or`.** `false or (empty)` is `empty` in `jq`, so a workload that fails the left side of a two-pronged test disappears from the pipeline instead of being rejected — silently unaudited, and indistinguishable in the output from a workload that passed. Every disjunct in §2 and §3 yields exactly one boolean.
- **No scope creep into the other streams.** Generic container hardening, RBAC, NetworkPolicy, and Workload Identity are the Security & RBAC Posture Audit's; accelerator cost is the Fleet Waste Audit's. One object, one verdict, one ledger.
- **Stable ids or the delta lies.** An unstable id — one that varies between runs because the `object` it is derived from moved — turns one persistent problem into an infinite stream of "new" findings. Never let an image tag, a model revision, or a pod name into `object`.
