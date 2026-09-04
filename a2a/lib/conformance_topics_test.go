package lib

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// Topic subjects the deployment provisions, per the payload spec's worked
// examples. The streams' subject lists are the registry, so the tests
// provision them the way the operator does: concrete subjects, one stream per
// retention class.
const (
	subjUpgradeReadiness = "a2a.topics.agent.platform.upgrade-readiness"
	subjBlueprint        = "a2a.topics.shared.blueprint"
	subjAnnotations      = "a2a.topics.shared.annotations"
)

// provisionTopicStreams creates TOPICS-STATE and TOPICS-JOURNAL as the
// operator renders them under mode: next - state keeps 8 per subject with no
// age limit, the journal ages out at 30d.
func provisionTopicStreams(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url)
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
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:              StreamTopicsState,
		Subjects:          []string{subjUpgradeReadiness, subjBlueprint},
		Retention:         jetstream.LimitsPolicy,
		MaxMsgsPerSubject: 8,
	}); err != nil {
		t.Fatalf("create %s: %v", StreamTopicsState, err)
	}
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:      StreamTopicsJournal,
		Subjects:  []string{subjAnnotations},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    30 * 24 * time.Hour,
	}); err != nil {
		t.Fatalf("create %s: %v", StreamTopicsJournal, err)
	}
}

func topicsClient(t *testing.T) *Client {
	t.Helper()
	s := startServer(t)
	provisionTopicStreams(t, clientURL(s))
	c, err := Connect(testCtx(t), clientURL(s), WithName("topics-test"))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(c.Close)
	return c
}

var seedParty = Party{Session: "seed"}

// TestAssertion16 - topic-update payloads are valid Artifacts and topic tokens
// contain no dots.
func TestAssertion16(t *testing.T) {
	c := topicsClient(t)
	ctx := testCtx(t)

	t.Run("a well formed topic update is accepted", func(t *testing.T) {
		a, err := NewTopicArtifact("upgrade-readiness", "two clusters ready",
			map[string]any{"ready": 2})
		if err != nil {
			t.Fatalf("NewTopicArtifact: %v", err)
		}
		if err := c.PublishTopic(ctx, subjUpgradeReadiness, seedParty, "", "", NewCorrelationID(), a); err != nil {
			t.Fatalf("publish topic: %v", err)
		}
	})

	t.Run("a dotted topic token is refused at mint", func(t *testing.T) {
		if _, err := NewTopicArtifact("upgrade.readiness", "", map[string]any{}); err == nil {
			t.Fatal("expected a dotted topic token to be refused")
		}
	})

	t.Run("a dotted topic token is refused at publish", func(t *testing.T) {
		// Hand-built past the mint check: the publish path has to hold the
		// line too, or the only guard is one nobody is required to call.
		payload, err := json.Marshal(Artifact{
			Name:  "upgrade.readiness",
			Parts: []Part{{Kind: "text", Text: "x"}},
		})
		if err != nil {
			t.Fatal(err)
		}
		_, err = NewTopicUpdateEnvelope(seedParty, "", "", NewCorrelationID(), payload)
		if err == nil {
			t.Fatal("expected a dotted artifact name to be refused")
		}
		var perr *ProtocolError
		if !errors.As(err, &perr) {
			t.Fatalf("want *ProtocolError, got %T: %v", err, err)
		}
	})

	t.Run("a payload that is not an Artifact is refused", func(t *testing.T) {
		_, err := NewTopicUpdateEnvelope(seedParty, "", "", NewCorrelationID(),
			json.RawMessage(`{"role":"user","messageId":"m1","parts":[{"kind":"text","text":"hi"}]}`))
		if err == nil {
			t.Fatal("expected a Message payload on kind topic-update to be refused")
		}
	})

	t.Run("an artifact naming another topic is refused", func(t *testing.T) {
		a, err := NewTopicArtifact("blueprint", "", map[string]any{"env": "dev"})
		if err != nil {
			t.Fatal(err)
		}
		err = c.PublishTopic(ctx, subjUpgradeReadiness, seedParty, "", "", NewCorrelationID(), a)
		if err == nil {
			t.Fatal("expected an artifact/subject topic disagreement to be refused")
		}
		var perr *ProtocolError
		if !errors.As(err, &perr) {
			t.Fatalf("want *ProtocolError, got %T: %v", err, err)
		}
	})

	t.Run("only topic-update rides a topic subject", func(t *testing.T) {
		payload, err := json.Marshal(Message{
			Role: "user", MessageID: "m1", Parts: []Part{{Kind: "text", Text: "hi"}},
		})
		if err != nil {
			t.Fatal(err)
		}
		env, err := NewMessageEnvelope(seedParty, NewTaskID(), NewContextID(), NewCorrelationID(), payload)
		if err != nil {
			t.Fatal(err)
		}
		if err := c.Publish(ctx, subjBlueprint, env); err == nil {
			t.Fatal("expected kind message on a topic subject to be refused")
		}
	})

	t.Run("subject parsing rejects dotted tokens", func(t *testing.T) {
		for _, subj := range []string{
			"a2a.topics.agent.platform.upgrade.readiness",
			"a2a.topics.shared.blue.print",
			"a2a.topics.agent.upgrade-readiness",
			"a2a.topics.scratch.platform.notes",
		} {
			if _, _, _, ok := ParseTopicSubject(subj); ok {
				t.Errorf("ParseTopicSubject(%q) accepted a subject it should not", subj)
			}
		}
	})
}

// TestAssertion17 - reading a state-class topic returns the latest entry per
// subject without replaying history.
func TestAssertion17(t *testing.T) {
	c := topicsClient(t)
	ctx := testCtx(t)

	for i, summary := range []string{"first", "second", "third"} {
		a, err := NewTopicArtifact("upgrade-readiness", summary, map[string]any{"generation": i})
		if err != nil {
			t.Fatal(err)
		}
		if err := c.PublishTopic(ctx, subjUpgradeReadiness, seedParty, "", "", NewCorrelationID(), a); err != nil {
			t.Fatalf("publish %s: %v", summary, err)
		}
	}
	// A second topic in the same stream: the read must be per subject, not
	// per stream, or every topic answers with whoever wrote most recently.
	other, err := NewTopicArtifact("blueprint", "the shared model", map[string]any{"env": "dev"})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.PublishTopic(ctx, subjBlueprint, seedParty, "", "", NewCorrelationID(), other); err != nil {
		t.Fatalf("publish blueprint: %v", err)
	}

	env, err := c.ReadTopicLatest(ctx, StreamTopicsState, subjUpgradeReadiness)
	if err != nil {
		t.Fatalf("read latest: %v", err)
	}
	var a Artifact
	if err := json.Unmarshal(env.Payload, &a); err != nil {
		t.Fatalf("unmarshal artifact: %v", err)
	}
	if a.Name != "upgrade-readiness" {
		t.Fatalf("read %s, want upgrade-readiness", a.Name)
	}
	if got := a.Parts[0].Text; got != "third" {
		t.Fatalf("latest entry is %q, want the newest write %q", got, "third")
	}

	// "Without replaying history" is the assertion's teeth: the history is
	// still on the stream (state class keeps 8 per subject), and the read did
	// not consume it. No consumer exists on the stream at all.
	_, js := c.conn()
	st, err := js.Stream(ctx, StreamTopicsState)
	if err != nil {
		t.Fatal(err)
	}
	info, err := st.Info(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if info.State.Msgs != 4 {
		t.Fatalf("stream holds %d messages, want the 4 that were written (a read must not consume)", info.State.Msgs)
	}
	if info.State.Consumers != 0 {
		t.Fatalf("read created %d consumers; the latest-entry read is a direct get, not a replay", info.State.Consumers)
	}

	// And the same read on a topic nobody has written is an empty topic, not
	// a failure: a provisioned topic with no entries is a legal state.
	_, err = c.ReadTopicLatest(ctx, StreamTopicsJournal, subjAnnotations)
	if !errors.Is(err, ErrTopicEmpty) {
		t.Fatalf("reading an unwritten topic returned %v, want ErrTopicEmpty", err)
	}
}

// TestTopicRegistry - the provisioned streams are the topic registry, and a
// bare name resolves through them.
func TestTopicRegistry(t *testing.T) {
	c := topicsClient(t)
	ctx := testCtx(t)

	registry, err := c.TopicRegistry(ctx)
	if err != nil {
		t.Fatalf("registry: %v", err)
	}
	if len(registry) != 3 {
		t.Fatalf("registry holds %d topics, want the 3 provisioned: %+v", len(registry), registry)
	}

	got := map[string]TopicEntry{}
	for _, e := range registry {
		got[e.Topic] = e
	}
	if e := got["upgrade-readiness"]; e.Class != "state" || e.Scope != TopicScopeAgent || e.Agent != "platform" {
		t.Errorf("upgrade-readiness resolved to %+v", e)
	}
	if e := got["annotations"]; e.Class != "journal" || e.Scope != TopicScopeShared {
		t.Errorf("annotations resolved to %+v", e)
	}

	for _, name := range []string{
		"upgrade-readiness",
		"agent.platform.upgrade-readiness",
		subjUpgradeReadiness,
	} {
		e, err := ResolveTopic(registry, name)
		if err != nil {
			t.Errorf("resolve %q: %v", name, err)
			continue
		}
		if e.Subject != subjUpgradeReadiness {
			t.Errorf("resolve %q gave %s", name, e.Subject)
		}
	}

	if _, err := ResolveTopic(registry, "no-such-topic"); err == nil {
		t.Error("expected an unprovisioned name to fail resolution")
	}

	// Ambiguity is an error rather than a guess: two scopes, one bare name.
	ambiguous := append(registry, TopicEntry{
		Subject: "a2a.topics.agent.cluster.blueprint", Stream: StreamTopicsState,
		Class: "state", Scope: TopicScopeAgent, Agent: "cluster", Topic: "blueprint",
	})
	if _, err := ResolveTopic(ambiguous, "blueprint"); err == nil {
		t.Error("expected an ambiguous bare name to fail resolution")
	}
	if _, err := ResolveTopic(ambiguous, "shared.blueprint"); err != nil {
		t.Errorf("a scope-qualified name should still resolve: %v", err)
	}
}

// TestSessionNames - <profile>-<animal>, and the pieces are subject-legal
// because a session name is an addressee token.
func TestSessionNames(t *testing.T) {
	for i := 0; i < 50; i++ {
		name := NewSessionName("worker")
		if !validDNS1123Label(name) {
			t.Fatalf("session name %q is not a legal subject token", name)
		}
	}
	for _, id := range []string{NewTaskID(), NewContextID(), NewCorrelationID()} {
		if !validDNS1123Label(id) {
			t.Fatalf("minted id %q is not a legal subject token", id)
		}
	}
	for _, w := range SessionNameWords {
		if !validDNS1123Label(w) {
			t.Fatalf("session word %q is not a legal subject token", w)
		}
	}
}
