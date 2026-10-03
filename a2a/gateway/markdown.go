package gateway

import (
	"net/url"
	"regexp"
	"strings"
)

// The chat adapters relay executor output -- markdown, by the executors'
// convention -- into surfaces that read a dialect of it. This file holds the
// rules Slack's mrkdwn and Google Chat's text format share: a bold pair is one
// star, not two; a code span is verbatim to the markdown rules; a markdown
// link becomes <url|text>; and a link whose label carries a URL must not
// name a host the link does not open. What differs per surface -- the
// escaping or defang that runs over the whole text before any of this, and
// the exact URL class -- stays in the adapter that owns it.

// mdCodeSpanRE matches the spans markdown renders verbatim, which no rewrite
// below may read through: a fenced block (an unclosed fence runs to the end
// of the text, as CommonMark reads it), a double-backtick span (which may
// carry a single backtick), and an inline span. Executor output is full of
// them -- `**kwargs`, `**/*.yaml`, `a ** b` -- and a bold rewrite that read
// through them altered the answer before the user did. Fence length is not
// tracked: a four-backtick fence closes at the first three.
var mdCodeSpanRE = regexp.MustCompile("(?s)```.*?(?:```|\\z)|``(?:[^`]|`[^`])*``|`[^`\n]*`")

// mdBoldRE matches a closed bold pair: `**` on both sides of one-line
// content that starts and ends on a non-space, non-star character and holds
// no `**` of its own (a single star followed by at least one more content
// character, as in `**Note: *not* recommended**`, is emphasis inside the
// pair and stays; one in the last position before the closing `**` is not,
// so `**a*b**` is left as written), with an optional third star on each
// side for the bold-italic form. Each side's third star is its own group so
// the rewrite can see whether the two match. `**kwargs` has no closing pair,
// `a ** b` has no content, `***` has neither, and none of them matches.
var mdBoldRE = regexp.MustCompile(`\*\*(\*?)([^*\s](?:(?:[^*\n]|\*[^*\n])*[^*\s])?)\*\*(\*?)`)

// mdURLLabelRE finds the URLs a link's label carries, anywhere in the label
// and in any case: a scheme, then the authority that follows it, up to the
// first character that ends one -- whitespace, a path, query or fragment
// delimiter, a pipe, an escaped or raw angle bracket, an entity's ampersand,
// a quote, a closing parenthesis. The scheme alone is a match, so a label
// with nothing readable after it is a claim with no host rather than no
// claim. A label that carries one is a claim about where the link goes,
// which linkLabelMisnamesHost checks; a bare hostname is prose and makes no
// claim the adapter can check, and so is a scheme Slack and Chat would not
// auto-link (`hxxps://`, `https:/`).
var mdURLLabelRE = regexp.MustCompile(`(?i)https?://[^\s/?#&<>|'")]*`)

// mdLabelMarksRE matches what a label can carry that does not render:
// emphasis and code marks, and Unicode format characters (a zero-width
// space, a joiner, a soft hyphen). They come out before the label is read
// for a URL, so `**https**://good.example` and `https:/​/good.example`
// are read as they render.
var mdLabelMarksRE = regexp.MustCompile("[*_`\\p{Cf}]")

// mdHostTrailingPunct is the sentence punctuation a label's URL may end on
// (`Read https://x.example.`); Go's URL parser admits it into the host, so
// it is trimmed before the host is compared.
const mdHostTrailingPunct = ".,;!"

// mdFence is the three-backtick fence that opens and closes a fenced block.
const mdFence = "```"

// mdMaskByte stands in for every byte of a protected range in the copy
// rewriteBoldPairs searches: not a star, not a space, so it can neither
// open nor close a pair nor break the content of one that wraps the range.
const mdMaskByte = 'x'

// mdStrong and mdEmphasis are the one-character marks both surfaces read:
// *bold*, _italic_, and *_both_*.
const (
	mdStrong   = "*"
	mdEmphasis = "_"
)

// chatLinkOpen, chatLinkSep and chatLinkClose spell the <url|text> link both
// surfaces read.
const (
	chatLinkOpen  = "<"
	chatLinkSep   = "|"
	chatLinkClose = ">"
)

// rewriteMarkdown translates the two markdown forms the relay emits, bold
// pairs and links, into the form both chat surfaces read, leaving code spans
// as written. It runs after the surface's own escaping or defang: Slack's
// escaping and Chat's mention defang cover the whole text, code included --
// a prompt-injected <!channel> or <users/all> in a code span is as live as
// one in prose -- and Chat's link defang covers everything outside closed
// code (rewriteOutsideCode), so no control sequence the executor wrote is live
// by the time rewriteLinks writes the adapter's own, and what reaches here
// needs no further escaping. Bold first, so a pair that wraps a whole link
// still converts; the link pass finds its code spans again afterwards,
// since the bold pass moved them without touching a backtick.
func rewriteMarkdown(s string, linkRE *regexp.Regexp) string {
	s = rewriteBoldPairs(s, mdCodeSpanRE.FindAllStringIndex(s, -1), linkRE)
	return rewriteLinks(s, mdCodeSpanRE.FindAllStringIndex(s, -1), linkRE)
}

// rewriteOutsideCode applies rewrite to each stretch of s that lies outside
// a closed code span (mdCodeSpanRE: a fenced block, a double-backtick span,
// an inline span), and keeps the spans as written. It is the segmentation
// the bold and link passes read code through, offered to a surface's own
// pass that runs before them -- Chat's link defang, which would otherwise
// read a shell's `<(gen)|` or `<f|` as a link opener and put a space in the
// command. The stretches are rewritten one at a time, so a rewrite that
// finds a sequence in one cannot read into the next; a sequence that is
// recognised by its opener alone is still found when a span cuts its text
// short.
//
// An unclosed fence (mdFenceUnclosed) is not kept: it is rewritten with the
// prose around it. The bold and link passes read one as a fence to the end
// of the text, as CommonMark does, but a pass that defuses a control
// sequence cannot afford to. The adapter sees a chunk of the result
// (Gateway.post, chatChunks), not the whole of it; the chunker closes a
// block it cuts and reopens it in the next chunk, so every chunk is
// balanced and an unclosed fence should only come from the executor's own
// output -- but this pass is the last line, and does not lean on that: a
// fence that never closes is read as prose, and the defang errs to
// defanging. The cost is a space in a `<f|` the executor left in a fence it
// forgot to close.
func rewriteOutsideCode(s string, rewrite func(string) string) string {
	var b strings.Builder
	end := 0
	for _, r := range mdCodeSpanRE.FindAllStringIndex(s, -1) {
		if mdFenceUnclosed(s[r[0]:r[1]]) {
			continue
		}
		b.WriteString(rewrite(s[end:r[0]]))
		b.WriteString(s[r[0]:r[1]])
		end = r[1]
	}
	b.WriteString(rewrite(s[end:]))
	return b.String()
}

// mdFenceUnclosed reports whether span, one mdCodeSpanRE match, is a fence
// that ran to the end of the text without its closing fence: it opens with
// one and does not also close with one. A double-backtick or inline span
// does not open with a fence, and is always closed.
func mdFenceUnclosed(span string) bool {
	return strings.HasPrefix(span, mdFence) &&
		(len(span) < 2*len(mdFence) || !strings.HasSuffix(span, mdFence))
}

// insideAny reports whether offset i of a string falls inside one of the
// [start, end) ranges.
func insideAny(i int, ranges [][]int) bool {
	for _, r := range ranges {
		if r[0] <= i && i < r[1] {
			return true
		}
	}
	return false
}

// maskRanges returns s with every byte inside one of the [start, end)
// ranges replaced by mdMaskByte, newlines excepted, so a search over the
// result reads nothing inside them and still sees where each line ends.
// Byte for byte, so an offset into the result is the same offset into s.
func maskRanges(s string, ranges [][]int) string {
	b := []byte(s)
	for _, r := range ranges {
		for i := r[0]; i < r[1]; i++ {
			if b[i] != '\n' {
				b[i] = mdMaskByte
			}
		}
	}
	return string(b)
}

// rewriteBoldPairs rewrites each closed bold pair in s to the single-star
// form both surfaces read, and a matched bold-italic triple to *_x_*. The
// pairs are sought in a copy of s with every protected range blanked
// (maskRanges): a code span, the destination of a markdown link (one linkRE
// matches), and the whole of a link rewriteLinks will refuse (linkRefused).
// A star inside one is not a candidate for either side of a pair, so a `**`
// in a code span or a URL never opens or closes one and is never altered,
// a refused link stays the markdown it arrived as, and a pair that wraps a
// whole code span or a whole link converts, since only its own stars
// change, even when the span holds a `**` of its own (**`**kwargs`**). The
// mask keeps newlines, so a pair is still one line of content. A triple
// closed by a double, or the reverse, is not a pair and is left as written.
// A bare URL is not a shape this recognises (both surfaces auto-link it),
// so a closed pair inside one is rewritten.
func rewriteBoldPairs(s string, code [][]int, linkRE *regexp.Regexp) string {
	protected := append([][]int(nil), code...)
	for _, m := range linkRE.FindAllStringSubmatchIndex(s, -1) {
		if linkRefused(s, m, code) {
			protected = append(protected, []int{m[0], m[1]})
			continue
		}
		protected = append(protected, []int{m[4], m[5]})
	}
	var b strings.Builder
	end := 0
	for _, m := range mdBoldRE.FindAllStringSubmatchIndex(maskRanges(s, protected), -1) {
		b.WriteString(s[end:m[0]])
		end = m[1]
		open, content, close := s[m[2]:m[3]], s[m[4]:m[5]], s[m[6]:m[7]]
		switch {
		case open != close:
			b.WriteString(s[m[0]:m[1]])
		case open == "":
			b.WriteString(mdStrong + content + mdStrong)
		default:
			b.WriteString(mdStrong + mdEmphasis + content + mdEmphasis + mdStrong)
		}
	}
	b.WriteString(s[end:])
	return b.String()
}

// rewriteLinks rewrites each markdown link linkRE matches in s to <url|text>,
// label and destination as written, and leaves a link linkRefused names as
// the markdown it arrived as.
func rewriteLinks(s string, code [][]int, linkRE *regexp.Regexp) string {
	var b strings.Builder
	end := 0
	for _, m := range linkRE.FindAllStringSubmatchIndex(s, -1) {
		b.WriteString(s[end:m[0]])
		end = m[1]
		if linkRefused(s, m, code) {
			b.WriteString(s[m[0]:m[1]])
			continue
		}
		b.WriteString(chatLinkOpen + s[m[4]:m[5]] + chatLinkSep + s[m[2]:m[3]] + chatLinkClose)
	}
	b.WriteString(s[end:])
	return b.String()
}

// linkRefused reports whether the markdown link linkRE matched at m in s is
// one the adapters will not convert, on either of two grounds. Its brackets
// open or close inside a code span: it is code, not a link (a label that
// merely contains a code span still converts). Or its label carries a URL
// naming a host other than the destination's: both surfaces render the
// label, so [https://good.example](https://evil.example) would read as a
// link to good.example that opens evil.example, and the pipe refusal in
// each adapter's URL class closes the other route to that display.
func linkRefused(s string, m []int, code [][]int) bool {
	return insideAny(m[0], code) || insideAny(m[1]-1, code) ||
		linkLabelMisnamesHost(s[m[2]:m[3]], s[m[4]:m[5]])
}

// linkLabelMisnamesHost reports whether label, read as it renders
// (mdLabelMarksRE), carries a URL (mdURLLabelRE) whose host is not dest's,
// compared case-insensitively and without the port: a label naming the host
// of a destination on another port is honest. Every URL in the label is
// checked, so an honest first one does not cover a second. A carried URL
// that does not parse, that has no host, or that carries userinfo
// (`https://good.example@evil.example` reads as good.example and parses as
// evil.example) is a misnaming too: it makes a claim the adapter cannot
// vouch for. A label with no scheme in it makes no claim this check can
// read, and is prose.
func linkLabelMisnamesHost(label, dest string) bool {
	claims := mdURLLabelRE.FindAllString(mdLabelMarksRE.ReplaceAllString(label, ""), -1)
	if len(claims) == 0 {
		return false
	}
	du, err := url.Parse(dest)
	if err != nil {
		return true
	}
	for _, claim := range claims {
		lu, err := url.Parse(strings.TrimRight(claim, mdHostTrailingPunct))
		if err != nil || lu.User != nil || lu.Hostname() == "" || !strings.EqualFold(lu.Hostname(), du.Hostname()) {
			return true
		}
	}
	return false
}
