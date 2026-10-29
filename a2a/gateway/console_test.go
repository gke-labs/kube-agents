package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
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
	want := InboundMessage{Conversation: "console:tab-1", Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m1", Text: "hello there"}
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

// A frame whose kind is neither empty nor "text" is a command the gateway
// does not interpret yet; it is dropped, not forwarded as an ask.
func TestConsoleDropsAnUnknownKind(t *testing.T) {
	r := startConsoleRig(t)
	r.send(t, "tab-6", ConsoleInFrame{MessageID: "m6", Text: "/restart everything", Kind: "command"})
	r.send(t, "tab-6", ConsoleInFrame{MessageID: "m7", Text: "a real ask", Kind: "text"})
	waitFor(t, "the text frame", func() bool { return len(r.inbound()) == 1 })
	time.Sleep(100 * time.Millisecond) // a late delivery of m6 would land now
	if got := r.inbound(); len(got) != 1 || got[0].MessageID != "m7" {
		t.Errorf("got %+v, want only m7", got)
	}
	if !strings.Contains(r.logs.String(), `console frame dropped`) || !strings.Contains(r.logs.String(), `reason="unknown kind" kind=command`) {
		t.Errorf("unknown-kind drop not logged with its kind:\n%s", r.logs.String())
	}
}

func TestConsoleLogsAnOversizeKindBounded(t *testing.T) {
	r := startConsoleRig(t)
	huge := strings.Repeat("k", 64*1024)
	r.send(t, "tab-6", ConsoleInFrame{MessageID: "m8", Text: "x", Kind: huge})
	// Frames on one subscription are handled in order, so once m9 is in,
	// m8's drop line has been written.
	r.send(t, "tab-6", ConsoleInFrame{MessageID: "m9", Text: "a real ask"})
	waitFor(t, "the text frame", func() bool { return len(r.inbound()) == 1 })
	if got := r.inbound(); got[0].MessageID != "m9" {
		t.Errorf("got %+v, want only m9", got)
	}
	if !strings.Contains(r.logs.String(), `reason="unknown kind"`) {
		t.Errorf("unknown-kind drop not logged:\n%.2000s", r.logs.String())
	}
	if strings.Contains(r.logs.String(), strings.Repeat("k", consoleMessageIDCap+1)) {
		t.Errorf("kind logged past the %d-byte cap", consoleMessageIDCap)
	}
	if n := r.logs.Len(); n > 4*1024 {
		t.Errorf("log is %d bytes after one frame, want it bounded", n)
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
	for _, conv := range []string{"console:", "console:Has.Dot", "console:UPPER", "console:with space", "console:" + strings.Repeat("a", 64), "console:-lead", "console:trail-", "discord:g1/c1"} {
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

// A terminal goes out as one frame naming its task, under an id derived from
// the task and its state, so the same terminal from a second adapter (a
// restart, a redelivery) repeats it. The wire spelling is pinned, since the MCP server
// and the page match on it. A notice carries none of the terminal fields.
func TestConsoleTerminalArrivesAsAResultFrame(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-5.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	term := ConsoleTerminal{TaskID: "task-1", Addressee: "platform", State: lib.StateCompleted, Text: "all green"}
	if err := r.adapter.PostTerminal("console:tab-5", term); err != nil {
		t.Fatal(err)
	}
	other, err := NewConsoleAdapter(r.browser.ConnectedUrl(), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(other.Close)
	if err := other.PostTerminal("console:tab-5", term); err != nil {
		t.Fatal(err)
	}
	if _, err := r.adapter.Post("console:tab-5", "a notice"); err != nil {
		t.Fatal(err)
	}
	var raw []string
	for len(raw) < 3 {
		msg, err := sub.NextMsg(5 * time.Second)
		if err != nil {
			t.Fatalf("after %d frames: %v", len(raw), err)
		}
		raw = append(raw, string(msg.Data))
	}
	// Two connections publish, so the frames arrive in no fixed order.
	const want = `{"messageId":"r-task-1-completed","text":"all green","kind":"result","state":"completed","addressee":"platform","taskId":"task-1"}`
	var terminals, notices []string
	for _, f := range raw {
		if f == want {
			terminals = append(terminals, f)
		} else {
			notices = append(notices, f)
		}
	}
	if len(terminals) != 2 || len(notices) != 1 {
		t.Fatalf("frames = %q, want two exactly %s and one notice", raw, want)
	}
	for _, field := range []string{`"kind"`, `"state"`, `"addressee"`, `"taskId"`} {
		if strings.Contains(notices[0], field) {
			t.Errorf("the notice frame carries %s: %s", field, notices[0])
		}
	}
}

// cardsPending is spelled on the wire only when set.
func TestConsoleTerminalSpellsCardsPendingOnlyWhenSet(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-9.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	if err := r.adapter.PostTerminal("console:tab-9", ConsoleTerminal{TaskID: "task-4", Addressee: "platform", State: lib.StateCompleted, Text: "ack", CardsPending: true}); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"messageId":"r-task-4-completed","text":"ack","kind":"result","state":"completed","addressee":"platform","taskId":"task-4","cardsPending":true}`
	if string(msg.Data) != want {
		t.Errorf("frame = %s, want %s", msg.Data, want)
	}
}

// A terminal past consoleResultTextCap is cut to fit under the server's
// max_payload, says so, and stays valid UTF-8 where the cut lands inside a
// multi-byte rune.
func TestConsoleTerminalCutsAnOversizeAnswer(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-6.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	long := strings.Repeat("é", consoleResultTextCap)
	if err := r.adapter.PostTerminal("console:tab-6", ConsoleTerminal{TaskID: "task-2", State: lib.StateCompleted, Text: long}); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var f ConsoleOutFrame
	if err := json.Unmarshal(msg.Data, &f); err != nil {
		t.Fatal(err)
	}
	if len(f.Text) > consoleResultTextCap || !strings.HasSuffix(f.Text, consoleResultCutNote) || !utf8.ValidString(f.Text) {
		t.Errorf("cut text: %d bytes, cut note %v, valid UTF-8 %v", len(f.Text), strings.HasSuffix(f.Text, consoleResultCutNote), utf8.ValidString(f.Text))
	}
}

// Text that JSON escapes six bytes for one (`<` becomes \u003c) passes the
// byte cap and would still overrun max_payload once encoded; the frame is cut
// again until it fits, rather than failing to publish.
func TestConsoleTerminalFitsUnderMaxPayloadAfterEscaping(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-7.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	escapable := strings.Repeat("<", consoleResultTextCap)
	if err := r.adapter.PostTerminal("console:tab-7", ConsoleTerminal{TaskID: "task-3", State: lib.StateCompleted, Text: escapable}); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if max := r.browser.MaxPayload(); int64(len(msg.Data)) > max {
		t.Fatalf("frame is %d bytes, over max_payload %d", len(msg.Data), max)
	}
	var f ConsoleOutFrame
	if err := json.Unmarshal(msg.Data, &f); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(f.Text, consoleResultCutNote) {
		t.Errorf("the escaped answer was not cut: %d bytes", len(f.Text))
	}
}

// A notify frame's wire spelling is pinned (the MCP server and the page read
// it): kind notify, the turn it answers, a minted n- id, and an edit that
// repeats the id it names.
func TestConsoleNotifyArrivesAsANotifyFrame(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-8.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	id, err := r.adapter.PostNotifyFrame("console:tab-8", "", "the report", "task-7", false)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(id, consoleNotifyIDPrefix) {
		t.Errorf("minted id %q lacks the %q prefix", id, consoleNotifyIDPrefix)
	}
	if _, err := r.adapter.PostNotifyFrame("console:tab-8", id, "the report, updated", "task-7", true); err != nil {
		t.Fatal(err)
	}
	var raw []string
	for len(raw) < 2 {
		msg, err := sub.NextMsg(5 * time.Second)
		if err != nil {
			t.Fatalf("after %d frames: %v", len(raw), err)
		}
		raw = append(raw, string(msg.Data))
	}
	if want := `{"messageId":"` + id + `","text":"the report","kind":"notify","replyTo":"task-7"}`; raw[0] != want {
		t.Errorf("notify frame = %s, want %s", raw[0], want)
	}
	if want := `{"messageId":"` + id + `","text":"the report, updated","edit":true,"kind":"notify","replyTo":"task-7"}`; raw[1] != want {
		t.Errorf("edit frame = %s, want %s", raw[1], want)
	}
}

// Notice ids carry a per-adapter random component, so a restarted gateway's
// first notice is not c-1 again and a page holding the last boot's c-1 does
// not take the new one's edits as its own.
func TestConsoleNoticeIDsDifferAcrossAdapters(t *testing.T) {
	s := startServer(t)
	idRe := regexp.MustCompile(`^c-[0-9a-f]{8}-1$`)
	var first []string
	for range 2 {
		a, err := NewConsoleAdapter(s.ClientURL(), nil, slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(a.Close)
		id, err := a.Post("console:tab-7", "hello")
		if err != nil {
			t.Fatal(err)
		}
		if !idRe.MatchString(id) {
			t.Errorf("notice id %q does not match c-<boot>-<n>", id)
		}
		first = append(first, id)
	}
	if first[0] == first[1] {
		t.Errorf("two adapters minted the same first notice id %q", first[0])
	}
}

// TestConsoleRunClosesTheConnectionOnCtxCancellation guards Close's doc
// comment: Run is documented to close the connection itself once ctx is
// cancelled, so a caller that trusts that and skips an explicit Close does
// not leak the connection.
func TestConsoleRunClosesTheConnectionOnCtxCancellation(t *testing.T) {
	s := startServer(t)
	a, err := NewConsoleAdapter(s.ClientURL(), nil, slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)))
	if err != nil {
		t.Fatalf("NewConsoleAdapter: %v", err)
	}
	t.Cleanup(a.Close)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = a.Run(ctx, func(InboundMessage) {})
		close(done)
	}()
	waitFor(t, "console subscription", func() bool { return a.subscribed() })

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after ctx cancellation")
	}
	if !a.closed() {
		t.Error("Run returned without closing the connection")
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

// lockedBuffer is a bytes.Buffer safe to write from nats.go's callback
// goroutine while the test reads it.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// startRestrictedServer runs a server whose one user, gateway, holds only
// a2a.> both ways: a render that predates the console identity.
func startRestrictedServer(t *testing.T) *natsserver.Server {
	t.Helper()
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
	return s
}

// Review focus 3: the render predates the console grants. The subscribe is
// refused asynchronously; the adapter must say so with the remedy. The
// caller passes its own ErrorHandler, which must not displace the
// adapter's, and nats.go hands the handler a nil subscription, so the
// subject logged is the adapter's one wildcard.
func TestConsoleLogsARefusedSubscriptionWithTheRemedy(t *testing.T) {
	s := startRestrictedServer(t)
	logs := &lockedBuffer{}
	callerHandler := nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {})
	a, err := NewConsoleAdapter(s.ClientURL(), []nats.Option{nats.UserInfo("gateway", "pw"), callerHandler}, slog.New(slog.NewTextHandler(logs, nil)))
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
	if !strings.Contains(logs.String(), `subject=chat.console.*.in`) {
		t.Errorf("refusal log does not name the subscription subject:\n%s", logs.String())
	}
	if strings.Contains(logs.String(), "console publish refused") {
		t.Errorf("a subscribe refusal was logged as a publish refusal:\n%s", logs.String())
	}
}

// A refused notice publish (the gateway grant lacks chat.console.*.out) is
// logged as a publish refusal naming the subject, not as a subscription one.
func TestConsoleLogsARefusedPublishAsAPublish(t *testing.T) {
	s := startRestrictedServer(t)
	logs := &lockedBuffer{}
	a, err := NewConsoleAdapter(s.ClientURL(), []nats.Option{nats.UserInfo("gateway", "pw")}, slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	if _, err := a.Post("console:tab-8", "hello"); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the publish refusal in the log", func() bool {
		return strings.Contains(logs.String(), "console publish refused") &&
			strings.Contains(logs.String(), `subject=chat.console.tab-8.out`)
	})
	if strings.Contains(logs.String(), "console subscription refused") {
		t.Errorf("a publish refusal was logged as a subscription refusal:\n%s", logs.String())
	}
}

// The connection-state handlers are the adapter's own: a disconnect is
// logged even when the caller passed its own DisconnectErrHandler.
func TestConsoleLogsADisconnect(t *testing.T) {
	s := startServer(t)
	logs := &lockedBuffer{}
	callerHandler := nats.DisconnectErrHandler(func(*nats.Conn, error) {})
	a, err := NewConsoleAdapter(s.ClientURL(), []nats.Option{callerHandler}, slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	s.Shutdown()
	waitFor(t, "the disconnect in the log", func() bool {
		return strings.Contains(logs.String(), "console connection lost") &&
			strings.Contains(logs.String(), "console="+consoleConnName)
	})
}

// The refusal log promises the subscription is resent on reconnect. Pin
// that promise: a gateway refused on a render that predates the console
// identity starts receiving frames, with no restart, once the server comes
// back holding the grant. nats.go keeps a refused subscription in its own
// table and resends it on reconnect. A server that only reloads its
// config in place would not drop the connection, so this recovery rides on
// the NATS pod rolling, which is what the operator does on a render change.
func TestConsoleRecoversARefusedSubscriptionOnReconnect(t *testing.T) {
	s := startRestrictedServer(t)
	port := s.Addr().(*net.TCPAddr).Port
	logs := &lockedBuffer{}
	a, err := NewConsoleAdapter(s.ClientURL(),
		[]nats.Option{nats.UserInfo("gateway", "pw"), nats.ReconnectWait(50 * time.Millisecond)},
		slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	var mu sync.Mutex
	var got []InboundMessage
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() {
		_ = a.Run(ctx, func(m InboundMessage) {
			mu.Lock()
			got = append(got, m)
			mu.Unlock()
		})
	}()
	waitFor(t, "the refusal in the log", func() bool {
		return strings.Contains(logs.String(), "console subscription refused")
	})

	// The pod rolls onto the new render: same address, gateway now holds
	// the console subscribe, and a second user stands in for the browser.
	s.Shutdown()
	s.WaitForShutdown()
	granted := &natsserver.Options{
		Host: "127.0.0.1", Port: port, NoLog: true, NoSigs: true,
		Users: []*natsserver.User{
			{Username: "gateway", Password: "pw", Permissions: &natsserver.Permissions{
				Subscribe: &natsserver.SubjectPermission{Allow: []string{"a2a.>", "chat.console.*.in"}},
				Publish:   &natsserver.SubjectPermission{Allow: []string{"a2a.>", "chat.console.*.out"}},
			}},
			{Username: "browser", Password: "pw"},
		},
	}
	s2, err := natsserver.NewServer(granted)
	if err != nil {
		t.Fatal(err)
	}
	go s2.Start()
	if !s2.ReadyForConnections(10 * time.Second) {
		t.Fatal("second server")
	}
	t.Cleanup(s2.Shutdown)
	waitFor(t, "the reconnect in the log", func() bool {
		return strings.Contains(logs.String(), "console connection restored")
	})

	browser, err := nats.Connect(s2.ClientURL(), nats.UserInfo("browser", "pw"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(browser.Close)
	data, _ := json.Marshal(ConsoleInFrame{MessageID: "m-1", Text: "hello after the roll"})
	// The resent SUB and the browser's PUB travel on different connections,
	// so publish until one lands rather than guess at ordering.
	waitFor(t, "a frame delivered after the reconnect", func() bool {
		_ = browser.Publish("chat.console.tab-9.in", data)
		_ = browser.Flush()
		mu.Lock()
		defer mu.Unlock()
		return len(got) > 0
	})
	mu.Lock()
	first := got[0]
	mu.Unlock()
	if first.Conversation != "console:tab-9" || first.Text != "hello after the roll" {
		t.Errorf("delivered frame = %+v, want conversation console:tab-9 with the sent text", first)
	}
}

// A deliberate Close is not a lost connection. nats.go runs
// DisconnectErrHandler for a user-initiated Close too (with a nil err), so
// without the closing guard every orderly gateway shutdown logs a reconnect
// that is never coming - once per process, on every process.
func TestConsoleCloseDoesNotLogAsALostConnection(t *testing.T) {
	srv := startServer(t)
	logs := &lockedBuffer{}
	a, err := NewConsoleAdapter(srv.ClientURL(), nil, slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	a.Close()
	// The handler runs on nats.go's own goroutine, so give it room to be
	// wrong rather than racing it to the assertion.
	waitFor(t, "the connection to close", func() bool { return a.closed() })
	time.Sleep(200 * time.Millisecond)
	for _, bad := range []string{"console connection lost", "closed for good"} {
		if got := logs.String(); strings.Contains(got, bad) {
			t.Errorf("a deliberate Close logged %q:\n%s", bad, got)
		}
	}
}

// Close's doc offers it for an adapter that "needs closing early". A caller
// taking that at its word, then waiting on Run (or on MultiAdapter.Run, which
// waits for every backend), must not hang: the mux ends the process only when
// the console's Run returns, so a Run left parked here is the same half-deaf
// gateway the ClosedHandler exists to prevent.
func TestConsoleCloseWhileRunningEndsRun(t *testing.T) {
	srv := startServer(t)
	a, err := NewConsoleAdapter(srv.ClientURL(), nil, slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)))
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- a.Run(context.Background(), func(InboundMessage) {}) }()
	waitFor(t, "console subscription", func() bool { return a.subscribed() })

	a.Close()

	select {
	case runErr := <-done:
		if runErr == nil {
			t.Fatal("Run returned nil after an early Close; the mux does not cancel on nil")
		}
		if !strings.Contains(runErr.Error(), "closed while running") {
			t.Errorf("Run error does not name a deliberate close: %v", runErr)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after Close; a caller waiting on it hangs")
	}
}

// A sender-chosen messageId reaches a log line on every drop path and the
// ingress line on every accepted frame, so it needs the cap its neighbour
// text already has - otherwise one frame writes up to max_payload of log.
func TestConsoleDropsAnOversizeMessageIDWithoutLoggingIt(t *testing.T) {
	r := startConsoleRig(t)
	huge := strings.Repeat("z", consoleMessageIDCap+1)
	r.send(t, "tab-9", ConsoleInFrame{MessageID: huge, Text: "hi"})
	// Empty text is refused earlier than the cap, and that path logs the id too.
	r.send(t, "tab-9", ConsoleInFrame{MessageID: huge, Text: "   "})
	// A good frame last, so waiting on it proves the two before it are done.
	r.send(t, "tab-9", ConsoleInFrame{MessageID: "m1", Text: "ok"})

	waitFor(t, "the good frame", func() bool { return len(r.inbound()) == 1 })
	if got := r.inbound()[0].MessageID; got != "m1" {
		t.Errorf("forwarded %q, want only the frame with a sane id", got)
	}
	got := r.logs.String()
	if !strings.Contains(got, "oversize messageId") {
		t.Errorf("the oversize id was not refused:\n%s", got)
	}
	if strings.Contains(got, huge) {
		t.Error("the oversize messageId was written to the log it exists to bound")
	}
}

// A connection nats.go has given up on leaves the adapter subscribed to
// nothing, with no reconnect coming. Run has to surface that: MultiAdapter
// turns the console's return into a process restart, and a gateway that instead
// stays Running drops every console frame with no log line after the
// (by then false) "reconnecting" one.
func TestConsoleRunReturnsWhenTheConnectionIsClosedForGood(t *testing.T) {
	srv := startServer(t)
	logs := &lockedBuffer{}
	a, err := NewConsoleAdapter(srv.ClientURL(), nil, slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)

	done := make(chan error, 1)
	go func() { done <- a.Run(context.Background(), func(InboundMessage) {}) }()
	waitFor(t, "console subscription", func() bool { return a.subscribed() })

	// Close the connection underneath the adapter rather than through
	// a.Close, which is what a terminal abort looks like from here: the
	// closing flag stays unset, so this is not our own shutdown.
	a.nc.Close()

	select {
	case runErr := <-done:
		if runErr == nil {
			t.Fatal("Run returned nil on a connection closed for good; the mux will not restart the gateway")
		}
		if !strings.Contains(runErr.Error(), "closed for good") {
			t.Errorf("Run error does not name the cause: %v", runErr)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after the connection was closed for good")
	}
	if got := logs.String(); !strings.Contains(got, "closed for good") {
		t.Errorf("a terminal close was not logged as terminal:\n%s", got)
	}
}

// A notify frame cut to fit says the report was cut, not that the full text
// is a task's result on the bus: a card's report is not on TASKS.
func TestAConsoleNotifyFrameCutSaysTheReportWasCut(t *testing.T) {
	r := startConsoleRig(t)
	sub, err := r.browser.SubscribeSync("chat.console.tab-11.out")
	if err != nil {
		t.Fatal(err)
	}
	_ = r.browser.Flush()
	if _, err := r.adapter.PostNotifyFrame("console:tab-11", "", strings.Repeat("<", consoleResultTextCap), "task-1", false); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var f ConsoleOutFrame
	if err := json.Unmarshal(msg.Data, &f); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(f.Text, consoleNotifyCutNote) || strings.HasSuffix(f.Text, consoleResultCutNote) {
		t.Errorf("cut notify text ends %q, want the notify note", f.Text[len(f.Text)-80:])
	}
}
