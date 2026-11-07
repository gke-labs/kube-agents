package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The task parameters that name a cluster target. A profile that declares all
// three takes one (spec-profiles-dispatch.md, "Addressing"); the dispatcher
// joins them into the verifier's resource in this order.
const (
	paramProject  = "project"
	paramLocation = "location"
	paramCluster  = "cluster"
)

// The kinds a target's verifier resource names, in its order:
// project/P/location/L/cluster/C (capability.Scope's profile form).
const (
	scopeKindProject  = "project"
	scopeKindLocation = "location"
	scopeKindCluster  = "cluster"
)

// The refusals resolveProfile and checkTargetScope give, as reasons a caller
// prefixes for the room (refusalNotice). Each names what was asked, since the
// asker may not know the install's profiles or scope.
const (
	noticeProfileUnknown     = "there is no agent profile named %q"
	noticeProfilePinnedOther = "%s works only on cluster %s, not %s"
	noticeProfileNoTarget    = "%s needs a target cluster (project, location and cluster)"
	noticeProfileTakesNone   = "%s doesn't take a target cluster"
	noticeProfileTier        = "%s doesn't run at tier %s"
	noticeProfileAmbiguous   = "more than one agent profile is pinned to cluster %s"
	noticeProfileNoGeneric   = "no agent profile serves cluster %s"
	noticeTargetExcluded     = "cluster %s is excluded from this install's scope"
	noticeTargetOutOfScope   = "cluster %s is outside this install's scope"
	noticeTargetUnconfirmed  = "can't confirm %s is in this install's scope yet"
	// noticeTargetStale refuses a target the resolved scope would decide,
	// when the resolved scope is missing, stale or from another declaration.
	noticeTargetStale = "can't confirm %s is in this install's scope yet (the resolved scope is stale)"
	// noticeTargetUnpublished refuses a target the resolved scope would decide,
	// when the document reads too-large or error; the second verb is that state.
	noticeTargetUnpublished = "can't confirm %s is in this install's scope (the resolved scope reads %s)"
	// noticeTargetUnreadable refuses a member project the publisher's probe
	// could not read; the second verb is the probe's outcome.
	noticeTargetUnreadable    = "can't confirm %s is in this install's scope (its project can't be read: %s)"
	noticeTargetParamsInvalid = "the task's parameters aren't valid: %v"
	noticeProfileNoIdentity   = "an agent profile runs a task only for an identified requester"
	noticeProfileRouteFailed  = "the task couldn't be routed to its agent profile"
	// noticeProfileParentToPlatform refuses a profile task's delegation to
	// platform, whose child would be minted wider than its parent.
	noticeProfileParentToPlatform = "an agent profile's task delegates only to another agent profile"
	// noticeProfileWhileRunning refuses a turn naming a profile while the
	// conversation has a task running, which would otherwise be steered
	// into that task with the profile dropped.
	noticeProfileWhileRunning = "a task is still running here; stop it or wait for it before asking an agent profile"
	// noticeProfileNoCapability refuses a profile task whose grant couldn't
	// be minted: its grant carries the tier and target the dispatcher checks.
	noticeProfileNoCapability = "⚠️ not started: could not mint this task's capability"
)

// The prefixes a profile refusal is posted under.
const (
	noticeTurnNotStarted       = "⚠️ not started: "
	noticeDelegationRefusedFor = "⚠️ delegation refused: "
)

// childNeverStarted is a child's end when it never reached an executor.
const childNeverStarted = "the delegated task never started"

// childResultCutNote ends a child result cut to lib.ChildResultTextCap.
const childResultCutNote = " … (cut; the child's full result is on its own task)"

// profileGrantDelegate is the principal a profile task's grant names as its
// delegate: the dispatcher, which resolves the grant when it asks the
// verifier whether the task may run (the contract on #3655, section 6). The
// verifier resolves an entry only for the caller it names.
const profileGrantDelegate = "dispatcher"

// mintTaskCapability mints a task's root grant: against the resolved profile
// for a profile route, and as mintCapability does for everything else.
func (g *Gateway) mintTaskCapability(ctx context.Context, taskID, addressee string, route *profileRoute) (*capability.Ref, error) {
	if route == nil {
		return g.mintCapability(ctx, taskID, addressee)
	}
	ref, err := capability.NewMinter(g.client.JetStream()).Mint(ctx, taskID, capability.Entry{
		Tier:     route.Tier,
		Scope:    route.Scope,
		Profile:  route.Profile,
		Delegate: profileGrantDelegate,
	})
	if err != nil {
		return nil, err
	}
	return &ref, nil
}

// submissionPayload is the submission's message: the text, and for a profile
// route with parameters, the taskParameters part beside it.
func submissionPayload(text, taskID, contextID string, route *profileRoute) ([]byte, error) {
	if route == nil || len(route.Params) == 0 {
		return messagePayload(text, taskID, contextID)
	}
	params, err := lib.TaskParametersPart(lib.TaskParameters{Parameters: route.Params})
	if err != nil {
		return nil, err
	}
	return messagePayloadParts([]lib.Part{{Kind: "text", Text: text}, params}, taskID, contextID)
}

// submissionSubject is where a new task is published: a profile's submission
// subject, carrying the tier and kind its durables route on, or the
// addressee's .in subject for platform and sessions.
func submissionSubject(addressee, taskID string, route *profileRoute) string {
	if route == nil {
		return lib.TaskInSubject(addressee, taskID)
	}
	return lib.TaskSubmitSubject(route.Profile, string(route.Tier), route.Kind, taskID)
}

// takesTarget reports whether a profile's card declares the three
// parameters that name a cluster (the contract on #3655, addendum 1).
func takesTarget(c lib.AgentCard) bool {
	have := map[string]bool{}
	for _, p := range c.Parameters {
		have[p.Name] = true
	}
	return have[paramProject] && have[paramLocation] && have[paramCluster]
}

// clusterTarget is a cluster a task is addressed to.
type clusterTarget struct {
	Project, Location, Cluster string
}

func (t clusterTarget) String() string {
	return strings.Join([]string{t.Project, t.Location, t.Cluster}, "/")
}

func (t clusterTarget) matches(r *lib.ClusterRef) bool {
	return r != nil && r.ProjectID == t.Project && r.Location == t.Location && r.Cluster == t.Cluster
}

// scope is the target as the verifier's resource: project/P/location/L/cluster/C.
func (t clusterTarget) scope() capability.Scope {
	return capability.Scope(strings.Join([]string{scopeKindProject, t.Project, scopeKindLocation, t.Location, scopeKindCluster, t.Cluster}, "/"))
}

// targetOf reads a cluster target out of task parameters. ok is false when
// none of the three is set; partial is true when some but not all are.
func targetOf(params map[string]string) (t clusterTarget, ok, partial bool) {
	t = clusterTarget{Project: params[paramProject], Location: params[paramLocation], Cluster: params[paramCluster]}
	set := 0
	for _, v := range []string{t.Project, t.Location, t.Cluster} {
		if v != "" {
			set++
		}
	}
	return t, set == 3, set > 0 && set < 3
}

// profileDirectory is the gateway's view of the profiles it can address, kept
// from their cards on DIRECTORY (a2a.agents.<profile>). Safe for concurrent
// use; a nil directory reads as empty.
type profileDirectory struct {
	mu    sync.RWMutex
	cards map[string]lib.AgentCard
}

func newProfileDirectory() *profileDirectory {
	return &profileDirectory{cards: map[string]lib.AgentCard{}}
}

// put records a card, or forgets the profile when card is nil (a deleted
// profile's card).
func (d *profileDirectory) put(name string, card *lib.AgentCard) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if card == nil {
		delete(d.cards, name)
		return
	}
	d.cards[name] = *card
}

func (d *profileDirectory) lookup(name string) (lib.AgentCard, bool) {
	if d == nil {
		return lib.AgentCard{}, false
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	c, ok := d.cards[name]
	return c, ok
}

// names is every profile the directory holds.
func (d *profileDirectory) names() []string {
	d.mu.RLock()
	defer d.mu.RUnlock()
	out := make([]string, 0, len(d.cards))
	for name := range d.cards {
		out = append(out, name)
	}
	return out
}

// pinnedTo is every profile pinned to t, in name order.
func (d *profileDirectory) pinnedTo(t clusterTarget) []string {
	if d == nil {
		return nil
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	var out []string
	for name, c := range d.cards {
		if t.matches(c.ClusterRef) {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// profileRoute is a task addressed to an AgentProfile: what the gateway mints
// the grant against and the subject it submits on.
type profileRoute struct {
	Profile string
	Tier    capability.Tier
	Kind    string
	Params  map[string]string
	Scope   capability.Scope
}

// errRefused carries a notice for the room.
type errRefused struct{ notice string }

func (e errRefused) Error() string { return e.notice }

// refusalNotice is err's notice for the room after prefix (the turn's own
// "not started" or a delegation's "delegation refused"): a refusal's reason,
// or a generic line for anything else, whose detail stays in the log.
func refusalNotice(prefix string, err error) string {
	var r errRefused
	if errors.As(err, &r) {
		return prefix + r.notice
	}
	return prefix + noticeProfileRouteFailed
}

func refused(format string, args ...any) error {
	return errRefused{notice: fmt.Sprintf(format, args...)}
}

// resolveProfile picks the profile a task addressed to `addressee` with
// `params` goes to (spec-profiles-dispatch.md, "Addressing", step 1):
//   - addressed to a pinned profile, the target must be its cluster;
//   - addressed to a profile that takes a target, a profile pinned to that
//     target gets it instead, and otherwise the addressed profile does;
//   - addressed to a profile that takes none, the task carries no target.
//
// Two profiles pinned to one cluster is refused rather than guessed between.
func resolveProfile(dir *profileDirectory, addressee string, params map[string]string) (string, *clusterTarget, error) {
	if err := lib.ValidateTaskParameters(params); err != nil {
		return "", nil, refused(noticeTargetParamsInvalid, err)
	}
	card, ok := dir.lookup(addressee)
	if !ok {
		// The addressee is the asker's to write, so the room gets it capped.
		return "", nil, refused(noticeProfileUnknown, truncateRunes(addressee, delegateAddresseeLogCap))
	}
	target, hasTarget, partial := targetOf(params)
	switch {
	case card.ClusterRef != nil:
		if partial {
			// Half a target names no cluster, and filling the rest from
			// the pin would run the task somewhere the asker didn't say.
			return "", nil, refused(noticeProfileNoTarget, addressee)
		}
		if !hasTarget {
			// A pinned profile's target is its own cluster.
			t := clusterTarget{Project: card.ClusterRef.ProjectID, Location: card.ClusterRef.Location, Cluster: card.ClusterRef.Cluster}
			return addressee, &t, nil
		}
		if !target.matches(card.ClusterRef) {
			ref := clusterTarget{Project: card.ClusterRef.ProjectID, Location: card.ClusterRef.Location, Cluster: card.ClusterRef.Cluster}
			return "", nil, refused(noticeProfilePinnedOther, addressee, ref, target)
		}
		return addressee, &target, nil
	case takesTarget(card):
		if !hasTarget {
			return "", nil, refused(noticeProfileNoTarget, addressee)
		}
		switch pinned := dir.pinnedTo(target); len(pinned) {
		case 0:
			return addressee, &target, nil
		case 1:
			return pinned[0], &target, nil
		default:
			return "", nil, refused(noticeProfileAmbiguous, target)
		}
	default:
		if hasTarget || partial {
			return "", nil, refused(noticeProfileTakesNone, addressee)
		}
		return addressee, nil, nil
	}
}

// scopeDeclaration is the operator's rendered spec.scope (scope.json, the
// file the platform agent also reads; renderScopeJSON in the operator).
type scopeDeclaration struct {
	Present       bool     `json:"present"`
	Projects      []string `json:"projects"`
	Folders       []string `json:"folders"`
	Organizations []string `json:"organizations"`
	// SharedVpcHosts and MetricsScopes are selectors whose member projects
	// the reconcile resolves, like folders and organizations.
	SharedVpcHosts []string `json:"sharedVpcHosts"`
	MetricsScopes  []string `json:"metricsScopes"`
	MaxProjects    *int     `json:"maxProjects"`
	Exclude        struct {
		Projects []string `json:"projects"`
		Clusters []struct {
			ProjectID   string `json:"projectId"`
			Location    string `json:"location"`
			ClusterName string `json:"clusterName"`
		} `json:"clusters"`
	} `json:"exclude"`
}

// errNoScopeFile is loadScope's answer when the file isn't there: an operator
// that renders none into the gateway pod.
var errNoScopeFile = errors.New("no scope file")

func loadScope(path string) (*scopeDeclaration, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, errNoScopeFile
	}
	if err != nil {
		return nil, err
	}
	var d scopeDeclaration
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, fmt.Errorf("scope file %s: %w", path, err)
	}
	return &d, nil
}

// checkTargetScope refuses a target outside the install's scope
// (spec-profiles-dispatch.md, "Addressing", step 2). An exclusion always
// refuses. A declared project list admits only its projects. Past that, a
// project a folder, organization, Shared VPC host or Metrics Scope may reach
// can't be confirmed from the declaration, so it refuses with errUnresolved,
// which Gateway.checkScope settles against the resolved scope: "not
// excluded" isn't "in scope". An install that declares no scope draws no
// boundary, as the platform agent reads it, so only exclusions apply. A nil
// declaration (no file rendered, or one that didn't parse) refuses every
// target.
func checkTargetScope(decl *scopeDeclaration, management string, t clusterTarget) error {
	if decl == nil {
		return refused(noticeTargetUnconfirmed, t)
	}
	// The management project is always in scope, and an exclude.projects
	// entry matching it is ignored, as the reconcile does. Its own
	// clusters can still be excluded by name below.
	isManagement := management != "" && t.Project == management
	if !isManagement {
		excluded, err := projectExcluded(t.Project, decl.Exclude.Projects)
		if err != nil {
			return refused(noticeTargetUnconfirmed, t)
		}
		if excluded {
			return refused(noticeTargetExcluded, t)
		}
		// A number entry also drops the project the number names, which the
		// reconcile ties to an ID through Metrics Scope naming. The gateway
		// has no number for an ID, so it can't confirm the target isn't the
		// project excluded.
		if !isProjectNumber(t.Project) && slices.ContainsFunc(decl.Exclude.Projects, isProjectNumber) {
			return refused(noticeTargetUnconfirmed, t)
		}
	}
	for _, c := range decl.Exclude.Clusters {
		if c.ProjectID == t.Project && c.Location == t.Location && c.ClusterName == t.Cluster {
			return refused(noticeTargetExcluded, t)
		}
	}
	if !decl.Present || isManagement {
		return nil
	}
	if slices.Contains(decl.Projects, t.Project) {
		return nil
	}
	if declaresResolution(decl) {
		// Only the resolved scope can confirm a project a folder,
		// organization or selector reaches (Gateway.checkScope).
		return errUnresolved{errRefused{notice: fmt.Sprintf(noticeTargetUnconfirmed, t)}}
	}
	return refused(noticeTargetOutOfScope, t)
}

// projectExcluded matches a project against exclude.projects as the
// reconcile's _excluded_by does: an entry is a project ID or a shell-style
// glob over IDs, and an all-digit project (a number) matches only an equal
// entry. A number entry against an ID target is checkTargetScope's to refuse. An entry that can't be compiled is an error, so the caller refuses
// rather than admitting a project the entry may have meant to drop.
func projectExcluded(project string, patterns []string) (bool, error) {
	if isProjectNumber(project) {
		return slices.Contains(patterns, project), nil
	}
	for _, pattern := range patterns {
		re, err := regexp.Compile(fnmatchRegexp(pattern))
		if err != nil {
			return false, fmt.Errorf("exclude.projects entry %q: %w", pattern, err)
		}
		if re.MatchString(project) {
			return true, nil
		}
	}
	return false, nil
}

func isProjectNumber(project string) bool {
	if project == "" {
		return false
	}
	for _, r := range project {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// fnmatchRegexp translates a shell-style glob to an anchored regexp the way
// Python's fnmatch.translate does: * and ? match any run and any one
// character, [seq] and [!seq] are sets, a ] first in a set is literal, and a
// [ with no closing ] is a literal [.
func fnmatchRegexp(pattern string) string {
	var b strings.Builder
	b.WriteString("^(?s:")
	runes := []rune(pattern)
	for i := 0; i < len(runes); i++ {
		switch c := runes[i]; c {
		case '*':
			b.WriteString(".*")
			for i+1 < len(runes) && runes[i+1] == '*' {
				i++
			}
		case '?':
			b.WriteString(".")
		case '[':
			j := i + 1
			if j < len(runes) && runes[j] == '!' {
				j++
			}
			if j < len(runes) && runes[j] == ']' {
				j++
			}
			for j < len(runes) && runes[j] != ']' {
				j++
			}
			if j >= len(runes) {
				b.WriteString(`\[`)
				continue
			}
			set := runes[i+1 : j]
			i = j
			switch {
			case len(set) == 0:
				b.WriteString(`[^\x00-\x{10FFFF}]`)
			case len(set) == 1 && set[0] == '!':
				b.WriteString(".")
			default:
				b.WriteString("[")
				k := 0
				if set[0] == '!' {
					b.WriteString("^")
					k = 1
				}
				for ; k < len(set); k++ {
					switch set[k] {
					case '\\', '[', '^':
						b.WriteString(`\`)
					}
					b.WriteRune(set[k])
				}
				b.WriteString("]")
			}
		default:
			b.WriteString(regexp.QuoteMeta(string(c)))
		}
	}
	b.WriteString(")$")
	return b.String()
}

// requesterTier is the tier a profile task runs at for this requester, until
// the PlatformAgent's membership mapping exists (the contract on #3655,
// addendum 1): read for a person at any door, and a refusal for a requester
// with no attested identity. Unattended is a named trigger's tier, and no
// trigger submits through the gateway yet.
func requesterTier(r TaskRequester) (capability.Tier, error) {
	if strings.TrimSpace(r.Subject) == "" {
		return "", refused(noticeProfileNoIdentity)
	}
	return capability.TierRead, nil
}

// profileRouteFor resolves addressee and params to the route a task takes:
// the resolved profile, the target checked against the install's scope, and
// the grant's scope (spec-profiles-dispatch.md, "Addressing", steps 1 to 3).
// A pinned profile addressed with no target gets its own cluster's three
// parameters, so the submission names its target either way. A task with no
// target is scoped to the install's namespace.
func (g *Gateway) profileRouteFor(addressee string, params map[string]string, tier capability.Tier, kind string) (*profileRoute, error) {
	profile, target, err := resolveProfile(g.profiles, addressee, params)
	if err != nil {
		return nil, err
	}
	// The dispatcher has a durable only for each tier the profile allows
	// (its card's tiers), so a submission at another tier would sit with
	// nothing to consume it until the never-started heal. A card with no
	// tiers is from an operator that predates them, and isn't checked.
	if card, ok := g.profiles.lookup(profile); ok && len(card.Tiers) > 0 && !slices.Contains(card.Tiers, string(tier)) {
		return nil, refused(noticeProfileTier, profile, tier)
	}
	route := &profileRoute{Profile: profile, Tier: tier, Kind: kind, Params: maps.Clone(params),
		Scope: capability.NamespaceScope(g.cfg.Namespace)}
	if target == nil {
		return route, nil
	}
	decl, err := loadScope(g.cfg.ScopeFile)
	if err != nil {
		g.log.Warn("scope file unreadable; refusing the target", "path", g.cfg.ScopeFile, "err", err)
		decl = nil
	}
	if err := g.checkScope(decl, *target); err != nil {
		return nil, err
	}
	if route.Params == nil {
		route.Params = map[string]string{}
	}
	route.Params[paramProject], route.Params[paramLocation], route.Params[paramCluster] = target.Project, target.Location, target.Cluster
	// The merge can make a valid set invalid: a pin's cluster added to a set
	// at the cap, or a clusterRef value the CRD admits and the parameter
	// grammar doesn't. Refused here, before the mint, with the notice.
	if err := lib.ValidateTaskParameters(route.Params); err != nil {
		return nil, refused(noticeTargetParamsInvalid, err)
	}
	route.Scope = target.scope()
	return route, nil
}

// deliverChildResult hands a child's end to a parent addressed to an
// AgentProfile: a childResult message on the parent's .in, which the parent
// reads while it waits open. It reports false only for a parent that isn't a
// profile task, which is woken instead. A delivery that fails is logged and
// the parent still holds the conversation again, so a stop reaches it.
func (g *Gateway) deliverChildResult(ctx context.Context, rec *SessionRecord, child TaskRef, state lib.TaskState, result, reason string) bool {
	parent, ok := rec.TaskRefFor(child.ParentTaskID)
	if !ok || parent.Tier == "" {
		return false
	}
	if !parent.Canceled && !parent.TerminalObserved && g.parentEnded(ctx, parent.ID) {
		// The relay retires a task's index entry on its terminal and sets
		// no flag on the history entry.
		parent.TerminalObserved = true
	}
	if parent.Canceled || parent.TerminalObserved {
		// A stop cancelled the parent with its child, or the parent has
		// ended: nothing reads its .in, and restoring it would hand the
		// conversation a task the user already saw end, or steer the next
		// message into one they stopped. Handled, so no session is woken.
		g.log.Info("child result not delivered: the parent is cancelled or has ended",
			"child", child.ID, "parent", parent.ID, "canceled", parent.Canceled, "ended", parent.TerminalObserved)
		return true
	}
	if err := g.publishChildResult(ctx, rec, parent, child, state, result, reason); err != nil {
		g.log.Error("child result not delivered", "child", child.ID, "parent", parent.ID, "err", err)
	} else {
		g.log.Info("child result delivered", "child", child.ID, "parent", parent.ID, "state", state)
	}
	// The parent is running again: it holds the conversation, so a stop
	// reaches it, a steer goes to it, and a new ask waits behind it. Its
	// rolling line comes back from the entry that kept it while the child ran.
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID == child.ID {
		rec.ActiveTask = &ActiveTask{TaskID: parent.ID, CorrelationID: parent.CorrelationID,
			Capability: parent.Capability, SubmittedAt: parent.StartedAt, Ask: truncateRunes(parent.Request, askCap),
			StatusMsgID: parent.StatusMsgID}
		setParentLine(rec, parent.ID, "")
		rec.Addressee = parent.Addressee
	}
	return true
}

// parentEnded reports whether a task's index entry is gone, which the relay
// does on the task's terminal. A failed read reports false: the delivery
// goes ahead, as it did before the check.
func (g *Gateway) parentEnded(ctx context.Context, taskID string) bool {
	key, err := g.reg.SessionForTask(ctx, taskID)
	if err != nil {
		g.log.Warn("parent task index unreadable", "parent", taskID, "err", err)
		return false
	}
	return key == ""
}

// publishChildResult writes the childResult message on the parent's .in.
func (g *Gateway) publishChildResult(ctx context.Context, rec *SessionRecord, parent, child TaskRef, state lib.TaskState, result, reason string) error {
	text := reason
	if state == lib.StateCompleted {
		text = result
	}
	if len(text) > lib.ChildResultTextCap {
		text = truncateBytes(text, lib.ChildResultTextCap-len(childResultCutNote)) + childResultCutNote
	}
	part, err := lib.ChildResultPart(lib.ChildResult{ChildTaskID: child.ID, Addressee: child.Addressee, State: state, Text: text})
	if err != nil {
		return fmt.Errorf("build: %w", err)
	}
	payload, err := messagePayloadParts([]lib.Part{part}, parent.ID, rec.ContextID)
	if err != nil {
		return fmt.Errorf("payload: %w", err)
	}
	authority, err := AuthorityFromAttribution(parent.Attribution)
	if err != nil {
		return fmt.Errorf("authority: %w", err)
	}
	// The parent's own capability: the message continues the parent's task,
	// as a steer does, and is no new request.
	env, err := lib.NewMessageEnvelope(gatewayParty, parent.ID, rec.ContextID, parent.CorrelationID, payload,
		lib.WithTo(lib.Party{Session: parent.Addressee}),
		lib.WithAuthority(authority.Render(parent.Capability)))
	if err != nil {
		return fmt.Errorf("envelope: %w", err)
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(parent.Addressee, parent.ID), env); err != nil {
		return fmt.Errorf("publish: %w", err)
	}
	return nil
}

// truncateBytes cuts s to at most n bytes without splitting a UTF-8 rune.
func truncateBytes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}

// publishDispatchStop writes the stop record for a profile task, beside the
// cancel already sent on its `…in`, for the dispatcher, which has no read on
// `…in` (addendum 9 on #3655). The stream and the dispatcher's reader aren't
// in this tree yet: until they are, the publish gets no ack and is logged,
// and a task still queued runs when a slot frees. A task addressed to platform or a session has no record. A
// failed publish is logged and leaves the cancel standing: a held task still
// gets the cancel, and only one still queued would run.
func (g *Gateway) publishDispatchStop(ctx context.Context, ref TaskRef) {
	if ref.Tier == "" {
		return
	}
	if err := g.client.PublishDispatchStop(ctx, ref.Addressee, ref.ID); err != nil {
		g.log.Error("dispatch stop record failed", "taskId", ref.ID, "profile", ref.Addressee, "err", err)
	}
}

// cancelWaitingProfileParents cancels the AgentProfile tasks waiting open on
// taskID, parent by parent up the chain, so a stop cancels the task tree
// (spec-profiles-dispatch.md, "Results and stop") and not only the child that
// held the conversation. A session parent's turn has already ended, so the
// walk stops at the first parent that isn't a profile task. Each cancel rides
// the parent's own chain and grant, and is recorded on its history entry.
func (g *Gateway) cancelWaitingProfileParents(ctx context.Context, rec *SessionRecord, taskID string, authority Authority) {
	ref, ok := rec.TaskRefFor(taskID)
	// Only a child's parent waits on it. A wake's parent link names the
	// child it continues, which has ended.
	if !ok || ref.Role != taskRoleChild {
		return
	}
	for steps := 0; ok && ref.ParentTaskID != "" && steps <= taskHistoryCap; steps++ {
		parent, held := rec.TaskRefFor(ref.ParentTaskID)
		if !held || parent.Tier == "" || parent.TerminalObserved || g.parentEnded(ctx, parent.ID) {
			return
		}
		if !parent.Canceled && parent.CorrelationID != "" {
			env, err := lib.NewCancelEnvelope(gatewayParty, parent.ID, rec.ContextID, parent.CorrelationID,
				lib.WithTo(lib.Party{Session: parent.Addressee}),
				lib.WithAuthority(authority.Render(parent.Capability)))
			if err == nil {
				err = g.client.Publish(ctx, lib.TaskInSubject(parent.Addressee, parent.ID), env)
			}
			if err != nil {
				g.log.Error("cancel of a waiting profile parent failed", "taskId", parent.ID, "child", ref.ID, "err", err)
				return
			}
			rec.MarkCanceled(parent.ID)
			g.publishDispatchStop(ctx, parent)
		}
		ref, ok = parent, true
	}
}
