package gateway

import (
	"context"
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"slices"
	"sync"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// peopleKeyPrefix starts every key in the people bucket (lib.PeopleBucket).
const peopleKeyPrefix = "people."

// peopleKeyInfo is the HKDF info string that binds the derived HMAC key to
// the people bucket, so a people key joins no other pseudonym the gateway
// derives from the same salt.
const peopleKeyInfo = "people"

// peopleKeyLen is how many bytes the derived HMAC key gets: one SHA-256
// block of output.
const peopleKeyLen = 32

// peopleKeyHexLen is how many hex characters of the HMAC a people key keeps
// after its prefix.
const peopleKeyHexLen = 32

// The people desk keeps one key per verified chat person in the people
// bucket, its value the RFC 3339 time of that person's latest noted turn, put
// at most once per peoplePutInterval. The bucket's max age expires a person
// nobody has seen since; the desk follows the bucket in memory (seen) so the
// distinct people of the last peopleWindowShort and peopleWindowLong can be
// counted without a read per key.
const (
	// peopleRetryInterval is how often the gateway looks again for a people
	// bucket it did not find at start, and peopleRetries how many times, as
	// for the feedback bucket (feedbackRetryInterval, feedbackRetries).
	peopleRetryInterval = time.Minute
	peopleRetries       = 30
	// peopleWatchRetryInterval is how long the desk waits before it watches
	// the bucket again after a watch ended or failed.
	peopleWatchRetryInterval = time.Minute
	// peoplePutInterval is the least time between two puts for one person.
	peoplePutInterval = time.Hour
	// peoplePutDeadline bounds one put.
	peoplePutDeadline = 10 * time.Second
	// peopleWindowShort and peopleWindowLong are the windows count reports;
	// an entry older than the long one is dropped from the map.
	peopleWindowShort = 7 * 24 * time.Hour
	peopleWindowLong  = 30 * 24 * time.Hour

	peopleOffSwitchLog     = "people count is off: turned off on this install (telemetry.peopleSeen)"
	peopleOffNoBucketLog   = "people count is off: no people bucket"
	peopleOffNoStatusLog   = "people count is off: the people bucket's settings could not be read"
	peopleOffNoMaxAgeLog   = "people count is off: the people bucket has no max age, so a key would never expire"
	peopleOffGaveUpLog     = "people count is off: still no people bucket the gateway can read; no longer looking"
	peopleOnLog            = "people count is on"
	peoplePutFailedLog     = "people: put failed; the count under-counts until a put succeeds"
	peopleWatchFailedLog   = "people: watch failed; the count is off until it is rebuilt"
	peopleWatchClosedLog   = "people: watch closed; the count is off until it is rebuilt"
	peopleBadValueLog      = "people: skipping a key whose value is not an RFC 3339 time"
	peoplePurgedLog        = "people: purged"
	peoplePurgeNoBucketLog = "people: nothing to purge: no people bucket"
	peoplePurgeFailedLog   = "people: purge failed"
	peopleDeleteFailedLog  = "people: purge could not delete a key"
)

// peopleBackends are the chat backends whose verified senders are people.
// Named here, not derived from a metrics label set: the doors and the
// console are programs or operators, not people.
var peopleBackends = []string{gchatBackend, slackBackend, discordBackend}

// peopleDesk notes verified chat people in the people bucket and follows the
// bucket in memory for count.
type peopleDesk struct {
	g     *Gateway
	keyer *peopleKeyer
	ttl   time.Duration
	now   func() time.Time

	mu sync.Mutex
	// loaded reports that seen holds the bucket: the watch has delivered
	// every value already there and has not ended since.
	loaded bool
	// seen is each key's latest time, from the bucket or from a put in
	// flight.
	seen map[string]time.Time
	// unparsable holds the keys whose value was skipped and logged, so each
	// is logged once.
	unparsable map[string]bool
}

// peopleKeyer derives the people bucket's key for a principal: an HMAC-SHA-256
// under a key that HKDF derives from the salt for this bucket alone.
type peopleKeyer struct {
	key []byte
}

// newPeopleKeyer derives the people bucket's HMAC key from salt.
func newPeopleKeyer(salt []byte) (*peopleKeyer, error) {
	key, err := hkdf.Key(sha256.New, salt, nil, peopleKeyInfo, peopleKeyLen)
	if err != nil {
		return nil, err
	}
	return &peopleKeyer{key: key}, nil
}

// Key returns the people bucket's key for principal, "people." plus
// peopleKeyHexLen hex characters, or "" for an empty principal.
func (k *peopleKeyer) Key(principal string) string {
	if principal == "" {
		return ""
	}
	mac := hmac.New(sha256.New, k.key)
	mac.Write([]byte(principal))
	return peopleKeyPrefix + hex.EncodeToString(mac.Sum(nil))[:peopleKeyHexLen]
}

// startPeople purges the people bucket when the count is turned off, and
// otherwise turns it on (tryPeople), or, when it cannot yet, tries again
// every peopleRetryInterval until it can, ctx ends or peopleRetries tries
// have failed.
func (g *Gateway) startPeople(ctx context.Context) {
	if g.cfg.PeopleSeenOff {
		g.log.Info(peopleOffSwitchLog)
		go g.purgePeople(ctx)
		return
	}
	if g.tryPeople(ctx, false) {
		return
	}
	go func() {
		ticker := time.NewTicker(peopleRetryInterval)
		defer ticker.Stop()
		for range peopleRetries {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if g.tryPeople(ctx, true) {
					return
				}
			}
		}
		g.log.Info(peopleOffGaveUpLog, "bucket", lib.PeopleBucket)
	}()
}

// tryPeople finds the people bucket and, when it has a max age, turns the
// count on: the desk is stored and starts following the bucket. A missing
// bucket, or one that would keep a key forever, leaves it off. A retry
// (quiet) logs why only at debug.
func (g *Gateway) tryPeople(ctx context.Context, quiet bool) bool {
	off := func(why string, args ...any) bool {
		args = append([]any{"bucket", lib.PeopleBucket}, args...)
		if quiet {
			g.log.Debug(why, args...)
		} else {
			g.log.Info(why, args...)
		}
		return false
	}
	kv, err := g.client.KV(ctx, lib.PeopleBucket)
	if err != nil {
		return off(peopleOffNoBucketLog, "err", err)
	}
	status, err := kv.Status(ctx)
	if err != nil {
		return off(peopleOffNoStatusLog, "err", err)
	}
	if status.TTL() <= 0 {
		return off(peopleOffNoMaxAgeLog)
	}
	desk := &peopleDesk{
		g:          g,
		keyer:      g.peopleKeyer,
		ttl:        status.TTL(),
		now:        time.Now,
		seen:       map[string]time.Time{},
		unparsable: map[string]bool{},
	}
	g.people.Store(desk)
	go desk.watch(ctx)
	g.log.Info(peopleOnLog, "bucket", lib.PeopleBucket, "keptFor", status.TTL().String())
	return true
}

// watch follows the bucket into seen until ctx ends: each time a watch ends,
// the map is unloaded and emptied, and a new watch starts after
// peopleWatchRetryInterval.
func (d *peopleDesk) watch(ctx context.Context) {
	timer := time.NewTimer(peopleWatchRetryInterval)
	defer timer.Stop()
	for {
		d.follow(ctx)
		d.reset()
		timer.Reset(peopleWatchRetryInterval)
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
	}
}

// follow runs one watch over the whole bucket, deletes and purges included,
// applying each entry to seen, and returns when the watch ends or ctx does.
func (d *peopleDesk) follow(ctx context.Context) {
	kv, err := d.g.client.KV(ctx, lib.PeopleBucket)
	if err != nil {
		if ctx.Err() == nil {
			d.g.log.Warn(peopleWatchFailedLog, "bucket", lib.PeopleBucket, "err", err)
		}
		return
	}
	watcher, err := kv.WatchAll(ctx)
	if err != nil {
		if ctx.Err() == nil {
			d.g.log.Warn(peopleWatchFailedLog, "bucket", lib.PeopleBucket, "err", err)
		}
		return
	}
	defer func() { _ = watcher.Stop() }()
	for {
		select {
		case <-ctx.Done():
			return
		case entry, ok := <-watcher.Updates():
			if !ok {
				d.g.log.Warn(peopleWatchClosedLog, "bucket", lib.PeopleBucket)
				return
			}
			d.apply(entry)
		}
	}
}

// apply folds one watch entry into seen; nil marks the end of the values
// already in the bucket.
func (d *peopleDesk) apply(entry jetstream.KeyValueEntry) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if entry == nil {
		d.loaded = true
		return
	}
	key := entry.Key()
	switch entry.Operation() {
	case jetstream.KeyValueDelete, jetstream.KeyValuePurge:
		delete(d.seen, key)
		delete(d.unparsable, key)
		return
	}
	at, err := time.Parse(time.RFC3339, string(entry.Value()))
	if err != nil {
		if !d.unparsable[key] {
			d.unparsable[key] = true
			d.g.log.Debug(peopleBadValueLog, "key", key, "err", err)
		}
		return
	}
	delete(d.unparsable, key)
	d.seen[key] = at
}

// reset unloads the map and empties it, for a watch that has ended: until a
// new watch has delivered the bucket again, seen does not hold it.
func (d *peopleDesk) reset() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.loaded = false
	clear(d.seen)
}

// note records a turn from principal: unless the person was noted within
// peoplePutInterval, the person's time is set to now and put to the bucket
// on a goroutine, so the turn never waits on the bucket.
func (d *peopleDesk) note(principal string) {
	key := d.keyer.Key(principal)
	if key == "" {
		return
	}
	d.mu.Lock()
	now := d.now()
	if now.Sub(d.seen[key]) < peoplePutInterval {
		d.mu.Unlock()
		return
	}
	// Set before the put, so a second turn in the same moment skips.
	d.seen[key] = now
	d.mu.Unlock()
	go d.put(key, now)
}

// put writes at as key's value, and on failure takes back note's entry if
// no later note has replaced it, so the next turn tries again.
func (d *peopleDesk) put(key string, at time.Time) {
	ctx, cancel := context.WithTimeout(d.g.runCtx, peoplePutDeadline)
	defer cancel()
	err := func() error {
		kv, err := d.g.client.KV(ctx, lib.PeopleBucket)
		if err != nil {
			return err
		}
		_, err = kv.PutString(ctx, key, at.UTC().Format(time.RFC3339))
		return err
	}()
	if err == nil {
		return
	}
	d.g.log.Warn(peoplePutFailedLog, "err", err)
	d.mu.Lock()
	defer d.mu.Unlock()
	if held, ok := d.seen[key]; ok && held.Equal(at) {
		delete(d.seen, key)
	}
}

// count reports the distinct people seen within peopleWindowShort and
// peopleWindowLong of now, and whether the map holds the bucket (ok). An
// entry older than peopleWindowLong is dropped.
func (d *peopleDesk) count(now time.Time) (short, long int, ok bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for key, at := range d.seen {
		age := now.Sub(at)
		if age > peopleWindowLong {
			delete(d.seen, key)
			continue
		}
		long++
		if age <= peopleWindowShort {
			short++
		}
	}
	return short, long, d.loaded
}

// purgePeople deletes every key in the people bucket, once, for an install
// that turned the count off.
func (g *Gateway) purgePeople(ctx context.Context) {
	kv, err := g.client.KV(ctx, lib.PeopleBucket)
	if errors.Is(err, jetstream.ErrBucketNotFound) {
		g.log.Info(peoplePurgeNoBucketLog, "bucket", lib.PeopleBucket)
		return
	}
	if err != nil {
		g.log.Warn(peoplePurgeFailedLog, "bucket", lib.PeopleBucket, "err", err)
		return
	}
	lister, err := kv.ListKeys(ctx)
	if err != nil {
		g.log.Warn(peoplePurgeFailedLog, "bucket", lib.PeopleBucket, "err", err)
		return
	}
	n := 0
	for key := range lister.Keys() {
		if err := kv.Delete(ctx, key); err != nil {
			g.log.Warn(peopleDeleteFailedLog, "bucket", lib.PeopleBucket, "err", err)
			continue
		}
		n++
	}
	g.log.Info(peoplePurgedLog, "bucket", lib.PeopleBucket, "keys", n)
}

// peopleNote notes a verified turn's person when it came from a chat backend
// and the count is on.
func (g *Gateway) peopleNote(backend, principal string) {
	if !slices.Contains(peopleBackends, backend) {
		return
	}
	desk := g.people.Load()
	if desk == nil {
		return
	}
	desk.note(principal)
}
