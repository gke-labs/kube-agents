package gateway

import (
	"context"
	"encoding/json"

	"github.com/gke-labs/kube-agents/a2a/capability"
)

// The authority block is the request-level field the gateway populates at
// ingress: who asked, verified how, in front of whom, and what this request is
// permitted to do.
//
// `requester` and `audience` are ADVISORY and stay that way. Nothing stops
// another bus client from inventing them, so consumers MUST NOT authorize on
// them; they are the audit trail.
//
// `grants` is not advisory, and it is not a claim either. It carries a
// reference — a key in the `cap` bucket and the revision the write returned —
// and the capability it names was written on a subject only the gateway may
// publish to. A consumer does not read the entry (no broker may) and does not
// trust the block: it hands the reference to the verifier, which reads the
// chain and answers whether a verb is permitted. Inventing a reference gets an
// attacker a key the gateway never wrote, or one whose delegate is somebody
// else; both are refused. See docs/architecture/09-capability-envelope.md.

// rosterCap bounds the audience snapshot; past it rosterComplete is false and
// the eventual LCD tool reads membership live instead (decided 8/24).
const rosterCap = 32

// AuthorityRequester identifies who asked. All identifiers are pseudonymous:
// HMAC under the install salt (decided 8/24).
type AuthorityRequester struct {
	Principal  string `json:"principal"`
	Backend    string `json:"backend"`
	Subject    string `json:"subject"`
	VerifiedBy string `json:"verifiedBy"`
}

// AuthorityAudience snapshots the room at the moment of the ask. Snapshots,
// deliberately: when the classifier later asks "who could have read this,"
// the answer is in the envelope for that turn.
type AuthorityAudience struct {
	Conversation   string   `json:"conversation"`
	Kind           string   `json:"kind"`
	Roster         []string `json:"roster"`
	RosterComplete bool     `json:"rosterComplete"`
}

// AuthorityGrants is what `grants` carries: a reference and nothing else.
//
// Deliberately not the tier and the scope. Putting them on the wire would let a
// consumer authorize on content it did not verify, which is the one thing 09
// forbids, and it would do it in the shape that looks most like working code.
type AuthorityGrants struct {
	Capability capability.Ref `json:"capability"`
}

// Authority is the block. Build it with BuildAuthority and render it once per
// envelope with Render, which is where the capability reference goes in: one
// task has one capability, minted at ingress, referenced by the submission and
// by every steer and cancel that follows it.
type Authority struct {
	Requester AuthorityRequester `json:"requester"`
	Audience  AuthorityAudience  `json:"audience"`
	Grants    json.RawMessage    `json:"grants"`
}

// Render marshals the block for one envelope. A nil ref renders `grants: null`,
// which is what a turn with no task behind it carries — a status ask, a
// refusal, anything the gateway answers itself.
func (a Authority) Render(ref *capability.Ref) json.RawMessage {
	a.Grants = json.RawMessage("null")
	if ref != nil {
		// Marshal of this struct cannot fail; leaving grants null if it
		// somehow did is the fail-closed degradation, because an
		// executor refuses a task with no capability.
		if b, err := json.Marshal(AuthorityGrants{Capability: *ref}); err == nil {
			a.Grants = b
		}
	}
	data, err := json.Marshal(a)
	if err != nil {
		return nil
	}
	return data
}

// BuildAuthority assembles the block for one turn. principal and subject are
// plaintext here; hashing is this function's job so no caller can forget it.
// rosterIDs are backend-native ids, pseudonymized likewise; entries with a
// principal mapping are recorded as mapped principals instead (gateway
// design: "mapped principals where the mapping exists, backend subjects
// where it doesn't").
func BuildAuthority(ps *Pseudonymizer, pm *PrincipalMap, principal, backend, subjectID, verifiedBy, conversation, kind string, rosterIDs []string, rosterComplete bool) Authority {
	roster := make([]string, 0, len(rosterIDs))
	complete := rosterComplete
	for _, id := range rosterIDs {
		if len(roster) >= rosterCap {
			complete = false
			break
		}
		entry := id
		if p := pm.Resolve(id); p != "" {
			entry = p
		}
		roster = append(roster, ps.Hash(entry))
	}
	return Authority{
		Requester: AuthorityRequester{
			Principal:  ps.Hash(principal),
			Backend:    backend,
			Subject:    ps.Hash(subjectID),
			VerifiedBy: verifiedBy,
		},
		Audience: AuthorityAudience{
			Conversation:   conversation,
			Kind:           kind,
			Roster:         roster,
			RosterComplete: complete,
		},
		Grants: json.RawMessage("null"),
	}
}

// mintCapability writes this task's root capability and returns the pinned
// reference.
//
// The JetStream handle is fetched per call rather than cached: it binds to the
// connection and does not survive a terminal rebuild (NR-2). The Minter
// publishes straight to the bucket's own subject and never binds the bucket,
// so the gateway holds exactly one grant on the capability path — publish under
// `$KV.cap.root.*` — and no read of any kind.
func (g *Gateway) mintCapability(ctx context.Context, taskID, delegate string) (*capability.Ref, error) {
	ref, err := capability.NewMinter(g.client.JetStream()).Mint(ctx, taskID, capability.Entry{
		Tier:     g.cfg.AuthorityTier,
		Scope:    g.cfg.AuthorityScope,
		Delegate: delegate,
	})
	if err != nil {
		return nil, err
	}
	return &ref, nil
}
