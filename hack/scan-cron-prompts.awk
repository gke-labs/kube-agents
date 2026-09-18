# Which `"prompt"` values in the documentation are claiming to quote the cron
# roster. Driven by hack/check-docs-terminology.sh, which passes the roster ids
# through `-v idfile=<path>` (one id per line) and the documents as operands.
#
# Prints one line per prompt occurrence it has an opinion about:
#
#   R:<file>:<line>:<text>   grade this against the roster, verbatim.
#   O:<file>:<line>:<text>   this renders a roster entry that names no id the
#                            roster knows. Report it; it cannot be graded.
#
# A prompt this prints nothing for is deliberately none of the guard's business.
#
# --- Why a fence, and why a block ------------------------------------------
#
# Only text inside a fence is a rendered manifest. Prose that happens to spell a
# JSON key is a sentence about a manifest, and grading it is how this scan
# blocks a pull request that did nothing wrong: `concepts/governance-sops.md`
# carries no fence at all and already writes `"skills": ["fleet-audit"]` in a
# sentence, so one more sentence mentioning `"prompt": "` anywhere in that file
# would have made the whole document look like one malformed roster entry.
#
# Deciding per block rather than per document is the other half of that. A page
# that named a job in one section and rendered an unrelated `"prompt"` in
# another used to fail CI with an error about cron prompts, pointing at a block
# that was never claiming to quote anything.
#
# `O` closes the hole on the other side. Eliding the `"id"` line from a rendered
# entry used to remove it from the check entirely -- no error, no coverage -- so
# deleting one line was all it took to silence the guard on a quotation. A block
# that still looks like a roster entry has to name a job the roster knows.
#
# "Looks like a roster entry" is decided by the keys only a roster entry has:
# `schedule`, `skills`, `deliver`, `no_agent`, `risk`. An unknown `"id"` counts
# as a renamed job only beside one of those; on its own it is what a bench
# scenario, an A2A payload or a tool definition carries beside a `"prompt"`,
# and `"name"` and `"enabled"` are too common to tell those apart from a roster
# entry, so none of them is a signal. The residual hole is a quotation trimmed
# to `"id"` and `"prompt"` alone whose id has since been renamed; the shipped
# pages quote whole entries, and the wrapper's error names the placeholder
# spelling for anything that is an illustration rather than a quotation.
#
# A block earns `R` by naming a roster id, or -- failing that -- by carrying
# nothing but a prompt in a document where some other fenced block does name
# one. That fallback covers a quotation trimmed down to the prompt alone: no id,
# no sibling keys, nothing for either rule above to catch, so deciding purely
# per block left it graded by nothing at all. It is confined to prompt-only
# blocks because a block carrying other keys is some other object that happens
# to have a prompt -- a LiteLLM request body is `{"model": …, "prompt": …}` --
# and confined to documents that render a roster entry because a page that never
# renders one is not quoting one.
#
# --- Why the matching looks like this ---------------------------------------
#
# The ids are compared whole, here in awk, and never become part of a pattern.
# Interpolated into an alternation instead, a single `(`, `|` or `+` in a job id
# made `grep -E` reject the pattern outright; the error went to /dev/null, the
# status went to `|| true`, and the guard reported PASS having read nothing.
#
# Matching a key wherever on the line it falls and however it is spaced is
# deliberate. An anchor that insisted on the key starting the line, followed by
# exactly one space, saw only prettier's rendering of a multi-line object -- a
# one-line `{"id": …, "prompt": …}` entry, or a hand-spaced one, went unchecked,
# which is the same silent skip the structural anchor was adopted to end.
# Prettier normalises much of this back, but only inside a fence tagged `json`,
# and CI does not run it over `.mdx` at all. What no line-based anchor can see
# is a quotation whose value sits on the line after its key; those are
# unchecked.
#
# tests/test_docs_terminology_guard.py drives this file directly.

# An unknown id anywhere in the block refuses the whole block, known ids
# beside it or not. A fence excerpting two entries, one current and one
# renamed, used to classify as `R` on the strength of the current one, and the
# renamed entry's prompt was graded verbatim while its stale id went unreported
# -- the exact identifier the `O` rule exists to refuse. The two entries are one
# excerpt and one edit fixes it, so refusing both lines is the honest report.
#
# A placeholder id in angle brackets -- `"id": "<your-audit>"` -- marks an
# illustration, not a quotation: a how-to has no other way to show the shape of
# an entry without naming a job. A block whose only ids are placeholders is
# left ungraded and unreported. It ranks below a real or an unknown id on
# purpose: a placeholder pasted into a fence that also quotes a job must not
# switch that quotation's grading off, so such a block is graded (or refused)
# as if the placeholder were not there. The error the guard prints for an
# orphan names this spelling, so a contributor who meant an example is told
# how to write one.
function flush(   i, kind) {
  kind = (badid && cronish) ? "O" : (hasid ? "R" : (placeholder ? "" : (cronish ? "O" : (otherkey ? "" : "?"))))
  for (i = 1; i <= n; i++) {
    m++; qkind[m] = kind
    qfile[m] = pfile[i]; qline[m] = pline[i]; qtext[m] = ptext[i]
  }
  n = 0; hasid = 0; badid = 0; placeholder = 0; cronish = 0; otherkey = 0
}

# Held to end of file because the prompt-only fallback needs to know whether any
# block anywhere in the document named a roster id, which a block that comes
# first cannot know yet.
function endfile(   i, kind) {
  flush()
  for (i = 1; i <= m; i++) {
    kind = qkind[i]
    if (kind == "?") kind = dochasid ? "R" : ""
    if (kind != "")
      printf "%s:%s:%s:%s\n", kind, qfile[i], qline[i], qtext[i]
  }
  m = 0; dochasid = 0; infence = 0
}

# The `idfile` guard is not redundant with the caller's. Driven without one,
# BSD awk and gawk abort, but mawk reads the empty string as a missing file,
# leaves `ids` empty, and reports every rendered entry as an orphan -- a full
# page of failures that look like findings. A wrapper that forgot to pass the
# roster should be told so, not graded against nothing.
BEGIN {
  if (idfile == "") {
    print "scan-cron-prompts.awk: -v idfile=<path> is required" > "/dev/stderr"
    exit 2
  }
  while ((getline id < idfile) > 0) if (id != "") ids["\"" id "\""] = 1
}

FNR == 1 { endfile() }

# `>` so a fence inside a blockquote still opens and closes a block. Without it
# two blockquoted manifests were one block, and the id in the first graded the
# prompt in the second.
#
# A fence closes only on the character that opened it, at the opener's length
# or longer, as CommonMark has it. Toggling on every marker line read a
# four-backtick block that shows a single ```` ``` ```` line -- the way a page
# demonstrates a fence -- as two fences, and left the scan inverted for the
# rest of the file: every real fence after it scanned as prose, every prose
# paragraph as fenced, and a roster quotation further down never graded.
/^[ \t>]*(```+|~~~+)/ {
  marker = $0; sub(/^[ \t>]*/, "", marker)
  fchar = substr(marker, 1, 1); flen = 0
  while (substr(marker, flen + 1, 1) == fchar) flen++
  if (!infence) { flush(); infence = 1; openchar = fchar; openlen = flen; next }
  if (fchar == openchar && flen >= openlen) { flush(); infence = 0; next }
  # A shorter or different marker inside an open fence is content, not a close.
  next
}

{
  if (!infence) next
  rest = $0
  while (match(rest, /"id"[ \t]*:[ \t]*"[^"]*"/)) {
    # RSTART/RLENGTH are read into locals before the sub() below, which POSIX
    # does not say preserves them. gawk and onetrue awk do; mawk is what CI
    # runs, and a scan that silently stopped after the first id on a line would
    # grade a one-line entry by nothing.
    s = RSTART; l = RLENGTH
    seg = substr(rest, s, l)
    sub(/^"id"[ \t]*:[ \t]*/, "", seg)
    # An id the roster does not know is a renamed or mistyped job when a
    # roster-only key stands beside it, whether or not a known id shares the
    # block (see `flush`); without one it is some other object's id.
    if (seg ~ /^"<[^"<>]+>"$/) placeholder = 1
    else if (seg in ids) { hasid = 1; dochasid = 1 }
    else badid = 1
    rest = substr(rest, s + l)
  }
  # The keys only a roster entry has; see the header for why `name` and
  # `enabled` are not among them. An entry trimmed to these with the `id` line
  # dropped used to set `otherkey` alone and fall out of the scan ungraded and
  # unreported -- the shape `O` exists for.
  if ($0 ~ /"(schedule|skills|deliver|no_agent|risk)"[ \t]*:/) cronish = 1
  # Any JSON key on the line other than `prompt`, which switches off the
  # document-level fallback for this block. A `":"` inside the prompt text
  # itself reads as a key here and switches it off too, which is the behaviour
  # this had before the fallback existed.
  t = $0
  gsub(/"prompt"[ \t]*:/, "", t)
  if (t ~ /"[^"]+"[ \t]*:/) otherkey = 1
  if ($0 ~ /"prompt"[ \t]*:[ \t]*"/) {
    n++; pfile[n] = FILENAME; pline[n] = FNR; ptext[n] = $0
  }
}

END { endfile() }
