package authcallout

import (
	"context"
	"io"
	"log/slog"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clientfeatures "k8s.io/client-go/features"
	clientfeaturestesting "k8s.io/client-go/features/testing"
	"k8s.io/client-go/kubernetes/fake"
)

func quietStore() *Store {
	return NewStore(slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func mapWithVersion(v, user string) string {
	return `{"version":"` + v + `","identities":[
	  {"serviceAccount":"system:serviceaccount:ns:` + user + `","user":"` + user + `","account":"APP",
	   "grants":{"publish":["a.>"],"subscribe":["_INBOX.` + user + `.>"]}}]}`
}

func TestAnEmptyStoreServesNothingAndIsNotReady(t *testing.T) {
	s := quietStore()
	if s.Ready() {
		t.Error("an empty store reports ready")
	}
	if s.Current() != nil {
		t.Error("an empty store returns a map")
	}
	if s.Version() != "" {
		t.Errorf("an empty store reports version %q", s.Version())
	}
}

func TestUpdateServesTheMapAndItsVersion(t *testing.T) {
	s := quietStore()
	if err := s.Update([]byte(mapWithVersion("v1", "gateway"))); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if !s.Ready() {
		t.Error("store is not ready after a good update")
	}
	if s.Version() != "v1" {
		t.Errorf("Version = %q, want v1", s.Version())
	}
	if _, ok := s.Current().Lookup("system:serviceaccount:ns:gateway"); !ok {
		t.Error("the served map does not resolve the identity it was given")
	}
}

// The behaviour that decides what a bad render costs. Refusing the update and
// continuing to serve the previous map means a render mistake delays a change;
// accepting it, or dropping to empty, means every subsequent connection is
// refused — with the callout on the connection path, that is the fabric going
// dark to new work because of a typo.
func TestABadUpdateLeavesThePreviousMapServing(t *testing.T) {
	s := quietStore()
	if err := s.Update([]byte(mapWithVersion("v1", "gateway"))); err != nil {
		t.Fatalf("first Update: %v", err)
	}

	err := s.Update([]byte(`{"version":"v2","identities":[{"serviceAccount":"not-a-service-account","user":"x","account":"APP","grants":{"publish":["a.>"]}}]}`))
	if err == nil {
		t.Fatal("a malformed map was accepted")
	}

	if !s.Ready() {
		t.Error("store stopped being ready because one update was bad")
	}
	if s.Version() != "v1" {
		t.Errorf("serving version %q, want the previous v1", s.Version())
	}
	if s.LastError() == "" {
		t.Error("the refusal was silent; nothing would tell an operator the render did not land")
	}
	if !strings.Contains(s.LastError(), "is not system:serviceaccount:") {
		t.Errorf("LastError = %q, want it to name the problem", s.LastError())
	}
}

func TestAGoodUpdateClearsThePreviousError(t *testing.T) {
	s := quietStore()
	_ = s.Update([]byte(`{"version":"bad"}`))
	if s.LastError() == "" {
		t.Fatal("no error recorded for a bad update")
	}
	if err := s.Update([]byte(mapWithVersion("v2", "gateway"))); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if s.LastError() != "" {
		t.Errorf("LastError still set after a good update: %q", s.LastError())
	}
}

// The whole reason for the informer, exercised end to end against a real
// client-go watch: a ConfigMap write must reach the store without anything
// polling for it.
func TestWatchPicksUpTheRenderedMapAndItsChanges(t *testing.T) {
	const (
		ns   = "kubeagents-system"
		name = "agent-a2a-authmap"
		key  = "identities.json"
	)

	// The reflector prefers the streaming WatchList protocol, which has
	// been on by default since client-go 1.35 and which the fake clientset
	// does not implement — with it enabled the informer issues a watch, gets
	// no initial state, and silently never fires. Turning it off here makes
	// the test exercise the LIST-then-WATCH fallback instead.
	//
	// Recorded rather than hidden: this is the one place the test and
	// production take different paths. A real API server serves WatchList,
	// so that is what the callout will actually use, and it is covered by
	// the live validation rather than here.
	clientfeaturestesting.SetFeatureDuringTest(t, clientfeatures.WatchListClient, false)

	client := fake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Data:       map[string]string{key: mapWithVersion("v1", "gateway")},
	})

	s := quietStore()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = s.WatchConfigMap(ctx, client, ns, name, key) }()

	if err := s.WaitForMap(ctx, 5*time.Second); err != nil {
		t.Fatalf("the watch never delivered the map: %v", err)
	}
	if s.Version() != "v1" {
		t.Fatalf("Version = %q, want v1", s.Version())
	}

	updated := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Data:       map[string]string{key: mapWithVersion("v2", "gateway")},
	}
	if _, err := client.CoreV1().ConfigMaps(ns).Update(ctx, updated, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("updating the ConfigMap: %v", err)
	}

	deadline := time.Now().Add(5 * time.Second)
	for s.Version() != "v2" {
		if time.Now().After(deadline) {
			t.Fatalf("the watch did not deliver the change; still serving %q", s.Version())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestWaitForMapReportsWhyNothingIsServed(t *testing.T) {
	s := quietStore()
	_ = s.Update([]byte(`{"version":"v1","identities":[{"serviceAccount":"nope","user":"x","account":"APP","grants":{"publish":["a.>"]}}]}`))

	err := s.WaitForMap(context.Background(), 100*time.Millisecond)
	if err == nil {
		t.Fatal("WaitForMap returned success with nothing served")
	}
	// A callout that starts, serves nothing, and says only "timeout" sends
	// whoever is debugging it to the network. The refusal reason is the
	// thing that names the render.
	if !strings.Contains(err.Error(), "is not system:serviceaccount:") {
		t.Errorf("error = %v, want it to carry the refusal reason", err)
	}
}

// shardDoc is one shard of a map: the stamps the operator writes, one identity.
func shardDoc(version string, shard, shards int, user string) string {
	return `{"version":"` + version + `","shard":` + strconv.Itoa(shard) + `,"shards":` + strconv.Itoa(shards) + `,"identities":[
	  {"serviceAccount":"system:serviceaccount:ns:` + user + `","user":"` + user + `","account":"APP",
	   "grants":{"publish":["a.>"],"subscribe":["_INBOX.` + user + `.>"]}}]}`
}

func shardedStore(t *testing.T, shards int) *Store {
	t.Helper()
	s, err := NewShardedStore(slog.New(slog.NewTextHandler(io.Discard, nil)), shards)
	if err != nil {
		t.Fatalf("NewShardedStore(%d): %v", shards, err)
	}
	return s
}

// The map is served only once every shard of one version is in hand, and then
// it is the whole map: an identity from each shard resolves.
func TestAShardedMapIsServedOnceEveryShardOfOneVersionHasArrived(t *testing.T) {
	s := shardedStore(t, 3)
	if err := s.UpdateShard(0, []byte(shardDoc("v1", 0, 3, "gateway"))); err != nil {
		t.Fatalf("shard 0: %v", err)
	}
	if err := s.UpdateShard(2, []byte(shardDoc("v1", 2, 3, "verifier"))); err != nil {
		t.Fatalf("shard 2: %v", err)
	}
	if s.Ready() {
		t.Fatal("the store serves a map with a shard still missing")
	}
	if !strings.Contains(s.Pending(), "shard(s) 1 of 3") {
		t.Errorf("Pending = %q, want it to name the missing shard", s.Pending())
	}
	if s.LastError() != "" {
		t.Errorf("an incomplete set is recorded as a refusal: %q", s.LastError())
	}
	if err := s.UpdateShard(1, []byte(shardDoc("v1", 1, 3, "session"))); err != nil {
		t.Fatalf("shard 1: %v", err)
	}
	if !s.Ready() || s.Version() != "v1" {
		t.Fatalf("ready=%v version=%q after every shard arrived, want ready at v1", s.Ready(), s.Version())
	}
	if s.Pending() != "" {
		t.Errorf("Pending = %q after a complete set", s.Pending())
	}
	for _, user := range []string{"gateway", "session", "verifier"} {
		if _, ok := s.Current().Lookup("system:serviceaccount:ns:" + user); !ok {
			t.Errorf("the assembled map does not resolve %s, which arrived in one of the shards", user)
		}
	}
	if got := s.Current().Users(); !slices.Equal(got, []string{"gateway", "session", "verifier"}) {
		t.Errorf("Users = %v, want every shard's", got)
	}
}

// A re-render lands one shard at a time. Until the last one lands the shards
// carry two versions, and the previous map keeps serving rather than a mix of
// the two; the status says so.
func TestAReRenderKeepsThePreviousMapUntilEveryShardIsOnTheNewVersion(t *testing.T) {
	s := shardedStore(t, 2)
	for i, user := range []string{"gateway", "session"} {
		if err := s.UpdateShard(i, []byte(shardDoc("v1", i, 2, user))); err != nil {
			t.Fatalf("v1 shard %d: %v", i, err)
		}
	}
	if err := s.UpdateShard(0, []byte(shardDoc("v2", 0, 2, "gateway"))); err != nil {
		t.Fatalf("v2 shard 0: %v", err)
	}
	if s.Version() != "v1" {
		t.Errorf("serving %q with one shard on v2, want the previous v1 until the set agrees", s.Version())
	}
	if !strings.Contains(s.Pending(), "v1") || !strings.Contains(s.Pending(), "v2") {
		t.Errorf("Pending = %q, want it to name both versions", s.Pending())
	}
	if err := s.UpdateShard(1, []byte(shardDoc("v2", 1, 2, "worker"))); err != nil {
		t.Fatalf("v2 shard 1: %v", err)
	}
	if s.Version() != "v2" {
		t.Errorf("serving %q after both shards reached v2", s.Version())
	}
	if _, ok := s.Current().Lookup("system:serviceaccount:ns:session"); ok {
		t.Error("the v2 map still resolves an identity only v1 carried; the shards were mixed")
	}
	if _, ok := s.Current().Lookup("system:serviceaccount:ns:worker"); !ok {
		t.Error("the v2 map does not resolve the identity v2's second shard carried")
	}
}

// The stamps are checked against what the callout was told and the name the
// document arrived under: a count other than the configured one, an index
// other than the name's, and no stamps at all on a store expecting several,
// are each refused with the previous map kept.
func TestAShardWhoseStampsDisagreeIsRefused(t *testing.T) {
	cases := map[string]struct {
		index int
		doc   string
		want  string
	}{
		"a count other than the configured one": {0, shardDoc("v2", 0, 3, "gateway"), "expects 2 shards"},
		"an index other than the name's":        {1, shardDoc("v2", 0, 2, "gateway"), "arrived under the name of shard 1"},
		"no stamps on a sharded store":          {0, mapWithVersion("v2", "gateway"), "carries no shard stamps"},
		"an index past the count":               {2, shardDoc("v2", 2, 2, "gateway"), "not a place in a count"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			s := shardedStore(t, 2)
			for i, user := range []string{"gateway", "session"} {
				if err := s.UpdateShard(i, []byte(shardDoc("v1", i, 2, user))); err != nil {
					t.Fatalf("v1 shard %d: %v", i, err)
				}
			}
			err := s.UpdateShard(tc.index, []byte(tc.doc))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("UpdateShard = %v, want a refusal containing %q", err, tc.want)
			}
			if s.Version() != "v1" {
				t.Errorf("serving %q after a refused shard, want the previous v1", s.Version())
			}
			if !strings.Contains(s.LastError(), tc.want) {
				t.Errorf("LastError = %q, want the refusal", s.LastError())
			}
		})
	}
}

// Two shards each sound on their own can name one ServiceAccount between
// them; the assembled set is checked as the single map it becomes.
func TestADuplicateAcrossShardsIsRefusedAtAssembly(t *testing.T) {
	s := shardedStore(t, 2)
	if err := s.UpdateShard(0, []byte(shardDoc("v1", 0, 2, "gateway"))); err != nil {
		t.Fatalf("shard 0: %v", err)
	}
	err := s.UpdateShard(1, []byte(shardDoc("v1", 1, 2, "gateway")))
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("UpdateShard = %v, want the duplicate refused", err)
	}
	if s.Ready() {
		t.Error("a map with a duplicate across shards is being served")
	}
}

// The one-shard store takes a stamped shard 0 of 1 and an unstamped whole map
// alike: the fixture the operator renders is the former, and a map from
// before sharding the latter.
func TestAOneShardStoreTakesAStampedOrUnstampedWholeMap(t *testing.T) {
	s := quietStore()
	if err := s.Update([]byte(shardDoc("v1", 0, 1, "gateway"))); err != nil {
		t.Fatalf("stamped 0 of 1: %v", err)
	}
	if err := s.Update([]byte(mapWithVersion("v2", "gateway"))); err != nil {
		t.Fatalf("unstamped: %v", err)
	}
	if s.Version() != "v2" {
		t.Errorf("serving %q, want v2", s.Version())
	}
	if _, err := NewShardedStore(nil, 0); err == nil {
		t.Error("a store expecting no shards was built; it could never serve anything")
	}
}

// The whole reason for one informer per shard: two ConfigMap writes, under the
// numbered names, reach the store and are served as one map.
func TestWatchingShardsAssemblesTheMapFromEveryConfigMap(t *testing.T) {
	const (
		ns   = "kubeagents-system"
		stem = "agent-a2a-authmap"
		key  = "identities.json"
	)
	clientfeaturestesting.SetFeatureDuringTest(t, clientfeatures.WatchListClient, false)

	client := fake.NewSimpleClientset(
		&corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Name: ShardName(stem, 0), Namespace: ns},
			Data:       map[string]string{key: shardDoc("v1", 0, 2, "gateway")},
		},
		&corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Name: ShardName(stem, 1), Namespace: ns},
			Data:       map[string]string{key: shardDoc("v1", 1, 2, "session")},
		},
	)
	if ShardName(stem, 1) != "agent-a2a-authmap-1" {
		t.Fatalf("ShardName = %q, want the operator's spelling agent-a2a-authmap-1", ShardName(stem, 1))
	}

	s := shardedStore(t, 2)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = s.WatchConfigMapShards(ctx, client, ns, stem, key) }()

	if err := s.WaitForMap(ctx, 5*time.Second); err != nil {
		t.Fatalf("the watches never delivered both shards: %v", err)
	}
	if got := s.Current().Users(); !slices.Equal(got, []string{"gateway", "session"}) {
		t.Fatalf("Users = %v, want both shards' identities", got)
	}

	// A re-render: shard 1 first, then shard 0. The served map changes once,
	// when the second write lands.
	for _, w := range []struct {
		i    int
		user string
	}{{1, "worker"}, {0, "gateway"}} {
		cm := &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Name: ShardName(stem, w.i), Namespace: ns},
			Data:       map[string]string{key: shardDoc("v2", w.i, 2, w.user)},
		}
		if _, err := client.CoreV1().ConfigMaps(ns).Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
			t.Fatalf("updating shard %d: %v", w.i, err)
		}
	}
	deadline := time.Now().Add(5 * time.Second)
	for s.Version() != "v2" {
		if time.Now().After(deadline) {
			t.Fatalf("the watches did not deliver the re-render; still serving %q (pending: %s)", s.Version(), s.Pending())
		}
		time.Sleep(10 * time.Millisecond)
	}
	if got := s.Current().Users(); !slices.Equal(got, []string{"gateway", "worker"}) {
		t.Errorf("Users = %v after the re-render, want the v2 set", got)
	}
}

// A shard refused and then replaced by one that is taken leaves no refusal on
// record; what stands is the shard still missing, and WaitForMap names it
// rather than the refusal the next document resolved, or nothing at all. The
// shard whose watch delivers nothing is the diagnostic the startup failure
// has to carry.
func TestATakenShardSupersedesItsRefusalAndWaitForMapNamesWhatIsPending(t *testing.T) {
	s := shardedStore(t, 2)
	if err := s.UpdateShard(0, []byte(`not a document`)); err == nil {
		t.Fatal("a document that does not parse was taken")
	}
	if s.LastError() == "" {
		t.Fatal("a refused shard left no error")
	}
	if err := s.UpdateShard(0, []byte(shardDoc("v1", 0, 2, "gateway"))); err != nil {
		t.Fatalf("shard 0: %v", err)
	}
	if s.LastError() != "" {
		t.Errorf("LastError = %q after a document under the refused index was taken; want it cleared", s.LastError())
	}
	err := s.WaitForMap(context.Background(), 100*time.Millisecond)
	if err == nil {
		t.Fatal("WaitForMap returned success with one shard of two")
	}
	if !strings.Contains(err.Error(), "shard(s) 1 of 2 not yet seen") {
		t.Errorf("error = %v, want it to name the shard still pending", err)
	}
	if strings.Contains(err.Error(), "refused") {
		t.Errorf("error = %v, carries a refusal a later document superseded", err)
	}
}

// A complete set refused at assembly is not pending: every shard of the
// version arrived. The status says the set was refused and why, not that a
// shard is still missing.
func TestASetRefusedAtAssemblyLeavesNothingPending(t *testing.T) {
	s := shardedStore(t, 2)
	if err := s.UpdateShard(0, []byte(shardDoc("v1", 0, 2, "gateway"))); err != nil {
		t.Fatalf("shard 0: %v", err)
	}
	if s.Pending() == "" {
		t.Fatal("one shard of two is not reported as pending")
	}
	if err := s.UpdateShard(1, []byte(shardDoc("v1", 1, 2, "gateway"))); err == nil {
		t.Fatal("a duplicate across shards was assembled")
	}
	if s.Pending() != "" {
		t.Errorf("Pending = %q after the complete set was refused; want nothing pending", s.Pending())
	}
	if !strings.Contains(s.LastError(), "duplicate") {
		t.Errorf("LastError = %q, want the assembly refusal", s.LastError())
	}
	if !strings.Contains(s.Unserved(), "duplicate") || strings.Contains(s.Unserved(), "pending") {
		t.Errorf("Unserved = %q, want the refusal and no pending note", s.Unserved())
	}
}

// A shard whose latest document was refused stays on record while the
// previous map serves, however often a sibling is redelivered unchanged: the
// redelivery re-assembles the set still in hand, which is the same map, and
// must not read as the broken shard having been fixed. A document taken under
// the refused index is what clears it.
func TestASiblingRedeliveredUnchangedDoesNotClearARefusedShard(t *testing.T) {
	s := shardedStore(t, 2)
	for i, user := range []string{"gateway", "session"} {
		if err := s.UpdateShard(i, []byte(shardDoc("v1", i, 2, user))); err != nil {
			t.Fatalf("v1 shard %d: %v", i, err)
		}
	}
	if err := s.UpdateShard(0, []byte(shardDoc("v2", 0, 3, "gateway"))); err == nil {
		t.Fatal("a shard stamped for another count was taken")
	}
	if !strings.Contains(s.LastError(), "shard 0") {
		t.Fatalf("LastError = %q, want shard 0's refusal", s.LastError())
	}
	// Shard 1 again, unchanged: a relist or a metadata-only update.
	if err := s.UpdateShard(1, []byte(shardDoc("v1", 1, 2, "session"))); err != nil {
		t.Fatalf("v1 shard 1 redelivered: %v", err)
	}
	if s.Version() != "v1" {
		t.Errorf("serving %q, want v1 kept", s.Version())
	}
	if !strings.Contains(s.LastError(), "shard 0") || !strings.Contains(s.LastError(), "expects 2 shards") {
		t.Errorf("LastError = %q after a sibling's redelivery; want shard 0's refusal kept", s.LastError())
	}
	if err := s.UpdateShard(0, []byte(shardDoc("v1", 0, 2, "gateway"))); err != nil {
		t.Fatalf("v1 shard 0 again: %v", err)
	}
	if s.LastError() != "" {
		t.Errorf("LastError = %q after shard 0 was taken again; want it cleared", s.LastError())
	}
}
