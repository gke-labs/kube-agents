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

// A narrowed user is named for its pod, and its task subjects are keyed on that
// name, so a pod named after a fixed-name addressee would be handed that
// addressee's subjects. These tests take the reserved names from the operator's
// render (testdata/rendered-reserved-addressees.txt, which the operator's
// TestRenderedReservedAddresseesMatchTheCalloutFixture keeps equal to the
// A2A_RESERVED_ADDRESSEES it renders), so an addressee the operator adds is
// refused here without anyone editing a list in this file.

const reservedAddresseesFixture = "testdata/rendered-reserved-addressees.txt"

// renderedFixtureAddressees is the operator's rendered list, parsed the way the
// callout parses it at startup. An empty or malformed fixture fails the test,
// so no test below can pass by iterating nothing.
func renderedFixtureAddressees(t *testing.T) []string {
	t.Helper()
	raw, err := os.ReadFile(reservedAddresseesFixture)
	if err != nil {
		t.Fatalf("reading the operator's rendered reserved addressees: %v", err)
	}
	names, err := ParseReservedAddressees(strings.TrimSuffix(string(raw), "\n"))
	if err != nil {
		t.Fatalf("the operator's rendered reserved addressees do not parse: %v", err)
	}
	if len(names) == 0 {
		t.Fatal("the operator's rendered reserved addressees are empty; every test below would pass vacuously")
	}
	return names
}

// The fixture is the whole reason the per-name tests are not vacuous, so it is
// pinned against the addressee the gap was reported with. A fixture that lost
// it would shrink the set the refusal tests iterate, silently.
func TestTheRenderedReservedAddresseesIncludeTheBridgesAddressee(t *testing.T) {
	if got := renderedFixtureAddressees(t); !slices.Contains(got, "platform") {
		t.Errorf("the rendered reserved addressees %v have no %q; the bridge's task subjects would be open to a pod of that name", got, "platform")
	}
}

// addresseePodToken is the token a pod named name presents under the narrowed
// session ServiceAccount.
func addresseePodToken(name string) string {
	return "token-for-a-session-pod-named-after-addressee-" + name + "-padded-out"
}

func addresseePodTokens(names []string) map[string]Attested {
	tokens := sessionTokens()
	for _, n := range names {
		tokens[addresseePodToken(n)] = Attested{ServiceAccount: sessionSA, PodName: n, PodUID: "uid-addressee-" + n}
	}
	return tokens
}

// Against a real server started from the operator's rendered nats.conf: a
// narrowed pod named after any reserved addressee is refused at connect, and in
// the same harness a narrowed pod with an ordinary name is admitted, so the
// refusal is the name and not the harness.
func TestANarrowedPodNamedAfterAnAddresseeIsRefusedAtConnect(t *testing.T) {
	names := renderedFixtureAddressees(t)
	h := startHarness(t, sessionMap, addresseePodTokens(names))

	nc, err := nats.Connect(h.url, nats.Token(tokenPodA), nats.CustomInboxPrefix("_INBOX."+podA))
	if err != nil {
		t.Fatalf("a narrowed pod with an ordinary name was refused: %v", err)
	}
	nc.Close()

	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			nc, err := nats.Connect(h.url, nats.Token(addresseePodToken(name)), nats.CustomInboxPrefix("_INBOX."+name))
			if err == nil {
				nc.Close()
				t.Fatalf("a narrowed pod named %q connected; it would hold addressee %s's task subjects", name, name)
			}
		})
	}
}

// newAddresseeTestService builds a Service with no bus, for asserting on the
// refusal reason itself.
func newAddresseeTestService(t *testing.T, tokens map[string]Attested, reserved []string) *Service {
	t.Helper()
	issuerKP, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	issuerSeed, _ := issuerKP.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := NewStore(log)
	if err := store.Update([]byte(sessionMap)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}
	validator, err := NewTokenValidator(tokenReviewer(tokens, nil), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	svc, err := NewService(store, validator, Config{IssuerSeed: string(issuerSeed), ReservedPrincipals: renderedFixtureAuthUsers(t), ReservedAddressees: reserved}, log)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func authorizeAddresseeToken(svc *Service, token string) (string, error) {
	req := &jwt.AuthorizationRequestClaims{}
	req.ConnectOptions.Token = token
	_, _, user, err := svc.authorize(context.Background(), req)
	return user, err
}

// One refusal per reserved addressee, with the reason the log line carries.
// Asserted in-process because the client sees only "Authorization Violation";
// the reason exists in the callout's log and nowhere else.
func TestTheRefusalNamesTheAddressee(t *testing.T) {
	names := renderedFixtureAddressees(t)
	svc := newAddresseeTestService(t, addresseePodTokens(names), names)

	user, err := authorizeAddresseeToken(svc, tokenPodA)
	if err != nil {
		t.Fatalf("a narrowed pod with an ordinary name was refused: %v", err)
	}
	if user != podA {
		t.Fatalf("the admitted pod was minted as %q, want %q", user, podA)
	}

	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			_, err := authorizeAddresseeToken(svc, addresseePodToken(name))
			if err == nil {
				t.Fatalf("a narrowed pod named %q was authorized", name)
			}
			if !strings.Contains(err.Error(), "which is the name of an addressee") || !strings.Contains(err.Error(), `"`+name+`"`) {
				t.Errorf("refused for the wrong reason: %v", err)
			}
		})
	}
}

// The comparison is exact. A pod whose name only starts with or contains a
// reserved addressee is a different addressee with subjects of its own, and is
// admitted; so is a mapped principal, whose user does not come from a pod name.
func TestTheReservedAddresseesRefuseOnlyTheExactPodName(t *testing.T) {
	names := renderedFixtureAddressees(t)
	tokens := addresseePodTokens(names)
	var lookalikes []string
	for _, n := range names {
		for _, l := range []string{n + "-1a2b", "chat-" + n} {
			tokens[addresseePodToken(l)] = Attested{ServiceAccount: sessionSA, PodName: l, PodUID: "uid-" + l}
			lookalikes = append(lookalikes, l)
		}
	}
	svc := newAddresseeTestService(t, tokens, names)

	for _, l := range lookalikes {
		t.Run(l, func(t *testing.T) {
			user, err := authorizeAddresseeToken(svc, addresseePodToken(l))
			if err != nil {
				t.Fatalf("a narrowed pod named %q was refused: %v", l, err)
			}
			if user != l {
				t.Fatalf("minted as %q, want %q", user, l)
			}
		})
	}
	user, err := authorizeAddresseeToken(svc, gatewayToken)
	if err != nil {
		t.Fatalf("the mapped gateway entry was refused: %v", err)
	}
	if user != "gateway" {
		t.Fatalf("minted as %q, want gateway", user)
	}
}

func TestParseReservedAddressees(t *testing.T) {
	got, err := ParseReservedAddressees("platform, planner ,chat")
	if err != nil {
		t.Fatalf("a well-formed list was refused: %v", err)
	}
	if want := []string{"platform", "planner", "chat"}; !slices.Equal(got, want) {
		t.Fatalf("parsed %v, want %v", got, want)
	}

	// Every one of these fails closed: an error, never a shorter list.
	for name, raw := range map[string]string{
		"empty":          "",
		"blank":          "  ",
		"empty element":  "platform,,chat",
		"trailing comma": "platform,",
		"leading comma":  ",platform",
		"uppercase":      "Platform",
		"dotted":         "plat.form",
		"wildcard":       "platform,*",
		"space inside":   "plat form",
	} {
		t.Run(name, func(t *testing.T) {
			if got, err := ParseReservedAddressees(raw); err == nil {
				t.Fatalf("ParseReservedAddressees(%q) = %v, want an error", raw, got)
			}
		})
	}
}

// NewService refuses to build a callout that reserves no addressee, so a
// caller that skips the parser cannot get one either.
func TestNewServiceRefusesAnEmptyReservedAddresseeSet(t *testing.T) {
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
		"malformed": {"platform", ""},
	} {
		t.Run(name, func(t *testing.T) {
			// The principals are well formed, so the refusal is the addressees.
			_, err := NewService(NewStore(log), validator, Config{IssuerSeed: string(issuerSeed), ReservedPrincipals: renderedFixtureAuthUsers(t), ReservedAddressees: reserved}, log)
			if err == nil {
				t.Fatalf("NewService accepted reserved addressees %q", reserved)
			}
			if !strings.Contains(err.Error(), "addressee") {
				t.Fatalf("NewService refused for something other than the addressees: %v", err)
			}
		})
	}
}
