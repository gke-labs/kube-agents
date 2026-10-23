# GKE Support Agent persona

The GKE Support Agent (`tse`) written in the
[persona format](../../../a2a/lib/persona/README.md): a read-only Technical
Solutions Engineer for one GKE cluster, which each task names through the
`project`, `cluster` and `location` parameters. It runs the 4-stage diagnostic
loop and returns a root cause analysis with a proposed patch, or a redacted
Google Cloud Support Escalation Bundle. It is the first persona of the first
non-team adopter of AgentProfiles, for the profile dispatcher (#3655) to run.

Today's install does not run it. The image copies it to
`/opt/tse-template/persona` with the rest of `agents/tse/`, but the optional
profile sync in `deploy/shared/docker-entrypoint.sh` (`OPTIONAL_SYNC_ITEMS`
and the `skills/` tree) leaves it out, so the Hermes `tse` profile still comes
from `agents/tse/` (`SOUL.md`, `AGENTS.md`, `CAPABILITIES.md`, `config.yaml`,
`skills/`). That image-baked path is temporary; it moves to an AgentProfile
under epic #3654.

The profile worker sets `PROFILE_TASK_PROJECT`, `PROFILE_TASK_LOCATION` and
`PROFILE_TASK_CLUSTER` from the task's parameters. The instructions' first step
reads the target from them and stops on an empty value or a mismatch with the
request, then makes one liveness read (`kubectl version -o json`) through the
credential proxy. Credentials come from the proxy under the task's grant; the
agent never fetches or configures them, and passes
`--project "$PROFILE_TASK_PROJECT"` to every `gcloud` call.

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

Left out of the Hermes profile's text, because a profile pod has none of it:
the kanban loop (`kanban_*` calls and `$HERMES_KANBAN_TASK`; the result is the
final answer and milestones are progress lines), the cluster preflight script
and `/opt/data` paths (replaced by the target check above), Hermes tool and
skill-catalog names, the glossary path, `USER.md`, and the Memory section.
Added: the target check, the denial rule, and the skill's redaction rules
stated as a red line.

Check it from `a2a/`:

```sh
go run ./cmd/persona validate --in ../agents/tse/persona
go run ./cmd/persona render --harness claude-code --in ../agents/tse/persona --out "$(mktemp -d)" \
  --relay-url http://credential-proxy:8765/mcp \
  --relay-command 'node /opt/mcp-remote/dist/proxy.js {url} --transport http-only'
```
