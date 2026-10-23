# Bitbucket Data Center API responses, one file per collaboration verb

The same contract `test_providers_contract.py` holds every forge to, and the same file shape as `../github/README.md` describes: `payload` is the request the broker receives, and `responses` are the API answers in the order the forge asks for them.

`config.json` is what this forge is built from for the contract: a Data Center host, a token path, and an explicit empty `allowed_paths`.

## Provenance

**These files are not recordings.** They are written by hand from Atlassian's published Bitbucket Data Center REST specification (`10.2.swagger.v3.json`), because no Data Center instance was available when the package was written. So they hold what the author read in the specification, not what an instance answers. They are to be replaced with responses recorded from a live instance, trimmed and redacted as `../gitlab/README.md` describes.

Known defects of the hand-written data: a commit is listed as its own parent; pull request 6 in `proposal-list.json` has its author as its only reviewer; the declined pull request in `proposal-close.json` has a `closedDate` later than its `updatedDate`.

Fields not yet checked against a live response:

- where an activity carries the anchor of a comment on a line (`comment.anchor` or `commentAnchor`);
- `closedDate` on a merged or declined pull request;
- the `SERVICE` user type of the account a project or repository token acts as;
- the `draft` field (Data Center 8.18 and later).
