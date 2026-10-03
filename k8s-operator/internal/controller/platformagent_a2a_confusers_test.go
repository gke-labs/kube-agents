package controller

import (
	"slices"
	"strings"
	"testing"
)

// A static user with one side populated and the other empty must render an
// explicit deny on the empty side.
//
// This is the same defect the callout's identity-map validator refuses, in the
// half of the render that has no validator. An absent `subscribe` key is not an
// empty allow list: nats-server's parseUserPermissions sets only the side it
// finds, and a side it never set is unrestricted. So a publish-only static user
// would ship able to subscribe to every subject on the bus -- including every
// other principal's inbox -- while its publishes stayed correctly narrow.
//
// Measured against a real nats-server before the fix: a one-sided user
// subscribed to ">" with no error, while a two-sided one was refused with a
// permissions violation. Run on v2.14.6 and taken again on v2.15.0 after
// #2232's bump; the line numbers moved and the behaviour did not.
func TestAOneSidedStaticUserRendersADenyOnTheEmptySide(t *testing.T) {
	for _, tc := range []struct {
		name          string
		publish       []string
		subscribe     []string
		wantDenied    string
		wantNotDenied string
	}{
		{
			name:          "subscribe empty",
			publish:       []string{"a2a.tasks.>"},
			wantDenied:    "subscribe",
			wantNotDenied: "publish",
		},
		{
			name:          "publish empty",
			subscribe:     []string{"_INBOX.u.>"},
			wantDenied:    "publish",
			wantNotDenied: "subscribe",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := renderA2AStaticUser(a2aIdentity{
				user:      "u",
				account:   a2aAccountApp,
				auth:      a2aAuthStatic,
				comment:   "a one-sided principal",
				publish:   tc.publish,
				subscribe: tc.subscribe,
			}, "pw")

			if !strings.Contains(got, tc.wantDenied+` { deny = [">"] }`) {
				t.Errorf("%s side is empty but renders no deny, so the server reads it as unrestricted:\n%s", tc.wantDenied, got)
			}
			if strings.Contains(got, tc.wantNotDenied+` { deny =`) {
				t.Errorf("%s side has grants and must not gain a deny:\n%s", tc.wantNotDenied, got)
			}
		})
	}
}

// Neither side populated still means no permissions block at all, which is how
// the $SYS user ships. Note what the omission is NOT doing: an empty
// "permissions {}" block would not close the user down either -- it parses to a
// non-nil *Permissions with both sides nil and reads as unrestricted, same as
// no block. Omitting it is how sys keeps the $SYS account's own privileges,
// which is what sys is for, not a denial.
func TestAStaticUserWithNoSubjectsRendersNoPermissionsBlock(t *testing.T) {
	got := renderA2AStaticUser(a2aIdentity{
		user:    "sys",
		account: a2aAccountSys,
		auth:    a2aAuthStatic,
		comment: "the system account's own user",
	}, "pw")

	if strings.Contains(got, "permissions") {
		t.Errorf("a user with no subject lists gained a permissions block; it would not be denied anything by it, so the block is noise:\n%s", got)
	}
}

// The load-bearing half of the case above. Rendering no permissions block is
// safe for sys and for nothing else: the user that gets it is unrestricted
// inside its account, which is the whole defect this file otherwise closes. sys
// is allowed it because it is the $SYS account's own operator login. If a
// second identity ever arrives with neither side populated, it would be handed
// the entire subject space silently -- no validator covers this path, and the
// render itself cannot tell the two apart. So the invariant is pinned here
// rather than inferred: exactly one static identity has no subjects, and it is
// sys.
//
// TestEveryNATSUserGrantIsEnumeratedAndStreamScoped would also fail on a new
// neither-side identity -- it needs a row, and a static row with an empty list
// fails that test's rule 1. It is kept separate because the failure it gives is
// "sys has no publish allow-list", which reads as a missing grant; the one here
// names what is actually wrong, that the principal is unrestricted inside its
// account.
func TestSysIsTheOnlyStaticIdentityWithNoSubjectsOfItsOwn(t *testing.T) {
	var unrestricted []string
	for _, id := range staticIdentities(identityTestAgent()) {
		if len(id.publish) == 0 && len(id.subscribe) == 0 {
			unrestricted = append(unrestricted, id.user)
		}
	}

	want := []string{"sys"}
	if !slices.Equal(unrestricted, want) {
		t.Errorf("static identities rendering no permissions block = %v, want %v.\n"+
			"Every name here is unrestricted inside its account. A new one needs either subjects of its own "+
			"or a recorded reason it should hold its account's full privileges the way sys does.",
			unrestricted, want)
	}
}
