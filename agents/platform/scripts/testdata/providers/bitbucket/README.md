# Bitbucket Cloud API responses, one file per collaboration verb

The same contract `test_providers_contract.py` holds every forge to, and the same file shape as `../github/README.md` describes: `payload` is the request the broker receives, and `responses` are the API answers in the order the forge asks for them.

`config.json` is what this forge is built from for the contract: bitbucket.org, a token path, and an explicit empty `allowed_paths`.

## Provenance

These files are recordings. They are the answers Bitbucket Cloud (bitbucket.org) gave to the broker's own provider code on 2026-11-02, during the live check of pull request #3687 on the test cluster `ka-bb-pr2`, recorded by wrapping the forge's HTTP transport and saving each answer in order. Nothing was added by hand.

They were anonymized before they were committed:

- the accounts are `example-maintainer` (the person who approves, comments and declines) and the install's own account, with placeholder UUIDs, account ids, names and e-mail addresses;
- the workspace, project, host and branch names became `acme` and `bitbucket.acme.example`;
- avatar links are a placeholder;
- the install's git identity is the contract's `platform-agent <platform-agent@kube-agents.invalid>`, as the other forges' recordings show it.

No token, cookie or header that carries a credential is in a recording.

What the live answers settled:

- The access token's bot user is `type: app_user`, with `kind: repository_access_token`, and Bitbucket links a commit pushed with it to that user.
- `q` and `state` together: Bitbucket ignores `state` when `q` is given, so the forge names its states in `q` (the tally recording holds the three counts that way).
- A squash merge leaves one commit on the pull request, authored by the account that merged.
- The page size of 50 for pull requests and newest-first commits are not checked here.
