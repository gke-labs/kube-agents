package lib

import (
	"fmt"
	"sync"
	"sync/atomic"

	"github.com/nats-io/nats.go"
)

// coreSub is a plain core NATS subscription the client keeps across
// rebuilds, the way it keeps a durable. nats.go restores core subscriptions
// itself across an ordinary reconnect; what it cannot restore is a
// subscription bound to a connection that closed for good, which is the
// case rebuild exists for. Without this a request-reply listener (the
// gateway's chat.notify handler) would stay up, TCP green, and never hear
// another request after the first terminal close.
type coreSub struct {
	c       *Client
	subject string
	handler nats.MsgHandler
	stopped atomic.Bool

	mu  sync.Mutex
	sub *nats.Subscription
}

// SubscribeCore subscribes handler to subject on a core NATS subscription
// (no JetStream, no queue group) that survives connection rebuilds.
func (c *Client) SubscribeCore(subject string, handler nats.MsgHandler) (Subscription, error) {
	if subject == "" {
		return nil, fmt.Errorf("SubscribeCore: subject is required")
	}
	s := &coreSub{c: c, subject: subject, handler: handler}
	// Register before starting, for the same race SubscribeDurableAttributed
	// names: a rebuild snapshotting c.cores without it would leave it bound
	// to a dead connection.
	c.mu.Lock()
	c.cores = append(c.cores, s)
	nc := c.nc
	c.mu.Unlock()
	if err := s.start(nc); err != nil {
		s.Stop()
		return nil, err
	}
	return s, nil
}

// start subscribes on nc and flushes, so the subscription is registered
// with the server before the caller goes on to rely on it.
func (s *coreSub) start(nc *nats.Conn) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopped.Load() {
		return nil
	}
	sub, err := nc.Subscribe(s.subject, s.handler)
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", s.subject, err)
	}
	if err := nc.Flush(); err != nil {
		_ = sub.Unsubscribe()
		return fmt.Errorf("subscribe %s: flush: %w", s.subject, err)
	}
	s.sub = sub
	return nil
}

// resubscribeCore re-binds every core subscription on nc. A core subscribe
// has no server-side state to wait for, so unlike resubscribe there is no
// retry loop: a failure here is the new connection failing, which the
// caller answers by dialing again.
func (c *Client) resubscribeCore(cores []*coreSub, nc *nats.Conn) bool {
	for _, s := range cores {
		if err := s.start(nc); err != nil {
			c.log.Error("nats rebuild core re-subscribe failed", "subject", s.subject, "err", err)
			return false
		}
		c.log.Info("nats rebuild re-subscribed", "subject", s.subject)
	}
	return !nc.IsClosed()
}

// Stop unsubscribes and drops the subscription from the rebuild set.
func (s *coreSub) Stop() {
	s.stopped.Store(true)
	s.mu.Lock()
	if s.sub != nil {
		_ = s.sub.Unsubscribe()
	}
	s.mu.Unlock()
	s.c.mu.Lock()
	defer s.c.mu.Unlock()
	for i, sub := range s.c.cores {
		if sub == s {
			s.c.cores = append(s.c.cores[:i], s.c.cores[i+1:]...)
			break
		}
	}
}
