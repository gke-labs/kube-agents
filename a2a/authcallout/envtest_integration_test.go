package authcallout

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
	authnv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

// The same suite as integration_test.go, against a real Kubernetes API server.
//
// What the fake-clientset suite cannot reach, and this can:
//
//   - **The audience binding, non-circularly.** The property that stops every
//     readable ServiceAccount token in the cluster from being a bus credential
//     is currently asserted against a stub that this package also wrote, which
//     proves the validator agrees with itself. Here the tokens are minted by
//     the API server's TokenRequest endpoint and validated by its TokenReview
//     endpoint, so "a token for another audience is refused" is a fact about
//     Kubernetes rather than about the stub.
//   - **The real TokenReview request shape** — that the audiences field is
//     honoured, and that the username comes back in the form the map is keyed
//     on rather than the form this package assumed.
//   - **The informer's real path.** The reflector prefers the streaming
//     WatchList protocol, which the fake clientset does not implement, so the
//     other suite disables it and tests the fallback. A real API server serves
//     it, so this is the only place the production path runs.
//   - **The callout's own RBAC**, exercised as the callout rather than as an
//     admin: it does the TokenReview through its own ServiceAccount.
//
// A pod is a token delivery mechanism, not the thing under test. What is under
// test is token to TokenReview to grants to a publish that is accepted or
// refused, and none of that needs a kubelet.
//
// Skipped without KUBEBUILDER_ASSETS; `make test` in k8s-operator installs the
// binaries, and the a2a workflow runs without them.

const (
	envtestNamespace = "kubeagents-system"
	busAudience      = "a2a-bus"

	// The ServiceAccounts the operator's rendered map is keyed on. Taken from
	// the fixture rather than invented, so a rename in the render fails here.
	provisionSAName = "agent-a2a-provision"
	// secondSAName holds no operator-rendered principal. It exists so the
	// per-identity resolution can be measured with two accounts while the
	// operator renders one; the map entry for it is added by the test.
	secondSAName   = "agent-second-principal"
	strangerSAName = "not-in-the-map"
)

type liveHarness struct {
	k8s       kubernetes.Interface
	namespace string
	nats      *natsserver.Server
	store     *Store
	status    *httptest.Server
	svcConn   *nats.Conn
}

func startLiveHarness(t *testing.T) *liveHarness {
	t.Helper()
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS is unset; install the envtest binaries with " +
			"`make -C ../k8s-operator setup-envtest` from a2a/, then set it from " +
			"`make -C ../k8s-operator -s envtest-path`")
	}

	env := &envtest.Environment{}
	cfg, err := env.Start()
	if err != nil {
		t.Fatalf("starting the API server: %v", err)
	}
	t.Cleanup(func() { _ = env.Stop() })

	admin, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatalf("admin client: %v", err)
	}
	ctx := context.Background()

	if _, err := admin.CoreV1().Namespaces().Create(ctx,
		&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: envtestNamespace}}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("namespace: %v", err)
	}
	for _, sa := range []string{provisionSAName, secondSAName, strangerSAName, "a2a-callout"} {
		if _, err := admin.CoreV1().ServiceAccounts(envtestNamespace).Create(ctx,
			&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: sa}}, metav1.CreateOptions{}); err != nil {
			t.Fatalf("serviceaccount %s: %v", sa, err)
		}
	}

	// The identity map, as the operator renders it, in the object the callout
	// watches.
	rawMap, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("reading the rendered identity map: %v", err)
	}
	if _, err := admin.CoreV1().ConfigMaps(envtestNamespace).Create(ctx, &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "agent-a2a-authmap", Namespace: envtestNamespace},
		Data:       map[string]string{"identities.json": string(rawMap)},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("identity map ConfigMap: %v", err)
	}

	// The callout's real grants, so the TokenReview below is authorized as the
	// callout rather than as an admin.
	if _, err := admin.RbacV1().ClusterRoleBindings().Create(ctx, &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "a2a-callout-tokenreview"},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "ClusterRole", Name: "system:auth-delegator"},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: "a2a-callout", Namespace: envtestNamespace}},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("callout ClusterRoleBinding: %v", err)
	}
	if _, err := admin.RbacV1().Roles(envtestNamespace).Create(ctx, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Name: "a2a-callout", Namespace: envtestNamespace},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{""}, Resources: []string{"configmaps"},
			ResourceNames: []string{"agent-a2a-authmap"}, Verbs: []string{"get", "list", "watch"},
		}},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("callout Role: %v", err)
	}
	if _, err := admin.RbacV1().RoleBindings(envtestNamespace).Create(ctx, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "a2a-callout", Namespace: envtestNamespace},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: "a2a-callout"},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: "a2a-callout", Namespace: envtestNamespace}},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("callout RoleBinding: %v", err)
	}

	// The callout's own client, impersonating its ServiceAccount so its RBAC
	// is what authorizes the TokenReview and the informer.
	asCallout := rest.CopyConfig(cfg)
	asCallout.Impersonate = rest.ImpersonationConfig{
		UserName: "system:serviceaccount:" + envtestNamespace + ":a2a-callout",
	}
	calloutClient, err := kubernetes.NewForConfig(asCallout)
	if err != nil {
		t.Fatalf("callout client: %v", err)
	}

	// The bus, from the operator's rendered config.
	issuerKP, _ := nkeys.CreateAccount()
	issuerSeed, _ := issuerKP.Seed()
	issuerPub, _ := issuerKP.PublicKey()
	xkeyKP, _ := nkeys.CreateCurveKeys()
	xkeySeed, _ := xkeyKP.Seed()
	xkeyPub, _ := xkeyKP.PublicKey()

	rendered, err := os.ReadFile("testdata/rendered-nats.conf")
	if err != nil {
		t.Fatalf("reading the rendered nats.conf: %v", err)
	}
	conf := replaceConfKey(string(rendered), "issuer: ", issuerPub)
	conf = replaceConfKey(conf, "xkey: ", xkeyPub)

	confPath := t.TempDir() + "/nats.conf"
	if err := os.WriteFile(confPath, []byte(conf), 0o600); err != nil {
		t.Fatalf("writing nats.conf: %v", err)
	}
	opts, err := natsserver.ProcessConfigFile(confPath)
	if err != nil {
		t.Fatalf("the rendered nats.conf was refused: %v", err)
	}
	opts.NoLog, opts.NoSigs = true, true
	opts.Port, opts.Websocket.Port = -1, -1
	opts.StoreDir = t.TempDir()
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(20 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)

	// The callout, against the real API server, with the real informer — no
	// WatchList feature gate disabled here, unlike the fake-client suite.
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := NewStore(log)
	ctxWatch, cancel := context.WithCancel(ctx)
	t.Cleanup(cancel)
	go func() {
		_ = store.WatchConfigMap(ctxWatch, calloutClient, envtestNamespace, "agent-a2a-authmap", "identities.json")
	}()
	if err := store.WaitForMap(ctx, 30*time.Second); err != nil {
		t.Fatalf("the informer never delivered the map from a real API server: %v", err)
	}

	validator, err := NewTokenValidator(calloutClient, busAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	svc, err := NewService(store, validator, Config{
		IssuerSeed: string(issuerSeed),
		XKeySeed:   string(xkeySeed),
	}, log)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	svcConn, err := nats.Connect(srv.ClientURL(), nats.UserInfo(svcUser, svcPass), nats.Name("auth-callout"))
	if err != nil {
		t.Fatalf("the callout could not connect: %v", err)
	}
	t.Cleanup(svcConn.Close)
	if _, err := svc.Subscribe(svcConn); err != nil {
		t.Fatalf("Subscribe: %v", err)
	}

	// The readiness probe reads the real connection, not a stand-in, so the
	// live tests below exercise both halves of it against a real server.
	status := httptest.NewServer(StatusHandler(store, svcConn.IsConnected))
	t.Cleanup(status.Close)

	return &liveHarness{
		k8s: admin, namespace: envtestNamespace, nats: srv,
		store: store, status: status, svcConn: svcConn,
	}
}

// mintToken asks the API server for a real, signed, audience-bound token for a
// ServiceAccount — the same object a projected volume would deliver into a pod.
func (h *liveHarness) mintToken(t *testing.T, sa string, audiences ...string) string {
	t.Helper()
	tr, err := h.k8s.CoreV1().ServiceAccounts(h.namespace).CreateToken(context.Background(), sa,
		&authnv1.TokenRequest{Spec: authnv1.TokenRequestSpec{Audiences: audiences}},
		metav1.CreateOptions{})
	if err != nil {
		t.Fatalf("minting a token for %s: %v", sa, err)
	}
	return tr.Status.Token
}

func (h *liveHarness) connect(t *testing.T, user, token string) (*nats.Conn, chan error) {
	t.Helper()
	violations := make(chan error, 16)
	nc, err := nats.Connect(h.nats.ClientURL(),
		nats.Token(token),
		nats.CustomInboxPrefix("_INBOX."+user),
		nats.Name(user),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, e error) { violations <- e }),
	)
	if err != nil {
		t.Fatalf("%s could not connect with a real ServiceAccount token: %v", user, err)
	}
	t.Cleanup(nc.Close)
	return nc, violations
}

// editMap rewrites the served identity map through the API server and blocks
// until the callout's informer has delivered the new version, so a connection
// made after it returns is answered from the edited map rather than racing it.
//
// It goes through ParseIdentityMap and the production types rather than editing
// the JSON as text: an edit that this package can no longer represent is a
// divergence from the operator's shape, and it should fail here rather than
// quietly serve something the callout will refuse at runtime.
func (h *liveHarness) editMap(t *testing.T, fn func(*IdentityMap)) {
	t.Helper()
	before := h.store.Version()

	cm, err := h.k8s.CoreV1().ConfigMaps(h.namespace).Get(context.Background(), "agent-a2a-authmap", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get map: %v", err)
	}
	m, err := ParseIdentityMap([]byte(cm.Data["identities.json"]))
	if err != nil {
		t.Fatalf("the served map does not parse: %v", err)
	}
	fn(m)
	m.Version = before + "-edited"

	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatalf("marshal map: %v", err)
	}
	cm.Data["identities.json"] = string(raw)
	if _, err := h.k8s.CoreV1().ConfigMaps(h.namespace).Update(context.Background(), cm, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("update map: %v", err)
	}

	deadline := time.Now().Add(30 * time.Second)
	for h.store.Version() == before {
		if time.Now().After(deadline) {
			t.Fatalf("the informer did not deliver the edit; still serving %q", before)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// DoD, with a token the cluster actually minted and actually validated.
func TestLiveARealServiceAccountTokenGetsItsMappedGrants(t *testing.T) {
	h := startLiveHarness(t)
	nc, violations := h.connect(t, "provision", h.mintToken(t, provisionSAName, busAudience))

	if publishRefused(t, nc, violations, "a2a.topics.shared.blueprint") {
		t.Error("provision was refused a starter topic its map entry grants")
	}
	if !publishRefused(t, nc, violations, "a2a.tasks.platform.t1.in") {
		t.Error("provision reached the task plane; its principal grants none of it")
	}
	if !nc.IsConnected() {
		t.Error("the connection closed on a permissions violation")
	}
}

// Two distinct cluster identities resolve to two distinct grant sets. This is
// the callout's reason to exist, so it is measured rather than reasoned.
//
// The operator renders one callout principal on this branch — `provision`; the
// second arrives with the session principal in the follow-on — so the second
// entry here is added to the served map rather than rendered into it. It is
// built by copying the operator's own entry and changing the two fields that
// make it a different identity, so the shape under test is the operator's even
// though the content is not: a field this package expects and the operator
// stops rendering still fails, in the contract test next door.
func TestLiveTwoServiceAccountsGetDifferentGrants(t *testing.T) {
	h := startLiveHarness(t)

	// The second identity: the same grants plus the heartbeat subject, keyed
	// on a different ServiceAccount.
	h.editMap(t, func(m *IdentityMap) {
		var extra Identity
		for _, id := range m.Identities {
			if id.User == "provision" {
				extra = id
			}
		}
		if extra.User == "" {
			t.Fatal("no provision principal in the operator's rendered map to copy")
		}
		extra.User = "second"
		extra.ServiceAccount = "system:serviceaccount:" + h.namespace + ":" + secondSAName
		extra.Grants.Publish = append(slices.Clone(extra.Grants.Publish), "agents.hb.>", "_INBOX.second.>")
		extra.Grants.Subscribe = append(slices.Clone(extra.Grants.Subscribe), "_INBOX.second.>")
		m.Identities = append(m.Identities, extra)
	})

	provision, provisionViolations := h.connect(t, "provision", h.mintToken(t, provisionSAName, busAudience))
	second, secondViolations := h.connect(t, "second", h.mintToken(t, secondSAName, busAudience))

	// Both are granted the provisioned topics. Without this the two refusals
	// below would be consistent with a second identity that authorises
	// nothing at all.
	if publishRefused(t, provision, provisionViolations, "a2a.topics.shared.annotations") {
		t.Error("provision was refused a granted topic")
	}
	if publishRefused(t, second, secondViolations, "a2a.topics.shared.annotations") {
		t.Error("the second identity was refused a granted topic")
	}
	// Only the second publishes heartbeats. Two distinct grant sets, resolved
	// from two distinct cluster identities on one bus.
	if publishRefused(t, second, secondViolations, "agents.hb.claude-code.owner.session") {
		t.Error("the second identity was refused the heartbeat subject its entry grants")
	}
	if !publishRefused(t, provision, provisionViolations, "agents.hb.claude-code.owner.session") {
		t.Error("provision reached the heartbeat plane; the two grant sets are not distinct")
	}
}

// The audience binding, proven against Kubernetes rather than against a stub
// this package also wrote.
//
// Without it, a TokenReview validates against the API server's own audience,
// which every ordinary pod's default ServiceAccount token carries — so any
// token readable anywhere in the cluster would authenticate to the bus as
// whoever it belongs to. This is the single assertion that says otherwise, and
// until it ran against a real issuer it was circular.
func TestLiveATokenForAnotherAudienceIsRefused(t *testing.T) {
	h := startLiveHarness(t)

	cases := []struct {
		name      string
		audiences []string
	}{
		{
			// THE case. Minting with no audience yields the API server's
			// own default — which is exactly what every ordinary pod's
			// default ServiceAccount token carries. If the bus accepted
			// this, any token readable anywhere in the cluster would be a
			// bus credential for whoever it belongs to.
			//
			// It has to be discovered from the server rather than written
			// down: an earlier version of this test named a plausible
			// default as a literal, which envtest does not use, so the
			// token was refused for the wrong reason and the test passed
			// with the audience binding deleted outright. Asking for the
			// default is what makes it a fact about Kubernetes.
			name:      "the API server's own default audience, as every pod's default token carries",
			audiences: nil,
		},
		{
			name:      "an unrelated service's audience",
			audiences: []string{"some-other-service"},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			token := h.mintToken(t, provisionSAName, tc.audiences...)
			nc, err := nats.Connect(h.nats.ClientURL(), nats.Token(token), nats.Name("wrong-audience"))
			if err == nil {
				nc.Close()
				t.Fatalf("a token minted for audiences %v authenticated to the bus", tc.audiences)
			}
			if !strings.Contains(err.Error(), "Authorization Violation") {
				t.Errorf("connect error = %v, want an Authorization Violation", err)
			}
		})
	}

	// The control: the same ServiceAccount, correctly audienced, connects. It
	// is what stops the two cases above passing because the identity is
	// unusable for some unrelated reason.
	t.Run("the control: the same identity with the right audience connects", func(t *testing.T) {
		nc, err := nats.Connect(h.nats.ClientURL(),
			nats.Token(h.mintToken(t, provisionSAName, busAudience)),
			nats.CustomInboxPrefix("_INBOX.provision"), nats.Name("provision"))
		if err != nil {
			t.Fatalf("the correctly-audienced token was refused: %v", err)
		}
		nc.Close()
	})
}

// A real, valid, correctly-audienced token for a ServiceAccount this deployment
// has no entry for. The cluster vouches for it and the bus still refuses it.
func TestLiveAnUnmappedServiceAccountIsRefused(t *testing.T) {
	h := startLiveHarness(t)
	token := h.mintToken(t, strangerSAName, busAudience)

	nc, err := nats.Connect(h.nats.ClientURL(), nats.Token(token), nats.Name("stranger"))
	if err == nil {
		nc.Close()
		t.Fatal("a ServiceAccount absent from the identity map connected")
	}
	if !strings.Contains(err.Error(), "Authorization Violation") {
		t.Errorf("connect error = %v, want an Authorization Violation", err)
	}
}

// The map version, read off the running callout rather than off the render.
func TestLiveTheServedMapVersionIsObservable(t *testing.T) {
	h := startLiveHarness(t)

	resp, err := http.Get(h.status.URL + StatusPath)
	if err != nil {
		t.Fatalf("GET %s: %v", StatusPath, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	// The version the operator rendered into the fixture, which is what the
	// operator would be comparing against.
	rendered, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("reading the rendered map: %v", err)
	}
	want, err := ParseIdentityMap(rendered)
	if err != nil {
		t.Fatalf("parsing the rendered map: %v", err)
	}
	if !strings.Contains(string(body), want.Version) {
		t.Errorf("status does not report the rendered version %q:\n%s", want.Version, body)
	}
	for _, user := range want.Users() {
		if !strings.Contains(string(body), user) {
			t.Errorf("status does not name the served user %q", user)
		}
	}

	ready, err := http.Get(h.status.URL + ReadyPath)
	if err != nil {
		t.Fatalf("GET %s: %v", ReadyPath, err)
	}
	defer ready.Body.Close()
	if ready.StatusCode != http.StatusOK {
		t.Errorf("readiness = %d while serving a map, want 200", ready.StatusCode)
	}
	readyBody, _ := io.ReadAll(ready.Body)
	if !strings.Contains(string(readyBody), want.Version) {
		t.Errorf("readiness body %q does not name the served version", readyBody)
	}
}

// The informer against a real API server, on the WatchList path the fake
// clientset could not serve: a map edited in the API server reaches the callout,
// and the grants a new connection receives change with it.
func TestLiveAMapEditReachesTheCalloutAndChangesNewGrants(t *testing.T) {
	h := startLiveHarness(t)
	before := h.store.Version()

	cm, err := h.k8s.CoreV1().ConfigMaps(h.namespace).Get(context.Background(), "agent-a2a-authmap", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get map: %v", err)
	}
	// Narrow provision: drop one of its starter-topic publishes.
	cm.Data["identities.json"] = strings.Replace(cm.Data["identities.json"],
		"\"a2a.topics.shared.annotations\",\n", "", 1)
	cm.Data["identities.json"] = strings.Replace(cm.Data["identities.json"],
		before, before+"-narrowed", 1)
	if _, err := h.k8s.CoreV1().ConfigMaps(h.namespace).Update(context.Background(), cm, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("update map: %v", err)
	}

	deadline := time.Now().Add(30 * time.Second)
	for h.store.Version() == before {
		if time.Now().After(deadline) {
			t.Fatalf("the informer did not deliver the edit; still serving %q", before)
		}
		time.Sleep(50 * time.Millisecond)
	}

	nc, violations := h.connect(t, "provision", h.mintToken(t, provisionSAName, busAudience))
	if !publishRefused(t, nc, violations, "a2a.topics.shared.annotations") {
		t.Error("a connection made after the narrowing still holds the removed grant")
	}
}

// The state readiness exists to report and did not: a callout holding a
// perfectly good map with no connection to the bus. It answers no
// authorization request, so every non-exempt client is refused — and until the
// probe read the connection, the pod stayed in the Service through all of it,
// its Deployment stayed Available, and BusCredentialsReady stayed True.
//
// Measured against a real nats-server rather than a stub, because the claim is
// about what the connection does, not about a boolean.
func TestLiveReadinessFailsWhenTheCalloutIsDetachedFromTheBus(t *testing.T) {
	h := startLiveHarness(t)

	// Precondition, and the control: the same probe with the same map says
	// ready while the connection is up. Without it, the 503 below is
	// consistent with a probe that never says ready at all.
	if code, _ := getStatus(t, h.status.URL+ReadyPath); code != http.StatusOK {
		t.Fatalf("precondition: readiness = %d with a map and a live connection, want 200", code)
	}

	h.svcConn.Close()
	if h.svcConn.IsConnected() {
		t.Fatal("the callout connection is still up after Close")
	}

	code, body := getStatus(t, h.status.URL+ReadyPath)
	switch {
	case code == http.StatusOK:
		t.Error("readiness = 200 with the bus connection closed; the pod stays in the Service " +
			"answering no authorization request, and nothing on the cluster says so")
	case !strings.Contains(body, "bus"):
		t.Errorf("readiness body %q does not say the bus is what is wrong; a missing map "+
			"produces the same symptom for every client and is fixed somewhere else", body)
	}

	// The map is still there, which is the point: the two failures are
	// independent, and only one of them was represented.
	if !h.store.Ready() {
		t.Error("precondition lost: the store stopped serving its map, so the 503 above " +
			"proves nothing about the connection")
	}
}

func getStatus(t *testing.T, url string) (int, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}
