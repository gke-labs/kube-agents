package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// A chat backend that stops is run again after a delay that doubles
	// from muxRestartBase up to muxRestartMax. A run that lasted at least
	// muxRestartMax before stopping starts the doubling over, so a backend
	// that drops once a day comes back in a second, and one with a bad
	// token settles into one attempt a minute.
	muxRestartBase = time.Second
	muxRestartMax  = time.Minute
)

// MultiAdapter presents several backends to the gateway as one Adapter.
// Conversation ids are backend-qualified by convention (discord:…, gchat:…,
// console:…), so every per-conversation operation dispatches on the prefix.
// OpenDirect takes a user id with no prefix and goes to the primary backend,
// the one the process was configured for.
//
// One backend is essential: when it stops, Run returns and the process
// restarts. Every other backend is contained: when it stops, Run logs it and
// runs it again after a backoff while the rest keep going. The console is
// the essential one, because it is the way in when chat is broken, and a
// chat backend that cannot connect (a bad or revoked token) must not take it
// down with it.
type MultiAdapter struct {
	primary   string
	essential string
	byPrefix  map[string]Adapter
	log       *slog.Logger

	restartBase, restartMax time.Duration
	after                   func(time.Duration) <-chan time.Time
}

// NewMultiAdapter builds the mux. primary and essential must both be keys of
// byPrefix.
func NewMultiAdapter(primary, essential string, byPrefix map[string]Adapter, log *slog.Logger) (*MultiAdapter, error) {
	if _, ok := byPrefix[primary]; !ok {
		return nil, fmt.Errorf("multi adapter: primary backend %q is not configured", primary)
	}
	if _, ok := byPrefix[essential]; !ok {
		return nil, fmt.Errorf("multi adapter: essential backend %q is not configured", essential)
	}
	for name, a := range byPrefix {
		if a == nil {
			return nil, fmt.Errorf("multi adapter: backend %q is nil", name)
		}
	}
	if log == nil {
		log = slog.Default()
	}
	return &MultiAdapter{
		primary: primary, essential: essential, byPrefix: byPrefix, log: log,
		restartBase: muxRestartBase, restartMax: muxRestartMax, after: time.After,
	}, nil
}

// backendPrefix returns the backend name a conversation id carries, or "".
func backendPrefix(conversation string) string {
	prefix, _, ok := strings.Cut(conversation, ":")
	if !ok {
		return ""
	}
	return prefix
}

func (m *MultiAdapter) pick(conversation string) (Adapter, error) {
	prefix := backendPrefix(conversation)
	a, ok := m.byPrefix[prefix]
	if !ok {
		return nil, fmt.Errorf("multi adapter: no backend for conversation %q (prefix %q)", conversation, prefix)
	}
	return a, nil
}

// Run runs every backend until ctx is done or the essential backend stops.
// It returns nil on ctx, and otherwise the essential backend's error. A
// backend that returns nil while ctx is still live has stopped all the same,
// and is treated as a failure: an essential one that returned nil would
// otherwise leave the process running with its door shut.
func (m *MultiAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	essentialDone := make(chan error, 1)
	var wg sync.WaitGroup
	for name, a := range m.byPrefix {
		wg.Add(1)
		go func(name string, a Adapter) {
			defer wg.Done()
			if name == m.essential {
				essentialDone <- stopped(ctx, name, a.Run(ctx, handler))
				return
			}
			m.runContained(ctx, name, a, handler)
		}(name, a)
	}
	var err error
	select {
	case <-ctx.Done():
	case err = <-essentialDone:
	}
	cancel()
	wg.Wait()
	return err
}

// stopped names why a backend's Run returned: nil if ctx ended it, its own
// error otherwise, and an error of our own if it returned nil early.
func stopped(ctx context.Context, name string, err error) error {
	if ctx.Err() != nil {
		return nil
	}
	if err == nil {
		err = errors.New("stopped while running")
	}
	return fmt.Errorf("%s adapter: %w", name, err)
}

// runContained runs one non-essential backend until ctx is done, running it
// again after a backoff whenever it stops.
func (m *MultiAdapter) runContained(ctx context.Context, name string, a Adapter, handler func(InboundMessage)) {
	delay := m.restartBase
	for {
		start := time.Now()
		err := stopped(ctx, name, a.Run(ctx, handler))
		if err == nil {
			return
		}
		if time.Since(start) >= m.restartMax {
			delay = m.restartBase
		}
		m.log.Error("chat backend stopped; restarting it, the console stays up",
			"backend", name, "err", err, "retryIn", delay)
		select {
		case <-ctx.Done():
			return
		case <-m.after(delay):
		}
		delay = min(delay*2, m.restartMax)
	}
}

func (m *MultiAdapter) Post(conversation, text string) (string, error) {
	a, err := m.pick(conversation)
	if err != nil {
		return "", err
	}
	return a.Post(conversation, text)
}

func (m *MultiAdapter) Edit(conversation, messageID, text string) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	return a.Edit(conversation, messageID, text)
}

func (m *MultiAdapter) Roster(conversation string) ([]string, bool, error) {
	a, err := m.pick(conversation)
	if err != nil {
		return nil, false, err
	}
	return a.Roster(conversation)
}

func (m *MultiAdapter) OpenDirect(userID string) (string, error) {
	return m.byPrefix[m.primary].OpenDirect(userID)
}

// The gateway finds TaskObserver and SessionLookupSink by type assertion on
// the top of the adapter stack, and with a real chat backend the mux is that
// top (or the primary under the inject door, which asserts on it in turn).
// So the mux implements both unconditionally and passes each call on: the
// observer calls to the backend that owns the conversation's prefix, when it
// implements TaskObserver, and the session lookup to every backend that
// implements SessionLookupSink. A backend that implements neither is told
// nothing, as it would be alone. The Slack adapter is the one that needs
// both: without them it never learns which threads are sessions and drops
// every unmentioned reply.
func (m *MultiAdapter) observerFor(conversation string) (TaskObserver, bool) {
	a, err := m.pick(conversation)
	if err != nil {
		return nil, false
	}
	observer, ok := a.(TaskObserver)
	return observer, ok
}

func (m *MultiAdapter) TaskStarted(conversation, taskID string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.TaskStarted(conversation, taskID)
	}
}

func (m *MultiAdapter) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.TaskTerminal(conversation, taskID, state, source, reason)
	}
}

func (m *MultiAdapter) TaskAccepted(conversation, taskID string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.TaskAccepted(conversation, taskID)
	}
}

func (m *MultiAdapter) CancelPublished(conversation, taskID string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.CancelPublished(conversation, taskID)
	}
}

func (m *MultiAdapter) SetSessionLookup(lookup SessionLookup, idleTTL time.Duration) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(SessionLookupSink); ok {
			sink.SetSessionLookup(lookup, idleTTL)
		}
	}
}
