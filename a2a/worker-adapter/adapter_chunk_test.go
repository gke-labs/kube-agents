package workeradapter

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// reassemble puts the chunks back together the way the reader side does:
// each chunk becomes a text Part on an ArtifactUpdate, the update is
// JSON-marshalled onto the bus, and lib.Task.mergeArtifact concatenates the
// Appends. The marshal/unmarshal round trip is the part that matters -- it is
// where encoding/json substitutes U+FFFD for an invalid UTF-8 sequence
// instead of erroring, so a byte cut through a rune loses the character
// silently rather than loudly.
func reassemble(t *testing.T, chunks []string) string {
	t.Helper()
	var b strings.Builder
	for _, chunk := range chunks {
		payload, err := json.Marshal(lib.Part{Kind: "text", Text: chunk})
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		var got lib.Part
		if err := json.Unmarshal(payload, &got); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		b.WriteString(got.Text)
	}
	return b.String()
}

// TestChunkStringSurvivesTheBusRoundTrip is the assertion the deliverable
// needs: what the gateway relays into chat is what the harness produced. A
// chunker that cuts on byte boundaries passes a naive `strings.Join` of its
// own output and fails this, because the corruption happens in the marshal,
// not in the split.
func TestChunkStringSurvivesTheBusRoundTrip(t *testing.T) {
	cases := []struct {
		name string
		in   string
		size int
	}{
		// The seam cases: a 2-byte rune straddling every possible offset
		// within a chunk, so one of these lands mid-rune whatever the parity.
		{"two byte runes, odd chunk", strings.Repeat("é", 64), 5},
		{"two byte runes, even chunk", strings.Repeat("é", 64), 6},
		// 3-byte (box drawing, CJK) and 4-byte (emoji) runes against a chunk
		// size coprime to their widths.
		{"three byte runes", strings.Repeat("─", 64), 7},
		{"cjk", strings.Repeat("計", 64), 7},
		{"four byte runes", strings.Repeat("🍊", 64), 7},
		// Mixed content, which is what a real answer looks like.
		{"mixed prose", strings.Repeat("ok — 計 🍊 done\n", 40), 11},
		// The degenerate ends.
		{"empty", "", 8},
		{"shorter than one chunk", "héllo", 64},
		{"exactly one chunk", strings.Repeat("a", 8), 8},
		{"one rune wider than a chunk", strings.Repeat("a", 8) + "é", 8},
		// A single rune wider than the whole budget cannot be split at all.
		{"rune wider than the chunk", "🍊🍊🍊", 2},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			chunks := chunkString(tc.in, tc.size)

			if got := reassemble(t, chunks); got != tc.in {
				t.Errorf("round trip lost content:\n in  = %q\n out = %q", tc.in, got)
			}

			for i, chunk := range chunks {
				if !utf8.ValidString(chunk) {
					t.Errorf("chunk %d is not valid UTF-8: %q", i, chunk)
				}
			}

			// Empty input still yields the one empty chunk publishResult
			// relies on -- completed must carry a result artifact.
			if tc.in == "" && len(chunks) != 1 {
				t.Errorf("empty input produced %d chunks, want 1", len(chunks))
			}
		})
	}
}

// TestChunkStringKeepsChunksUnderTheBusCeiling pins the other half of the
// contract. Walking a cut back to a rune boundary must shrink a chunk, never
// grow one: resultChunkSize is headroom under the bus's max message size, and
// a chunk over it is a publish the bus rejects.
func TestChunkStringKeepsChunksUnderTheBusCeiling(t *testing.T) {
	// utf8.UTFMax-1 is the worst case: a rune whose last byte sits one past
	// the budget forces the longest possible walk back.
	for size := utf8.UTFMax; size <= 16; size++ {
		for _, in := range []string{
			strings.Repeat("🍊", 32),
			strings.Repeat("é", 32),
			strings.Repeat("a🍊", 32),
		} {
			for _, chunk := range chunkString(in, size) {
				if len(chunk) > size {
					t.Errorf("size %d, input %q: chunk of %d bytes exceeds the budget",
						size, in, len(chunk))
				}
			}
		}
	}
}

// TestChunkStringMakesProgressOnAnUnsplittableRune is the termination
// guarantee. A rune wider than the chunk size has no boundary inside the
// budget, so walking back would cut at zero and loop forever on the same
// input. The chunker must emit the whole rune and move on.
func TestChunkStringMakesProgressOnAnUnsplittableRune(t *testing.T) {
	chunks := chunkString(strings.Repeat("🍊", 4), 1)
	if len(chunks) != 4 {
		t.Fatalf("got %d chunks, want 4: %q", len(chunks), chunks)
	}
	for i, chunk := range chunks {
		if chunk != "🍊" {
			t.Errorf("chunk %d = %q, want the whole rune", i, chunk)
		}
	}
}
