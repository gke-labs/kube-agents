package hermesbridge

import (
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The buffer the cli executor reads a child's stdout through: everything
// under the cap is kept whole, a stream of exactly the cap is kept whole and
// counts nothing dropped, and one byte over keeps the cap and counts the one.
func TestBoundedBuffer_UnderAtAndOverTheCap(t *testing.T) {
	const capacity = 16
	for _, tc := range []struct {
		name        string
		size        int
		wantKept    int
		wantDropped int64
	}{
		{"under", capacity - 1, capacity - 1, 0},
		{"at", capacity, capacity, 0},
		{"one over", capacity + 1, capacity, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := newBoundedBuffer(capacity)
			// Written in two pieces, so the cut can land inside a write.
			in := strings.Repeat("x", tc.size)
			for _, piece := range []string{in[:tc.size/2], in[tc.size/2:]} {
				n, err := b.Write([]byte(piece))
				if err != nil || n != len(piece) {
					t.Fatalf("Write(%d bytes) = (%d, %v), want the whole write accepted", len(piece), n, err)
				}
			}
			if got := b.String(); len(got) != tc.wantKept || got != in[:tc.wantKept] {
				t.Errorf("kept %d bytes, want the first %d", len(got), tc.wantKept)
			}
			if got := b.Dropped(); got != tc.wantDropped {
				t.Errorf("dropped = %d, want %d", got, tc.wantDropped)
			}
		})
	}
}

// A cut that lands inside a multi-byte rune drops the partial rune from what
// is kept, so the stdout tail a failed terminal quotes is valid UTF-8; a
// stream that fits keeps its last rune whole.
func TestBoundedBuffer_CutsOnARuneBoundary(t *testing.T) {
	// "aé" is 3 bytes and each é 2: a cap of 4 splits the second é, and
	// a cap of 5 holds "aéé" exactly.
	b := newBoundedBuffer(4)
	if _, err := b.Write([]byte("aééé")); err != nil {
		t.Fatal(err)
	}
	got := b.String()
	if !utf8.ValidString(got) {
		t.Fatalf("kept %q, which is not valid UTF-8", got)
	}
	if got != "aé" {
		t.Errorf("kept %q, want %q", got, "aé")
	}
	if b.Dropped() != 3 {
		t.Errorf("dropped = %d, want 3", b.Dropped())
	}

	fits := newBoundedBuffer(5)
	if _, err := fits.Write([]byte("aéé")); err != nil {
		t.Fatal(err)
	}
	if got := fits.String(); got != "aéé" {
		t.Errorf("a stream that fits came back as %q", got)
	}
}

// cliStdoutCap is the api executor's cap, so the two executors refuse the
// same answer, and it is the 8 MiB the design names.
func TestTheCLIStdoutCapIsTheAPICap(t *testing.T) {
	if cliStdoutCap != apiResponseCap {
		t.Fatalf("cliStdoutCap = %d, apiResponseCap = %d; the two executors must refuse the same size", cliStdoutCap, apiResponseCap)
	}
	if cliStdoutCap != 8<<20 {
		t.Fatalf("cliStdoutCap = %d, want 8 MiB", cliStdoutCap)
	}
}

// printingScript is a hermes stub whose answer is size bytes of one letter,
// read from /dev/zero so the shell never holds the string itself.
func printingScript(t *testing.T, size int) []string {
	t.Helper()
	return script(t, fmt.Sprintf(`head -c %d /dev/zero | tr '\0' x`, size))
}

// A child whose stdout is one byte over cliStdoutCap ends failed with the
// cap named, and no result artifact: the answer is refused rather than cut
// and published as complete. The bridge keeps the cap's worth and counts the
// rest, so the child's whole stream is never in memory.
func TestCLI_OversizeStdoutIsRefusedWithTheLimitNamed(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, printingScript(t, cliStdoutCap+1))
	c := gatewayClient(t, url)
	submit(t, c, "task-cli-oversize", "write something enormous")
	task := waitTerminalFor(t, c, "task-cli-oversize", atCapTerminalWait)
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed: an answer over the cap must not complete truncated", task.State)
	}
	reason := terminalReason(t, task)
	want := fmt.Sprintf("reason: hermes-cli-oversize - the child's stdout is over the %d-byte limit (cliStdoutCap in bridge.go) by 1 bytes", cliStdoutCap)
	if !strings.HasPrefix(reason, want) {
		t.Fatalf("reason = %q, want prefix %q", reason, want)
	}
	if !strings.Contains(reason, "refused rather than truncated") {
		t.Errorf("reason does not say the answer was refused, not truncated: %q", reason)
	}
	if task.Artifact(lib.ArtifactResult) != nil {
		t.Error("a refused answer was still published as the result artifact")
	}
}

// The boundary's other side: a child whose stdout is exactly cliStdoutCap
// bytes completes with the whole answer, so the cap refuses nothing it
// admits. The terminal budget is the api at-cap test's, for the same reason:
// the chat fold and the chunked publish of an 8 MiB answer.
func TestCLI_StdoutAtTheCapCompletes(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, printingScript(t, cliStdoutCap))
	c := gatewayClient(t, url)
	submit(t, c, "task-cli-atcap", "write something large")
	task := waitTerminalFor(t, c, "task-cli-atcap", atCapTerminalWait)
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s (%s), want completed", task.State, terminalReason(t, task))
	}
	result := task.Artifact(lib.ArtifactResult)
	if result == nil || len(result.Parts) == 0 {
		t.Fatal("no result artifact")
	}
	// The artifact is published in chunks (ResultChunkSize), one part each.
	got := 0
	for _, part := range result.Parts {
		got += len(part.Text)
	}
	if got != cliStdoutCap {
		t.Errorf("result is %d bytes across %d parts, want the child's %d", got, len(result.Parts), cliStdoutCap)
	}
}
