package authcallout

import (
	"context"
	"io"
	"log/slog"
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
