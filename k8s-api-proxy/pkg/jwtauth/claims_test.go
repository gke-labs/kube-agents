package jwtauth

import (
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// The nested claims sit under a key that itself holds a dot.
	testDomainKey      = "example.io"
	testUserPath       = `example\.io.user`
	testUIDPath        = `example\.io.uid`
	testGroupsPath     = `example\.io.groups`
	testPartitionPath  = `example\.io.tenant`
	testNestedUser     = "agent-1"
	testNestedUID      = "cae8-0001"
	testNestedTenant   = "team-a"
	testSmallPadBytes  = 1024
	testSubPrefix      = "actor/space-a/"
	testFlatPartition  = "tenant"
	testOtherPartition = "team-b"
)

func TestParseClaimPath(t *testing.T) {
	for _, tc := range []struct {
		path string
		want []string
	}{
		{"", nil},
		{"sub", []string{"sub"}},
		{"a.b", []string{"a", "b"}},
		{`a\.b.c`, []string{"a.b", "c"}},
		{`example\.io.tenant`, []string{"example.io", "tenant"}},
		{`a.b\.c\.d.e`, []string{"a", "b.c.d", "e"}},
		{`a\.`, []string{"a."}},
		{`\..x`, []string{".", "x"}},
		// An escape before anything but the separator is itself.
		{`a\b.c`, []string{`a\b`, "c"}},
		{`a\`, []string{`a\`}},
	} {
		got, err := parseClaimPath(tc.path)
		if err != nil {
			t.Errorf("parseClaimPath(%q): %v", tc.path, err)
			continue
		}
		if !slices.Equal(got, tc.want) {
			t.Errorf("parseClaimPath(%q) = %q, want %q", tc.path, got, tc.want)
		}
	}
	for _, path := range []string{".", "a..b", ".a", "a.", `a\..`} {
		if got, err := parseClaimPath(path); err == nil {
			t.Errorf("parseClaimPath(%q) = %q, want an error for the empty key", path, got)
		}
	}
}

// nestedConfig reads every identity field from under the dotted domain key.
func nestedConfig(is *issuer) Config {
	cfg := is.config()
	cfg.UsernameClaim = testUserPath
	cfg.UIDClaim = testUIDPath
	cfg.GroupsClaim = testGroupsPath
	cfg.PartitionClaim = testPartitionPath
	return cfg
}

func nestedClaims() map[string]any {
	return map[string]any{
		"user":   testNestedUser,
		"uid":    testNestedUID,
		"groups": []string{"agents", "readers"},
		"tenant": testNestedTenant,
	}
}

// signNested signs a token whose domain object is nestedClaims after mutate edited it.
func signNested(is *issuer, mutate func(domain map[string]any)) string {
	return is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
		domain := nestedClaims()
		if mutate != nil {
			mutate(domain)
		}
		extra[testDomainKey] = domain
	})
}

func TestAuthenticatorReadsNestedClaims(t *testing.T) {
	is := newIssuer(t)
	a := newAuth(t, nestedConfig(is))

	id, err := a.AuthenticateRequest(bearer(signNested(is, nil)))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	want := k8sapiproxy.CallerIdentity{
		Username: testNestedUser, UID: testNestedUID, Groups: []string{"agents", "readers"}, Partition: testNestedTenant,
	}
	if id.Username != want.Username || id.UID != want.UID || id.Partition != want.Partition || !slices.Equal(id.Groups, want.Groups) {
		t.Errorf("identity = %+v, want %+v", *id, want)
	}

	// The groups claim alone may be absent.
	id, err = a.AuthenticateRequest(bearer(signNested(is, func(d map[string]any) { delete(d, "groups") })))
	if err != nil {
		t.Fatalf("token without the groups claim: %v", err)
	}
	if len(id.Groups) != 0 {
		t.Errorf("groups = %v, want none", id.Groups)
	}
}

// Without UIDClaim and PartitionClaim the UID is the subject and there is no partition.
func TestAuthenticatorDefaultsUIDToTheSubject(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.UsernameClaim = testUserPath
	id, err := newAuth(t, cfg).AuthenticateRequest(bearer(signNested(is, nil)))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if id.Username != testNestedUser || id.UID != testSubject || id.Partition != "" {
		t.Errorf("identity = %+v, want username %q, UID %q and no partition", *id, testNestedUser, testSubject)
	}
}

func TestAuthenticatorRefusesMissingOrMistypedNestedClaims(t *testing.T) {
	is := newIssuer(t)
	a := newAuth(t, nestedConfig(is))

	// The unescaped spelling of the path: an "example" object holding "io".
	splitDomain := is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
		extra["example"] = map[string]any{"io": nestedClaims()}
	})
	flatDomain := is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
		extra[testDomainKey] = "not-an-object"
	})
	// Top-level claims named like the whole path are not the nested claim.
	flatKeys := is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
		for key, value := range nestedClaims() {
			extra[testDomainKey+"."+key] = value
		}
	})

	for _, tc := range []struct {
		name  string
		token string
	}{
		{"no domain object", is.sign(testKeyID, jose.RS256, nil)},
		{"domain object under the unescaped keys", splitDomain},
		{"domain claim that is not an object", flatDomain},
		{"top-level claims named like the path", flatKeys},
		{"missing username", signNested(is, func(d map[string]any) { delete(d, "user") })},
		{"username that is not a string", signNested(is, func(d map[string]any) { d["user"] = 7 })},
		{"empty username", signNested(is, func(d map[string]any) { d["user"] = "" })},
		{"missing UID", signNested(is, func(d map[string]any) { delete(d, "uid") })},
		{"UID that is not a string", signNested(is, func(d map[string]any) { d["uid"] = 7 })},
		{"empty UID", signNested(is, func(d map[string]any) { d["uid"] = "" })},
		{"UID that is an object", signNested(is, func(d map[string]any) { d["uid"] = map[string]any{"id": testNestedUID} })},
		{"groups that are an object", signNested(is, func(d map[string]any) { d["groups"] = map[string]any{"a": "b"} })},
		{"groups holding a number", signNested(is, func(d map[string]any) { d["groups"] = []any{"agents", 7} })},
		{"missing partition", signNested(is, func(d map[string]any) { delete(d, "tenant") })},
		{"partition that is not a string", signNested(is, func(d map[string]any) { d["tenant"] = []string{testNestedTenant} })},
		{"empty partition", signNested(is, func(d map[string]any) { d["tenant"] = "" })},
	} {
		t.Run(tc.name, func(t *testing.T) {
			id, err := a.AuthenticateRequest(bearer(tc.token))
			if err == nil {
				t.Fatalf("accepted as %+v", *id)
			}
			if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
				t.Errorf("err = %v, want a refusal (401), not unavailable", err)
			}
		})
	}
}

func TestAuthenticatorAllowedPartitions(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.PartitionClaim = testFlatPartition
	// Entries are trimmed and empty ones dropped.
	cfg.AllowedPartitions = []string{" " + testNestedTenant + " ", "", testOtherPartition}
	a := newAuth(t, cfg)
	tokenFor := func(partition any) string {
		return is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
			if partition != nil {
				extra[testFlatPartition] = partition
			}
		})
	}

	for _, partition := range []string{testNestedTenant, testOtherPartition} {
		id, err := a.AuthenticateRequest(bearer(tokenFor(partition)))
		if err != nil {
			t.Fatalf("partition %q: %v", partition, err)
		}
		if id.Partition != partition {
			t.Errorf("Partition = %q, want %q", id.Partition, partition)
		}
	}
	for name, partition := range map[string]any{
		"unlisted partition":          "team-c",
		"listed partition, padded":    " " + testNestedTenant,
		"listed partition, as a list": []string{testNestedTenant},
		"no partition":                nil,
	} {
		t.Run(name, func(t *testing.T) {
			id, err := a.AuthenticateRequest(bearer(tokenFor(partition)))
			if err == nil {
				t.Fatalf("accepted as %+v", *id)
			}
			if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
				t.Errorf("err = %v, want a refusal, not unavailable", err)
			}
		})
	}
}

func TestAuthenticatorRequiredSubPrefix(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.RequiredSubPrefix = testSubPrefix
	// The username comes from another claim, so the subject alone decides.
	cfg.UsernameClaim = "actor"
	a := newAuth(t, cfg)
	tokenFor := func(sub string) string {
		return is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, extra map[string]any) {
			c.Subject = sub
			extra["actor"] = testNestedUser
		})
	}

	if !strings.HasPrefix(testSubject, testSubPrefix) {
		t.Fatalf("test subject %q lacks the prefix %q", testSubject, testSubPrefix)
	}
	if _, err := a.AuthenticateRequest(bearer(tokenFor(testSubject))); err != nil {
		t.Fatalf("subject with the prefix: %v", err)
	}
	for name, sub := range map[string]string{
		"another prefix":       "actor/space-b/session-1",
		"prefix not at start":  "x" + testSubject,
		"prefix cut short":     strings.TrimSuffix(testSubPrefix, "/"),
		"no subject":           "",
		"prefix in other case": strings.ToUpper(testSubject),
	} {
		t.Run(name, func(t *testing.T) {
			id, err := a.AuthenticateRequest(bearer(tokenFor(sub)))
			if err == nil {
				t.Fatalf("accepted as %+v", *id)
			}
			if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
				t.Errorf("err = %v, want a refusal, not unavailable", err)
			}
		})
	}
}

// A bearer token over the size bound is refused before any of it is parsed, so it never
// triggers a key set fetch even though its signature would verify.
func TestAuthenticatorRefusesAnOversizedBearerToken(t *testing.T) {
	is := newIssuer(t)
	a := newAuth(t, is.config())
	padded := func(n int) string {
		return is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
			extra["pad"] = strings.Repeat("a", n)
		})
	}

	oversized := padded(maxBearerTokenBytes)
	if len(oversized) <= maxBearerTokenBytes {
		t.Fatalf("test token is %d bytes, want over %d", len(oversized), maxBearerTokenBytes)
	}
	id, err := a.AuthenticateRequest(bearer(oversized))
	if err == nil {
		t.Fatalf("oversized token accepted as %+v", *id)
	}
	if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("err = %v, want a refusal, not unavailable", err)
	}
	if got := is.served.Load(); got != 0 {
		t.Errorf("key set fetched %d times for an oversized token, want 0", got)
	}

	// The bound leaves room for a token carrying ordinary claims.
	small := padded(testSmallPadBytes)
	if len(small) >= maxBearerTokenBytes {
		t.Fatalf("control token is %d bytes, want under %d", len(small), maxBearerTokenBytes)
	}
	if _, err := a.AuthenticateRequest(bearer(small)); err != nil {
		t.Errorf("token under the bound: %v", err)
	}
}

// A Kubernetes ServiceAccount token, which carries the kubernetes.io claim, is the
// TokenReview authenticator's to admit (and to hold to its pod), even when this issuer signs
// it for an accepted audience.
func TestAuthenticatorRefusesAServiceAccountToken(t *testing.T) {
	is := newIssuer(t)
	a := newAuth(t, is.config())
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("the same token without the claim: %v", err)
	}
	for name, claim := range map[string]any{
		"projected": map[string]any{"namespace": "ns", "pod": map[string]any{"name": "p", "uid": "u"}, "serviceaccount": map[string]any{"name": "sa", "uid": "s"}},
		"not bound": map[string]any{"namespace": "ns", "serviceaccount": map[string]any{"name": "sa", "uid": "s"}},
	} {
		token := is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) { extra[kubernetesClaim] = claim })
		if _, err := a.AuthenticateRequest(bearer(token)); err == nil {
			t.Errorf("%s: admitted a ServiceAccount token", name)
		}
	}
}
