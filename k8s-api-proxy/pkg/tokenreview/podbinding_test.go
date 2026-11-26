package tokenreview

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apiserver/pkg/authentication/user"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const otherPeer = "198.51.100.7:40000"

var podsResource = schema.GroupResource{Resource: "pods"}

// newPodBoundAuthenticator is newTestTokenReviewAuthenticator with a logger of its own.
func newPodBoundAuthenticator(t *testing.T, fake *fakeTokenReviewServer, logger *slog.Logger) k8sapiproxy.CallerAuthenticator {
	t.Helper()
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	a, err := NewAuthenticator(Config{
		Kubernetes:     &rest.Config{Host: srv.URL, BearerToken: testReviewerToken, ContentConfig: jsonContent},
		Audience:       testAudience,
		AllowedCallers: []string{shellUsername},
		Logger:         logger,
	})
	if err != nil {
		t.Fatalf("NewAuthenticator: %v", err)
	}
	return a
}

func requestFrom(remoteAddr, token string) *http.Request {
	r := bearerRequest(token)
	r.RemoteAddr = remoteAddr
	return r
}

func TestPodBindingAdmitsTheTokensOwnPod(t *testing.T) {
	fake := &fakeTokenReviewServer{status: shellReview()}
	a := newPodBoundAuthenticator(t, fake, nil)
	id, err := a.AuthenticateRequest(requestFrom(shellPodIP+":51000", testCallerToken))
	if err != nil {
		t.Fatalf("a request from the token's own pod: %v", err)
	}
	if id.Username != shellUsername {
		t.Errorf("Username = %q", id.Username)
	}
	if got := fake.podCalls(); got != 1 {
		t.Errorf("pod GETs = %d, want 1", got)
	}
}

func TestPodBindingRefusesAnotherPeerWithAValidToken(t *testing.T) {
	fake := &fakeTokenReviewServer{status: shellReview()}
	a := newPodBoundAuthenticator(t, fake, nil)
	_, err := a.AuthenticateRequest(requestFrom(otherPeer, testCallerToken))
	if !errors.Is(err, errPeerNotPod) {
		t.Fatalf("a valid token from another address: err = %v, want errPeerNotPod", err)
	}
	if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("refused as unavailable (503); want a refusal (401)")
	}

	// Through the handler it is the same 401 an unknown token gets, and nothing is forwarded.
	h, upstream := newTestHandler(t, a)
	if rec := serve(h, requestFrom(otherPeer, testCallerToken)); rec.Code != http.StatusUnauthorized {
		t.Errorf("handler code = %d, want 401", rec.Code)
	}
	if upstream.req != nil {
		t.Error("a refused request reached the upstream")
	}
}

func TestPodBindingRefusesAPodItCannotRead(t *testing.T) {
	for _, tc := range []struct {
		name    string
		podCode int
		pods    map[string]*corev1.Pod
		logs    bool
	}{
		{name: "pod gone (404)", pods: map[string]*corev1.Pod{}},
		{name: "API error (500)", podCode: http.StatusInternalServerError},
		{name: "no permission (403)", podCode: http.StatusForbidden, logs: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var logs bytes.Buffer
			fake := &fakeTokenReviewServer{status: shellReview(), podCode: tc.podCode, pods: tc.pods}
			a := newPodBoundAuthenticator(t, fake, slog.New(slog.NewJSONHandler(&logs, nil)))
			_, err := a.AuthenticateRequest(requestFrom(shellPodIP+":51000", testCallerToken))
			if err == nil {
				t.Fatal("admitted a caller whose pod could not be read")
			}
			if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
				t.Errorf("refused as unavailable (503); want a refusal (401): %v", err)
			}
			logged := strings.Contains(logs.String(), `"permission":"get pods"`) && strings.Contains(logs.String(), `"namespace":"`+shellPodNamespace+`"`)
			if logged != tc.logs {
				t.Errorf("logged the missing permission = %v, want %v; logs: %s", logged, tc.logs, logs.String())
			}
			if !tc.logs {
				return
			}
			// A second refusal within the interval is not logged again.
			before := strings.Count(logs.String(), "\n")
			if _, err := a.AuthenticateRequest(requestFrom(shellPodIP+":51000", testCallerToken)); err == nil {
				t.Fatal("admitted on the second request")
			}
			if after := strings.Count(logs.String(), "\n"); after != before {
				t.Errorf("logged %d more lines within the interval, want 0", after-before)
			}
		})
	}
}

func TestPodBindingRefusesAMismatchedOrStoppedPod(t *testing.T) {
	for _, tc := range []struct {
		name string
		pod  *corev1.Pod
	}{
		{"another pod by the same name", testPod(shellPodNamespace, shellPodName, "pod-uid-2", corev1.PodRunning, shellPodIP)},
		{"succeeded", testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodSucceeded, shellPodIP)},
		{"failed", testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodFailed, shellPodIP)},
		{"no address", testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodPending)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeTokenReviewServer{status: shellReview(), pods: map[string]*corev1.Pod{shellPodNamespace + "/" + shellPodName: tc.pod}}
			a := newPodBoundAuthenticator(t, fake, nil)
			if _, err := a.AuthenticateRequest(requestFrom(shellPodIP+":51000", testCallerToken)); err == nil {
				t.Error("admitted")
			}
		})
	}
}

func TestPodBindingRefusesATokenThatNamesNoPod(t *testing.T) {
	for name, extra := range map[string]map[string]authenticationv1.ExtraValue{
		"no extra":  nil,
		"no uid":    {extraPodName: {shellPodName}},
		"two names": {extraPodName: {shellPodName, "other"}, extraPodUID: {shellPodUID}},
	} {
		t.Run(name, func(t *testing.T) {
			status := shellReview()
			status.User.Extra = extra
			fake := &fakeTokenReviewServer{status: status}
			a := newPodBoundAuthenticator(t, fake, nil)
			if _, err := a.AuthenticateRequest(requestFrom(shellPodIP+":51000", testCallerToken)); !errors.Is(err, errNoPodIdentity) {
				t.Errorf("err = %v, want errNoPodIdentity", err)
			}
			if got := fake.podCalls(); got != 0 {
				t.Errorf("pod GETs = %d, want 0", got)
			}
		})
	}
}

func TestPodBindingMatchesIPv6AndIPv4MappedPeers(t *testing.T) {
	pod := testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodRunning, shellPodIP, "2001:db8::5")
	for _, peer := range []string{"[2001:db8::5]:51000", "[::ffff:" + shellPodIP + "]:51000", shellPodIP + ":51000"} {
		t.Run(peer, func(t *testing.T) {
			fake := &fakeTokenReviewServer{status: shellReview(), pods: map[string]*corev1.Pod{shellPodNamespace + "/" + shellPodName: pod}}
			a := newPodBoundAuthenticator(t, fake, nil)
			if _, err := a.AuthenticateRequest(requestFrom(peer, testCallerToken)); err != nil {
				t.Errorf("refused: %v", err)
			}
		})
	}
	// status.podIP alone, when status.podIPs lists none.
	single := testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodRunning)
	single.Status.PodIP = "2001:db8::9"
	fake := &fakeTokenReviewServer{status: shellReview(), pods: map[string]*corev1.Pod{shellPodNamespace + "/" + shellPodName: single}}
	a := newPodBoundAuthenticator(t, fake, nil)
	if _, err := a.AuthenticateRequest(requestFrom("[2001:db8::9]:51000", testCallerToken)); err != nil {
		t.Errorf("status.podIP fallback refused: %v", err)
	}
	if _, err := a.AuthenticateRequest(requestFrom("[2001:db8::5]:51000", testCallerToken)); err == nil {
		t.Error("admitted an IPv6 peer that is not the pod's")
	}
}

// shellUser is the user a TokenReview of the shell's token yields.
func shellUser() user.Info {
	return &user.DefaultInfo{Name: shellUsername, Extra: map[string][]string{extraPodName: {shellPodName}, extraPodUID: {shellPodUID}}}
}

// countingGetter answers pod GETs from respond, counting them.
type countingGetter struct {
	calls   atomic.Int32
	respond func() (*corev1.Pod, error)
}

func (g *countingGetter) get(context.Context, string, string) (*corev1.Pod, error) {
	g.calls.Add(1)
	return g.respond()
}

func TestPodBindingCacheExpiresAndKeepsNoRefusals(t *testing.T) {
	g := &countingGetter{respond: func() (*corev1.Pod, error) { return shellPod(), nil }}
	b := newPodBinderWithGetter(g.get, 10*time.Second, slog.New(slog.DiscardHandler))
	now := time.Unix(1_800_000_000, 0)
	b.now = func() time.Time { return now }
	ctx := context.Background()
	peer := shellPodIP + ":51000"

	for range 3 {
		if err := b.check(ctx, peer, shellUser()); err != nil {
			t.Fatal(err)
		}
	}
	if got := g.calls.Load(); got != 1 {
		t.Fatalf("pod GETs within the TTL = %d, want 1", got)
	}
	// A different peer is refused from the cached addresses, without a GET.
	if err := b.check(ctx, otherPeer, shellUser()); !errors.Is(err, errPeerNotPod) {
		t.Fatalf("err = %v, want errPeerNotPod", err)
	}
	if got := g.calls.Load(); got != 1 {
		t.Errorf("pod GETs = %d, want 1", got)
	}

	now = now.Add(10 * time.Second)
	if err := b.check(ctx, peer, shellUser()); err != nil {
		t.Fatal(err)
	}
	if got := g.calls.Load(); got != 2 {
		t.Errorf("pod GETs after the TTL = %d, want 2", got)
	}

	// A refusal is not cached: it is looked up again an interval later, and a pod that then
	// reads correctly is admitted.
	gone := &countingGetter{respond: func() (*corev1.Pod, error) { return nil, apierrors.NewNotFound(podsResource, shellPodName) }}
	b = newPodBinderWithGetter(gone.get, 10*time.Second, slog.New(slog.DiscardHandler))
	b.now = func() time.Time { return now }
	for range 2 {
		if err := b.check(ctx, peer, shellUser()); err == nil {
			t.Fatal("admitted a pod that is gone")
		}
		now = now.Add(maxLookupInterval)
	}
	if got := gone.calls.Load(); got != 2 {
		t.Errorf("pod GETs for two refusals an interval apart = %d, want 2", got)
	}
	gone.respond = func() (*corev1.Pod, error) { return shellPod(), nil }
	if err := b.check(ctx, peer, shellUser()); err != nil {
		t.Errorf("the pod, readable again, refused: %v", err)
	}
	// A stopped pod is a refusal too, and is not cached either.
	stopped := &countingGetter{respond: func() (*corev1.Pod, error) {
		return testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodSucceeded, shellPodIP), nil
	}}
	b = newPodBinderWithGetter(stopped.get, 10*time.Second, slog.New(slog.DiscardHandler))
	b.now = func() time.Time { return now }
	for range 2 {
		if err := b.check(ctx, peer, shellUser()); err == nil {
			t.Fatal("admitted a stopped pod")
		}
		now = now.Add(maxLookupInterval)
	}
	if got := stopped.calls.Load(); got != 2 {
		t.Errorf("pod GETs for a stopped pod = %d, want 2", got)
	}
}

// userFor is the user a TokenReview yields for a token issued to the shell's pod by uid.
func userFor(uid string) user.Info {
	return &user.DefaultInfo{Name: shellUsername, Extra: map[string][]string{extraPodName: {shellPodName}, extraPodUID: {uid}}}
}

// A pod recreated under the same name has a new UID and, usually, a new address. The old
// pod's cached addresses are never used for the new pod's token, and the old token is not
// admitted from the old address once its pod is gone.
func TestPodBindingCacheIsKeyedByUID(t *testing.T) {
	const oldIP, newIP = "192.0.2.1", "192.0.2.2"
	current := testPod(shellPodNamespace, shellPodName, "uid-old", corev1.PodRunning, oldIP)
	g := &countingGetter{respond: func() (*corev1.Pod, error) { return current, nil }}
	b := newPodBinderWithGetter(g.get, 10*time.Second, slog.New(slog.DiscardHandler))
	ctx := context.Background()
	if err := b.check(ctx, oldIP+":51000", userFor("uid-old")); err != nil {
		t.Fatal(err)
	}
	current = testPod(shellPodNamespace, shellPodName, "uid-new", corev1.PodRunning, newIP)
	if err := b.check(ctx, oldIP+":51000", userFor("uid-new")); err == nil {
		t.Error("the new pod's token was admitted from the old pod's cached address")
	}
	if got := g.calls.Load(); got != 2 {
		t.Errorf("pod GETs = %d, want 2: the new UID must be read, not served from the old one's entry", got)
	}
	if err := b.check(ctx, newIP+":51000", userFor("uid-new")); err != nil {
		t.Errorf("the new pod's token from its own address: %v", err)
	}
}

// One request leaving does not fail the others waiting on the same GET.
func TestPodBindingLookupOutlivesAWaiterThatLeaves(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	b := newPodBinderWithGetter(func(ctx context.Context, _, _ string) (*corev1.Pod, error) {
		once.Do(func() { close(entered) })
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-release:
			return shellPod(), nil
		}
	}, 10*time.Second, slog.New(slog.DiscardHandler))
	peer := shellPodIP + ":51000"

	leaving, leave := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { first <- b.check(leaving, peer, shellUser()) }()
	<-entered
	second := make(chan error, 1)
	go func() { second <- b.check(context.Background(), peer, shellUser()) }()
	// Let the second request join the GET in flight before the first one leaves.
	time.Sleep(20 * time.Millisecond)
	leave()
	time.Sleep(20 * time.Millisecond)
	close(release)
	if err := <-second; err != nil {
		t.Errorf("the request still waiting was refused when another left: %v", err)
	}
	<-first
}

// status.podIPs is the pod's list of addresses; status.podIP is read only without it.
func TestPodAddressesPreferPodIPs(t *testing.T) {
	pod := testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodRunning, "192.0.2.1", "2001:db8::1")
	pod.Status.PodIP = "192.0.2.99"
	got := podAddresses(pod)
	if len(got) != 2 || got[0].String() != "192.0.2.1" || got[1].String() != "2001:db8::1" {
		t.Errorf("podAddresses = %v, want status.podIPs alone", got)
	}
	pod.Status.PodIPs = nil
	if got := podAddresses(pod); len(got) != 1 || got[0].String() != "192.0.2.99" {
		t.Errorf("podAddresses without podIPs = %v, want status.podIP", got)
	}
	pod.Status.PodIPs = []corev1.PodIP{{IP: "::ffff:192.0.2.5"}}
	if got := podAddresses(pod); len(got) != 1 || got[0].String() != "192.0.2.5" {
		t.Errorf("podAddresses = %v, want the IPv4-mapped address unmapped", got)
	}
}

// A flood of requests with a token whose pod is gone costs one GET an interval, and does
// not hold up another pod's lookup.
func TestPodBindingRefusalFloodCostsOneGetAnInterval(t *testing.T) {
	var goneGets, liveGets atomic.Int32
	b := newPodBinderWithGetter(func(_ context.Context, _, name string) (*corev1.Pod, error) {
		if name == "gone-0" {
			goneGets.Add(1)
			return nil, apierrors.NewNotFound(podsResource, name)
		}
		liveGets.Add(1)
		return shellPod(), nil
	}, 10*time.Second, slog.New(slog.DiscardHandler))
	now := time.Unix(1_800_000_000, 0)
	b.now = func() time.Time { return now }
	ctx := context.Background()
	gone := &user.DefaultInfo{Name: shellUsername, Extra: map[string][]string{extraPodName: {"gone-0"}, extraPodUID: {"uid-gone"}}}
	for range 500 {
		if err := b.check(ctx, shellPodIP+":51000", gone); err == nil {
			t.Fatal("admitted a token whose pod is gone")
		}
	}
	if got := goneGets.Load(); got != 1 {
		t.Errorf("pod GETs for 500 refused requests in one interval = %d, want 1", got)
	}
	if err := b.check(ctx, shellPodIP+":51000", shellUser()); err != nil {
		t.Errorf("another pod's lookup during the flood: %v", err)
	}
	if got := liveGets.Load(); got != 1 {
		t.Errorf("the other pod's GETs = %d, want 1", got)
	}
	now = now.Add(maxLookupInterval)
	_ = b.check(ctx, shellPodIP+":51000", gone)
	if got := goneGets.Load(); got != 2 {
		t.Errorf("pod GETs after an interval = %d, want 2", got)
	}
	// A matched pod is cached for longer than the interval, so it never meets it.
	if b.interval > b.ttl {
		t.Errorf("interval %s is longer than the pod TTL %s", b.interval, b.ttl)
	}
}

func TestPodBindingTTLIsBoundedByTheTokenCache(t *testing.T) {
	for _, tc := range []struct{ tokenTTL, want time.Duration }{
		{10 * time.Second, 10 * time.Second},
		{5 * time.Second, 5 * time.Second},
		{time.Minute, maxPodLookupTTL},
	} {
		b := newPodBinderWithGetter(nil, tc.tokenTTL, nil)
		if b.ttl != tc.want {
			t.Errorf("token cache TTL %s: pod TTL = %s, want %s", tc.tokenTTL, b.ttl, tc.want)
		}
	}
	if podLookupTimeout > tokenReviewTimeout {
		t.Errorf("pod lookup timeout %s is longer than the TokenReview's %s", podLookupTimeout, tokenReviewTimeout)
	}
}

func TestPodBindingSharesOneGetAcrossConcurrentLookups(t *testing.T) {
	const callers = 16
	release := make(chan struct{})
	g := &countingGetter{respond: func() (*corev1.Pod, error) {
		<-release
		return shellPod(), nil
	}}
	b := newPodBinderWithGetter(g.get, 10*time.Second, slog.New(slog.DiscardHandler))
	var started atomic.Int32
	var wg sync.WaitGroup
	errs := make(chan error, callers)
	for range callers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			started.Add(1)
			errs <- b.check(context.Background(), shellPodIP+":51000", shellUser())
		}()
	}
	for started.Load() < callers {
		time.Sleep(time.Millisecond)
	}
	// Give the last goroutines time to join the lookup in flight; one that arrives after it
	// ends reads the cache, so the count holds either way.
	time.Sleep(20 * time.Millisecond)
	close(release)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Errorf("refused: %v", err)
		}
	}
	if got := g.calls.Load(); got != 1 {
		t.Errorf("pod GETs for %d concurrent lookups = %d, want 1", callers, got)
	}
}

// Every route that takes a pod token goes through the one authenticator, so each holds the
// caller to its pod: a valid token from another address is a 401 that reaches no upstream,
// and from the pod it is past authentication. Only the unauthenticated health probe is not.
func TestPodBindingCoversEveryAuthenticatedRoute(t *testing.T) {
	routes := []struct{ method, path string }{
		{http.MethodGet, "/clusters/in-cluster/api/v1/pods"},
		{http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/pods/web-0"},
		{http.MethodGet, "/clusters/in-cluster/api/v1/pods?watch=true"},
		{http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/pods/web-0/log"},
		{http.MethodPost, "/clusters/in-cluster/api/v1/namespaces/default/pods/web-0/exec?command=ls"},
		{http.MethodPost, "/clusters/in-cluster/api/v1/namespaces/default/pods/web-0/portforward"},
		{http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/secrets"},
		{http.MethodPost, "/clusters/in-cluster/apis/apps/v1/namespaces/default/deployments?dryRun=All"},
		{http.MethodGet, "/clusters/in-cluster/api"},
		{http.MethodGet, "/clusters/in-cluster/apis"},
		{http.MethodGet, "/clusters/in-cluster/version"},
		{http.MethodGet, "/clusters/in-cluster/openapi/v3"},
		{http.MethodGet, "/clusters/gke_project_us-central1_other/api/v1/pods"},
		{http.MethodGet, "/api/v1/pods"}, // the default cluster, with DefaultClusterID set
	}
	fake := &fakeTokenReviewServer{status: shellReview()}
	a := newPodBoundAuthenticator(t, fake, nil)
	upstream := &recordingUpstream{}
	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator:    a,
		Upstream:         upstream,
		Policy:           k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:           slog.New(slog.DiscardHandler),
		DefaultClusterID: k8sapiproxy.InClusterID,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, route := range routes {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			wrong := httptest.NewRequest(route.method, route.path, nil)
			wrong.Header.Set("Authorization", "Bearer "+testCallerToken)
			wrong.RemoteAddr = otherPeer
			upstream.req = nil
			if rec := serve(h, wrong); rec.Code != http.StatusUnauthorized {
				t.Errorf("from another address: code = %d, want 401", rec.Code)
			}
			if upstream.req != nil {
				t.Error("from another address: reached the upstream")
			}
			right := httptest.NewRequest(route.method, route.path, nil)
			right.Header.Set("Authorization", "Bearer "+testCallerToken)
			right.RemoteAddr = shellPodIP + ":51000"
			if rec := serve(h, right); rec.Code == http.StatusUnauthorized || rec.Code == http.StatusServiceUnavailable {
				t.Errorf("from the pod: code = %d, want past authentication", rec.Code)
			}
		})
	}
	health := httptest.NewRequest(http.MethodGet, k8sapiproxy.HealthzPath, nil)
	health.RemoteAddr = otherPeer
	if rec := serve(h, health); rec.Code != http.StatusOK {
		t.Errorf("health probe: code = %d, want 200", rec.Code)
	}
}
