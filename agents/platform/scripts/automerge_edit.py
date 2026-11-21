"""The edit-manifest verb: whether a pull request is exactly a recipe's scalar edit to one manifest.

A recipe with `verify: edit-manifest` (`fix_recipes/<kind>/recipe.yaml`) fixes
a finding by changing declared scalar fields of one existing manifest, such as
a PodDisruptionBudget's `spec.minAvailable`. Before the broker may treat such a
pull request as the recipe's fix it establishes, from the forge's diff and a
base it reads itself, that the head is byte for byte what `yaml_edit.set_scalar`
writes at the recipe's `edit_paths` on that base, and nothing else:

- the diff changes one file, already on the base, mode 100644, with no rename,
  copy, mode change, binary patch or missing final newline
  (`modified_file_from_diff`), at a path inside a cluster's own tree;
- the head is the base with the diff's hunks applied, strictly
  (`apply_hunks`): every context and removed line must match the base;
- both files are at most `MAX_FILE_BYTES`, and every changed line lies in one
  document, whose `kind` is the recipe's `edit_kind`;
- the new values are read from the head only in the forms `set_scalar` writes
  (`yaml_edit.read_scalar`), `set_scalar` is re-run on the base with them, and
  the result must equal the head. So a comment, whitespace, key order, another
  document or an undeclared field that changed refuses the pull request, and
  `set_scalar` itself refuses anchors, aliases, tags, flow style and
  templates on the path, and `apiVersion`, `kind`, `metadata.name` and
  `metadata.namespace`;
- the recipe's `fix.py` `allowed_edit(path, old, new)` accepts every change;
  a recipe without one accepts none.

Stdlib only: the credential proxy runs it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, NamedTuple

from fix_recipes import yaml_edit
from fix_recipes.registry import Recipe

MAX_FILE_BYTES = yaml_edit.MAX_FILE_BYTES
CLUSTER_TREE_ROOT = "clusters"
MANIFEST_SUFFIXES = (".yaml", ".yml")
_DIFF_HEADER = re.compile(r"diff --git a/(?P<a>[^\s\"]+) b/(?P<b>[^\s\"]+)")
# Git prints the mode on the index line only when it did not change.
_INDEX = re.compile(r"index (?P<old>[0-9a-f]{7,64})\.\.[0-9a-f]{7,64} 100644")
_HUNK = re.compile(r"@@ -(?P<os>[1-9][0-9]*)(?:,(?P<oc>[0-9]+))? \+(?P<ns>[1-9][0-9]*)(?:,(?P<nc>[0-9]+))? @@(?: .*)?")


class EditRefused(ValueError):
    """Why a pull request is not the recipe's edit, in words fit for the broker's log."""


class Hunk(NamedTuple):
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


class EditedManifest(NamedTuple):
    """A pull request proven to be `kind`'s edit of one manifest."""

    kind: str
    path: str
    cluster: str  # the short name: the tree `clusters/<cluster>/` it sits in
    doc_index: int
    object_kind: str
    name: str
    namespace: str
    changes: tuple[tuple[tuple[str | int, ...], Any, Any], ...]  # (path, old, new)


def modified_file_from_diff(diff: Any) -> tuple[str, tuple[Hunk, ...], str] | None:
    """`(path, hunks, old blob id)` when a unified diff modifies exactly one
    existing regular file in place and changes nothing else, else None. The
    blob id is the (possibly abbreviated) git object id of the file the diff
    was taken against."""
    if not isinstance(diff, str):
        return None
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 5 or sum(line.startswith("diff --git ") for line in lines) != 1:
        return None
    header = _DIFF_HEADER.fullmatch(lines[0])
    if header is None or header["a"] != header["b"]:
        return None
    path = header["b"]
    index = _INDEX.fullmatch(lines[1])
    if index is None or lines[2:4] != [f"--- a/{path}", f"+++ b/{path}"]:
        return None
    hunks: list[Hunk] = []
    at = 4
    while at < len(lines):
        match = _HUNK.fullmatch(lines[at])
        if match is None:
            return None
        old_count = int(match["oc"] if match["oc"] is not None else 1)
        new_count = int(match["nc"] if match["nc"] is not None else 1)
        body: list[str] = []
        at += 1
        while at < len(lines) and lines[at][:1] in (" ", "+", "-"):
            body.append(lines[at])
            at += 1
        hunks.append(Hunk(int(match["os"]), old_count, int(match["ns"]), new_count, tuple(body)))
    return (path, tuple(hunks), index["old"]) if hunks else None


def git_blob_id(text: str) -> str:
    """The SHA-1 git object id of `text` stored as a blob."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def apply_hunks(base: str, hunks: tuple[Hunk, ...]) -> str | None:
    """The base with `hunks` applied, or None unless each hunk applies exactly
    where it says, in order, with every context and removed line matching."""
    if not base.endswith("\n"):
        return None
    old = base[:-1].split("\n")
    out: list[str] = []
    pos = 0
    for hunk in hunks:
        start = hunk.old_start - 1
        if hunk.old_count == 0 or hunk.new_count == 0 or start < pos or start > len(old):
            return None
        out += old[pos:start]
        if hunk.new_start - 1 != len(out):
            return None
        i, added = start, 0
        for line in hunk.lines:
            tag, text = line[0], line[1:]
            if tag in " -":
                if i >= len(old) or old[i] != text:
                    return None
                i += 1
            if tag in " +":
                out.append(text)
                added += 1
        if i - start != hunk.old_count or added != hunk.new_count:
            return None
        pos = i
    return "\n".join(out + old[pos:]) + "\n"


def cluster_of(path: str) -> str | None:
    """The short cluster name of a manifest inside `clusters/<name>/`, or None
    for any other path, or one with an empty, `.` or dot-led segment."""
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != CLUSTER_TREE_ROOT or not parts[-1].endswith(MANIFEST_SUFFIXES):
        return None
    if any(not part or part.startswith(".") for part in parts):
        return None
    return parts[1]


def verify_edited_manifest(recipe: Recipe, fix_module: Any, diff: Any, path: str, base: str) -> EditedManifest:
    """The edit `diff` makes to `path`, proven against `base` (the file as the
    broker read it on the base branch), or EditRefused."""
    if recipe.verify != "edit-manifest":
        raise EditRefused(f"the {recipe.kind} recipe does not verify edits")
    parsed = modified_file_from_diff(diff)
    if parsed is None or parsed[0] != path:
        raise EditRefused("the diff does not modify exactly that one file in place")
    cluster = cluster_of(path)
    if cluster is None:
        raise EditRefused(f"{path} is not a manifest inside a cluster's own tree")
    # The forge diffs the head against the merge base. Applied by line number
    # to a base that has moved since, a hunk can land on another object of
    # the same shape, so the base read here must be the very blob the diff
    # was taken against (which also rules out `git archive`'s attribute
    # rewrites). A SHA-256 repository never matches and is refused.
    if not git_blob_id(base).startswith(parsed[2]):
        raise EditRefused("the diff was not taken against the base the broker read (the base branch moved)")
    head = apply_hunks(base, parsed[1])
    if head is None:
        raise EditRefused("the diff does not apply exactly to the base the broker read")
    if max(len(base.encode()), len(head.encode())) > MAX_FILE_BYTES:
        raise EditRefused(f"the manifest is larger than {MAX_FILE_BYTES} bytes")
    try:
        return _verify(recipe, fix_module, path, cluster, base, head)
    except yaml_edit.RefusedEdit as exc:
        raise EditRefused(f"the editor refuses this manifest ({exc.reason}: {exc.detail})") from None


def _verify(recipe: Recipe, fix_module: Any, path: str, cluster: str, base: str, head: str) -> EditedManifest:
    spans = yaml_edit.document_spans(base)
    if len(yaml_edit.document_spans(head)) != len(spans):
        raise EditRefused("the head has a different number of documents")
    old_lines, new_lines = base.split("\n"), head.split("\n")
    if len(old_lines) != len(new_lines):
        raise EditRefused("the edit adds or removes lines")
    changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b]
    docs = {next((d for d, (s, e) in enumerate(spans) if s <= i < e), -1) for i in changed}
    if not changed or len(docs) != 1 or -1 in docs:
        raise EditRefused("the changed lines do not lie in exactly one document")
    doc = docs.pop()
    object_kind = yaml_edit.read_scalar(base, doc, ["kind"])
    if object_kind != recipe.edit_kind:
        raise EditRefused(f"the edited document is a {object_kind}, not a {recipe.edit_kind}")
    changes = []
    for field in recipe.edit_path_list():
        try:
            old = yaml_edit.read_scalar(base, doc, list(field))
        except yaml_edit.RefusedEdit as exc:
            if exc.reason == "path-missing":
                continue  # an edit never creates a field; a head that adds one fails the byte check
            raise
        new = yaml_edit.read_scalar(head, doc, list(field))
        if new != old or type(new) is not type(old):
            changes.append((field, old, new))
    if not changes:
        raise EditRefused("no declared field changed")
    expected = base
    for field, _, new in changes:
        expected = yaml_edit.set_scalar(expected, doc, list(field), new)
    if expected != head:
        raise EditRefused("the head is not, byte for byte, the declared edits made to the base")
    allowed = getattr(fix_module, "allowed_edit", None)
    if not callable(allowed):
        raise EditRefused(f"the {recipe.kind} recipe's fix.py has no allowed_edit, so it accepts no edit")
    try:
        refused = [".".join(map(str, f)) for f, old, new in changes if allowed(f, old, new) is not True]
    except Exception as exc:  # noqa: BLE001 -- a recipe's check that fails accepts nothing
        raise EditRefused(f"the {recipe.kind} recipe's allowed_edit failed ({type(exc).__name__})") from None
    if refused:
        raise EditRefused(f"the {recipe.kind} recipe does not accept the new value of {', '.join(refused)}")
    name = yaml_edit.read_scalar(base, doc, ["metadata", "name"])
    try:
        namespace = yaml_edit.read_scalar(base, doc, ["metadata", "namespace"])
    except yaml_edit.RefusedEdit as exc:
        if exc.reason != "path-missing":
            raise
        namespace = ""
    return EditedManifest(
        recipe.kind, path, cluster, doc, str(object_kind), str(name), str(namespace), tuple(changes)
    )
