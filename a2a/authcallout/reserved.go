package authcallout

import (
	"fmt"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Reserved principal names: the users a narrowed pod may not be named after.
//
// A narrowed user is named for its pod, and that name is also its inbox
// prefix: sessionGrants hands it `_INBOX.<pod>.>` on both sides. A pod named
// after another principal would therefore be granted that principal's inbox,
// and could read the JetStream replies delivered there or publish forged ones
// into it. The gateway mints session pod names that never collide with a
// principal; a pod someone created by hand under a narrowed ServiceAccount can
// be named anything.
//
// Three kinds of name are reserved, and one check covers all of them:
//
//   - The static users nats.conf authenticates by password (gateway, bridge,
//     seed, web, console, sys, and the callout's own user). They are in no
//     identity map, so the callout cannot learn them from the map it serves.
//     The operator renders them into the callout's environment instead, from
//     the same list it renders nats.conf's auth_users from, as a
//     comma-separated list. The callout does not read nats.conf: that file
//     carries every static user's password, and the callout has no reason to
//     hold any of them.
//   - The identity map's own users (provision, agent, verifier, session, and
//     whatever else the map holds). authorize mints each entry that is not
//     narrowed under its `user`, and that entry's grants carry
//     `_INBOX.<user>.>`, so these are the names whose inboxes a same-named pod
//     would share. The narrowed entry's own user (session) is never minted,
//     since its connections are renamed to their pods; it is reserved with the
//     rest, harmlessly, because one check covers every map user. They are
//     read from the map being served, built when the map is parsed (see
//     IdentityMap.users), so a reload that adds or removes a user moves the
//     reserved set in the same pointer swap that moves the map. A removed
//     user's name is released at once, but the swap revokes nothing:
//     connections the removed principal already holds keep its inbox until
//     their user JWT expires, at most GrantTTL (defaultGrantTTL, one hour,
//     since the operator does not set A2A_GRANT_TTL_SECONDS), and a narrowed
//     pod named after it can be minted the same inbox inside that window.
//     Only an operator re-render removes a map user; the window is accepted.
//   - The fixed-name addressees (the bridge's `platform`). A narrowed pod's
//     task subjects are keyed on its name, so a pod named after one would be
//     handed that addressee's subjects rather than an inbox. The operator
//     renders them separately, as A2A_RESERVED_ADDRESSEES; see addressees.go.
//
// A name in more than one is reported as a static principal first, then as an
// addressee, then as a map user. Neither overlap happens in a rendered config
// (the operator's contract test keeps the static residue out of the map, and
// no static user is named after an addressee), and the static wording is the
// one the refusal has always carried.

const (
	// reservedPrincipalSeparator splits the operator-rendered list.
	reservedPrincipalSeparator = ","
)

// ParseReservedPrincipals reads the operator-rendered list of static principal
// names. It fails closed: an empty list, an empty element, or a name that is
// not a single dot-free DNS-1123 label is an error, never a smaller set. A
// smaller set would admit a narrowed pod named after the dropped principal, and
// a parse that quietly skipped a malformed name would do exactly that.
//
// The label check is also what makes an exact comparison the right one. Pod
// names are DNS-1123 and so lowercase; a reserved name that is lowercase
// DNS-1123 too can be compared byte for byte, and a rendered name in any other
// case is refused here rather than silently never matching a pod.
// Identity.validate holds map users to the same label, so the comparison is
// exact for both halves: a dotted map user `a.b` would otherwise sit inside the
// inbox of a pod named `a` without matching it.
func ParseReservedPrincipals(raw string) ([]string, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, fmt.Errorf("the reserved principal list is empty; a narrowed pod could take any static principal's name and inbox")
	}
	var names []string
	for _, field := range strings.Split(raw, reservedPrincipalSeparator) {
		name := strings.TrimSpace(field)
		if name == "" {
			return nil, fmt.Errorf("the reserved principal list %q has an empty element", raw)
		}
		if !lib.ValidSubjectToken(name) {
			return nil, fmt.Errorf("reserved principal %q is not a dot-free DNS-1123 label, so no pod could be refused for it", name)
		}
		names = append(names, name)
	}
	return names, nil
}

// reservedKind is what a refused pod name copies, phrased to complete "which is
// the name of ...". The end state is one name-to-kind map with one check; the
// static names are held that way here, and the map's users join them in
// reservedAs because they change with every reload while the static names are
// fixed for the life of the process.
type reservedKind string

const (
	reservedStatic    reservedKind = "a static principal"
	reservedMapUser   reservedKind = "an identity-map user"
	reservedAddressee reservedKind = "an addressee"
	// reservedProfile is an AgentProfile's addressee: a session pod's task
	// subjects are its pod name, so a session-ServiceAccount pod named after
	// a profile would be minted that profile's events and input. Read from
	// the served map's profile entries, so it follows a reload.
	reservedProfile reservedKind = "an AgentProfile's addressee"
)

// copied completes the refusal: what the pod would have been handed.
func (k reservedKind) copied() string {
	if k == reservedAddressee || k == reservedProfile {
		return "its task subjects are that addressee's"
	}
	return "its inbox is that principal's"
}

// reservedSet builds the fixed half of the lookup the callout refuses against:
// the static principals and the fixed-name addressees. It refuses an empty
// input on either side for the same reasons ParseReservedPrincipals and
// ParseReservedAddressees do, so a caller that skipped the parsers cannot
// construct a Service that reserves nothing of either kind.
func reservedSet(principals, addressees []string) (map[string]reservedKind, error) {
	if len(principals) == 0 {
		return nil, fmt.Errorf("the callout needs the static principal names; with none, a narrowed pod could take any of their inboxes")
	}
	if len(addressees) == 0 {
		return nil, fmt.Errorf("the callout needs the reserved addressee names; with none, a narrowed pod could take any addressee's task subjects")
	}
	set := make(map[string]reservedKind, len(principals)+len(addressees))
	for _, n := range addressees {
		if !lib.ValidSubjectToken(n) {
			return nil, fmt.Errorf("reserved addressee %q is not a dot-free DNS-1123 label, so no pod could be refused for it", n)
		}
		set[n] = reservedAddressee
	}
	// Static second, so a name in both is reported as a static principal.
	for _, n := range principals {
		if !lib.ValidSubjectToken(n) {
			return nil, fmt.Errorf("reserved principal %q is not a dot-free DNS-1123 label, so no pod could be refused for it", n)
		}
		set[n] = reservedStatic
	}
	return set, nil
}

// reservedAs reports whether a narrowed user name is reserved, and as which
// kind. m must be the map the connection's identity was resolved against, so
// the check and the lookup see one snapshot: a reload landing mid-request
// cannot pair a new map with an old reserved set or the reverse.
func (s *Service) reservedAs(m *IdentityMap, user string) (reservedKind, bool) {
	if kind, ok := s.reserved[user]; ok {
		return kind, true
	}
	if m.servesUser(user) {
		return reservedMapUser, true
	}
	if slices.Contains(m.Profiles(), user) {
		return reservedProfile, true
	}
	return "", false
}
