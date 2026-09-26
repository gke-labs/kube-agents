// Package capability implements the attenuating capability envelope of
// docs/architecture/09-capability-envelope.md.
//
// There is no token format here, nothing is signed, and no key sits on the
// capability path. A capability lives in the NATS KV bucket `cap`; a message
// carries a lookup id and the revision the write returned, never the
// capability itself. Integrity comes from two places and neither is
// cryptography: the server's subject permissions, fixed when a client
// authenticates, decide who could have written a key; and every reference pins
// a revision, so an entry rewritten after it was resolved no longer resolves.
//
// The package holds the data shapes and the rules. The chain walk is in
// verify.go and is the verification service's alone — 09 §4 is explicit that no
// broker may read the bucket, and a broker that imports this package gets the
// shapes and the writer side, not a reader.
package capability

import (
	"strconv"
	"strings"
)

// Tier is the authority tier an entry carries. The vocabulary is
// 02-agent-personas §9's controller label `kube-agents/tier`, and the order
// below is the whole lattice: platform ⊃ cluster-admin ⊃ developer-team.
type Tier string

const (
	TierPlatform      Tier = "platform"
	TierClusterAdmin  Tier = "cluster-admin"
	TierDeveloperTeam Tier = "developer-team"
)

// tierRank orders the lattice. Lower is narrower. A tier absent from this map
// is not a tier: an entry carrying one is refused rather than ranked, because
// the alternative is an unknown string comparing equal to itself and passing
// the narrowing check against anything.
var tierRank = map[Tier]int{
	TierDeveloperTeam: 0,
	TierClusterAdmin:  1,
	TierPlatform:      2,
}

// Scope is a `/`-segmented resource path, and containment is segment prefix:
//
//	project/P
//	project/P/cluster/C
//	project/P/cluster/C/ns/N
//
// 09 §3's example narrows `scope: project-P` to `scope: cluster-C` with nothing
// linking the two, which cannot be checked — a cluster named in isolation could
// belong to any project. The path form is the amendment 09 is owed; it is also
// what makes "narrower" decidable without a registry lookup on the request
// path.
type Scope string

// Ref pins one entry: the key AND the revision the write returned. Nothing in
// this package ever refers to an entry by key alone. A KV put on an existing
// key is an update rather than an error and a KV delete is itself a publish to
// the key's own subject, so the one permission that lets a broker create a link
// also lets it rewrite that link after the link was resolved and acted on. The
// pin is what closes both: an overwrite moves the entry to a new revision and a
// delete-and-recreate lands at a fresh sequence, so either way the pin dangles.
type Ref struct {
	Key      string `json:"key"`
	Revision uint64 `json:"revision"`
}

func (r Ref) String() string { return r.Key + " @" + strconv.FormatUint(r.Revision, 10) }

// Entry is 09 §3's shape: three fields, plus the pinned link to the parent it
// descends from. A root has no parent.
//
// There is deliberately no action list. A4 owes 09 that amendment along with
// the per-call lookup, and adding one here would ship a schema A4 immediately
// amends. What a verb is permitted against is the tier — see Permits.
type Entry struct {
	// Tier and Scope are the authority. Both may only narrow along the chain.
	Tier  Tier  `json:"tier"`
	Scope Scope `json:"scope"`

	// Delegate is the single principal permitted to write children of this
	// entry, and the single principal permitted to resolve it. One field,
	// two jobs, one level apart: the writer of an entry must be the
	// delegate its *parent* names, and the resolver of an entry must be the
	// delegate *it* names. Both are the party that was handed the id, which
	// is the point — request ids are not secrets, so naming one is no
	// barrier and the prefix alone proves nothing about entitlement.
	//
	// It holds a per-request principal, not an agent id. 09 §5 says a
	// per-request caller compared against a per-agent delegate field "buys
	// nothing", and it is right; both sides move. The session pod name is
	// the principal, the gateway allocates it and the taskId in the same
	// breath at spawn, and the auth callout derives that pod's grants from
	// the API server's attestation. So the name is predictable by the
	// parent before the credential exists, which is what 09 §5 asks for.
	Delegate string `json:"delegate"`

	// Parent pins the entry this one descends from. Nil for a root.
	Parent *Ref `json:"parent,omitempty"`
}

// Key prefixes. A bucket key does not live at its bare name: bucket `cap` is
// stream `KV_cap` on `$KV.cap.>`, and the key is appended to that prefix. These
// are the key names; the subject permissions that make them mean anything are
// rendered by the operator and asserted by the conformance suite.
//
// The bucket's own name is deliberately NOT repeated here. 09 spells the key
// `cap.root.<request-id>` in its worked example and the permission
// `$KV.cap.root.*` in §4, and those two cannot both be true: a key of
// `cap.root.x` in bucket `cap` is the subject `$KV.cap.cap.root.x`, which that
// permission does not match. §4 is the half that is load-bearing — it is the
// security control — so the key drops the bucket name and the subject comes out
// exactly as §4 writes it. The conformance suite in a2a/authcallout is what
// found this, by minting through a real permission set; every test in this
// package passed with the stutter in place, because none of them crossed a
// server. 09 §3's worked example is amended to match.
const (
	// RootPrefix is the gateway's namespace and only the gateway's. On the
	// wire: `$KV.cap.root.<request-id>`.
	RootPrefix = "root."
	// HopPrefix is followed by the writing principal's own name. On the
	// wire: `$KV.cap.hop.<principal>.<n>`.
	HopPrefix = "hop."
)

// MaxDepth bounds the chain walk. The bound is not a performance cushion: a
// broker holds publish across `$KV.cap.hop.<its-own-name>.*`, so it can write two
// entries in its own namespace naming each other as parent, each naming itself
// as delegate, with identical payloads. Every other rule holds — both writes
// are inside its permitted subject, each entry's parent names it as delegate,
// and equality satisfies narrowing — and the walk never reaches a terminal.
// One broker, using only the permissions this design grants it, would hang the
// single service on the request path.
const MaxDepth = 8

// checkToken guards one token of a KV key. Keys are dotted, so a name
// containing a dot would silently restructure the key and change who the prefix
// says wrote it. Session pod names are DNS-1123 labels and taskIds are `task-` plus hex,
// so nothing legitimate is rejected here.
func checkToken(what, s string) error {
	switch {
	case s == "":
		return refuse("%s is empty", what)
	case strings.ContainsAny(s, ".*> \t"):
		return refuse("%s contains a character that is not legal in a key token", what)
	}
	return nil
}

// RootKey is where the gateway mints. The request id is the taskId, which the
// gateway allocates.
func RootKey(requestID string) (string, error) {
	if err := checkToken("request id", requestID); err != nil {
		return "", err
	}
	return RootPrefix + requestID, nil
}

// HopKey is where a broker writes a child. The principal token must be the
// writer's own name — the server enforces that, and this only spells it.
func HopKey(principal string, n int) (string, error) {
	if err := checkToken("principal", principal); err != nil {
		return "", err
	}
	if n < 0 {
		return "", refuse("hop index is negative")
	}
	return HopPrefix + principal + "." + strconv.Itoa(n), nil
}

// writerOf says which principal the key's own shape proves wrote it. This is
// the whole of "who wrote this link" — the subject prefix proves it, because
// forging a write under another principal's prefix means publishing on a
// subject the server refuses you.
//
// A root's writer is the gateway, structurally: `$KV.cap.root.*` is the
// gateway's namespace and nobody else's, and the key does not name it.
func writerOf(key string) (principal string, isRoot bool, err error) {
	switch {
	case strings.HasPrefix(key, RootPrefix):
		rest := strings.TrimPrefix(key, RootPrefix)
		if rest == "" || strings.Contains(rest, ".") {
			return "", false, refuse("root key is not `root.<request-id>`")
		}
		return "", true, nil
	case strings.HasPrefix(key, HopPrefix):
		rest := strings.TrimPrefix(key, HopPrefix)
		name, idx, ok := strings.Cut(rest, ".")
		if !ok || name == "" || idx == "" || strings.Contains(idx, ".") {
			return "", false, refuse("hop key is not `hop.<principal>.<n>`")
		}
		return name, false, nil
	default:
		return "", false, refuse("key is in neither the root nor the hop namespace")
	}
}

// Validate checks an entry is well formed before any rule is applied to it. An
// entry that fails here is refused rather than interpreted: an unknown tier
// would otherwise compare equal to itself and satisfy narrowing against any
// parent carrying the same unknown string.
func (e Entry) Validate() error {
	if _, ok := tierRank[e.Tier]; !ok {
		return refuse("entry carries a tier that is not in the lattice")
	}
	if err := e.Scope.validate(); err != nil {
		return err
	}
	if err := checkToken("delegate", e.Delegate); err != nil {
		return err
	}
	if e.Parent != nil {
		if e.Parent.Key == "" {
			return refuse("parent reference has no key")
		}
		if e.Parent.Revision == 0 {
			// A revision of zero is what an unpinned reference looks
			// like after a round trip through JSON, and it is the one
			// value the KV API reads as "latest". Refusing it here is
			// what keeps an unpinned chain from resolving at all.
			return refuse("parent reference is not pinned to a revision")
		}
	}
	return nil
}

func (s Scope) validate() error {
	if s == "" {
		return refuse("entry carries an empty scope")
	}
	segs := strings.Split(string(s), "/")
	if len(segs)%2 != 0 {
		return refuse("scope is not a sequence of kind/name segments")
	}
	for _, seg := range segs {
		if seg == "" {
			return refuse("scope has an empty segment")
		}
	}
	return nil
}

// Contains reports whether s contains other: every segment of s is a prefix of
// other's, segment-wise. A scope contains itself.
func (s Scope) Contains(other Scope) bool {
	if s == other {
		return true
	}
	a, b := strings.Split(string(s), "/"), strings.Split(string(other), "/")
	if len(a) > len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// Narrows reports whether child is no wider than parent on either axis.
// Equality is narrowing: a hop that forwards what it received is within the
// rules, and 09 §5 is honest that stopping it is not something a KV scheme can
// do. What this rule buys is the bound that makes that survivable — a hop
// cannot exceed what it was delegated.
func Narrows(parent, child Entry) error {
	pr, ok := tierRank[parent.Tier]
	if !ok {
		return refuse("parent carries a tier that is not in the lattice")
	}
	cr, ok := tierRank[child.Tier]
	if !ok {
		return refuse("child carries a tier that is not in the lattice")
	}
	if cr > pr {
		return refuse("child widens the tier")
	}
	if !parent.Scope.Contains(child.Scope) {
		return refuse("child widens the scope")
	}
	return nil
}

// NamespaceScope is the scope a component defaults to when nothing told it a
// better one: its own namespace, which is the narrowest thing certainly true
// of it. The gateway mints under it and the executor checks against it, so
// they have to spell it the same way — hence one function rather than two
// string concatenations that agree until somebody edits one.
//
// An empty namespace yields "namespace/-". "-" is a legal scope segment and
// is not a legal DNS-1123 namespace name, so no rendered namespace is ever
// inside it and no rendered namespace ever contains it: against a real scope
// the placeholder mismatches in both directions, which is what makes an
// unrendered half of the pair fail closed.
//
// It is a mismatch marker and not a deny-all, and the difference matters.
// Contains treats equality as containment, so a capability minted at
// "namespace/-" does permit a request at "namespace/-" — the pairing two
// components that defaulted *together* produce. On a rendered install neither
// half defaults: Config.FromEnv reads POD_NAMESPACE with "kubeagents-system"
// as its fallback, and the spawner writes the gateway's resolved scope onto
// the session pod as A2A_AUTHORITY_SCOPE. A Config built without Namespace —
// tests, and embedders that bypass FromEnv — is the case that reaches the
// tautology, and what it waives is this axis alone; the delegate check still
// binds. Pinned by TestTheNamespacePlaceholderIsAMismatchMarkerNotADenyAll.
func NamespaceScope(namespace string) Scope {
	if namespace == "" {
		return Scope("namespace/-")
	}
	return Scope("namespace/" + namespace)
}
