package gateway

import (
	"fmt"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// EnvTierMembership is the operator-rendered membership list: comma-separated
// backend:user=tier entries (#3659, Tier 2c).
const EnvTierMembership = "A2A_TIER_MEMBERSHIP"

// TierMember is one person's tier: a backend, the plaintext user id on it as
// the allowlists name users, and the tier name.
type TierMember struct {
	Backend, User string
	Tier          capability.Tier
}

// ruleSteerTier names the refusal of a steer whose author is at another tier
// than the profile task it would reach, in the audit line.
const ruleSteerTier = "steer-tier"

// Refusals a tier decision posts in the room.
const (
	noticeProfileTierUndefined  = "your tier %s isn't defined on this install, so a profile task can't run for you; ask an administrator to define it or change your tier"
	noticeProfileTiersDiffer    = "people in this conversation are at different tiers (%s), and a task runs at one tier; start a new thread, or ask an administrator to put everyone at one tier"
	noticeProfileTierCeiling    = "tier %s allows more than this install's own access, so no profile task can run here; ask an administrator to check the install's settings"
	noticeProfileAuthorsUnknown = "this conversation has too many people in it to check their tiers; start a new thread to ask"
	noticeSteerTierDiffers      = "⚠️ not sent: the running task is at tier %s and you're at tier %s; ask someone at that tier to pass it on, or ask in a new thread"
)

// tierMemberBackends are the backends membership can name: the ones whose
// requester is one verified person. Not the console, whose principal
// everyone on it shares (#5557); not inject, the eval transport; and not the
// a2a door, whose caller names itself under a shared token.
var tierMemberBackends = []string{gchatBackend, slackBackend, discordBackend, a2aGoogleBackend}

// parseTierMembership reads A2A_TIER_MEMBERSHIP. An entry that isn't
// backend:user=tier, names an unknown backend or a tier that isn't a subject
// token, or repeats a person, is an error at boot: the operator renders the
// list from a validated CR, so a bad one is a bug, not a typo to skip.
func parseTierMembership(raw string) ([]TierMember, error) {
	var out []TierMember
	seen := map[string]bool{}
	for _, entry := range strings.Split(raw, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		who, tier, ok := strings.Cut(entry, "=")
		backend, user, ok2 := strings.Cut(who, ":")
		if !ok || !ok2 || strings.TrimSpace(user) == "" {
			return nil, fmt.Errorf("%s: entry %q isn't backend:user=tier", EnvTierMembership, entry)
		}
		if !slices.Contains(tierMemberBackends, backend) {
			return nil, fmt.Errorf("%s: entry %q names backend %q; want one of %s", EnvTierMembership, entry, backend, strings.Join(tierMemberBackends, ", "))
		}
		if !lib.ValidSubjectToken(tier) {
			return nil, fmt.Errorf("%s: entry %q names tier %q, which isn't a subject token", EnvTierMembership, entry, tier)
		}
		// The key a turn is matched on (requesterSubject): a Google Chat id
		// is case-blind, so two spellings of one person are a repeat.
		key := backend + ":" + strings.TrimSpace(user)
		if backend == gchatBackend {
			key = strings.ToLower(key)
		}
		if seen[key] {
			return nil, fmt.Errorf("%s: %s is listed twice", EnvTierMembership, key)
		}
		seen[key] = true
		out = append(out, TierMember{Backend: backend, User: user, Tier: capability.Tier(tier)})
	}
	return out, nil
}

// buildTierMembers keys the membership by requester: the backend and the
// HMAC'd subject a turn carries (requesterSubject).
func buildTierMembers(members []TierMember, ps *Pseudonymizer) map[TaskRequester]capability.Tier {
	out := make(map[TaskRequester]capability.Tier, len(members))
	for _, m := range members {
		id := m.User
		if m.Backend == a2aGoogleBackend {
			// The door's caller for a verified email, as its turns carry it.
			id = a2aGoogleCaller(strings.TrimSpace(id))
		}
		if subject := requesterSubject(ps, m.Backend, id); subject != "" {
			out[TaskRequester{Backend: m.Backend, Subject: subject}] = m.Tier
		}
	}
	return out
}

// tierOf is one requester's tier: the membership's, else read. The console's
// shared principal is read whatever the membership says: it can't tell its
// users apart (#5557).
func (g *Gateway) tierOf(r TaskRequester) capability.Tier {
	if r.Backend == consoleBackend {
		return capability.TierRead
	}
	if t, ok := g.tierMembers[r]; ok {
		return t
	}
	return capability.TierRead
}

// tierFor is the tier an AgentProfile task runs at for authors, the people
// whose text reaches it (delegateAuthors), replacing the install-wide read
// every requester used to get (#3659, Tier 2c). Each author needs an
// attested identity; each author's tier must be an interactive profile tier
// the tiers file defines; all of them must be at the same tier, or the turn
// is refused naming the tiers (Brian's decision: a thread runs at its
// authors' common tier, and never at a wider or narrower one picked for
// them); and that tier must fit under the install's AuthorityTier, leaving
// profile.run aside (OutsideCeiling). unknown says the author list overflowed:
// with any membership configured, an author left off it could be at another
// tier, so the turn is refused rather than guessed at.
func (g *Gateway) tierFor(authors []TaskRequester, unknown bool) (capability.Tier, error) {
	if unknown && len(g.tierMembers) > 0 {
		return "", refused(noticeProfileAuthorsUnknown)
	}
	var tier capability.Tier
	var tiers []string
	for _, a := range authors {
		if strings.TrimSpace(a.Subject) == "" {
			return "", refused(noticeProfileNoIdentity)
		}
		t := g.tierOf(a)
		if !slices.Contains(tiers, string(t)) {
			tiers = append(tiers, string(t))
		}
		tier = t
	}
	if len(tiers) == 0 {
		return "", refused(noticeProfileNoIdentity)
	}
	if len(tiers) > 1 {
		slices.Sort(tiers)
		return "", refused(noticeProfileTiersDiffer, strings.Join(tiers, ", "))
	}
	if !capability.IsInteractiveProfileTier(tier) {
		return "", refused(noticeProfileTierUndefined, tier)
	}
	if out := capability.OutsideCeiling(tier, g.cfg.AuthorityTier); len(out) > 0 {
		names := make([]string, len(out))
		for i, v := range out {
			names[i] = string(v)
		}
		// The verbs are plumbing: the log names them, the room doesn't.
		g.log.Warn("profile tier refused: it permits verbs the install's authority tier doesn't",
			"tier", tier, "authorityTier", g.cfg.AuthorityTier, "verbs", strings.Join(names, ","))
		return "", refused(noticeProfileTierCeiling, tier)
	}
	return tier, nil
}
