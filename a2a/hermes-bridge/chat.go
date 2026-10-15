package hermesbridge

import (
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// The bridge's chat messages (docs/designs/spec-a2a-payloads.md, "Chat
// messages"): under spec.mode next the gateway renders a kube-agents.chat/v1
// object and composes nothing, so the bridge states the structure of its own
// turn, each a port of the today presenter
// (agents/platform/scripts/slack_presenter.py) tested on the shared cases
// (tests/conformance/chat/, and testdata/chat_cases.json, which
// agents/platform/scripts/test_chat_message.py keeps equal to the Python
// original's answers):
//
//   - the answer on the result: its first sentence as the headline, cut at
//     a clause past the cap, and the rest as the detail, or under "why" when
//     the rest is longer than a detail holds (split_answer); an answer that
//     ends by asking which of its options to take offers them as choices
//     (answer_choices, chat_choices.go);
//   - mark.ask, the kind of ask the prompt is, chosen by keyword
//     (arrival_reaction), on each heartbeat while the turn runs;
//   - when the turn filed kanban cards, the terminal's mark-only message
//     holding that ask, so it stays marked until the cards' notifies settle
//     it. The same kind rides the conversation route (route.go) for those
//     notifies to keep. A direct answer's terminal carries no mark, which
//     takes the ask off.
//
// The card work itself (plan rows, questions, reports) is the kanban
// notifier's, in the Hermes gateway process, where the board is.
const (
	// chatHeadlineMax is the headline cap, the presenter's HEADLINE_MAX.
	chatHeadlineMax = 150
	// chatDetailMax is the detail's cap; a longer rest goes in the fold, up
	// to chatFoldTextMax, and past that the answer posts as text.
	chatDetailMax   = 2000
	chatFoldTextMax = 12000
	// chatEllipsis ends a clipped headline; chatClipMinShare is the
	// presenter's CLIP_MIN_SHARE: a word-aligned clip shorter than half the
	// limit cuts hard instead.
	chatEllipsis     = "…"
	chatClipMinShare = 2
	// abbreviationTail bounds how far back from a candidate sentence end an
	// abbreviation may start (ABBREVIATION_TAIL).
	abbreviationTail = 16
	// kanbanCreateTool is the Hermes tool that files a card; a turn that
	// called it successfully keeps its ask marked on the terminal.
	kanbanCreateTool = "kanban_create"
	// slackUXEnv is the agent's KAGE_SLACK_UX, which the bridge inherits
	// with the rest of the agent environment: unset is on, and once set only
	// slackUXOnValues keep it on (slack_presenter.FLAG_ON_VALUES).
	slackUXEnv = "KAGE_SLACK_UX"
	// codeHold wraps a held code span's index (CODE_PLACEHOLDER's NULs).
	codeHold = "\x00"
)

// The ask kinds, mark.ask tokens for arrival_reaction's emoji, tried in
// arrival_reaction's order: the first match wins. A question outranks the
// incident words, so "is checkout crashlooping?" is investigate while
// "checkout is down" is incident.
const (
	askQuestion    = "question"
	askInvestigate = "investigate"
	askFleet       = "fleet"
	askUpgrade     = "upgrade"
	askCost        = "cost"
	askSecurity    = "security"
	askChange      = "change"
	askBoard       = "board"
	askIncident    = "incident"
)

var (
	changeWords = regexp.MustCompile(`(?i)\bfix(es|ing)?\b|\bbump\b|\broll(ing)?[\s-]*back\b|\brollback\b` +
		`|\bopen\s+(a\s+|the\s+|an\s+)?(pr|pull\s+request)\b|\bscale\b`)
	upgradeWords  = regexp.MustCompile(`(?i)\bupgrad(e|es|ed|ing)\b|\bversions?\b|\brelease\s+channels?\b|\boutdated\b|\bout\s+of\s+date\b`)
	costWords     = regexp.MustCompile(`(?i)\bcosts?\b|\bcosting\b|\bspend(s|ing)?\b|\bbill(s|ing)?\b|\bbudgets?\b|\bpric(e|es|ing)\b`)
	securityWords = regexp.MustCompile(`(?i)\bsecurity\b|\baudit(s|ing)?\b|\bcves?\b|\bvulnerab|\brbac\b|\biam\b|\bpermissions?\b|\bexposed\b`)
	fleetWords    = regexp.MustCompile(`(?i)\b(all|every|which|each)\s+(of\s+(the|my|our)\s+)?(my\s+|our\s+|the\s+)?clusters?\b` +
		`|\bacross\s+(the\s+|my\s+|our\s+)?(fleet|clusters)\b|\bfleet[\s-]*wide\b`)
	investigateWords = regexp.MustCompile(`(?i)\bwhy\b|\brestart|\berrors?\b|\bfail|\bcrash|\boom(killed|kill|ed)?\b|\bpending\b|\bslow\b` +
		`|\bdebug|\bdiagnos|\binvestigat|\bbroken\b`)
	boardWords    = regexp.MustCompile(`(?i)\bboard\b|\bstatus\b|\bwhat'?s\s+running\b|\bwhat\s+is\s+running\b`)
	incidentWords = regexp.MustCompile(`(?i)\bdown\b|\bpag(e|ed|es|ing)\b|\boutage\b|\bsev\s?\d\b|\bsev\b|\bcrash\s?loop`)
	askMention    = regexp.MustCompile(`<[@#!][^<>]*>`)
	askFirstWord  = regexp.MustCompile(`[A-Za-z']+`)
	askClasses    = []struct {
		words *regexp.Regexp
		kind  string
	}{
		{changeWords, askChange},
		{upgradeWords, askUpgrade},
		{boardWords, askBoard},
		{costWords, askCost},
		{securityWords, askSecurity},
		{fleetWords, askFleet},
	}
	questionOpeners = map[string]bool{
		"is": true, "are": true, "was": true, "were": true, "why": true, "what": true, "whats": true,
		"what's": true, "how": true, "which": true, "who": true, "when": true, "where": true, "does": true,
		"do": true, "did": true, "can": true, "could": true, "should": true, "will": true, "would": true,
		"has": true, "have": true, "check": true, "show": true, "list": true, "tell": true, "explain": true,
		"describe": true,
	}
)

// slackUXOnValues are the KAGE_SLACK_UX values that keep it on.
var slackUXOnValues = map[string]bool{"1": true, "true": true, "yes": true, "on": true}

// slackUXOn reads KAGE_SLACK_UX as slack_presenter.enabled does.
func slackUXOn() bool {
	value, set := os.LookupEnv(slackUXEnv)
	return !set || slackUXOnValues[strings.ToLower(pyStrip(value))]
}

// pySpaceChars is Python's \s on str, which RE2's \s ([\t\n\f\r ]) is not:
// the ASCII ones with \v, the separators U+001C-U+001F, U+0085, and every
// Unicode space separator (\p{Z}). pySpace is it as a class; pyURLRun is a
// markdown link url's character, anything but a parenthesis or one of them.
const (
	pySpaceChars = `\t\n\v\f\r \x{1c}-\x{1f}\x{85}\p{Z}`
	pySpace      = `[` + pySpaceChars + `]`
	pyURLRun     = `[^()` + pySpaceChars + `]`
)

// isPySpace is Python's str.isspace: unicode.IsSpace and the separators
// U+001C-U+001F, which Go does not count.
func isPySpace(r rune) bool { return unicode.IsSpace(r) || (r >= '\x1c' && r <= '\x1f') }

// pyStrip is Python's str.strip(): isPySpace trimmed from both ends.
func pyStrip(s string) string { return strings.TrimFunc(s, isPySpace) }

// pyFolds are the non-ASCII letters Python's re.IGNORECASE matches to an
// ASCII keyword letter: dotted and dotless i, long s, the Kelvin sign (an
// escape, since a normalising editor turns the raw rune into an ASCII K).
var pyFolds = map[rune]rune{'İ': 'i', 'ı': 'i', 'ſ': 's', '\u212a': 'k'}

// asciiWords stands in for Python's Unicode \s, \b and case folding, which
// RE2 spells in ASCII: a space Python's \s takes becomes a space, a letter
// Python folds to an ASCII one becomes it, and any other non-ASCII letter or
// number becomes "_", a word character in both engines that no keyword
// holds, so every boundary and space run reads as the presenter reads it.
func asciiWords(text string) string {
	return strings.Map(func(r rune) rune {
		if folded, ok := pyFolds[r]; ok {
			return folded
		}
		switch {
		case r == '\v' || (r >= '\x1c' && r <= '\x1f') || (r >= utf8.RuneSelf && isPySpace(r)):
			return ' '
		case r < utf8.RuneSelf:
			return r
		case unicode.IsLetter(r) || unicode.IsNumber(r):
			return '_'
		}
		return r
	}, text)
}

// askKind is the mark.ask token for an ask, from its words alone.
func askKind(text string) string {
	stripped := pyStrip(askMention.ReplaceAllString(text, " "))
	clean := asciiWords(stripped)
	for _, c := range askClasses {
		if c.words.MatchString(clean) {
			return c.kind
		}
	}
	// The opener check reads the ask as written: the presenter's FIRST_WORD
	// is case-sensitive ASCII and never folds, so "İs" opens on "s".
	if !askIsQuestion(stripped) && incidentWords.MatchString(clean) {
		return askIncident
	}
	if investigateWords.MatchString(clean) || incidentWords.MatchString(clean) {
		return askInvestigate
	}
	return askQuestion
}

func askIsQuestion(text string) bool {
	if strings.HasSuffix(strings.TrimRightFunc(text, isPySpace), "?") {
		return true
	}
	first := askFirstWord.FindString(text)
	return first != "" && questionOpeners[strings.ToLower(first)]
}

// answerChat is the result's chat message, or false when the answer posts as
// text: no headline (one opening on a code fence) or a rest past the fold's
// cap.
func answerChat(answer string) (chatmsg.Message, bool) {
	headline, body := splitAnswer(answer)
	if headline == "" {
		return chatmsg.Message{}, false
	}
	rest := pyStrip(strings.Join(body, "\n\n"))
	msg := chatmsg.Message{Schema: chatmsg.Schema, Headline: headline}
	if runeLen(rest) <= chatFoldTextMax {
		if offered, ok := offerChat(msg, rest); ok {
			return offered, true
		}
	}
	switch n := runeLen(rest); {
	case n > chatFoldTextMax:
		return chatmsg.Message{}, false
	case n > chatDetailMax:
		msg.Fold = &chatmsg.Fold{Title: chatmsg.DefaultFoldTitle, Text: rest}
	default:
		msg.Detail = rest
	}
	return msg, true
}

// offerChat is answer_message's layout for an answer that ends by asking
// which of its options to take (answerChoices): the detail is its opening
// paragraph, each option's shown line and the question, the rest goes under
// "why", and the options are choices. False when it asks none, when a label
// is one the gateway would act on as a command, or when the detail is past
// its cap, and the answer keeps the plain layout.
func offerChat(msg chatmsg.Message, rest string) (chatmsg.Message, bool) {
	offer, ok := answerChoices(rest)
	if !ok {
		return chatmsg.Message{}, false
	}
	for _, option := range offer.options {
		text := clipRunes(strings.Join(strings.Fields(option.label), " "), buttonTextMax)
		if text == "" || chattext.IsCommand(text) {
			return chatmsg.Message{}, false
		}
		msg.Choices = append(msg.Choices, chatmsg.Choice{Text: text, Primary: option.primary})
	}
	var parts []string
	for _, part := range []string{offer.shown, offer.question} {
		if part != "" {
			parts = append(parts, part)
		}
	}
	msg.Detail = strings.Join(parts, "\n\n")
	if runeLen(msg.Detail) > chatDetailMax {
		return chatmsg.Message{}, false
	}
	if offer.folded != "" {
		msg.Fold = &chatmsg.Fold{Title: chatmsg.DefaultFoldTitle, Text: offer.folded}
	}
	return msg, true
}

// resultChatPart is the answer's chat message as the result's last-chunk
// DataPart, validated; false when there is none or it breaks a rule, and the
// answer posts as text.
func resultChatPart(answer string) (lib.Part, bool) {
	msg, ok := answerChat(answer)
	if !ok {
		return lib.Part{}, false
	}
	part, err := chatmsg.Part(chatmsg.Result, msg)
	return part, err == nil
}

// progressParts is a heartbeat's part list: the line, and beside it a chat
// message holding only the ask's mark, which keeps the ask's reaction on
// while the turn runs.
func progressParts(line, ask string) []lib.Part {
	parts, err := chatmsg.Parts(chatmsg.Progress, line, chatmsg.Message{Mark: &chatmsg.Mark{Ask: ask}})
	if err != nil {
		return []lib.Part{{Kind: "text", Text: line}}
	}
	return parts
}

// --- split_answer ---------------------------------------------------------

var (
	fenceLine    = regexp.MustCompile("^ {0,3}(`{3,}|~{3,})")
	headingLine  = regexp.MustCompile(`^` + pySpace + `{0,3}#{1,6}` + pySpace + `+`)
	listMarker   = regexp.MustCompile(`^` + pySpace + `*([-*+]|\p{Nd}+[.)])` + pySpace + `+`)
	mdLink       = regexp.MustCompile(`\[([^\[\]]+)\]\((` + pyURLRun + `+(?:\(` + pyURLRun + `*\)` + pyURLRun + `*)*)\)`)
	backtickRun  = regexp.MustCompile("`+")
	codeHeld     = regexp.MustCompile("\x00(\\d+)\x00")
	abbreviation = []string{"e.g", "i.e", "vs", "approx", "incl", "cf", "etc", "esp", "ex", "fig", "rev", "ver",
		"cont", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}
	numberAbbreviation = []string{"no", "max", "min"}
)

// nextFence is the fence open after line, given the one open before it
// (next_fence).
func nextFence(line, fence string) string {
	m := fenceLine.FindStringSubmatch(line)
	if m == nil {
		return fence
	}
	mark := m[1]
	if fence == "" {
		if mark[0] == '`' && strings.Contains(line[len(m[0]):], "`") {
			return ""
		}
		return mark
	}
	closes := mark[0] == fence[0] && len(mark) >= len(fence) && strings.Trim(pyStrip(line), mark[:1]) == ""
	if closes {
		return ""
	}
	return fence
}

func opensFence(line string) bool { return nextFence(line, "") != "" }

// paragraphs splits on blank lines, never inside a fenced block.
func paragraphs(md string) []string {
	var out, current []string
	fence := ""
	flush := func() {
		if len(current) > 0 {
			out = append(out, strings.Trim(strings.Join(current, "\n"), "\n"))
			current = nil
		}
	}
	for _, line := range strings.Split(md, "\n") {
		fence = nextFence(line, fence)
		if fence == "" && pyStrip(line) == "" {
			flush()
			continue
		}
		current = append(current, line)
	}
	flush()
	kept := out[:0]
	for _, p := range out {
		if pyStrip(p) != "" {
			kept = append(kept, p)
		}
	}
	return kept
}

type codeSpan struct{ start, end, opener int }

// codeSpans finds each code span, left to right, by line (_code_spans): a
// backtick run closed by the next later run of exactly its length.
func codeSpans(text string) []codeSpan {
	var spans []codeSpan
	offset := 0
	for _, line := range strings.Split(text, "\n") {
		runs := backtickRun.FindAllStringIndex(line, -1)
		starts := map[int][]int{}
		for _, r := range runs {
			starts[r[1]-r[0]] = append(starts[r[1]-r[0]], r[0])
		}
		end := 0
		for _, r := range runs {
			start, length := r[0], r[1]-r[0]
			if start < end {
				continue
			}
			closers := starts[length]
			after := sort.SearchInts(closers, start+1)
			if after < len(closers) {
				end = closers[after] + length
				spans = append(spans, codeSpan{offset + start, offset + end, length})
			}
		}
		offset += len(line) + 1
	}
	return spans
}

type heldSpan struct{ whole, content string }

func holdCode(text string) (string, []heldSpan) {
	var b strings.Builder
	var spans []heldSpan
	last := 0
	for _, s := range codeSpans(text) {
		b.WriteString(text[last:s.start])
		b.WriteString(codeHold + strconv.Itoa(len(spans)) + codeHold)
		spans = append(spans, heldSpan{text[s.start:s.end], text[s.start+s.opener : s.end-s.opener]})
		last = s.end
	}
	b.WriteString(text[last:])
	return b.String(), spans
}

func restoreCode(text string, spans []heldSpan, content bool) string {
	return codeHeld.ReplaceAllStringFunc(text, func(m string) string {
		i, err := strconv.Atoi(strings.Trim(m, codeHold))
		if err != nil || i >= len(spans) {
			return m
		}
		if content {
			return pyStrip(spans[i].content)
		}
		return spans[i].whole
	})
}

// isWord is Python's \w: a letter, a digit or an underscore.
func isWord(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r) }

func at(rs []rune, i int) (rune, bool) {
	if i < 0 || i >= len(rs) {
		return 0, false
	}
	return rs[i], true
}

func hasAt(rs []rune, i int, marker string) bool {
	m := []rune(marker)
	if i < 0 || i+len(m) > len(rs) {
		return false
	}
	for k, r := range m {
		if rs[i+k] != r {
			return false
		}
	}
	return true
}

// boldAt matches MD_BOLD at i, returning its end and content: **x** with no
// word character before it (or, in a word, a run with no space or *), and
// __x__ with no word character on either side, each ending at the nearest
// marker.
func boldAt(rs []rune, i int) (end int, content string, ok bool) {
	for _, marker := range []string{"**", "__"} {
		if !hasAt(rs, i, marker) {
			continue
		}
		prev, hasPrev := at(rs, i-1)
		next, hasNext := at(rs, i+2)
		if (!hasPrev || !isWord(prev)) && hasNext && !isPySpace(next) {
			for j := i + 3; j+2 <= len(rs); j++ {
				k := j - 1
				if rs[k] == '\n' || hasAt(rs, k, marker) {
					break
				}
				if hasAt(rs, j, marker) && !isPySpace(rs[j-1]) {
					after, hasAfter := at(rs, j+2)
					if marker == "__" && hasAfter && isWord(after) {
						continue
					}
					return j + 2, string(rs[i+2 : j]), true
				}
			}
		}
		if marker == "**" && hasPrev && isWord(prev) {
			j := i + 2
			for j < len(rs) && rs[j] != '*' && !isPySpace(rs[j]) {
				j++
			}
			if j > i+2 && hasAt(rs, j, "**") {
				return j + 2, string(rs[i+2 : j]), true
			}
		}
	}
	return 0, "", false
}

// italicAt matches MD_ITALIC at i: *x* or _x_ with no word character or
// marker outside it, not opening on a space or ".", one line, no marker
// inside.
func italicAt(rs []rune, i int) (end int, content string, ok bool) {
	m := rs[i]
	if m != '*' && m != '_' {
		return 0, "", false
	}
	if prev, has := at(rs, i-1); has && (isWord(prev) || prev == m) {
		return 0, "", false
	}
	if next, has := at(rs, i+1); !has || isPySpace(next) || next == '.' {
		return 0, "", false
	}
	for j := i + 1; j < len(rs); j++ {
		if rs[j] == '\n' {
			return 0, "", false
		}
		if rs[j] != m {
			continue
		}
		if j == i+1 || isPySpace(rs[j-1]) {
			return 0, "", false
		}
		if after, has := at(rs, j+1); has && (isWord(after) || after == m) {
			return 0, "", false
		}
		return j + 1, string(rs[i+1 : j]), true
	}
	return 0, "", false
}

func replaceSpans(text string, match func([]rune, int) (int, string, bool)) string {
	rs := []rune(text)
	var b strings.Builder
	for i := 0; i < len(rs); {
		if end, content, ok := match(rs, i); ok {
			b.WriteString(content)
			i = end
			continue
		}
		b.WriteRune(rs[i])
		i++
	}
	return b.String()
}

// plainLine is one line of markdown as plain text (_plain).
func plainLine(md string) string {
	text, spans := holdCode(pyStrip(strings.ReplaceAll(md, "\x00", "")))
	if loc := headingLine.FindStringIndex(text); loc != nil {
		text = text[loc[1]:]
	}
	if loc := listMarker.FindStringIndex(text); loc != nil {
		text = text[loc[1]:]
	}
	text = mdLink.ReplaceAllString(text, "$1")
	text = replaceSpans(text, boldAt)
	text = replaceSpans(text, italicAt)
	return pyStrip(restoreCode(text, spans, true))
}

// sentenceEnds yields each candidate sentence end in line, as rune
// positions: the space run after ".", "!" or "?" (or after the emphasis
// closing on one) when what follows is not a lowercase letter (SENTENCE_END).
func sentenceEnds(rs []rune) [][2]int {
	var out [][2]int
	for i := 0; i < len(rs); i++ {
		if !isPySpace(rs[i]) || i == 0 || !endsSentence(rs[:i]) {
			continue
		}
		j := i
		for j < len(rs) && isPySpace(rs[j]) {
			j++
		}
		if j < len(rs) && !(rs[j] >= 'a' && rs[j] <= 'z') {
			out = append(out, [2]int{i, j})
		}
		i = j - 1
	}
	return out
}

func endsSentence(before []rune) bool {
	n := len(before)
	punct := func(k int) bool { return k >= 0 && strings.ContainsRune(".!?", before[k]) }
	if punct(n - 1) {
		return true
	}
	if n >= 2 && (before[n-1] == '*' || before[n-1] == '_') && punct(n-2) {
		return true
	}
	return n >= 3 && (string(before[n-2:]) == "**" || string(before[n-2:]) == "__") && punct(n-3)
}

// endsInAbbreviation reports whether stem ends with one of words and a ".",
// at the start or after a space or one of seps, starting no earlier than
// tail.
func endsInAbbreviation(stem []rune, tail int, words []string, seps string) bool {
	lower := []rune(strings.ToLower(string(stem)))
	for _, w := range words {
		suffix := []rune(w + ".")
		k := len(lower) - len(suffix)
		if k < 0 || string(lower[k:]) != string(suffix) {
			continue
		}
		start := k
		if k > 0 {
			sep := lower[k-1]
			if !isPySpace(sep) && !strings.ContainsRune(seps, sep) {
				continue
			}
			start = k - 1
		}
		if start >= tail {
			return true
		}
	}
	return false
}

func countMarker(s, marker string) int { return strings.Count(s, marker) }

// openerIn reports whether marker opens anywhere in s: no word character
// (nor, for a single marker, the marker itself) before it, and for bold no
// space after it, for italic neither a space nor a ".".
func openerIn(s, marker string) bool {
	rs := []rune(s)
	single := len([]rune(marker)) == 1
	for i := range rs {
		if !hasAt(rs, i, marker) {
			continue
		}
		if prev, has := at(rs, i-1); has && (isWord(prev) || single && string(prev) == marker) {
			continue
		}
		next, has := at(rs, i+len([]rune(marker)))
		if !has || isPySpace(next) || single && next == '.' {
			continue
		}
		return true
	}
	return false
}

// closerIn reports whether marker closes anywhere in s: no space before it
// and no word character (nor, for a single marker, the marker) after it.
func closerIn(s, marker string) bool {
	rs := []rune(s)
	single := len([]rune(marker)) == 1
	for i := range rs {
		if !hasAt(rs, i, marker) {
			continue
		}
		if prev, has := at(rs, i-1); !has || isPySpace(prev) {
			continue
		}
		if next, has := at(rs, i+len([]rune(marker))); has && (isWord(next) || single && string(next) == marker) {
			continue
		}
		return true
	}
	return false
}

// firstSentence splits line after its first sentence, not after an
// abbreviation, keeping bold and italic around the split paired
// (_first_sentence).
func firstSentence(line string) (string, string) {
	rs := []rune(line)
	for _, end := range sentenceEnds(rs) {
		sentence := string(rs[:end[0]])
		stem := []rune(strings.TrimRight(sentence, "*_"))
		tail := len(stem) - abbreviationTail
		if tail < 0 {
			tail = 0
		}
		next, hasNext := at(rs, end[1])
		numbered := endsInAbbreviation(stem, tail, numberAbbreviation, "([#") && hasNext && unicode.IsDigit(next)
		if numbered || endsInAbbreviation(stem, tail, abbreviation, "([") {
			continue
		}
		rest := pyStrip(string(rs[end[1]:]))
		for _, marker := range []string{"**", "__"} {
			if countMarker(sentence, marker)%2 == 1 && openerIn(sentence, marker) && closerIn(rest, marker) {
				sentence, rest = sentence+marker, marker+rest
			}
		}
		for _, marker := range []string{"*", "_"} {
			lone := countMarker(strings.ReplaceAll(sentence, marker+marker, ""), marker)
			if lone%2 == 1 && openerIn(sentence, marker) && closerIn(rest, marker) {
				sentence, rest = sentence+marker, marker+rest
			}
		}
		return sentence, rest
	}
	return line, ""
}

func runeLen(s string) int { return len([]rune(s)) }

// clipRunes cuts text on a word to limit code points, the ellipsis included
// (_clip).
func clipRunes(text string, limit int) string {
	rs := []rune(text)
	if len(rs) <= limit {
		return text
	}
	hard := string(rs[:limit-runeLen(chatEllipsis)])
	cut := hard
	if i := strings.LastIndex(hard, " "); i >= 0 {
		cut = hard[:i]
	}
	if runeLen(cut)*chatClipMinShare < runeLen(hard) {
		cut = hard
	}
	return strings.TrimRightFunc(cut, isPySpace) + chatEllipsis
}

// splitAnswer is the presenter's split_answer: the first sentence of the
// first paragraph as a plain headline, cut at a clause when it runs past the
// cap (clauseCut), and the rest as markdown sections.
func splitAnswer(md string) (string, []string) {
	paras := paragraphs(strings.ReplaceAll(md, "\x00", ""))
	if len(paras) == 0 {
		return "", nil
	}
	first, rest := paras[0], paras[1:]
	if opensFence(strings.SplitN(first, "\n", 2)[0]) {
		return "", paras
	}
	lines := strings.Split(first, "\n")
	wrapped := 1
	for wrapped < len(lines) && !headingLine.MatchString(lines[0]) &&
		!(listMarker.MatchString(lines[wrapped]) || headingLine.MatchString(lines[wrapped]) || opensFence(lines[wrapped])) {
		wrapped++
	}
	joinedLines := make([]string, wrapped)
	for i, line := range lines[:wrapped] {
		joinedLines[i] = pyStrip(line)
	}
	joined := strings.Join(joinedLines, " ")
	moreLines := strings.Join(lines[wrapped:], "\n")
	if loc := listMarker.FindStringIndex(joined); loc != nil {
		joined = joined[loc[1]:]
	}
	held, spans := holdCode(joined)
	sentence, remainder := firstSentence(held)
	sentence, remainder = restoreCode(sentence, spans, false), restoreCode(remainder, spans, false)
	if runeLen(plainLine(sentence)) > chatHeadlineMax {
		if head, overflow, ok := clauseCut(sentence); ok {
			sentence = head
			if remainder != "" {
				overflow += " " + remainder
			}
			remainder = overflow
		}
	}
	headline := clipRunes(plainLine(sentence), chatHeadlineMax)
	var tailParts []string
	for _, part := range []string{remainder, strings.Trim(moreLines, "\n")} {
		if part != "" {
			tailParts = append(tailParts, part)
		}
	}
	tail := strings.Join(tailParts, "\n")
	var body []string
	if pyStrip(tail) != "" {
		body = append(body, tail)
	}
	return headline, append(body, rest...)
}
