---
name: chat-delivery
description: Answer why alerts or scheduled reports do not reach Slack or Google Chat while replies in a thread do, what the PlatformAgent's HomeChannelUnset condition means, and how an operator sets or moves the home channel. Never a test post to check the routing, never an edit of the PlatformAgent, never a pull request for it.
---

# chat-delivery — Where Chat Posts Go and Why Some Do Not

When `A2A_NOTIFY_CONVERSATIONS` is in your environment, the A2A gateway holds the chat backend
and two routes carry what reaches chat. Without that variable the install posts through Hermes
and this skill does not apply.

## Two routes

- **Conversation route, no home channel needed.** A card's report back into the thread or
  message it was filed from (the kanban notifier's post), and onboarding's first report into the
  chat where the first person wrote.
- **Home channel.** An alert, a scheduled report, and every `send_notification`; a `session_id`
  makes it a reply in a thread of the home channel, not a post elsewhere. `A2A_NOTIFY_PLATFORM`
  names the platforms whose `homeChannel` the `PlatformAgent` sets, and a file under
  `/etc/kube-agents/chat-home/` names the home the operator admitted: set on the `PlatformAgent`,
  learned from a mention, or picked on Slack's Home tab.
- **No home channel** is `A2A_NOTIFY_CONVERSATIONS` set with `A2A_NOTIFY_PLATFORM` empty and
  those files empty. The `PlatformAgent` reports it as the `HomeChannelUnset` condition. Replies
  in the thread that asked still arrive. The gateway records the posts it could not place and,
  once a home channel exists, posts one notice naming how many there were and their last
  subjects; the posts themselves are not delivered.

## "Why do alerts or reports not arrive?"

- Say the above in words. Do not probe: no `send_notification` to test the routing, no live
  check of the gateway.
- Name the install's settings (below), never your environment variables, the pod's files, or a
  command.

## "Set (or move) the home channel"

Give the operator these ways and change nothing yourself: the `PlatformAgent` in
`kubeagents-system` is theirs to edit, and no pull request of yours reaches it. Name each as a
setting, in words: no `kubectl` command and no manifest.

To set a home channel where none is configured:

- Mention the agent once, top-level, in the Slack channel or Chat space it should use (home-channel
  policy `auto`, the default; a direct message or a thread reply never counts). A mention does not
  move a home channel that is already set and still works.
- On Slack, an allowed member can pick it on the app's Home tab. The picker shows only while no
  `homeChannel` is set on the `PlatformAgent` and the policy is `auto`; it also moves a home the
  gateway learned.

To set one, or move one that is already set:

- Set `SLACK_HOME_CHANNEL` (the channel id) or `GOOGLE_CHAT_HOME_CHANNEL` (`spaces/<id>`) in
  `install.env` and run `upgrade.sh`.
- Set `spec.integration.slack.homeChannel` or `spec.integration.googleChat.homeChannel` on the
  `PlatformAgent`. A home channel set this way moves only by changing this setting.

`/sethome` does not set it on an install with the gateway.
