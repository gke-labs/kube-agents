# GKE Support Agent persona

The GKE Support Agent (`tse`) written in the
[persona format](../../../a2a/lib/persona/README.md): a read-only Technical
Solutions Engineer for one GKE cluster, which each task names through the
`project`, `cluster` and `location` parameters. It runs the 4-stage diagnostic
loop and returns a root cause analysis with a proposed patch, or a redacted
Google Cloud Support Escalation Bundle. It is the first persona of the first
non-team adopter of AgentProfiles, for the
[profile dispatcher](../../../docs/designs/spec-profiles-dispatch.md) to run.

It ships as its own image, `persona-tse` (`a2a/Dockerfile.persona-tse`, with
this directory under `/files`), and the chart's `profiles.tse.enabled` renders
the `tse` AgentProfile that names it. A profile worker pod mounts the image at
`/persona`.

The Hermes `tse` profile doesn't use it. The platform-agent image copies it to
`/opt/tse-template/persona` with the rest of `agents/tse/`, but the optional
profile sync in `deploy/shared/docker-entrypoint.sh` (`OPTIONAL_SYNC_ITEMS`
and the `skills/` tree) leaves it out, so that profile still comes from
`agents/tse/` (`SOUL.md`, `AGENTS.md`, `CAPABILITIES.md`, `config.yaml`,
`skills/`). The image-baked Hermes copy goes when Hermes does.

The contract with the dispatcher: the dispatcher states the parameters in the
task, and the profile worker sets `PROFILE_TASK_PROJECT`,
`PROFILE_TASK_LOCATION` and `PROFILE_TASK_CLUSTER` from them. The first step reads
the target from them and stops on an empty value or a mismatch with the
request, then makes one liveness read (`kubectl version -o json`) through the
credential proxy. Credentials come from the proxy under the task's grant; the
agent never fetches or configures them, and passes
`--project "$PROFILE_TASK_PROJECT"` to every `gcloud` call.

Under the dispatcher, `tse` takes a cluster target: all three parameters are
required, as for the cluster agent. A question about a whole project (billing,
quota, org policy) isn't supported yet, because a profile task has either a
full cluster target or none and its grant is scoped to one cluster; #5424
tracks project-scoped tasks.

What it holds:

- [`persona.yaml`](persona.yaml): the manifest, with the three parameters and
  the tools it grants (`read_file`, `shell`, `web_search` and the two servers);
- [`instructions.md`](instructions.md): the instructions, from `SOUL.md`,
  `AGENTS.md` and `CAPABILITIES.md`;
- [`mcp.yaml`](mcp.yaml): the `gke` and `developer_knowledge` servers on the
  broker's relay, each limited to its read tools;
- the [`support-escalation-bundle`](skills/support-escalation-bundle/SKILL.md)
  skill: its `agents/tse/skills` source with only the edit
  `a2a/lib/persona/claudecode/tse_persona_test.go` lists (the bundle's
  variables set from the task's target). The same test fails if they drift.
  The platform-agent image's skill manifest does not hash its copy under
  `/opt/tse-template/persona`, because nothing in that image loads it.

`instructions.md` is a rewrite, not a copy, so no test holds it to `SOUL.md`,
`AGENTS.md` and `CAPABILITIES.md`: a rule added there has to be carried here
by hand. The test pins the red lines it states.

Left out of the Hermes profile's text, because a profile pod has none of it:
the kanban loop (`kanban_*` calls and `$HERMES_KANBAN_TASK`; the result is the
final answer and milestones are progress lines), the cluster preflight script
and `/opt/data` paths (replaced by the target check above), Hermes tool and
skill-catalog names, the glossary path, `USER.md`, and the Memory section.
`gcloud monitoring` is narrowed to the reads the broker's command policy
admits (`monitoring policies list` and `describe`, `beta monitoring
metrics-scopes describe`). Added: the target check, the denial rule, and the
skill's redaction rules stated as a red line.

Check it from `a2a/`:

```sh
go run ./cmd/persona validate --in ../agents/tse/persona
go run ./cmd/persona render --harness claude-code --in ../agents/tse/persona --out "$(mktemp -d)" \
  --relay-url http://credential-proxy:8765/mcp \
  --relay-command 'node /opt/mcp-remote/dist/proxy.js {url} --transport http-only'
```
