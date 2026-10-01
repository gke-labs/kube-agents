package gateway

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
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
