"""Whether a pull request adds exactly a recipe's generated fix, for the credential broker.

A fix recipe writes its fix in code (`fix_recipes/<kind>/fix.py`). Before the
broker may mark such a pull request as one a pipeline could merge without a
review, it has to establish what the pull request adds itself, because nothing
the sandbox reports can be trusted for that:

- `verify_added_manifest`: the diff adds one file and changes nothing else
  (`added_file_from_diff`); the file is printable ASCII; and the recipe's own
  `generate`, re-run here on the inputs the file names (`read_inputs`), gives
  the same bytes. One generator serves the audit and the broker, so a file
  anyone edited or wrote by hand is not the generated fix.
- `path_fits`: the file sits under the generated name, in its cluster's own
  tree.
- `regenerates_from`: once the broker has read the cluster, the file is also
  byte for byte what the generator writes from that read (the workload's live
  selector) in place of what the file says.
- `PRACTICE_LABEL` and `AUTOMERGE_LABEL` are the labels, which only the broker
  writes (`vcs_broker.VcsBroker.proposal_practice_label`):
  `reserved_label_refusal` is how every caller-reachable verb refuses them.

Stdlib only: the credential-proxy image ships this directory (with
`fix_recipes/`) and nothing from the fleet-audit skill.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from fix_recipes import registry

#: The labels the broker puts on a fix pull request it judged eligible: the
#: first while `KUBEAGENTS_AUTOMERGE` is `practice`, which nothing acts on, the
#: second while it is `on`, which the customer's pipeline may merge on.
#: Reserved: no caller may add, remove, ensure or set either.
PRACTICE_LABEL = "kube-agents:would-auto-merge"
AUTOMERGE_LABEL = "kube-agents:auto-merge"
RESERVED_LABELS = (PRACTICE_LABEL, AUTOMERGE_LABEL)
#: Deterministic risk-tier labels reserved for the credential broker alone.
RISK_LABEL_PREFIX = "risk:"
RISK_LABELS = ("risk:low", "risk:medium", "risk:high", "risk:critical", "risk:unknown")
LABEL_FOR_SWITCH = {"practice": PRACTICE_LABEL, "on": AUTOMERGE_LABEL}
LABELLING_SWITCH_VALUES = frozenset(LABEL_FOR_SWITCH)
LABEL_COLOR = "c5def5"
LABEL_DESCRIPTIONS = {
    PRACTICE_LABEL: "kube-agents would let this fix merge without a review (practice)",
    AUTOMERGE_LABEL: "kube-agents judged this fix safe to merge without a review",
}
#: The payload fields that write a proposal's or an issue's labels. A filter
#: (`excludeLabels`, and `labels` on a list verb) only reads, and may name the label.
LABEL_WRITE_FIELDS = ("labels", "labelsAdd", "labelsRemove")
#: The fix type the rules file names (`automation_rules.KNOWN_FIX_TYPES`).
FIX_TYPE = "no-pdb"
# The per-cluster tree of the reference GitOps layout, `clusters/<name>/`.
CLUSTER_TREE_ROOT = "clusters"
_DIFF_HEADER = re.compile(r"diff --git a/(?P<a>[^\s\"]+) b/(?P<b>[^\s\"]+)")
_HUNK = re.compile(r"@@ -0,0 \+1(?:,(?P<count>\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"
#: The largest added file read, the rules file's cap (`automation_rules.MAX_FILE_BYTES`).
MAX_ADDED_FILE_BYTES = 64 * 1024


class AddedManifest(NamedTuple):
    """A pull request's one added file, proven to be `kind`'s generated fix."""

    kind: str
    path: str
    name: str
    candidate: dict
    finding_id: str
    text: str

    # What a recipe's `checks(change, live)` reads (`fix_recipes/<kind>/checks.py`).
    @property
    def object(self) -> str:
        return str(self.candidate.get("object") or "")

    @property
    def namespace(self) -> str:
        return str(self.candidate.get("namespace") or "")

    @property
    def selector(self) -> dict:
        return self.candidate.get("pod_selector") or {}


def reserved_label_refusal(verb: str, payload: Any) -> str | None:
    """Why a caller's `verb` with `payload` may not run, or None.

    Any label write naming one of `RESERVED_LABELS`, in any case and with any
    surrounding space, and a `label-ensure` of one: the broker alone decides
    who carries them.
    """
    if not isinstance(payload, dict):
        return None
    reserved = {label.casefold() for label in RESERVED_LABELS}
    risk_reserved = {label.casefold() for label in RISK_LABELS}
    named = [payload.get("name")] if verb == "label-ensure" else []
    for field in LABEL_WRITE_FIELDS:
        if field == "labels" and verb.endswith("-list"):
            continue
        value = payload.get(field)
        named += list(value) if isinstance(value, (list, tuple)) else [value]
    # Split on commas too: a forge that takes labels as one comma-joined
    # parameter (GitLab's `add_labels`) reads `x,<label>` as two labels.
    names = [part.strip().casefold() for label in named if isinstance(label, str) for part in label.split(",")]
    if any(name in reserved for name in names):
        return (
            f"The labels {' and '.join(RESERVED_LABELS)} are reserved: the credential broker "
            "applies them itself to a fix it judged safe to merge (proposal-practice-label), "
            "and no request may add, remove or create them."
        )
    if any(name in risk_reserved or name.startswith(RISK_LABEL_PREFIX) for name in names):
        return (
            f"The labels {', '.join(RISK_LABELS)} (and all `{RISK_LABEL_PREFIX}*` labels) are reserved: "
            "the credential broker computes and applies them deterministically from git state, "
            "and no request may add, remove or create them."
        )
    return None


def added_file_from_diff(diff: Any) -> tuple[str, str] | None:
    """`(path, text)` when a unified diff adds exactly one new file and changes
    nothing else, else None. A quoted path (one git had to escape) is refused
    rather than unquoted."""
    if not isinstance(diff, str):
        return None
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if sum(line.startswith("diff --git ") for line in lines) != 1:
        return None
    header = _DIFF_HEADER.fullmatch(lines[0]) if lines else None
    if header is None or header["a"] != header["b"]:
        return None
    path = header["b"]
    at = 1
    # A diff with no mode line is still read; `--- /dev/null` below is what says
    # the file is new. One that does must be a plain file's.
    if at < len(lines) and lines[at].startswith("new file mode "):
        if lines[at] != "new file mode 100644":
            return None
        at += 1
    if at < len(lines) and lines[at].startswith("index "):
        at += 1
    if lines[at : at + 2] != ["--- /dev/null", f"+++ b/{path}"]:
        return None
    at += 2
    hunk = _HUNK.fullmatch(lines[at]) if at < len(lines) else None
    if hunk is None:
        return None
    body = lines[at + 1 :]
    newline = True
    if body and body[-1] == _NO_NEWLINE:
        body.pop()
        newline = False
    if not body or not all(line.startswith("+") for line in body):
        return None
    if len(body) != int(hunk["count"] or 1):
        return None
    return path, "\n".join(line[1:] for line in body) + ("\n" if newline else "")


def verify_added_manifest(kind: str, diff: Any, *, check_path: bool = True) -> AddedManifest | None:
    """The one file `diff` adds, when it is byte for byte `kind`'s generated fix, else None.

    The recipe reads the inputs the file names (`read_inputs`, untrusted) and
    `generate` is re-run on them here; the file is the fix only when the
    output is the same bytes and the same name. Anything but printable ASCII
    and newlines is refused first: a carriage return or a byte past ASCII can
    end a line for a YAML reader and carry a second document through a
    byte-exact round trip. `check_path=False` leaves `path_fits` to a caller
    that reports a misplaced file in words of its own.
    """
    added = added_file_from_diff(diff)
    if added is None:
        return None
    path, text = added
    if len(text.encode()) > MAX_ADDED_FILE_BYTES or not all(c == "\n" or " " <= c <= "~" for c in text):
        return None
    fix = registry.generator(kind)
    read = fix.read_inputs(text)
    if read is None:
        return None
    candidate, finding_id = read
    built = fix.generate(candidate, finding_id)
    if built is None or built[1] != text:
        return None
    name = built[0]
    if check_path and not path_fits(path, name, str(candidate.get("cluster") or "")):
        return None
    return AddedManifest(kind, path, name, candidate, finding_id, text)


def regenerates_from(fix: AddedManifest, read: dict) -> bool:
    """Whether `fix`'s file is byte for byte what its recipe generates when the
    inputs the broker read itself (`read`, for example the workload's live
    `pod_selector`) replace the ones the file names."""
    built = registry.generator(fix.kind).generate({**fix.candidate, **read}, fix.finding_id)
    return built == (fix.name, fix.text)


def path_fits(path: str, name: str, cluster: str) -> bool:
    """Whether the file's path can hold the fix `name` for `cluster`: named
    for it, under no dot-directory, and inside its cluster's own tree of the
    reference layout, `clusters/<name>/`, by the cluster's short name, the last
    segment of `project/location/name`. A shared directory could be applied to
    every cluster. The tree is matched by short name only, so a repository
    whose layout reuses one short name for two clusters cannot be told apart
    here."""
    parts = path.split("/")
    if parts[-1] != f"{name}.yaml" or any(part.startswith(".") or not part for part in parts):
        return False
    return len(parts) > 2 and parts[0] == CLUSTER_TREE_ROOT and parts[1] == cluster.rsplit("/", 1)[-1]
