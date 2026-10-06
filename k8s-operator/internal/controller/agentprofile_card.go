/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// An AgentProfile's agent card on the directory, and the operator's bus
// connection that publishes it. "Profile" is the A2A AgentProfile resource.
//
// The envelope is written here rather than with a2a/lib's constructors because
// the operator cannot import that module. What keeps the two from drifting is
// the fixture: TestTheAgentCardFixtureIsWhatTheOperatorRenders writes the bytes
// this file produces into a2a/authcallout/testdata, and the a2a side parses
// them with lib.ParseEnvelope and checks them against the directory subject's
// agreement rules.

const (
	// a2aEnvelopeProtocol is lib.Protocol, the one version an emitter speaks.
	a2aEnvelopeProtocol = "a2a-jetstream/0.4"

	// a2aKindAgentCard and a2aKindAgentClosed are the directory's two kinds:
	// the card, and the tombstone that replaces it.
	a2aKindAgentCard   = "agent-card"
	a2aKindAgentClosed = "agent-closed"

	// a2aCardSession is the from.session the operator's directory envelopes
	// carry. The directory's agreement rule binds from.profile, not the
	// session, so this names who spoke rather than what for.
	a2aCardSession = "operator"

	// a2aEnvelopeIDBytes is the random part of an envelope id, in bytes.
	a2aEnvelopeIDBytes = 12

	// cardBusTimeout bounds one directory read or publish.
	cardBusTimeout = 10 * time.Second

	// operatorBusTokenPathEnvVar overrides where the manager reads its own
	// projected bus token; the default is the path every bus client uses.
	operatorBusTokenPathEnvVar = "A2A_BUS_TOKEN_FILE"

	// natsStatusHeader and natsStatusNotFound are how a direct get says
	// "no message on that subject".
	natsStatusHeader   = "Status"
	natsStatusNotFound = "404"
)

// a2aCardEnvelope is the envelope's wire shape, field for field with
// lib.Envelope for the fields a directory envelope uses.
type a2aCardEnvelope struct {
	Protocol      string          `json:"protocol"`
	EnvelopeID    string          `json:"envelopeId"`
	CorrelationID string          `json:"correlationId"`
	TS            time.Time       `json:"ts"`
	From          a2aCardParty    `json:"from"`
	Identity      json.RawMessage `json:"identity"`
	Authority     json.RawMessage `json:"authority"`
	Kind          string          `json:"kind"`
	Payload       json.RawMessage `json:"payload"`
}

type a2aCardParty struct {
	Session string `json:"session"`
	Profile string `json:"profile,omitempty"`
}

// a2aAgentCard is the card payload: the profile's name and its routing blurb.
type a2aAgentCard struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

// agentCardSubject is the profile's directory subject.
func agentCardSubject(profile string) string {
	return a2aDirectorySubjectPrefix + profile
}

// desiredAgentCard is the card the profile's spec renders.
func desiredAgentCard(p *agentv1alpha1.AgentProfile) a2aAgentCard {
	return a2aAgentCard{Name: p.Name, Description: p.Spec.Description}
}

// renderDirectoryEnvelope builds a card (card != nil) or a tombstone for one
// profile. now and id are parameters so the fixture test can pin the bytes.
func renderDirectoryEnvelope(profile string, card *a2aAgentCard, now time.Time, id string) ([]byte, error) {
	kind, payload := a2aKindAgentClosed, json.RawMessage(`{}`)
	if card != nil {
		raw, err := json.Marshal(card)
		if err != nil {
			return nil, err
		}
		kind, payload = a2aKindAgentCard, raw
	}
	return json.Marshal(a2aCardEnvelope{
		Protocol:      a2aEnvelopeProtocol,
		EnvelopeID:    "env-" + id,
		CorrelationID: "agentprofile-" + profile,
		TS:            now.UTC(),
		From:          a2aCardParty{Session: a2aCardSession, Profile: profile},
		Identity:      json.RawMessage(`null`),
		Authority:     json.RawMessage(`null`),
		Kind:          kind,
		Payload:       payload,
	})
}

func newEnvelopeID() (string, error) {
	b := make([]byte, a2aEnvelopeIDBytes)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// directoryEntry is what the directory holds for one profile: nothing, a card,
// or a tombstone.
type directoryEntry struct {
	present bool
	kind    string
	card    a2aAgentCard
}

// current reports whether the entry is exactly the card the profile renders.
func (d directoryEntry) current(want a2aAgentCard) bool {
	return d.present && d.kind == a2aKindAgentCard && d.card == want
}

// cardPublisher is the operator's view of the directory, behind an interface so
// the reconciler's tests need no bus.
type cardPublisher interface {
	// Read returns the profile's directory entry.
	Read(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string) (directoryEntry, error)
	// Publish writes a card (card != nil) or a tombstone.
	Publish(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string, card *a2aAgentCard) error
}

// natsCardPublisher holds one bus connection per PlatformAgent, opened on first
// use and kept. The connection authenticates with the manager's projected bus
// token, re-read on every (re)connect because the kubelet rotates it, and pins
// the inbox prefix the callout grants the operator.
type natsCardPublisher struct {
	mu    sync.Mutex
	conns map[string]*nats.Conn
}

func newNATSCardPublisher() *natsCardPublisher {
	return &natsCardPublisher{conns: map[string]*nats.Conn{}}
}

func operatorBusTokenFile() string {
	if p := os.Getenv(operatorBusTokenPathEnvVar); p != "" {
		return p
	}
	return filepath.Join(a2aBusTokenPath, a2aBusTokenFile)
}

func (n *natsCardPublisher) conn(agent *agentv1alpha1.PlatformAgent) (*nats.Conn, error) {
	key := agent.Namespace + "/" + agent.Name
	url := a2aNATSClientURL(agent)
	n.mu.Lock()
	defer n.mu.Unlock()
	if nc, ok := n.conns[key]; ok {
		// A connection that is reconnecting is still the right one: nats.go
		// retries forever (MaxReconnects(-1)) and re-reads the token each
		// time. Only a closed one is replaced.
		if !nc.IsClosed() {
			return nc, nil
		}
		delete(n.conns, key)
	}
	tokenFile := operatorBusTokenFile()
	nc, err := nats.Connect(url,
		nats.Name("kubeagents-operator"),
		nats.TokenHandler(func() string {
			b, err := os.ReadFile(tokenFile) // #nosec G304 -- the manager's own projected token path
			if err != nil {
				return ""
			}
			return string(b)
		}),
		nats.CustomInboxPrefix("_INBOX."+a2aOperatorBusUser),
		nats.MaxReconnects(-1),
	)
	if err != nil {
		return nil, fmt.Errorf("connecting to the bus at %s as the operator: %w", url, err)
	}
	n.conns[key] = nc
	return nc, nil
}

// forget closes and drops every connection held for an agent in namespace.
func (n *natsCardPublisher) forget(namespace string) {
	n.mu.Lock()
	defer n.mu.Unlock()
	prefix := namespace + "/"
	for key, nc := range n.conns {
		if strings.HasPrefix(key, prefix) {
			nc.Close()
			delete(n.conns, key)
		}
	}
}

// Read fetches the profile's last directory message with a direct get by
// subject: the one read the operator's grant allows, and it needs no stream
// handle (STREAM.INFO), which the operator does not hold.
func (n *natsCardPublisher) Read(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string) (directoryEntry, error) {
	nc, err := n.conn(agent)
	if err != nil {
		return directoryEntry{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, cardBusTimeout)
	defer cancel()
	msg, err := nc.RequestWithContext(ctx, "$JS.API.DIRECT.GET."+a2aDirectoryStream+"."+agentCardSubject(profile), nil)
	if err != nil {
		return directoryEntry{}, fmt.Errorf("reading %s: %w", agentCardSubject(profile), err)
	}
	if msg.Header.Get(natsStatusHeader) == natsStatusNotFound {
		return directoryEntry{}, nil
	}
	if status := msg.Header.Get(natsStatusHeader); status != "" {
		return directoryEntry{}, fmt.Errorf("reading %s: status %s %s", agentCardSubject(profile), status, msg.Header.Get("Description"))
	}
	return parseDirectoryEntry(msg.Data)
}

func parseDirectoryEntry(data []byte) (directoryEntry, error) {
	var env a2aCardEnvelope
	if err := json.Unmarshal(data, &env); err != nil {
		return directoryEntry{}, fmt.Errorf("decoding the directory entry: %w", err)
	}
	entry := directoryEntry{present: true, kind: env.Kind}
	if env.Kind == a2aKindAgentCard {
		if err := json.Unmarshal(env.Payload, &entry.card); err != nil {
			return directoryEntry{}, fmt.Errorf("decoding the card payload: %w", err)
		}
	}
	return entry, nil
}

// Publish writes through JetStream with the envelope id as the dedup key, as
// every other emitter does, and waits for the ack.
func (n *natsCardPublisher) Publish(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string, card *a2aAgentCard) error {
	nc, err := n.conn(agent)
	if err != nil {
		return err
	}
	id, err := newEnvelopeID()
	if err != nil {
		return err
	}
	body, err := renderDirectoryEnvelope(profile, card, time.Now(), id)
	if err != nil {
		return err
	}
	js, err := jetstream.New(nc)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, cardBusTimeout)
	defer cancel()
	ack, err := js.Publish(ctx, agentCardSubject(profile), body, jetstream.WithMsgID("env-"+id))
	if err != nil {
		return fmt.Errorf("publishing to %s: %w", agentCardSubject(profile), err)
	}
	if ack.Stream != a2aDirectoryStream {
		return errors.New("the directory publish was stored in stream " + strconv.Quote(ack.Stream) + ", not " + a2aDirectoryStream)
	}
	return nil
}
