package answerscrub

import (
	"encoding/json"
	"testing"
)

// FuzzScrubInlineTableCells: no table cell, whatever its code spans, blanks,
// tabs, pipes or key, panics the scrub or the seen set's tool-result read.
// The seeds are the code-span edges a panic once lived at: a span whose
// content ends in a blank or a tab, at the cell's end, with or without a
// header row, a scheme word before it, and text after it.
func FuzzScrubInlineTableCells(f *testing.F) {
	for _, seed := range []string{
		"| DB password | `synthtoken0001abcd ` |\n",
		"| DB password | `synthtoken0001abcd\t` |\n",
		"|pAsswd|`0 ` ",
		"| Key | Value |\n|---|---|\n| api_token | Bearer `sy`nthtoken0001abcd |\n",
		"| Key | Value |\n|---|---|\n| password | `appuser` synthtoken0001abcd |\n",
		"| Key | Value |\n|---|---|\n| password | `admin` / 12345 |\n",
		"| password | `` |\n",
		"| password | ` ` |\n",
		"| password | `a|b` |\n",
		"| password |`\t\t`|",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, s string) {
		_ = ScrubInline(s)
		raw, err := json.Marshal(s)
		if err != nil {
			return
		}
		var seen Seen
		seen.CollectToolResult(raw)
		_ = Scrub(s, &seen)
	})
}
