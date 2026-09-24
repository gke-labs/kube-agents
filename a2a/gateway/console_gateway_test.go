package gateway

import (
	"context"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A console turn on a gateway configured for discord: the backend on the
// authority block is console, the principal is the fixed console principal,
// and no principal-map entry was needed.
func TestConsoleTurnCarriesTheConsoleBackendAndPrincipal(t *testing.T) {
	r := startRig(t) // gateway_test.go:171; Backend: "discord", the map holds 1001 only
	// startRig's fakeAdapter stands in for whichever backend is configured
	// and its default roster ("1001") is that backend's, not the console's;
	// a console conversation is a 1:1 DM, so the room is just the sender
	// (mux_test.go:50 sets the same field the same way for the same reason).
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-9", Kind: "dm",
		AuthorID: consoleAuthor, MessageID: "m1", Text: "console hello",
	}
	var auth Authority
	waitFor(t, "the console submission", func() bool {
		env, a := latestSubmissionOrNil(r.bus)
		if env == nil {
			return false
		}
		var m lib.Message
		if json.Unmarshal(env.Payload, &m) != nil || joinTextParts(m.Parts) != "console hello" {
			return false
		}
		auth = a
		return true
	})
	if auth.Requester.Backend != consoleBackend {
		t.Errorf("backend = %q, want %q", auth.Requester.Backend, consoleBackend)
	}
	if auth.Requester.VerifiedBy != consoleVerifiedBy {
		t.Errorf("verifiedBy = %q, want %q", auth.Requester.VerifiedBy, consoleVerifiedBy)
	}
	if want := r.g.ps.Hash(consolePrincipal); auth.Requester.Principal != want {
		t.Errorf("principal = %q, want the hashed console principal", auth.Requester.Principal)
	}
	if auth.Audience.Kind != "dm" || len(auth.Audience.Roster) != 1 {
		t.Errorf("audience = %+v", auth.Audience)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "can't verify") {
			t.Errorf("console sender was dropped as unverified: %q", p)
		}
	}
}

// Review focus 5: a discord turn on the same gateway still goes through the
// principal map and still says discord.
func TestDiscordTurnStillResolvesThroughTheMapAfterThePerMessageBackend(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-pm", Kind: "group",
		AuthorID: "1001", MessageID: "m2", Text: "discord hello",
	}
	var auth Authority
	waitFor(t, "the discord submission", func() bool {
		env, a := latestSubmissionOrNil(r.bus)
		if env == nil {
			return false
		}
		var m lib.Message
		if json.Unmarshal(env.Payload, &m) != nil || joinTextParts(m.Parts) != "discord hello" {
			return false
		}
		auth = a
		return true
	})
	if auth.Requester.Backend != "discord" || auth.Requester.VerifiedBy != "principal-map" {
		t.Errorf("requester = %+v", auth.Requester)
	}
	// And an unmapped discord sender is still dropped with the map remedy.
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-pm2", Kind: "group",
		AuthorID: "9999", MessageID: "m3", Text: "who am i",
	}
	waitFor(t, "the drop notice", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "can't verify who you are on discord") && strings.Contains(p, "principal map") {
				return true
			}
		}
		return false
	})
}

// A console author id arriving on a NON-console conversation must not get
// the console principal: the fixed principal is only as good as the grant,
// and the grant is on the console subject, not on the string "console".
func TestTheConsolePrincipalIsBoundToTheConsoleConversation(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-spoof", Kind: "group",
		AuthorID: consoleAuthor, MessageID: "m4", Text: "spoof",
	}
	waitFor(t, "the drop notice", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "can't verify who you are on discord") {
				return true
			}
		}
		return false
	})
}

func latestSubmissionOrNil(bus *lib.Client) (*lib.Envelope, Authority) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	env, err := bus.ReadTopicLatest(ctx, "TASKS", "a2a.tasks.platform.*.in")
	if err != nil || env == nil {
		return nil, Authority{}
	}
	var a Authority
	_ = json.Unmarshal(env.Authority, &a)
	return env, a
}

// With the console running beside the configured backend, an empty
// principal map no longer drops every inbound message, only the configured
// backend's: the warning names that backend.
func TestEmptyPrincipalMapWarningNamesTheBackend(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("gateway-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	logs := &lockedBuffer{}
	cfg := &Config{
		NATSURL:          s.ClientURL(),
		PrincipalMapPath: filepath.Join(t.TempDir(), "absent"),
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
	}
	if _, err := New(Options{Client: client, Adapter: newFakeAdapter(), Config: cfg, Backend: "discord", Logger: slog.New(slog.NewTextHandler(logs, nil))}); err != nil {
		t.Fatal(err)
	}
	if want := "principal map is empty; every discord message will be dropped at verification"; !strings.Contains(logs.String(), want) {
		t.Errorf("log lacks %q:\n%s", want, logs.String())
	}
}
