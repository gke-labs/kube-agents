package gateway

import (
	"context"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestTheRehydrationPrimerCutsOnRuneBoundaries pins the per-task cut in
// buildRehydrationPrimer. The primer is annotated onto the next incarnation's
// pod and marshalled to JSON on the way, and encoding/json substitutes U+FFFD
// for invalid UTF-8 rather than erroring — so a byte cut here does not fail,
// it silently replaces a character in what the fresh pod reads as its own
// transcript. spawn.go's truncateRunes guards the primer's tail only; this cut
// lands mid-transcript and survives it.
func TestTheRehydrationPrimerCutsOnRuneBoundaries(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/primer-runes"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001",
		MessageID: "d-primer-1", Text: "summarise the fleet",
	}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	// A result comfortably past primerTaskResultCap, made only of 3-byte
	// runes so the cut at 2000 bytes cannot land on a boundary: 2000 is not
	// divisible by 3.
	const rune3 = "計"
	if utf8.RuneLen([]rune(rune3)[0]) != 3 {
		t.Fatalf("fixture assumption broken: %q is not 3 bytes", rune3)
	}
	body := strings.Repeat(rune3, primerTaskResultCap)
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: body}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}

	rec := &SessionRecord{
		Key:       conv,
		ContextID: origin.ContextID,
		Addressee: "platform",
		Tasks:     []TaskRef{{ID: origin.TaskID, Addressee: "platform"}},
	}

	var primer string
	waitFor(t, "the task's result on the stream", func() bool {
		primer = r.g.buildRehydrationPrimer(ctx, rec)
		return strings.Contains(primer, rune3)
	})

	if !utf8.ValidString(primer) {
		t.Errorf("primer is not valid UTF-8; the per-task cut went through a rune")
	}
	if strings.ContainsRune(primer, utf8.RuneError) {
		t.Errorf("primer carries U+FFFD, so a character was replaced rather than dropped")
	}
	// The cut still has to bind, or this test would pass against no cut at
	// all. Rune-safe means at or under the cap, never over it.
	body = strings.TrimSuffix(strings.TrimSpace(primer[strings.Index(primer, rune3):]), "…")
	if len(body) > primerTaskResultCap {
		t.Errorf("cut task body is %d bytes, over the %d cap", len(body), primerTaskResultCap)
	}
	if len(body) < primerTaskResultCap-utf8.UTFMax {
		t.Errorf("cut task body is %d bytes, further under the %d cap than a rune walk-back explains",
			len(body), primerTaskResultCap)
	}
}
