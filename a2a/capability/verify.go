package capability

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

// ErrNotFound is what a Store returns when nothing lives at the pinned
// revision. It is the store's vocabulary, not the verifier's: the walk turns it
// into a refusal, because "the entry you pinned is gone" and "you may not have
// it" are the same answer to a caller.
var ErrNotFound = errors.New("capability: no entry at that revision")

// ErrRefused marks every denial this package produces. A store failure is
// deliberately NOT one: the verifier fails closed either way, but an operator
// reading the logs needs to tell "the bus is sick" from "somebody tried
// something".
var ErrRefused = errors.New("capability refused")

// Refusal names the rule that fired and nothing else. It never carries a key, a
// principal or a revision, and the tests enforce that: the refusal is answered
// to the party that provoked it, so echoing back a key it just refused to
// resolve turns the verifier into a confirmation oracle for ids.
type Refusal struct{ Rule string }

func (r *Refusal) Error() string { return "capability refused: " + r.Rule }
func (r *Refusal) Unwrap() error { return ErrRefused }

func refuse(format string, a ...any) error { return &Refusal{Rule: fmt.Sprintf(format, a...)} }

// Reason returns the rule that fired, or "" if err is not a refusal.
func Reason(err error) string {
	var r *Refusal
	if errors.As(err, &r) {
		return r.Rule
	}
	return ""
}

// Store is the read side of the `cap` bucket. 09 §4: no broker may hold one.
// Only the verification service does, and that means both `$KV.cap.>` and the
// JetStream API subjects that serve the bucket.
type Store interface {
	// GetRevision fetches the value at exactly this revision and never
	// falls back to the latest. The distinction is the whole of the
	// rewrite defence: at the bucket's provisioned history of 1 the two
	// agree, so an implementation that used a plain Get would pass every
	// test but the one written for this line, and would start serving
	// rewritten entries the day an operator raises history.
	GetRevision(ctx context.Context, key string, revision uint64) ([]byte, error)
}

// Resolver walks capability chains. One per verifier process; it holds no
// state between calls on purpose.
//
// Nothing here caches. 09 §5 spells out why: a per-id cache invalidated only by
// revoking that id serves a stale answer after an *ancestor* is deleted, which
// is exactly what revocation is, so the obvious cache breaks the property the
// design was chosen for. A correct cache has to be invalidated by any delete or
// overwrite anywhere in the chain, which means watching the bucket. Not a
// problem at three or four hops — leave it uncached until it measures.
type Resolver struct {
	Store Store

	// MaxDepth overrides the package bound. Zero means MaxDepth.
	MaxDepth int
}

// Resolve walks from ref to a root and returns the leaf entry, or the rule that
// refused it. caller is the authenticated principal, and on the bus that means
// the principal the *subject* named — a NATS message does not carry its
// publisher, so the verifier's request subject is what the server bound to an
// identity when the caller authenticated.
//
// The six checks of 09 §3, in the order they are cheapest to fail:
//
//  1. the entry names the caller as its delegate;
//  2. every reference is pinned and resolves at exactly its pinned revision;
//  3. the walk is bounded in depth and revisits nothing;
//  4. the chain terminates under `root.*`, and only there;
//  5. each link was written by the principal its parent named as delegate;
//  6. each link is no wider than its parent.
func (r *Resolver) Resolve(ctx context.Context, caller string, ref Ref) (Entry, error) {
	if caller == "" {
		return Entry{}, refuse("the caller is unauthenticated")
	}
	leaf, err := r.fetch(ctx, ref)
	if err != nil {
		return Entry{}, err
	}
	// Check 1, first because it is free and because it is the only thing
	// standing between an attacker and a root it merely named. Every other
	// check passes vacuously on a single-entry chain.
	if leaf.Delegate != caller {
		return Entry{}, refuse("the entry does not name the caller as its delegate")
	}

	max := r.MaxDepth
	if max <= 0 {
		max = MaxDepth
	}
	cur, curRef := leaf, ref
	visited := map[string]bool{ref.Key: true}

	for depth := 0; ; depth++ {
		if depth >= max {
			return Entry{}, refuse("the chain is deeper than the bound")
		}
		writer, isRoot, err := writerOf(curRef.Key)
		if err != nil {
			return Entry{}, err
		}
		if isRoot {
			if cur.Parent != nil {
				// Only the gateway can write here, so this is the
				// gateway misbehaving — but a walk that followed
				// the pointer would let a root be laundered under
				// a wider parent, so the shape is refused rather
				// than trusted.
				return Entry{}, refuse("a root carries a parent")
			}
			return leaf, nil
		}
		if cur.Parent == nil {
			return Entry{}, refuse("the chain does not terminate at a root the gateway minted")
		}
		if visited[cur.Parent.Key] {
			// Defence in depth rather than the load-bearing check —
			// see TestPinningMakesACycleUnconstructible for why a
			// cycle cannot be built while every reference is pinned,
			// and why this stays anyway.
			return Entry{}, refuse("the chain revisits a key")
		}
		parent, err := r.fetch(ctx, *cur.Parent)
		if err != nil {
			return Entry{}, err
		}
		if writer != parent.Delegate {
			return Entry{}, refuse("the entry was not written by the principal its parent named as delegate")
		}
		if err := Narrows(parent, cur); err != nil {
			return Entry{}, err
		}
		visited[cur.Parent.Key] = true
		cur, curRef = parent, *cur.Parent
	}
}

// Check is the question the verifier exists to answer: does the capability at
// ref, presented by caller, permit verb v on resource res.
func (r *Resolver) Check(ctx context.Context, caller string, ref Ref, v Verb, res Scope) error {
	leaf, err := r.Resolve(ctx, caller, ref)
	if err != nil {
		return err
	}
	return Permits(leaf, v, res)
}

func (r *Resolver) fetch(ctx context.Context, ref Ref) (Entry, error) {
	if ref.Key == "" {
		return Entry{}, refuse("the reference has no key")
	}
	if ref.Revision == 0 {
		// Zero is what an unpinned reference looks like after a round trip
		// through JSON, and it is the one value the KV API reads as
		// "latest". Passing it through would undo every pin at once.
		return Entry{}, refuse("the reference is not pinned to a revision")
	}
	b, err := r.Store.GetRevision(ctx, ref.Key, ref.Revision)
	switch {
	case errors.Is(err, ErrNotFound):
		return Entry{}, refuse("no entry at the pinned revision")
	case err != nil:
		return Entry{}, fmt.Errorf("capability store: %w", err)
	}
	var e Entry
	if err := json.Unmarshal(b, &e); err != nil {
		return Entry{}, refuse("the entry is not well-formed")
	}
	if err := e.Validate(); err != nil {
		return Entry{}, err
	}
	return e, nil
}
