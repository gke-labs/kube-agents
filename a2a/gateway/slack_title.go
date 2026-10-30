package gateway

import (
	"regexp"
	"strings"
	"unicode"

	"golang.org/x/text/unicode/norm"
)

// The session title rules, ported from session_title in
// agents/platform/scripts/slack_status.py (#2312), which today's path uses
// and whose tests slack_title_test.go carries over. agents.sessions.rename
// refuses a whole title with invalid_name when it runs over 80 code points or
// holds one character outside a narrow set, measured against Slack: a
// letter, a decimal digit or a combining mark in any script, a symbol in the
// emoji blocks below, and of everything else only slackTitleKept. A letter
// with a compatibility decomposition (ª, ﬁ, 𝐀) is refused too, so the title
// is NFKC-normalized first, which also turns … into ... and ² into 2. A
// refused character an ask is likely to hold becomes an ASCII stand-in, a
// format character is dropped, and any other becomes a space. A slash
// between two word characters becomes a hyphen, so kube-system/coredns
// stays one name. A combining mark goes with its base, so one on a refused
// character is dropped. A clipped title ends on "...", since "…" is refused.
const (
	slackTitleMax      = 80
	slackTitleKept     = " -'\"&|_!?.,()=“”"
	slackTitleEllipsis = "..."
	// slackTitleClipMinShare: a word-aligned clip shorter than 1/this of the
	// hard cut drops too much, and cuts hard instead.
	slackTitleClipMinShare = 2
	// slackTitleQuestionEnd and slackTitleQuestionMinWords end the ask's
	// first question (slackFirstQuestion), as slack_status.py's QUESTION_END
	// and QUESTION_MIN_WORDS do. Only "?": a "." or "!" also closes "e.g.",
	// "1." and "Hi!", and a thread keeps its first title.
	slackTitleQuestionEnd      = '?'
	slackTitleQuestionMinWords = 2
)

// slackTitleReplacements are the ASCII stand-ins for refused characters an
// ask is likely to hold.
var slackTitleReplacements = map[rune]string{
	':': ",", ';': ",", '·': ",", '•': ",",
	'‒': " - ", '–': " - ", '—': " - ", '―': " - ",
	'−': "-", '‐': "-", '‘': "'", '’': "'",
	'[': "(", ']': ")", '{': "(", '}': ")",
	'%': " percent", '×': "x", '、': ",", '。': ".", '«': "\"", '»': "\"",
}

// slackTitleEmoji are the symbol blocks Slack accepts, inclusive: the
// Miscellaneous Symbols and Dingbats, and the emoji planes from Miscellaneous
// Symbols and Pictographs to Symbols and Pictographs Extended-A. Regional
// indicators (flags) sit below them and are refused.
var slackTitleEmoji = [][2]rune{{0x2600, 0x27BF}, {0x1F300, 0x1FAFF}}

// Slack markup in an ask: a user or channel mention, and a link with or
// without its label. Mentions go, a channel keeps its name, a link its label,
// and a bare link goes too, since every ":" and "/" in it would be replaced.
// RE2 is linear, so an ask of unclosed "<" cannot stall the event pump.
var (
	slackTitleMention       = regexp.MustCompile(`<[@!][^<>]*>`)
	slackTitleChannel       = regexp.MustCompile(`<#[A-Z0-9]+\|([^<>]*)>`)
	slackTitleLink          = regexp.MustCompile(`<([^<>|]+)(?:\|([^<>]*))?>`)
	slackTitleWhitespace    = regexp.MustCompile(`\s+`)
	slackTitleRepeatedComma = regexp.MustCompile(`\s*,[\s,]*`)
)

// slackFirstQuestion is text up to the end of its first question, or all of
// it when it asks none. A question ends at a run of slackTitleQuestionEnd
// followed by whitespace, which stays with it, when it closes a word (a run of
// non-space holding a word character, so punctuation a stripped mention or
// link left behind does not count) and the text so far holds
// slackTitleQuestionMinWords such words.
func slackFirstQuestion(text string) string {
	runes := []rune(text)
	words := 0
	worded := false
	for i, r := range runes {
		if unicode.IsSpace(r) {
			worded = false
			continue
		}
		if !worded && slackWordChar(r) {
			worded = true
			words++
		}
		if r == slackTitleQuestionEnd && worded && words >= slackTitleQuestionMinWords &&
			i+1 < len(runes) && unicode.IsSpace(runes[i+1]) {
			return string(runes[:i+1])
		}
	}
	return text
}

// slackSessionTitle is a short title agents.sessions.rename accepts, from
// the ask up to its first question, or "" when nothing survives.
func slackSessionTitle(ask string) string {
	title := slackTitleMention.ReplaceAllString(ask, " ")
	title = slackTitleChannel.ReplaceAllString(title, "$1")
	title = slackTitleLink.ReplaceAllStringFunc(title, func(m string) string {
		if sub := slackTitleLink.FindStringSubmatch(m); len(sub) > 2 && sub[2] != "" {
			return sub[2]
		}
		return " "
	})
	title = norm.NFKC.String(title)
	title = slackFirstQuestion(title)
	title = slackTitleChars(title)
	title = slackTitleWhitespace.ReplaceAllString(title, " ")
	title = strings.Trim(slackTitleRepeatedComma.ReplaceAllString(title, ", "), " ,")
	return slackTitleClip(title)
}

// slackWordChar is a letter, a decimal digit, "_" or a combining mark.
func slackWordChar(r rune) bool {
	return unicode.IsLetter(r) || unicode.Is(unicode.Nd, r) || r == '_' || unicode.Is(unicode.M, r)
}

// slackTitleChar is r if rename accepts it, else its stand-in, nothing or a
// space.
func slackTitleChar(r rune) string {
	if slackWordChar(r) || strings.ContainsRune(slackTitleKept, r) {
		return string(r)
	}
	if unicode.Is(unicode.So, r) || unicode.Is(unicode.Sk, r) {
		for _, block := range slackTitleEmoji {
			if block[0] <= r && r <= block[1] {
				return string(r)
			}
		}
	}
	if stand, ok := slackTitleReplacements[r]; ok {
		return stand
	}
	if unicode.Is(unicode.Cf, r) {
		return ""
	}
	return " "
}

// slackTitleChars maps each character through slackTitleChar, joining a
// slash between words. A combining mark is kept only on a letter, digit or
// emoji kept as itself, the bases Slack was measured to accept one on.
func slackTitleChars(title string) string {
	runes := []rune(title)
	var b strings.Builder
	marksKept, afterWord := false, false
	for i, r := range runes {
		if unicode.Is(unicode.M, r) {
			if marksKept {
				b.WriteRune(r)
			}
			continue
		}
		joins := r == '/' && afterWord && i+1 < len(runes) && slackWordChar(runes[i+1])
		stand := slackTitleChar(r)
		if joins {
			stand = "-"
		}
		same := stand == string(r)
		afterWord = same && slackWordChar(r)
		marksKept = same && !strings.ContainsRune(slackTitleKept, r)
		b.WriteString(stand)
	}
	return b.String()
}

// slackTitleClip cuts title on a word to slackTitleMax code points, the
// ellipsis included.
func slackTitleClip(title string) string {
	runes := []rune(title)
	if len(runes) <= slackTitleMax {
		return title
	}
	hard := string(runes[:slackTitleMax-len(slackTitleEllipsis)])
	cut := hard
	if i := strings.LastIndex(hard, " "); i >= 0 {
		cut = hard[:i]
	}
	// A long unbroken word would otherwise take everything after the last
	// space with it.
	if len([]rune(cut))*slackTitleClipMinShare < len([]rune(hard)) {
		cut = hard
	}
	return strings.TrimRight(cut, " ,") + slackTitleEllipsis
}
