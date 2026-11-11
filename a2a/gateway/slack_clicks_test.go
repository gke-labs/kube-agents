package gateway

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/slack-go/slack"
	"github.com/slack-go/slack/socketmode"
)

// slackClickRig is a gateway on an embedded server whose backend is a real
// SlackAdapter running its own Socket Mode pump: a test pushes an envelope
// into the socketmode client's Events, as the websocket would, and reads what
// the gateway did with it on the bus and in the feedback bucket. The client
// is pointed at a stub that parks apps.connections.open, so the pump runs
// and nothing dials Slack. U1 is listed and mapped.
type slackClickRig struct {
	adapter  *SlackAdapter
	feedback *fakeFeedbackAPI
	r        *rig
}

func startSlackClickRig(t *testing.T) *slackClickRig {
	t.Helper()
	srv, _ := stalledSlackStub(t)
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-slack-clicks"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	adapter, _ := newUXSlackAdapter(&fakeSlackAPI{team: "T1", members: []string{"U1"}})
	adapter.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))
	feedback := &fakeFeedbackAPI{}
	adapter.feedbackAPI = feedback
	cfg := &Config{
		NATSURL:           url,
		PrincipalMapPath:  mapFile,
		DefaultAddressee:  "platform",
		IdleTTL:           30 * time.Minute,
		AttributionSalt:   []byte("test-salt"),
		SlackAllowedUsers: []string{"U1"},
	}
	withFeedbackBucket(t, 24*time.Hour)(cfg)
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: slackBackend, Logger: slog.Default()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	runGateway(t, ctx, g.Run)
	waitFor(t, "answer feedback on", func() bool { return g.feedback.Load() != nil && adapter.feedbackRecorder() != nil })
	return &slackClickRig{adapter: adapter, feedback: feedback, r: &rig{g: g, client: client, bus: bus, url: url}}
}

// deliver hands payload to the pump as Slack's interactive envelope.
func (c *slackClickRig) deliver(envelopeID string, payload json.RawMessage) {
	c.adapter.sm.Events <- socketmode.Event{
		Type:    socketmode.EventTypeInteractive,
		Request: &socketmode.Request{Type: socketmode.RequestTypeInteractive, EnvelopeID: envelopeID, Payload: payload},
	}
}

// documentedChoicePayload is a tap on the question's first choice in the
// shape Slack documents for block_actions (docs.slack.dev, "block_actions
// payload"): action_ts inside the action, none at the top.
func documentedChoicePayload(t *testing.T) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"type":       "block_actions",
		"user":       map[string]any{"id": "U1", "team_id": "T1"},
		"team":       map[string]any{"id": "T1"},
		"channel":    map[string]any{"id": "C1"},
		"container":  map[string]any{"type": "message", "message_ts": "50.0", "channel_id": "C1", "thread_ts": "40.0"},
		"trigger_id": "trig-choice",
		"message":    map[string]any{"ts": "50.0", "thread_ts": "40.0", "text": "Which one?", "blocks": json.RawMessage(uxQuestionBlocks)},
		"actions": []any{map[string]any{"type": "button", "action_id": "kage.choice.0", "block_id": "a",
			"value": "Option A: restart", "text": map[string]any{"type": "plain_text", "text": "Option A"}, "action_ts": "62.0"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// documentedDownPayload is a 👎 on the answer to taskID, in the same shape.
func documentedDownPayload(t *testing.T, taskID string) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"type":       "block_actions",
		"user":       map[string]any{"id": "U1", "team_id": "T1"},
		"team":       map[string]any{"id": "T1"},
		"channel":    map[string]any{"id": "C1"},
		"container":  map[string]any{"type": "message", "message_ts": "70.0", "channel_id": "C1", "thread_ts": "40.0"},
		"trigger_id": "trig-down",
		"message":    map[string]any{"ts": "70.0", "thread_ts": "40.0", "text": "*It is OOMKilled.*"},
		"actions": []any{map[string]any{"type": slackFeedbackButtons, "action_id": slackFeedbackAction,
			"block_id": slackFeedbackBlockPrefix + taskID, "value": FeedbackDown, "action_ts": "71.0"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// A choice tap and a 👎, replayed through the Socket Mode pump in the payload
// shape Slack sends, reach the gateway: the choice becomes a task submitted on
// the bus in the question's thread, carrying the choice's reply, and the 👎 on
// that task's answer is kept in the feedback bucket, counted, and opens the
// note box.
func TestSlackClicksThroughThePumpBecomeATurnAndARating(t *testing.T) {
	c := startSlackClickRig(t)
	ctx := context.Background()

	c.deliver("Env-choice", documentedChoicePayload(t))
	origin := c.r.awaitTask(t, "platform")
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); !strings.Contains(got, "Option A: restart") {
		t.Fatalf("the task's text = %q, want the choice's reply", got)
	}
	waitForRecord(t, c.r, "slack:C1/40.0", "the click's task in the question's thread", func(r *SessionRecord) bool {
		return r.ActiveTask != nil && r.ActiveTask.TaskID == origin.TaskID
	})

	exec := c.r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "It is OOMKilled."}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitForRecord(t, c.r, "slack:C1/40.0", "the turn to end", func(r *SessionRecord) bool { return r.ActiveTask == nil })

	c.deliver("Env-down", documentedDownPayload(t, origin.TaskID))
	kv, err := c.r.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the 👎 to be kept", func() bool {
		kept, err := getFeedback(ctx, kv, feedbackKey(origin.TaskID, "U1"))
		return err == nil && kept.Rating == FeedbackDown
	})
	// The desk keeps the record before it counts it, and the note box opens
	// only once Rate has returned, so the counter is read after the box.
	waitFor(t, "the note box", func() bool {
		c.feedback.mu.Lock()
		defer c.feedback.mu.Unlock()
		return len(c.feedback.triggers) == 1 && c.feedback.triggers[0] == "trig-down"
	})
	if got := counterValue(t, c.r.g.Metrics().answerFeedback.WithLabelValues(platformSlack, FeedbackDown, answerReply)); got != 1 {
		t.Errorf("answer_feedback_total{slack,down,reply} = %v, want 1", got)
	}
}
