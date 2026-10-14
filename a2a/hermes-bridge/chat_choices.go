package hermesbridge

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Two more ports of the today presenter, beside split_answer's in chat.go,
// on the same shared cases (testdata/chat_cases.json):
//
//   - clauseCut, slack_presenter.clause_cut: a first sentence too long for
//     the headline is cut at a clause into a shorter one, and the rest of it
//     leads the detail, rather than clipped;
//   - answerChoices, slack_moments.answer_choices: an answer that ends by
//     asking which of two to five options to take offers them as choices,
//     with the explanation under "why".
const (
	// clauseHeadlineMin is CLAUSE_HEADLINE_MIN: the shortest headline a
	// clause cut may leave, its stop included.
	clauseHeadlineMin = 20
	// clauseStop and clauseEnds are CLAUSE_STOP and CLAUSE_ENDS.
	clauseStop = "."
	clauseEnds = ".!?"
	// clauseOpeners and clauseClosers bound what a cut never falls inside.
	clauseOpeners = "(["
	clauseClosers = ")]"
	// clauseMask stands in for each byte inside a bracket while the breaks
	// are found, keeping offsets (CODE_FILLER).
	clauseMask = '\x00'
	// buttonTextMax is the presenter's BUTTON_TEXT_MAX.
	buttonTextMax = 75
	// optionsMin and optionsMax are slack_moments' OPTIONS_MIN and
	// OPTIONS_MAX (BUTTONS_PER_ROW).
	optionsMin = 2
	optionsMax = 5
	// listScanMax is LIST_SCAN_MAX: how many item lines listStart reads up
	// from the end before it gives up.
	listScanMax = 50
	// questionEnd is QUESTION_END.
	questionEnd = "?"
	// leadJoiner is LEAD_JOINER: what may sit between a bold lead and the
	// rest of its line.
	leadJoiner = " :-–—"
	// sentenceStartAfter is SENTENCE_START's lookbehind class.
	sentenceStartAfter = ".!:;"
	// shownOption and shownSummary are SHOWN_OPTION and SHOWN_SUMMARY.
	shownOption  = "%s **%s**"
	shownSummary = "%s — %s"
	// labelTrim is what a label sheds from its end ("Fix it:").
	labelTrim = ":"
)

var (
	clauseBreak = regexp.MustCompile(`(?i)(?:,|;|` + pySpace + `+[—–])` + pySpace + `+|(` + pySpace + `+)(?:due` + pySpace + `+to|because)\b`)
	itemLine    = regexp.MustCompile(`^( {0,3})([-*+•]|\p{Nd}+[.)]|[A-Za-z][.)])` + pySpace + `+([^` + pySpaceChars + `].*?)` + pySpace + `*$`)
	// clauseRelative is CLAUSE_RELATIVE, matched at the rest's start.
	clauseRelative = regexp.MustCompile(`(?i)^(?:which|who|whom|whose|where|that)\b`)
	boldLead       = regexp.MustCompile(`^\*\*(.+?)\*\*|^__(.+?)__`)
	recommended    = regexp.MustCompile(`(?i)` + pySpace + `*\(` + pySpace + `*recommended` + pySpace + `*\)`)
	optionNum      = regexp.MustCompile(`(?i)^option` + pySpace + `+(?:\p{Nd}+|[a-z])\b` + pySpace + `*[:.)\-–—]?` + pySpace + `*`)
	notProse       = regexp.MustCompile(`^` + pySpace + `*[>|]`)
	choiceWord     = regexp.MustCompile(`(?i)\b(?:which|or)\b`)
	yesNoOpener    = regexp.MustCompile(`(?i)^(?:shall|should|can|could|may|might|will|would|do|does|did|is|are|am|was|were|want|have|has|ok|okay|sounds?|looks?)\b`)
	// proceedQuestion is slack_moments.PROCEED_QUESTION.
	proceedQuestion = regexp.MustCompile(`(?i)^(?:(?:shall|should|can|may)\s+(?:i|we)|(?:is\s+it\s+)?ok(?:ay)?\s+to)\b.*?\b(?:proceed|continue|go\s+ahead)\b` +
		`|^do\s+you\s+approve\b` +
		`|^(?:(?:does\s+(?:this|that|it|the)|do\s+(?:these|they|those|the))(?:\s+[\w-]+){0,2}\s+)?(?:sounds?|looks?)\s+(?:good|ok(?:ay)?|right|fine)` +
		`(?:\s+to\s+you)?\s*\?$` +
		`|^(?:ok(?:ay)?|agreed|any\s+objections)\s*\?$`)
)

// clauseCut is slack_presenter.clause_cut: the headline and the rest for a
// first sentence longer than the headline cap, cut at its last clause break
// outside a code span, a bracket and a bold run that leaves a headline of
// clauseHeadlineMin to chatHeadlineMax code points once plain, a stop added;
// false when no break does. The rest's first word is capitalized as
// as_line capitalizes one.
func clauseCut(sentence string) (string, string, bool) {
	held, spans := holdCode(sentence)
	masked := []byte(held)
	depth := 0
	for i, r := range held {
		if strings.ContainsRune(clauseOpeners, r) {
			depth++
		}
		if depth > 0 {
			for k := 0; k < utf8.RuneLen(r); k++ {
				masked[i+k] = clauseMask
			}
		}
		if strings.ContainsRune(clauseClosers, r) && depth > 0 {
			depth--
		}
	}
	var head, rest string
	found := false
	for _, m := range clauseBreak.FindAllStringSubmatchIndex(string(masked), -1) {
		h := strings.TrimRightFunc(held[:m[0]], isPySpace)
		connector := m[2] >= 0
		from := m[1]
		if connector {
			from = m[3]
		}
		r := pyStrip(held[from:])
		if r == "" || strings.Count(h, "**")%2 == 1 || strings.Count(h, "__")%2 == 1 {
			continue
		}
		if !connector && clauseRelative.MatchString(r) {
			continue
		}
		if last, _ := utf8.DecodeLastRuneInString(h); h != "" && !strings.ContainsRune(clauseEnds, last) {
			h += clauseStop
		}
		n := runeLen(plainLine(restoreCode(h, spans, false)))
		if n > chatHeadlineMax {
			// Every later break leaves a longer headline.
			break
		}
		if n >= clauseHeadlineMin {
			head, rest, found = h, r, true
		}
	}
	if !found {
		return "", "", false
	}
	head, rest = restoreCode(head, spans, false), restoreCode(rest, spans, false)
	if lowerWordAt(rest) {
		rest = strings.ToUpper(rest[:1]) + rest[1:]
	}
	return head, rest, true
}

// lowerWordAt is LOWER_WORD's match at the start of s: a run of ASCII
// lowercase letters followed by neither a word character nor a dash.
func lowerWordAt(s string) bool {
	n := 0
	for n < len(s) && s[n] >= 'a' && s[n] <= 'z' {
		n++
	}
	if n == 0 {
		return false
	}
	next, _ := utf8.DecodeRuneInString(s[n:])
	return n == len(s) || !(isWord(next) || next == '-')
}

// answerOption is one option an answer offers: its label, whether it is the
// recommended one, and the line shown for it.
type answerOption struct {
	label   string
	primary bool
	shown   string
}

// answerOffer is answer_choices' result: the shown text, the folded text,
// the question, and the options, the first recommended one primary.
type answerOffer struct {
	shown, folded, question string
	options                 []answerOption
}

// listItem is one list item: its marker, its text and the lines that
// continue it.
type listItem struct {
	marker, text string
	more         []string
}

// notProseLine is slack_moments._not_prose.
func notProseLine(line string) bool {
	return headingLine.MatchString(line) || listMarker.MatchString(line) || itemLine.MatchString(line) ||
		opensFence(line) || notProse.MatchString(line)
}

func indentOf(line string) int { return len(line) - len(strings.TrimLeft(line, " ")) }

// markerKind is _marker_kind: numbered, lettered, or neither.
func markerKind(marker string) [2]bool {
	r, _ := utf8.DecodeRuneInString(marker)
	return [2]bool{unicode.IsDigit(r), unicode.IsLetter(r)}
}

// listItems is _items: lines as one list's items, or false when they are
// not one.
func listItems(lines []string) ([]listItem, bool) {
	if len(lines) == 0 {
		return nil, false
	}
	first := itemLine.FindStringSubmatch(lines[0])
	if first == nil {
		return nil, false
	}
	indent, kind := len(first[1]), markerKind(first[2])
	var items []listItem
	for _, line := range lines {
		if m := itemLine.FindStringSubmatch(line); m != nil && len(m[1]) == indent && markerKind(m[2]) == kind {
			items = append(items, listItem{marker: m[2], text: m[3]})
		} else if pyStrip(line) == "" || indentOf(line) > indent {
			items[len(items)-1].more = append(items[len(items)-1].more, line)
		} else {
			return nil, false
		}
	}
	return items, true
}

// listStart is _list_start: where the longest list ending lines starts.
func listStart(lines []string) (int, bool) {
	found, ok, seen := 0, false, 0
	for start := len(lines) - 1; start >= 0; start-- {
		line := lines[start]
		if itemLine.MatchString(line) {
			if seen++; seen > listScanMax {
				return 0, false
			}
			if _, valid := listItems(lines[start:]); valid {
				found, ok = start, true
			}
		} else if pyStrip(line) != "" && indentOf(line) == 0 {
			break
		}
	}
	return found, ok
}

// lastSentence is SENTENCE_START.split(text)[-1]: text after its last
// whitespace run that follows one of sentenceStartAfter.
func lastSentence(text string) string {
	rs := []rune(text)
	from := 0
	for i := 1; i < len(rs); i++ {
		if isPySpace(rs[i]) && !isPySpace(rs[i-1]) && strings.ContainsRune(sentenceStartAfter, rs[i-1]) {
			j := i
			for j < len(rs) && isPySpace(rs[j]) {
				j++
			}
			from = j
		}
	}
	return string(rs[from:])
}

// asksChoice is _asks_choice.
func asksChoice(question string) bool {
	text := plainLine(question)
	if !strings.HasSuffix(text, questionEnd) {
		return false
	}
	asked := lastSentence(text)
	return choiceWord.MatchString(asked) || !(proceedQuestion.MatchString(asked) || yesNoOpener.MatchString(asked))
}

// optionOf is _option: the option an item makes, or false when it cannot be
// a button.
func optionOf(item listItem) (answerOption, bool) {
	text := item.text
	lead := boldLead.FindStringSubmatchIndex(text)
	said, raw := "", text
	if lead != nil {
		said = strings.TrimLeft(text[lead[1]:], leadJoiner)
		if lead[2] >= 0 {
			raw = text[lead[2]:lead[3]]
		} else {
			raw = text[lead[4]:lead[5]]
		}
	}
	label := plainLine(raw)
	primary := recommended.MatchString(label)
	label = strings.Join(strings.Fields(optionNum.ReplaceAllString(recommended.ReplaceAllString(label, ""), "")), " ")
	label = pyStrip(strings.TrimRight(label, labelTrim))
	if label == "" || (lead == nil && runeLen(label) > buttonTextMax) {
		return answerOption{}, false
	}
	label = clipRunes(label, buttonTextMax)
	if said == "" {
		var prose []string
		for _, line := range item.more {
			stripped := pyStrip(line)
			if stripped == "" || notProseLine(stripped) {
				break
			}
			prose = append(prose, stripped)
		}
		said = strings.Join(prose, " ")
	}
	held, spans := holdCode(said)
	sentence, _ := firstSentence(held)
	summary := pyStrip(restoreCode(sentence, spans, false))
	shown := ""
	if summary != "" {
		shown = fmt.Sprintf(shownSummary, fmt.Sprintf(shownOption, item.marker, label), summary)
	}
	return answerOption{label: label, primary: primary, shown: shown}, true
}

func trimBlankTail(lines []string) []string {
	for len(lines) > 0 && pyStrip(lines[len(lines)-1]) == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

// answerChoices is slack_moments.answer_choices: the options an answer's
// rest ends by asking between, or false when it asks none.
func answerChoices(rest string) (answerOffer, bool) {
	lines := trimBlankTail(strings.Split(strings.Trim(rest, "\n"), "\n"))
	para := len(lines)
	for para > 0 && pyStrip(lines[para-1]) != "" {
		para--
	}
	closing := lines[para:]
	joined := make([]string, len(closing))
	prose := len(closing) > 0
	for i, line := range closing {
		joined[i] = pyStrip(line)
		if notProseLine(line) {
			prose = false
		}
	}
	question := strings.Join(joined, " ")
	var region, head []string
	if prose && asksChoice(question) {
		before := trimBlankTail(append([]string(nil), lines[:para]...))
		if start, ok := listStart(before); ok {
			region, head = before[start:], before[:start]
		}
	} else {
		start, ok := listStart(lines)
		asked := start - 1
		for asked >= 0 && pyStrip(lines[asked]) == "" {
			asked--
		}
		if !ok || asked < 0 || notProseLine(lines[asked]) || !asksChoice(lines[asked]) {
			return answerOffer{}, false
		}
		question, region, head = pyStrip(lines[asked]), lines[start:], lines[:asked]
	}
	items, ok := listItems(region)
	if !ok || len(items) < optionsMin || len(items) > optionsMax {
		return answerOffer{}, false
	}
	offer := answerOffer{question: question}
	seen := map[string]bool{}
	firstPrimary := -1
	continued := false
	for i, item := range items {
		option, ok := optionOf(item)
		key := strings.ToLower(option.label)
		if !ok || seen[key] {
			return answerOffer{}, false
		}
		seen[key] = true
		if option.primary && firstPrimary < 0 {
			firstPrimary = i
		}
		option.primary = false
		offer.options = append(offer.options, option)
		if len(item.more) > 0 {
			continued = true
		}
	}
	if firstPrimary >= 0 {
		offer.options[firstPrimary].primary = true
	}
	head = trimBlankTail(append([]string(nil), head...))
	leadEnd := len(head)
	for i, line := range head {
		if pyStrip(line) == "" {
			leadEnd = i
			break
		}
	}
	var lead []string
	if len(head) > 0 && !notProseLine(head[0]) {
		lead = head[:leadEnd]
	}
	folded := append([]string(nil), head[len(lead):]...)
	if continued {
		folded = append(append(folded, ""), region...)
	}
	// The options' lines show only when each says more than its button does.
	shownLines := make([]string, len(offer.options))
	every := true
	for i, option := range offer.options {
		shownLines[i] = option.shown
		every = every && option.shown != ""
	}
	optionsShown := ""
	if every {
		optionsShown = strings.Join(shownLines, "\n")
	}
	var shownParts []string
	for _, part := range []string{pyStrip(strings.Join(lead, "\n")), optionsShown} {
		if part != "" {
			shownParts = append(shownParts, part)
		}
	}
	offer.shown = strings.Join(shownParts, "\n\n")
	offer.folded = pyStrip(strings.Join(folded, "\n"))
	return offer, true
}
