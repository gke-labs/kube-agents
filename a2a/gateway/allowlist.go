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
// backend -> set, with Google Chat emails lowercased and Slack ids kept exact.
type targetAllowed map[string]map[string]map[string]bool

func buildTargetAllowed(cfg *Config) targetAllowed {
	out := targetAllowed{}
	for target, byBackend := range cfg.TargetAllowedUsers {
		for backend, ids := range byBackend {
			set := map[string]bool{}
			for _, id := range ids {
				if id = strings.TrimSpace(id); id == "" {
					continue
				}
				if backend == gchatBackend {
					id = strings.ToLower(id)
				}
				set[id] = true
			}
			if len(set) == 0 {
				continue // an empty list is no list: all authenticated users
			}
			if out[target] == nil {
				out[target] = map[string]map[string]bool{}
			}
			out[target][backend] = set
		}
	}
	return out
}

// targetAllows answers whether authorID, in backend's vocabulary, may reach
// target. No list for the (target, backend) pair means the ingress allowlist
// is the only gate, which is today's bound, so the answer is true. Google
// Chat ids are emails and compare case-insensitively; Slack member ids
// compare exactly.
func (g *Gateway) targetAllows(target, backend, authorID string) bool {
	set := g.targetAllowed[target][backend]
	if set == nil {
		return true
	}
	if backend == gchatBackend {
		authorID = strings.ToLower(authorID)
	}
	return set[authorID]
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
