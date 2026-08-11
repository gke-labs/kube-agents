# Legacy `/hermes` Command Rewrite (`legacy_slash_commands`)

A one-hook plugin on the `default` (Chat Agent) profile that turns a **typed**
`/hermes <subcommand>` message into the real gateway command before the gateway
resolves it. Without it, `/hermes sethome` — the command Hermes itself tells the
user to run — does nothing.

## The failure it fixes

Slack routes a leading-slash string to the app's slash-command handler **only if
that slash is registered on the Slack app**. Our provisioning flow creates the
app from tokens alone, so nothing is registered, and Slack delivers
`/hermes sethome` as an ordinary channel message. From there:

1. `gateway/run.py` parses the message text and reads the command name as
   `hermes`.
2. `hermes` is not in `COMMAND_REGISTRY` (the registry holds `sethome`,
   `model`, `compress`, … — never `hermes`), so the gateway logs
   `Unrecognized slash command /hermes from slack` and replies "Unknown command
   `/hermes`".
3. `_handle_set_home_command` never runs, no `SLACK_HOME_CHANNEL` is written,
   and the "📬 No home channel is set" prompt fires again on the next session.

Observed in this deployment on 2026-08-02: the unknown-command reply at
`15:54:11`, and the user's follow-up in the same thread reaching the Chat Agent
as free text — which it then filed as a kanban task to `platform`, whose worker
wrote `SLACK_HOME_CHANNEL` into the **platform** profile's `.env`. Slack ingress
runs on the **default** profile, so the setting had no effect there.

## What it does

Hermes' Slack adapter already unwraps this form: `_handle_slash_command` maps
`/hermes <sub> [args]` through `slack_subcommand_map()` before dispatch, so a
_registered_ `/hermes` slash works. The plugin performs the same unwrapping one
layer earlier, on the inbound `MessageEvent`, so both paths behave identically:

| Typed message              | Dispatched as             |
| -------------------------- | ------------------------- |
| `/hermes sethome`          | `/sethome`                |
| `/hermes model gpt-5`      | `/model gpt-5`            |
| `/hermes compact`          | `/compress`               |
| `/hermes`                  | `/help`                   |
| `/hermes what's deployed?` | `what's deployed?` (text) |
| `/sethome`, `hello`, …     | unchanged                 |

The unknown-subcommand row matters: the prefix is **stripped** rather than
passed through, because passing it through is exactly what produces the
unknown-command reply. That matches upstream, which documents `/hermes <free
text>` as a way to ask a question through a single slash entry point.

## How it is wired

`pre_gateway_dispatch` fires once per inbound user message, after the
internal-event guard and **before** auth and command resolution
(`gateway/run.py`), and a hook may return `{"action": "rewrite", "text": ...}`
to replace `event.text`. Command resolution reads `event.text` afterwards
(`MessageEvent.get_command()`), so the rewrite lands in time.

The plugin must be enabled on the profile that receives chat ingress — the
`default` profile. It is listed in `plugins.enabled` in **both**
`agents/chat/config.yaml` and the operator's `renderConfigYAML`
(`k8s-operator/internal/controller/platformagent_manifests.go`); the operator's
copy is authoritative on the deployed default profile, so a change to one
without the other is a no-op.

## Maintenance rules

- **Keep the vocabulary sourced from `slack_subcommand_map()`.** It is generated
  from `COMMAND_REGISTRY`, so a command added or renamed upstream is picked up
  for free. Do not hardcode a command list here.
- **Never fail the turn.** The hook runs before auth on every inbound message;
  any exception is caught and returns `None` so a bug here degrades to today's
  behaviour rather than dropping messages.
- **Anchored match only.** `/hermes` must start the message (after an optional
  leading bot mention). Rewriting a mid-sentence mention would mangle ordinary
  prose that happens to quote the command.

## Registering the native slash commands (the other half)

This plugin makes the typed form work; it does not give the user Slack's
autocomplete. For that, register the slashes on the Slack app:

```bash
hermes slack manifest > slack-manifest.json
```

then paste the JSON into the Slack app config (Features → App Manifest → Edit)
and reinstall when Slack prompts. `k8s-operator/scripts/provision_06_slack.sh`
points at this step. With the slashes registered, `_handle_slash_command`
handles `/hermes sethome` and this plugin sees `/sethome` already and passes it
through untouched.

## Tests

`test_plugin.py` covers the rewrite table above and the hook contract (patching
`_subcommand_map`, since `hermes_cli` is not importable outside the image).
Run from the repository root:

```bash
python3 -m unittest agents/chat/defaults/plugins/legacy_slash_commands/test_plugin.py
```
