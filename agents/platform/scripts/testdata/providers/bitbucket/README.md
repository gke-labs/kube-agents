# Bitbucket Cloud API responses, one file per collaboration verb

The same contract `test_providers_contract.py` holds every forge to, and the same file shape as `../github/README.md` describes: `payload` is the request the broker receives, and `responses` are the API answers in the order the forge asks for them.

`config.json` is what this forge is built from for the contract: bitbucket.org, a token path, and an explicit empty `allowed_paths`.

## Provenance

**These files are not recordings.** They are written by hand from Atlassian's published Bitbucket Cloud API specification (`swagger.v3.json`), because no Bitbucket workspace was available when the package was written. So they hold what the author read in the specification, not what bitbucket.org answers. They are to be replaced with responses recorded from a live workspace, trimmed and redacted as `../gitlab/README.md` describes.

Known defects of the hand-written data: a commit is listed as its own parent, and one hash carries two different dates and messages across files.

Fields not yet checked against a live response:

- the `app_user` account type of an access-token bot, which the `bot` flag of a comment reads;
- the `inline` object of a comment on a line (`path`, `from`, `to`);
- the order of `pullrequests/{id}/commits` (newest first is assumed);
- the 12-character `source.commit.hash` of a pull request;
- the page size of 50 for pull requests.
