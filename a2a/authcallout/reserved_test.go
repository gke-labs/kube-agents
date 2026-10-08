package authcallout

import (
	"context"
	"io"
	"log/slog"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

// A narrowed user is named for its pod, and the name is its inbox prefix, so a
// pod named after a static nats.conf principal would hold that principal's
// inbox. These tests take the static names from the operator's rendered
// nats.conf (testdata/rendered-nats.conf, which the operator's
// TestRenderedNATSConfMatchesTheCalloutFixture keeps byte-identical to the
// render), so a static user the operator adds is refused here without anyone
// editing a list in this file.

// renderedAuthUsers is the auth_users list of a rendered nats.conf: every user
// the server authenticates by password rather than through the callout. It is
// the reference the operator's A2A_RESERVED_PRINCIPALS must equal.
func renderedAuthUsers(t *testing.T, conf string) []string {
	t.Helper()
	const key = "auth_users:"
	start := strings.Index(conf, key)
	if start < 0 {
		t.Fatal("the rendered nats.conf has no auth_users list")
	}
	rest := conf[start+len(key):]
	open, end := strings.Index(rest, "["), strings.Index(rest, "]")
	if open < 0 || end < open {
		t.Fatal("the rendered nats.conf's auth_users list is not a bracketed list")
	}
	var names []string
	for _, f := range strings.Split(rest[open+1:end], ",") {
		if n := strings.TrimSpace(f); n != "" {
			names = append(names, n)
		}
	}
	if len(names) == 0 {
		t.Fatal("the rendered nats.conf's auth_users list is empty; every test below would pass vacuously")
	}
	return names
}

func renderedFixtureAuthUsers(t *testing.T) []string {
	t.Helper()
	rendered, err := os.ReadFile("testdata/rendered-nats.conf")
	if err != nil {
		t.Fatalf("reading the operator's rendered nats.conf: %v", err)
	}
	return renderedAuthUsers(t, string(rendered))
}

// The derivation above is the whole reason the per-name tests are not vacuous,
// so it is pinned against the names the gap was reported with. A fixture that
// lost a static user would shrink the set the refusal tests iterate, silently.
func TestTheRenderedStaticUsersIncludeEveryPrincipalWithAnAppInbox(t *testing.T) {
	got := renderedFixtureAuthUsers(t)
	for _, want := range []string{"gateway", "web", "console", "bridge", "seed"} {
		if !slices.Contains(got, want) {
			t.Errorf("auth_users %v has no %q; the refusal tests would not cover it", got, want)
		}
	}
}

// reservedPodToken is the token a pod named name presents under the narrowed
// session ServiceAccount.
func reservedPodToken(name string) string {
	return "token-for-a-session-pod-named-" + name + "-padded-to-a-realistic-length"
}

func reservedPodTokens(names []string) map[string]Attested {
	tokens := sessionTokens()
	for _, n := range names {
		tokens[reservedPodToken(n)] = Attested{ServiceAccount: sessionSA, PodName: n, PodUID: "uid-" + n}
	}
	return tokens
}

// Against a real server started from the real render: a narrowed pod named
// after any static principal is refused at connect, and in the same harness a
// narrowed pod with an ordinary name is admitted, so the refusal is the name
// and not the harness.
func TestANarrowedPodNamedAfterAStaticPrincipalIsRefusedAtConnect(t *testing.T) {
	names := renderedFixtureAuthUsers(t)
	h := startHarness(t, sessionMap, reservedPodTokens(names))

	nc, err := nats.Connect(h.url, nats.Token(tokenPodA), nats.CustomInboxPrefix("_INBOX."+podA))
	if err != nil {
		t.Fatalf("a narrowed pod with an ordinary name was refused: %v", err)
	}
	nc.Close()

	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			nc, err := nats.Connect(h.url, nats.Token(reservedPodToken(name)), nats.CustomInboxPrefix("_INBOX."+name))
			if err == nil {
				nc.Close()
				t.Fatalf("a narrowed pod named %q connected; it would hold %s's inbox", name, name)
			}
		})
	}
}

// newReservedTestService builds a Service with no bus, for asserting on the
// refusal reason itself.
func newReservedTestService(t *testing.T, identityMap string, tokens map[string]Attested, reserved []string) *Service {
	t.Helper()
	issuerKP, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	issuerSeed, _ := issuerKP.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := NewStore(log)
	if err := store.Update([]byte(identityMap)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}
	validator, err := NewTokenValidator(tokenReviewer(tokens, nil), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	svc, err := NewService(store, validator, Config{IssuerSeed: string(issuerSeed), ReservedPrincipals: reserved, ReservedAddressees: renderedFixtureAddressees(t)}, log)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func authorizeToken(svc *Service, token string) (string, error) {
	req := &jwt.AuthorizationRequestClaims{}
	req.ConnectOptions.Token = token
	_, _, user, err := svc.authorize(context.Background(), req)
	return user, err
}

// One refusal per static name, with the reason the log line carries. Asserted
// in-process because the client sees only "Authorization Violation"; the
// reason exists in the callout's log and nowhere else.
func TestTheRefusalNamesTheStaticPrincipal(t *testing.T) {
	names := renderedFixtureAuthUsers(t)
	svc := newReservedTestService(t, sessionMap, reservedPodTokens(names), names)

	user, err := authorizeToken(svc, tokenPodA)
	if err != nil {
		t.Fatalf("a narrowed pod with an ordinary name was refused: %v", err)
	}
	if user != podA {
		t.Fatalf("the admitted pod was minted as %q, want %q", user, podA)
	}

	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			_, err := authorizeToken(svc, reservedPodToken(name))
			if err == nil {
				t.Fatalf("a narrowed pod named %q was authorized", name)
			}
			if !strings.Contains(err.Error(), "the name of a static principal") || !strings.Contains(err.Error(), `"`+name+`"`) {
				t.Errorf("refused for the wrong reason: %v", err)
			}
		})
	}
}

// A non-narrowed principal is not touched by the reserved set: the gateway's
// own ServiceAccount, mapped to user `gateway` in this fixture, still
// authorizes. The check is on the derived pod name, not on any user.
func TestTheReservedSetDoesNotRefuseAMappedPrincipal(t *testing.T) {
	names := renderedFixtureAuthUsers(t)
	svc := newReservedTestService(t, sessionMap, reservedPodTokens(names), names)
	user, err := authorizeToken(svc, gatewayToken)
	if err != nil {
		t.Fatalf("the mapped gateway entry was refused: %v", err)
	}
	if user != "gateway" {
		t.Fatalf("minted as %q, want gateway", user)
	}
}

func TestParseReservedPrincipals(t *testing.T) {
	got, err := ParseReservedPrincipals("callout,gateway, bridge ,seed")
	if err != nil {
		t.Fatalf("a well-formed list was refused: %v", err)
	}
	if want := []string{"callout", "gateway", "bridge", "seed"}; !slices.Equal(got, want) {
		t.Fatalf("parsed %v, want %v", got, want)
	}

	// Every one of these fails closed: an error, never a shorter list.
	for name, raw := range map[string]string{
		"empty":            "",
		"blank":            "  ",
		"empty element":    "gateway,,web",
		"trailing comma":   "gateway,web,",
		"leading comma":    ",gateway",
		"uppercase":        "gateway,Web",
		"dotted":           "gateway,we.b",
		"wildcard":         "gateway,*",
		"space inside":     "gate way",
		"rendered spacing": "callout, gateway, ",
	} {
		t.Run(name, func(t *testing.T) {
			if got, err := ParseReservedPrincipals(raw); err == nil {
				t.Fatalf("ParseReservedPrincipals(%q) = %v, want an error", raw, got)
			}
		})
	}
}

// NewService refuses to build a callout that reserves nothing, so a caller
// that skips the parser cannot get one either.
func TestNewServiceRefusesAnEmptyReservedSet(t *testing.T) {
	issuerKP, _ := nkeys.CreateAccount()
	issuerSeed, _ := issuerKP.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	validator, err := NewTokenValidator(tokenReviewer(nil, nil), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	for name, reserved := range map[string][]string{
		"nil":       nil,
		"empty":     {},
		"malformed": {"gateway", ""},
	} {
		t.Run(name, func(t *testing.T) {
			// The addressees are well formed, so the refusal is the principals.
			_, err := NewService(NewStore(log), validator, Config{IssuerSeed: string(issuerSeed), ReservedPrincipals: reserved, ReservedAddressees: renderedFixtureAddressees(t)}, log)
			if err == nil {
				t.Fatalf("NewService accepted reserved principals %q", reserved)
			}
			if !strings.Contains(err.Error(), "principal") {
				t.Fatalf("NewService refused for something other than the principals: %v", err)
			}
		})
	}
}

// The identity map's own users have the same exposure as the static ones: the
// callout mints a mapped entry as its `user`, and that entry's grants carry
// `_INBOX.<user>.>`. A narrowed pod named `verifier` would be handed the
// verifier's inbox. These tests take the names from the operator's rendered
// map (testdata/rendered-identity-map.json, which the operator's
// TestRenderedAuthMapMatchesTheCalloutFixture keeps identical to the render).

func renderedFixtureMap(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("reading the operator's rendered identity map: %v", err)
	}
	return string(raw)
}

// renderedFixtureMapUsers is every user the rendered map serves: the names
// the callout mints its entries that are not narrowed as, plus the narrowed
// entry's own user.
func renderedFixtureMapUsers(t *testing.T) []string {
	t.Helper()
	m, err := ParseIdentityMap([]byte(renderedFixtureMap(t)))
	if err != nil {
		t.Fatalf("the operator's rendered map does not parse: %v", err)
	}
	users := m.Users()
	if len(users) == 0 {
		t.Fatal("the rendered identity map serves no users; every test below would pass vacuously")
	}
	return users
}

// Pinned against the names the gap was reported with, for the same reason the
// static list is: a fixture that lost a user would shrink what the refusal
// tests iterate, silently.
func TestTheRenderedMapUsersIncludeEveryPrincipalWithAnInbox(t *testing.T) {
	got := renderedFixtureMapUsers(t)
	for _, want := range []string{"verifier", "agent", "provision"} {
		if !slices.Contains(got, want) {
			t.Errorf("the rendered map's users %v have no %q; the refusal tests would not cover it", got, want)
		}
	}
}

// One refusal per rendered map user, with the reason the log line carries,
// against a callout serving the rendered map itself.
func TestTheRefusalNamesTheIdentityMapUser(t *testing.T) {
	static := renderedFixtureAuthUsers(t)
	users := renderedFixtureMapUsers(t)
	svc := newReservedTestService(t, renderedFixtureMap(t), reservedPodTokens(users), static)

	user, err := authorizeToken(svc, tokenPodA)
	if err != nil {
		t.Fatalf("a narrowed pod with an ordinary name was refused: %v", err)
	}
	if user != podA {
		t.Fatalf("the admitted pod was minted as %q, want %q", user, podA)
	}

	for _, name := range users {
		t.Run(name, func(t *testing.T) {
			if slices.Contains(static, name) {
				t.Fatalf("%q is both a static principal and a map user; the rendered config serves it twice", name)
			}
			_, err := authorizeToken(svc, reservedPodToken(name))
			if err == nil {
				t.Fatalf("a narrowed pod named %q was authorized; it would hold %s's inbox", name, name)
			}
			if !strings.Contains(err.Error(), "the name of an identity-map user") || !strings.Contains(err.Error(), `"`+name+`"`) {
				t.Errorf("refused for the wrong reason: %v", err)
			}
		})
	}
}

// Against a real server started from the real render and serving the real
// map: every map user's name is refused at connect, and an ordinary name is
// admitted in the same harness.
func TestANarrowedPodNamedAfterAnIdentityMapUserIsRefusedAtConnect(t *testing.T) {
	users := renderedFixtureMapUsers(t)
	h := startHarness(t, renderedFixtureMap(t), reservedPodTokens(users))

	nc, err := nats.Connect(h.url, nats.Token(tokenPodA), nats.CustomInboxPrefix("_INBOX."+podA))
	if err != nil {
		t.Fatalf("a narrowed pod with an ordinary name was refused: %v", err)
	}
	nc.Close()

	for _, name := range users {
		t.Run(name, func(t *testing.T) {
			nc, err := nats.Connect(h.url, nats.Token(reservedPodToken(name)), nats.CustomInboxPrefix("_INBOX."+name))
			if err == nil {
				nc.Close()
				t.Fatalf("a narrowed pod named %q connected; it would hold %s's inbox", name, name)
			}
		})
	}
}

// reloadNewcomer is a user the reload test adds to sessionMap and removes again.
const reloadNewcomer = "newcomer"

func mapWithNewcomer(version string) string {
	entry := `{
      "serviceAccount": "system:serviceaccount:kubeagents-system:newcomer",
      "user": "` + reloadNewcomer + `",
      "account": "APP",
      "grants": {"publish": ["_INBOX.` + reloadNewcomer + `.>"], "subscribe": ["_INBOX.` + reloadNewcomer + `.>"]}
    },
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-session",`
	m := strings.Replace(sessionMap, `{
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-session",`, entry, 1)
	return strings.Replace(m, `"version": "session-itest-1"`, `"version": "`+version+`"`, 1)
}

// The reserved map users follow the map the callout is serving: a reload that
// adds a user reserves its name on the next connection, and a reload that
// removes it releases the name at once, even though connections the removed
// principal already holds keep its inbox until their GrantTTL expiry (the
// accepted window documented in reserved.go). The static names hold across both.
func TestAMapReloadMovesTheReservedUsers(t *testing.T) {
	static := renderedFixtureAuthUsers(t)
	tokens := reservedPodTokens(append([]string{reloadNewcomer}, static...))
	svc := newReservedTestService(t, sessionMap, tokens, static)

	if strings.Contains(sessionMap, `"user": "`+reloadNewcomer+`"`) {
		t.Fatalf("the starting map already serves %q", reloadNewcomer)
	}
	if _, err := authorizeToken(svc, reservedPodToken(reloadNewcomer)); err != nil {
		t.Fatalf("before the reload, a narrowed pod named %q was refused: %v", reloadNewcomer, err)
	}

	added := mapWithNewcomer("session-itest-2")
	if !strings.Contains(added, `"user": "`+reloadNewcomer+`"`) {
		t.Fatal("the reloaded map does not carry the new user; the fixture splice missed")
	}
	if err := svc.store.Update([]byte(added)); err != nil {
		t.Fatalf("reloading with %q added: %v", reloadNewcomer, err)
	}
	_, err := authorizeToken(svc, reservedPodToken(reloadNewcomer))
	if err == nil {
		t.Fatalf("after %q joined the map, a narrowed pod with its name was authorized", reloadNewcomer)
	}
	if !strings.Contains(err.Error(), "the name of an identity-map user") {
		t.Errorf("refused for the wrong reason: %v", err)
	}

	removed := strings.Replace(sessionMap, `"version": "session-itest-1"`, `"version": "session-itest-3"`, 1)
	if err := svc.store.Update([]byte(removed)); err != nil {
		t.Fatalf("reloading with %q removed: %v", reloadNewcomer, err)
	}
	if _, err := authorizeToken(svc, reservedPodToken(reloadNewcomer)); err != nil {
		t.Fatalf("after %q left the map, a narrowed pod with its name was still refused: %v", reloadNewcomer, err)
	}
	for _, name := range static {
		if _, err := authorizeToken(svc, reservedPodToken(name)); err == nil || !strings.Contains(err.Error(), "the name of a static principal") {
			t.Errorf("after the reloads, static principal %q is not refused as one: %v", name, err)
		}
	}
}

// A map that did not come through ParseIdentityMap has no cached user set.
// Its users are still reserved: servesUser scans the entries rather than
// reporting none, so a map installed some other way cannot fail open.
func TestAMapWithoutTheCachedSetStillReservesItsUsers(t *testing.T) {
	m := &IdentityMap{Version: "hand-built", Identities: []Identity{{User: "verifier"}}}
	if !m.servesUser("verifier") {
		t.Fatal("a hand-built map does not serve its own user; the map-user refusal would fail open")
	}
	if m.servesUser("pod-a") {
		t.Fatal("a hand-built map serves a user it has no entry for")
	}
}
