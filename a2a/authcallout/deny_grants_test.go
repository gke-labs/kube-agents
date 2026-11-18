package authcallout

import (
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// Deny lists (#5301): an entry's grants may subtract subjects from its allows,
// for a principal whose allow is a wildcard with named exceptions (the
// gateway's consumer-create grant, minus the pre-created durables). They only
// narrow: the both-sides allow rule still holds, and a narrowed entry, whose
// grants are derived, may carry none.

func denyEntry(pub, sub, denyPub, denySub string) string {
	return `{"version":"v1","identities":[{
		"serviceAccount":"system:serviceaccount:ns:sa",
		"user":"u","account":"APP",
		"grants":{"publish":` + pub + `,"subscribe":` + sub + `,"denyPublish":` + denyPub + `,"denySubscribe":` + denySub + `}}]}`
}

func TestAnEntryWithAllowsOnBothSidesMayCarryDenies(t *testing.T) {
	m, err := ParseIdentityMap([]byte(denyEntry(`["a2a.tasks.>"]`, `["_INBOX.u.>"]`, `["a2a.tasks.x.y.in"]`, `["_INBOX.u.secret"]`)))
	if err != nil {
		t.Fatalf("a deny on a two-sided entry was refused: %v", err)
	}
	g := m.Identities[0].Grants
	if !slices.Equal(g.DenyPublish, []string{"a2a.tasks.x.y.in"}) || !slices.Equal(g.DenySubscribe, []string{"_INBOX.u.secret"}) {
		t.Errorf("denies = %+v", g)
	}
}

// A deny never stands in for an allow: an entry with an empty allow side is
// refused whatever it denies, because the server reads an absent allow as
// unrestricted, and "everything but X" is exactly what the rule forbids.
func TestADenyDoesNotStandInForAnEmptyAllow(t *testing.T) {
	for name, doc := range map[string]string{
		"deny with no publish allow":   denyEntry(`[]`, `["_INBOX.u.>"]`, `["a2a.tasks.x.y.in"]`, `[]`),
		"deny with no subscribe allow": denyEntry(`["a2a.tasks.>"]`, `[]`, `[]`, `["_INBOX.u.secret"]`),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseIdentityMap([]byte(doc)); err == nil || !strings.Contains(err.Error(), "unrestricted") {
				t.Fatalf("err = %v, want the empty-side refusal", err)
			}
		})
	}
}

// A narrowed entry's grants are derived, so a deny written into the map
// counts as a grant and is refused, as an allow is: one map edit can't hand
// every session or profile pod a deny-shaped grant. The dispatcher's entry
// carries allows (its control connection's) but no deny.
func TestANarrowedEntryMayCarryNoDeny(t *testing.T) {
	pod := Identity{ServiceAccount: "system:serviceaccount:ns:session", User: "session", Account: AccountApp, Narrowing: NarrowingPod,
		Grants: Grants{DenyPublish: []string{"a2a.tasks.>"}}}
	if _, err := ParseIdentityMap(renderMap(t, pod)); err == nil {
		t.Error("a pod-narrowed entry carrying only a deny was accepted")
	}
	prof := profileEntry("a", "read")
	prof.Grants = Grants{DenySubscribe: []string{"_INBOX.>"}}
	if _, err := ParseIdentityMap(renderMap(t, prof)); err == nil {
		t.Error("a profile-narrowed entry carrying only a deny was accepted")
	}
	disp := dispatcherEntry()
	disp.Grants.DenyPublish = []string{"a2a.dispatch.>"}
	if _, err := ParseIdentityMap(renderMap(t, disp)); err == nil || !strings.Contains(err.Error(), "deny") {
		t.Errorf("a dispatcher entry with a deny: err = %v, want refused", err)
	}
}

func TestTheMintedJWTCarriesTheEntrysDenies(t *testing.T) {
	p := permissionsFor(Grants{
		Publish: []string{"a2a.tasks.>"}, Subscribe: []string{"_INBOX.u.>"},
		DenyPublish: []string{"a2a.tasks.x.y.in"}, DenySubscribe: []string{"_INBOX.u.secret"},
	})
	if !slices.Contains(p.Pub.Deny, "a2a.tasks.x.y.in") || !slices.Contains(p.Sub.Deny, "_INBOX.u.secret") {
		t.Fatalf("permissions = %+v, want the denies on each side", p)
	}
	if slices.Contains(p.Pub.Deny, ">") || slices.Contains(p.Sub.Deny, ">") {
		t.Errorf("a two-sided grant set gained a blanket deny: %+v", p)
	}
}

// Against a real server: the minted user may publish under its allow but not
// on the subject its deny names.
func TestADeniedSubjectIsRefusedByTheServer(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	h.store.current.Store(&IdentityMap{
		Version: "planted",
		Identities: []Identity{{
			ServiceAccount: sessionSA, User: "session", Account: "APP",
			Grants: Grants{
				Publish:     []string{"a2a.tasks.>"},
				Subscribe:   []string{"_INBOX.session.>"},
				DenyPublish: []string{"a2a.tasks.held.task.in"},
			},
		}},
	})
	var mu sync.Mutex
	var violations []string
	nc, err := nats.Connect(h.url, h.dial, nats.Token(tokenPodA), nats.CustomInboxPrefix("_INBOX.session"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			mu.Lock()
			violations = append(violations, err.Error())
			mu.Unlock()
		}))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	if err := nc.Publish("a2a.tasks.other.task.in", []byte("x")); err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish("a2a.tasks.held.task.in", []byte("x")); err != nil {
		t.Fatal(err)
	}
	_ = nc.FlushTimeout(2 * time.Second)
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(violations)
		mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(violations) != 1 || !strings.Contains(violations[0], "a2a.tasks.held.task.in") {
		t.Fatalf("violations = %v, want exactly the denied subject's", violations)
	}
}

// The gateway minted from its token entry in the real render, with profiles
// bound, on a real server (#5301). Its ordinary publishes pass. It is refused
// on a bound profile's five-token supervisor subject, which only the entry's
// per-profile deny covers, and on creating over its relay durable, which only
// the durable deny covers: the two places its allows reach further than it
// may. It is also refused on the six-token profile form, which its five-token
// grant can't match at all; that one holds by token count, not by a deny, and
// the token flip is gated on it.
func TestTheGatewaysTokenEntryIsRefusedWhereItsStaticUserIs(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedProfilesMap(t)))
	if err != nil {
		t.Fatal(err)
	}
	var gatewaySA string
	for _, id := range m.Identities {
		if id.User == "gateway" {
			gatewaySA = id.ServiceAccount
		}
	}
	if gatewaySA == "" {
		t.Fatal("the rendered map has no gateway entry")
	}
	const tokenGateway = "token-for-the-gateway-deployment-padded-to-a-realistic-length"
	tokens := sessionTokens()
	tokens[tokenGateway] = Attested{ServiceAccount: gatewaySA, PodName: "agent-a2a-gateway-0", PodUID: "uid-gw"}
	h := startHarness(t, renderedProfilesMap(t), tokens)

	var mu sync.Mutex
	var violations []string
	nc, err := nats.Connect(h.url, h.dial, nats.Token(tokenGateway), nats.CustomInboxPrefix("_INBOX.gateway"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			mu.Lock()
			violations = append(violations, err.Error())
			mu.Unlock()
		}))
	if err != nil {
		t.Fatalf("the gateway's token was refused: %v", err)
	}
	defer nc.Close()

	allowed := []string{"a2a.tasks.cto-otter-1a2b.t1.supervisor", "a2a.tasks.cto-otter-1a2b.t1.in"}
	refused := []string{
		"a2a.tasks.auditor.t1.supervisor",
		"a2a.tasks.p.auditor.t1.supervisor",
		"$JS.API.CONSUMER.CREATE.TASKS.gateway-relay",
		"$JS.API.CONSUMER.DELETE.TASKS.gateway-relay",
	}
	for _, s := range append(append([]string(nil), allowed...), refused...) {
		if err := nc.Publish(s, []byte("{}")); err != nil {
			t.Fatal(err)
		}
	}
	_ = nc.FlushTimeout(2 * time.Second)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(violations)
		mu.Unlock()
		if n >= len(refused) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	joined := strings.Join(violations, "\n")
	for _, s := range refused {
		if !strings.Contains(joined, `"`+s+`"`) {
			t.Errorf("no violation for %q; got:\n%s", s, joined)
		}
	}
	for _, s := range allowed {
		if strings.Contains(joined, `"`+s+`"`) {
			t.Errorf("an ordinary gateway publish on %q was refused", s)
		}
	}
}
