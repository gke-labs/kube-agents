// Package askkind classifies an ask by its words into a mark.ask token
// (docs/designs/spec-a2a-payloads.md, "mark"), a port of the today
// presenter's arrival_reaction (agents/platform/scripts/slack_presenter.py).
// The hermes-bridge marks each heartbeat with it, and the A2A gateway draws it
// on the ask the moment it accepts the message, so the reaction is there
// before any model call however fast the turn ends.
package askkind

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// The ask kinds, mark.ask tokens for arrival_reaction's emoji, tried in
// arrival_reaction's order: the first match wins. A question outranks the
// incident words, so "is checkout crashlooping?" is investigate while
// "checkout is down" is incident.
const (
	Question    = "question"
	Investigate = "investigate"
	Fleet       = "fleet"
	Upgrade     = "upgrade"
	Cost        = "cost"
	Security    = "security"
	Change      = "change"
	Board       = "board"
	Incident    = "incident"
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
	mention       = regexp.MustCompile(`<[@#!][^<>]*>`)
	firstWord     = regexp.MustCompile(`[A-Za-z']+`)
	classes       = []struct {
		words *regexp.Regexp
		kind  string
	}{
		{changeWords, Change},
		{upgradeWords, Upgrade},
		{boardWords, Board},
		{costWords, Cost},
		{securityWords, Security},
		{fleetWords, Fleet},
	}
	questionOpeners = map[string]bool{
		"is": true, "are": true, "was": true, "were": true, "why": true, "what": true, "whats": true,
		"what's": true, "how": true, "which": true, "who": true, "when": true, "where": true, "does": true,
		"do": true, "did": true, "can": true, "could": true, "should": true, "will": true, "would": true,
		"has": true, "have": true, "check": true, "show": true, "list": true, "tell": true, "explain": true,
		"describe": true,
	}
	// pyFolds are the non-ASCII letters Python's re.IGNORECASE matches to an
	// ASCII keyword letter: dotted and dotless i, long s, the Kelvin sign (an
	// escape, since a normalising editor turns the raw rune into an ASCII K).
	pyFolds = map[rune]rune{'İ': 'i', 'ı': 'i', 'ſ': 's', '\u212a': 'k'}
)

// Kind is the mark.ask token for an ask, from its words alone.
func Kind(text string) string {
	stripped := pyStrip(mention.ReplaceAllString(text, " "))
	clean := asciiWords(stripped)
	for _, c := range classes {
		if c.words.MatchString(clean) {
			return c.kind
		}
	}
	// The opener check reads the ask as written: the presenter's FIRST_WORD
	// is case-sensitive ASCII and never folds, so "İs" opens on "s".
	if !isQuestion(stripped) && incidentWords.MatchString(clean) {
		return Incident
	}
	if investigateWords.MatchString(clean) || incidentWords.MatchString(clean) {
		return Investigate
	}
	return Question
}

func isQuestion(text string) bool {
	if strings.HasSuffix(strings.TrimRightFunc(text, isPySpace), "?") {
		return true
	}
	first := firstWord.FindString(text)
	return first != "" && questionOpeners[strings.ToLower(first)]
}

// isPySpace is Python's str.isspace: unicode.IsSpace and the separators
// U+001C-U+001F, which Go does not count.
func isPySpace(r rune) bool { return unicode.IsSpace(r) || (r >= '\x1c' && r <= '\x1f') }

// pyStrip is Python's str.strip(): isPySpace trimmed from both ends.
func pyStrip(s string) string { return strings.TrimFunc(s, isPySpace) }

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
