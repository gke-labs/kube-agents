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
	if got := r.adapter.feedbackMarks(); len(got) != 1 {
		t.Fatalf("feedback marks = %v, want one, on the last chunk", got)
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

func TestDeskKeepsADownRatingWithItsNoteAndAnUpTakesItBack(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb5",
		MessageID: "m9", TaskID: "t-1", Rating: FeedbackDown, Ask: "why is it crashing?", Answer: "it is OOMKilled",
		Transcript: []FeedbackTurn{{From: FeedbackFromUser, Text: "why is it crashing?"}, {From: FeedbackFromAgent, Text: "it is OOMKilled"}}}
	if err := desk.Rate(down); err != nil {
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
	for _, want := range []string{"it is OOMKilled", "t-1", "### agent"} {
		if !strings.Contains(transcript, want) {
			t.Errorf("transcript.md lacks %q:\n%s", want, transcript)
		}
	}
	if !strings.Contains(log.String(), "1 👎 · ") {
		t.Errorf("export log = %q, want a line per draft", log.String())
	}

	up := down
	up.Rating = FeedbackUp
	if err := desk.Rate(up); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	if n, err := ExportFeedback(context.Background(), r.client, 0, time.Now(), &out, io.Discard); err != nil || n != 0 {
		t.Fatalf("export after 👍 = %d, %v; want nothing kept", n, err)
	}
	// Send under "What went wrong?" after a 👍 is a 👎 again.
	if err := desk.Note(down, "late"); err != nil {
		t.Fatal(err)
	}
	if n, _ := ExportFeedback(context.Background(), r.client, 0, time.Now(), io.Discard, io.Discard); n != 1 {
		t.Errorf("a note sent after a 👍 kept %d, want its 👎 back", n)
	}
	m := r.g.Metrics()
	if got := counterValue(t, m.answerFeedback.WithLabelValues(platformSlack, FeedbackDown)); got != 1 {
		t.Errorf("down ratings counted = %v, want 1", got)
	}
	if got := counterValue(t, m.answerFeedback.WithLabelValues(platformSlack, FeedbackUp)); got != 1 {
		t.Errorf("up ratings counted = %v, want 1", got)
	}
}

func TestDeskNoteBeforeTheRatingKeepsBoth(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "discord:g1/fb8",
		MessageID: "m1", TaskID: "t-4", Rating: FeedbackDown}
	// The modal's Send can land before the thread is read and the rating
	// written; neither half may lose the other.
	if err := desk.Note(down, "stale limit"); err != nil {
		t.Fatal(err)
	}
	down.Ask, down.Answer = "why?", "because"
	if err := desk.Rate(down); err != nil {
		t.Fatal(err)
	}
	kv, err := r.client.KV(context.Background(), lib.FeedbackBucket)
	if err != nil {
		t.Fatal(err)
	}
	rec, err := getFeedback(context.Background(), kv, feedbackKey("t-4", "1001"))
	if err != nil || rec.Note != "stale limit" || rec.Ask != "why?" || rec.Answer != "because" {
		t.Fatalf("record = %+v, %v; want the note and the exchange together", rec, err)
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
	err := r.g.feedback.Load().Rate(FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "9999",
		Conversation: "discord:g1/fb6", TaskID: "t-2", Rating: FeedbackDown, Ask: "x"})
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
	if err := r.g.feedback.Load().Rate(FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001",
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

func TestFeedbackCaseYAMLQuotesTheAsk(t *testing.T) {
	got := feedbackCaseYAML("feedback-20261016-abcd", FeedbackRecord{Ask: `say "hi": now` + "\n# not a comment", RatedAt: time.Now()})
	if !strings.Contains(got, `prompt: "say \"hi\": now\n# not a comment"`) {
		t.Errorf("the ask must be one quoted scalar:\n%s", got)
	}
	if !strings.Contains(got, "#   "+feedbackNoNote) {
		t.Errorf("a draft without a note says so:\n%s", got)
	}
}
