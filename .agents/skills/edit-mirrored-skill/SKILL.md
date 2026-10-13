---
name: edit-mirrored-skill
description: Edit, sync or start mirroring a `gke-*` skill in `agents/platform/skills/` that is copied from google/skills. Use when changing one of those skills, updating it to upstream, or resolving a skill sync conflict.
---

# Task

Change a mirrored skill so the change survives upstream syncs and `make skills-check` passes.
This skill is the procedure; [`docs/designs/upstream-skill-overlays.md`](../../../docs/designs/upstream-skill-overlays.md)
is the design and its rationale. `scripts/skill_overlay.py` is the tool behind the
`make skills-*` targets.

A skill is mirrored when `agents/platform/skill-overlays/<skill>/upstream.lock` exists. It has
three layers:

| Layer         | Path                                      | Edit by hand                                |
| ------------- | ----------------------------------------- | ------------------------------------------- |
| Upstream copy | `third_party/google-skills/<skill>/`      | never; only the tool writes it and the lock |
| Overlay       | `agents/platform/skill-overlays/<skill>/` | patch headers, `append.md`, deleting a file |
| Shipped skill | `agents/platform/skills/<skill>/`         | yes, then record it with `skills-refresh`   |

The shipped skill is the copy with the patches applied in filename order, then `append.md`. It
is committed, and it is what the agent reads. `skills-refresh` writes patch bodies and
`append.md` from your edits; `skills-sync` and `skills-continue` rewrite the copy, lock, patches
and shipped skill; `skills-generate` rebuilds the shipped skill.

# Rules

- Never edit `third_party/google-skills/` or `upstream.lock` by hand. CI compares both with
  `google/skills`. If a sync reports that upstream renamed or removed the skill, stop and ask a
  maintainer.
- Keep one patch per reason. Fold a follow-up edit into the patch with the same reason.
- Put an upstream sync in its own pull request, with no other edit than the patches it needs to
  pass the checks (conflict resolutions, a `make shellcheck` fix).
- Never delete a patch to get past a conflict. Drop one only when upstream's new text covers its
  `Why:`, and say so in the pull request.
- Keep `MSG=` to plain words: the Makefile passes it through the shell, so backticks and double
  quotes break. Edit the `Subject:` line in the patch afterwards if it needs code formatting.
- Run `make skills-check` before committing; commit the shipped skill and its overlay together.
- Treat a skill's scripts like its Markdown: edit the shipped script and record it with
  `make skills-refresh`. `make shellcheck` lints the shipped `gke-*` scripts. Fix a finding in a
  patch; add `# shellcheck disable=SCnnnn # reason` (also in a patch) only for a false positive, or
  when the fix would change what upstream's script does. If a sync brings in a new finding, add that
  patch in the same pull request. Put tests for those scripts in `tests/`
  ([Where Tests Go](../../../AGENTS.md#where-tests-go)); a file in the skill folder that no patch
  produces fails `make skills-check`.
- Run `make docs-generate` when a skill is added or a frontmatter `description` changes, and
  commit the regenerated skill catalogue. Run `make docs-check` before pushing.
- Expect the `Docker Build` check (the platform image build), not `make skills-check`, to run every
  shell block of the shipped skills through `deploy/docker/check_skill_commands.py`. If an edit or sync changes a block listed in its
  `KNOWN_FINDINGS`, update the entry in the same pull request; rewrite a command Tirith refuses in
  a patch.
- Follow the eval loop ([`.agents/rules/eval_driven_development.md`](../../rules/eval_driven_development.md))
  for any change to what the agent reads: adding, changing or removing a patch or `append.md`, or a
  sync. A change that leaves every shipped skill byte-identical, or a patch that only fixes a lint
  finding in a script without changing what it does, states that exemption in one line under
  **Live validation**.
- Leave stopping mirroring (deleting a skill's copy, lock and overlay) to a maintainer.

# Workflow

## Make a change

1. Edit files under `agents/platform/skills/<skill>/`.
2. If the edit serves the same reason as an existing patch, run
   `make skills-refresh SKILL=<skill> PATCH=<nnnn>`. If it succeeds, stop here. If it fails with
   `conflicts with a later patch`, a later patch depends on those lines: go to step 3, keep the
   new patch even if refresh warns, and skip step 4.
3. Otherwise run `make skills-refresh SKILL=<skill> MSG="<subject>"`. It writes the next
   `NNNN-<slug>.patch`.
4. If refresh warns that the edit changes lines an earlier patch introduced, delete the new patch
   and go to step 2 with that patch's number.
5. Fill in the new patch's header; refresh writes `TODO` in the first three and `none` in
   `Upstream-Issue:`:
   - `Why:`: why this repository needs the change. Whoever resolves a future conflict reads only
     this line, so state the intent, not the edit. Name a related earlier patch if there is one.
   - `Local-Issue:`: the issue that asked for it, not the pull request.
   - `Retire-When:`: the upstream change that makes the patch unnecessary, or `never; <reason>`.
   - `Upstream-Issue:`: the `google/skills` issue filed for a general fix, `none filed yet` if the
     fix is general but not reported yet, or `none (specific to this repository)`.

## Add or edit the appended section

- Add one by ending the shipped `SKILL.md` with a section whose first line starts with
  `<!-- kube-agents: local addition`, then run `make skills-refresh SKILL=<skill>`; refresh writes
  it to `append.md`.
- Edit an existing one in the shipped skill and refresh the same way. Keep the marker line.

## Remove a change

1. Delete the patch, or edit `append.md`.
2. Run `make skills-generate SKILL=<skill>`.
3. If it reports that a later patch no longer applies, that patch builds on the one you deleted,
   and refresh cannot fix it. Delete that patch too and run `make skills-generate` again. If its
   `Why:` still holds, redo its edit in the shipped skill, run
   `make skills-refresh SKILL=<skill> MSG="<subject>"`, and copy the old patch's header into the
   new one.

## Sync to upstream

Sync a skill only when you are about to change it and upstream changed the same text, when an
eval or a report shows the skill misleading the agent, or when upstream fixes a correctness or
security bug in it. Being behind upstream is not a reason: each sync needs the eval loop. To change
a skill otherwise, patch it at its current pin ("Make a change").

1. Run `make skills-status` to see which mirrored skills upstream has moved past.
2. Run `make skills-sync SKILL=<skill>`, or add `REF=<commit>` for a commit on upstream's `main`.
3. If the output starts with `CONFLICT:`, for each stopped patch:
   - Open the listed files under `.skill-sync/<skill>/` and read the patch's `Why:`.
   - Merge so both upstream's change and the patch's intent hold. Keep text upstream added beside
     the patch's lines.
   - To drop the patch because upstream now covers its `Why:`, resolve to upstream's text.
   - Remove every conflict marker, then run `make skills-continue SKILL=<skill>`.
4. Any other failure is an error, not a conflict: read the message.
5. Run `make shellcheck`; clear a new finding in an upstream script as the scripts rule above says.
6. Copy the sync's final report into the pull request: retired patches with their `Why:`, dropped
   patches and the reason, any `append.md` note, and the patch count, share of lines changed and
   conflicts resolved. Question a retired patch whose `Retire-When:` says never.
7. To abandon a sync, delete `.skill-sync/<skill>/`. Nothing outside it has changed.

## Start mirroring

- Adopt an upstream `gke-*` skill this repository does not ship with
  `make skills-sync SKILL=<skill>`, then add it to `SKILL_GROUPS` in `scripts/generate_docs.py`
  and run `make docs-generate`.
- If `skills-sync` or `skills-status` reports a `gke-*` skill that ships without a lock, it is
  either this repository's own skill (rename it) or a hand copy of upstream's. For a copy:
  1. Run `make skills-import SKILL=<skill> REF=<commit>` with the upstream commit it was copied
     from. If the remaining differences are not all local edits, `REF` is wrong: delete the copy
     and lock and import again.
  2. Run `make skills-generate SKILL=<skill>`; the local text stays in `HEAD`.
  3. For each reason, re-apply that reason's hunks from
     `git diff HEAD -- agents/platform/skills/<skill>/` and run
     `make skills-refresh SKILL=<skill> MSG="<reason>"`. Stop when that diff is empty.
  4. Fill in each patch's header as in "Make a change", step 5.

## Fix a failing check

| `make skills-check` reports                     | Cause                                        | Fix                                            |
| ----------------------------------------------- | -------------------------------------------- | ---------------------------------------------- |
| the committed skill differs from copy + overlay | the skill was edited and no patch records it | `make skills-refresh SKILL=<skill>`            |
| the committed skill differs from copy + overlay | a patch or `append.md` was edited            | `make skills-generate SKILL=<skill>`           |
| the copy does not match its lock                | the upstream copy was edited by hand         | revert the copy; change it only through a sync |
| a patch no longer applies                       | a patch it builds on was deleted or edited   | "Remove a change", step 3                      |
