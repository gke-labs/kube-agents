# External feedback form

A public Google Form that files each submission as an issue on `gke-labs/kube-agents`,
labelled `external-feedback`. `Code.gs` is the Google Apps Script behind it; it runs in
Apps Script, not in this repository, and this file is the record of how it is set up.

The link to publish is <https://gke-labs.github.io/kube-agents/feedback>. It is a redirect
the docs site serves to the form's own URL, and `FEEDBACK_FORM_URL` in
`docs/site/astro.config.mjs` is the one place that URL is recorded. If the form is ever
recreated, change it there.

## Why it exists

The repository is public and Issues are open to any GitHub account, but an
enterprise-managed GitHub account cannot open issues, comment, or fork on any repository
outside its own enterprise. GitHub reports that to the person as a restriction on the
target repository, so people at those companies concluded the tracker was closed to them.
The form gives them a path that needs no GitHub or Google account. The
[contributing guide](../../docs/site/src/content/docs/contributing.md#where-to-file-issues)
is where readers are pointed at it.

## What a submission becomes

An issue on `gke-labs/kube-agents`, opened by `kube-agents-bot`, with:

- the one-line summary as the title, truncated at 120 characters;
- labels `external-feedback` plus `bug`, `enhancement`, or `question` when the reporter
  picked Bug, Feature request, or Question;
- a body that names the reporter as they typed it, links back to the form, and carries the
  free-text answers under headings. Empty optional answers produce no section.

The follow-up email is never posted. It stays in the form's own response store, which only
the form owner sees. Everything else the reporter types is public the moment it is filed,
and the form's description says so.

## Setup

One person owns the form and the script; today that is the maintainer who created it.

1. Open <https://script.google.com>, create a new project, replace the default file with
   `Code.gs`, and save.
2. Run `setup` once from the editor and grant the permissions it asks for. They cover Forms,
   Drive (to create the form), external requests (GitHub), Mail (failure alerts), and
   managing the project's triggers.
   It logs the share link and the edit link. It records the form id in the `FORM_ID` script
   property as soon as the form exists, and every later step is safe to repeat, so if a run
   throws partway, run it again and it finishes the form rather than creating another.
   If the form is ever deleted, remove that property before running `setup` again; the
   error message says so.
3. Give the script the bot's identity. The `kube-agents-bot` GitHub App already holds
   Issues: Read and write on this repository, which also lets it set labels. On the App's
   settings page (the `gke-labs` organization, Developer settings, GitHub Apps,
   kube-agents-bot, Private keys) generate a new private key for this script. Do not reuse
   the key the bot service holds in Secret Manager: a second key gives the form its own
   credential to revoke without touching the bot.
4. In the Apps Script project, Project Settings, Script Properties, add `GITHUB_APP_ID`
   with the App's id (`4437198`, the value the bot service runs with) and
   `GITHUB_APP_PRIVATE_KEY` with the downloaded PEM pasted whole. GitHub issues the key in
   PKCS#1 form (`BEGIN RSA PRIVATE KEY`) and Apps Script's signer takes PKCS#8; the script
   converts, so no `openssl` step is needed. For each submission it signs a JWT with the key
   and exchanges that for a token it asks to be limited to this repository and Issues:
   write.

   Be clear about what that key is. The limit above is something the script requests per
   token; the key itself is the App's full identity and can mint a token with every
   permission the App holds on every repository it is installed on, including pull request
   reviews and the `AI Review` check run that `.github/workflows/auto_request_review.yml`
   trusts by this App's id. Anyone who can read the script's properties, which is every
   editor of the Apps Script project, holds that, and the project's access control is the
   owner's Google account rather than the Secret Manager IAM and audit trail the bot
   service's own key sits behind. Revoking the key stops new tokens; a token already minted
   stays valid for the rest of its hour. This trade was made knowingly, for issues that
   carry the bot's name rather than a person's. Keep the project to its owner and add no
   editors. The ways back, if the trade stops being worth it: a separate GitHub App holding
   Issues: write only, which the script supports by changing `GITHUB_APP_ID` and the key,
   or the fine-grained personal token below, which is narrower still.

5. Submit the form once yourself and check the issue arrives with the right labels, authored
   by `kube-agents-bot`. Close that issue.

A fine-grained personal access token in a `GITHUB_TOKEN` property is the fallback the script
uses only when no App key is set. Issues then carry that person's name, GitHub only sets the
labels if their account has write access to the repository, and the token expires. Prefer the
App.

The `external-feedback` label exists on the repository. If a label named in the script did
not, GitHub would create it on the first issue, default grey and with no description, rather
than fail the call. A misspelt label name therefore shows up as a stray new label, not as
an error.

`setup` opens the form to anyone with the link and no sign-in. If the Workspace domain that
owns the account forbids external sharing, that call throws and the form has to be created
from an account whose domain allows it.

## When filing fails

The trigger emails the form owner the complete issue text and the GitHub error, then
records the failure in the project's Executions view. The submission is also kept in the
form's responses, so nothing is lost; file it by hand from the email and fix the cause. The
error names which call failed. The usual causes are a revoked App key (401 on the
installation lookup), the App no longer installed on the repository (404 there), an App
that lost Issues: write (422 minting the token), and a GitHub outage.

## Operating it

- **Changing a question.** The script matches answers to questions by the question's title.
  Edit the title in the form UI and in `QUESTIONS` together, or the answer is dropped from
  the issue.
- **Abuse.** Anyone with the link can file, and free text goes into a public issue, including
  any `@mentions` it contains. If that is abused, set the form to require sign-in from its
  settings, or close it to responses; both take effect immediately and neither needs a code
  change. Issues already filed are ordinary issues and can be closed or deleted like any
  other. Issues are authored by the bot, so a flood lands on the bot's record with GitHub,
  not on a person's. Revoking the form's private key on the App's Private keys page cuts the
  form off at once without affecting the bot service, which signs with a different key.
- **Rotating the key.** Generate a new private key on the App, replace the
  `GITHUB_APP_PRIVATE_KEY` script property, then revoke the old key. Nothing else changes.
- **Retiring it.** Close the form to responses, delete the trigger, revoke the key, remove
  the redirect from `docs/site/astro.config.mjs`, and remove the links from the contributing
  guide and the root `README.md`. If this directory goes too, drop its row from
  `docs/README.md` and its mention in the tree at the top of that file.
