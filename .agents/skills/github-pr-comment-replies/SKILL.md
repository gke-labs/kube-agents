---
name: github-pr-comment-replies
description: Drafts pull request review comment replies in a casual, succinct SRE/developer-focused voice.
---

# Drafting PR Review Comment Replies

Use this skill when drafting replies to pull request review comments on GitHub.

## Guidelines

1. **Be Succinct and Direct:** Jump straight to the technical resolution or rationale. Avoid opening boilerplate like "I appreciate the suggestion", "Good catch", or "Thank you".
2. **Use a Casual SRE/Developer Voice:** Write in a natural, collaborative peer voice (e.g., "Makes sense, but...", "Actually...", "We need to...", "Switched to...").
3. **Keep Explanations Technical and Grounded:** Focus on the concrete constraints, endpoints, database fields, or runtime variables. Use short bullet points to break down complex multi-step reasons.
4. **Indicate Fix Action clearly:** If a change was implemented, explicitly state what was modified and its status (e.g. "Fixed. Switched to...").

## Examples

### Example 1: Explaining design constraints (FastAPI vs Webhook)
* **Bad:** "Thank you for the comment. I appreciate your feedback. However, we cannot use stateless webhooks because we need to register dynamic thread mappings to enable reply routing..."
* **Good:**
  > Makes sense, but stateless webhooks don't support the dynamic threading and deduplication flow we need here:
  > 
  > 1. **Alert Threading:** We have to post the warning to Chat first, grab the returned `message_id`, and register it in `state.db` on the fly so replies route back to the same session.
  > 2. **Deduplication:** When repeat warnings occur, the watcher queries and calls `/inject` to append counts into the same thread rather than spawning duplicate reasoning runs.
  > 
  > So `session_kv_server.py` acts as that stateful bridge.

### Example 2: Explaining DB choice
* **Bad:** "Yes, you are correct. However, we choose state.db because it is an existing table that Hermes uses..."
* **Good:**
  > Yes, `/opt/data/state.db` is the database the core Hermes gateway runner actively queries to route incoming chat webhook replies back to active sessions. If we register the routing in our own custom DB, the gateway runner won't see it and thread replies won't resolve.
