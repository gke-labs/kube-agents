package answerscrub

import (
	"encoding/json"
	"testing"
)

// scrubFuzzSeeds are inputs that have reached the scrub's edges: table cells
// with code spans (a code span ending on the cell's trailing space once cut a
// slice past its end), empty and whitespace-only spans, and a pipe inside a
// span. Every value is synthetic.
var scrubFuzzSeeds = []string{
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
	"password=synthetic-pass-0001 --token synthetic-token-0002",
	`{"stdout":"API_KEY=synthetic-pass-0001\n","stderr":"","exit_code":0}`,
	"",
	Redacted,
}

// FuzzScrub runs the whole answer path on one text: the text's own secrets
// collected from it as a tool result, then Scrub with that seen set. A panic
// anywhere in it is the failure: the bridge runs this on model and user text,
// and a panic there ends the task.
func FuzzScrub(f *testing.F) {
	for _, s := range scrubFuzzSeeds {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		_ = ScrubInline(s)
		raw, err := json.Marshal(s)
		if err != nil {
			t.Skip()
		}
		var seen Seen
		seen.CollectToolResult(raw)
		_ = Scrub(s, &seen)
		_ = seen.Redact(s)
	})
}

// FuzzCollectToolResult feeds arbitrary bytes as a tool result, valid JSON or
// not, then scrubs a text with what was collected. Tool results come from the
// harness's tools, so nothing about their shape can be assumed.
func FuzzCollectToolResult(f *testing.F) {
	for _, s := range scrubFuzzSeeds {
		f.Add([]byte(s), s)
		if raw, err := json.Marshal(s); err == nil {
			f.Add(raw, s)
		}
	}
	f.Add([]byte(`{"a":{"b":[{"password":"synthetic-pass-0001"}]}}`), "synthetic-pass-0001")
	f.Add([]byte(`[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]`), "")
	f.Fuzz(func(t *testing.T, raw []byte, text string) {
		var seen Seen
		seen.CollectToolResult(json.RawMessage(raw))
		_ = Scrub(text, &seen)
		_ = seen.Values()
		_ = seen.Size()
	})
}
