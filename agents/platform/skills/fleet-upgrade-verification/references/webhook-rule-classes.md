# How the webhook rule classes a dead webhook's rules

The fail-closed webhook rule of `--readiness` (SKILL.md, "Check upgrade readiness") grades a webhook
whose backend is unreachable by what its rules match. This is how a rule is matched, how each rule is classed and
what the table's cell then says of it. "The list" below is `UPGRADE_PATH_TARGETS` in
`scripts/upgrade_readiness.py`.

Rules are matched on API group, API version, operation, resource (with the API's `*`, `*/*` and
`pods/*` semantics) and scope, as the API server matches them: a rule's `apiVersions` must carry `*`
or the version the server serves the write at (`v1` for every write on the list), so a rule pinned
to a version the server no longer serves (`policy/v1beta1`, `certificates.k8s.io/v1beta1`) matches
nothing.

Each rule of a dead off-path webhook is then classed by what the server serves it, on the resources
it names and not on its operations: a rule at `*` or `v1` fails now every request the server sends
it (whether the server serves an off-list resource at all is not the reader's to say, and the cell
claims no more); a rule naming graded resources only, at an unserved version, is sent nothing
(whatever the operation; the cell then names the resource, and a group-resource pair the rule spells
that the server serves at no version, such as `policy/pods`, is said so rather than served at `v1`);
a rule at an unserved version on a resource, group or wildcard off the lists is one whose served
versions the reader does not know. The cell says each for the rules the webhook has: a webhook whose
every rule is pinned is told the server sends it no request at a served version rather than that its
requests fail now; otherwise the live rules are named as failing now every request the server sends
them, the pinned rules as sent nothing, and the unknown rules as failing only if the server still
serves their version, so a failure now is asserted for the live rules alone.
