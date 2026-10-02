"""Reshape the first inventory report for chat behind ``KAGE_SLACK_UX``.

``bootstrap_delivery.py`` prints ``INVENTORY.md`` as one cron message, which
reaches Slack as text: the only Block Kit it carries is what the adapter's
markdown renderer draws, so there are no buttons and no collapsible container.
``present`` gives a shorter text layout instead, with one number on it: the
total. Its headline is written here, not taken from the posture: "I scanned
3 clusters and 41 workloads and found 22 things to look at.", naming only
the counts the posture states in a sentence saying what was scanned ("3 of 5
clusters" is 3), then a lead
("Two are worth fixing first:") when the total is more than the rows
shown. The part of the posture naming what was not scanned ("2 clusters could
not be scanned (permission denied)") is kept, as written, on its own line under
the headline, since a silent gap reads as clean; a sentence it shares with the
scan counts is split at its clauses, so the counts stay in the headline. A gap
named after the list joins it from the roll-up, which both layouts leave out,
and on the card from the closing lines too, which only the text keeps. The top two findings and every
finding labelled critical follow (the whole list when it runs past the SOP's
five, which the SOP allows only for an all-critical list), a row each, led by its severity as inline
code when the report labels a finding's severity. Each finding keeps its
headline and, on the line under it, the sentence the report wrote there,
unchanged. The total is the listed items plus the roll-up's count, the
roll-up being the first paragraph after the list that counts "<n> more" or
"Also found", else the first whose count reads as more: a count of a
severity ("2 high"), or one beside "also", "plus", "other", "remaining",
"lower-priority" ("plus 18", "the other 3"), or "also" or "plus" up to three words before a
findings count that is not of the listed ones ("I also flagged 18 issues",
"Plus, I found 18 issues", "18 issues also need attention") or "remain" ending its clause ("18 findings remain, mostly low"), so
"These 2 findings are the only ones; ask me for more detail" restates the list.
A bare "22 findings" is otherwise a closing line's, not a roll-up's, unless a
colon follows it ("4 issues in 2 namespaces: ...") and the count is not an offer's ("I can open 2
issues: one per cluster"). A count it states for the whole roll-up
wins ("18 more items: 2 high, 16 low" is 18, not 36): its "<n> more" unless,
within two words and past no preposition or article, a scope noun follows it
with its own findings count ("2 more clusters with 9 findings" counts
clusters) or with its state ("2 more clusters could not be scanned" counts
none); the "<n>" of "Also found: <n>" or "Other findings: <n>" unless such a
scope noun follows it with a findings count ("Also found: 2 clusters with ..."
counts clusters) or with its state, or a first term that heads the rest ("18
items: 2 high, 16 low"). Only with none are its terms ("2 high, 1 medium
and 1 low") summed. With no
roll-up, the total is a closing line's "all <n> findings", then the
posture's "<n> findings" after a scan verb, then the largest of the closing
lines' "<n> findings in total" (one may count one cluster), then any other
"<n> findings" in the posture, then the listed items. The rest of the
findings, the rest of the posture and the roll-up paragraph are left out;
every other closing line is kept as written, counts and all, since they are
how a text reader asks for the rest. When the roll-up was the last of them,
"Ask me to see all <n>." takes its place.

``blocks`` is the same card as Block Kit, which ``bootstrap_delivery``
posts itself when it can: the headline, lead and top rows with no count
above them, then a primary "Fix the first one" button and, when the total is
more than the rows shown, a "See all N" button, both answered as the
clicker's turn. The primary button's value names the first row as the card
shows it ("Fix the first one: <finding>"), so a click handler that sends the
value as the turn tells the agent which finding without its reading the
thread. The closing lines are left out too: "See all N" asks for the
rest.

The report is model-written to the format in
``agents/platform/governance/inventory_prioritize_sop.md`` (Step 6). A report
that does not parse to that shape is returned unchanged.
"""

import re

from slack_presenter import as_line, blocks_report, fallback_text, gap_parts, severity_row, shown_text

TOP_COUNT = 2
#: The most items the SOP lists (Step 5) unless every one is critical: a longer
#: list is all criticals, labelled or not, and criticals are never capped.
SOP_LIST_CAP = 5
BOLD_MARK = "**"
PARAGRAPH_BREAK = "\n\n"
#: Severities that earn the "worth fixing" lead; criticals are never rolled up.
CRITICAL = "critical"
URGENT_SEVERITIES = frozenset({CRITICAL, "major"})
#: A bold span ending in one of these is a whole headline; the text after it
#: on the line is the sentence.
SENTENCE_PUNCTUATION = ".!?"

HEADING = re.compile(r"^#{1,6}\s")
ITEM_START = re.compile(r"^\d+[.)]\s+(.*)$")
#: The shape check for an item line: it opens with a bold span.
BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")
#: A numbered bold item inside a paragraph: the list did not parse as one.
INLINE_ITEM = re.compile(r"\d+[.)]\s+\*\*")

#: A leading "Critical:", "Critical —", "Critical –" or "Critical - " label, bold or plain; not "Critical-path".
SEVERITY_LABEL = re.compile(r"^(critical|major|minor)(?:\s*[:—–]|\s+-(?=\s))\s*", re.IGNORECASE)
#: "[critical]" or "(critical)" anywhere in the headline. It starts only where a whitespace
#: run does, so a long run is not rescanned from each of its spaces.
SEVERITY_TAG = re.compile(r"(?<!\s)\s*[\[(](critical|major|minor)[\])]\s*", re.IGNORECASE)

#: The Block Kit report.
ACTION_ID_PREFIX = "kage_inventory"
#: The card's headline, from the posture's own counts; a count it does not state is left out.
SCANNED_TEMPLATE = "I scanned {scanned} and found {found}"
FOUND_TEMPLATE = "I found {found}"
SCANNED_JOIN = " and "
FINDINGS_NOUN = ("thing to look at", "things to look at")
CLUSTERS_NOUN = ("cluster", "clusters")
WORKLOADS_NOUN = ("workload", "workloads")
#: The headline ends on a full stop before a lead, and on a colon straight into the rows.
LEADS_ON = "."
ROWS_FOLLOW = ":"
CARD_LEAD_TEMPLATE = "{count} are worth fixing first:"
CARD_LEAD_ONE = "One is worth fixing first:"
CARD_NEUTRAL_LEAD_TEMPLATE = "Start with these {count}:"
CARD_NEUTRAL_LEAD_ONE = "Start with this one:"
FIX_FIRST = "Fix the first one"
FIX_ONLY = "Fix it"
#: The primary button's value: its label naming the first row, as the click's turn.
FIX_TURN = "{label}: {finding}"
SEE_ALL = "See all {count}"
#: Counts written as words in a lead; past the last, the digits.
COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
#: Number words a posture may use for a count.
NUMBER_WORDS = {word: n for n, word in [(1, "one"), *COUNT_WORDS.items()]}
#: A whole number: not the tail of "1.30", "v2" or "2,500".
WHOLE = r"(?<![\w.,-])"
COUNT = WHOLE + r"(\d+|" + "|".join(NUMBER_WORDS) + r")\b"
#: The fleet a partial scan counts out of: "3 of 5 clusters" scanned 3.
OUT_OF = r"(?:\s+(?:out\s+)?of\s+(?:\d+|" + "|".join(NUMBER_WORDS) + r")\b)?"
#: "3 clusters", "three GKE clusters", "3 of 5 clusters"; "41 workloads". Read
#: only in a clause that says what was scanned, and only when every such clause agrees.
CLUSTER_COUNT = re.compile(COUNT + OUT_OF + r"\s+(?:GKE\s+)?clusters?\b", re.IGNORECASE)
WORKLOAD_COUNT = re.compile(COUNT + OUT_OF + r"\s+workloads?\b", re.IGNORECASE)
#: A posture clause: split at a semicolon or a sentence end, not at "e.g. prod".
CLAUSE_END = re.compile(r"[;!?]\s+|\.\s+(?=[A-Z0-9])")
GAP_SEPARATOR = ", "
SCAN_VERB = re.compile(
    r"\b(?:scanned|scanning|checked|covered|covering|across|inventoried|reviewed|examined|looked at)\b",
    re.IGNORECASE,
)
#: The posture's own total, used only when no roll-up or closing line counts.
POSTURE_TOTAL = re.compile(WHOLE + r"(\d+)\s+findings\b", re.IGNORECASE)
#: "2 high-priority", "3 medium-severity": still one severity's count.
SEVERITY_SUFFIX = r"(?:-(?:priority|severity|risk))?"
#: A findings noun, after at most two words: "items", "lower-priority findings".
FINDINGS_WORD = r"(?:[a-z-]+\s+){0,2}?(?:findings?|items?|issues?|problems?)\b"
#: What a roll-up may count before its findings: "2 clusters with 9 findings".
SCOPE_NOUN = r"(?:clusters?|namespaces?|nodes?|workloads?|projects?)\b"
#: Up to two words between a count and its scope noun, none a preposition or an
#: article: "2 more big clusters", not "18 more in other clusters" or "18 across
#: the clusters", whose 18 is not of clusters.
SCOPE_LEAD = r"(?:(?!(?:in|across|from|on|of|at|for|with|within|over|among|the|a|an|these|those|its|their)\s)[a-z-]+\s+){0,2}?"
#: After a count, a scope noun whose findings are counted later in the clause:
#: "2 (more) clusters with 9 findings" counts clusters. Bounded so a paragraph
#: of repeated "more clusters" is not scanned to its end once per count.
COUNTS_SCOPE = r"\s+" + SCOPE_LEAD + SCOPE_NOUN + r"[^.;]{0,200}?\b\d+\s+" + FINDINGS_WORD
#: A state that leaves a scope unscanned: "skipped", "unreachable", "still syncing".
SCAN_STATE = r"(?:unreachable|inaccessible|unavailable|unscanned|skipped|offline|pending|syncing|provisioning|initiali[sz]ing)"
#: What a negated verb says the scan could not do, the verbs of
#: slack_presenter's GAP among them: "could not be scanned", "could not be
#: read", "weren't reachable", "did not respond"; not "cannot enforce network policy".
NOT_SCANNED = (
    r"(?:scanned|reached|checked|read|contacted|inspected|inventoried|accessed|queried"
    r"|reachable|accessible|available|connect|respond|load|authenticate)(?![\w-])"
)
#: What may stand between the negation and its verb: "are not yet scanned",
#: "could not currently be reached", "were not able to be scanned".
NOT_BETWEEN = r"(?:\s+(?:yet|currently|fully|always))?(?:\s+able\s+to)?(?:\s+be(?:en)?)?\s+"
#: A scan still owed: "remain to be scanned", "still need to be scanned".
SCAN_OWED = r"(?:remain(?:s|ed)?|(?:still\s+)?needs?)\s+to\s+be\s+(?:scanned|checked|read|reached|inspected|inventoried)\b"
#: What "failed to" may go on to: "2 more clusters failed to scan" is a gap,
#: "4 more nodes failed to drain" a finding.
FAILED_TO = r"(?:scan|connect|respond|reach|load|list|query|authenticate|be)"
#: "failed to" a verb of the scan's own output: "failed to complete the scan",
#: "failed to return results".
FAILED_SCAN = r"failed\s+to\s+[a-z]+\s+(?:the\s+|any\s+)?(?:scan|results?|inventory)\b"
#: A failure is a state only where its clause ends or goes on to say why or when:
#: "4 more nodes failed." and "2 more clusters timed out after 60s" are gaps, "4
#: more nodes failed readiness probes" and "4 nodes: forbidden hostPath mounts" are
#: findings.
STATE_END = r"\b(?=\s*(?:[.;,:()\n\u2013\u2014-]|$|(?:to|with|after|during|and|or|for|because|due|on|while|when|at|since)\b))"
#: Left out of the scan: "were excluded from the scan", "were not in scope",
#: "were not part of this scan".
SCAN_EXCLUDED = (
    r"(?:not\s+part\s+of\s+(?:the|this)\s+(?:scan|scope)|excluded\s+from\s+(?:the\s+)?(?:scan|scope)|(?:not\s+in|outside(?:\s+of)?|out\s+of)\s+(?:the\s+)?(?:scan['’]?s?\s+)?scope)"
)
#: Where a scan state ends: as a failure's, but "for" only as a reason ("skipped
#: for lack of credentials"), so "4 more nodes are unavailable for scheduling" is
#: a finding.
SCAN_END = (
    r"\b(?=\s*(?:[.;,:()\n\u2013\u2014-]|$"
    r"|(?:to|with|after|during|and|or|because|due|on|while|when|at|since|for\s+(?:lack|want|now|the\s+scan))\b))"
)
#: Names after a scope noun, before its state: "2 more clusters (seeded-d,
#: seeded-e) timed out", "2 more clusters, seeded-d and seeded-e, timed out".
SCOPE_NAMES = r"(?:\s*\([^()\n]{1,200}\)|\s*,[^,.;:()\n]{1,200}?,)?"
#: The kind of error a scan returns: "403 Forbidden", "PERMISSION_DENIED".
ERROR_KIND = r"(?:permission|access|auth\w*|4\d\d(?:\s+[a-z]+){0,2}|[a-z]+(?:_[a-z]+)+)"
#: A server's error ("server", "500 internal server"), which a cluster also
#: returns to its own clients, so a gap only where its clause ends: "returned
#: 500 errors." but not "returned 500 errors to clients".
SERVER_KIND = r"(?:(?:internal\s+)?server|5\d\d(?:\s+[a-z]+){0,2})"
#: A status a scan returns with no "errors" after it: "403 Forbidden", "503 Service
#: Unavailable", "PERMISSION_DENIED"; a gap only where its clause ends, as a server's error.
#: Its reason is words, not a preposition or "errors", so "returned 503 to their clients"
#: and "returned 500 errors to clients" stay findings.
STATUS = r"(?:[45]\d\d(?:\s+(?!(?:to|for|from|on|in|at|by|with|via|their|its|the|errors?)\b)[a-z]+){0,3}|[a-z]+(?:_[a-z]+)+)"
#: An error is a scan's state for a cluster, namespace or project, which the scan
#: queries ("2 more clusters returned errors", "returned 403 Forbidden errors",
#: "returned PERMISSION_DENIED errors", "returned server errors"), and a
#: finding for a node, whose errors are its own ("4 more nodes returned errors").
#: Where an error ends as a state: "returned errors." and "errored during the
#: scan" are gaps, "returned errors on 3 workloads" a finding.
SCOPE_ERRORED = (
    r"(?:clusters?|namespaces?|projects?)" + SCOPE_NAMES + r"\s+(?:"
    r"(?:errored|returned\s+(?:an?\s+)?(?:" + ERROR_KIND + r"\s+)?errors?)"
    r"\b(?=\s*(?:[.;,:()\n\u2013\u2014-]|$|(?:to|after|during|because|due|while|when)\b))"
    r"|returned\s+(?:an?\s+)?(?:" + SERVER_KIND + r"\s+errors?|" + STATUS + r")\b(?=\s*(?:[.;,:()\n\u2013\u2014-]|$)))"
)
#: After "<n> more", "Also found: <n>" or "Other findings: <n>", what a scan
#: covers rather than a finding: "2 more clusters could not be scanned", "1 more
#: namespace is still syncing". The noun must say it was not scanned, so "18 more
#: node pool findings", "18 more in prod clusters", "4 more nodes are not ready"
#: and "2 more clusters can be upgraded" are still findings; a colon then a failure
#: ("2 more clusters: permission denied") is a state too. Not workloads, which a
#: roll-up counts as findings ("19 more workloads run as root").
MORE_SCOPE = (
    r"\s+" + SCOPE_LEAD + r"(?:" + SCOPE_ERRORED + r"|(?:clusters?|namespaces?|nodes?|projects?)" + SCOPE_NAMES
    + r"(?:\s+(?:(?:(?:could|did|do|does|is|are|was|were|has|have|had)(?:n['’]t|\s+not)|can(?:['’]t|not|\s+not))"
    + NOT_BETWEEN + NOT_SCANNED
    + r"|(?:(?:is|are|was|were)\s+)?" + SCAN_OWED
    + r"|(?:(?:is|are|was|were|remain(?:ed)?)\s+)?(?:still\s+)?(?:being\s+(?:set\s+up|provisioned|created|scanned|synced)|" + SCAN_STATE + r")" + SCAN_END
    + r"|" + FAILED_SCAN
    + r"|(?:(?:is|are|was|were)\s+)?" + SCAN_EXCLUDED + STATE_END
    + r"|(?:failed(?!\s+to\s+(?!" + FAILED_TO + r"\b))|timed\s+out)" + STATE_END + r")"
    r"|\s*:\s*(?:permission\s+denied|access\s+denied|forbidden|unreachable|skipped|offline|failed|timed\s+out|no\s+credentials)" + STATE_END + r"))\b"
)
#: One count in a roll-up: "18 more", "2 high", "19 lower-priority findings";
#: never "all 22 findings", which is a total, not more.
ROLLUP_TERM = re.compile(
    r"(?<!all )" + WHOLE + r"(\d+)\s+(?:"
    r"(?:critical|high|major|medium|moderate|minor|low)" + SEVERITY_SUFFIX + r"(?![\w-])"
    r"|" + FINDINGS_WORD + r"|more\b(?!" + COUNTS_SCOPE + r"|" + MORE_SCOPE + r"))",
    re.IGNORECASE,
)
#: A closing line's total: "See all 22 findings"; not "Fixing all 3 findings
#: clears 22 findings in total", where the sentence's own total is the 22.
ALL_TOTAL = re.compile(
    r"\ball\s+" + WHOLE + r"(\d+)\s+findings\b(?![^.!?\n]*\bfindings\s+in\s+total\b)", re.IGNORECASE
)
#: A closing line's weaker total, "I found 22 findings in total": it may count
#: one cluster ("seeded-a has 9 findings in total"), so the largest is taken,
#: and a posture total after a scan verb outranks it.
IN_TOTAL = re.compile(WHOLE + r"(\d+)\s+findings\s+in\s+total\b", re.IGNORECASE)
#: A count the roll-up states for all of itself: "18 more"; not "2 more
#: clusters with 9 findings", whose 2 counts clusters, nor "2 more clusters
#: could not be scanned", which counts no findings at all.
MORE_TOTAL = re.compile(WHOLE + r"(\d+)\s+more\b(?!" + COUNTS_SCOPE + r"|" + MORE_SCOPE + r")", re.IGNORECASE)
#: Words that read a count as more, beside it: "18 more", "plus 18", "the other 3".
MORE_WORD = r"(?:more|also|plus|other|another|further|additional|remaining|lower[- ]priority)"
#: The SOP's roll-up wording: "Also found: 18", "Also found: 18 items", and a
#: label like it, "Other findings: 18"; not "Also found: 2 high, ...", whose 2 is
#: one term of a breakdown, nor "Also found: 2 clusters with 9 findings", whose
#: 2 counts clusters. A later count that is not of findings ("19 workloads across
#: 3 clusters") leaves it standing.
ALSO_FOUND = re.compile(
    r"\b(?:also found|" + MORE_WORD + r"\s+" + FINDINGS_WORD + r")\s*(?::\s*)?(\d+)\b"
    r"(?!\s+(?:critical|high|major|medium|moderate|minor|low)" + SEVERITY_SUFFIX + r"(?![\w-]))"
    r"(?!" + COUNTS_SCOPE + r"|" + MORE_SCOPE + r")",
    re.IGNORECASE,
)
#: A paragraph whose count reads as more: only such a paragraph is a roll-up.
#: A word like "more" marks one only beside a count ("18 more", "plus 18") or
#: labelling one ("Other findings: 18"), not elsewhere ("These 2 findings ... ask me for more detail"), and "also found" always;
#: a severity marks one only as a count's ("2 high"), not as a word ("low risk");
#: a findings count marks one when a colon follows it ("4 issues in 2 namespaces: ..."),
#: within 200 characters, so a paragraph of counts with no colon is not scanned to its end at each.
#: The space around a label's colon is "\s*(?::\s*)?", not "\s*:?\s*", whose two
#: runs split one run of spaces every way before failing.
ROLLUP_MARK = re.compile(
    r"\b\d+\s+" + MORE_WORD + r"\b|\b" + MORE_WORD + r"(?:\s+" + FINDINGS_WORD + r")?\s*(?::\s*)?\d|\balso found\b"
    r"|\b\d+\s+(?:critical|high|major|medium|moderate|minor|low)" + SEVERITY_SUFFIX + r"(?![\w-])",
    re.IGNORECASE,
)
#: :data:`ROLLUP_MARK`'s colon form, which marks one only when the count is not
#: an offer's (:func:`_offered`): not "I can open 2 issues: one per cluster".
COLON_MARK = re.compile(WHOLE + r"\d+\s+" + FINDINGS_WORD + r"[^.:]{0,200}:", re.IGNORECASE)
#: A findings count, its digits in group "n".
LOOSE_COUNT = r"(?P<n>\d+)\s+" + FINDINGS_WORD
#: A roll-up worded as a sentence: "also" or "plus" up to one word before a
#: findings count ("I also flagged 18 issues", "18 issues also need
#: attention"), or up to three when the last is a verb of :data:`FOUND_VERB`
#: ("Plus, the scan surfaced 18 issues"; :func:`_loose_rollup` checks), not
#: across "to" ("I also want to fix 2 issues"), or "remain" or "remaining"
#: ending its clause ("18 findings remain, mostly low"). Not "I found 2
#: issues; ask me for more" or "2 issues remain open"; :func:`_loose_rollup`
#: rules out an offer and a count of the listed ones.
LOOSE_ALSO = re.compile(
    r"\b(?:also|plus),?\s+(?P<gap>(?:(?!to\b)[a-z]+\s+){0,3})\b" + LOOSE_COUNT,
    re.IGNORECASE,
)
LOOSE_AFTER = re.compile(
    r"\b" + LOOSE_COUNT + r"\s+(?:also\b|remain(?:s|ing)?(?=\s*(?:[.;,:()–—-]|$)))", re.IGNORECASE
)
#: Words up to two before a count, in its own clause, that make it the listed
#: ones: "these 2", "your top 2", "the same 2", "either of the 2"; not "Besides
#: these, 18 findings remain", whose "these" is a lead-in's. "All 2" is a total,
#: which :data:`ROLLUP_TERM` already passes over.
LISTED_NEAR = frozenset({"these", "those", "both", "the", "your", "top", "first", "same", "above"})
#: A clause just before a count that picks it out of the listed ones: "Of
#: these, 2 issues also block the upgrade", "Among those, ..."; not "On top of
#: these," or "Regardless of those,", whose clause starts before "of".
LISTED_LEAD = re.compile(r"(?:^|[,.;:!?(]\s*)(?:of|among)\s+(?:these|those|them)\s*,\s*$", re.IGNORECASE)
#: The verbs of finding something, or of its being there, that may stand two
#: or three words after "also" or "plus" (:data:`LOOSE_ALSO`): "Also, there are
#: 2 issues", "Plus, staging has 2 issues"; the phrases end in a particle.
FOUND_VERB = frozenset(
    {
        "found", "flagged", "surfaced", "spotted", "noticed", "noted", "saw", "detected", "identified", "uncovered",
        "discovered", "reported", "revealed", "observed", "caught", "counted", "shows", "showed", "see", "hit",
        "are", "were", "is", "has", "have",
    }
)
FOUND_PHRASE = frozenset({("turned", "up"), ("came", "across"), ("picked", "up"), ("ran", "into")})
#: Where a count's clause starts, for :data:`LISTED_NEAR`.
CLAUSE_BREAK = re.compile(r"[,.;:!?()\u2013\u2014]")
#: A clause just before a count that says the listed ones are fixed, so the
#: count is what is left of them: "After these fixes, 2 issues remain", "With
#: these fixes in, ...", "Once you apply these, ...", "When these are fixed,
#: ...", "If you apply these, ...", and with no comma where the verb ends it
#: ("Once these are fixed 2 issues remain"); not "After the scan, I also flagged
#: 18 issues". :data:`FIXED_IF` is the "when" and "if" leads.
FIXED_VERB = r"(?:fix(?:es|ed|ing)?|appl(?:y|ied|ying)|patch(?:es|ed|ing)?|remediat\w*|resolv\w*|merg\w*)"
FIXED_TAIL = r"[^,.;:!?]{0,80}\b" + FIXED_VERB + r"\b(?:[^,.;:!?]{0,40},\s*(?:[a-z]+\s+){0,2}|\s+)$"
FIXED_FIRST = re.compile(r"\b(?:after|once|with)\b" + FIXED_TAIL, re.IGNORECASE)
FIXED_IF = re.compile(r"\b(?:when|if)\b" + FIXED_TAIL, re.IGNORECASE)
#: A modal just before "also" that makes the count an offer: "I can also fix 2
#: issues", "Should I also fix 2 issues now?"; "will", "would", "may", "might",
#: "should", "shall", "I'll" and "I'd" only with a verb of :data:`OFFER_VERB`, so
#: "I will also mention 18 issues" is a roll-up; "want me to" and "would you like
#: me to" always.
OFFER_MODAL = re.compile(
    r"(?:\b(?P<always>can|could|(?:want|like)\s+me\s+to)|\b(?:will|would|may|might|should|shall)|['’](?:ll|d))"
    r"\s+(?:(?:i|we|you)\s+)?$",
    re.IGNORECASE,
)
OFFER_VERB = frozenset({"fix", "open", "patch", "remediate", "address", "resolve", "handle"})
#: The verbs that make a count before a colon an offer, with :data:`OFFER_LEAD`
#: before them: "Shall I raise 2 items: ...".
OFFER_COLON_VERB = OFFER_VERB | {"file", "raise", "create"}
#: What comes before an offer's verb: a modal, its subject, and "also" or "go
#: ahead and" ("Would you like me to go ahead and open 2 issues: ...").
OFFER_LEAD = re.compile(
    r"(?:\b(?:can|could|(?:want|like)\s+me\s+to|will|would|may|might|should|shall)|['’](?:ll|d))"
    r"\s+(?:(?:i|we|you)\s+)?(?:also\s+|go\s+ahead\s+and\s+)?$",
    re.IGNORECASE,
)
#: How far back from a count before a colon :func:`_offered` reads, how many
#: words may stand between the offer's verb and the count, and the only words
#: that may ("file tickets for"); "fix that and 18 issues: ..." is no offer.
OFFER_COLON_LOOKBACK = 64
OFFER_OBJECT_WORDS = 3
OFFER_OBJECT_FILLER = frozenset(
    {"up", "a", "an", "the", "these", "those", "for", "ticket", "tickets", "issue", "issues", "pr", "prs"}
)
#: How far back from "also" :data:`OFFER_MODAL` reads.
OFFER_LOOKBACK = 24
#: How far back from a count :func:`_loose_rollup` reads, so a long paragraph of
#: counts is not reread to its start at each.
LOOSE_LOOKBACK = 200
#: "these" or "those" just before a roll-up term, which makes it the listed
#: ones: "Besides those 2 issues, 18 findings remain" adds 18, not 20.
LISTED_TERM = re.compile(r"\b(?:these|those)\s+$", re.IGNORECASE)
LISTED_LOOKBACK = 8
#: The roll-up wording a paragraph is preferred for over an earlier marked one.
ROLLUP_STRONG = re.compile(r"\b(?:\d+\s+more|also found)\b", re.IGNORECASE)
SEVERITY_TERM = re.compile(r"\s+(?:critical|high|major|medium|moderate|minor|low)(?![\w-])", re.IGNORECASE)
#: What follows a first term that heads the breakdown: "18 items: ...", "18 findings remain (...",
#: "4 issues in 2 namespaces: ...".
HEADS_BREAKDOWN = re.compile(r"(?:\s+[\w-]+){0,4}?\s*[:(]", re.IGNORECASE)
#: The text layout's ask, when the roll-up it drops was the last line.
ASK_ALL = "Ask me to see all {count}."
#: A paragraph that is one bold span: a title written without a "#".
BOLD_PARAGRAPH = re.compile(r"^\*\*([^*]+)\*\*$")


def _paragraphs(lines: list[str]) -> list[str]:
    """Blank-line-separated paragraphs, each joined onto one line."""
    out: list[str] = []
    current: list[str] = []
    for line in lines + [""]:
        if line.strip():
            current.append(line.strip())
        elif current:
            out.append(" ".join(current))
            current = []
    return out


def _loose_rollup(text: str, listed: int | None = None) -> bool:
    """Whether ``text`` states a roll-up as a sentence (:data:`LOOSE_ALSO`, :data:`LOOSE_AFTER`).

    A count must not be of the listed ones: no word of :data:`LISTED_NEAR` in
    the two before it in its clause, and no :data:`FIXED_FIRST` clause just
    before it, within :data:`LOOSE_LOOKBACK` characters. A count no more than
    the ``listed`` ones also must not follow a :data:`FIXED_IF` or
    :data:`LISTED_LEAD` clause; "also" must not be an offer's
    (:data:`OFFER_MODAL`), and two or three words after it must end in a verb
    of :data:`FOUND_VERB`. A count more than the listed ones cannot be any of
    those ("I'll also open PRs for 18 issues", "If you fix these, 18 issues
    remain").
    """
    for pattern in (LOOSE_ALSO, LOOSE_AFTER):
        for match in pattern.finditer(text):
            start = match.start("n")
            before = text[max(0, start - LOOSE_LOOKBACK) : start]
            clause = CLAUSE_BREAK.split(before)[-1]
            near = {word.lower() for word in re.findall(r"[A-Za-z]+", clause)[-2:]}
            if near & LISTED_NEAR or FIXED_FIRST.search(before):
                continue
            if listed is not None and int(match.group("n")) > listed:
                return True
            if FIXED_IF.search(before) or LISTED_LEAD.search(before):
                continue
            if pattern is LOOSE_ALSO:
                words = [word.lower() for word in match.group("gap").split()]
                found = bool(words) and words[-1] in FOUND_VERB or tuple(words[-2:]) in FOUND_PHRASE
                if len(words) > 1 and not found:
                    continue
                modal = OFFER_MODAL.search(text[max(0, match.start() - OFFER_LOOKBACK) : match.start()])
                if modal and (modal.group("always") or set(words) & OFFER_VERB):
                    continue
            return True
    return False


def _offered(text: str, start: int) -> bool:
    """Whether the count at ``start`` is an offer's: :data:`OFFER_LEAD`, then a verb of :data:`OFFER_COLON_VERB`.

    Up to :data:`OFFER_OBJECT_WORDS` words of :data:`OFFER_OBJECT_FILLER` may
    follow the verb ("open up 2 issues", "file tickets for 2 issues"); "I can
    see 18 issues: ..." is no offer.
    """
    clause = CLAUSE_BREAK.split(text[max(0, start - OFFER_COLON_LOOKBACK) : start])[-1]
    words = list(re.finditer(r"[A-Za-z]+", clause))
    for word in reversed(words[-(OFFER_OBJECT_WORDS + 1) :]):
        if word.group(0).lower() in OFFER_COLON_VERB and OFFER_LEAD.search(clause[: word.start()]):
            return True
        if word.group(0).lower() not in OFFER_OBJECT_FILLER:
            return False
    return False


def _marked(text: str) -> bool:
    """Whether ``text`` has a :data:`ROLLUP_MARK`, or a :data:`COLON_MARK` that is not an offer's."""
    return bool(ROLLUP_MARK.search(text)) or any(not _offered(text, m.start()) for m in COLON_MARK.finditer(text))


def _restated(text: str, term: re.Match, listed: int | None) -> bool:
    """Whether roll-up ``term`` counts the listed ones: "these" or "those" before a count they can hold."""
    if listed is not None and int(term.group(1)) > listed:
        return False
    return bool(LISTED_TERM.search(text[max(0, term.start(1) - LISTED_LOOKBACK) : term.start(1)]))


def _rollup_count(text: str, listed: int | None = None) -> int | None:
    """The roll-up's count in ``text``, or None when it has none.

    A total it states wins over its breakdown; only with none are the terms
    summed, leaving out a count of the listed ones (:data:`LISTED_TERM`, no more
    than ``listed``) when another is left. A paragraph with no roll-up marker
    (:data:`ROLLUP_MARK`, or a roll-up worded as a sentence,
    :func:`_loose_rollup`, given the ``listed`` count) has none.
    """
    if not _marked(text) and not _loose_rollup(text, listed):
        return None
    more = [int(n) for n in MORE_TOTAL.findall(text)]
    if more:
        return sum(more)
    also = ALSO_FOUND.search(text)
    if also:
        return int(also.group(1))
    terms = list(ROLLUP_TERM.finditer(text))
    kept = [term for term in terms if not _restated(text, term, listed)]
    terms = kept or terms
    if not terms:
        return None
    first = terms[0]
    heads = not SEVERITY_TERM.match(text, first.end(1)) and HEADS_BREAKDOWN.match(text, first.end())
    if len(terms) > 1 and heads:
        return int(first.group(1))
    return sum(int(term.group(1)) for term in terms)


def _split_gap(clause: str) -> tuple[list[str], str]:
    """``(gaps, rest)``: the parts of a posture clause naming what was not scanned, and the rest of it.

    A part naming a gap keeps its parentheticals ("could not be scanned
    (permission denied)"); the rest keeps the scan counts beside it, each gap
    cut out where it was read, in one pass.
    """
    gaps = gap_parts(clause)
    pieces: list[str] = []
    pos = 0
    for gap in gaps:
        at = clause.find(gap, pos) if gap else -1
        if at < 0:
            return gaps, _replace_each(clause, gaps)
        pieces.append(clause[pos:at])
        pos = at + len(gap)
    pieces.append(clause[pos:])
    return gaps, " ".join(pieces)


def _replace_each(clause: str, gaps: list[str]) -> str:
    """``clause`` with each of ``gaps`` replaced by a space where it first occurs.

    What :func:`_split_gap` does in one pass when the gaps come in text order,
    as :func:`slack_presenter.gap_parts` returns them; this is the fallback for a
    gap not found after the one before it, rereading the clause per gap.
    """
    for gap in gaps:
        clause = clause.replace(gap, " ", 1)
    return clause


def _is_title(paragraph: str) -> bool:
    """True for a paragraph that is entirely bold with no sentence end: a title, not posture."""
    bold = BOLD_PARAGRAPH.match(paragraph)
    return bool(bold) and bold.group(1).strip()[-1:] not in SENTENCE_PUNCTUATION


Item = tuple[str | None, str, str]


def _item(first: str, more: list[str]) -> Item | None:
    """``(severity, headline, sentence)`` from an item's lines, or None if it has no bold lead.

    ``more`` is the item's lines under its first, joined into the sentence.

    The headline is the whole line, so a bold span covering only part of it
    (``**seeded-c:** the default SA is cluster-admin``) keeps the rest. Two
    shapes take less: a bold span ending a sentence is the headline and what
    follows it is the item's sentence, and a bold span that is only a severity
    label (``**Critical:**``) is the severity.
    """
    bold = BOLD_LEAD.match(first)
    if not bold:
        return None
    span, after = bold.group(1).strip(), bold.group(2).strip()
    sentence = ""
    if span[-1:] in SENTENCE_PUNCTUATION and after:
        headline, sentence = span, after
    else:
        headline = first.replace(BOLD_MARK, "").strip()
    sentence = " ".join(part for part in [sentence, *more] if part)
    severity = None
    label = SEVERITY_LABEL.match(headline)
    if label:
        severity, headline = label.group(1).lower(), headline[label.end() :]
    else:
        tag = SEVERITY_TAG.search(headline)
        if tag:
            severity = tag.group(1).lower()
            headline = SEVERITY_TAG.sub(" ", headline, count=1).strip()
    if not headline:
        return None
    return severity, headline, sentence


def _parse(report: str) -> tuple[str, list[Item], list[str]] | None:
    """Posture, (severity, headline, sentence) items and the trailing paragraphs, or None."""
    lines = report.splitlines()
    starts = [i for i, line in enumerate(lines) if ITEM_START.match(line)]
    if not starts:
        return None
    posture = _paragraphs([line for line in lines[: starts[0]] if not HEADING.match(line)])
    posture = [paragraph for paragraph in posture if not _is_title(paragraph)]
    if not posture:
        return None

    items: list[tuple[str, list[str]]] = []
    index = starts[0]
    while index < len(lines):
        line = lines[index]
        match = ITEM_START.match(line)
        if match:
            items.append((match.group(1).strip(), []))
        elif line.strip() and not line[:1].isspace() and not ITEM_START.match(lines[index - 1]):
            # An unindented line ends the list, unless it directly follows an
            # item line: CommonMark reads that as the item's lazy continuation.
            break
        elif line.strip():
            items[-1][1].append(line.strip())
        index += 1

    parsed = [_item(first, more) for first, more in items]
    if None in parsed:
        return None

    tail = _paragraphs(lines[index:])
    if any(HEADING.match(p) or ITEM_START.match(p) or INLINE_ITEM.search(p) for p in tail):
        return None
    return " ".join(posture), parsed, tail


def _row(severity: str | None, headline: str) -> str:
    """A finding's headline line, led by its severity as inline code when the headline was labelled."""
    if severity:
        return severity_row(severity, headline)
    return f"{BOLD_MARK}{headline}{BOLD_MARK}"


def _with_sentence(row: str, sentence: str) -> str:
    """``row`` with its finding's sentence on the line under it."""
    return f"{row}\n{sentence}" if sentence else row


def _gap_line(gaps: list[list[str]]) -> str:
    """Each list of gap parts as a sentence, in order, skipping the empty ones and a part already said."""
    seen: set[str] = set()
    lines = []
    for parts in gaps:
        # A closing line may repeat the posture's gap; case and spacing aside, it is the same gap.
        fresh = [part for part in parts if " ".join(part.lower().split()).rstrip(".") not in seen]
        seen.update(" ".join(part.lower().split()).rstrip(".") for part in fresh)
        if fresh:
            lines.append(as_line(GAP_SEPARATOR.join(fresh)))
    return " ".join(lines)


class _Shape:
    """A parsed report reduced to its top rows, its total and the closing lines."""

    def __init__(self, posture: str, items: list[Item], tail: list[str]):
        if len(items) > SOP_LIST_CAP:
            self.top = list(items)
        else:
            self.top = [item for i, item in enumerate(items) if i < TOP_COUNT or item[0] == CRITICAL]
        counted = [p for p in tail if _rollup_count(p, len(items)) is not None]
        rollup = next((p for p in counted if ROLLUP_STRONG.search(p)), counted[0] if counted else None)
        # Only the roll-up goes: the headline carries its count, and a closing
        # line with a count in it is still how a text reader asks.
        self.closing = [p for p in tail if p is not rollup]
        clauses = [c.strip() for c in CLAUSE_END.split(posture.replace(BOLD_MARK, "")) if c.strip()]
        split = [_split_gap(c) for c in clauses]
        # The roll-up is left out of both layouts, so a gap it names joins the posture's.
        self.gaps = [gaps for gaps, _ in split] + [gap_parts(rollup or "")]
        # Only the card leaves out the closing lines, so only the card lifts their gaps.
        self.closing_gaps = [gap_parts(p) for p in self.closing]
        self.scanned = [rest for _, rest in split if SCAN_VERB.search(rest)]
        closing_totals = [int(n) for p in tail for n in ALL_TOTAL.findall(p)]
        in_totals = [int(n) for p in tail for n in IN_TOTAL.findall(p)]
        stated = POSTURE_TOTAL.search(posture)
        # "I scanned 3 clusters and found 22 findings" counts the fleet; "seeded-a has 9 findings" may not.
        scan_total = next(
            (
                int(found.group(1))
                for clause in clauses
                if (verb := SCAN_VERB.search(clause)) and (found := POSTURE_TOTAL.search(clause, verb.end()))
            ),
            None,
        )
        if rollup is not None:
            total = len(items) + (_rollup_count(rollup, len(items)) or 0)
        elif closing_totals:
            total = closing_totals[0]
        elif scan_total is not None:
            total = scan_total
        elif in_totals:
            total = max(in_totals)
        elif stated:
            total = int(stated.group(1))
        else:
            total = len(items)
        # Never fewer than the report lists.
        self.total = max(total, len(items))
        self.ask = ASK_ALL.format(count=self.total) if rollup is not None and tail[-1] is rollup and self.total > len(self.top) else ""


def _shape(report: str) -> _Shape | None:
    parsed = _parse(report)
    return None if parsed is None else _Shape(*parsed)


def present(report: str) -> str:
    """The report as the card's headline, the top findings and the closing lines or the ask; unchanged if it does not parse."""
    # A NUL inside a gap ("could not\0 be scanned") hides it from _split_gap.
    report = report.replace("\0", "")
    shape = _shape(report)
    if shape is None:
        return report
    headline, note, detail = card_headline(shape)
    head = f"{BOLD_MARK}{headline}{BOLD_MARK}" + (f" {note}" if note else "") + (f"\n{detail}" if detail else "")
    rows = "\n".join(_with_sentence(_row(severity, h), sentence) for severity, h, sentence in shape.top)
    return PARAGRAPH_BREAK.join([head, rows, *shape.closing, *filter(None, [shape.ask])]) + "\n"


def _rows(items: list[Item]) -> list[dict]:
    return [{"severity": severity, "text": headline, "detail": sentence} for severity, headline, sentence in items]


def _stated(pattern: re.Pattern, clauses: list[str]) -> int | None:
    """The count ``pattern`` finds in ``clauses``, digits or a number word; None if absent or they disagree."""
    found = {
        int(word) if word.isdigit() else NUMBER_WORDS[word]
        for word in (m.group(1).lower() for c in clauses for m in pattern.finditer(c))
    }
    return found.pop() if len(found) == 1 else None


def _counted(count: int, nouns: tuple[str, str]) -> str:
    return f"{count} {nouns[count != 1]}"


def card_headline(shape: _Shape, closing_gaps: bool = False) -> tuple[str, str, str]:
    """The card's ``(headline, note, detail)``.

    The headline says what was scanned and the total. The lead saying what to
    fix first is the note on the headline's line, or, when the report names a
    gap, follows the gap on the line under it, so it still runs into the rows.
    ``closing_gaps`` adds the gaps the closing lines name, for a layout that
    leaves them out.
    """
    scanned = [
        _counted(count, nouns)
        for count, nouns in (
            (_stated(CLUSTER_COUNT, shape.scanned), CLUSTERS_NOUN),
            (_stated(WORKLOAD_COUNT, shape.scanned), WORKLOADS_NOUN),
        )
        if count is not None
    ]
    found = _counted(shape.total, FINDINGS_NOUN)
    if scanned:
        headline = SCANNED_TEMPLATE.format(scanned=SCANNED_JOIN.join(scanned), found=found)
    else:
        headline = FOUND_TEMPLATE.format(found=found)
    shown = len(shape.top)
    lead = ""
    if shape.total > shown:
        if any(item[0] in URGENT_SEVERITIES for item in shape.top):
            one, template = CARD_LEAD_ONE, CARD_LEAD_TEMPLATE
        else:
            one, template = CARD_NEUTRAL_LEAD_ONE, CARD_NEUTRAL_LEAD_TEMPLATE
        lead = one if shown == 1 else template.format(count=COUNT_WORDS.get(shown, str(shown)))
        lead = lead[:1].upper() + lead[1:]
    gap = _gap_line(shape.gaps + (shape.closing_gaps if closing_gaps else []))
    if gap:
        return headline + LEADS_ON, "", " ".join(part for part in (gap, lead) if part)
    return headline + (LEADS_ON if lead else ROWS_FOLLOW), lead, ""


def blocks(report: str) -> tuple[list[dict], str] | None:
    """The Block Kit report's ``(blocks, text)``, or None when the report does not parse.

    ``text`` is the message's mrkdwn ``text`` field: the headline, the gap and
    lead, then the top rows.
    """
    # A NUL inside a gap ("could not\0 be scanned") hides it from _split_gap.
    report = report.replace("\0", "")
    shape = _shape(report)
    if shape is None:
        return None
    headline, note, detail = card_headline(shape, closing_gaps=True)
    top = _rows(shape.top)
    label = FIX_FIRST if len(shape.top) > 1 else FIX_ONLY
    # The row as the card shows it, so the click's turn names only what was seen.
    first = shown_text(shape.top[0][1])
    choices: list = [(label, FIX_TURN.format(label=label, finding=first))]
    if shape.total > len(shape.top):
        choices.append(SEE_ALL.format(count=shape.total))
    built = blocks_report(
        headline,
        note=note,
        detail=detail,
        rows=top,
        choices=choices,
        action_id_prefix=ACTION_ID_PREFIX,
    )
    return built, fallback_text(" ".join(part for part in (headline, note, detail) if part), rows=top)
