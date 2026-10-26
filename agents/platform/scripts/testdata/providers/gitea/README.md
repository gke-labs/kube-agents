# Recorded Gitea API responses, one file per collaboration verb

The same contract `test_providers_contract.py` holds every forge to, and the same file shape as `../github/README.md` describes: `payload` is the request the broker receives, and `responses` are the API answers in the order the forge asks for them. `{"__status__": N}` is a recorded refusal.

`config.json` is what this forge is built from for the contract. Gitea is configured per host and builds nothing from an empty configuration, so it ships the entry the registry would hand `for_config`: `gitea.example.com`, a `scheme` (`https`), a token path, and an explicit empty `allowed_paths`.

## Provenance

Recorded against Gitea's `/api/v1` REST surface from a throwaway repository, below the transport, so no token or `Authorization` header was ever recorded. Then edited three ways:

- **Trimmed.** Pull request and issue payloads are trimmed to the fields the translation reads plus neighbours it should _not_ read (`id` beside `number`, `merge_commit_sha` beside `head.sha`), so a translation that reached for the wrong one has something to get wrong.
- **Redacted.** Every login, display name, host, repository owner/name and numeric id is replaced. The repository is `acme/infra` on `gitea.example.com`.
- **Composed.**
  - `proposal-list.json` carries three pull requests in the three states the contract asserts: open, merged (`state: "closed"` with `merged: true`), and closed and draft (`draft: true` / `WIP:` prefix).
  - `proposal-view.json` combines the pull request, its issue comments, its reviews and review comments, and its commits into the neutral `ProposalDetail` view.
  - `proposal-update.json` opens with the `GET` a title update makes first so a draft pull request keeps its `WIP:` prefix unless `draft` is explicitly cleared.
