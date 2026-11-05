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
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

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

	// a2aEnvelopeIDPrefix starts every envelope id, as lib's constructors
	// spell it; the id is also the JetStream dedup key.
	a2aEnvelopeIDPrefix = "env-"

	// a2aEnvelopeIDBytes is the random part of an envelope id, in bytes.
	a2aEnvelopeIDBytes = 12

	// cardBusTimeout bounds one directory read or publish.
	cardBusTimeout = 10 * time.Second
	// cardBundleReadTimeout bounds the read of the bus CA bundle each TLS
	// handshake makes (busTLS).
	cardBundleReadTimeout = 5 * time.Second

	// operatorBusAuthFileEnvVar overrides where the manager reads its own
	// projected bus token; the default is the path every bus client uses.
	operatorBusAuthFileEnvVar = "A2A_BUS_TOKEN_FILE"

	// natsStatusHeader and natsStatusNotFound are how a direct get says
	// "no message on that subject", and natsDescriptionHeader carries the
	// reason for any other status.
	natsStatusHeader      = "Status"
	natsStatusNotFound    = "404"
	natsDescriptionHeader = "Description"

	// jsDirectGetPrefix is the JetStream API's direct-get-by-subject request:
	// $JS.API.DIRECT.GET.<stream>.<subject>.
	jsDirectGetPrefix = "$JS.API.DIRECT.GET."
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

// a2aAgentCard is the card payload: the profile's name, its routing blurb, and
// what the gateway routes on (contract on #3655, addendum 1): the tiers it may
// run at, the cluster it is pinned to if any, and the task parameters it
// declares. The JSON names are lib.AgentCard's; this module can't import that
// one, so the card fixture the a2a contract test decodes holds them equal.
type a2aAgentCard struct {
	Name        string             `json:"name"`
	Description string             `json:"description"`
	Tiers       []string           `json:"tiers,omitempty"`
	ClusterRef  *a2aCardClusterRef `json:"clusterRef,omitempty"`
	Parameters  []a2aCardParameter `json:"parameters,omitempty"`
}

// a2aCardClusterRef is lib.ClusterRef.
type a2aCardClusterRef struct {
	ProjectID string `json:"projectId"`
	Location  string `json:"location"`
	Cluster   string `json:"cluster"`
}

// a2aCardParameter is lib.ProfileParameter.
type a2aCardParameter struct {
	Name     string `json:"name"`
	Required bool   `json:"required,omitempty"`
}

// agentCardSubject is the profile's directory subject.
func agentCardSubject(profile string) string {
	return a2aDirectorySubjectPrefix + profile
}

// desiredAgentCard is the card the profile's spec renders.
func desiredAgentCard(p *agentv1alpha1.AgentProfile) a2aAgentCard {
	card := a2aAgentCard{Name: p.Name, Description: p.Spec.Description, Tiers: p.AllowedTierList()}
	if ref := p.Spec.ClusterRef; ref != nil {
		card.ClusterRef = &a2aCardClusterRef{ProjectID: ref.ProjectID, Location: ref.Location, Cluster: ref.Cluster}
	}
	for _, param := range p.Spec.Parameters {
		card.Parameters = append(card.Parameters, a2aCardParameter{Name: param.Name, Required: param.Required})
	}
	return card
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
		EnvelopeID:    a2aEnvelopeIDPrefix + id,
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
	return d.present && d.kind == a2aKindAgentCard && reflect.DeepEqual(d.card, want)
}

// cardPublisher is the operator's view of the directory, behind an interface so
// the reconciler's tests need no bus.
type cardPublisher interface {
	// Read returns the profile's directory entry.
	Read(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string) (directoryEntry, error)
	// Publish writes a card (card != nil) or a tombstone.
	Publish(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string, card *a2aAgentCard) error
	// SyncDurables makes the profile's shared durables exactly want, and
	// deletes the rest this operator made (agentprofile_durables.go).
	SyncDurables(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string, want []profileDurable) error
}

// natsCardPublisher holds one bus connection per PlatformAgent, opened on first
// use and kept. The connection authenticates with the manager's projected bus
// token, re-read on every (re)connect because the kubelet rotates it, and pins
// the inbox prefix the callout grants the operator.
type natsCardPublisher struct {
	mu    sync.Mutex
	conns map[string]*nats.Conn
	// reader fetches the bus's CA bundle, which the operator trusts the bus
	// by like every other client: the bus requires TLS.
	reader client.Reader
}

func newNATSCardPublisher(reader client.Reader) *natsCardPublisher {
	return &natsCardPublisher{conns: map[string]*nats.Conn{}, reader: reader}
}

// busTLS is the operator's trust in this agent's bus: the CA bundle the
// operator itself renders, read on every TLS handshake rather than once. A
// connection here is never closed (MaxReconnects(-1)), so a pool fixed at
// connect would keep trusting a CA the reconcile has since replaced, and every
// reconnect after the bus restarted on the new one would fail.
func (n *natsCardPublisher) busTLS(agent *agentv1alpha1.PlatformAgent) nats.Option {
	key := types.NamespacedName{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace}
	return nats.ClientTLSConfig(nil, func() (*x509.CertPool, error) {
		ctx, cancel := context.WithTimeout(context.Background(), cardBundleReadTimeout)
		defer cancel()
		bundle := &corev1.ConfigMap{}
		if err := n.reader.Get(ctx, key, bundle); err != nil {
			return nil, fmt.Errorf("reading the bus CA bundle: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM([]byte(bundle.Data[a2aNATSCACertKey])) {
			return nil, fmt.Errorf("the bus CA bundle %s holds no certificate", bundle.Name)
		}
		return pool, nil
	})
}

func operatorBusAuthFile() string {
	if p := os.Getenv(operatorBusAuthFileEnvVar); p != "" {
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
	tokenFile := operatorBusAuthFile()
	nc, err := nats.Connect(url, n.busTLS(agent),
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
	msg, err := nc.RequestWithContext(ctx, jsDirectGetPrefix+a2aDirectoryStream+"."+agentCardSubject(profile), nil)
	if err != nil {
		return directoryEntry{}, fmt.Errorf("reading %s: %w", agentCardSubject(profile), err)
	}
	if msg.Header.Get(natsStatusHeader) == natsStatusNotFound {
		return directoryEntry{}, nil
	}
	if status := msg.Header.Get(natsStatusHeader); status != "" {
		return directoryEntry{}, fmt.Errorf("reading %s: status %s %s", agentCardSubject(profile), status, msg.Header.Get(natsDescriptionHeader))
	}
	return parseDirectoryEntry(msg.Data)
}

// parseDirectoryEntry reads one directory message. An entry it cannot decode
// is an answer about the content, not about the bus: it is present and it is
// not this profile's card, so the caller overwrites it (with the card, or with
// a tombstone on deletion) instead of waiting on an outage that is not there.
func parseDirectoryEntry(data []byte) (directoryEntry, error) {
	var env a2aCardEnvelope
	if err := json.Unmarshal(data, &env); err != nil {
		return directoryEntry{present: true}, nil
	}
	entry := directoryEntry{present: true, kind: env.Kind}
	if env.Kind == a2aKindAgentCard {
		if err := json.Unmarshal(env.Payload, &entry.card); err != nil {
			return directoryEntry{present: true}, nil
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
	ack, err := js.Publish(ctx, agentCardSubject(profile), body, jetstream.WithMsgID(a2aEnvelopeIDPrefix+id))
	if err != nil {
		return fmt.Errorf("publishing to %s: %w", agentCardSubject(profile), err)
	}
	if ack.Stream != a2aDirectoryStream {
		return errors.New("the directory publish was stored in stream " + strconv.Quote(ack.Stream) + ", not " + a2aDirectoryStream)
	}
	return nil
}
