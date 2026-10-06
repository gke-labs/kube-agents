// Package authcallout implements the NATS auth callout service the deployment
// spec specifies: it validates a client's Kubernetes ServiceAccount token
// against the cluster and answers with the account and permission set that
// identity is mapped to.
//
// The property it exists to preserve is the deployment spec's, unchanged: the
// bus decides who may say what before a message is read. What changes is where
// the permission set comes from. Statically rendered users put one password per
// role in a Secret, so every workload sharing a role shares a credential and
// the grant list can only be as fine as the roles someone thought to write. The
// callout resolves an identity the cluster already issues and vouches for, so
// the grant list can be as fine as the identities are.
//
// The agents never read the map. The constrained party does not see its own
// ceiling; it just hits it.
package authcallout

import (
	"encoding/json"
	"fmt"
	"slices"
	"sort"
	"strings"
)

// mintableAccounts is every NATS account the callout will issue a user into.
//
// The deployment renders exactly one application account, and the callout's own
// account holds nothing but the callout. Anything else - $SYS above all - is
// either a mistake or an attempt, and both are worth refusing at the point the
// map is parsed rather than at the point a connection lands somewhere it should
// not be.
var mintableAccounts = []string{"APP"}

const (
	// ServiceAccountPrefix is how the Kubernetes TokenReview API spells a
	// ServiceAccount in the username it returns:
	// system:serviceaccount:<namespace>:<name>. Entries are keyed by that
	// exact string so the lookup compares against what the API server said
	// rather than against something reassembled from parts.
	ServiceAccountPrefix = "system:serviceaccount:"

	// serviceAccountFields is the token count of a well-formed
	// ServiceAccount username once split on ":" — system, serviceaccount,
	// namespace, name.
	serviceAccountFields = 4
)

// Grants is one identity's subject permissions. The lists are exact rather
// than namespace wildcards wherever the deployment spec requires it.
//
// Deny-by-default holds per side and only while that side has entries in it: a
// subject absent from a NON-EMPTY list is refused by the server, but a side
// with no entries at all is read by nats-server as "unrestricted" rather than
// as "nothing", and the two sides are independent. So an entry granting one
// subscribe and no publishes is a client that may publish anywhere. Neither
// list may be empty; validate refuses that, and Service.authorize refuses it
// again at the mint for grant sets that never came from a map.
type Grants struct {
	Publish   []string `json:"publish"`
	Subscribe []string `json:"subscribe"`
}

// Identity is one entry in the map: a Kubernetes ServiceAccount, the NATS user
// it authenticates as, and what that user may do.
type Identity struct {
	// ServiceAccount is the full TokenReview username this entry matches.
	ServiceAccount string `json:"serviceAccount"`

	// User is the NATS user name issued for this identity. It is not
	// decoration: the grant lists carry a per-user inbox prefix
	// (_INBOX.<user>.>), and a client must set a matching custom inbox
	// prefix or every reply it waits on times out. The operator renders
	// this name and the client's own copy of it from one source for that
	// reason.
	User string `json:"user"`

	// Account is the NATS account the issued user lands in. The account is
	// the tenant boundary and the blast-radius container.
	Account string `json:"account"`

	Grants Grants `json:"grants"`

	// Narrowing, when set, means this entry's grants are not in the map at
	// all: they are derived at mint time from a claim the API server
	// attested about the particular workload connecting: NarrowingPod or
	// NarrowingProfile. Such an entry MUST carry no
	// grants, and the map is refused if it does — see session.go for why an
	// entry that could hold real grants AND be narrowed is one skipped code
	// path away from handing every session everything.
	Narrowing string `json:"narrowing,omitempty"`

	// Profile is the AgentProfile a NarrowingProfile entry executes for: the
	// addressee token on the task subjects its pods publish. Set only with
	// that narrowing.
	Profile string `json:"profile,omitempty"`

	// Topics are a NarrowingProfile entry's blackboard grants in the
	// profile's own spelling; the callout composes the subjects (see
	// profile_narrowing.go). Set only with that narrowing.
	Topics *TopicGrants `json:"topics,omitempty"`
}

// IdentityMap is what the operator renders and the callout serves. Version is
// the operator's content hash of the entries; the callout reports the version
// it is serving so "the map says X" is checkable against the running system
// rather than against the rendered object.
type IdentityMap struct {
	Version    string     `json:"version"`
	Identities []Identity `json:"identities"`
}

// ParseIdentityMap decodes a rendered map and rejects one it cannot serve
// safely. Rejecting here rather than at lookup time is deliberate: a malformed
// entry that surfaces only when its identity happens to connect is a refusal
// for one legitimate workload at an arbitrary later moment, which is the
// failure mode the BusCredentialsReady ordering exists to prevent.
func ParseIdentityMap(raw []byte) (*IdentityMap, error) {
	var m IdentityMap
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		return nil, fmt.Errorf("decoding identity map: %w", err)
	}
	if err := m.validate(); err != nil {
		return nil, err
	}
	return &m, nil
}

func (m *IdentityMap) validate() error {
	if m.Version == "" {
		return fmt.Errorf("identity map has no version")
	}
	// An empty map is never intentional. The operator always renders at
	// least the provisioner and the session principal, so zero entries
	// means the render
	// produced nothing — and serving it would refuse every
	// connection on a callout that reports itself perfectly healthy. Refuse
	// it here so the previous map keeps serving and the reason is logged.
	if len(m.Identities) == 0 {
		return fmt.Errorf("identity map serves no identities; every connection would be refused")
	}
	seenSA := make(map[string]bool, len(m.Identities))
	seenUser := make(map[string]bool, len(m.Identities))
	for i, id := range m.Identities {
		if err := id.validate(); err != nil {
			return fmt.Errorf("identity %d: %w", i, err)
		}
		// Two entries for one ServiceAccount would make the grant set a
		// function of map order, and two ServiceAccounts sharing a NATS
		// user would silently re-create the shared-credential problem
		// the callout exists to end — the second one also collides on
		// the first one's inbox prefix.
		if seenSA[id.ServiceAccount] {
			return fmt.Errorf("identity %d: duplicate serviceAccount %q", i, id.ServiceAccount)
		}
		if seenUser[id.User] {
			return fmt.Errorf("identity %d: duplicate user %q", i, id.User)
		}
		seenSA[id.ServiceAccount] = true
		seenUser[id.User] = true
	}
	return nil
}

func (id Identity) validate() error {
	if !strings.HasPrefix(id.ServiceAccount, ServiceAccountPrefix) ||
		len(strings.Split(id.ServiceAccount, ":")) != serviceAccountFields {
		return fmt.Errorf("serviceAccount %q is not %s<namespace>:<name>", id.ServiceAccount, ServiceAccountPrefix)
	}
	if id.User == "" {
		return fmt.Errorf("serviceAccount %q has no user", id.ServiceAccount)
	}
	if id.Account == "" {
		return fmt.Errorf("user %q has no account", id.User)
	}
	// Allowlisted, not merely non-empty. The account name in a map entry
	// becomes the audience of the user JWT this callout signs, which is what
	// decides the account a connection lands in - so an entry naming SYS would
	// mint a system-account user with whatever grants it also names. Writing
	// the map is already a privileged act, but the callout is the enforcement
	// point and has no reason to honour an account the deployment never
	// renders.
	if !slices.Contains(mintableAccounts, id.Account) {
		return fmt.Errorf("user %q names account %q, which this callout will not mint into (allowed: %v)",
			id.User, id.Account, mintableAccounts)
	}
	hasGrants := len(id.Grants.Publish) > 0 || len(id.Grants.Subscribe) > 0
	// The profile fields mean something only under NarrowingProfile. On any
	// other entry they would be read by nothing, and a field the callout
	// silently ignores is a grant someone believes exists.
	if id.Narrowing != NarrowingProfile && (id.Profile != "" || id.Topics != nil) {
		return fmt.Errorf("user %q carries profile or topics but narrows on %q; only %q reads them", id.User, id.Narrowing, NarrowingProfile)
	}
	switch id.Narrowing {
	case "":
		// An entry with an empty side is almost certainly a render bug,
		// and serving it is not the harmless failure it looks like: an
		// empty list is the server's spelling of "unrestricted", not of
		// "nothing", and the sides are independent. A render that dropped
		// the publish list would hand this principal the whole subject
		// space to publish on — $JS.API.STREAM.DELETE.TASKS included —
		// while its subscribes stayed correctly narrow, which is the
		// version of this bug nobody would notice. Refuse the map instead.
		//
		// This is checked per side rather than across both because the
		// server is per side. There is deliberately no way to spell "may
		// publish nothing" here: Grants carries no deny list, so a
		// principal that must not publish cannot be expressed as an empty
		// allow list and has to be refused rather than silently widened.
		if len(id.Grants.Publish) == 0 || len(id.Grants.Subscribe) == 0 {
			return fmt.Errorf("user %q has %d publish and %d subscribe grants; an empty side is minted as unrestricted on that side, not as closed",
				id.User, len(id.Grants.Publish), len(id.Grants.Subscribe))
		}
	case NarrowingPod:
		// The fail-closed shape, and the reason narrowing is a field
		// rather than a convention. If a narrowed entry could also carry
		// grants, then one map edit — or one code path that forgot to
		// narrow — would hand every session pod whatever was written
		// there, which is the shared `worker` credential reborn under a
		// new name. An entry that is unusable without its claim cannot be
		// widened by editing the map alone.
		if hasGrants {
			return fmt.Errorf("user %q narrows on %q, so its grants are derived from the attested claim and the map must carry none; it carries %d publish and %d subscribe",
				id.User, id.Narrowing, len(id.Grants.Publish), len(id.Grants.Subscribe))
		}
	case NarrowingProfile:
		// The same fail-closed shape as NarrowingPod: the map carries the
		// profile and its topics, never subjects.
		if hasGrants {
			return fmt.Errorf("user %q narrows on %q, so its grants are derived from the profile and the attested pod and the map must carry none; it carries %d publish and %d subscribe",
				id.User, id.Narrowing, len(id.Grants.Publish), len(id.Grants.Subscribe))
		}
		if err := validateProfileEntry(id); err != nil {
			return err
		}
	default:
		return fmt.Errorf("user %q names narrowing %q, which this callout does not implement", id.User, id.Narrowing)
	}
	return nil
}

// Lookup returns the identity mapped to a TokenReview username. The bool
// result distinguishes an unmapped identity — which the callout refuses
// outright — from a mapped one, and callers must not treat a zero Identity as
// a usable grant set.
func (m *IdentityMap) Lookup(serviceAccount string) (Identity, bool) {
	for _, id := range m.Identities {
		if id.ServiceAccount == serviceAccount {
			return id, true
		}
	}
	return Identity{}, false
}

// Users returns the NATS user names the map serves, sorted. The operator reads
// this back when deciding whether the callout is serving the identity a
// workload is about to need.
func (m *IdentityMap) Users() []string {
	users := make([]string, 0, len(m.Identities))
	for _, id := range m.Identities {
		users = append(users, id.User)
	}
	sort.Strings(users)
	return users
}
