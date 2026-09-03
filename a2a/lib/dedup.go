package lib

import (
	"sync"
	"time"
)

// dedupWindow is the target size of a subscription's envelopeId memory, and
// dedupMinRetention the floor below which entries are never evicted: a
// JetStream redelivery arrives within the consumer's AckWait (30s default),
// so evicting an id younger than twice that would let the duplicate through
// (assertion 5). Under burst load the set grows past the target rather than
// give up the at-most-once property.
const (
	dedupWindow       = 4096
	dedupMinRetention = 60 * time.Second
)

// dedupSet is a FIFO set of envelopeIds bounded by capacity, except that
// entries younger than the retention floor are kept regardless.
type dedupSet struct {
	mu     sync.Mutex
	seen   map[string]struct{}
	order  []dedupEntry
	cap    int
	minAge time.Duration
}

type dedupEntry struct {
	id string
	at time.Time
}

func newDedupSet(capacity int) *dedupSet {
	return newDedupSetWithRetention(capacity, dedupMinRetention)
}

func newDedupSetWithRetention(capacity int, minAge time.Duration) *dedupSet {
	return &dedupSet{seen: make(map[string]struct{}, capacity), cap: capacity, minAge: minAge}
}

// add records id and reports whether it was new.
func (d *dedupSet) add(id string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, dup := d.seen[id]; dup {
		return false
	}
	now := time.Now()
	d.seen[id] = struct{}{}
	d.order = append(d.order, dedupEntry{id: id, at: now})
	for len(d.order) > d.cap && now.Sub(d.order[0].at) >= d.minAge {
		delete(d.seen, d.order[0].id)
		d.order = d.order[1:]
	}
	return true
}
