package gateway

import "strings"

// The platform agent's trusted-human allowlists, rendered by the operator from
// the PlatformAgent CR's integration.{googleChat,slack}.allowedUsers. They
// gate the one thing the ingress allowlist does not: a session asking the
// gateway to mint a child task to the platform agent on a human's behalf
// (spec-chatops-gateway.md, "Sessions by default"). The names are spelled
// here and in the operator; the conformance suite pins them equal.
const (
	EnvTargetAllowedUsersGchat = "A2A_TARGET_ALLOWED_USERS_GCHAT"
	EnvTargetAllowedUsersSlack = "A2A_TARGET_ALLOWED_USERS_SLACK"
)

// targetPlatform is the only target with a rendered list today.
const targetPlatform = "platform"

// targetAllowed is Config.TargetAllowedUsers compiled for lookup: target ->
// backend -> set of requester subjects (requesterSubject of each entry), so
// the set compares against the pseudonym the session KV stores and never
// against plaintext.
type targetAllowed map[string]map[string]map[string]bool

// requesterSubject is the requester as the gateway stores and compares it:
// the author id normalized in its backend's vocabulary (trimmed; Google Chat
// ids are emails and lowercased, Slack member ids and every other backend's
// kept exact), then pseudonymized under the install salt the way the
// authority block's identifiers are. The session-state KV holds this and not
// the id (spec-chatops-gateway.md, "Identifiers in `authority` are
// pseudonymous"), and the target allowlists are hashed through the same
// function, so the two sides cannot normalize differently. An id that is
// blank after trimming has no subject: "".
func requesterSubject(ps *Pseudonymizer, backend, authorID string) string {
	id := strings.TrimSpace(authorID)
	if backend == gchatBackend {
		id = strings.ToLower(id)
	}
	return ps.Hash(id)
}

func buildTargetAllowed(cfg *Config, ps *Pseudonymizer) targetAllowed {
	out := targetAllowed{}
	for target, byBackend := range cfg.TargetAllowedUsers {
		for backend, ids := range byBackend {
			set := map[string]bool{}
			for _, id := range ids {
				if subject := requesterSubject(ps, backend, id); subject != "" {
					set[subject] = true
				}
			}
			// An empty set is kept: a list that is present but blank
			// admits nobody (#2207's rule for the Chat ingress list).
			if out[target] == nil {
				out[target] = map[string]map[string]bool{}
			}
			out[target][backend] = set
		}
	}
	return out
}

// targetAllows answers whether the requester whose subject (requesterSubject,
// as a history entry's TaskRequester stores it) came in on backend may reach
// target. No list for the (target, backend) pair means the ingress allowlist
// is the only gate, which is today's bound, so the answer is true. Under a
// list, a blank subject is never a member, and an empty list has none. The
// A2A door's backend is the one exception to the absent-list rule, and the
// delegation checks it before asking here (doorUnlisted).
func (g *Gateway) targetAllows(target, backend, subject string) bool {
	set := g.targetAllowed[target][backend]
	if set == nil {
		return true
	}
	return subject != "" && set[subject]
}

// splitList parses a comma-separated env value, dropping blanks.
func splitList(raw string) []string {
	var out []string
	for _, s := range strings.Split(raw, ",") {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}
