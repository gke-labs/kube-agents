package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sruntime "k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/rest"
	k8stesting "k8s.io/client-go/testing"
	"k8s.io/client-go/tools/cache"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/fakeapiserver"
)

// brokerFor stands in for the credential broker: one plain-HTTP server that
// serves each fake cluster under the path Broker.ClientConfig addresses it by,
// so the clients under test are built by the production code, transport and
// all. It returns the broker and the identities, in the order of servers.
func brokerFor(t *testing.T, servers ...*fakeapiserver.Server) (clusterprofiles.Broker, []clusterprofiles.Identity) {
	t.Helper()
	mux := http.NewServeMux()
	ids := make([]clusterprofiles.Identity, len(servers))
	for i, s := range servers {
		ids[i] = clusterprofiles.Identity{Project: "proj", Location: "us-central1", Cluster: fmt.Sprintf("cluster-%d", i)}
		prefix := fmt.Sprintf("/v1/k8s/%s/%s/%s", ids[i].Project, ids[i].Location, ids[i].Cluster)
		mux.Handle(prefix+"/", http.StripPrefix(prefix, s.Server.Config.Handler))
	}
	broker := httptest.NewServer(mux)
	t.Cleanup(broker.Close)
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("test-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	return clusterprofiles.Broker{URL: broker.URL, TokenFile: token}, ids
}

// The broker's config qualifies for the watch twin, and the twin differs from
// the shared transport in keep-alives alone. Fails if Broker.ClientConfig ever
// gains TLS, a dialer or a proxy, which would turn the twin off.
func TestBrokerConfigGetsANonPooledWatchTransport(t *testing.T) {
	broker, ids := brokerFor(t, fakeapiserver.Start(fakeapiserver.Options{Seed: 1, Sizes: fakeapiserver.Sizes{Nodes: 1, Pods: 1, Events: 1}}))
	cfg, err := broker.ClientConfig(ids[0])
	if err != nil {
		t.Fatal(err)
	}
	twin, separate := watchConfig(cfg)
	if !separate {
		t.Fatal("the broker's config does not get a watch twin; a pooled watch can stall another cluster's relist (#4825)")
	}
	if cfg.Transport != nil {
		t.Fatal("watchConfig changed the original config's transport; lists must stay on the shared pool")
	}
	got, ok := twin.Transport.(*http.Transport)
	if !ok || !got.DisableKeepAlives {
		t.Fatalf("watch twin transport = %#v; want an *http.Transport with keep-alives off", twin.Transport)
	}
	want := http.DefaultTransport.(*http.Transport).Clone()
	want.DisableKeepAlives = true
	if reflect.ValueOf(got.Proxy).Pointer() != reflect.ValueOf(want.Proxy).Pointer() ||
		reflect.ValueOf(got.DialContext).IsNil() != reflect.ValueOf(want.DialContext).IsNil() ||
		got.TLSHandshakeTimeout != want.TLSHandshakeTimeout || got.IdleConnTimeout != want.IdleConnTimeout ||
		got.MaxIdleConns != want.MaxIdleConns || got.ExpectContinueTimeout != want.ExpectContinueTimeout ||
		got.ForceAttemptHTTP2 != want.ForceAttemptHTTP2 {
		t.Fatalf("watch twin transport differs from http.DefaultTransport beyond keep-alives:\n got %+v\nwant %+v", got, want)
	}
	if twin.Host != cfg.Host || twin.BearerTokenFile != cfg.BearerTokenFile {
		t.Fatalf("watch twin addresses %q with %q; want the original's %q with %q", twin.Host, twin.BearerTokenFile, cfg.Host, cfg.BearerTokenFile)
	}
}

// A config client-go already builds its own transport for is left as it is.
func TestWatchConfigLeavesAConfigWithItsOwnTransportAlone(t *testing.T) {
	for name, cfg := range map[string]*rest.Config{
		"a CA":     {Host: "https://h", TLSClientConfig: rest.TLSClientConfig{CAData: []byte("ca")}},
		"insecure": {Host: "https://h", TLSClientConfig: rest.TLSClientConfig{Insecure: true}},
		"a proxy":  {Host: "http://h", Proxy: http.ProxyFromEnvironment},
		"a dialer": {Host: "http://h", Dial: func(context.Context, string, string) (net.Conn, error) {
			return nil, fmt.Errorf("no dialing")
		}},
		"a transport":         {Host: "http://h", Transport: &http.Transport{}},
		"a wrapped transport": {Host: "http://h", WrapTransport: func(rt http.RoundTripper) http.RoundTripper { return rt }},
	} {
		t.Run(name, func(t *testing.T) {
			got, separate := watchConfig(cfg)
			if separate || got != cfg {
				t.Fatalf("watchConfig(%s) = %v, %v; want the config itself and false", name, got, separate)
			}
		})
	}
}

// The watcher opens its Events watch with the watch client, and everything
// else (the list, the access review) with the client.
func TestWatcherWatchesWithTheWatchClient(t *testing.T) {
	broker, ids := brokerFor(t, fakeapiserver.Start(fakeapiserver.Options{Seed: 1, Sizes: fakeapiserver.Sizes{Nodes: 1, Pods: 1, Events: 1}}))
	cfg, err := broker.ClientConfig(ids[0])
	if err != nil {
		t.Fatal(err)
	}
	client, watchClient, err := newClusterClients(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if client == watchClient {
		t.Fatal("a broker-routed cluster got one client for both; want a separate watch client")
	}
	w := newWatcher(client, nil, targetCluster{Name: "c", Client: client, WatchClient: watchClient})
	if w.watchClient != watchClient || w.client != client {
		t.Fatal("the watcher does not hold the watch client for its watch")
	}
	if newWatcher(client, nil, targetCluster{Name: "c", Client: client}).watchClient != client {
		t.Fatal("with no watch client, the watcher should watch with its client")
	}

	// And the Events watch the reflector opens goes through the watch client.
	listFake, watchFake := fake.NewClientset(), fake.NewClientset()
	listFake.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		t.Error("the Events watch went through the list client")
		return true, watch.NewFake(), nil
	})
	var watched atomic.Int64
	watchFake.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		watched.Add(1)
		return true, watch.NewFake(), nil
	})
	fw := newWatcher(listFake, nil, targetCluster{Name: "c", Client: listFake, WatchClient: watchFake})
	wi, err := fw.newListWatch().Watch(metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	wi.Stop()
	if watched.Load() != 1 {
		t.Fatalf("watch client saw %d Events watches; want 1", watched.Load())
	}
}

// eventsListerWatcher is the watcher's split: the list on one client, the
// watch on another, with watch-list semantics as the watcher has them.
func eventsListerWatcher(list, watchWith kubernetes.Interface) cache.ListerWatcher {
	return cache.ToListWatcherWithWatchListSemantics(&cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, opts metav1.ListOptions) (k8sruntime.Object, error) {
			return list.CoreV1().Events(metav1.NamespaceAll).List(ctx, opts)
		},
		WatchFuncWithContext: func(ctx context.Context, opts metav1.ListOptions) (watch.Interface, error) {
			return watchWith.CoreV1().Events(metav1.NamespaceAll).Watch(ctx, opts)
		},
	}, list)
}

// reflectorRecoversThroughBroker is the reflector-expiry scenario #4823
// traced the race in (fakeapiserver's TestReflector_SyncsAndRecoversFromExpiry),
// on the production route: the clients come from Broker.ClientConfig, and split
// chooses the watcher's own (newClusterClients, the watch on its non-pooled
// twin) or one pooled client for both, as the watcher was before #4825. The
// fake throttles the first request, the reflector syncs, the fake expires its
// watch with a 410, and the reflector must stream the state again within 20 s.
// Under -race on Go 1.27 the pooled client fails that a few times in a hundred:
// the re-stream lands on the connection the stopped watch left a reader parked
// on, and hangs until the connection closes.
func reflectorRecoversThroughBroker(t *testing.T, split bool) {
	t.Helper()
	s := fakeapiserver.Start(fakeapiserver.Options{Seed: 5, Sizes: fakeapiserver.Sizes{Nodes: 7, Pods: 23, Events: 61}})
	t.Cleanup(s.Close)
	broker, ids := brokerFor(t, s)
	cfg, err := broker.ClientConfig(ids[0])
	if err != nil {
		t.Fatal(err)
	}
	var client, watchWith kubernetes.Interface
	if split {
		client, watchWith, err = newClusterClients(cfg)
	} else {
		client, err = newClusterClient(cfg)
		watchWith = client
	}
	if err != nil {
		t.Fatal(err)
	}
	s.ThrottleFirst(1, time.Second)
	store := cache.NewStore(cache.MetaNamespaceKeyFunc)
	reflector := cache.NewReflector(eventsListerWatcher(client, watchWith), &corev1.Event{}, store, 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go reflector.RunWithContext(ctx)

	eventually(t, 20*time.Second, func() bool { return len(store.List()) == 61 }, "the reflector's initial sync")
	s.Expire()
	eventually(t, 20*time.Second, func() bool { return s.Counts().WatchLists == 2 }, "the reflector to stream the state again")
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "after-expiry", Namespace: "team-1"}})
	eventually(t, 20*time.Second, func() bool {
		_, ok, _ := store.GetByKey("team-1/after-expiry")
		return ok
	}, "the post-expiry Event to reach the store")
}

// With the watcher's clients, a cluster reached through the broker recovers
// from an expired watch. The race is rare per run; this is the scenario, run
// on every CI pass, and the pooled baseline below is the comparison.
func TestBrokerReflectorRecoversFromExpiry(t *testing.T) {
	reflectorRecoversThroughBroker(t, true)
}

// The baseline the fix is measured against: one pooled client for list and
// watch, as before #4825. Opt-in, because on Go 1.27 under -race it is expected
// to fail a few runs in a hundred, and that failure is the point:
//
//	KUBE_AGENTS_REPRO_POOLED=1 go test -race -run TestBrokerReflectorPooledBaseline -count=200 ./cmd/k8s-event-watcher/
func TestBrokerReflectorPooledBaseline(t *testing.T) {
	if os.Getenv("KUBE_AGENTS_REPRO_POOLED") != "1" {
		t.Skip("set KUBE_AGENTS_REPRO_POOLED=1 to run the pooled baseline")
	}
	reflectorRecoversThroughBroker(t, false)
}

// Every broker-routed cluster's client shares one transport, the process's
// default pool, so a reader one cluster's watch leaves parked can stall
// another cluster's next request. The watches get transports of their own.
func TestBrokerClustersShareOnePoolButNotTheirWatches(t *testing.T) {
	a := fakeapiserver.Start(fakeapiserver.Options{Seed: 1, Sizes: fakeapiserver.Sizes{Nodes: 1, Pods: 1, Events: 1}})
	b := fakeapiserver.Start(fakeapiserver.Options{Seed: 2, Sizes: fakeapiserver.Sizes{Nodes: 1, Pods: 1, Events: 1}})
	t.Cleanup(a.Close)
	t.Cleanup(b.Close)
	broker, ids := brokerFor(t, a, b)
	var lists, watches []http.RoundTripper
	for _, id := range ids {
		cfg, err := broker.ClientConfig(id)
		if err != nil {
			t.Fatal(err)
		}
		lt, err := rest.TransportFor(cfg)
		if err != nil {
			t.Fatal(err)
		}
		lists = append(lists, baseTransport(lt))
		twin, separate := watchConfig(cfg)
		if !separate {
			t.Fatal("no watch twin for a broker-routed cluster")
		}
		watches = append(watches, twin.Transport)
	}
	if lists[0] != http.DefaultTransport || lists[1] != http.DefaultTransport {
		t.Fatalf("the clusters' list transports are %T and %T; want both on http.DefaultTransport, the shared pool this guards against", lists[0], lists[1])
	}
	if watches[0] == watches[1] || watches[0] == http.DefaultTransport || watches[1] == http.DefaultTransport {
		t.Fatal("the clusters' watches share a transport, or the default pool; want one non-pooled transport each")
	}
}

// baseTransport unwraps client-go's round-tripper wrappers (bearer auth,
// user agent) to the transport underneath.
func baseTransport(rt http.RoundTripper) http.RoundTripper {
	for {
		w, ok := rt.(interface{ WrappedRoundTripper() http.RoundTripper })
		if !ok {
			return rt
		}
		rt = w.WrappedRoundTripper()
	}
}

// Discovery hands each broker-routed cluster a separate watch client, and a
// cluster reached directly over TLS keeps the one client it had.
func TestDiscoveryGivesBrokerClustersAWatchClient(t *testing.T) {
	for _, viaBroker := range []bool{true, false} {
		t.Run(fmt.Sprintf("viaBroker=%v", viaBroker), func(t *testing.T) {
			stubGKE(t)
			if viaBroker {
				token := filepath.Join(t.TempDir(), "token")
				if err := os.WriteFile(token, []byte("t"), 0o600); err != nil {
					t.Fatal(err)
				}
				discovery.Broker = &clusterprofiles.Broker{URL: "http://127.0.0.1:1", TokenFile: token}
			}
			dir := t.TempDir()
			writeClusterProfile(t, dir, "cluster-projA-prod-us-central1", "projA", "prod", "us-central1")
			clusters, err := discoverClusterProfiles(context.Background(), dir, newMetrics())
			if err != nil || len(clusters) != 1 {
				t.Fatalf("discoverClusterProfiles = %v, %v; want one cluster", clusterNames(clusters), err)
			}
			c := clusters[0]
			if viaBroker && (c.WatchClient == nil || c.WatchClient == c.Client) {
				t.Fatal("a broker-routed cluster has no separate watch client")
			}
			if viaBroker {
				// The client actually built must sit on the non-pooled twin, not
				// merely be a second client on the shared pool.
				rc, ok := c.WatchClient.CoreV1().RESTClient().(*rest.RESTClient)
				if !ok || rc.Client == nil {
					t.Fatalf("watch client's REST client is %T; want a *rest.RESTClient with an HTTP client", c.WatchClient.CoreV1().RESTClient())
				}
				if tr, ok := baseTransport(rc.Client.Transport).(*http.Transport); !ok || !tr.DisableKeepAlives {
					t.Fatalf("watch client's transport is %T (%v); want an *http.Transport with keep-alives off", baseTransport(rc.Client.Transport), ok)
				}
				lrc := c.Client.CoreV1().RESTClient().(*rest.RESTClient)
				if baseTransport(lrc.Client.Transport) != http.DefaultTransport {
					t.Fatal("the list client left the shared pool; lists should stay pooled")
				}
			}
			if !viaBroker && c.WatchClient != c.Client {
				t.Fatal("a directly reached TLS cluster got a second client; want its one client for the watch too")
			}
		})
	}
}
