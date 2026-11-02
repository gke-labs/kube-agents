package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func TestPeopleKeyIsDerivedForTheBucketAlone(t *testing.T) {
	salt := []byte("a fixed salt for the test")
	k, err := newPeopleKeyer(salt)
	if err != nil {
		t.Fatal(err)
	}
	key := k.Key("user@example.com")
	if !strings.HasPrefix(key, peopleKeyPrefix) || len(key) != len(peopleKeyPrefix)+peopleKeyHexLen {
		t.Fatalf("key %q is not people.<32 hex>", key)
	}
	if key != k.Key("user@example.com") {
		t.Fatal("the key is not stable")
	}
	if strings.TrimPrefix(key, peopleKeyPrefix) == strings.TrimPrefix(NewPseudonymizer(salt).Hash("user@example.com"), "hmac:") {
		t.Fatal("the people key equals the authority block's pseudonym; it must join nothing")
	}
	if k.Key("other@example.com") == key {
		t.Fatal("two principals share a key")
	}
	if k.Key("") != "" {
		t.Fatal("an empty principal must yield no key")
	}
}

// peopleTestTTL is the max age the tests give the people bucket.
const peopleTestTTL = 30 * 24 * time.Hour

// peopleTestPrincipal is what the rig's sender "1001" resolves to.
const peopleTestPrincipal = "test:bnaylor"

// peopleBucketJS connects to the rig's server for a test's own KV calls.
func peopleBucketJS(t *testing.T, url string) jetstream.JetStream {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	return js
}

// createPeopleBucket creates the people bucket from cfg, and returns it.
func createPeopleBucket(t *testing.T, url string, cfg jetstream.KeyValueConfig) jetstream.KeyValue {
	t.Helper()
	cfg.Bucket = lib.PeopleBucket
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	kv, err := peopleBucketJS(t, url).CreateKeyValue(ctx, cfg)
	if err != nil {
		t.Fatalf("create people: %v", err)
	}
	return kv
}

// withPeopleBucket is a rig tweak that provisions the people bucket before
// the gateway runs, with maxAge as its retention (zero: none).
func withPeopleBucket(t *testing.T, maxAge time.Duration) func(*Config) {
	return func(cfg *Config) {
		createPeopleBucket(t, cfg.NATSURL, jetstream.KeyValueConfig{History: 1, TTL: maxAge})
	}
}

// startPeopleRig is startRigWith with the gateway's log captured in r.logs
// and written nowhere else.
func startPeopleRig(t *testing.T, tweak func(*Config)) *rig {
	t.Helper()
	logs := &lockedBuffer{}
	r := startRigWithLogger(t, tweak, slog.New(slog.NewTextHandler(logs, nil)))
	r.logs = logs
	return r
}

// peopleTestClock pins a peopleDesk's clock to a value the test moves by
// hand.
type peopleTestClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *peopleTestClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *peopleTestClock) advance(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
	return c.t
}

// fixPeopleClock waits for the rig's people desk and pins its clock.
func fixPeopleClock(t *testing.T, r *rig) *peopleTestClock {
	t.Helper()
	waitFor(t, "people on", func() bool { return r.g.people.Load() != nil })
	clock := &peopleTestClock{t: time.Date(2026, 11, 2, 12, 0, 0, 0, time.UTC)}
	desk := r.g.people.Load()
	desk.mu.Lock()
	desk.now = clock.now
	desk.mu.Unlock()
	return clock
}

// waitPeopleLoaded waits until the desk has read the bucket's initial values.
func waitPeopleLoaded(t *testing.T, r *rig) *peopleDesk {
	t.Helper()
	waitFor(t, "the people map to load", func() bool {
		d := r.g.people.Load()
		if d == nil {
			return false
		}
		_, _, ok := d.count()
		return ok
	})
	return r.g.people.Load()
}

// seenAt is the desk's in-memory time for key, if it has one.
func (d *peopleDesk) seenAt(key string) (time.Time, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	at, ok := d.seen[key]
	return at, ok
}

func (d *peopleDesk) seenLen() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.seen)
}

// peopleKeys lists the bucket's live keys.
func peopleKeys(t *testing.T, kv jetstream.KeyValue) []string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	lister, err := kv.ListKeys(ctx)
	if err != nil {
		t.Fatalf("list keys: %v", err)
	}
	var keys []string
	for k := range lister.Keys() {
		keys = append(keys, k)
	}
	return keys
}

// peopleKV binds the rig's people bucket.
func peopleKV(t *testing.T, r *rig) jetstream.KeyValue {
	t.Helper()
	kv, err := r.client.KV(context.Background(), lib.PeopleBucket)
	if err != nil {
		t.Fatalf("bind people: %v", err)
	}
	return kv
}

// testPeopleKey is the people key the rig's salt gives principal.
func testPeopleKey(t *testing.T, principal string) string {
	t.Helper()
	k, err := newPeopleKeyer([]byte("test-salt"))
	if err != nil {
		t.Fatal(err)
	}
	return k.Key(principal)
}

// peopleTurn sends one turn from the rig's verified sender in conv.
func peopleTurn(r *rig, conv, messageID string) {
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: messageID, Text: "why is it crashing?"}
}

// awaitSubmissions waits until n tasks have been submitted to platform.
func awaitSubmissions(t *testing.T, r *rig, n int) {
	t.Helper()
	waitFor(t, fmt.Sprintf("%d submissions", n), func() bool {
		got := 0
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindMessage {
				got++
			}
		}
		return got >= n
	})
}

func TestNoPeopleBucketCountsNobody(t *testing.T) {
	r := startPeopleRig(t, nil) // provision() creates no people bucket
	waitFor(t, "no bucket log", func() bool {
		return strings.Contains(r.logs.String(), "people count is off: no people bucket")
	})
	peopleTurn(r, "discord:g1/pn1", "d-1")
	awaitSubmissions(t, r, 1)
	if r.g.people.Load() != nil {
		t.Fatal("a desk exists without a bucket")
	}
}

func TestAPeopleBucketWithoutAMaxAgeIsLeftAlone(t *testing.T) {
	r := startPeopleRig(t, withPeopleBucket(t, 0))
	waitFor(t, "no max age log", func() bool {
		return strings.Contains(r.logs.String(), "the people bucket has no max age")
	})
	if r.g.people.Load() != nil {
		t.Fatal("a desk exists on a bucket without a max age")
	}
}

func TestAChatTurnWritesItsPersonOnceAnHour(t *testing.T) {
	r := startPeopleRig(t, withPeopleBucket(t, peopleTestTTL))
	clock := fixPeopleClock(t, r)
	waitPeopleLoaded(t, r)
	if !strings.Contains(r.logs.String(), "people count is on") {
		t.Errorf("log lacks the on line:\n%s", r.logs.String())
	}
	kv := peopleKV(t, r)
	key := r.g.people.Load().keyer.Key(peopleTestPrincipal)
	if key != testPeopleKey(t, peopleTestPrincipal) {
		t.Fatalf("desk key %q is not the keyer's for the test salt", key)
	}
	get := func() jetstream.KeyValueEntry {
		entry, err := kv.Get(context.Background(), key)
		if err != nil {
			return nil
		}
		return entry
	}

	peopleTurn(r, "discord:g1/ph1", "d-1")
	first := clock.now().UTC().Format(time.RFC3339)
	waitFor(t, "the first put", func() bool { return get() != nil })
	if e := get(); e.Revision() != 1 || string(e.Value()) != first {
		t.Fatalf("after turn 1: revision %d value %q, want 1 %q", e.Revision(), e.Value(), first)
	}

	peopleTurn(r, "discord:g1/ph2", "d-2")
	awaitSubmissions(t, r, 2)

	later := clock.advance(61 * time.Minute).UTC().Format(time.RFC3339)
	peopleTurn(r, "discord:g1/ph3", "d-3")
	waitFor(t, "the second put", func() bool { e := get(); return e != nil && e.Revision() > 1 })
	// A put for turn 2 would have made turn 3's revision 3.
	if e := get(); e.Revision() != 2 || string(e.Value()) != later {
		t.Fatalf("after turn 3: revision %d value %q, want 2 %q", e.Revision(), e.Value(), later)
	}
	if keys := peopleKeys(t, kv); len(keys) != 1 {
		t.Fatalf("keys = %v, want one per person", keys)
	}
}

func TestTheHourlyBoundSurvivesARestart(t *testing.T) {
	key := testPeopleKey(t, peopleTestPrincipal)
	before := time.Now().Add(-10 * time.Minute).UTC().Truncate(time.Second)
	r := startPeopleRig(t, func(c *Config) {
		kv := createPeopleBucket(t, c.NATSURL, jetstream.KeyValueConfig{History: 1, TTL: peopleTestTTL})
		if _, err := kv.PutString(context.Background(), key, before.Format(time.RFC3339)); err != nil {
			t.Fatalf("seed put: %v", err)
		}
	})
	desk := waitPeopleLoaded(t, r)
	if at, ok := desk.seenAt(key); !ok || !at.Equal(before) {
		t.Fatalf("seen[key] = %v, %v; want the seeded %v", at, ok, before)
	}
	peopleTurn(r, "discord:g1/pr1", "d-1")
	awaitSubmissions(t, r, 1)
	// note runs before the turn is routed, so a put would already show here.
	if at, _ := desk.seenAt(key); !at.Equal(before) {
		t.Fatalf("seen[key] = %v after the turn, want the seeded %v kept", at, before)
	}
	entry, err := peopleKV(t, r).Get(context.Background(), key)
	if err != nil {
		t.Fatal(err)
	}
	if entry.Revision() != 1 {
		t.Fatalf("revision = %d, want 1 (no second put inside the hour)", entry.Revision())
	}
}

func TestDoorAndConsoleTurnsAreNotPeople(t *testing.T) {
	t.Run("inject door", func(t *testing.T) {
		r := startInjectRigWith(t, withPeopleBucket(t, peopleTestTTL))
		waitFor(t, "people on", func() bool { return r.g.people.Load() != nil })
		reply := r.inject(t, "people-door", injectTestAuthor, "how is the fleet?")
		if !reply.Accepted || reply.TaskID == "" {
			t.Fatalf("the door turn was not accepted: %+v", reply)
		}
		if n := r.g.people.Load().seenLen(); n != 0 {
			t.Fatalf("the desk noted %d people from a door turn", n)
		}
		kv, err := r.g.client.KV(context.Background(), lib.PeopleBucket)
		if err != nil {
			t.Fatal(err)
		}
		if keys := peopleKeys(t, kv); len(keys) != 0 {
			t.Fatalf("keys = %v, want none from a door turn", keys)
		}
	})
	t.Run("console", func(t *testing.T) {
		r := startPeopleRig(t, withPeopleBucket(t, peopleTestTTL))
		waitFor(t, "people on", func() bool { return r.g.people.Load() != nil })
		r.adapter.roster = []string{consoleAuthor}
		r.adapter.inbox <- InboundMessage{
			Conversation: "console:people-tab", Kind: "dm", Backend: consoleBackend,
			AuthorID: consoleAuthor, MessageID: "m1", Text: "console hello",
		}
		awaitSubmissions(t, r, 1)
		if n := r.g.people.Load().seenLen(); n != 0 {
			t.Fatalf("the desk noted %d people from a console turn", n)
		}
		if keys := peopleKeys(t, peopleKV(t, r)); len(keys) != 0 {
			t.Fatalf("keys = %v, want none from a console turn", keys)
		}
	})
}

func TestAFailedPutLeavesTheTurnAlone(t *testing.T) {
	r := startPeopleRig(t, func(c *Config) {
		createPeopleBucket(t, c.NATSURL, jetstream.KeyValueConfig{History: 1, TTL: peopleTestTTL, MaxBytes: 1})
	})
	desk := waitPeopleLoaded(t, r)
	peopleTurn(r, "discord:g1/pf1", "d-1")
	r.awaitTask(t, "platform")
	waitFor(t, "the put failure log", func() bool { return strings.Contains(r.logs.String(), "people: put failed") })
	// The failed put's optimistic entry is rolled back, so the next turn tries again.
	waitFor(t, "the rollback", func() bool { return desk.seenLen() == 0 })
}

func TestAFailedPutKeepsAPriorEntry(t *testing.T) {
	key := testPeopleKey(t, peopleTestPrincipal)
	prior := time.Now().Add(-2 * time.Hour).UTC().Truncate(time.Second)
	r := startPeopleRig(t, func(c *Config) {
		kv := createPeopleBucket(t, c.NATSURL, jetstream.KeyValueConfig{History: 1, TTL: peopleTestTTL})
		if _, err := kv.PutString(context.Background(), key, prior.Format(time.RFC3339)); err != nil {
			t.Fatalf("seed put: %v", err)
		}
		// A value cap below any time: the seeded entry stays, and every put fails.
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := peopleBucketJS(t, c.NATSURL).UpdateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.PeopleBucket, History: 1, TTL: peopleTestTTL, MaxValueSize: 1}); err != nil {
			t.Fatalf("cap people: %v", err)
		}
	})
	desk := waitPeopleLoaded(t, r)
	if at, ok := desk.seenAt(key); !ok || !at.Equal(prior) {
		t.Fatalf("seen[key] = %v, %v; want the seeded %v", at, ok, prior)
	}
	peopleTurn(r, "discord:g1/pk1", "d-1")
	r.awaitTask(t, "platform")
	waitFor(t, "the put failure log", func() bool { return strings.Contains(r.logs.String(), "people: put failed") })
	waitFor(t, "the prior entry restored", func() bool {
		at, ok := desk.seenAt(key)
		return ok && at.Equal(prior)
	})
	if short, long, ok := desk.count(); !ok || short != 1 || long != 1 {
		t.Fatalf("count = %d, %d, %v; want 1, 1, true", short, long, ok)
	}
}

func TestPeopleSeenOffWritesNothingAndPurges(t *testing.T) {
	var kv jetstream.KeyValue
	r := startPeopleRig(t, func(c *Config) {
		kv = createPeopleBucket(t, c.NATSURL, jetstream.KeyValueConfig{History: 1, TTL: peopleTestTTL})
		for _, p := range []string{"test:a", "test:b"} {
			if _, err := kv.PutString(context.Background(), testPeopleKey(t, p), time.Now().UTC().Format(time.RFC3339)); err != nil {
				t.Fatalf("seed put: %v", err)
			}
		}
		c.PeopleSeenOff = true
	})
	waitFor(t, "the purge", func() bool { return len(peopleKeys(t, kv)) == 0 })
	waitFor(t, "the purge log", func() bool { return strings.Contains(r.logs.String(), "people: purged") })
	if !strings.Contains(r.logs.String(), "keys=2") {
		t.Errorf("purge log lacks keys=2:\n%s", r.logs.String())
	}
	if !strings.Contains(r.logs.String(), "people count is off: turned off on this install (telemetry.peopleSeen)") {
		t.Errorf("log lacks the off line:\n%s", r.logs.String())
	}
	peopleTurn(r, "discord:g1/po1", "d-1")
	awaitSubmissions(t, r, 1)
	if keys := peopleKeys(t, kv); len(keys) != 0 {
		t.Fatalf("keys = %v after a turn, want none", keys)
	}
	if r.g.people.Load() != nil {
		t.Fatal("a desk exists with the count turned off")
	}
}

// startPeopleOffRigWithoutBucket starts a rig with the count turned off and
// no people bucket, waits for the start's purge to find nothing, then
// creates the bucket holding keys n people, and returns the rig and bucket.
// The test calls purgePeople itself, with its seams set.
func startPeopleOffRigWithoutBucket(t *testing.T, n int) (*rig, jetstream.KeyValue) {
	t.Helper()
	r := startPeopleRig(t, func(c *Config) { c.PeopleSeenOff = true })
	waitFor(t, "the no-bucket log", func() bool {
		return strings.Contains(r.logs.String(), "people: nothing to purge: no people bucket")
	})
	kv := createPeopleBucket(t, r.url, jetstream.KeyValueConfig{History: 1, TTL: peopleTestTTL})
	for i := range n {
		if _, err := kv.PutString(context.Background(), testPeopleKey(t, fmt.Sprintf("test:p%d", i)), time.Now().UTC().Format(time.RFC3339)); err != nil {
			t.Fatalf("seed put: %v", err)
		}
	}
	r.g.peoplePurgeRetry = time.Millisecond
	return r, kv
}

func TestAFailedPurgeIsTriedAgain(t *testing.T) {
	r, kv := startPeopleOffRigWithoutBucket(t, 2)
	failed := false
	r.g.peoplePurgeList = func(ctx context.Context, kv jetstream.KeyValue) ([]string, error) {
		if !failed {
			failed = true
			return nil, fmt.Errorf("list failed")
		}
		return listPeopleKeys(ctx, kv)
	}
	r.g.purgePeople(context.Background())
	if keys := peopleKeys(t, kv); len(keys) != 0 {
		t.Fatalf("keys = %v after the retry, want none", keys)
	}
	logs := r.logs.String()
	if got := strings.Count(logs, "people: a purge attempt failed"); got != 1 {
		t.Errorf("purge-failed warnings = %d, want 1:\n%s", got, logs)
	}
	warn := strings.Index(logs, "people: a purge attempt failed")
	purged := strings.Index(logs, "people: purged")
	if purged < warn || !strings.Contains(logs[purged:], "keys=2") {
		t.Errorf("want the warning, then people: purged with keys=2:\n%s", logs)
	}
	if strings.Contains(logs, "no longer trying") {
		t.Errorf("the purge gave up:\n%s", logs)
	}
}

func TestAPurgeThatKeepsFailingGivesUp(t *testing.T) {
	r, kv := startPeopleOffRigWithoutBucket(t, 2)
	lists := 0
	r.g.peoplePurgeList = func(context.Context, jetstream.KeyValue) ([]string, error) {
		lists++
		return nil, fmt.Errorf("list failed")
	}
	r.g.purgePeople(context.Background())
	if want := peopleRetries + 1; lists != want {
		t.Errorf("attempts = %d, want %d", lists, want)
	}
	logs := r.logs.String()
	if got := strings.Count(logs, "people: a purge attempt failed"); got != peopleRetries+1 {
		t.Errorf("purge-failed warnings = %d, want %d", got, peopleRetries+1)
	}
	if !strings.Contains(logs, "people: purge failed; no longer trying") {
		t.Errorf("log lacks the give-up line:\n%s", logs)
	}
	if strings.Contains(logs, "people: purged") {
		t.Errorf("a failed purge logged people: purged:\n%s", logs)
	}
	if keys := peopleKeys(t, kv); len(keys) != 2 {
		t.Errorf("keys = %d, want both left", len(keys))
	}
}

// fakePeopleWatcher is a jetstream.KeyWatcher whose updates the test sends.
type fakePeopleWatcher struct {
	updates chan jetstream.KeyValueEntry
}

func (w *fakePeopleWatcher) Updates() <-chan jetstream.KeyValueEntry { return w.updates }
func (w *fakePeopleWatcher) Stop() error                             { return nil }

// fakePeopleEntry is a put of value under key.
type fakePeopleEntry struct {
	jetstream.KeyValueEntry
	key, value string
}

func (e fakePeopleEntry) Key() string                     { return e.key }
func (e fakePeopleEntry) Value() []byte                   { return []byte(e.value) }
func (e fakePeopleEntry) Operation() jetstream.KeyValueOp { return jetstream.KeyValuePut }

func TestAClosedWatchUnloadsTheMapUntilItIsRebuilt(t *testing.T) {
	logs := &lockedBuffer{}
	first := &fakePeopleWatcher{updates: make(chan jetstream.KeyValueEntry, 2)}
	second := &fakePeopleWatcher{updates: make(chan jetstream.KeyValueEntry, 1)}
	rebuild := make(chan struct{})
	var opened int
	var openedMu sync.Mutex
	d := &peopleDesk{
		g:          &Gateway{log: slog.New(slog.NewTextHandler(logs, nil))},
		now:        time.Now,
		watchRetry: time.Millisecond,
		seen:       map[string]time.Time{},
		unparsable: map[string]bool{},
	}
	d.openWatch = func(ctx context.Context) (jetstream.KeyWatcher, error) {
		openedMu.Lock()
		opened++
		n := opened
		openedMu.Unlock()
		if n == 1 {
			return first, nil
		}
		select {
		case <-rebuild:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return second, nil
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go d.watch(ctx)

	first.updates <- fakePeopleEntry{key: "people.x", value: time.Now().UTC().Format(time.RFC3339)}
	first.updates <- nil
	waitFor(t, "the first watch to load", func() bool {
		short, long, ok := d.count()
		return ok && short == 1 && long == 1
	})

	close(first.updates)
	waitFor(t, "the closed watch to unload the map", func() bool {
		_, _, ok := d.count()
		return !ok
	})
	if d.seenLen() != 0 {
		t.Fatalf("seen holds %d entries after the watch closed, want none", d.seenLen())
	}
	if !strings.Contains(logs.String(), peopleWatchClosedLog) {
		t.Errorf("log lacks the closed line:\n%s", logs.String())
	}

	second.updates <- nil
	close(rebuild)
	waitFor(t, "the rebuilt watch to load", func() bool {
		short, long, ok := d.count()
		return ok && short == 0 && long == 0
	})
}

func TestPeopleCountWindows(t *testing.T) {
	now := time.Date(2026, 11, 2, 12, 0, 0, 0, time.UTC)
	d := &peopleDesk{now: func() time.Time { return now }, loaded: true, seen: map[string]time.Time{
		"people.today":   now.Add(-time.Hour),
		"people.week":    now.Add(-peopleWindowShort),
		"people.month":   now.Add(-20 * 24 * time.Hour),
		"people.expired": now.Add(-peopleWindowLong - time.Second),
	}}
	short, long, ok := d.count()
	if !ok || short != 2 || long != 3 {
		t.Fatalf("count = %d, %d, %v; want 2, 3, true", short, long, ok)
	}
	if _, kept := d.seenAt("people.expired"); kept {
		t.Fatal("an entry older than the long window was kept")
	}
}

func TestADeletedOrPurgedPersonLeavesTheMap(t *testing.T) {
	r := startPeopleRig(t, withPeopleBucket(t, peopleTestTTL))
	desk := waitPeopleLoaded(t, r)
	kv := peopleKV(t, r)
	ctx := context.Background()
	stamp := time.Now().UTC().Format(time.RFC3339)
	for _, k := range []string{"people.deleted", "people.purged"} {
		if _, err := kv.PutString(ctx, k, stamp); err != nil {
			t.Fatal(err)
		}
	}
	waitFor(t, "both entries in the map", func() bool { return desk.seenLen() == 2 })
	if err := kv.Delete(ctx, "people.deleted"); err != nil {
		t.Fatal(err)
	}
	if err := kv.Purge(ctx, "people.purged"); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "both entries gone from the map", func() bool { return desk.seenLen() == 0 })
}

func TestThePutHappensAfterVerificationAndBeforeTheSessionLock(t *testing.T) {
	r := startPeopleRig(t, func(c *Config) {
		withPeopleBucket(t, peopleTestTTL)(c)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := peopleBucketJS(t, c.NATSURL).UpdateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.SessionStateBucket, MaxBytes: 1}); err != nil {
			t.Fatalf("cap session-state: %v", err)
		}
	})
	desk := waitPeopleLoaded(t, r)
	kv := peopleKV(t, r)

	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/pv1", Kind: "group", AuthorID: "9999", MessageID: "d-1", Text: "hello"}
	waitFor(t, "the unverified drop", func() bool {
		return strings.Contains(r.logs.String(), "dropping message from unverified sender")
	})
	if n := desk.seenLen(); n != 0 {
		t.Fatalf("the desk noted %d people from an unverified sender", n)
	}

	peopleTurn(r, "discord:g1/pv2", "d-2")
	key := testPeopleKey(t, peopleTestPrincipal)
	waitFor(t, "the verified sender's key", func() bool {
		_, err := kv.Get(context.Background(), key)
		return err == nil
	})
	waitFor(t, "the session mint failure", func() bool {
		return strings.Contains(r.logs.String(), "session mint failed: session-state bucket is full (max bytes reached)")
	})
	if keys := peopleKeys(t, kv); len(keys) != 1 || keys[0] != key {
		t.Fatalf("keys = %v, want only the verified sender's %s", keys, key)
	}
}

func TestTheWindowsCountTheRecordedTimes(t *testing.T) {
	now := time.Now().UTC()
	logs := &lockedBuffer{}
	r := startRigWithLogger(t, func(c *Config) {
		kv := createPeopleBucket(t, c.NATSURL, jetstream.KeyValueConfig{History: 1, TTL: peopleTestTTL})
		for key, value := range map[string]string{
			"people.k1": now.Add(-time.Hour).Format(time.RFC3339),
			"people.k2": now.Add(-10 * 24 * time.Hour).Format(time.RFC3339),
			"people.k3": now.Add(-40 * 24 * time.Hour).Format(time.RFC3339),
			"people.k4": "not a time",
		} {
			if _, err := kv.PutString(context.Background(), key, value); err != nil {
				t.Fatalf("seed put %s: %v", key, err)
			}
		}
	}, slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})))
	desk := waitPeopleLoaded(t, r)
	if short, long, ok := desk.count(); !ok || short != 1 || long != 2 {
		t.Fatalf("count = %d, %d, %v; want 1, 2, true", short, long, ok)
	}
	if n := strings.Count(logs.String(), peopleBadValueLog); n != 1 {
		t.Errorf("the skipped value was logged %d times, want once:\n%s", n, logs.String())
	}
	if strings.Contains(logs.String(), peopleKeyPrefix) {
		t.Errorf("a log line carries a people key:\n%s", logs.String())
	}
	_, body := scrape(t, http.MethodGet, startMetricsServer(t, r.g.Metrics())+MetricsPath)
	for _, want := range []string{
		`kubeagents_a2a_gateway_people_seen{window="7d"} 1`,
		`kubeagents_a2a_gateway_people_seen{window="30d"} 2`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("the gateway's body lacks %q:\n%s", want, body)
		}
	}
}

func TestAnEntryLeavesTheCountWhenItsTimeAgesOut(t *testing.T) {
	r := startPeopleRig(t, withPeopleBucket(t, peopleTestTTL))
	clock := fixPeopleClock(t, r)
	desk := waitPeopleLoaded(t, r)
	if _, err := peopleKV(t, r).PutString(context.Background(), "people.aging", clock.now().Add(-29*24*time.Hour).Format(time.RFC3339)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the entry in the map", func() bool { return desk.seenLen() == 1 })
	if short, long, ok := desk.count(); !ok || short != 0 || long != 1 {
		t.Fatalf("count = %d, %d, %v; want 0, 1, true", short, long, ok)
	}
	clock.advance(2 * 24 * time.Hour)
	if short, long, ok := desk.count(); !ok || short != 0 || long != 0 {
		t.Fatalf("after two days: count = %d, %d, %v; want 0, 0, true", short, long, ok)
	}
}

func TestADeletedKeyLeavesTheCountAtOnce(t *testing.T) {
	r := startPeopleRig(t, withPeopleBucket(t, peopleTestTTL))
	desk := waitPeopleLoaded(t, r)
	kv := peopleKV(t, r)
	ctx := context.Background()
	const key = "people.leaving"
	if _, err := kv.PutString(ctx, key, time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the entry counted", func() bool {
		short, long, ok := desk.count()
		return ok && short == 1 && long == 1
	})
	if err := kv.Delete(ctx, key); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the entry gone from the count", func() bool {
		short, long, ok := desk.count()
		return ok && short == 0 && long == 0
	})
}
