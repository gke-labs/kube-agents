package workeradapter

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// TestTruncateKeepsTheBudgetAcrossMultibyteRunes pins the property the first
// spelling lost: the budget applies to the whole string, not just its ASCII
// prefix. Both cases below are what that bug looked like from the outside --
// a status message that silently lost its content the moment a non-ASCII rune
// appeared anywhere in it.
func TestTruncateKeepsTheBudgetAcrossMultibyteRunes(t *testing.T) {
	for _, tc := range []struct {
		name  string
		input string
		limit int
		want  int // minimum bytes of real content expected before the ellipsis
	}{
		{"multibyte early in a long string", strings.Repeat("a", 10) + "é" + strings.Repeat("b", 200), 50, 45},
		{"entirely multibyte", strings.Repeat("é", 100), 50, 45},
		{"pure ascii", strings.Repeat("a", 200), 50, 45},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := truncate(tc.input, tc.limit)
			body := strings.TrimSuffix(got, "…")
			if len(body) < tc.want {
				t.Errorf("kept %d bytes of a %d-byte budget: %q", len(body), tc.limit, got)
			}
			if len(body) > tc.limit {
				t.Errorf("kept %d bytes, over the %d-byte budget", len(body), tc.limit)
			}
			if !utf8.ValidString(got) {
				t.Errorf("cut through a rune: %q", got)
			}
		})
	}
}

// A string already inside the budget is returned untouched, ellipsis included.
func TestTruncateLeavesShortStringsAlone(t *testing.T) {
	if got := truncate("héllo", 64); got != "héllo" {
		t.Errorf("got %q, want the input unchanged", got)
	}
}
