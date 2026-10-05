package gateway

import (
	"strings"
	"testing"
	"time"
)

func TestIsStatusQuery(t *testing.T) {
	// Exact phrases are the affordance everywhere - including narrow mode,
	// the posture for executors that absorb steers.
	exact := []string{
		"what is it doing", "What's it doing?", "status", "Status?",
		"how's it going", "how is it going", "whats going on",
		"any progress", "any updates?", "progress", "where are we",
	}
	// Interrogative shapes match only under the wide posture, where the
	// executor refuses steers and a false positive costs nothing.
	wideOnly := []string{
		"what is the agent doing", // the live miss that created this test
		"what is kage doing",
		"any update on the rollout",
	}
	// Steers the wide rule mistakes for status asks - the documented cost
	// of the width bias, and why steer-absorbing executors get narrow.
	wideCost := []string{
		"any update to the config should be reverted",
		"how about doing the upgrade instead",
	}
	never := []string{
		"also check the memory limits",
		"actually, focus on the kube-system namespace instead",
		"stop",
		"what is the memory limit on the nats pod and can you also check its restarts", // long compound: steer
		"delete the deployment",
	}
	for _, s := range exact {
		if !isStatusQuery(s, false) || !isStatusQuery(s, true) {
			t.Errorf("expected status query in both modes: %q", s)
		}
	}
	for _, s := range append(wideOnly, wideCost...) {
		if !isStatusQuery(s, true) {
			t.Errorf("expected wide status query: %q", s)
		}
		if isStatusQuery(s, false) {
			t.Errorf("expected narrow mode to forward as a steer: %q", s)
		}
	}
	for _, s := range never {
		if isStatusQuery(s, true) || isStatusQuery(s, false) {
			t.Errorf("expected NOT a status query in any mode: %q", s)
		}
	}
}

func TestIsDelegate(t *testing.T) {
	yes := map[string]string{
		"Delegate: write a haiku about message buses": "write a haiku about message buses",
		"delegate write a haiku":                      "write a haiku",
		"DELEGATE - check the fleet, then report":     "check the fleet, then report",
		"  delegate,  summarize #930  ":               "summarize #930",
		"Delegate:\nmultiline task":                   "multiline task",
	}
	for in, want := range yes {
		got, ok := isDelegate(in)
		if !ok || got != want {
			t.Errorf("isDelegate(%q) = (%q, %v), want (%q, true)", in, got, ok, want)
		}
	}
	no := []string{
		"delegate",
		"delegate:",
		"Delegated tasks are neat",
		"can you delegate this",
		"delegation is the demo",
		"what is it doing",
		"",
	}
	for _, in := range no {
		if got, ok := isDelegate(in); ok {
			t.Errorf("isDelegate(%q) = (%q, true), want false", in, got)
		}
	}
}

// TestChatChunksKeepFencesBalanced: the adapters translate each chunk alone,
// so a cut inside a fenced block must close the block at the end of the
// chunk and reopen it with a bare fence at the start of the next. Every
// chunk is then balanced on its fences, so a fenced block never leaks its
// closer into the next chunk; no chunk exceeds the cap even with the fences
// added; and the text between the inserted fences is the original, byte for
// byte. When a cut falls inside a code span opening delimiter, or inside a
// span that fits in a chunk whose start lies in the second half of the
// budget, the cut moves back to the span start, keeping the span intact in the
// next chunk; longer spans are split with balanced fences as before. The opener's
// info string is not carried: a continuation of a yaml block reopens with ``` alone.
func TestChatChunksKeepFencesBalanced(t *testing.T) {
	logs := strings.Repeat("log line\n", 300)
	cases := map[string]struct {
		text string
	}{
		"two blocks with prose between": {
			text: "intro\n```\n" + logs + "```\nsee <https://evil.example|https://good.example>\n```\nkubectl get pods\n```\n",
		},
		"one block then prose": {
			text: "```\n" + logs + "```\n**Summary:** see [runbook](https://x.example/r)",
		},
		"a language tag": {
			text: "```yaml\n" + logs + "```\ndone",
		},
	}
	for name, tc := range cases {
		chunks := chatChunks(tc.text, discordChunk)
		if len(chunks) < 2 {
			t.Fatalf("%s: chatChunks gave %d chunks; the block must be cut for the test to mean anything", name, len(chunks))
		}
		var joined strings.Builder
		closed := false
		for i, chunk := range chunks {
			if len(chunk) > discordChunk {
				t.Errorf("%s: chunk %d is %d bytes, over the cap of %d", name, i, len(chunk), discordChunk)
			}
			lines := strings.Split(chunk, "\n")
			fences := 0
			for _, l := range lines {
				fences += strings.Count(l, "```")
			}
			if fences%2 != 0 {
				t.Errorf("%s: chunk %d has %d fence lines; a chunk must be balanced to parse alone:\n%q", name, i, fences, chunk)
			}
			body := chunk
			if closed {
				reopen := "```"
				if !strings.HasPrefix(body, reopen) {
					t.Errorf("%s: chunk %d follows a cut block and does not reopen it with %q: %q", name, i, reopen, body[:min(len(body), 40)])
				}
				body = strings.TrimPrefix(body, reopen)
				if !strings.HasPrefix(body, "\n") {
					t.Errorf("%s: chunk %d: the reopened fence does not end its line: %q", name, i, chunk[:min(len(chunk), 40)])
				}
				body = strings.TrimPrefix(body, "\n")
			}
			closed = false
			if i < len(chunks)-1 && strings.HasSuffix(body, "\n```") {
				// The cut fell inside the block: the inserted closer, after
				// a line that was the block's own.
				body = strings.TrimSuffix(body, "\n```")
				closed = true
			}
			joined.WriteString(body)
			if closed {
				joined.WriteString("\n") // the line break the cut landed on
			}
		}
		if joined.String() != tc.text {
			t.Errorf("%s: the chunks, with the inserted fences removed, are not the original text:\n got %q\nwant %q", name, joined.String(), tc.text)
		}
	}
}

// TestChatChunksAvoidSplittingCodeSpans: when a cut falls inside any code
// span (multi-line double-backtick spans or mid-line fence openers) starting in
// the second half of the budget, the chunker moves the cut back to the span's
// start so that each chunk parses independently without orphaned delimiters
// breaking downstream markdown conversion (#2288).
func TestChatChunksAvoidSplittingCodeSpans(t *testing.T) {
	// Case 1: A multi-line double-backtick span starting in the second half
	// of the budget. Without adjusting the cut, an ordinary line break inside
	// the double-backtick span is chosen, splitting the span and leaving an
	// orphaned closer in chunk 2 that swallows subsequent prose as code.
	in1 := strings.Repeat("filler line\n", 150) + "``" + strings.Repeat("filler line\n", 20) + "end``\nsee **bold** and <https://a.example|https://b.example>\n``x``\n"
	chunks1 := chatChunks(in1, discordChunk)
	if len(chunks1) != 2 {
		t.Fatalf("case 1: got %d chunks, want 2", len(chunks1))
	}
	for i, c := range chunks1 {
		if len(c) > discordChunk {
			t.Errorf("case 1: chunk %d exceeds %d bytes: %d", i, discordChunk, len(c))
		}
	}
	wantCut1 := len(strings.Repeat("filler line\n", 150))
	if len(chunks1[0]) != wantCut1 {
		t.Errorf("case 1: chunk 1 len = %d, want %d", len(chunks1[0]), wantCut1)
	}
	gotGchat1 := toGchatText(chunks1[1])
	if !strings.Contains(gotGchat1, "*bold*") || strings.Contains(gotGchat1, "**bold**") {
		t.Errorf("case 1: toGchatText(chunk 2) left bold unconverted: %q", gotGchat1)
	}
	if strings.Contains(gotGchat1, "<https://a.example|") {
		t.Errorf("case 1: toGchatText(chunk 2) left link undefanged: %q", gotGchat1)
	}

	// Case 2: A hard cut inside a mid-line fence opener at byte 1898.
	// Without adjusting the cut, chunk 1 ends with two backticks and chunk 2
	// starts with the third, corrupting fence pairing in chunk 2.
	in2 := strings.Repeat("a", 1898) + "```\ncodeA\n```\nsee **bold** and <https://a.example|https://b.example>\n```\ncodeB\n```\n"
	chunks2 := chatChunks(in2, discordChunk)
	if len(chunks2) != 2 {
		t.Fatalf("case 2: got %d chunks, want 2", len(chunks2))
	}
	if len(chunks2[0]) != 1898 {
		t.Errorf("case 2: chunk 1 len = %d, want 1898", len(chunks2[0]))
	}
	gotGchat2 := toGchatText(chunks2[1])
	if !strings.Contains(gotGchat2, "*bold*") || strings.Contains(gotGchat2, "**bold**") {
		t.Errorf("case 2: toGchatText(chunk 2) left bold unconverted: %q", gotGchat2)
	}
	if strings.Contains(gotGchat2, "<https://a.example|") {
		t.Errorf("case 2: toGchatText(chunk 2) left link undefanged: %q", gotGchat2)
	}

	// Fallback case: a span starting in the first half of the budget (< budget/2)
	// falls through to existing behavior and makes progress without looping.
	in3 := "``" + strings.Repeat("filler line\n", 170) + "end``\n"
	chunks3 := chatChunks(in3, discordChunk)
	if len(chunks3) < 2 {
		t.Fatalf("case 3: got %d chunks, want >= 2", len(chunks3))
	}
	for i, c := range chunks3 {
		if len(c) > discordChunk {
			t.Errorf("case 3: chunk %d exceeds %d bytes: %d", i, discordChunk, len(c))
		}
	}

	// Case 4: Recut wiring when a fence is open. A fence opener starting at
	// byte 949 has 949 < budget/2 (1900/2 = 950), so the initial cut at 1900
	// does not move back. The open fence triggers a recut with budget 1896
	// (budget - len(fenceClose)). Since 949 >= 1896/2 (948), the recut
	// adjustment moves the cut back to 949, preventing the chunk from ending
	// with a closing fence. Sabotaging the recut adjustment leaves chunk 1 at
	// length 1900 (1896 + len(fenceClose)) instead of 949.
	in4 := strings.Repeat("a", 949) + "```" + strings.Repeat("b", 1000)
	chunks4 := chatChunks(in4, discordChunk)
	if len(chunks4) < 2 {
		t.Fatalf("case 4: got %d chunks, want >= 2", len(chunks4))
	}
	if len(chunks4[0]) != 949 {
		t.Errorf("case 4: chunk 1 len = %d, want 949", len(chunks4[0]))
	}
}

// TestChatChunksLongSpanNotMovedWhenExceedingBudget: a fenced block or code span
// longer than the chunk budget is split across chunks rather than moving the cut
// back to the start and wasting chunk capacity (#2288).
func TestChatChunksLongSpanNotMovedWhenExceedingBudget(t *testing.T) {
	// A 1000-byte prose intro followed by a 2500-byte fenced block (3508 bytes total).
	// Under budget 1900, moving the cut back to 1000 would produce 3 chunks because
	// the 2500-byte block would still be split on iteration 2. By not moving the cut
	// for spans that exceed the budget, chunk 1 packs up to the line break inside the block
	// (1893 bytes + "\n```" = 1897 bytes) and chunk 2 carries the remainder (1615 bytes),
	// requiring only 2 chunks total.
	intro := strings.Repeat("intro line\n", 90) + strings.Repeat("x", 10) // 1000 bytes
	block := "```\n" + strings.Repeat("xxxxxxxxx\n", 250) + "```\n"        // 2505 bytes
	text := intro + block
	chunks := chatChunks(text, discordChunk)
	if len(chunks) != 2 {
		t.Fatalf("got %d chunks, want 2", len(chunks))
	}
	if len(chunks[0]) > discordChunk {
		t.Errorf("chunk 1 len = %d, exceeds discord cap %d", len(chunks[0]), discordChunk)
	}
	if !strings.HasSuffix(chunks[0], "\n```") {
		t.Errorf("chunk 1 should be closed with balanced fence, got %q", chunks[0][len(chunks[0])-10:])
	}
	if !strings.HasPrefix(chunks[1], "```\n") {
		t.Errorf("chunk 2 should reopen with fence, got %q", chunks[1][:10])
	}
}

// TestChatChunksContinuationAfterFencedBlock verifies that when a fenced code
// block is cut across chunks, the continuation chunk scans candidate code spans
// with the reopen prefix in place so that the block's closing fence is not
// inverted into a phantom opener that drags subsequent prose cuts back or emits
// empty code blocks in later chunks (#2288).
func TestChatChunksContinuationAfterFencedBlock(t *testing.T) {
	// A 400-line fenced block whose remaining lines in chunk 2 close at offset
	// 1711 (in the second half of budget 1897), followed by 150 lines of prose.
	in := "```\n" + strings.Repeat("log line\n", 400) + "```\n" + strings.Repeat("prose line with **bold** and <https://a.example|link>\n", 150)
	chunks := chatChunks(in, discordChunk)
	if len(chunks) < 3 {
		t.Fatalf("got %d chunks, want >= 3", len(chunks))
	}
	for i, c := range chunks {
		if len(c) > discordChunk {
			t.Errorf("chunk %d exceeds %d bytes: %d", i, discordChunk, len(c))
		}
		if strings.HasPrefix(c, "```\n```\n") {
			t.Errorf("chunk %d opens with empty fence block: %q", i, c[:min(len(c), 30)])
		}
	}
	// Verify that prose after the block converts markdown properly
	gchat := toGchatText(chunks[len(chunks)-1])
	if !strings.Contains(gchat, "*bold*") || strings.Contains(gchat, "**bold**") {
		t.Errorf("toGchatText left bold unconverted in prose: %q", gchat)
	}
	if strings.Contains(gchat, "<https://a.example|") {
		t.Errorf("toGchatText left link undefanged in prose: %q", gchat)
	}
	// Verify round trip unchunking
	if got := unchunk(t, "continuation after fence", chunks, in); got != in {
		t.Errorf("unchunked text does not match original")
	}
}

// TestChatChunksUnchangedOutsideFences: text with no fence open at the cut
// is split as it always was, so a result that is prose and closed blocks
// posts the same chunks as before the chunker learned about fences.
func TestChatChunksUnchangedOutsideFences(t *testing.T) {
	text := strings.Repeat("a line of prose that goes on\n", 200) + "```\nshort block\n```\n" + strings.Repeat("more prose\n", 100)
	chunks := chatChunks(text, discordChunk)
	if got := strings.Join(chunks, ""); got != text {
		t.Fatalf("chunks do not join back to the text")
	}
	for i, c := range chunks {
		if strings.Contains(c, "\n```\n```") || len(c) > discordChunk {
			t.Errorf("chunk %d carries an inserted fence or is over the cap: %q", i, c)
		}
	}
	// A hard cut (no line break to land on) inside a fence still closes and
	// reopens, with the closer on its own line, and makes progress.
	long := "```\n" + strings.Repeat("x", 5000) + "\n```\n"
	chunks = chatChunks(long, discordChunk)
	if len(chunks) < 3 {
		t.Fatalf("hard cuts: got %d chunks", len(chunks))
	}
	for i, c := range chunks {
		if len(c) > discordChunk {
			t.Errorf("hard cuts: chunk %d is %d bytes", i, len(c))
		}
		if strings.Count(c, "```")%2 != 0 {
			t.Errorf("hard cuts: chunk %d is unbalanced: %q", i, c)
		}
		if i > 0 && !strings.HasPrefix(c, "```\n") {
			t.Errorf("hard cuts: chunk %d does not reopen the fence: %q", i, c[:min(len(c), 20)])
		}
	}
}

// endsInsideFence reads chunk as the adapters do (mdCodeSpanRE) and reports
// whether its last span is a fence that ran to the end without a closer: the
// state the chunker exists to keep every chunk out of.
func endsInsideFence(chunk string) bool {
	spans := mdCodeSpanRE.FindAllStringIndex(chunk, -1)
	if len(spans) == 0 {
		return false
	}
	last := spans[len(spans)-1]
	return last[1] == len(chunk) && mdFenceUnclosed(chunk[last[0]:last[1]])
}

// unchunk checks the chunker's contract over chunks, the split of orig at
// discordChunk, and returns the text they carry once the inserted fences are
// removed, for the caller to compare with orig byte for byte: no chunk is
// over the cap or ends inside a fence when parsed alone; a chunk cut inside
// a block ends with fenceClose and the next opens with a bare mdFence,
// followed by a line break of its own only where the cut left none (which
// orig tells).
func unchunk(t *testing.T, name string, chunks []string, orig string) string {
	t.Helper()
	var joined strings.Builder
	reopened := false
	for i, chunk := range chunks {
		if len(chunk) > discordChunk {
			t.Errorf("%s: chunk %d is %d bytes, over the cap of %d", name, i, len(chunk), discordChunk)
		}
		if endsInsideFence(chunk) {
			t.Errorf("%s: chunk %d ends inside a fence when parsed alone: %q", name, i, chunk)
		}
		body := chunk
		if reopened {
			if !strings.HasPrefix(body, mdFence) {
				t.Fatalf("%s: chunk %d follows a cut block and does not reopen it: %q", name, i, body[:min(len(body), 40)])
			}
			body = strings.TrimPrefix(body, mdFence)
			if !strings.HasPrefix(orig[joined.Len():], "\n") {
				if !strings.HasPrefix(body, "\n") {
					t.Fatalf("%s: chunk %d reopened the fence mid-line: %q", name, i, chunk[:min(len(chunk), 40)])
				}
				body = strings.TrimPrefix(body, "\n")
			}
		}
		// The closer is the chunker's when the chunk without it, read as
		// the adapter reads it, ends inside a fence; a block's own closer
		// at the end of a chunk leaves it balanced.
		reopened = i < len(chunks)-1 && strings.HasSuffix(chunk, fenceClose) &&
			strings.HasPrefix(chunks[i+1], mdFence) && endsInsideFence(strings.TrimSuffix(chunk, fenceClose))
		if reopened {
			body = strings.TrimSuffix(body, fenceClose)
		}
		joined.WriteString(body)
	}
	return joined.String()
}

// TestChatChunksSurviveALongOpenerLine: the reopened fence used to carry the
// opener's info string, which is the rest of the opener's line and so as
// long as the executor made it. With an opener line near the chunk cap the
// budget left for the next chunk fell to a few bytes or below zero, and
// chunkCut either made no progress (the relay spun) or sliced past the start
// of the text (the relay panicked, and Gateway.post has no recover). Each
// input returns, within the cap, and with the inserted fences removed is the
// original text.
func TestChatChunksSurviveALongOpenerLine(t *testing.T) {
	cases := map[string]string{
		// The three lengths that spun or panicked at the Discord cap.
		"opener of 1892": "```" + strings.Repeat("x", 1892) + "\nbody\n```\n",
		"opener of 1894": "```" + strings.Repeat("x", 1894) + "\nbody\n```\n",
		"opener of 1897": "```" + strings.Repeat("x", 1897) + "\nbody\n```\n",
		// One line, no break at all: every cut is a hard cut inside the
		// opener's own line.
		"single-line blob": "```" + strings.Repeat(`{"a":1},`, 500) + "```",
	}
	for name, text := range cases {
		var chunks []string
		done := make(chan struct{})
		go func() {
			defer close(done)
			chunks = chatChunks(text, discordChunk)
		}()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatalf("%s: chatChunks did not return", name)
		}
		if len(chunks) < 2 {
			t.Fatalf("%s: chatChunks gave %d chunks; the text must be cut for the test to mean anything", name, len(chunks))
		}
		if got := unchunk(t, name, chunks, text); got != text {
			t.Errorf("%s: the chunks, with the inserted fences removed, are not the original text:\n got %q\nwant %q", name, got, text)
		}
	}
}

// TestChatChunksReadFencesAsTheAdaptersDo: the chunker decides whether a cut
// fell inside a fence with the parse the adapters make of the chunk,
// mdCodeSpanRE. A line such as `use ``` to open a fence` holds three
// backticks and no fence by that parse (the first closes the inline span,
// the other two are a span of their own); a count of ``` per line read it as
// an opener, and a cut after it closed a fence that was never open and
// reopened it at the head of the next chunk, so the adapter rendered the
// prose after the cut as code. The cut is pinned to land on that line's
// break; no fence is inserted, every chunk parses alone outside a fence, and
// the bold after the cut converts as prose.
func TestChatChunksReadFencesAsTheAdaptersDo(t *testing.T) {
	line := "`use ``` to open a fence`\n"
	head := strings.Repeat("a line of prose that goes on\n", 64) + line
	tail := "after the cut this is prose with **bold** and a [link](https://x.example/r) in it\n" + strings.Repeat("more prose\n", 10)
	text := head + tail
	chunks := chatChunks(text, discordChunk)
	// A cut on a line break leaves the break at the head of the next chunk.
	if len(chunks) != 2 || chunks[0] != strings.TrimSuffix(head, "\n") {
		t.Fatalf("the cut did not land after the backtick line; got %d chunks, the first ending %q", len(chunks), chunks[0][max(0, len(chunks[0])-40):])
	}
	if strings.Join(chunks, "") != text {
		t.Fatalf("a fence was inserted around the cut:\n%q\n%q", chunks[0][len(chunks[0])-40:], chunks[1][:40])
	}
	for i, c := range chunks {
		if endsInsideFence(c) {
			t.Errorf("chunk %d ends inside a fence when parsed alone: %q", i, c)
		}
	}
	bold := strings.Index(chunks[1], "**bold**")
	if insideAny(bold, mdCodeSpanRE.FindAllStringIndex(chunks[1], -1)) {
		t.Errorf("the prose after the cut parses as code: %q", chunks[1][:80])
	}
	if got := toMrkdwn(chunks[1]); !strings.Contains(got, "with *bold* and a <https://x.example/r|link>") {
		t.Errorf("the prose after the cut was not converted as prose: %q", got[:min(len(got), 120)])
	}
}

// TestChatChunksDoNotRepeatTheOpenersLine: the info string carried onto the
// reopened fence was the rest of the opener's line, so an executor that
// wrote content on the opener's line (a one-line JSON blob after ```) saw it
// posted once in the first chunk and again at the head of every chunk after.
// The content appears once across the chunks, and each continuation reopens
// with a bare fence.
func TestChatChunksDoNotRepeatTheOpenersLine(t *testing.T) {
	text := "```{\"a\":1}\n" + strings.Repeat("log line\n", 300) + "```\n"
	chunks := chatChunks(text, discordChunk)
	if len(chunks) < 2 {
		t.Fatalf("chatChunks gave %d chunks; the block must be cut for the test to mean anything", len(chunks))
	}
	if n := strings.Count(strings.Join(chunks, ""), `{"a":1}`); n != 1 {
		t.Errorf("the opener's content appears %d times across the chunks, want once", n)
	}
	for i, c := range chunks[1:] {
		if !strings.HasPrefix(c, "```\nlog line\n") {
			t.Errorf("chunk %d does not reopen with a bare fence: %q", i+1, c[:min(len(c), 40)])
		}
	}
	if got := unchunk(t, "opener content", chunks, text); got != text {
		t.Errorf("the chunks, with the inserted fences removed, are not the original text:\n got %q\nwant %q", got, text)
	}
}

func TestIsSessionCommand(t *testing.T) {
	yes := map[string]string{
		"/session":                             "",
		"/session off":                         "off",
		"/SESSION Off":                         "Off",
		"  /session what is running in ns x  ": "what is running in ns x",
		"/session\nmultiline first turn":       "multiline first turn",
		"/session\toff":                        "off",
	}
	for in, want := range yes {
		got, ok := isSessionCommand(in)
		if !ok || got != want {
			t.Errorf("isSessionCommand(%q) = (%q, %v), want (%q, true)", in, got, ok, want)
		}
	}
	no := []string{
		"/sessions", "/sessionoff", "/ session", "session", "session off",
		"delegate: /session", "/help", "/", "", "please /session",
	}
	for _, in := range no {
		if got, ok := isSessionCommand(in); ok {
			t.Errorf("isSessionCommand(%q) = (%q, true), want false", in, got)
		}
	}
	for _, rest := range []string{"off", "OFF", " off ", "off.", "off!", "Off,"} {
		if !isSessionOff(rest) {
			t.Errorf("isSessionOff(%q) = false, want true", rest)
		}
	}
	for _, rest := range []string{"", "offline", "turn off", "on"} {
		if isSessionOff(rest) {
			t.Errorf("isSessionOff(%q) = true, want false", rest)
		}
	}
}
