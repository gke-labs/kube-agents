package gateway

import (
	"context"
	"errors"
	"strings"
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

func TestMultiAdapterDispatchesOnTheConversationPrefix(t *testing.T) {
	d, c := newFakeAdapter(), newFakeAdapter()
	m, err := NewMultiAdapter("discord", map[string]Adapter{"discord": d, "console": c})
	if err != nil {
		t.Fatal(err)
	}
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

func TestMultiAdapterFansRunInAndReturnsTheFirstError(t *testing.T) {
	d := newFakeAdapter()
	boom := &runErrAdapter{err: errors.New("console died")}
	m, err := NewMultiAdapter("discord", map[string]Adapter{"discord": d, "console": boom})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var got []InboundMessage
	done := make(chan error, 1)
	go func() { done <- m.Run(ctx, func(msg InboundMessage) { got = append(got, msg) }) }()
	d.inbox <- InboundMessage{Conversation: "discord:g1/c1", Text: "hi"}
	err = <-done
	if err == nil || !strings.Contains(err.Error(), "console died") {
		t.Errorf("Run returned %v, want the console error", err)
	}
	if len(got) != 1 {
		t.Errorf("discord's message did not reach the handler before the exit: %v", got)
	}
}

func TestMultiAdapterOpenDirectGoesToThePrimary(t *testing.T) {
	d, c := newFakeAdapter(), newFakeAdapter()
	m, _ := NewMultiAdapter("discord", map[string]Adapter{"discord": d, "console": c})
	if _, err := m.OpenDirect("1001"); err != nil {
		t.Errorf("OpenDirect via primary: %v", err)
	}
	if _, err := NewMultiAdapter("slack", map[string]Adapter{"discord": d}); err == nil {
		t.Error("a primary that is not in the map was accepted")
	}
}
