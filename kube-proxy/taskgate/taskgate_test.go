package taskgate

import (
	"bytes"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testWorker    = "system:serviceaccount:agents:auditor-read"
	testNonWorker = "system:serviceaccount:kubeagents-system:platform-agent"
	testTask      = "task-1"
	testProfile   = "auditor"
	testTier      = "read"
	testPod       = "auditor-read-x7k2p"
	testPodUID    = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
	testProject   = "proj-a"
	testLocation  = "us-central1"
	testCluster   = "cluster-a"
	testClusterID = "gke_proj-a_us-central1_cluster-a"
	testOtherID   = "gke_proj-a_us-central1_cluster-b"
	testPodsPath  = "/api/v1/namespaces/default/pods"
	testLifetime  = time.Hour
	testTTL       = 10 * time.Minute
	// testMinSegment is the shortest piece of a credential the leak check looks for.
	testMinSegment = 16
)

func newTestKey(t *testing.T) (nkeys.KeyPair, string) {
	t.Helper()
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatal(err)
	}
	return kp, pub
}

// testClaims is a credential for testWorker's pod on testClusterID.
func testClaims() taskcred.Claims {
	return taskcred.Claims{
		Task:     testTask,
		Profile:  testProfile,
		Tier:     testTier,
		Pod:      testPod,
		PodUID:   testPodUID,
		Audience: taskcred.AudienceCredentialProxy,
		Expires:  time.Now().Add(testTTL),
		Target:   &taskcred.Target{Project: testProject, Location: testLocation, Cluster: testCluster},
	}
}

func mintTest(t *testing.T, kp nkeys.KeyPair, c taskcred.Claims) string {
	t.Helper()
	credential, err := taskcred.Mint(kp, c)
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}
	return credential
}

func testExtra() map[string][]string {
	return map[string][]string{podNameExtra: {testPod}, podUIDExtra: {testPodUID}}
}

func testIdentities() map[string]Identity {
	return map[string]Identity{testWorker: {Profile: testProfile, Tier: testTier}}
}

// testAttrs is a request from username to clusterID carrying each of credentials in a
// header of its own.
func testAttrs(username, clusterID string, extra map[string][]string, credentials ...string) k8sapiproxy.AuthorizeAttributes {
	req := httptest.NewRequest(http.MethodGet, "/clusters/"+clusterID+testPodsPath, nil)
	for _, credential := range credentials {
		req.Header.Add(CredentialHeader, credential)
	}
	return k8sapiproxy.AuthorizeAttributes{
		Request:   req,
		Caller:    &k8sapiproxy.CallerIdentity{Username: username, Extra: extra},
		ClusterID: clusterID,
	}
}

// assertRefused checks err is a refusal with reason and the reason's fixed message, and
// that neither it nor the message holds any part of credentials.
func assertRefused(t *testing.T, err error, reason k8sapiproxy.RejectionReason, credentials ...string) {
	t.Helper()
	if err == nil {
		t.Fatalf("admitted, want a %s refusal", reason)
	}
	var authz *k8sapiproxy.AuthorizeError
	if !errors.As(err, &authz) || authz == nil {
		t.Fatalf("err = %v, want one wrapping an AuthorizeError", err)
	}
	if authz.Reason != reason {
		t.Errorf("reason = %q, want %q (detail: %v)", authz.Reason, reason, err)
	}
	wantMessage := messageTaskCredential
	if reason == ReasonTaskTarget {
		wantMessage = messageTaskTarget
	}
	if authz.Message != wantMessage {
		t.Errorf("message = %q, want the fixed %q", authz.Message, wantMessage)
	}
	if len(err.Error()) > detailLimit {
		t.Errorf("detail is %d bytes, want at most %d", len(err.Error()), detailLimit)
	}
	for _, credential := range credentials {
		assertNoCredentialText(t, err.Error(), credential)
		assertNoCredentialText(t, authz.Message, credential)
	}
}

// assertNoCredentialText fails when text holds the credential or one of its segments.
func assertNoCredentialText(t *testing.T, text, credential string) {
	t.Helper()
	for _, piece := range append([]string{credential}, strings.Split(credential, ".")...) {
		if len(piece) >= testMinSegment && strings.Contains(text, piece) {
			t.Errorf("%q holds credential text %q", text, piece)
		}
	}
}

func TestNonWorkerPassesUntouched(t *testing.T) {
	kp, pub := newTestKey(t)
	var logs bytes.Buffer
	hook := New(Config{Identities: testIdentities(), SigningKeys: []string{pub}, MaxLifetime: testLifetime},
		slog.New(slog.NewJSONHandler(&logs, nil)), nil)
	valid := mintTest(t, kp, testClaims())

	for name, credentials := range map[string][]string{
		"no header":         nil,
		"a valid header":    {valid},
		"a garbage header":  {"not-a-credential"},
		"two headers":       {valid, valid},
		"a blank header":    {"  "},
		"an over-long one":  {strings.Repeat("a", maxCredentialBytes+1)},
		"a non-ASCII value": {"crédential"},
	} {
		for _, clusterID := range []string{testClusterID, testOtherID, k8sapiproxy.InClusterID} {
			if err := hook(testAttrs(testNonWorker, clusterID, nil, credentials...)); err != nil {
				t.Errorf("%s to %s: non-worker refused: %v", name, clusterID, err)
			}
		}
	}
	if logs.Len() != 0 {
		t.Errorf("a non-worker request logged: %s", logs.String())
	}
}

func TestWorkerOnTheMatchingClusterIsAdmitted(t *testing.T) {
	kp, pub := newTestKey(t)
	var logs bytes.Buffer
	hook := New(Config{Identities: testIdentities(), SigningKeys: []string{pub}, MaxLifetime: testLifetime},
		slog.New(slog.NewJSONHandler(&logs, nil)), nil)
	credential := mintTest(t, kp, testClaims())
	claims, err := taskcred.Verify(credential, []string{pub}, taskcred.AudienceCredentialProxy, time.Now(), testLifetime, taskcred.Skew)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}

	// A value with surrounding whitespace is the same credential, as at the broker.
	for _, sent := range []string{credential, " " + credential + " "} {
		if err := hook(testAttrs(testWorker, testClusterID, testExtra(), sent)); err != nil {
			t.Fatalf("worker on its task's cluster refused: %v", err)
		}
	}
	logged := logs.String()
	for _, want := range []string{
		`"msg":"` + admittedMessage + `"`,
		`"level":"INFO"`,
		`"task":"` + testTask + `"`,
		`"jti":"` + claims.ID + `"`,
		`"principal":"` + testWorker + ` (caller task ` + testTask + ` jti ` + claims.ID + `)"`,
		`"cluster":"` + testClusterID + `"`,
	} {
		if !strings.Contains(logged, want) {
			t.Errorf("admit log = %s, want it to hold %s", logged, want)
		}
	}
	assertNoCredentialText(t, logged, credential)
}

func TestWorkerRefusals(t *testing.T) {
	kp, pub := newTestKey(t)
	otherKP, _ := newTestKey(t)
	valid := mintTest(t, kp, testClaims())
	with := func(edit func(*taskcred.Claims)) string {
		c := testClaims()
		edit(&c)
		return mintTest(t, kp, c)
	}
	reportOnly := func(c *taskcred.Claims) {
		c.Tier = taskcred.ReportTier
		c.Target = nil
		c.Trigger = &taskcred.Trigger{Name: "stall-watch", Origin: taskcred.TriggerOriginBuiltin}
	}

	for _, tc := range []struct {
		name        string
		credentials []string
		clusterID   string
		extra       map[string][]string
		identities  map[string]Identity
		reason      k8sapiproxy.RejectionReason
		detail      string
	}{
		{name: "no header", reason: ReasonTaskCredential, detail: "no task credential"},
		{name: "a blank header", credentials: []string{"   "}, reason: ReasonTaskCredential, detail: "no task credential"},
		{name: "two headers", credentials: []string{valid, valid}, reason: ReasonTaskCredential, detail: "more than one"},
		{name: "not a credential", credentials: []string{"not-a-credential"}, reason: ReasonTaskCredential},
		{name: "over-long", credentials: []string{valid + strings.Repeat("A", maxCredentialBytes)}, reason: ReasonTaskCredential, detail: "too long"},
		{name: "not ASCII", credentials: []string{valid + "é"}, reason: ReasonTaskCredential, detail: "not ASCII"},
		{
			name:        "tampered",
			credentials: []string{valid[:len(valid)-4] + "AAAA"},
			reason:      ReasonTaskCredential,
		},
		{
			name:        "expired",
			credentials: []string{with(func(c *taskcred.Claims) { c.Expires = time.Now().Add(-2 * time.Minute) })},
			reason:      ReasonTaskCredential,
			detail:      "expired",
		},
		{
			name:        "over the lifetime ceiling",
			credentials: []string{with(func(c *taskcred.Claims) { c.Expires = time.Now().Add(2 * testLifetime) })},
			reason:      ReasonTaskCredential,
			detail:      "ceiling",
		},
		{
			name: "another audience",
			credentials: []string{with(func(c *taskcred.Claims) {
				c.Audience = taskcred.AudienceNATS
				c.Target = nil
			})},
			reason: ReasonTaskCredential,
			detail: string(taskcred.AudienceNATS),
		},
		{
			name:        "an untrusted issuer",
			credentials: []string{mintTest(t, otherKP, testClaims())},
			reason:      ReasonTaskCredential,
			detail:      "not a trusted task-signing key",
		},
		{
			name:        "another pod name",
			credentials: []string{with(func(c *taskcred.Claims) { c.Pod = "auditor-read-other" })},
			reason:      ReasonTaskCredential,
			detail:      "another pod (jti",
		},
		{
			name:        "another pod UID",
			credentials: []string{with(func(c *taskcred.Claims) { c.PodUID = "ffffffff-4f50-6172-8394-a5b6c7d8e9f0" })},
			reason:      ReasonTaskCredential,
			detail:      "another pod UID",
		},
		{
			name:        "another profile",
			credentials: []string{with(func(c *taskcred.Claims) { c.Profile = "fixer" })},
			reason:      ReasonTaskCredential,
			detail:      "another profile",
		},
		{
			name:        "another tier",
			credentials: []string{with(func(c *taskcred.Claims) { c.Tier = "write" })},
			reason:      ReasonTaskCredential,
			detail:      "another tier",
		},
		{
			name:        "the TokenReview names no pod",
			credentials: []string{valid},
			extra:       map[string][]string{},
			reason:      ReasonTaskCredential,
			detail:      "another pod",
		},
		{
			name:        "the TokenReview names no pod UID",
			credentials: []string{valid},
			extra:       map[string][]string{podNameExtra: {testPod}},
			reason:      ReasonTaskCredential,
			detail:      "another pod UID",
		},
		{
			name:        "the TokenReview names two pods",
			credentials: []string{valid},
			extra:       map[string][]string{podNameExtra: {testPod, "auditor-read-other"}, podUIDExtra: {testPodUID}},
			reason:      ReasonTaskCredential,
			detail:      "another pod",
		},
		{
			name:        "a report-only credential",
			credentials: []string{with(reportOnly)},
			identities:  map[string]Identity{testWorker: {Profile: testProfile, Tier: taskcred.ReportTier}},
			reason:      ReasonTaskCredential,
			detail:      "report credential",
		},
		{
			name:        "a target that is not a GKE cluster name",
			credentials: []string{with(func(c *taskcred.Claims) { c.Target.Cluster = "cluster_a" })},
			reason:      ReasonTaskCredential,
			detail:      "not a GKE cluster name",
		},
		{
			name:        "another cluster",
			credentials: []string{valid},
			clusterID:   testOtherID,
			reason:      ReasonTaskTarget,
			detail:      testOtherID,
		},
		{
			// Rendered naively, this target would equal the request's cluster ID.
			name: "a target whose components shift across the separator",
			credentials: []string{with(func(c *taskcred.Claims) {
				c.Target = &taskcred.Target{Project: "proj-a_us", Location: "central1", Cluster: testCluster}
			})},
			clusterID: "gke_proj-a_us_central1_cluster-a",
			reason:    ReasonTaskCredential,
			detail:    "not a GKE cluster name",
		},
		{
			name:        "the default cluster",
			credentials: []string{valid},
			clusterID:   k8sapiproxy.InClusterID,
			reason:      ReasonTaskTarget,
			detail:      k8sapiproxy.InClusterID,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			identities := tc.identities
			if identities == nil {
				identities = testIdentities()
			}
			var logs bytes.Buffer
			hook := New(Config{Identities: identities, SigningKeys: []string{pub}, MaxLifetime: testLifetime},
				slog.New(slog.NewJSONHandler(&logs, nil)), nil)
			clusterID := tc.clusterID
			if clusterID == "" {
				clusterID = testClusterID
			}
			extra := tc.extra
			if extra == nil {
				extra = testExtra()
			}
			err := hook(testAttrs(testWorker, clusterID, extra, tc.credentials...))
			assertRefused(t, err, tc.reason, tc.credentials...)
			if err != nil && !strings.Contains(err.Error(), tc.detail) {
				t.Errorf("detail = %q, want it to hold %q", err.Error(), tc.detail)
			}
			if logs.Len() != 0 {
				t.Errorf("a refusal logged: %s", logs.String())
			}
		})
	}
}

// With workers named and no signing key, every worker is refused and everyone else passes.
func TestNoSigningKeyRefusesEveryWorker(t *testing.T) {
	kp, _ := newTestKey(t)
	hook := New(Config{Identities: testIdentities(), MaxLifetime: testLifetime}, slog.New(slog.DiscardHandler), nil)
	credential := mintTest(t, kp, testClaims())

	assertRefused(t, hook(testAttrs(testWorker, testClusterID, testExtra(), credential)), ReasonTaskCredential, credential)
	assertRefused(t, hook(testAttrs(testWorker, testClusterID, testExtra())), ReasonTaskCredential)
	if err := hook(testAttrs(testNonWorker, testClusterID, nil)); err != nil {
		t.Errorf("non-worker refused: %v", err)
	}
}

// The clock is the gate's: a credential valid now is refused once its expiry has passed.
func TestTheGateReadsItsClock(t *testing.T) {
	kp, pub := newTestKey(t)
	credential := mintTest(t, kp, testClaims())
	later := func() time.Time { return time.Now().Add(2 * testTTL) }
	hook := New(Config{Identities: testIdentities(), SigningKeys: []string{pub}, MaxLifetime: testLifetime}, slog.New(slog.DiscardHandler), later)
	assertRefused(t, hook(testAttrs(testWorker, testClusterID, testExtra(), credential)), ReasonTaskCredential, credential)
}

func TestCallerHeadersNameTheCredentialHeader(t *testing.T) {
	if got := CallerHeaders(); len(got) != 1 || got[0] != CredentialHeader {
		t.Errorf("CallerHeaders() = %v, want [%s]", got, CredentialHeader)
	}
}
