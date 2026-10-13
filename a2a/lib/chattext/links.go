package chattext

import (
	"net/url"
	"regexp"
	"strings"
)

// urlLabelRE finds the URLs a link's label carries, anywhere in the label
// and in any case: a scheme, then the authority that follows it, up to the
// first character that ends one -- whitespace, a path, query or fragment
// delimiter, a pipe, an escaped or raw angle bracket, an entity's ampersand,
// a quote, a closing parenthesis. The scheme alone is a match, so a label
// with nothing readable after it is a claim with no host rather than no
// claim. A label that carries one is a claim about where the link goes,
// which LinkLabelMisnamesHost checks; a bare hostname is prose and makes no
// claim the adapter can check, and so is a scheme Slack and Chat would not
// auto-link (`hxxps://`, `https:/`).
var urlLabelRE = regexp.MustCompile(`(?i)https?://[^\s/?#&<>|'")]*`)

// labelMarksRE matches what a label can carry that does not render:
// emphasis and code marks, and Unicode format characters (a zero-width
// space, a joiner, a soft hyphen). They come out before the label is read
// for a URL, so `**https**://good.example` and `https:/​/good.example`
// are read as they render.
var labelMarksRE = regexp.MustCompile("[*_`\\p{Cf}]")

// hostTrailingPunct is the sentence punctuation a label's URL may end on
// (`Read https://x.example.`); Go's URL parser admits it into the host, so
// it is trimmed before the host is compared.
const hostTrailingPunct = ".,;!"

// LinkLabelMisnamesHost reports whether label, read as it renders
// (labelMarksRE), carries a URL (urlLabelRE) whose host is not dest's,
// compared case-insensitively and without the port: a label naming the host
// of a destination on another port is honest. Every URL in the label is
// checked, so an honest first one does not cover a second. A carried URL
// that does not parse, that has no host, or that carries userinfo
// (`https://good.example@evil.example` reads as good.example and parses as
// evil.example) is a misnaming too: it makes a claim the adapter cannot
// vouch for. A label with no scheme in it makes no claim this check can
// read, and is prose.
func LinkLabelMisnamesHost(label, dest string) bool {
	claims := urlLabelRE.FindAllString(labelMarksRE.ReplaceAllString(label, ""), -1)
	if len(claims) == 0 {
		return false
	}
	du, err := url.Parse(dest)
	if err != nil {
		return true
	}
	for _, claim := range claims {
		lu, err := url.Parse(strings.TrimRight(claim, hostTrailingPunct))
		if err != nil || lu.User != nil || lu.Hostname() == "" || !strings.EqualFold(lu.Hostname(), du.Hostname()) {
			return true
		}
	}
	return false
}
