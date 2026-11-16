package gateway

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

var (
	_ TaskObserver      = (*MultiAdapter)(nil)
	_ SessionLookupSink = (*MultiAdapter)(nil)
)

type runErrAdapter struct {
	fakeAdapter
	err error
}

func (a *runErrAdapter) Run(ctx context.Context, _ func(InboundMessage)) error {
	select {
	case <-ctx.Done():
		return nil
	case <-time.After(50 * time.Millisecond):
		return a.err
	}
}

// flakyAdapter stops right away on every Run, with err (nil included), and
// counts how often it was started.
type flakyAdapter struct {
	fakeAdapter
	err  error
	runs atomic.Int32
}

func (a *flakyAdapter) Run(context.Context, func(InboundMessage)) error {
	a.runs.Add(1)
	return a.err
}

func newTestMux(t *testing.T, byPrefix map[string]Adapter) *MultiAdapter {
	t.Helper()
	m, err := NewMultiAdapter("discord", "console", byPrefix, nil)
	if err != nil {
		t.Fatal(err)
	}
	m.restartBase, m.restartMax = time.Millisecond, 4*time.Millisecond
	return m
}

func TestMultiAdapterDispatchesOnTheConversationPrefix(t *testing.T) {
	d, c := newFakeAdapter(), newFakeAdapter()
	m := newTestMux(t, map[string]Adapter{"discord": d, "console": c})
	if _, err := m.Post("discord:g1/c1", "to discord"); err != nil {
		t.Fatal(err)
	}
	id, err := m.Post("console:tab-1", "to console")
	if err != nil {
		t.Fatal(err)
	}
	if err := m.Edit("console:tab-1", id, "edited"); err != nil {
		t.Fatal(err)
	}
	if got := d.postTexts(); len(got) != 1 || got[0] != "to discord" {
		t.Errorf("discord posts = %v", got)
	}
	if got := c.postTexts(); len(got) != 1 || got[0] != "to console" {
		t.Errorf("console posts = %v", got)
	}
	if got := c.editTexts(); len(got) != 1 || got[0] != "edited" {
		t.Errorf("console edits = %v", got)
	}
	c.roster = []string{"console"}
	ids, _, err := m.Roster("console:tab-1")
	if err != nil || len(ids) != 1 || ids[0] != "console" {
		t.Errorf("roster = %v %v", ids, err)
	}
	if _, err := m.Post("slack:x", "nobody"); err == nil || !strings.Contains(err.Error(), "slack") {
		t.Errorf("unknown prefix: err = %v", err)
	}
	if _, err := m.Post("noprefix", "nobody"); err == nil {
		t.Error("prefix-less conversation accepted")
	}
}

func TestMultiAdapterReturnsWhenTheConsoleStops(t *testing.T) {
	d := newFakeAdapter()
	boom := &runErrAdapter{err: errors.New("console died")}
	m := newTestMux(t, map[string]Adapter{"discord": d, "console": boom})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var got []InboundMessage
	done := make(chan error, 1)
	go func() { done <- m.Run(ctx, func(msg InboundMessage) { got = append(got, msg) }) }()
	d.inbox <- InboundMessage{Conversation: "discord:g1/c1", Text: "hi"}
	err := <-done
	if err == nil || !strings.Contains(err.Error(), "console died") {
		t.Errorf("Run returned %v, want the console error", err)
	}
	if len(got) != 1 {
		t.Errorf("discord's message did not reach the handler before the exit: %v", got)
	}
}

// A console that returns nil while the gateway is still running has stopped
// just the same, and must not leave the process up with its door shut.
func TestMultiAdapterTreatsAConsoleNilReturnAsAStop(t *testing.T) {
	m := newTestMux(t, map[string]Adapter{"discord": newFakeAdapter(), "console": &flakyAdapter{}})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := m.Run(ctx, func(InboundMessage) {})
	if err == nil || !strings.Contains(err.Error(), "console adapter") {
		t.Errorf("Run returned %v, want a console stop", err)
	}
}

// A chat backend that keeps failing is restarted, and the console keeps
// delivering the whole time.
func TestMultiAdapterRestartsAChatBackendAndKeepsTheConsole(t *testing.T) {
	for _, tc := range []struct {
		name string
		err  error
	}{{"error", errors.New("bad token")}, {"nil", nil}} {
		t.Run(tc.name, func(t *testing.T) {
			chat, c := &flakyAdapter{err: tc.err}, newFakeAdapter()
			m := newTestMux(t, map[string]Adapter{"discord": chat, "console": c})
			ctx, cancel := context.WithCancel(context.Background())
			var mu sync.Mutex
			var got []string
			done := make(chan error, 1)
			go func() {
				done <- m.Run(ctx, func(msg InboundMessage) {
					mu.Lock()
					got = append(got, msg.Text)
					mu.Unlock()
				})
			}()
			deadline := time.Now().Add(5 * time.Second)
			for chat.runs.Load() < 3 && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			if n := chat.runs.Load(); n < 3 {
				t.Fatalf("chat backend ran %d times, want it restarted", n)
			}
			select {
			case err := <-done:
				t.Fatalf("Run returned %v while the console was up", err)
			default:
			}
			c.inbox <- InboundMessage{Conversation: "console:tab-1", Text: "still here"}
			for time.Now().Before(deadline) {
				mu.Lock()
				n := len(got)
				mu.Unlock()
				if n == 1 {
					break
				}
				time.Sleep(time.Millisecond)
			}
			cancel()
			if err := <-done; err != nil {
				t.Errorf("Run returned %v on shutdown, want nil", err)
			}
			mu.Lock()
			defer mu.Unlock()
			if len(got) != 1 || got[0] != "still here" {
				t.Errorf("console messages = %v", got)
			}
		})
	}
}

// slowOnceAdapter stops right away on every Run but the slowRun'th, which
// lasts slowFor first.
type slowOnceAdapter struct {
	fakeAdapter
	slowRun int32
	slowFor time.Duration
	runs    atomic.Int32
}

func (a *slowOnceAdapter) Run(context.Context, func(InboundMessage)) error {
	if a.runs.Add(1) == a.slowRun {
		time.Sleep(a.slowFor)
	}
	return errors.New("down")
}

// The restart delay doubles to the cap, and a run that lasted the cap starts
// the doubling over.
func TestMultiAdapterRestartBackoff(t *testing.T) {
	chat := &slowOnceAdapter{slowRun: 6, slowFor: 20 * time.Millisecond}
	m := newTestMux(t, map[string]Adapter{"discord": chat, "console": newFakeAdapter()})
	ms := time.Millisecond
	want := []time.Duration{ms, 2 * ms, 4 * ms, 4 * ms, 4 * ms, ms, 2 * ms, 4 * ms}
	var mu sync.Mutex
	var delays []time.Duration
	ctx, cancel := context.WithCancel(context.Background())
	m.after = func(d time.Duration) <-chan time.Time {
		mu.Lock()
		defer mu.Unlock()
		delays = append(delays, d)
		if len(delays) >= len(want) {
			cancel()
		}
		ch := make(chan time.Time, 1)
		ch <- time.Time{}
		return ch
	}
	if err := m.Run(ctx, func(InboundMessage) {}); err != nil {
		t.Fatalf("Run returned %v on shutdown, want nil", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(delays) < len(want) {
		t.Fatalf("delays = %v, want at least %v", delays, want)
	}
	for i, d := range want {
		if delays[i] != d {
			t.Fatalf("delays = %v, want %v", delays[:len(want)], want)
		}
	}
}

// observingAdapter records the conversations TaskStarted named.
type observingAdapter struct {
	fakeAdapter
	started []string
}

func (a *observingAdapter) TaskStarted(conversation, _ string) {
	a.started = append(a.started, conversation)
}
func (a *observingAdapter) TaskTerminal(string, string, lib.TaskState, TerminalSource, string) {}
func (a *observingAdapter) TaskAccepted(string, string)                                        {}
func (a *observingAdapter) CancelPublished(string, string)                                     {}

// Observer calls go to the backend that owns the conversation's prefix, and
// a backend that is not a TaskObserver is simply told nothing.
func TestMultiAdapterRoutesObserverCallsOnThePrefix(t *testing.T) {
	obs := &observingAdapter{fakeAdapter: *newFakeAdapter()}
	m := newTestMux(t, map[string]Adapter{"discord": newFakeAdapter(), "console": obs})
	m.TaskStarted("discord:g1/c1", "t1")
	m.TaskStarted("noprefix", "t2")
	m.TaskStarted("console:tab-1", "t3")
	if len(obs.started) != 1 || obs.started[0] != "console:tab-1" {
		t.Errorf("console observer saw %v, want only console:tab-1", obs.started)
	}
}

func TestMultiAdapterOpenDirectGoesToThePrimary(t *testing.T) {
	d, c := newFakeAdapter(), newFakeAdapter()
	m := newTestMux(t, map[string]Adapter{"discord": d, "console": c})
	if _, err := m.OpenDirect("1001"); err != nil {
		t.Errorf("OpenDirect via primary: %v", err)
	}
	if _, err := NewMultiAdapter("slack", "console", map[string]Adapter{"discord": d, "console": c}, nil); err == nil {
		t.Error("a primary that is not in the map was accepted")
	}
	if _, err := NewMultiAdapter("discord", "console", map[string]Adapter{"discord": d}, nil); err == nil {
		t.Error("an essential backend that is not in the map was accepted")
	}
}

// A terminal reaches the backend that owns the conversation when it takes
// terminal frames; one that does not answers errNoTerminalFrames, which the
// caller treats as nothing to do (the relay's notices go out either way).
func TestMultiAdapterRoutesATerminalToItsBackend(t *testing.T) {
	d, c := newFakeAdapter(), newFakeAdapter()
	c.setTakesTerminals()
	m := newTestMux(t, map[string]Adapter{"discord": d, "console": c})
	term := ConsoleTerminal{TaskID: "task-1", Addressee: "platform", State: lib.StateCompleted, Text: "done"}
	if err := m.PostTerminal("console:tab-1", term); err != nil {
		t.Fatal(err)
	}
	if got := c.terminalPosts(); len(got) != 1 || got[0] != (fakeTerminal{"console:tab-1", term}) {
		t.Errorf("console terminals = %+v", got)
	}
	if err := m.PostTerminal("discord:g1/c1", term); !errors.Is(err, errNoTerminalFrames) {
		t.Errorf("discord terminal err = %v, want errNoTerminalFrames", err)
	}
	if err := m.PostTerminal("nowhere:x", term); err == nil {
		t.Error("a terminal for an unknown backend was accepted")
	}
}

func TestMultiAdapterStampsTheBackendOnInbound(t *testing.T) {
	s, g, c := newFakeAdapter(), newFakeAdapter(), newFakeAdapter()
	m, err := NewMultiAdapter("", "console", map[string]Adapter{"slack": s, "gchat": g, "console": c}, nil)
	if err != nil {
		t.Fatal(err)
	}
	got := make(chan InboundMessage, 3)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go m.Run(ctx, func(msg InboundMessage) { got <- msg })
	s.inbox <- InboundMessage{Conversation: "slack:C1/1.1", AuthorID: "U1"}
	g.inbox <- InboundMessage{Conversation: "gchat:spaces/A", AuthorID: "a@x"}
	c.inbox <- InboundMessage{Conversation: "console:tab", AuthorID: "me", Backend: consoleBackend}
	want := map[string]string{"slack:C1/1.1": "slack", "gchat:spaces/A": "gchat", "console:tab": "console"}
	for range want {
		select {
		case msg := <-got:
			if msg.Backend != want[msg.Conversation] {
				t.Errorf("%s stamped %q, want %q", msg.Conversation, msg.Backend, want[msg.Conversation])
			}
		case <-time.After(time.Second):
			t.Fatal("inbound not delivered")
		}
	}
}

// namedDMAdapter answers OpenDirect with its own name and the id it was
// handed, so a test sees which backend a direct message reached and with
// which id.
type namedDMAdapter struct {
	*fakeAdapter
	name string
}

func (a namedDMAdapter) OpenDirect(userID string) (string, error) {
	return a.name + ":dm:" + userID, nil
}

func TestMultiAdapterOpenDirectResolvesPerBackend(t *testing.T) {
	s := namedDMAdapter{newFakeAdapter(), "slack"}
	g, c := newFakeAdapter(), newFakeAdapter()
	m, err := NewMultiAdapter("", "console", map[string]Adapter{"slack": s, "gchat": g, "console": c}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := m.OpenDirect("slack:U1"); err != nil || got != "slack:dm:U1" {
		t.Errorf("prefixed id: got %q, %v; want slack:dm:U1", got, err)
	}
	// A Slack guest id from another workspace carries the separator itself;
	// the prefix in front of it still picks the backend.
	if got, err := m.OpenDirect("slack:T1:U1"); err != nil || got != "slack:dm:T1:U1" {
		t.Errorf("prefixed guest id: got %q, %v; want slack:dm:T1:U1", got, err)
	}
	if _, err := m.OpenDirect("U1"); err == nil {
		t.Error("a bare id with two real backends was accepted")
	}
	if _, err := m.OpenDirect("T1:U1"); err == nil || !strings.Contains(err.Error(), "T1") {
		t.Errorf("bare guest id with two real backends: err = %v, want one naming T1", err)
	}
	if _, err := m.OpenDirect("nosuch:U1"); err == nil || !strings.Contains(err.Error(), "nosuch") {
		t.Errorf("unknown prefix: err = %v, want one naming nosuch", err)
	}
	d := namedDMAdapter{newFakeAdapter(), "discord"}
	one := newTestMux(t, map[string]Adapter{"discord": d, "console": c})
	if got, err := one.OpenDirect("1001"); err != nil || got != "discord:dm:1001" {
		t.Errorf("bare id with one real backend: got %q, %v; want discord:dm:1001", got, err)
	}
	// One real backend takes any id it understands, a colon in it or not.
	if got, err := one.OpenDirect("T1:U1"); err != nil || got != "discord:dm:T1:U1" {
		t.Errorf("bare guest id with one real backend: got %q, %v; want discord:dm:T1:U1", got, err)
	}
	// A known backend the mux does not run is refused by name, not handed to
	// the primary.
	if _, err := one.OpenDirect("slack:U1"); err == nil || !strings.Contains(err.Error(), "slack") {
		t.Errorf("unarmed backend with one real backend: err = %v, want one naming slack", err)
	}
}

func TestMultiAdapterRestartsOneBackendWhileTheOtherServes(t *testing.T) {
	// Slack fails at every start; gchat and the console keep serving.
	bad := &flakyAdapter{err: errors.New("auth.test: invalid_auth")}
	g, c := newFakeAdapter(), newFakeAdapter()
	m, err := NewMultiAdapter("", "console", map[string]Adapter{"slack": bad, "gchat": g, "console": c}, nil)
	if err != nil {
		t.Fatal(err)
	}
	m.restartBase, m.restartMax = time.Millisecond, 4*time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	got := make(chan InboundMessage, 1)
	done := make(chan error, 1)
	go func() { done <- m.Run(ctx, func(msg InboundMessage) { got <- msg }) }()
	deadline := time.Now().Add(time.Second)
	for bad.runs.Load() < 3 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if bad.runs.Load() < 3 {
		t.Fatalf("slack restarted %d times, want at least 3", bad.runs.Load())
	}
	select {
	case err := <-done:
		t.Fatalf("Run returned while slack restarts: %v", err)
	case <-time.After(20 * time.Millisecond):
	}
	g.inbox <- InboundMessage{Conversation: "gchat:spaces/A", AuthorID: "a@x"}
	select {
	case msg := <-got:
		if msg.Backend != "gchat" {
			t.Errorf("gchat inbound stamped %q, want gchat", msg.Backend)
		}
	case <-time.After(time.Second):
		t.Fatal("gchat stopped serving while slack restarts: inbound not delivered")
	}
	if _, err := m.Post("gchat:spaces/A", "still here"); err != nil {
		t.Errorf("gchat stopped dispatching while slack restarts: %v", err)
	}
}

// knownBackendName reads backendArms, so a backend added there is refused by
// name without a second edit.
func TestKnownBackendNameFollowsTheArmTable(t *testing.T) {
	for _, arm := range backendArms {
		if !knownBackendName(arm.name) {
			t.Errorf("knownBackendName(%q) = false for a backendArms row", arm.name)
		}
	}
	if !knownBackendName(consoleBackend) {
		t.Errorf("knownBackendName(%q) = false", consoleBackend)
	}
	for _, name := range []string{"nosuch", "T1"} {
		if knownBackendName(name) {
			t.Errorf("knownBackendName(%q) = true", name)
		}
	}
}
