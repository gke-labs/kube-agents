package workeradapter

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode"
	"unicode/utf8"
)

// fileTools are a single-file tool of each harness, with the argument that
// names the file.
var fileTools = []struct{ tool, key string }{
	{"Read", "file_path"},
	{"NotebookEdit", "notebook_path"},
	{"read_file", "path"},
	{"view_file_outline", "path"},
}

// A file name's non-printing characters never reach the title: line breaks,
// escape sequences, bidirectional overrides and zero-width characters are
// dropped, under either harness's tool names. A name that is only such
// characters leaves the bare tool name.
func TestAnActivityTitleDropsAFileNamesNonPrintingCharacters(t *testing.T) {
	for _, c := range []struct{ name, want string }{
		{"deploy\n.yaml", "deploy.yaml"},
		{"report\r\n\tfinal.md", "reportfinal.md"},
		{"\x1b[31mred\x1b[0m.txt", "[31mred[0m.txt"},
		{"invoice\u202egpj.exe", "invoicegpj.exe"},
		{"zero\u200bwidth.go", "zerowidth.go"},
		{"\n\t\u202e", ""},
		// A line or paragraph separator is a line break too.
		{"two\u2028lines.md", "twolines.md"},
	} {
		for _, f := range fileTools {
			got := titleOf(t, f.tool, map[string]any{f.key: "/scratch/" + c.name})
			want := f.tool
			if c.want != "" {
				want += " " + c.want
			}
			if got != want {
				t.Errorf("activityTitle(%s, %q) = %q, want %q", f.tool, c.name, got, want)
			}
		}
	}
}

// A file name longer than activityFileNameMaxBytes is clipped on a rune
// boundary and marked, so the title stays valid UTF-8 and short. A name at
// the limit is kept whole.
func TestAnActivityTitleClipsALongFileName(t *testing.T) {
	atLimit := strings.Repeat("a", activityFileNameMaxBytes)
	for _, f := range fileTools {
		if got, want := titleOf(t, f.tool, map[string]any{f.key: "/scratch/" + atLimit}), f.tool+" "+atLimit; got != want {
			t.Errorf("activityTitle(%s, at the limit) = %q, want %q", f.tool, got, want)
		}
		for _, long := range []string{
			strings.Repeat("b", 4*activityFileNameMaxBytes) + ".yaml",
			// Three-byte runes, so a byte cut at the limit would split one.
			strings.Repeat("日本", activityFileNameMaxBytes),
		} {
			got := titleOf(t, f.tool, map[string]any{f.key: "/scratch/" + long})
			name := strings.TrimPrefix(got, f.tool+" ")
			if len(name) > activityFileNameMaxBytes {
				t.Errorf("activityTitle(%s, %d-byte name) keeps %d bytes, over %d", f.tool, len(long), len(name), activityFileNameMaxBytes)
			}
			if !utf8.ValidString(name) {
				t.Errorf("activityTitle(%s, %d-byte name) = %q, not valid UTF-8", f.tool, len(long), name)
			}
			if !strings.HasSuffix(name, activityFileNameClipMark) {
				t.Errorf("activityTitle(%s, %d-byte name) = %q, not marked as clipped", f.tool, len(long), name)
			}
			if !strings.HasPrefix(long, strings.TrimSuffix(name, activityFileNameClipMark)) {
				t.Errorf("activityTitle(%s, %d-byte name) = %q, not a prefix of the name", f.tool, len(long), name)
			}
		}
	}
}

// Ordinary file names keep the titles they had: the same cases as before,
// under both harnesses.
func TestAnActivityTitleKeepsAnOrdinaryFileName(t *testing.T) {
	for _, name := range []string{"deploy.yaml", "README.md", "my notes.txt", "café-ünïcödé.md", "values-prod.v2.yaml"} {
		for _, f := range fileTools {
			if got, want := titleOf(t, f.tool, map[string]any{f.key: "/scratch/" + name}), f.tool+" "+name; got != want {
				t.Errorf("activityTitle(%s, %q) = %q, want %q", f.tool, name, got, want)
			}
		}
	}
}

// A secret in a file name is scrubbed before the name is clipped, so a cut
// can't leave part of one the scrubber no longer recognises, and after the
// non-printing characters go, so one split by a zero-width character is
// still found.
func TestAnActivityTitleScrubsAFileNameBeforeClippingIt(t *testing.T) {
	const token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
	for _, name := range []string{
		// Placed so a clip at the limit would keep the prefix and eight
		// characters, fewer than the scrubber's pattern needs.
		strings.Repeat("x", activityFileNameMaxBytes-len(activityFileNameClipMark)-len("ghp_")-8) + token,
		"ghp_\u200b" + token[len("ghp_"):],
	} {
		for _, f := range fileTools {
			got := titleOf(t, f.tool, map[string]any{f.key: "/scratch/" + name})
			if strings.Contains(got, token[len("ghp_"):len("ghp_")+8]) {
				t.Errorf("activityTitle(%s, %q) = %q, carries part of the token", f.tool, name, got)
			}
			for _, r := range got {
				if !unicode.IsPrint(r) {
					t.Errorf("activityTitle(%s, %q) = %q, carries %U", f.tool, name, got, r)
				}
			}
		}
	}
}

// Any space character in a file name becomes a plain space rather than
// going with the other non-printing characters, so a name written with a
// no-break or ideographic space keeps its gap.
func TestAnActivityTitleKeepsAFileNamesSpacesAsSpaces(t *testing.T) {
	for name, want := range map[string]string{
		"my\u00a0notes.txt":    "my notes.txt",
		"会議\u3000メモ.md":        "会議 メモ.md",
		"thin\u2009space.yaml": "thin space.yaml",
	} {
		for _, f := range fileTools {
			if got := titleOf(t, f.tool, map[string]any{f.key: "/scratch/" + name}); got != f.tool+" "+want {
				t.Errorf("activityTitle(%s, %q) = %q, want %q", f.tool, name, got, f.tool+" "+want)
			}
		}
	}
}

// A value the task has seen, with no shape the scrubber knows, is redacted
// from a file name before the clip, as publishActivity builds the title: a
// clip first would cut the value in two, and its head, no longer the value,
// would pass the title's later scrub.
func TestAPublishedTitleRedactsASeenValueBeforeClippingTheFileName(t *testing.T) {
	// Made up for this test: sixteen bytes with no recognisable shape.
	const seen = "Zq8vT2mWc4xLp9Rb"
	a := &adapter{}
	a.seen.Add(seen)
	// Placed so a clip at the limit would keep the value's first seven bytes.
	name := strings.Repeat("x", activityFileNameMaxBytes-len(activityFileNameClipMark)-7) + seen + ".txt"
	for _, f := range fileTools {
		input, err := json.Marshal(map[string]any{f.key: "/scratch/" + name})
		if err != nil {
			t.Fatal(err)
		}
		got := a.scrub(activityTitleScrubbed(f.tool, input, a.scrub))
		if strings.Contains(got, seen[:7]) {
			t.Errorf("published title for %s = %q, carries the head of a seen value", f.tool, got)
		}
		if len(strings.TrimPrefix(got, f.tool+" ")) > activityFileNameMaxBytes {
			t.Errorf("published title for %s = %q, file name over %d bytes", f.tool, got, activityFileNameMaxBytes)
		}
	}
}
