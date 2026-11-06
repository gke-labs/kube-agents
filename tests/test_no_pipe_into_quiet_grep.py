"""No script feeds `grep -q` through a pipe where pipefail is on.

`grep -q` exits at its first match. A producer that is still writing when
that happens takes SIGPIPE and exits 141, and under `set -o pipefail` the
pipeline's status is then the producer's. So `if producer | grep -q X` takes
the no-match branch although X was there, and under `set -e` a bare one stops
the script. The producer loses only if it writes again after the line that
matched. A multi-line value longer than one stdio buffer does that (bash's
printf and echo write through a 4 KiB buffer on Linux), and so do kubectl,
kind and helm, which write as they go. Whether grep exits before that next
write is a matter of scheduling, so the failure comes and goes: the GitLab
lane's registration check in hack/ci-eval-pr.sh read a registered case as
unregistered on loaded CI runners (#4357).

The repair is a here-string, `grep -q PAT <<<"$text"` or
`grep -q PAT <<<"$(producer)"`: bash expands the word in full before grep
starts, so nothing is left writing when grep exits. `grep PAT file` reads the
file itself and needs no producer at all.

What is read, and why there:

  - every tracked `*.sh` file, the set `make shellcheck` reads (the byte-
    identical upstream copies under third_party/google-skills/ excepted).
    A script's pipefail can come from the script that sources it, so this
    does not decide it per file: every pipe into a quiet grep is flagged, and
    the ones left as they are sit in ALLOWED with the reason they cannot race;
  - every heredoc in a tracked `*.tf` file whose body turns pipefail on,
    which is how the bench stacks' local-exec provisioners run their shell;
  - every `run:` block in .github/workflows that runs under pipefail: a step
    whose shell, its own or its job's or workflow's default, is `bash`
    (GitHub runs that as `bash --noprofile --norc -eo pipefail {0}`) or a
    command line naming pipefail, or a block that turns pipefail on itself.
    A step with no shell anywhere runs as `bash -e {0}`, without pipefail;
  - every Makefile whose SHELL or .SHELLFLAGS turns pipefail on.

Not read: Dockerfiles (RUN goes through `/bin/sh -c`, and none sets a
pipefail SHELL), Markdown, and shell held in Go strings. `head`, `grep -m`
and an `awk` that exits early close the pipe the same way and are not looked
for. hack/ci-eval-pr.sh is not read either: see SKIPPED_SCRIPTS.
"""

import collections
import pathlib
import re
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The shell scripts read, as `git ls-files` pathspecs: the Makefile's
#: SHELLCHECK_PATHSPEC and SHELLCHECK_SKIP_PATHSPEC.
_SHELL_PATHSPECS = ("*.sh", ":!third_party/google-skills/*")

#: Scripts this guard does not read, each with the test module that owns the
#: rule for it. #4372 converts hack/ci-eval-pr.sh's pipelines (#4357, #5022)
#: and adds a guard over that whole script to scripts/test_ci_eval_nightly.py.
#: Reading the script here as well would fail on main until #4372 merges, and
#: would hold one file to two grammars after.
SKIPPED_SCRIPTS = {
    "hack/ci-eval-pr.sh": "scripts/test_ci_eval_nightly.py",
}

_TERRAFORM_PATHSPECS = ("*.tf",)
_WORKFLOW_PATHSPECS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
_MAKEFILE_PATHSPECS = ("Makefile", "*/Makefile", "*.mk")

#: The step shell GitHub runs with pipefail: the bare keyword. A custom
#: command line (one with `{0}`) runs exactly as written.
_PIPEFAIL_SHELL_KEYWORD = "bash"
_PIPEFAIL_WORD = "pipefail"

#: Floors on what one scan reads, well under the tree's counts, so a walk
#: that silently reads nothing cannot pass.
_MIN_SHELL_FILES = 100
_MIN_PIPEFAIL_HEREDOCS = 10
_MIN_PIPEFAIL_RUN_BLOCKS = 1

#: Where bash lets a word begin, so where a `#` starts a comment: after
#: whitespace or after one of the operator characters `;`, `&`, `|`, `(`, `)`.
_COMMENT_CAN_FOLLOW = frozenset(" \t;&|()")

#: One pipe (`|` or `|&`, not `||`), optionally a newline, escaped or not, then
#: grep under any of its spellings (`command grep`, an env assignment in front, `egrep`,
#: `fgrep`), then any arguments up to a quiet spelling: `-q` anywhere in a
#: short-option cluster, or the long forms. The argument walk crosses a quoted
#: token whole, so `-E '(a|b)' -q` is read to its `-q`, and otherwise stops at
#: `|`, `;`, `&` and a closing paren, so a `-eq` after `$(... | grep -c .)` is
#: not read as grep's. Each token is a run of quoted strings and bare
#: characters that are never quotes, so the walk reads a token one way only and
#: cannot backtrack exponentially; the short-option cluster names its first `q`
#: for the same reason.
PIPE_INTO_GREP_QUIET = re.compile(
    r"(?<!\|)\|(?!\|)&?(?:\s|\\\n)*(?:command[ \t]+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*[ \t]+)*[ef]?grep"
    r"(?:[ \t]+(?:'[^']*'|\"[^\"]*\"|[^|;&()\s'\"])+)*?"
    r"[ \t]+(?:-[a-pr-zA-Z]*q[a-zA-Z]*|--quiet|--silent)(?=[\s;)]|$)"
)

#: A `set` that turns pipefail on: `set -o pipefail`, `set -euo pipefail`,
#: `set -e -o pipefail`; not `set +o pipefail`.
_SETS_PIPEFAIL = re.compile(r"^[ \t]*set[ \t]+(?:-[A-Za-z]+[ \t]+)*-[A-Za-z]*o[ \t]+pipefail\b", re.M)

#: A Terraform heredoc: `<<EOT` or `<<-EOT` to the line holding the marker
#: alone. A shell heredoc nested in it names another marker, so the first
#: line that is the outer marker alone closes the outer one.
_TERRAFORM_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\n(.*?)\n[ \t]*\1[ \t]*$", re.S | re.M)

#: A Makefile line that runs its recipes under pipefail.
_MAKEFILE_PIPEFAIL = re.compile(r"^[ \t]*\.?SHELL(?:FLAGS)?[ \t]*[:+?]?=.*\bpipefail\b", re.M)

# Why a pipeline left as it is cannot race. grep reports a match only once it
# has read the end of the line that matched, so a producer that prints one
# line has nothing left to write by then.
_ONE_LINE = "the producer prints one line, so grep has read its last byte before it can match"
_NO_PIPEFAIL = "never runs under pipefail, so the pipeline's status is grep's"

#: Pipelines into a quiet grep that stay as they are: (file, a fragment of the
#: pipeline's line) -> why. Each entry matches one pipeline, so a second copy
#: of an allowed line is flagged.
ALLOWED = {
    ("hack/check-docs-terminology.sh", """printf '%s\\n' "$HIT_TEXT" | grep -qiE "$MINTY_SUBJECT\""""):
        _ONE_LINE + ": HIT_TEXT is one line of `search` output, read by `read -r`",
    ("hack/check-docs-terminology.sh", """printf '%s\\n' "$HIT_TEXT" | grep -qF "$REQUIRED\""""):
        _ONE_LINE + ": HIT_TEXT is one line of `search` output, read by `read -r`",
    ("scripts/pr_evidence_screenshot.sh", """git remote get-url "$r" | grep -q 'gke-labs/kube-agents'"""):
        _ONE_LINE + ": `git remote get-url` prints one URL",
    ("scripts/pr_evidence_screenshot.sh", """git remote get-url "$remote" | grep -q 'gke-labs/kube-agents'"""):
        _ONE_LINE + ": `git remote get-url` prints one URL",
    ("agentplugins/gke-stockout-investigator/scenarios/lib/common.sh", """-o jsonpath='{.status.phase}' 2>/dev/null | grep -q Terminating"""):
        _ONE_LINE + ": a namespace's phase is one word",
    ("scripts/dev/dev_rebuild_agent.sh", """echo "$container_names" | grep -Fxq "${proxy_container}\""""):
        _NO_PIPEFAIL + ": the script sets only -e, and `make dev-rebuild-agent` executes it",
    ("agents/platform/scripts/cluster_preflight.sh", """printf '%s' "$ERR" | grep -Eiq "$CONNECTIVITY_FAILURE_RE\""""):
        _NO_PIPEFAIL + ": the script sets only -u, and the agent runs it as `bash cluster_preflight.sh`",
    ("agents/platform/scripts/cluster_preflight.sh", """printf '%s' "$ERR" | grep -Eiq "$NOT_A_CONNECTION_FAILURE_RE\""""):
        _NO_PIPEFAIL + ": the script sets only -u, and the agent runs it as `bash cluster_preflight.sh`",
    ("agents/platform/skills/gke-cluster-autoscaler/assets/log-autoscaler-events.sh", """| grep -q "roles/logging.viewer\\|roles/owner\\|roles/editor\""""):
        _NO_PIPEFAIL + ": the script sets no options; it is generated from google/skills",
    ("agents/platform/skills/gke-compute-classes/assets/log-autoscaler-events.sh", """| grep -q "roles/logging.viewer\\|roles/owner\\|roles/editor\""""):
        _NO_PIPEFAIL + ": the script sets no options; it is generated from google/skills",
    ("bench/upgrade-scenarios/common.sh", """tail -4 "$EVID/$sc.txt" | grep -q "incompatible operation" ||"""):
        _NO_PIPEFAIL + ": retry_busy runs from run.sh and hold.sh, which set only -u; compat-probe.sh sources this file under pipefail and calls neither function",
    ("bench/upgrade-scenarios/common.sh", """tail -4 "$EVID/$sc.txt" | grep -q "incompatible operation" &&"""):
        _NO_PIPEFAIL + ": attempt_refusal runs from run.sh, which sets only -u; compat-probe.sh sources this file under pipefail and calls neither function",
    ("bench/upgrade-scenarios/scenarios/19.sh", """tail -4 "$EVID/csi.txt" | grep -q "incompatible operation\""""):
        _NO_PIPEFAIL + ": run.sh and hold.sh, the two that source it, set only -u",
    ("bench/upgrade-scenarios/scenarios/19.sh", """tail -4 "$EVID/csi.txt" | grep -Eq "STOCKOUT|RESOURCE_POOL_EXHAUSTED\""""):
        _NO_PIPEFAIL + ": run.sh and hold.sh, the two that source it, set only -u",
    ("bench/tf/prebuilt/gitops-fix-cycle/scripts/setup.sh", """bash ${SANDBOX_PREFLIGHT_SCRIPT} --json | grep -q"""):
        _NO_PIPEFAIL + ": it runs in the sandbox pod's `bash -lc \"set -e; ...\"`, not in this script's shell",
    ("bench/tf/prebuilt/gitops-fix-cycle/scripts/setup.sh", """bash /opt/data/scripts/cluster_preflight.sh --json | grep -q"""):
        _NO_PIPEFAIL + ": it runs in the gateway pod's `sh -c \"set -e; ...\"`, not in this script's shell",
}

Hit = collections.namedtuple("Hit", "path line text")


def shell_without_comments(text: str) -> str:
    """`text` with every comment cut: a `#` that begins a word (at the start
    of a line, after whitespace, or after `;`, `&`, `|`, `(` or `)`), outside
    single and double quotes, to the end of its line. `${#arr[@]}` and a `#`
    inside a sed script survive, since neither begins a word. Inside `$'...'`
    a backslash escapes the next character, so `$'a\\' # b'` is one word and
    not a comment.

    The scan is per line, so the line numbers survive and quote state does not
    carry over a newline: a heredoc body can hold an unbalanced quote that
    would desynchronise everything after it. A `#` on a later line of a
    multi-line quoted program is therefore cut as a comment, which loses only
    text that is not shell."""
    kept = []
    for line in text.split("\n"):
        in_double = in_single = ansi_c = dollar_before = False
        cut = None
        i = 0
        while i < len(line):
            char = line[i]
            if char == "\\" and (not in_single or ansi_c):
                i += 2
                dollar_before = False
                continue
            if char == '"' and not in_single:
                in_double = not in_double
            elif char == "'" and not in_double:
                in_single = not in_single
                ansi_c = in_single and dollar_before
            elif char == "#" and not in_double and not in_single and (i == 0 or line[i - 1] in _COMMENT_CAN_FOLLOW):
                cut = i
                break
            dollar_before = char == "$" and not in_single and not in_double
            i += 1
        kept.append(line if cut is None else line[:cut])
    return "\n".join(kept)


def pipes_into_grep_quiet(shell: str) -> "list[tuple[int, str]]":
    """(1-based line, that line stripped) for each pipe into a quiet grep in
    `shell`; the line is the one the pipe sits on."""
    lines = shell.split("\n")
    stripped = shell_without_comments(shell)
    found = []
    for match in PIPE_INTO_GREP_QUIET.finditer(stripped):
        number = stripped.count("\n", 0, match.start()) + 1
        found.append((number, lines[number - 1].strip()))
    return found


def pipefail_heredocs(terraform: str) -> "list[tuple[int, str]]":
    """(1-based line of the body's first line, body) for each heredoc in
    `terraform` whose body turns pipefail on."""
    return [
        (terraform.count("\n", 0, match.start(2)) + 1, match.group(2))
        for match in _TERRAFORM_HEREDOC.finditer(terraform)
        if _SETS_PIPEFAIL.search(match.group(2))
    ]


def _runs_with_pipefail(shell) -> bool:
    if shell is None:
        return False
    shell = str(shell).strip()
    return shell == _PIPEFAIL_SHELL_KEYWORD or _PIPEFAIL_WORD in shell


def pipefail_run_blocks(workflow: dict) -> "list[tuple[str, str]]":
    """(job/step label, run text) for each `run:` block of a parsed workflow
    that runs under pipefail."""
    def default_shell(node):
        return ((node.get("defaults") or {}).get("run") or {}).get("shell")

    blocks = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            shell = next(
                (s for s in (step.get("shell"), default_shell(job), default_shell(workflow)) if s is not None),
                None,
            )
            if _runs_with_pipefail(shell) or _SETS_PIPEFAIL.search(run):
                blocks.append((f"{job_id}/{step.get('name') or index}", run))
    return blocks


def _tracked(root: pathlib.Path, pathspecs) -> "list[str]":
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return sorted(path for path in out.split("\0") if path)


def scan(root: pathlib.Path):
    """Every pipe into a quiet grep the guard reads, and how much it read."""
    hits = []
    read = collections.Counter()
    for path in _tracked(root, _SHELL_PATHSPECS):
        if path in SKIPPED_SCRIPTS:
            continue
        read["shell files"] += 1
        text = (root / path).read_text(encoding="utf-8")
        hits += [Hit(path, number, line) for number, line in pipes_into_grep_quiet(text)]
    for path in _tracked(root, _TERRAFORM_PATHSPECS):
        for first_line, body in pipefail_heredocs((root / path).read_text(encoding="utf-8")):
            read["pipefail heredocs"] += 1
            hits += [Hit(path, first_line + number - 1, line) for number, line in pipes_into_grep_quiet(body)]
    for path in _tracked(root, _WORKFLOW_PATHSPECS):
        for label, run in pipefail_run_blocks(yaml.safe_load((root / path).read_text(encoding="utf-8")) or {}):
            read["pipefail run blocks"] += 1
            hits += [Hit(f"{path} ({label}, run line {number})", None, line) for number, line in pipes_into_grep_quiet(run)]
    for path in _tracked(root, _MAKEFILE_PATHSPECS):
        text = (root / path).read_text(encoding="utf-8")
        if _MAKEFILE_PIPEFAIL.search(text):
            read["pipefail makefiles"] += 1
            hits += [Hit(path, number, line) for number, line in pipes_into_grep_quiet(text)]
    return hits, read


def _file_of(hit: Hit) -> str:
    return hit.path.split(" (", 1)[0]


def triage(hits):
    """(hits no ALLOWED entry covers, ALLOWED entries that cover no hit)."""
    unused = list(ALLOWED)
    flagged = []
    for hit in hits:
        entry = next((key for key in unused if key[0] == _file_of(hit) and key[1] in hit.text), None)
        if entry is None:
            flagged.append(hit)
        else:
            unused.remove(entry)
    return flagged, unused


class NoPipeIntoQuietGrepTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits, cls.read = scan(_REPO_ROOT)
        cls.flagged, cls.stale = triage(cls.hits)

    def test_nothing_pipes_a_producer_into_grep_q_under_pipefail(self):
        self.assertEqual(
            [],
            [f"{hit.path}{'' if hit.line is None else f':{hit.line}'}: {hit.text}" for hit in self.flagged],
            "a producer piped into a quiet grep loses to SIGPIPE when grep exits first, and pipefail "
            "turns that into the no-match branch. Feed grep from a here-string instead: "
            "`grep -q PAT <<<\"$text\"` or `grep -q PAT <<<\"$(producer)\"`. A pipeline that cannot race "
            "(its producer prints one line, or it never runs under pipefail) goes in ALLOWED with why.",
        )

    def test_every_allowed_pipeline_is_still_there(self):
        self.assertEqual(
            [],
            self.stale,
            "these ALLOWED entries match no pipeline any more; the line was converted or edited, "
            "so remove the entry, or re-check the edited line and update the fragment",
        )

    def test_the_scan_reads_the_tree(self):
        for what, floor in (
            ("shell files", _MIN_SHELL_FILES),
            ("pipefail heredocs", _MIN_PIPEFAIL_HEREDOCS),
            ("pipefail run blocks", _MIN_PIPEFAIL_RUN_BLOCKS),
        ):
            with self.subTest(what=what):
                self.assertGreaterEqual(
                    self.read[what], floor, f"the scan read {self.read[what]} {what}; a pathspec or reader broke"
                )

    def test_a_skipped_script_and_the_module_that_owns_it_exist(self):
        for script, owner in SKIPPED_SCRIPTS.items():
            with self.subTest(script=script):
                self.assertTrue((_REPO_ROOT / script).is_file(), f"{script} is gone; drop it from SKIPPED_SCRIPTS")
                self.assertTrue((_REPO_ROOT / owner).is_file(), f"{owner}, named as the owner of {script}, is gone")


class GrammarTest(unittest.TestCase):
    """The pipelines the scan must flag and the text it must leave alone, so a
    later edit to the pattern or the readers cannot quietly narrow them."""

    def test_the_grammar_reads_every_quiet_spelling_and_no_false_pipe(self):
        flagged = (
            "if ! printf '%s\\n' \"$a\" \"$b\" | grep -qxF -- \"$e\"; then",
            "x | grep -x -q -- y",
            "x | grep --quiet y",
            "x | grep --silent y",
            "x | grep -e pat -q",
            "x | grep -Eq 'y'",
            "x | grep -qs 'y'",
            "echo \"${#TASKS[@]} tasks\" | grep -q x",
            "sed 's/#.*$//' f | grep -q x",
            "v=\"$(printf x | grep -q .)\"",
            "if ! printf '%s\\n' \"$a\" |\n  grep -qxF -- \"$e\"; then",
            "kubectl logs pod |\\\n  grep -q fire",
            "until kubectl logs pod \\\n    --tail=-1 2>/dev/null \\\n  | grep -q \"fire \"; do",
            "x |& grep -q y",
            "x | grep -E '(a|b)' -q",
            "x | grep -F \"$(f)\" -q",
            "x | command grep -q y",
            "x | LC_ALL=C grep -q y",
            "x | egrep -q y",
            "x | fgrep -q y",
            "cut -f3 <<<\"$x\" | grep -qxF \"$b\"",
            "printf $'a\\' # b' | grep -q x",
            "x | grep -F\"$(f)\" -q",
            "x | grep --file=\"$f\" -q",
        )
        passed = (
            "cmd || grep -qxF -- x <<< y",
            "grep -qxF -- \"${ENTRY}\" <<<\"${PRESUBMIT_ENTRIES}\"$'\\n'\"${NIGHTLY_ENTRIES}\"",
            "if ! grep -q 'CA:TRUE' <<<\"$(openssl x509 -noout -text -in \"$f\" 2>/dev/null)\"; then",
            "until grep -q \"fire \" <<<\"$(kubectl logs pod --tail=-1 2>/dev/null)\"; do",
            "foo <<< \"$x\"  # not printf | grep -q",
            "# A here-string, never `printf ... | grep -q`",
            "x | grep -c foo",
            "x | grep foo | wc -q",
            "[ \"$(printf x | grep -c .)\" -eq 1 ]",
            "[ \"$(printf x | grep -c '(a|b)')\" -eq 1 ]",
            "x |& grep -c y",
            "case $x in a|b) grep -q y <<< \"$z\" ;; esac",
            "x |\n  grep -c y",
            "echo done;# never printf | grep -q here",
            "echo done &# never printf | grep -q here",
            "(echo done)# never printf | grep -q here",
            "x |grep\t" + '""\t' * 40 + "y",
        )
        for text in flagged:
            with self.subTest(flagged=text):
                self.assertEqual(len(pipes_into_grep_quiet(text)), 1)
        for text in passed:
            with self.subTest(passed=text):
                self.assertEqual(pipes_into_grep_quiet(text), [])

    def test_a_hit_names_the_line_the_pipe_is_on(self):
        self.assertEqual(
            pipes_into_grep_quiet("set -euo pipefail\nkubectl logs pod \\\n  | grep -q fire\n"),
            [(3, "| grep -q fire")],
        )

    def test_only_heredocs_that_turn_pipefail_on_are_read(self):
        terraform = (
            'resource "a" "b" {\n'
            '  provisioner "local-exec" {\n'
            '    interpreter = ["/bin/bash", "-c"]\n'
            "    command     = <<-EOT\n"
            "      set -euo pipefail\n"
            "      kubectl apply -f - <<'MANIFEST'\n"
            "      kind: Namespace\n"
            "      MANIFEST\n"
            '      x | grep -q "y"\n'
            "    EOT\n"
            "  }\n"
            '  provisioner "local-exec" {\n'
            "    command = <<EOT\n"
            '      x | grep -q "z"\n'
            "    EOT\n"
            "  }\n"
            '  provisioner "local-exec" {\n'
            "    command = <<-EOT\n"
            "      set -eu\n"
            "      set +o pipefail\n"
            '      x | grep -q "w"\n'
            "    EOT\n"
            "  }\n"
            "}\n"
        )
        heredocs = pipefail_heredocs(terraform)
        self.assertEqual([first for first, _ in heredocs], [5])
        self.assertEqual(pipes_into_grep_quiet(heredocs[0][1]), [(5, 'x | grep -q "y"')])

    def test_only_run_blocks_under_pipefail_are_read(self):
        workflow = yaml.safe_load(
            "defaults: {run: {shell: sh}}\n"
            "jobs:\n"
            "  plain:\n"
            "    steps:\n"
            "      - {name: inherits sh, run: 'x | grep -q a'}\n"
            "      - {name: explicit bash, shell: bash, run: 'x | grep -q b'}\n"
            "      - {name: own set, run: \"set -euo pipefail\\nx | grep -q c\"}\n"
            "      - {name: custom bash, shell: 'bash -e {0}', run: 'x | grep -q d'}\n"
            "      - {name: custom pipefail, shell: 'bash -o pipefail {0}', run: 'x | grep -q e'}\n"
            "  defaulted:\n"
            "    defaults: {run: {shell: bash}}\n"
            "    steps:\n"
            "      - {name: job default, run: 'x | grep -q f'}\n"
        )
        self.assertEqual(
            [label for label, _ in pipefail_run_blocks(workflow)],
            ["plain/explicit bash", "plain/own set", "plain/custom pipefail", "defaulted/job default"],
        )
        self.assertEqual(pipefail_run_blocks(yaml.safe_load("jobs: {j: {steps: [{run: 'x | grep -q a'}]}}")), [])


if __name__ == "__main__":
    unittest.main()
