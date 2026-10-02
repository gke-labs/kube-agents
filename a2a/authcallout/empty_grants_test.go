package authcallout

import (
	"slices"
	"strings"
	"testing"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
)

// The empty grant set, which is the one value in this package that means the
// opposite of what it reads as.
//
// `Grants{}` looks like the safe default and is the natural thing to return
// from an error path. It is not safe: nats-server builds a client's
// permissions from the lists in the minted JWT and treats an ABSENT list as
// unrestricted rather than as nothing, so a credential minted from an empty
// grant set may publish to every subject on the bus and subscribe to ">".
// Worse, the two sides are independent — an empty publish list alone mints an
// unrestricted publisher while the subscribe side stays correctly narrow,
// which is the version of the bug that looks healthy from the outside.
//
// Measured, not reasoned: with the guards below removed, a session pod minted
// from `Grants{}` was allowed `$JS.API.STREAM.DELETE.TASKS` — destroying the
// task stream the whole product runs on — and `>` on subscribe, against the
// operator's own rendered nats.conf.
//
// There is no value of type Grants that means "refuse", so every path that
// cannot produce grants has to say so out of band. These tests pin the
// places that now do.

// The map refuses an entry with either side empty, per side rather than across
// both. The `publish: []` case is the one the old `||` check admitted: it has
// grants, so it passed, and it minted an unrestricted publisher.
func TestTheMapRefusesAnEntryWithEitherSideEmpty(t *testing.T) {
	entry := func(pub, sub string) string {
		return `{"version":"v1","identities":[{
			"serviceAccount":"system:serviceaccount:ns:sa",
			"user":"u","account":"APP",
			"grants":{"publish":` + pub + `,"subscribe":` + sub + `}}]}`
	}
	cases := map[string]string{
		"no publish":   entry(`[]`, `["_INBOX.u.>"]`),
		"no subscribe": entry(`["a2a.tasks.>"]`, `[]`),
		"neither":      entry(`[]`, `[]`),
	}
	for name, doc := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := ParseIdentityMap([]byte(doc))
			if err == nil {
				t.Fatal("the map was accepted; an empty side is minted as unrestricted on that side")
			}
			if !strings.Contains(err.Error(), "unrestricted") {
				t.Errorf("refused, but not for this reason: %v", err)
			}
		})
	}
	t.Run("both sides populated is still accepted", func(t *testing.T) {
		if _, err := ParseIdentityMap([]byte(entry(`["a2a.tasks.>"]`, `["_INBOX.u.>"]`))); err != nil {
			t.Fatalf("a well-formed entry was refused: %v", err)
		}
	})
}

// The mint refuses an empty side even when the map never did, against a real
// server. The map is planted past ParseIdentityMap on purpose: the scenario
// this guard exists for is precisely the one where validation and the mint
// have drifted apart, so a test that went through validation could not reach
// it. This is the drift, staged.
func TestAnEmptySideIsRefusedBeforeItCanBeMinted(t *testing.T) {
	// Both one-sided shapes, because the check is an OR of two terms and a
	// test for one of them leaves the other free: dropping
	// `|| len(grants.Subscribe) == 0` from authorize keeps the suite green if
	// only the publish-empty case is planted.
	cases := map[string]Grants{
		"publishes empty, subscribes intact": {Subscribe: []string{"_INBOX.session.>"}},
		"subscribes empty, publishes intact": {Publish: []string{"a2a.tasks.>"}},
	}
	for name, grants := range cases {
		t.Run(name, func(t *testing.T) {
			h := startHarness(t, sessionMap, sessionTokens())

			h.store.current.Store(&IdentityMap{
				Version: "planted",
				Identities: []Identity{{
					ServiceAccount: sessionSA,
					User:           "session",
					Account:        "APP",
					Grants:         grants,
				}},
			})

			nc, err := nats.Connect(h.url, nats.Token(tokenPodA), nats.CustomInboxPrefix("_INBOX.session"))
			if err == nil {
				nc.Close()
				t.Fatal("a one-sided grant set connected; it would have been unrestricted on the empty side")
			}
			if !strings.Contains(err.Error(), "Authorization Violation") {
				t.Errorf("connect error = %v, want an Authorization Violation", err)
			}
		})
	}
}

// permissionsFor never emits a side with neither an allow nor a deny, because
// that is the combination nats-server reads as unrestricted.
func TestPermissionsNeverLeaveASideEmpty(t *testing.T) {
	cases := map[string]Grants{
		"both empty":   {},
		"no publish":   {Subscribe: []string{"_INBOX.u.>"}},
		"no subscribe": {Publish: []string{"a2a.tasks.>"}},
	}
	// The deny has to BE ">", not merely be present. With an empty allow list
	// and a deny of one ordinary subject, buildPermissionsFromJwt still builds
	// the side, pubAllowedFullCheck skips the allow check because pub.allow is
	// nil, and the deny matches that one subject -- so everything else is
	// permitted. A deny of "nothing.ever" or "*" reads like a closed side and
	// is wide open; only ">" closes it.
	closed := func(t *testing.T, side string, allow, deny jwt.StringList) {
		t.Helper()
		if len(allow) != 0 {
			return
		}
		if len(deny) == 0 {
			t.Errorf("%s has neither an allow nor a deny: the server reads that as unrestricted", side)
			return
		}
		if !slices.Contains(deny, ">") {
			t.Errorf("%s has an empty allow and deny %v, which closes only those subjects; want a deny of \">\"", side, deny)
		}
	}
	for name, g := range cases {
		t.Run(name, func(t *testing.T) {
			p := permissionsFor(g)
			closed(t, "publish", p.Pub.Allow, p.Pub.Deny)
			closed(t, "subscribe", p.Sub.Allow, p.Sub.Deny)
		})
	}
	t.Run("a populated side is left alone", func(t *testing.T) {
		p := permissionsFor(Grants{Publish: []string{"a"}, Subscribe: []string{"b"}})
		if len(p.Pub.Deny) != 0 || len(p.Sub.Deny) != 0 {
			t.Errorf("a deny was added to a side that had grants: %+v", p)
		}
	})
}
