export const meta = {
  name: 'verify-phase',
  description: 'Run kube-agents verification suites in parallel and adversarially confirm each result',
  whenToUse: 'After a build phase/task is implemented, to run acceptance + spec Verification suites concurrently and get a per-suite PASS/FAIL with evidence.',
  phases: [
    { title: 'Run suites', detail: 'one agent per verification suite in scope' },
    { title: 'Confirm', detail: 'adversarially re-check each PASS/FAIL (negative tests must truly deny)' },
  ],
}

// args: { phase: number, target: 'kind'|'gke', suites: [{ id, spec, section, target, prompt }] }
// Each suite prompt should tell the agent exactly which checks to run (from harness-verify) and to
// return evidence. Falls back to a default suite list if args.suites is omitted.
const target = (args && args.target) || 'kind'
const phase = (args && args.phase != null) ? args.phase : 'current'

const DEFAULT_SUITES = [
  { id: '03-11-readonly-sar', spec: '03', section: '§11', prompt: 'Run the read-only per-tier SAR checks (kubectl auth can-i ... --as=<agent-sa>): create|update|delete must be "no" for every resource, get|list|watch "yes" only within tier scope. Report each command and result.' },
  { id: '03-11-no-write-tools', spec: '03', section: '§11', prompt: 'Grep the operator-RENDERED config (renderConfigYAML / mounted ConfigMap, not just baked config.yaml): confirm no create_cluster, gke MCP read-only, apply_manifest/delete_cluster_manifest removed.' },
  { id: '03-11-attenuation', spec: '03', section: '§11', prompt: 'Apply a Role/ClusterRole granting an agent SA a write verb (and a cluster-scoped binding to a namespace-tier SA). Confirm the ValidatingAdmissionPolicy REJECTS it at apply time (non-zero apply, denial message).' },
  { id: '03-11-no-breakglass', spec: '03', section: '§11', prompt: 'Attempt a direct kubectl apply / cloud write with an agent identity. Confirm it is forbidden; only a merged PR actuated by CI/CD succeeds.' },
  { id: '05-08-chaos', spec: '05', section: '§8', prompt: 'Failure-isolation chaos: kill hub (spoke workloads keep running, agents pause), kill controller (running pods continue, no new reconciles), kill a Cluster Admin Agent (its Dev Team Agents keep running), confirm controller relaunches killed agent pods.' },
]

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['id', 'result', 'evidence'],
  properties: {
    id: { type: 'string' },
    result: { enum: ['PASS', 'FAIL', 'SKIP'] },
    evidence: { type: 'string', description: 'command(s) + output snippet or PR/commit link' },
    notes: { type: 'string' },
  },
}

const suites = (args && args.suites) || DEFAULT_SUITES
log(`Verifying phase ${phase} on ${target}: ${suites.length} suites`)

// pipeline: each suite runs, then is adversarially confirmed — no barrier between suites.
const results = await pipeline(
  suites,
  (s) => agent(
    `Verification suite ${s.id} (spec ${s.spec} ${s.section}) on target ${s.target || target}.\n${s.prompt}\nReturn PASS only if the check truly held. For a negative test, PASS means the bad action was DENIED.`,
    { label: `verify:${s.id}`, phase: 'Run suites', schema: SCHEMA }
  ),
  (r, s) => r == null
    ? null
    : agent(
        `Adversarially confirm this verification result for suite ${s.id}: ${JSON.stringify(r)}.\n` +
        `A check that silently no-ops must NOT count as PASS. For negative tests, confirm the denial was real (e.g. non-zero apply, explicit policy message), not a malformed manifest. Return the corrected verdict.`,
        { label: `confirm:${s.id}`, phase: 'Confirm', schema: SCHEMA }
      )
)

const clean = results.filter(Boolean)
const failed = clean.filter((r) => r.result === 'FAIL')
log(`Done: ${clean.filter((r) => r.result === 'PASS').length} pass, ${failed.length} fail, ${clean.filter((r) => r.result === 'SKIP').length} skip`)

return { phase, target, results: clean, failed, allGreen: failed.length === 0 }
