package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// consoleRig is a console adapter on an embedded server plus a raw client
// standing in for the browser.
type consoleRig struct {
	adapter *ConsoleAdapter
	browser *nats.Conn
	logs    *bytes.Buffer
	mu      sync.Mutex
	got     []InboundMessage
}

func startConsoleRig(t *testing.T) *consoleRig {
	t.Helper()
	s := startServer(t)
	logs := &bytes.Buffer{}
	log := slog.New(slog.NewTextHandler(logs, nil))
	a, err := NewConsoleAdapter(s.ClientURL(), nil, log)
	if err != nil {
		t.Fatalf("NewConsoleAdapter: %v", err)
	}
	t.Cleanup(a.Close)
	browser, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatalf("browser connect: %v", err)
	}
	t.Cleanup(browser.Close)
	r := &consoleRig{adapter: a, browser: browser, logs: logs}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() {
		_ = a.Run(ctx, func(m InboundMessage) {
			r.mu.Lock()
			r.got = append(r.got, m)
			r.mu.Unlock()
		})
	}()
	// Run subscribes asynchronously; wait for it so the first publish is
	// not lost. Flush on the adapter's own connection orders after the SUB.
	waitFor(t, "console subscription", func() bool { return a.subscribed() })
	return r
}

func (r *consoleRig) inbound() []InboundMessage {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]InboundMessage(nil), r.got...)
}

func (r *consoleRig) send(t *testing.T, token string, frame ConsoleInFrame) {
	t.Helper()
	data, _ := json.Marshal(frame)
	if err := r.browser.Publish("chat.console."+token+".in", data); err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
}

func TestConsoleFrameBecomesAnInboundMessage(t *testing.T) {
	r := startConsoleRig(t)
	r.send(t, "tab-1", ConsoleInFrame{MessageID: "m1", Text: "hello there"})
	waitFor(t, "inbound", func() bool { return len(r.inbound()) == 1 })
	got := r.inbound()[0]
	want := InboundMessage{Conversation: "console:tab-1", Kind: "dm", AuthorID: consoleAuthor, MessageID: "m1", Text: "hello there"}
	if got != want {
		t.Errorf("inbound = %+v, want %+v", got, want)
	}
}

func TestConsoleDropsEmptyMalformedAndOversizeFrames(t *testing.T) {
	r := startConsoleRig(t)
	// Not JSON.
	if err := r.browser.Publish("chat.console.tab-2.in", []byte("not json")); err != nil {
		t.Fatal(err)
	}
	// Empty text.
	r.send(t, "tab-2", ConsoleInFrame{MessageID: "m2", Text: "   "})
	// Oversize text: past consoleTextCap.
	r.send(t, "tab-2", ConsoleInFrame{MessageID: "m3", Text: strings.Repeat("x", consoleTextCap+1)})
	// A good one after them, to prove the drops did not wedge the subscription.
	r.send(t, "tab-2", ConsoleInFrame{MessageID: "m4", Text: "ok"})
	waitFor(t, "the good frame", func() bool { return len(r.inbound()) == 1 })
	if r.inbound()[0].MessageID != "m4" {
		t.Errorf("got %+v, want only m4", r.inbound())
	}
	if !strings.Contains(r.logs.String(), "console frame dropped") {
		t.Errorf("drops were silent:\n%s", r.logs.String())
	}
}

func TestConsoleOversizeFrameGetsANotice(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-3.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	r.send(t, "tab-3", ConsoleInFrame{MessageID: "m5", Text: strings.Repeat("x", consoleTextCap+1)})
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("no notice on .out: %v", err)
	}
	var out ConsoleOutFrame
	if err := json.Unmarshal(msg.Data, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.Text, "16 KiB") || out.Edit {
		t.Errorf("notice = %+v", out)
	}
	if len(r.inbound()) != 0 {
		t.Errorf("oversize frame reached the gateway: %+v", r.inbound())
	}
}

func TestConsoleRejectsATokenThatIsNotOneLabel(t *testing.T) {
	// The wildcard subscription cannot deliver a dotted token, so this is
	// the parser's own check on the conversation id it is handed back
	// (Post/Edit take the id, not the subject).
	for _, conv := range []string{"console:", "console:Has.Dot", "console:UPPER", "console:with space", "console:" + strings.Repeat("a", 64), "discord:g1/c1"} {
		if _, ok := consoleConversationToken(conv); ok {
			t.Errorf("%q accepted", conv)
		}
	}
	for _, conv := range []string{"console:a", "console:tab-1", "console:" + strings.Repeat("a", 63)} {
		if _, ok := consoleConversationToken(conv); !ok {
			t.Errorf("%q rejected", conv)
		}
	}
	r := startConsoleRig(t)
	if _, err := r.adapter.Post("console:Has.Dot", "hi"); err == nil {
		t.Error("Post to a malformed conversation succeeded")
	}
}

func TestConsolePostAndEditArriveAsOutFrames(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-4.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()

	id, err := r.adapter.Post("console:tab-4", "spinning up")
	if err != nil {
		t.Fatal(err)
	}
	if err := r.adapter.Edit("console:tab-4", id, "step 1"); err != nil {
		t.Fatal(err)
	}
	var frames []ConsoleOutFrame
	for len(frames) < 2 {
		msg, err := sub.NextMsg(5 * time.Second)
		if err != nil {
			t.Fatalf("after %d frames: %v", len(frames), err)
		}
		var f ConsoleOutFrame
		if err := json.Unmarshal(msg.Data, &f); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, f)
	}
	if frames[0] != (ConsoleOutFrame{MessageID: id, Text: "spinning up"}) {
		t.Errorf("post frame = %+v", frames[0])
	}
	if frames[1] != (ConsoleOutFrame{MessageID: id, Text: "step 1", Edit: true}) {
		t.Errorf("edit frame = %+v", frames[1])
	}
}

func TestConsoleRosterIsTheOneAuthorAndOpenDirectIsNotOffered(t *testing.T) {
	r := startConsoleRig(t)
	ids, complete, err := r.adapter.Roster("console:tab-5")
	if err != nil || !complete || len(ids) != 1 || ids[0] != consoleAuthor {
		t.Errorf("roster = %v %v %v", ids, complete, err)
	}
	if _, err := r.adapter.OpenDirect(consoleAuthor); err == nil {
		t.Error("OpenDirect succeeded; the console has no channel beyond the conversation")
	}
}

// Review focus 3: the render predates the console grants. The subscribe is
// refused asynchronously; the adapter must say so with the remedy.
func TestConsoleLogsARefusedSubscriptionWithTheRemedy(t *testing.T) {
	opts := &natsserver.Options{
		Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true,
		Users: []*natsserver.User{{
			Username: "gateway", Password: "pw",
			Permissions: &natsserver.Permissions{
				Subscribe: &natsserver.SubjectPermission{Allow: []string{"a2a.>"}},
				Publish:   &natsserver.SubjectPermission{Allow: []string{"a2a.>"}},
			},
		}},
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("server")
	}
	t.Cleanup(s.Shutdown)

	logs := &bytes.Buffer{}
	a, err := NewConsoleAdapter(s.ClientURL(), []nats.Option{nats.UserInfo("gateway", "pw")}, slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = a.Run(ctx, func(InboundMessage) {}) }()
	waitFor(t, "the refusal in the log", func() bool {
		return strings.Contains(logs.String(), "console subscription refused") &&
			strings.Contains(logs.String(), "predates the console identity")
	})
}
