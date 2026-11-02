package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A home post the route refused for want of a home channel is kept rather
// than lost. The notifier writes it to the session-state bucket under its
// own key prefix (the registry lists "sessions.>" alone, so the reaper never
// sees these), counts it in a meta entry beside the posts, and posts one
// digest into the home channel when one arrives: set before this start
// (primeKept, from Start) or seen later in the projected home the route reads
// per request (validate, digestKept). The digest names the counts
// and the last subjects and never re-posts the texts, so a fresh channel is
// not flooded. The operator's view of the count (the HomeChannelUnset
// condition) is a later change, once the operator reads the home-channel
// record; nothing here writes that record.
const (
	// refusedKeyPrefix is the bucket prefix every kept post and its meta
	// entry sit under; the backend's name follows, then the entry's kind.
	refusedKeyPrefix   = "refused."
	refusedMetaKind    = "meta"
	refusedPostKind    = "post"
	refusedKeySep      = "."
	refusedPostKeyWide = 20 // unix nanoseconds, zero-padded, so keys sort by time
	// refusedKeepMax bounds the posts kept per backend, newest first;
	// refusedKeepTTL bounds their age. Both are pruned on the next keep.
	refusedKeepMax = 50
	refusedKeepTTL = 30 * hoursPerDay * time.Hour
	// refusedLastMax is how many subjects the meta entry carries, newest
	// first.
	refusedLastMax = 5
	// refusedSubjectMax bounds a subject: a post's first line, cut.
	refusedSubjectMax = 120
	// refusedStoreTimeout bounds one keep (a put, a list and the prunes) and
	// one digest's reads; both run off the route's delivery goroutine.
	refusedStoreTimeout = 10 * time.Second
	// The digest's lines. refusedDigestHead takes the count and the two
	// times; refusedDigestKept takes how many texts the bucket still holds
	// from this run of refusals and the retention in days; each subject is
	// one refusedDigestBullet.
	refusedDigestHead     = "While no home channel was set, %d proactive %s (alerts and scheduled reports) could not be delivered here, between %s and %s UTC."
	refusedDigestLast     = "The last %s:"
	refusedDigestBullet   = "- %s: %s"
	refusedDigestKept     = "The gateway kept the full text of %d of them for %d days."
	refusedDigestTimeFmt  = "2006-01-02 15:04"
	refusedDigestOneWord  = "post"
	refusedDigestManyWord = "posts"
	refusedDigestNewline  = "\n"
)

// refusedPost is one kept home post.
type refusedPost struct {
	At      time.Time       `json:"at"`
	Backend string          `json:"backend"`
	Subject string          `json:"subject"`
	Text    string          `json:"text"`
	Blocks  json.RawMessage `json:"blocks,omitempty"`
}

// refusedSubject is one line of the meta entry's tail: when, and the post's
// first line.
type refusedSubject struct {
	At      time.Time `json:"at"`
	Subject string    `json:"subject"`
}

// refusedMeta is the per-backend count since the last digest.
type refusedMeta struct {
	Count int              `json:"count"`
	Since time.Time        `json:"since,omitzero"`
	Last  time.Time        `json:"last,omitzero"`
	Tail  []refusedSubject `json:"tail,omitempty"`
}

// refusedStore keeps refused home posts for one backend.
type refusedStore interface {
	// Keep stores post, updates the meta entry and prunes, and returns the
	// meta as it stands.
	Keep(ctx context.Context, post refusedPost) (refusedMeta, error)
	// Meta is the count since the last digest, zero when there is none.
	Meta(ctx context.Context) (refusedMeta, error)
	// Clear takes digested refusals off the count and returns what stands:
	// a refusal kept while the digest was in flight. The posts stay until
	// pruned.
	Clear(ctx context.Context, digested int) (refusedMeta, error)
	// Kept is how many posts the store holds from since on.
	Kept(ctx context.Context, since time.Time) (int, error)
}

// kvRefusedStore is refusedStore over the session-state bucket.
type kvRefusedStore struct {
	client  *lib.Client
	backend string
	now     func() time.Time
}

// NewRefusedStore keeps one backend's refused home posts in the client's
// session-state bucket.
func NewRefusedStore(client *lib.Client, backend string) *kvRefusedStore {
	return &kvRefusedStore{client: client, backend: backend, now: time.Now}
}

func (s *kvRefusedStore) kv(ctx context.Context) (jetstream.KeyValue, error) {
	return s.client.KV(ctx, lib.SessionStateBucket)
}

func (s *kvRefusedStore) metaKey() string {
	return refusedKeyPrefix + s.backend + refusedKeySep + refusedMetaKind
}

func (s *kvRefusedStore) postPrefix() string {
	return refusedKeyPrefix + s.backend + refusedKeySep + refusedPostKind + refusedKeySep
}

func (s *kvRefusedStore) postKey(at time.Time) string {
	return s.postPrefix() + fmt.Sprintf("%0*d", refusedPostKeyWide, at.UnixNano())
}

// postKeyTime is the time a post key encodes, or the zero time.
func (s *kvRefusedStore) postKeyTime(key string) time.Time {
	n, err := strconv.ParseInt(strings.TrimPrefix(key, s.postPrefix()), 10, 64)
	if err != nil {
		return time.Time{}
	}
	return time.Unix(0, n)
}

func (s *kvRefusedStore) Keep(ctx context.Context, post refusedPost) (refusedMeta, error) {
	kv, err := s.kv(ctx)
	if err != nil {
		return refusedMeta{}, err
	}
	if post.At.IsZero() {
		post.At = s.now()
	}
	data, err := json.Marshal(post)
	if err != nil {
		return refusedMeta{}, err
	}
	if _, err := kv.Put(ctx, s.postKey(post.At), data); err != nil {
		return refusedMeta{}, fmt.Errorf("keeping the post: %w", err)
	}
	meta, err := s.readMeta(ctx, kv)
	if err != nil {
		return refusedMeta{}, err
	}
	meta = metaWith(meta, post)
	if err := s.writeMeta(ctx, kv, meta); err != nil {
		return refusedMeta{}, err
	}
	if err := s.prune(ctx, kv); err != nil {
		return meta, fmt.Errorf("pruning kept posts: %w", err)
	}
	return meta, nil
}

func (s *kvRefusedStore) Meta(ctx context.Context) (refusedMeta, error) {
	kv, err := s.kv(ctx)
	if err != nil {
		return refusedMeta{}, err
	}
	return s.readMeta(ctx, kv)
}

func (s *kvRefusedStore) Clear(ctx context.Context, digested int) (refusedMeta, error) {
	kv, err := s.kv(ctx)
	if err != nil {
		return refusedMeta{}, err
	}
	meta, err := s.readMeta(ctx, kv)
	if err != nil {
		return refusedMeta{}, err
	}
	left := metaLess(meta, digested)
	if err := s.writeMeta(ctx, kv, left); err != nil {
		return refusedMeta{}, err
	}
	return left, nil
}

// metaWith is meta with one more refusal counted. Keeps run one at a time
// but not in request order, so the span is the earliest and latest post seen
// and the tail is ordered by the post's time, newest first.
func metaWith(meta refusedMeta, post refusedPost) refusedMeta {
	meta.Count++
	if meta.Since.IsZero() || post.At.Before(meta.Since) {
		meta.Since = post.At
	}
	if post.At.After(meta.Last) {
		meta.Last = post.At
	}
	at := 0
	for at < len(meta.Tail) && meta.Tail[at].At.After(post.At) {
		at++
	}
	tail := append([]refusedSubject(nil), meta.Tail[:at]...)
	tail = append(tail, refusedSubject{At: post.At, Subject: post.Subject})
	tail = append(tail, meta.Tail[at:]...)
	if len(tail) > refusedLastMax {
		tail = tail[:refusedLastMax]
	}
	meta.Tail = tail
	return meta
}

// metaLess is meta with digested refusals taken off: the count drops, the
// tail keeps its newest entries, and the span starts at the oldest entry
// left in the tail. Empty when nothing stands. More refusals than the tail
// holds during one digest's flight leave the next span's start, and its
// kept-text count, later than the oldest of them: a bound accepted for a
// window of a few seconds, rather than a second timestamp in the meta.
func metaLess(meta refusedMeta, digested int) refusedMeta {
	left := meta.Count - digested
	if left <= 0 {
		return refusedMeta{}
	}
	tail := meta.Tail
	if len(tail) > left {
		tail = tail[:left]
	}
	since := meta.Since
	if len(tail) > 0 {
		since = tail[len(tail)-1].At
	}
	return refusedMeta{Count: left, Since: since, Last: meta.Last, Tail: tail}
}

func (s *kvRefusedStore) Kept(ctx context.Context, since time.Time) (int, error) {
	kv, err := s.kv(ctx)
	if err != nil {
		return 0, err
	}
	keys, err := s.postKeys(ctx, kv)
	if err != nil {
		return 0, err
	}
	kept := 0
	for _, key := range keys {
		if !s.postKeyTime(key).Before(since) {
			kept++
		}
	}
	return kept, nil
}

func (s *kvRefusedStore) readMeta(ctx context.Context, kv jetstream.KeyValue) (refusedMeta, error) {
	entry, err := kv.Get(ctx, s.metaKey())
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return refusedMeta{}, nil
	}
	if err != nil {
		return refusedMeta{}, fmt.Errorf("reading the refused meta: %w", err)
	}
	var meta refusedMeta
	if err := json.Unmarshal(entry.Value(), &meta); err != nil {
		return refusedMeta{}, fmt.Errorf("decoding the refused meta: %w", err)
	}
	return meta, nil
}

func (s *kvRefusedStore) writeMeta(ctx context.Context, kv jetstream.KeyValue, meta refusedMeta) error {
	data, err := json.Marshal(meta)
	if err != nil {
		return err
	}
	if _, err := kv.Put(ctx, s.metaKey(), data); err != nil {
		return fmt.Errorf("writing the refused meta: %w", err)
	}
	return nil
}

// postKeys lists the kept posts' keys, oldest first. The lister's channel
// closes silently when ctx ends, so the context is checked after the walk:
// a cut-off listing must not read as a complete one.
func (s *kvRefusedStore) postKeys(ctx context.Context, kv jetstream.KeyValue) ([]string, error) {
	lister, err := kv.ListKeysFiltered(ctx, s.postPrefix()+">")
	if err != nil {
		return nil, err
	}
	var keys []string
	for key := range lister.Keys() {
		keys = append(keys, key)
	}
	if err := ctx.Err(); err != nil {
		return nil, fmt.Errorf("listing kept posts: %w", err)
	}
	sort.Strings(keys)
	return keys, nil
}

// prune drops the posts past refusedKeepMax, oldest first, and any older
// than refusedKeepTTL.
func (s *kvRefusedStore) prune(ctx context.Context, kv jetstream.KeyValue) error {
	keys, err := s.postKeys(ctx, kv)
	if err != nil {
		return err
	}
	cutoff := s.now().Add(-refusedKeepTTL)
	excess := len(keys) - refusedKeepMax
	for i, key := range keys {
		if i >= excess && !s.postKeyTime(key).Before(cutoff) {
			continue
		}
		if err := kv.Purge(ctx, key); err != nil {
			return err
		}
	}
	return nil
}

// refusedDigest is the one message posted into a home channel that arrives
// after refusals: the count, the span, the last subjects, and how many of
// those texts the gateway still holds. "" when nothing was refused.
func refusedDigest(meta refusedMeta, kept int) string {
	if meta.Count == 0 {
		return ""
	}
	word := refusedDigestManyWord
	if meta.Count == 1 {
		word = refusedDigestOneWord
	}
	var b strings.Builder
	fmt.Fprintf(&b, refusedDigestHead, meta.Count, word,
		meta.Since.UTC().Format(refusedDigestTimeFmt), meta.Last.UTC().Format(refusedDigestTimeFmt))
	if len(meta.Tail) > 0 {
		lastWord := refusedDigestOneWord
		if len(meta.Tail) > 1 {
			lastWord = refusedDigestManyWord
		}
		b.WriteString(refusedDigestNewline + fmt.Sprintf(refusedDigestLast, lastWord))
		for _, s := range meta.Tail {
			b.WriteString(refusedDigestNewline + fmt.Sprintf(refusedDigestBullet, s.At.UTC().Format(refusedDigestTimeFmt), s.Subject))
		}
	}
	if kept > meta.Count {
		kept = meta.Count
	}
	if kept > 0 {
		b.WriteString(refusedDigestNewline + fmt.Sprintf(refusedDigestKept, kept, int(refusedKeepTTL.Hours())/hoursPerDay))
	}
	return b.String()
}

// SetKept arms keeping: refused home posts go to store. Called before Start.
func (n *Notifier) SetKept(store refusedStore) {
	n.kept = store
}

// Backend is the route's backend name, as lib's NotifyPlatform spells it.
func (n *Notifier) Backend() string {
	return n.backend
}

// keepRefused keeps a refused home post, off the route's delivery goroutine
// and one at a time, so the count is exact and a slow store never holds the
// subscription, and marks a digest owed. Best effort: the refusal already
// went back, and a keep that fails is logged.
func (n *Notifier) keepRefused(req lib.NotifyRequest) {
	if n.kept == nil {
		return
	}
	post := refusedPost{At: time.Now(), Backend: n.backend, Subject: firstLine(req.Text, refusedSubjectMax), Text: req.Text, Blocks: req.Blocks}
	n.keptWG.Add(1)
	go func() {
		defer n.keptWG.Done()
		n.keptMu.Lock()
		defer n.keptMu.Unlock()
		ctx, cancel := context.WithTimeout(context.Background(), refusedStoreTimeout)
		defer cancel()
		meta, err := n.kept.Keep(ctx, post)
		if err != nil {
			n.log.Error("notify refused and not kept", "backend", n.backend, "subject", post.Subject, "err", err)
			return
		}
		n.keptPending = true
		n.log.Warn("notify refused and kept: no home channel", "backend", n.backend, "subject", post.Subject, "refused", meta.Count)
	}()
}

// primeKept reads, once per process, the count an earlier process left: a
// non-zero count owes a digest to the next home channel this route sees,
// which may be the one it has now. A read that fails leaves the route
// unprimed, and the next home request tries again (digestKept), so a store
// unreachable at boot defers the digest rather than losing it. One read is
// in flight at a time: home requests arriving while it runs start no more.
func (n *Notifier) primeKept() {
	if n.kept == nil {
		return
	}
	n.keptMu.Lock()
	if n.keptPrimed || n.keptPriming {
		n.keptMu.Unlock()
		return
	}
	n.keptPriming = true
	n.keptMu.Unlock()
	n.keptWG.Add(1)
	go func() {
		defer n.keptWG.Done()
		ctx, cancel := context.WithTimeout(context.Background(), refusedStoreTimeout)
		meta, err := n.kept.Meta(ctx)
		cancel()
		n.keptMu.Lock()
		n.keptPriming = false
		if err != nil {
			n.keptMu.Unlock()
			n.log.Error("refused-post count not read at start; read again at the next home request", "backend", n.backend, "err", err)
			return
		}
		n.keptPrimed = true
		n.keptPending = n.keptPending || meta.Count > 0
		n.keptMu.Unlock()
		if home, _ := n.homeState(); home != "" {
			n.digestKept(home)
		}
	}()
}

// digestKept posts the digest of refused posts into home when one is owed,
// and clears the count when the post lands. Nothing is posted when keeping
// is off, when the count was never read (primeKept runs instead), when no
// digest is owed, or before Start.
func (n *Notifier) digestKept(home string) {
	if n.kept == nil || home == "" {
		return
	}
	n.keptMu.Lock()
	primed, pending := n.keptPrimed, n.keptPending
	n.keptMu.Unlock()
	if !primed {
		n.primeKept()
		return
	}
	if !pending {
		return
	}
	n.keptWG.Add(1)
	go func() {
		defer n.keptWG.Done()
		n.keptMu.Lock()
		defer n.keptMu.Unlock()
		if !n.keptPending {
			return // another request's digest took it
		}
		ctx, cancel := context.WithTimeout(context.Background(), refusedStoreTimeout)
		defer cancel()
		meta, err := n.kept.Meta(ctx)
		if err != nil {
			n.log.Error("refused-post digest not posted: the meta could not be read", "backend", n.backend, "err", err)
			return
		}
		if meta.Count == 0 {
			n.keptPending = false
			return
		}
		kept, err := n.kept.Kept(ctx, meta.Since)
		if err != nil {
			n.log.Warn("refused-post digest: the kept count could not be read", "backend", n.backend, "err", err)
		}
		text := refusedDigest(meta, kept)
		job := notifyJob{req: lib.NotifyRequest{Text: text}, home: home, answer: func(r lib.NotifyReply) {
			if r.Error != "" {
				n.log.Error("refused-post digest not posted; the count stands", "backend", n.backend, "refused", meta.Count, "err", r.Error)
				return
			}
			// Only what this digest named comes off the count: a refusal
			// kept while the post was in flight stays owed to the next one.
			n.keptMu.Lock()
			defer n.keptMu.Unlock()
			ctx, cancel := context.WithTimeout(context.Background(), refusedStoreTimeout)
			defer cancel()
			left, err := n.kept.Clear(ctx, meta.Count)
			if err != nil {
				n.log.Error("refused-post digest posted but the count was not cleared", "backend", n.backend, "err", err)
				return
			}
			n.keptPending = left.Count > 0
			n.log.Info("refused-post digest posted", "backend", n.backend, "refused", meta.Count, "home", home, "message", r.MessageID, "still_owed", left.Count)
		}}
		// Under mu, as handle enqueues: Stop closes jobs under it, so a send
		// here never meets a closed channel. keptPending stays set until the
		// post lands, and the next request skips while it is in the queue.
		n.mu.Lock()
		defer n.mu.Unlock()
		if n.stopping || n.jobs == nil {
			return
		}
		select {
		case n.jobs <- job:
			n.keptPending = false
		default:
			n.log.Error("refused-post digest not queued: the route's queue is full; the count stands", "backend", n.backend, "refused", meta.Count)
		}
	}()
}
