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
- **`docs/README.md`** — the documentation MAP: what lives where (a directory tree), which files carry generated regions and from which sources, and which source files own the identifiers that docs state as fact. It carries no per-document rows; what a document covers is the document's own business.

This skill deliberately holds no repository facts of its own — no file lists, no agent topology, no counts. Facts live in the two instruments above and in the sources they point to; when this skill and a source disagree, the source wins and this skill needs fixing.

Read both before reviewing the diff.

# Procedure

## 1. Collect the change surface

- `git diff --stat main...HEAD` (or the PR's base) — list every changed file.
- Classify each changed path:
  - **Source of a generated region?** Check the generated-regions table in the map (`docs/README.md` §2). If the changed file — or its frontmatter or comment banner — feeds a generated region, that region must be regenerated (`make docs-generate`) and committed.
  - **Source of documented identifiers?** Check the identifier-sources table in the map (`docs/README.md` §2). If the changed file owns names, defaults, versions, section numbering, or baked paths that docs state as fact, find every doc that states a fact about the changed item.
  - **Doc file?** → review it under step 3.
  - **Anything else** (code, scripts, workflows, examples) → find the pages that describe that component (step 2).

## 2. Find the affected docs (code → docs)

For each changed source item:

- Find the pages that document that area: the tree in `docs/README.md` names the directory that holds them, and a `git grep` for the identifier (old AND new spelling) across `*.md`/`*.mdx` finds every page that states it. The map carries no per-document summaries, so a page that describes the component in prose without naming it is found only by reading the directory the tree points at.
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
- **Audience, for every hunk under `docs/site/`:** who is the reader, and could they act on this against their own install? Blocking when the hunk names anything on the identifier list in `.agents/rules/documentation.md` (App or installation IDs, workflow secret or variable names, the maintainers' projects, service accounts, Workload Identity pools, environments or clusters, internal repository paths), describes which workflow runs when, or addresses the reader as someone changing the source ("when changing this code", "before you edit", a test or Go symbol name). Suggest the destination from the `AGENTS.md` canonical-home rows for maintainer environments, release runbooks, or the evaluation pool, and the map section that goes with it.

## 4. Check the instruments themselves

- **`docs/README.md` (the map):** the directory tree plus the generated-regions and identifier-sources tables, with no per-document rows. If the PR adds a document, the document must be linked from the page that owns its topic: `make docs-check` (`docs-check-links`) fails a tracked document no reader reaches — the root files, every `README.md`, the site's sidebar pages, the design documents code cites, and the uniform families its `LINK_EXEMPT_FAMILY_GLOBS` name are reached without a link — and the fix is the link, never a new entry in its `UNLINKED_ALLOWLIST`. If the PR links or deletes a document on that allowlist, the entry goes in the same PR; the check fails a stale one. A new uniform family is a glob added to the checker, argued in the pull request, and a directory added, moved, or emptied is a tree edit. The map states no counts by design. The tables have no mechanical guard beyond compactness (`tests/test_docs_map_compact.py`), so verify here that the identifier-sources rows touching the PR's area still name the right source.
- **Map churn is a finding.** The map's two tables are edited from several branches a week. A map diff that rewrites rows the PR did not author — re-aligned table columns, re-wrapped cells — is Blocking: it conflicts with every other open PR that adds a row. The correct diff is the inserted rows and nothing else.
- **Map staleness window:** the map stores no "last verified" stamp; derive the delta from git instead — everything that changed since the map itself was last touched is the map's unreviewed backlog:

  ```bash
  git diff --name-status "$(git log -1 --format=%H -- docs/README.md)"..HEAD -- '*.md' '*.mdx'
  ```

  If that list contains a directory added, moved, or emptied that the tree does not reflect, or a moved file that an identifier-sources row still names by its old path, the map is stale even if this PR didn't cause it — report it either way. A single document added, renamed, or deleted is the reachability check's business, not the map's.

- **`AGENTS.md`:** if the PR changes the repo layout, the docs toolchain (`scripts/generate_docs.py`, checkers in `hack/`/`scripts/`), or where a category of content lives, the layout section and canonical-home table need the same update. If the PR invalidates a rule's example, fix the example.

## 5. Run the mechanical gates

- `make docs-check` at the PR's HEAD — generated tables current, relative links resolve (targets must be git-tracked, and so must a `docs/designs/…` or `docs/architecture/…` path cited from code) and a reader reaches every tracked document, terminology matches source, no maintainer identifier on a site page (`docs-check-audience`), `AGENTS.md` plus `CLAUDE.md` inside their context budget. The map's tables staying compact is `tests/test_docs_map_compact.py`'s check, under `make test-python`.
- If the PR touched a generated-table source: run `make docs-generate` and confirm `git status` is clean afterwards (a dirty tree means the PR forgot to commit regenerated tables).
- `npx prettier --check` on changed `.md`/`.json`/`.yaml` files (note: the generated `skills/index.mdx` is intentionally prettier-exempt).
- If site pages changed: `cd docs/site && npm run build`.

# Output

Report a triage table: **finding → evidence (file:line + the source that contradicts it) → severity → required action (which doc, what change)**. Separate:

- **Blocking:** a doc now states something false, a generated region is stale or hand-edited, a link is broken, the map/AGENTS.md missed a structural change.
- **Advisory:** style-rule violations (duplication, summarise-without-canonical-link), drift magnets worth a follow-up.

Do not fix silently — the report is the deliverable unless you were explicitly asked to apply fixes. Never resolve a finding by editing a generated region or by making two docs agree with each other without checking the underlying source: source wins, always.
