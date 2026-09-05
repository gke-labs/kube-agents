# Recorded API responses, one file per collaboration verb

`test_providers_contract.py` runs every forge in `providers.AVAILABLE` against
the same assertions, and it needs each forge to supply what its API actually
answers. That is what these are. A forge package that ships no `fixtures/`
directory fails the harness by name rather than being skipped, because a
contract test that silently covers one forge is the failure mode the harness
exists to prevent.

## Shape

Each file is one JSON object:

```json
{
  "payload": { "repository": "...", "...": "..." },
  "responses": [{ "...": "..." }]
}
```

`payload` is the request the broker would receive on `POST /v1/vcs/<verb>`.
`responses` are the API answers, in the order the forge asks for them — two
entries for the `view` verbs, which fetch the object and then its comments.
The harness raises on any call the file does not cover, so an implementation
that grew a third request fails rather than quietly reading `None`.

## Provenance

Recorded against `api.github.com` from a real repository, then edited three
ways and never regenerated wholesale:

- **Trimmed.** GitHub answers a pull request with roughly 80 fields; what is
  left is what the translation reads plus enough neighbours that a field the
  translation should _not_ be reading is present to be gotten wrong. Trimming
  to exactly the fields used would make the harness unable to catch a
  translation that reached for the wrong one.
- **Redacted.** Every login, avatar URL, node ID, repository name and
  organisation name is replaced. No token, installation ID or `Authorization`
  header was recorded at any point — the recording sat below the transport.
- **Composed.** `proposal-list.json` carries three proposals in the three
  states the contract asserts (open, merged, closed-and-draft) and
  `issue-list.json` carries an issue-shaped pull request — GitHub returns both
  from `/issues` — so the filtering has something to filter. Those are
  arrangements of real responses, not invented ones.

## Changing them

Prefer re-recording over hand-editing when a translation changes, and keep the
three edits above. If a verb starts making a different number of calls, the
`responses` array is the file that has to say so; the harness will not infer
it.

These are not a mock of the forge. They are one recorded answer per verb, which
is enough to pin the shape the broker promises and deliberately not enough to
stand in for the real thing — `test_vcs_broker.py` exercises the broker's own
plumbing against a local repository instead.
