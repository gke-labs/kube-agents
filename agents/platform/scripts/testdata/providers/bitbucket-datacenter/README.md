# Bitbucket Data Center API responses, one file per collaboration verb

The same contract `test_providers_contract.py` holds every forge to, and the same file shape as `../github/README.md` describes: `payload` is the request the broker receives, and `responses` are the API answers in the order the forge asks for them.

`config.json` is what this forge is built from for the contract: a Data Center host, a token path, and an explicit empty `allowed_paths`.

## Provenance

These files are recordings. They are the answers Bitbucket Data Center 8.19 (the `atlassian/bitbucket` image, a 3-hour timebomb test licence) gave to the broker's own provider code on 2026-11-02, during the live check of pull request #3687 on the test cluster `ka-bb-pr2`, recorded by wrapping the forge's HTTP transport and saving each answer in order. Nothing was added by hand.

They were anonymized before they were committed:

- the accounts are `example-maintainer` (the person who approves, comments and declines) and the install's own account, with placeholder UUIDs, account ids, names and e-mail addresses;
- the workspace, project, host and branch names became `acme` and `bitbucket.acme.example`;
- avatar links are a placeholder;
- the install's git identity is the contract's `platform-agent <platform-agent@kube-agents.invalid>`, as the other forges' recordings show it.

No token, cookie or header that carries a credential is in a recording.

What the live answers settled:

- A project HTTP access token acts as `access-token-user/<project>/<token>` (`type: SERVICE`), which every answer names in `X-AUSERNAME` and the `whoami` servlet does not answer.
- The participant filter `role.1=AUTHOR&username.1=` finds no proposal of that user, so the forge filters the author itself.
- A comment on a line carries its anchor in `commentAnchor` on the activity.
- `closedDate` is set on a declined pull request.
- The token user has no e-mail address, so it cannot merge; the merged pull requests were merged by the admin account.
