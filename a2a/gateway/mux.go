package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"
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
// chat backend that cannot connect (a bad token, a relay that is down) must
// not take it down with it.
type MultiAdapter struct {
	primary   string
	essential string
	byPrefix  map[string]Adapter
	log       *slog.Logger

	restartBase, restartMax time.Duration
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
		restartBase: muxRestartBase, restartMax: muxRestartMax,
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
		m.log.Error(fmt.Sprintf("%s backend stopped; the %s backend stays up and %s retries", name, m.essential, name),
			"err", err, "retryIn", delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
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
