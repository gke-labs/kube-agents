# Recorded GitLab API responses, one file per collaboration verb

The same contract `test_providers_contract.py` holds every forge to, and the same file shape as `../github/README.md` describes: `payload` is the request the broker receives, and `responses` are the API answers in the order the forge asks for them. `{"__status__": N}` is a recorded refusal.

`config.json` is what this forge is built from for the contract. GitLab is configured per host and builds nothing from an empty configuration, so it ships the entry the registry would hand `for_config`: gitlab.com, a token path, and an explicit empty `allowed_paths`: the whole host, which GitLab is only given when asked for.

## Provenance

Recorded against gitlab.com's v4 API from a private throwaway project, below the transport, so no token or `PRIVATE-TOKEN` header was ever recorded. Then edited three ways:

- **Trimmed.** A merge request answers with about sixty fields. What's left is what the translation reads plus neighbours it should _not_ read (`id` beside `iid`, `merge_commit_sha` beside `sha`, `closed_at` beside `merged_at`), so a translation that reached for the wrong one has something to get wrong.
- **Redacted.** Every username, name, email, avatar URL, project path and numeric id is replaced. The project is `acme/infra`; its encoded form `acme%2Finfra` is what every path carries.
- **Composed.**
  - `proposal-list.json` carries three merge requests in the three states the contract asserts: open, merged, and closed and draft.
  - `proposal-view.json`'s notes carry a conversation note, a system note (GitLab's own bookkeeping, which the forge drops) and a diff note, so both neutral comment kinds appear.
  - `issue-view.json` carries a system note too.
  - `proposal-update.json` opens with the read a re-title makes first (the same merge request under its earlier title, not a draft), so the update is sent as written.
  - These are arrangements of real responses, not invented ones.

GitLab specifics the files pin:

- issue `web_url`s are `/-/work_items/N`;
- a draft merge request's title carries the `Draft:` prefix;
- label colours carry a `#`;
- a merge request's commits arrive newest first.
