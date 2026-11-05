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
// digest.
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
// peopleRetryInterval is how often the gateway looks again for a people
// bucket it did not find at start, and peopleRetries how many times, as for
// the feedback bucket (feedbackRetryInterval, feedbackRetries). The off
// switch's purge (purgePeople) retries a failed attempt on the same schedule.
// Each Gateway carries its own copy (peopleRetry), which a test shortens on
// its own gateway.
const peopleRetryInterval = time.Minute

const (
	peopleRetries = 30
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
	peoplePurgeFailedLog   = "people: a purge attempt failed"
	peoplePurgeGaveUpLog   = "people: purge failed; no longer trying"
)

// The people desk's log attribute keys. No log line carries a people key: a
// key is a person's pseudonym.
const (
	peopleLogBucket     = "bucket"
	peopleLogKeptFor    = "keptFor"
	peopleLogKeys       = "keys"
	peopleLogErr        = "err"
	peopleLogUnparsable = "unparsable"
	peopleLogRemaining  = "remaining"
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
	now   func() time.Time
	// openWatch starts one watch over the whole bucket (watchBucket), and
	// watchRetry is how long watch waits to start another after one ends;
	// tests replace both.
	openWatch  func(ctx context.Context) (jetstream.KeyWatcher, error)
	watchRetry time.Duration

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

// startPeople makes the first try to turn the people count on (tryPeople),
// and reports whether that settled it: on, or turned off on this install
// (which purges the bucket). Run calls it before the relay binds, and calls
// retryPeople after the bind when it did not.
func (g *Gateway) startPeople(ctx context.Context) bool {
	if g.cfg.PeopleSeenOff {
		g.log.Info(peopleOffSwitchLog)
		go g.purgePeople(ctx)
		return true
	}
	return g.tryPeople(ctx, false)
}

// retryPeople tries again every g.peopleRetry until the count is on,
// ctx ends or peopleRetries tries have failed. Run starts it once the relay
// has bound rather than before: the provision Job creates the bucket with
// TASKS, and a relay that waited for TASKS longer than the retries last
// would otherwise come up with the count given up on for good.
func (g *Gateway) retryPeople(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(g.peopleRetry)
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
		g.log.Info(peopleOffGaveUpLog, peopleLogBucket, lib.PeopleBucket)
	}()
}

// tryPeople finds the people bucket and, when it has a max age, turns the
// count on: the desk is stored and starts following the bucket. A missing
// bucket, or one that would keep a key forever, leaves it off. A retry
// (quiet) logs why only at debug.
func (g *Gateway) tryPeople(ctx context.Context, quiet bool) bool {
	off := func(why string, args ...any) bool {
		args = append([]any{peopleLogBucket, lib.PeopleBucket}, args...)
		if quiet {
			g.log.Debug(why, args...)
		} else {
			g.log.Info(why, args...)
		}
		return false
	}
	kv, err := g.client.KV(ctx, lib.PeopleBucket)
	if err != nil {
		return off(peopleOffNoBucketLog, peopleLogErr, err)
	}
	status, err := kv.Status(ctx)
	if err != nil {
		return off(peopleOffNoStatusLog, peopleLogErr, err)
	}
	if status.TTL() <= 0 {
		return off(peopleOffNoMaxAgeLog)
	}
	desk := &peopleDesk{
		g:          g,
		keyer:      g.peopleKeyer,
		now:        time.Now,
		watchRetry: peopleWatchRetryInterval,
		seen:       map[string]time.Time{},
		unparsable: map[string]bool{},
	}
	desk.openWatch = desk.watchBucket
	g.people.Store(desk)
	g.metrics.setPeopleSource(desk.count)
	go desk.watch(ctx)
	g.log.Info(peopleOnLog, peopleLogBucket, lib.PeopleBucket, peopleLogKeptFor, status.TTL().String())
	return true
}

// watch follows the bucket into seen until ctx ends: each time a watch ends,
// the map is unloaded and emptied, and a new watch starts after
// watchRetry.
func (d *peopleDesk) watch(ctx context.Context) {
	timer := time.NewTimer(d.watchRetry)
	defer timer.Stop()
	for {
		d.follow(ctx)
		d.reset()
		timer.Reset(d.watchRetry)
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
	watcher, err := d.openWatch(ctx)
	if err != nil {
		if ctx.Err() == nil {
			d.g.log.Warn(peopleWatchFailedLog, peopleLogBucket, lib.PeopleBucket, peopleLogErr, err)
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
				d.g.log.Warn(peopleWatchClosedLog, peopleLogBucket, lib.PeopleBucket)
				return
			}
			d.apply(entry)
		}
	}
}

// watchBucket binds the people bucket and watches all of it.
func (d *peopleDesk) watchBucket(ctx context.Context) (jetstream.KeyWatcher, error) {
	kv, err := d.g.client.KV(ctx, lib.PeopleBucket)
	if err != nil {
		return nil, err
	}
	return kv.WatchAll(ctx)
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
			d.g.log.Debug(peopleBadValueLog, peopleLogErr, err, peopleLogUnparsable, len(d.unparsable))
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
	prior := d.seen[key]
	if now.Sub(prior) < peoplePutInterval {
		d.mu.Unlock()
		return
	}
	// Set before the put, so a second turn in the same moment skips.
	d.seen[key] = now
	d.mu.Unlock()
	go d.put(key, now, prior)
}

// put writes at as key's value. On failure, unless a later note or the
// watch has replaced note's entry, it puts back prior, the time the map held
// before (the zero time: none, and the entry is deleted), so a person
// already counted stays counted and the next turn tries again.
func (d *peopleDesk) put(key string, at, prior time.Time) {
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
	d.g.log.Warn(peoplePutFailedLog, peopleLogErr, err)
	d.mu.Lock()
	defer d.mu.Unlock()
	if held, ok := d.seen[key]; !ok || !held.Equal(at) {
		return
	}
	if prior.IsZero() {
		delete(d.seen, key)
		return
	}
	d.seen[key] = prior
}

// count reports the distinct people seen within peopleWindowShort and
// peopleWindowLong of the desk's clock, and whether the map holds the bucket
// (ok). An entry older than peopleWindowLong is dropped.
func (d *peopleDesk) count() (short, long int, ok bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	now := d.now()
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

// purgePeople deletes every key in the people bucket, for an install that
// turned the count off. A failed attempt (the bind, the list, or a delete)
// is tried again after peoplePurgeRetry, at most peopleRetries times, each
// attempt listing and deleting what remains. A missing bucket has nothing
// to purge: a bucket created after this start holds no keys, since an off
// gateway puts none.
func (g *Gateway) purgePeople(ctx context.Context) {
	deleted := 0
	for try := 0; ; try++ {
		n, remaining, err := g.purgePeopleOnce(ctx)
		deleted += n
		if errors.Is(err, jetstream.ErrBucketNotFound) {
			g.log.Info(peoplePurgeNoBucketLog, peopleLogBucket, lib.PeopleBucket)
			return
		}
		if err == nil {
			g.log.Info(peoplePurgedLog, peopleLogBucket, lib.PeopleBucket, peopleLogKeys, deleted)
			return
		}
		args := []any{peopleLogBucket, lib.PeopleBucket, peopleLogErr, err}
		if remaining >= 0 {
			args = append(args, peopleLogRemaining, remaining)
		}
		g.log.Warn(peoplePurgeFailedLog, args...)
		if try == peopleRetries {
			g.log.Info(peoplePurgeGaveUpLog, peopleLogBucket, lib.PeopleBucket, peopleLogKeys, deleted)
			return
		}
		timer := time.NewTimer(g.peoplePurgeRetry)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

// purgePeopleOnce makes one purge attempt: it lists the bucket's keys and
// deletes each. It returns how many it deleted, how many of those it listed
// it did not (-1 when the bind or the list failed), and the first error.
func (g *Gateway) purgePeopleOnce(ctx context.Context) (deleted, remaining int, err error) {
	kv, err := g.client.KV(ctx, lib.PeopleBucket)
	if err != nil {
		return 0, -1, err
	}
	keys, err := g.peoplePurgeList(ctx, kv)
	if err != nil {
		return 0, -1, err
	}
	var first error
	for _, key := range keys {
		if err := kv.Delete(ctx, key); err != nil {
			if first == nil {
				first = err
			}
			continue
		}
		deleted++
	}
	return deleted, len(keys) - deleted, first
}

// listPeopleKeys lists every key in kv. A list cut short by ctx is an
// error, not a short list.
func listPeopleKeys(ctx context.Context, kv jetstream.KeyValue) ([]string, error) {
	lister, err := kv.ListKeys(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = lister.Stop() }()
	var keys []string
	for key := range lister.Keys() {
		keys = append(keys, key)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return keys, nil
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
