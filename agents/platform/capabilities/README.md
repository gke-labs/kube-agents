# Capability criteria templates

One directory per capability on the delivery vehicle, named after the cron job and audit stream it
belongs to. The Dockerfile bakes this tree into `/opt/platform-template/capabilities/` and the
entrypoint seeds it into the Platform Agent's profile at `/opt/data/profiles/platform/capabilities/`,
where the agent reads and edits it through the `capability_criteria` tool on the `platform_control`
MCP server (`agents/platform/scripts/capability_store.py` is the implementation).

Each directory holds:

| File                   | Owner                   | Across a pod start                                                 |
| ---------------------- | ----------------------- | ------------------------------------------------------------------ |
| `criteria.json`        | Operator, via the agent | Volume wins every key it holds; the image adds keys it newly ships |
| `criteria.schema.json` | Image                   | Replaced                                                           |
| `learning.json`        | Image                   | Replaced                                                           |
| `changelog.jsonl`      | The store               | Not in the template, so never touched                              |

`criteria.json` here is the shipped default and must validate against the schema beside it —
`test_capability_store.py` checks every directory. Keep the defaults quiet: a capability's first
weeks on a fleet are for tuning, and the procedure that reads these values (the governance SOP or
`SKILL.md`) names each key beside the check it drives. A key a release stops shipping stays on the
volume until the next `set`, which prunes it and says so in the changelog; the schema is what
declares it gone.

`learning.json` gives each key one of three policies. `propose` means the agent must show the
operator the before/after and record who agreed before the store accepts the write; `autonomous`
means a reason is enough; `never` means the key changes only through a reviewed change to this
template. It is image-owned so that a release can tighten a policy and know every install got it;
ship everything as `propose` until a capability has earned otherwise.

This tree is not under `skills/` on purpose: the entrypoint replaces `skills/` wholesale on every
start and the shell sandbox carries its own image copy, so nothing editable at runtime can live
there.
