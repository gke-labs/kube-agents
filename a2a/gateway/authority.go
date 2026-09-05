package gateway

import "encoding/json"

// The authority block is the request-level field the gateway populates at
// ingress: who asked, verified how, in front of whom. It is ADVISORY — until
// connection-bound publisher identity arms, nothing stops another bus client
// from inventing one, so consumers MUST NOT authorize on it. It is carried
// now for the audit trail and parity testing (payload spec 0.3 rule).

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

// Authority is the advisory block. Grants stays null until the attenuating
// capability work lands.
type Authority struct {
	Requester AuthorityRequester `json:"requester"`
	Audience  AuthorityAudience  `json:"audience"`
	Grants    json.RawMessage    `json:"grants"`
}

// BuildAuthority assembles the block for one turn. principal and subject are
// plaintext here; hashing is this function's job so no caller can forget it.
// rosterIDs are backend-native ids, pseudonymized likewise; entries with a
// principal mapping are recorded as mapped principals instead (gateway
// design: "mapped principals where the mapping exists, backend subjects
// where it doesn't").
func BuildAuthority(ps *Pseudonymizer, pm *PrincipalMap, principal, backend, subjectID, verifiedBy, conversation, kind string, rosterIDs []string, rosterComplete bool) json.RawMessage {
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
	a := Authority{
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
	data, err := json.Marshal(a)
	if err != nil {
		// Marshal of this struct cannot fail; a nil block (advisory anyway) is
		// the safe degradation if it ever does.
		return nil
	}
	return data
}
