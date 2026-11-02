package gateway

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// withFeedbackBucket is a rig tweak that provisions the feedback bucket
// before the gateway runs, with maxAge as its retention (zero: none).
func withFeedbackBucket(t *testing.T, maxAge time.Duration) func(*Config) {
	return func(cfg *Config) {
		nc, err := nats.Connect(cfg.NATSURL)
		if err != nil {
			t.Fatalf("connect: %v", err)
		}
		defer nc.Close()
		js, err := jetstream.New(nc)
		if err != nil {
			t.Fatalf("jetstream: %v", err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.FeedbackBucket, History: 1, TTL: maxAge}); err != nil {
			t.Fatalf("create feedback: %v", err)
		}
	}
}

func (a *fakeAdapter) feedbackMarks() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]string(nil), a.feedbacks...)
}

// completeTurn runs one turn in conv to a completed result and waits for the
// result to post.
func completeTurn(t *testing.T, r *rig, conv, messageID, result string) *lib.Envelope {
	t.Helper()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: messageID, Text: "why is it crashing?"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: result}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p != "" && strings.Contains(result, p) {
				return true
			}
		}
		return false
	})
	return origin
}

func TestAnswerIsMarkedForFeedbackWhenTheBucketKeepsRatings(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 30*24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	origin := completeTurn(t, r, "discord:g1/fb1", "d-1", "the fleet is fine")
	if got := r.adapter.feedbackMarks(); len(got) != 1 || got[0] != origin.TaskID {
		t.Fatalf("feedback marks = %v, want the answer marked with its task %s", got, origin.TaskID)
	}
	if n := strings.Count(strings.Join(r.adapter.postTexts(), "\n"), "the fleet is fine"); n != 1 {
		t.Errorf("the answer posted %d times, want once", n)
	}
	if d := r.g.feedback.Load(); d.KeptDays() != 30 {
		t.Errorf("KeptDays = %d, want the bucket's max age in days (30)", d.KeptDays())
	}
}

func TestLongAnswerIsMarkedOnItsLastChunkOnly(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	long := strings.Repeat("word ", discordChunk/2)
	completeTurn(t, r, "discord:g1/fb2", "d-1", long)
	// The first chunk can land before the marked one; wait for the mark.
	waitFor(t, "the marked chunk", func() bool { return len(r.adapter.feedbackMarks()) > 0 })
	chunks := chatChunks(long, discordChunk)
	if len(chunks) < 2 {
		t.Fatalf("the answer is %d chunk(s); the test needs more than one", len(chunks))
	}
	r.adapter.mu.Lock()
	marked := append([]string(nil), r.adapter.markedTexts...)
	r.adapter.mu.Unlock()
	if len(marked) != 1 || marked[0] != chunks[len(chunks)-1] {
		t.Fatalf("marked posts = %q, want only the last of %d chunks", marked, len(chunks))
	}
}

func TestNoFeedbackBucketMarksNothing(t *testing.T) {
	r := startRig(t)
	completeTurn(t, r, "discord:g1/fb3", "d-1", "the fleet is fine")
	if got := r.adapter.feedbackMarks(); len(got) != 0 {
		t.Fatalf("feedback marks = %v, want none without the bucket", got)
	}
}

func TestFeedbackBucketWithoutMaxAgeLeavesFeedbackOff(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 0))
	completeTurn(t, r, "discord:g1/fb4", "d-1", "the fleet is fine")
	if r.g.feedback.Load() != nil || len(r.adapter.feedbackMarks()) != 0 {
		t.Fatal("a bucket that would keep ratings forever must leave feedback off")
	}
}

func TestAnswerFeedbackTurnedOffMarksNothingWhateverTheBucket(t *testing.T) {
	bucket := withFeedbackBucket(t, 24*time.Hour)
	r := startRigWith(t, func(cfg *Config) {
		bucket(cfg)
		cfg.AnswerFeedbackOff = true
	})
	completeTurn(t, r, "discord:g1/fb10", "d-1", "the fleet is fine")
	if r.g.feedback.Load() != nil || len(r.adapter.feedbackMarks()) != 0 {
		t.Fatal("slack.answerFeedback=false must leave feedback off with the bucket there")
	}
}

func TestAnswerFeedbackOffIsReadFromItsEnv(t *testing.T) {
	setBaseEnv(t)
	for value, off := range map[string]bool{"": false, "true": false, "false": true, " false ": true} {
		t.Setenv(envSlackAnswerFeedback, value)
		cfg, err := FromEnv()
		if err != nil {
			t.Fatal(err)
		}
		if cfg.AnswerFeedbackOff != off {
			t.Errorf("%q: AnswerFeedbackOff = %v, want %v", value, cfg.AnswerFeedbackOff, off)
		}
	}
}

func TestFeedbackTargetIsTheChainsFinalAnswerOnly(t *testing.T) {
	g := &Gateway{}
	g.feedback.Store(&feedbackDesk{g: g, ttl: time.Hour})
	rec := &SessionRecord{Tasks: []TaskRef{
		{ID: "root", Children: []string{"child"}},
		{ID: "child", Role: taskRoleChild, RootTaskID: "root"},
		{ID: "wake", Role: taskRoleWake, RootTaskID: "root"},
		{ID: "handoff", DelegationEnd: "reason: delegation-refused - no"},
	}}
	for task, want := range map[string]string{"root": "", "child": "", "wake": "root", "handoff": "", "unknown": "unknown"} {
		if got := g.feedbackTarget(rec, task); got != want {
			t.Errorf("feedbackTarget(%s) = %q, want %q", task, got, want)
		}
	}
	g.feedback.Store(nil)
	if got := g.feedbackTarget(rec, "wake"); got != "" {
		t.Errorf("feedback off: target = %q, want none", got)
	}
}

func TestDeskKeepsADownRatingWithItsNoteAndAnUpReplacesIt(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	// A real chain, so the ask is the session record's copy of the message
	// that started it, not anything read from the thread.
	origin := completeTurn(t, r, "discord:g1/fb5", "d-1", "it is OOMKilled")
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb5",
		MessageID: "m9", TaskID: origin.TaskID, Rating: FeedbackDown, Answer: "it is OOMKilled"}
	if _, err := desk.Rate(down); err != nil {
		t.Fatal(err)
	}
	if err := desk.Note(down, "  the limit is 256Mi  "); err != nil {
		t.Fatal(err)
	}
	var out, log bytes.Buffer
	n, err := ExportFeedback(context.Background(), r.client, 0, time.Now(), &out, &log)
	if err != nil || n != 1 {
		t.Fatalf("export = %d, %v; want the one kept 👎", n, err)
	}
	files := map[string]string{}
	tr := tar.NewReader(&out)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(tr)
		files[h.Name] = string(body)
	}
	var task, transcript string
	for name, body := range files {
		switch {
		case strings.HasSuffix(name, "/"+feedbackTaskFile):
			task = body
			id := strings.TrimSuffix(name, "/"+feedbackTaskFile)
			if !strings.HasPrefix(id, feedbackCaseIDPrefix) || !strings.Contains(body, "id: "+id+"\n") {
				t.Errorf("task.yaml's id must equal its directory %q:\n%s", id, body)
			}
		case strings.HasSuffix(name, "/"+feedbackTranscript):
			transcript = body
		}
	}
	for _, want := range []string{`prompt: "why is it crashing?"`, "the limit is 256Mi", "deployer: noop", "verification_spec:"} {
		if !strings.Contains(task, want) {
			t.Errorf("task.yaml lacks %q:\n%s", want, task)
		}
	}
	for _, want := range []string{"why is it crashing?", "it is OOMKilled", origin.TaskID, "the limit is 256Mi"} {
		if !strings.Contains(transcript, want) {
			t.Errorf("transcript.md lacks %q:\n%s", want, transcript)
		}
	}
	if !strings.Contains(log.String(), "1 👎 · ") {
		t.Errorf("export log = %q, want a line per draft", log.String())
	}

	up := down
	up.Rating = FeedbackUp
	if _, err := desk.Rate(up); err != nil {
		t.Fatal(err)
	}
	kv, err := r.client.KV(context.Background(), lib.FeedbackBucket)
	if err != nil {
		t.Fatal(err)
	}
	rec, err := getFeedback(context.Background(), kv, feedbackKey(origin.TaskID, "1001"))
	if err != nil || rec.Rating != FeedbackUp || rec.Note != "" || rec.Ask != "why is it crashing?" || rec.Answer != "it is OOMKilled" {
		t.Fatalf("record after 👍 = %+v, %v; want a 👍 with the ask and answer and no note", rec, err)
	}
	// A note sent after a 👍 keeps nothing and says so, so nobody is
	// thanked for a 👎 that is no longer there.
	if err := desk.Note(down, "late"); !errors.Is(err, errFeedbackNotKept) {
		t.Errorf("a note after a 👍 = %v, want errFeedbackNotKept", err)
	}
	if rec, _ := getFeedback(context.Background(), kv, feedbackKey(origin.TaskID, "1001")); rec.Note != "" {
		t.Errorf("a note sent after a 👍 was kept: %q", rec.Note)
	}
	m := r.g.Metrics()
	if got := counterValue(t, m.answerFeedback.WithLabelValues(platformSlack, FeedbackDown)); got != 1 {
		t.Errorf("down ratings counted = %v, want 1", got)
	}
	if got := counterValue(t, m.answerFeedback.WithLabelValues(platformSlack, FeedbackUp)); got != 1 {
		t.Errorf("up ratings counted = %v, want 1", got)
	}
}

func TestDeskReRatingKeepsTheNote(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb8",
		MessageID: "m1", TaskID: "t-4", Rating: FeedbackDown, Answer: "because"}
	if err := desk.Note(down, "too early"); !errors.Is(err, errFeedbackNotKept) {
		t.Fatalf("a note with no 👎 kept = %v, want errFeedbackNotKept", err)
	}
	if _, err := desk.Rate(down); err != nil {
		t.Fatal(err)
	}
	if err := desk.Note(down, "stale limit"); err != nil {
		t.Fatal(err)
	}
	if _, err := desk.Rate(down); err != nil {
		t.Fatal(err)
	}
	kv, err := r.client.KV(context.Background(), lib.FeedbackBucket)
	if err != nil {
		t.Fatal(err)
	}
	rec, err := getFeedback(context.Background(), kv, feedbackKey("t-4", "1001"))
	if err != nil || rec.Note != "stale limit" || rec.Answer != "because" {
		t.Fatalf("record = %+v, %v; want the note kept across a re-rating", rec, err)
	}
}

func TestDeskKeepsAnUpAsAGoodAnswerAndTalliesTheWeek(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	origin := completeTurn(t, r, "discord:g1/fb11", "d-1", "it is OOMKilled")
	up := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb11",
		MessageID: "m1", TaskID: origin.TaskID, Rating: FeedbackUp, Answer: "it is OOMKilled"}
	if _, err := desk.Rate(up); err != nil {
		t.Fatal(err)
	}
	other := up
	other.Rater, other.Rating = "1002", FeedbackDown
	if _, err := desk.Rate(other); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if u, d, err := desk.Tally(ctx, time.Now().Add(-time.Hour)); err != nil || u != 1 || d != 1 {
		t.Errorf("tally = %d 👍, %d 👎, %v; want 1 and 1", u, d, err)
	}
	if u, d, err := desk.Tally(ctx, time.Now().Add(time.Hour)); err != nil || u != 0 || d != 0 {
		t.Errorf("tally since a later time = %d, %d, %v; want none", u, d, err)
	}
	var out, log bytes.Buffer
	if n, err := ExportFeedback(ctx, r.client, 0, time.Now(), &out, &log); err != nil || n != 2 {
		t.Fatalf("export = %d, %v; want both ratings", n, err)
	}
	if !strings.Contains(log.String(), " 👍 · ") || !strings.Contains(log.String(), " 👎 · ") {
		t.Errorf("export log = %q, want a line per rating with its mark", log.String())
	}
	var task, transcript string
	tr := tar.NewReader(&out)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(tr)
		if strings.Contains(string(body), "👍") {
			switch {
			case strings.HasSuffix(h.Name, "/"+feedbackTaskFile):
				task = string(body)
			case strings.HasSuffix(h.Name, "/"+feedbackTranscript):
				transcript = string(body)
			}
		}
	}
	for _, want := range []string{"from a 👍 on an answer", "as a good answer", `prompt: "why is it crashing?"`, "the checks the rated answer passes"} {
		if !strings.Contains(task, want) {
			t.Errorf("the 👍 task.yaml lacks %q:\n%s", want, task)
		}
	}
	if strings.Contains(task, "said was wrong") || !strings.Contains(transcript, "a good answer") || !strings.Contains(transcript, "it is OOMKilled") {
		t.Errorf("the 👍 draft must read as a good answer:\n%s\n%s", task, transcript)
	}
}

// Rate reports whether the kept record holds the ask: a chain's own ask, or
// none for a conversation with no session on record.
func TestDeskReportsWhetherTheAskIsKept(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	origin := completeTurn(t, r, "discord:g1/fb14", "d-1", "it is OOMKilled")
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb14",
		MessageID: "m1", TaskID: origin.TaskID, Rating: FeedbackDown, Answer: "it is OOMKilled"}
	if kept, err := desk.Rate(down); err != nil || !kept {
		t.Errorf("Rate = %v, %v; want the ask kept", kept, err)
	}
	gone := down
	gone.Rater, gone.Conversation = "1002", "discord:g1/reaped"
	if kept, err := desk.Rate(gone); err != nil || kept {
		t.Errorf("Rate with no session = %v, %v; want no ask kept", kept, err)
	}
}

func TestDeskReRatingAfterTheAskIsGoneKeepsTheKeptAsk(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	origin := completeTurn(t, r, "discord:g1/fb13", "d-1", "it is OOMKilled")
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb13",
		MessageID: "m1", TaskID: origin.TaskID, Rating: FeedbackDown, Answer: "it is OOMKilled"}
	if _, err := desk.Rate(down); err != nil {
		t.Fatal(err)
	}
	// The session no longer holds the ask, as after AskTTL or a reap.
	up := down
	up.Rating, up.Conversation = FeedbackUp, "discord:g1/reaped"
	if _, err := desk.Rate(up); err != nil {
		t.Fatal(err)
	}
	kv, err := r.client.KV(context.Background(), lib.FeedbackBucket)
	if err != nil {
		t.Fatal(err)
	}
	rec, err := getFeedback(context.Background(), kv, feedbackKey(origin.TaskID, "1001"))
	if err != nil || rec.Rating != FeedbackUp || rec.Ask != "why is it crashing?" || rec.ContextID == "" {
		t.Fatalf("record = %+v, %v; want the 👍 to keep the ask and context the 👎 kept", rec, err)
	}
}

func TestFeedbackTurnsOnWhenTheBucketAppearsLater(t *testing.T) {
	r := startRig(t)
	if r.g.tryFeedback(context.Background(), true) {
		t.Fatal("no bucket yet: feedback stays off")
	}
	withFeedbackBucket(t, time.Hour)(&Config{NATSURL: r.url})
	if !r.g.tryFeedback(context.Background(), true) || r.g.feedback.Load() == nil {
		t.Fatal("a bucket provisioned after the gateway started must turn feedback on at the next try")
	}
}

func TestDeskRefusesAnUnverifiedRater(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	_, err := r.g.feedback.Load().Rate(FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "9999",
		Conversation: "discord:g1/fb6", TaskID: "t-2", Rating: FeedbackDown, Answer: "x"})
	if !errors.Is(err, errFeedbackUnverified) {
		t.Fatalf("Rate from an unmapped sender = %v, want errFeedbackUnverified", err)
	}
	if n, _ := ExportFeedback(context.Background(), r.client, 0, time.Now(), io.Discard, io.Discard); n != 0 {
		t.Errorf("an unverified rating was kept")
	}
	if got := counterValue(t, r.g.Metrics().answerFeedback.WithLabelValues(platformSlack, FeedbackDown)); got != 0 {
		t.Errorf("an unverified rating was counted: %v", got)
	}
}

func TestExportSinceSkipsOlderRatings(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	if _, err := r.g.feedback.Load().Rate(FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001",
		Conversation: "discord:g1/fb7", TaskID: "t-3", Rating: FeedbackDown}); err != nil {
		t.Fatal(err)
	}
	later := time.Now().Add(2 * time.Hour)
	if n, err := ExportFeedback(context.Background(), r.client, time.Hour, later, io.Discard, io.Discard); err != nil || n != 0 {
		t.Errorf("export --since 1h two hours later = %d, %v; want none", n, err)
	}
	if n, err := ExportFeedback(context.Background(), r.client, 3*time.Hour, later, io.Discard, io.Discard); err != nil || n != 1 {
		t.Errorf("export --since 3h = %d, %v; want the one", n, err)
	}
}

func TestFeedbackDraftEscapesWhatYAMLRefusesAndMarksAMissingAsk(t *testing.T) {
	got := feedbackCaseYAML("feedback-20261016-abcd", FeedbackRecord{Ask: "it\u0092s broken\u007f", Note: "bad\u0085 note\r", RatedAt: time.Now()})
	for _, r := range got {
		if r == '\u007f' || (r >= '\u0080' && r <= '\u009f') || r == '\r' {
			t.Fatalf("draft carries %U, which YAML refuses:\n%s", r, got)
		}
	}
	if !strings.Contains(got, `\u0092`) {
		t.Errorf("the ask's C1 character must be escaped, not dropped:\n%s", got)
	}
	ansi := feedbackCaseYAML("feedback-20261016-abcd", FeedbackRecord{Ask: "x\uffff", Note: "\x1b[31mred\x1b[0m\u2028id: injected", RatedAt: time.Now()})
	for _, line := range strings.Split(ansi, "\n") {
		if strings.HasPrefix(line, "id: injected") {
			t.Errorf("a line separator in the note ended its comment:\n%s", ansi)
		}
	}
	if strings.ContainsAny(ansi, "\x1b\u2028\uffff") {
		t.Errorf("draft carries ESC, U+2028 or U+FFFF raw:\n%s", ansi)
	}
	empty := feedbackCaseYAML("feedback-20261016-abcd", FeedbackRecord{RatedAt: time.Now()})
	if !strings.Contains(empty, "prompt: "+yamlString(feedbackNoAsk)) || !strings.Contains(feedbackTranscriptMD(FeedbackRecord{}), feedbackNoAsk) {
		t.Errorf("a draft with no ask on record must say so:\n%s", empty)
	}
}

func TestFeedbackCaseYAMLQuotesTheAsk(t *testing.T) {
	got := feedbackCaseYAML("feedback-20261016-abcd", FeedbackRecord{Ask: `say "hi": now` + "\n# not a comment", RatedAt: time.Now()})
	if !strings.Contains(got, `prompt: "say \"hi\": now\n# not a comment"`) {
		t.Errorf("the ask must be one quoted scalar:\n%s", got)
	}
	if !strings.Contains(got, "#   "+feedbackNoNote) {
		t.Errorf("a draft without a note says so:\n%s", got)
	}
}
