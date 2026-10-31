package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// directoryStream holds the profile cards (a2a.agents.<profile>), one
	// per subject; the operator writes them.
	directoryStream = "DIRECTORY"
	// directoryRetryInterval spaces retries of the start-up read. A gateway
	// that can't read DIRECTORY yet (the stream not provisioned, or an
	// operator that hasn't granted the read) keeps running with no profiles,
	// so a task addressed to one is refused as unknown until a read lands.
	directoryRetryInterval = 30 * time.Second
	// directoryResyncInterval spaces the re-reads after the first. The live
	// subscription is core NATS, so a card or tombstone published while the
	// connection was down never arrives; a re-read bounds how long the
	// gateway routes on a stale card to this.
	directoryResyncInterval = 5 * time.Minute
)

// agentsWildcard is every profile's card subject: profile names are one
// token (a DNS label).
var agentsWildcard = lib.AgentSubject("*")

// directoryFeed keeps a profileDirectory current: a live subscription to
// a2a.agents.* for changes, and one read of the stream's newest card per
// subject for what was there before it subscribed. A profile the live feed
// has spoken for since the subscription began is not overwritten by the
// read, which may have fetched an older card.
type directoryFeed struct {
	dir *profileDirectory
	log interface {
		Warn(msg string, args ...any)
		Info(msg string, args ...any)
	}
	mu   sync.Mutex
	live map[string]bool
}

// apply records one directory message, from either source. It reports
// whether the message was a card or tombstone it understood.
func (f *directoryFeed) apply(subject string, raw []byte, fromLive bool) bool {
	profile, ok := strings.CutPrefix(subject, lib.AgentSubject(""))
	if !ok || profile == "" || strings.Contains(profile, ".") {
		return false
	}
	env, err := lib.ParseEnvelope(raw)
	if err != nil {
		f.log.Warn("directory message unreadable", "subject", subject, "err", err)
		return false
	}
	if err := lib.CheckSubjectAgreement(subject, env, lib.AgreementPolicy{}); err != nil {
		f.log.Warn("directory message refused", "subject", subject, "err", err)
		return false
	}
	var card *lib.AgentCard
	if env.Kind != lib.KindAgentClosed {
		card = &lib.AgentCard{}
		if err := json.Unmarshal(env.Payload, card); err != nil || card.Name != profile {
			f.log.Warn("profile card refused", "subject", subject, "name", card.Name, "err", err)
			return false
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	// The live feed speaks for a profile only with a card or tombstone it
	// applied: a refused one leaves the read free to fill it in.
	if !fromLive && f.live[profile] {
		return true
	}
	if fromLive {
		f.live[profile] = true
	}
	f.dir.put(profile, card)
	return true
}

// startProfileDirectory subscribes to the cards and reads what DIRECTORY
// already holds, retrying the read until it lands or ctx ends. The
// subscription comes first, so a card published between the two is seen.
func (g *Gateway) startProfileDirectory(ctx context.Context) {
	f := &directoryFeed{dir: g.profiles, log: g.log, live: map[string]bool{}}
	sub, err := g.client.SubscribeCore(agentsWildcard, func(m *nats.Msg) { f.apply(m.Subject, m.Data, true) })
	if err != nil {
		g.log.Warn("profile directory subscription failed; profiles are unaddressable", "err", err)
		return
	}
	go func() {
		defer sub.Stop()
		for {
			wait := directoryResyncInterval
			if err := g.readDirectory(ctx, f); err != nil {
				g.log.Warn("profile directory read failed; retrying", "err", err, "in", directoryRetryInterval)
				wait = directoryRetryInterval
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}
	}()
}

// readDirectory reads the newest card on each subject DIRECTORY holds: the
// stream's subject list, then a direct get per subject, so no consumer is
// created on the stream.
func (g *Gateway) readDirectory(ctx context.Context, f *directoryFeed) error {
	stream, err := g.client.JetStream().Stream(ctx, directoryStream)
	if err != nil {
		return err
	}
	info, err := stream.Info(ctx, jetstream.WithSubjectFilter(agentsWildcard))
	if err != nil {
		return err
	}
	// A read speaks for every profile the live feed hasn't since it began;
	// one the read no longer finds is gone (its tombstone was missed).
	f.beginRead()
	n := 0
	seen := map[string]bool{}
	for subject := range info.State.Subjects {
		if profile, ok := strings.CutPrefix(subject, lib.AgentSubject("")); ok {
			seen[profile] = true
		}
		msg, err := stream.GetLastMsgForSubject(ctx, subject)
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			continue
		}
		if err != nil {
			return err
		}
		if f.apply(subject, msg.Data, false) {
			n++
		}
	}
	f.endRead(seen)
	g.log.Info("profile directory read", "cards", n)
	return nil
}

// beginRead starts a read: the live feed's claims so far are settled, so
// the read may speak for every profile again until the feed claims it.
func (f *directoryFeed) beginRead() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.live = map[string]bool{}
}

// endRead forgets every profile the read didn't find on DIRECTORY, unless
// the live feed spoke for it during the read.
func (f *directoryFeed) endRead(seen map[string]bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, name := range f.dir.names() {
		if !seen[name] && !f.live[name] {
			f.dir.put(name, nil)
		}
	}
}
