package capability

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// Bucket is the KV bucket the provision Job creates (`nats kv add cap
// --history=1`). It was reserved for this and has been empty since.
const Bucket = "cap"

// Stream and SubjectPrefix are how the bucket is actually spelled on the wire.
// A KV key does not live at its bare name: bucket `cap` is stream `KV_cap` on
// `$KV.cap.>`. Every permission and every denial test has to be written in this
// space — written against the bare key names they match no subject any KV
// operation touches, so they grant and deny nothing, and a denial test written
// that way passes on a server where no `$KV` permission is configured at all.
const (
	Stream        = "KV_" + Bucket
	SubjectPrefix = "$KV." + Bucket + "."
)

// Subject is the wire subject a key lives at.
func Subject(key string) string { return SubjectPrefix + key }

// JetStream API subjects the verifier needs, and the only ones. A get by
// revision is stream.GetMsg, which rides DIRECT.GET on a direct-enabled stream
// and STREAM.MSG.GET otherwise; binding the bucket reads STREAM.INFO.
//
// Deliberately absent: subscribe on `$KV.cap.>`. 09 §4 names that subject
// alongside the API ones, and it is right to name it — as something no broker
// may hold. The verifier does not need it either: it resolves on demand and
// never watches, and a subscribe there would give the one component that can
// read every capability a live feed of every capability being minted.
var VerifierAPISubjects = []string{
	"$JS.API.STREAM.INFO." + Stream,
	"$JS.API.DIRECT.GET." + Stream,
	"$JS.API.STREAM.MSG.GET." + Stream,
}

// kvStore is the verifier's read side.
type kvStore struct{ kv jetstream.KeyValue }

// NewStore binds the bucket for reading. Only the verification service calls
// this; a broker that did would be refused by the server at the first get.
func NewStore(ctx context.Context, js jetstream.JetStream) (Store, error) {
	kv, err := js.KeyValue(ctx, Bucket)
	if err != nil {
		return nil, fmt.Errorf("bind %s bucket: %w", Bucket, err)
	}
	return kvStore{kv: kv}, nil
}

func (s kvStore) GetRevision(ctx context.Context, key string, revision uint64) ([]byte, error) {
	e, err := s.kv.GetRevision(ctx, key, revision)
	switch {
	case errors.Is(err, jetstream.ErrKeyNotFound), errors.Is(err, jetstream.ErrKeyDeleted):
		// GetRevision fetches by stream sequence and then checks the
		// message's subject matches the key, so a pin that resolves to
		// some other key's write lands here rather than returning the
		// wrong entry.
		return nil, ErrNotFound
	case err != nil:
		return nil, err
	}
	return e.Value(), nil
}

// Minter is the write side, and it is deliberately not a jetstream.KeyValue.
//
// A KV put is a core publish to the key's own subject with an expected-last-
// sequence header; the revision comes back on the publish ack. Binding the
// bucket the library way would additionally require `$JS.API.STREAM.INFO.KV_cap`,
// which is a read, and 09 §4's rule is that no broker may read the bucket at
// all. Publishing directly means the gateway and every broker can be granted
// exactly one subject — their own namespace — and nothing else.
type Minter struct {
	js jetstream.JetStream
}

func NewMinter(js jetstream.JetStream) *Minter { return &Minter{js: js} }

// Write creates an entry and returns the pinned reference. The expected-last-
// sequence-per-subject of 0 makes this a create rather than an update: a
// second write to the same key is refused by the server.
//
// That refusal is a nicety, not the control. A subject permission cannot
// express "create but do not update", so a caller that wanted to overwrite
// could publish without the header. What actually protects a resolved entry is
// that every reference pins the revision the write returned.
func (m *Minter) Write(ctx context.Context, key string, e Entry) (Ref, error) {
	if err := e.Validate(); err != nil {
		return Ref{}, err
	}
	if _, _, err := writerOf(key); err != nil {
		return Ref{}, err
	}
	b, err := json.Marshal(e)
	if err != nil {
		return Ref{}, fmt.Errorf("marshal capability entry: %w", err)
	}
	ack, err := m.js.PublishMsg(ctx,
		&nats.Msg{Subject: Subject(key), Data: b},
		jetstream.WithExpectLastSequencePerSubject(0))
	if err != nil {
		return Ref{}, fmt.Errorf("write capability entry: %w", err)
	}
	return Ref{Key: key, Revision: ack.Sequence}, nil
}

// Mint is the gateway's call: a root at key `root.<request-id>` — subject
// `$KV.cap.root.<request-id>`, the gateway's whole grant — naming the principal
// the gateway is about to dispatch to. The gateway allocates the request id and
// the principal's name in the same breath, which is what makes the delegate
// predictable before the credential exists.
//
// The entry is NOT derived from the requester. An earlier version of this
// comment said it was ("from the requester's own authority"), and that sentence
// described a product this one is not: mintCapability (a2a/gateway/authority.go)
// fills Tier and Scope from g.cfg, which is install-wide configuration, and
// varies only Delegate — by route, not by who asked. The requester appears in
// the envelope's `authority` block, which is attribution and is advisory; it
// reaches no check. Nothing in this tree intersects a capability with a
// per-requester ceiling, because no per-requester ceiling exists to intersect
// with, and a reader who takes this comment at its word will believe the
// opposite. The bound an executor is held to is its install's shared one.
func (m *Minter) Mint(ctx context.Context, requestID string, e Entry) (Ref, error) {
	key, err := RootKey(requestID)
	if err != nil {
		return Ref{}, err
	}
	if e.Parent != nil {
		return Ref{}, refuse("a root carries a parent")
	}
	return m.Write(ctx, key, e)
}

// Attenuate is a broker's call: a child in its OWN namespace, narrower than the
// parent it descends from, naming the next principal. It refuses to write
// something wider before the server ever sees it — the verifier catches a
// widening either way, but a hop that widens by accident should find out at the
// write rather than have its work refused a hop later.
func (m *Minter) Attenuate(ctx context.Context, self string, n int, parent Entry, parentRef Ref, child Entry) (Ref, error) {
	key, err := HopKey(self, n)
	if err != nil {
		return Ref{}, err
	}
	if parent.Delegate != self {
		return Ref{}, refuse("the parent does not name this principal as its delegate")
	}
	if parentRef.Revision == 0 {
		return Ref{}, refuse("the reference is not pinned to a revision")
	}
	child.Parent = &parentRef
	if err := Narrows(parent, child); err != nil {
		return Ref{}, err
	}
	return m.Write(ctx, key, child)
}
