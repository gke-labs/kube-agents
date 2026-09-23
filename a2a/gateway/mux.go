package gateway

import (
	"context"
	"fmt"
	"strings"
	"sync"
)

// MultiAdapter presents several backends to the gateway as one Adapter.
// Conversation ids are backend-qualified by convention (discord:…, gchat:…,
// console:…), so every per-conversation operation dispatches on the prefix.
// OpenDirect takes a user id with no prefix and goes to the primary backend,
// the one the process was configured for.
type MultiAdapter struct {
	primary  string
	byPrefix map[string]Adapter
}

// NewMultiAdapter builds the mux. primary must be a key of byPrefix.
func NewMultiAdapter(primary string, byPrefix map[string]Adapter) (*MultiAdapter, error) {
	if _, ok := byPrefix[primary]; !ok {
		return nil, fmt.Errorf("multi adapter: primary backend %q is not configured", primary)
	}
	for name, a := range byPrefix {
		if a == nil {
			return nil, fmt.Errorf("multi adapter: backend %q is nil", name)
		}
	}
	return &MultiAdapter{primary: primary, byPrefix: byPrefix}, nil
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

// Run runs every backend. The first to return an error ends the others via
// ctx and that error is returned: a gateway with one dead backend restarts
// rather than running half-deaf.
func (m *MultiAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	errs := make(chan error, len(m.byPrefix))
	var wg sync.WaitGroup
	for name, a := range m.byPrefix {
		wg.Add(1)
		go func(name string, a Adapter) {
			defer wg.Done()
			if err := a.Run(ctx, handler); err != nil {
				errs <- fmt.Errorf("%s adapter: %w", name, err)
				return
			}
			errs <- nil
		}(name, a)
	}
	var first error
	for range m.byPrefix {
		if err := <-errs; err != nil && first == nil {
			first = err
			cancel()
		}
	}
	wg.Wait()
	return first
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
