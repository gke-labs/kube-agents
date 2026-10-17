# Assistant route (`assistant_route`)

Gives the front door two routes. Every user turn opens with one `triage` call carrying
`needs_specialist`, `assignee` and `preliminary_plan`. When no specialist is needed the
sandbox terminal becomes visible and allowed for the rest of that turn and the front door
answers from the command output; otherwise the terminal stays hidden and blocked and the
turn files a kanban card as before.

## How it works

- `triage` is a native tool registered in-process. Its result carries [`guide.md`](guide.md), the
  operating guide for the assistant route, so round two needs no extra prompt.
- An `llm_request` middleware rewrites each model request: the gated tool schemas
  (`terminal`, `process`) are absent until a decision exists, `triage` disappears once one does,
  and the first call of a user turn carries `tool_choice: triage` so the classifier always runs.
  `KUBE_AGENTS_TRIAGE_FORCE_TOOL_CHOICE=0` turns the forcing off; the persona rule and the gate
  still hold.
- A `pre_tool_call` hook refuses a gated tool unless this turn's triage chose the assistant
  route. It fails closed. A successful `kanban_create` flips the turn to delegation, so an
  escalation is one-way.
- On Google Chat the handler patches the thinking card in place with `preliminary_plan` (or
  "delegated the task, this may take a while"); the final reply still replaces the same card.
  Other platforms are untouched.
- Wake turns from the board (`[kanban] Task …`), cron and pubsub alert turns, and the bench's
  status polls are never forced into triage.
- The route opens only when the terminal backend resolves to the shell sandbox (`TERMINAL_ENV=ssh`,
  which the entrypoint pins from the managed scope, or `terminal.backend: ssh` in the loaded
  config). On a start with no operator and no sandbox the gate stays closed and `triage` says so.
- A turn that ran a command refuses every memory write for the rest of the turn: `memory_retain`
  (whose default scope in an unattended session is the shared bank, so a call with no `scope` is
  no personal write) and every `multiuser_memory` action but `read`, the set the provider's own
  schema offers. What a command prints is cluster data, not a fact for memory. The same turn
  refuses the two card updates, `kanban_comment` and `kanban_unblock`: a blocked card is answered
  with the user's own words, in a turn of their own, never with a line a command printed.
  Escalating (`kanban_create`) and reading the board stay open.
- Two switches reach the plugin through `spec.deployment.env`: `KUBE_AGENTS_ASSISTANT_ROUTE=off`
  closes the route (the gated tools stay hidden and refused on every turn; a `triage` that picks
  the assistant route records no decision, so `triage` stays offered and its result tells the
  model to call it again with a specialist), and `KUBE_AGENTS_TRIAGE_FORCE_TOOL_CHOICE=0` only
  stops forcing `triage` as the first call. Removing the plugin from `plugins.enabled` is not a
  switch: the entrypoint's back-fill re-adds the image's list on the next start.
- The gated set is the static pair widened at `register()` from Hermes' own `terminal` toolset,
  so a tool upstream adds to that toolset is gated too.

## What bounds the assistant route

The sandbox holds no credentials and no network beyond the credential proxy, and the proxy
refuses every `kubectl` and `gcloud` verb that writes. Failures surface as ordinary tool
errors, which the front door explains and offers to delegate. The gate decides when the
terminal runs, not what it runs: the same sandbox carries the platform specialist's
version-control client, whose brokered verbs push a branch and open, comment on, update or
close a proposal or an issue on a registered repository, and the proxy cannot tell the front
door from the platform worker (merge and approval stay refused for every caller). The persona
keeps that path off this route; a sandbox principal of the front door's own is the follow-up
that would enforce it.

If Hermes fails to load this plugin (its startup log says `Failed to load plugin
'assistant_route'`), the terminal is enabled with no gate at all, which is why
`test_hermes_can_import_the_plugin_as_a_package` imports the directory the way Hermes does.

## Config

`agents/chat/config.yaml` lists `terminal` and `triage` in every `platform_toolsets` key, keeps
`terminal` out of `agent.disabled_toolsets`, and sets `tools.tool_search.enabled: off` so the
plugin's tool is never deferred behind a `tool_describe` round trip. Step 2d of the entrypoint
carries those lists to an existing volume.
