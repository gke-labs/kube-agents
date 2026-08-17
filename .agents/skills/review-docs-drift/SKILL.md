---
name: review-docs-drift
description: Reviews a pull request for documentation drift — finds which docs the change should have updated, verifies doc claims against source, and checks the docs map and AGENTS.md themselves for staleness.
---

# Task

Given a pull request (a branch diff against `main`), determine whether the repository's documentation is still accurate after the change, and report exactly which documents need updating and why. You are checking two directions:

1. **Code → docs:** the PR changed behavior, names, defaults, paths, or structure that some document states as fact.
2. **Docs → source:** the PR changed documentation, and what it now says must match the source of truth, the repo's documentation rules, and the other docs.

Your two navigation instruments are:

- **`AGENTS.md` (repo root)** — owns the documentation RULES: the canonical-home table (one home per fact), the generated-region rule, link-don't-summarise, no PR-status prose, verify-identifiers-against-source.
- **`docs/README.md`** — the documentation MAP: what lives where, what each document covers, which files carry generated regions and from which sources, and which source files own the identifiers that docs state as fact.

This skill deliberately holds no repository facts of its own — no file lists, no agent topology, no counts. Facts live in the two instruments above and in the sources they point to; when this skill and a source disagree, the source wins and this skill needs fixing.

Read both before reviewing the diff.

# Procedure

## 1. Collect the change surface

- `git diff --stat main...HEAD` (or the PR's base) — list every changed file.
- Classify each changed path:
  - **Source of a generated region?** Check the generated-regions table in the map (`docs/README.md` §2). If the changed file — or its frontmatter or comment banner — feeds a generated region, that region must be regenerated (`make docs-generate`) and committed.
  - **Source of documented identifiers?** Check the identifier-sources table in the map (`docs/README.md` §2). If the changed file owns names, defaults, versions, section numbering, or baked paths that docs state as fact, find every doc that states a fact about the changed item.
  - **Doc file?** → review it under step 3.
  - **Anything else** (code, scripts, workflows, examples) → check the map for pages that describe that component.

## 2. Find the affected docs (code → docs)

For each changed source item:

- Look it up in `docs/README.md` to find the pages that document that area; then `git grep` the identifier (old AND new spelling) across `*.md`/`*.mdx` to catch pages the map's summaries don't surface.
- A doc sentence that names a file, flag, default, section number, count, or identifier is a **testable assertion** — test it against the PR's version of the source, not against other docs and not against your memory.
- Pay specific attention to known drift magnets. Each is a category of claim to re-verify, not a fact to assume — the current truth lives in the named source, and this skill deliberately does not restate it:
  - Identifiers that have a source-of-truth file (service-account and namespace names, permission-set defaults, versions) — verify against the identifier-sources table in the map, never against other docs.
  - `SOUL.md §N` references anywhere in the docs — verify against the current headings of that `SOUL.md`.
  - Paths docs claim are baked into container images — verify against the Dockerfile.
  - Hard-coded counts ("eleven steps", "20 skills") — these should generally not exist anywhere, the map included; flag any the PR introduces.
  - Agent scope and topology claims — which profile receives chat ingress, which agents may mutate infrastructure or write to GitOps, which are read-only, and how work is delegated between them. Verify against the repository layout in `AGENTS.md` and the agents' own persona docs and config (`agents/*/SOUL.md`, `agents/*/config.yaml`), never against other docs or your memory of the architecture. Docs that conflate two agents' scopes, or that still describe the topology from before a PR that changed it, are drift.

## 3. Review changed docs (docs → rules and source)

For every doc the PR adds or edits:

- **Canonical home:** is this fact's home per the `AGENTS.md` table? If the content duplicates another page, it should link instead (the rule is link-don't-summarise; if it must summarise, it must name the canonical page).
- **Generated regions:** nothing inside `<!-- BEGIN GENERATED: ... -->` / `{/* BEGIN GENERATED ... */}` may be hand-edited. If the rendered table is wrong, the fix is in the source + `make docs-generate`.
- **No PR-status prose:** docs describe `main`; "PR #NNN adds/proposes…" sentences rot on merge.
- **Identifiers verified:** every named file/target/SA/version in the new prose exists in the tree at the PR's HEAD.
- **Internal consistency:** the page must not say two different things after the merge (read the whole page, not just the hunk).
- **Deletion audit:** if the PR deletes or trims a doc, confirm every deleted fact genuinely exists at the canonical home the page now points to.

## 4. Check the instruments themselves

- **`docs/README.md` (the map):** if the PR adds, moves, renames, or deletes a doc that no collapsed family row's glob already covers, the map must reflect it — tree section and inventory table. A file landing inside an existing family (a new skill, SOP, or reference) needs no map edit, only `make docs-generate`. The map is hand-maintained; `make docs-check` (`docs-check-map`) enforces presence and shape — an inventory entry per tracked doc outside root-level dot-directories, no dead paths in the path column, and single-space table padding. The map states no counts by design. A file _deleted_ from inside a family glob is invisible to those checks — the glob still matches the survivors — and is caught instead by the generated `docs/family-roster.txt`, which `docs-check-generated` fails on until it is regenerated; if the PR removes a family member, expect the deleted roster line in the diff and check the row still describes what is left. The row summaries and the identifier-sources table have no mechanical guard, so verify those here. Also spot-check that map entries touching the PR's area are still accurate.
- **Map churn is a finding.** The map is the repository's most conflict-prone file. A map diff that rewrites rows the PR did not author — re-aligned table columns, re-wrapped cells — is Blocking: it conflicts with every other open PR that adds a row. The correct diff is the inserted rows and nothing else.
- **Map staleness window:** the map stores no "last verified" stamp; derive the delta from git instead — everything that changed since the map itself was last touched is the map's unreviewed backlog:

  ```bash
  git diff --name-status "$(git log -1 --format=%H -- docs/README.md)"..HEAD -- '*.md' '*.mdx'
  ```

  If that list contains adds/renames/deletes the map does not reflect, the map is stale even if this PR didn't cause it — report it either way.

- **`AGENTS.md`:** if the PR changes the repo layout, the docs toolchain (`scripts/generate_docs.py`, checkers in `hack/`/`scripts/`), or where a category of content lives, the layout section and canonical-home table need the same update. If the PR invalidates a rule's example, fix the example.

## 5. Run the mechanical gates

- `make docs-check` at the PR's HEAD — generated tables current, relative links resolve (targets must be git-tracked), terminology matches source, map inventory current and its tables un-re-aligned.
- If the PR touched a generated-table source: run `make docs-generate` and confirm `git status` is clean afterwards (a dirty tree means the PR forgot to commit regenerated tables).
- `npx prettier --check` on changed `.md`/`.json`/`.yaml` files (note: the generated `skills/index.mdx` is intentionally prettier-exempt).
- If site pages changed: `cd docs/site && npm run build`.

# Output

Report a triage table: **finding → evidence (file:line + the source that contradicts it) → severity → required action (which doc, what change)**. Separate:

- **Blocking:** a doc now states something false, a generated region is stale or hand-edited, a link is broken, the map/AGENTS.md missed a structural change.
- **Advisory:** style-rule violations (duplication, summarise-without-canonical-link), drift magnets worth a follow-up.

Do not fix silently — the report is the deliverable unless you were explicitly asked to apply fixes. Never resolve a finding by editing a generated region or by making two docs agree with each other without checking the underlying source: source wins, always.
