package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The rules a delegation is refused or ignored under, as the audit line
// names them (spec-chatops-gateway.md, "Sessions by default").
const (
	ruleDelegationAllowedUsers  = "delegation.allowed-users"
	ruleDelegationTarget        = "delegation.target"
	ruleDelegationSteerAuthor   = "delegation.steer-author"
	ruleDelegationSessionAuthor = "delegation.session-author"
	ruleDelegationBusy          = "delegation.busy"
	ruleDelegationDepth         = "delegation.depth"
	ruleDelegationWidth         = "delegation.width"
	// ruleDelegationProfileBatch ignores a batch from a profile task, which
	// waits open for one child result and has no way yet to take several.
	ruleDelegationProfileBatch = "delegation.profile-batch"
	ruleDelegationNoRequester  = "delegation.no-requester"
	ruleDelegationStale        = "delegation.stale"
	ruleDelegationMalformed    = "delegation.malformed"
	ruleDelegationDoorUnlisted = "delegation.door-unlisted"
	// ruleDelegationChildSteer refuses a steer into a delegated child from
	// an author the target's list refuses (gke-labs#2531 item 5).
	ruleDelegationChildSteer = "delegation.child-steer"
)

// reasonWakeNotStarted is the reason token on a delegation chain's root
// terminal when the child ended and no wake could run (observeChildEnd).
const reasonWakeNotStarted = "wake-not-started"

// reasonDelegationRefused and reasonDelegationNotStarted are the reason
// tokens on the root terminal an observer is told for a session turn that
// asked to delegate and minted no child (TaskRef.DelegationEnd,
// SessionRecord.handOffEnd): the gateway refused the request, or the request
// did not become a child for any other reason.
const (
	reasonDelegationRefused    = "delegation-refused"
	reasonDelegationNotStarted = "delegation-not-started"
)

// The why of a delegation-not-started end, one per path that leaves a
// session turn's request without a child.
const (
	whyChildOffBus      = "the delegated task could not reach the bus"
	whyTurnStopped      = "the turn was stopped"
	whyRequestMalformed = "the request was malformed"
	whyNoChild          = "the request minted no child"
	whyRequestUnread    = "the delegate request could not be read from the stream"
	whyProfileBatch     = "a profile task can't hand out a batch yet"
	// whyBatchPlanUnwritten: the batch's plan could not be written to the
	// record before its first child, so none was minted (mintBatch).
	whyBatchPlanUnwritten = "the batch could not be recorded before its tasks were started"
	// whyBatchCutShort: a gateway stopped partway through the batch, and
	// its request could not finish it when it came back.
	whyBatchCutShort = "the batch was cut short and could not be finished"
)

// notStartedEnd is the DelegationEnd for a request that minted no child for
// why.
func notStartedEnd(why string) string {
	return "reason: " + reasonDelegationNotStarted + " - " + why
}

// delegateAddresseeLogCap bounds a delegate request's addressee in the audit
// lines, in bytes. Addressees are agent names ("platform" today), so 64 keeps
// every real one whole and a pathological one readable.
const delegateAddresseeLogCap = 64

// delegatedLineNote suffixes a child's rolling line, so the room can tell the
// task the session handed on from one a human asked for.
const delegatedLineNote = "(delegated to platform)"

// delegatedProfileLineNote is delegatedLineNote for a child sent to an
// AgentProfile, naming the profile it resolved to.
const delegatedProfileLineNote = "(delegated to %s)"

// requesterGone is the one spelling of "the turn's requester has aged out of
// the record" (AskTTL clears it), for the delegation refusal and the wake
// that cannot run for the same reason.
const requesterGone = "requester is no longer on record"

const (
	noticeDelegationNoRequester       = "⚠️ delegation refused: this turn's " + requesterGone + "; ask again"
	noticeDelegationNotAllowed        = "🚫 not allowed to reach %s from here"
	noticeDelegationSessionIncomplete = "⚠️ delegation refused: this session's authors can no longer all be checked; ask again in a new turn"
	noticeDelegationSteerOverflow     = "⚠️ delegation refused: more people steered this turn than can be checked; ask again"
	noticeWakeNoRequester             = "ℹ️ the delegated task finished, but the delegating turn's " + requesterGone + "; the session was not woken"
	noticeDelegationTooWide           = "⚠️ delegation refused: %d tasks in one delegation, and this conversation may hand out at most %d; split them or pick the ones that matter"
)

// delegateAddresseeSep joins a batch's addressees in the audit lines.
const delegateAddresseeSep = ","

// handleDelegateRequest is the gateway's side of the delegation primitive: a
// session turn's lib.ArtifactDelegate, accepted, checked, and minted as a
// child task to the platform agent or an AgentProfile, or as a batch of them
// (mintBatch). Called from the relay under the session
// lock with the record the relay writes back.
//
// The cases that cannot be a live request from the conversation's own session
// (a straggler, a repeat, a malformed part) are ignored: logged at warning,
// nothing posted, nothing minted. The cases that are a real request this
// gateway will not serve are refused: logged likewise, and the conversation
// told, naming the target and never the requester.
func (g *Gateway) handleDelegateRequest(ctx context.Context, rec *SessionRecord, subject, taskID string, parts []lib.Part) {
	session, _, _, _ := lib.ParseTaskSubject(subject)
	parent, known := rec.TaskRefFor(taskID)
	var req lib.DelegateRequest
	parsed := len(parts) == 1 && parts[0].Kind == "data" && json.Unmarshal(parts[0].Data, &req) == nil

	log := g.log.With("task", taskID, "session", session, "conversation", rec.Key)
	// The addressee is the session's to write and nothing upstream bounds
	// its length (only the text has a cap), so the audit lines carry it cut
	// to delegateAddresseeLogCap: a valid one is a single short name. A
	// batch's are each cut, and joined, for at most lib.DelegateBatchMax
	// entries: a longer batch is refused or ignored below.
	var logNames []string
	for i, e := range req.Entries() {
		if i == lib.DelegateBatchMax {
			break
		}
		logNames = append(logNames, truncateRunes(strings.TrimSpace(e.Addressee), delegateAddresseeLogCap))
	}
	logAddressee := strings.Join(logNames, delegateAddresseeSep)
	log.Info("delegation requested", "addressee", logAddressee, "depth", parent.Depth)
	// Every refusal and ignore line carries the rule, the backend and the
	// requester as the record stores it: already hashed.
	audit := []any{"addressee", logAddressee, "depth", parent.Depth}
	if parent.Requester != nil {
		audit = append(audit, "backend", parent.Requester.Backend, "requester", parent.Requester.Subject)
	}
	ignore := func(rule string, extra ...any) {
		log.Warn("delegation ignored", append(append([]any{"rule", rule}, audit...), extra...)...)
	}
	// noChild records on the turn's entry why its request minted no child,
	// so the turn's own `completed` - only the hand-off line - is never
	// handed to an observer as the answer (handOffEnd). Written now, under
	// the session lock the caller holds, as startTaskWith writes a mint:
	// left to the relay's end-of-batch write, a failed write would lose it.
	noChild := func(reason string) {
		rec.markDelegationEnd(taskID, reason)
		if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
			g.log.Error("session record write failed after a delegation minted no child", "conversation", rec.Key, "err", err)
		}
	}
	// A refusal's notice waits for the delegating turn's terminal, so the
	// room reads the session's "delegated to platform" first and the
	// refusal after it.
	refuse := func(rule, notice string, extra ...any) {
		log.Warn("delegation refused", append(append([]any{"rule", rule}, audit...), extra...)...)
		g.deferNotice(taskID, notice)
		noChild("reason: " + reasonDelegationRefused + " - " + notice)
	}

	// Only the task the gateway started, from the incarnation that owns it
	// now: the subject's session is the record's bus session and the task
	// was addressed to it. That is a session-routed turn or a delegate:
	// incarnation; a platform task, or a pod a later turn retired, is not.
	// A task addressed to an AgentProfile delegates too: its subject's
	// addressee is the profile it was submitted to. It waits open for its
	// child, and its context is its own task alone (a warm worker is reset
	// between tasks), so the incarnation's author set below is a session's
	// check, not its.
	profileParent := known && parent.Tier != "" && session == parent.Addressee
	sessionParent := known && rec.BusSession != "" && session == rec.BusSession && parent.Addressee == rec.BusSession
	if !profileParent && !sessionParent {
		ignore(ruleDelegationStale, "busSession", rec.BusSession)
		return
	}
	// A request is accepted once per task, whether it asked for one child
	// or a batch: a parent with any child has had its request. Checked
	// before the active task, so a repeat from the parent logs as what it
	// is.
	//
	// The exception is a batch a gateway stopped partway through: its plan
	// (BatchSize) is on the record with fewer children than it names. Its
	// request, redelivered or read back from the stream, finishes it, every
	// check applying again; anything that stops it here withdraws the
	// children already on the bus, so a batch is whole or not run. minting
	// is set once the request reaches mintBatch, which then finishes the
	// batch or withdraws it itself.
	resuming, minting := false, false
	if len(parent.Children) > 0 {
		if parent.batchComplete() || parent.DelegationEnd != "" {
			ignore(ruleDelegationBusy, "child", parent.Children[len(parent.Children)-1])
			return
		}
		resuming = true
		defer func() {
			if !minting {
				g.withdrawCutShortBatch(ctx, rec, taskID, log)
			}
		}()
		log.Info("finishing a batch cut short", "minted", len(parent.Children), "tasks", parent.BatchSize)
	}
	if active := rec.ActiveTask; active == nil || active.TaskID != taskID {
		ignore(ruleDelegationStale)
		return
	}
	// A turn the human stopped is not a live request, whichever reached
	// the gateway first: the stop, or the artifact the adapter published
	// before its cancel arrived. The stop turn and this relay batch
	// serialize on the session lock, so this is the one place the check
	// holds without a race. Ignored rather than refused: the human said
	// stop, and a notice about a delegation they stopped says nothing new.
	if rec.ActiveTask.Detached || parent.Canceled {
		ignore(ruleDelegationStale, "stopped", true)
		noChild(notStartedEnd(whyTurnStopped))
		return
	}
	// A profile task waits open for its child's result, and nothing yet
	// lets it take several, so a batch from one is not served.
	if parsed && req.IsBatch() && profileParent {
		ignore(ruleDelegationProfileBatch, "tasks", len(req.Tasks))
		noChild(notStartedEnd(whyProfileBatch))
		return
	}
	// A batch wider than this gateway's setting is refused whole, naming
	// the cap, so the session can split it or choose. The adapter holds the
	// width it was told, so this is a setting changed under a running turn,
	// or a request from elsewhere.
	if parsed && req.IsBatch() && len(req.Tasks) > g.cfg.DelegationWidthMax {
		refuse(ruleDelegationWidth, fmt.Sprintf(noticeDelegationTooWide, len(req.Tasks), g.cfg.DelegationWidthMax), "tasks", len(req.Tasks))
		return
	}
	// The adapter holds the same shape and cap and refuses blank text; a
	// request that breaks any of them reached the bus some other way.
	if !parsed || lib.ValidateDelegateRequest(req) != nil || (resuming && len(req.Tasks) != parent.BatchSize) {
		ignore(ruleDelegationMalformed, "textBytes", len(req.Text), "tasks", len(req.Tasks))
		noChild(notStartedEnd(whyRequestMalformed))
		return
	}
	// A batch of one is the single shape: the child takes the active slot,
	// as it always has.
	if len(req.Tasks) == 1 {
		e := req.Tasks[0]
		req = lib.DelegateRequest{Addressee: e.Addressee, Text: e.Text, Parameters: e.Parameters}
	}

	// One live child per conversation, whichever turn asked
	// for it: a stop detaches a child without ending it.
	// A batch being finished has its own children live.
	if live := g.liveChild(ctx, rec); live != "" && !resuming {
		refuse(ruleDelegationBusy, fmt.Sprintf("⚠️ delegation refused: a delegated task is still running (task %s)", live), "child", live)
		return
	}
	if parent.Depth >= g.cfg.DelegationDepthMax {
		refuse(ruleDelegationDepth, fmt.Sprintf("⚠️ delegation refused: this conversation has delegated as deep as it may (%d)", g.cfg.DelegationDepthMax))
		return
	}
	authority, err := AuthorityFromAttribution(parent.Attribution)
	if parent.Requester == nil || len(parent.Attribution) == 0 || err != nil {
		refuse(ruleDelegationNoRequester, noticeDelegationNoRequester)
		return
	}
	// Each child the request asks for meets the same checks, all of them
	// before any is minted, so one entry that fails refuses the request with
	// nothing minted. check refuses (with the notice a single child would
	// get) and reports false, or reports the entry's route: nil for
	// platform.
	check := func(e lib.DelegateTask) (*profileRoute, bool) {
		// Anything but platform is an AgentProfile: resolved, its target checked
		// against the scope, and minted at the parent's tier, which for a
		// session parent is its requester's (profileRouteFor).
		var route *profileRoute
		var err error
		addressee := strings.TrimSpace(e.Addressee)
		// A profile task delegates only to another profile: a platform child is
		// minted at the install's tier and scope, wider than the parent's.
		if profileParent && addressee == targetPlatform {
			refuse(ruleDelegationTarget, noticeDelegationRefusedFor+noticeProfileParentToPlatform)
			return nil, false
		}
		if addressee == targetPlatform && len(e.Parameters) > 0 {
			// Only a profile route carries parameters; platform's would be dropped.
			refuse(ruleDelegationTarget, noticeDelegationRefusedFor+noticePlatformTakesNoParameters)
			return nil, false
		}
		if addressee != targetPlatform {
			tier := capability.Tier(parent.Tier)
			if !profileParent {
				if tier, err = requesterTier(*parent.Requester); err != nil {
					refuse(ruleDelegationTarget, refusalNotice(noticeDelegationRefusedFor, err))
					return nil, false
				}
			}
			if route, err = g.profileRouteFor(addressee, e.Parameters, tier, lib.SubmitKindChild); err != nil {
				refuse(ruleDelegationTarget, refusalNotice(noticeDelegationRefusedFor, err))
				return nil, false
			}
		}
		// The lists are the target's: the profile the address resolved to, so a
		// list on a pinned profile can't be stepped round by addressing another.
		target := addressee
		if route != nil {
			target = route.Profile
		}
		// The A2A door's callers delegate only under a list of their own,
		// which nothing renders yet (gke-labs#2478): an absent list there is
		// nobody, where on a chat backend it is everyone the ingress admits.
		if g.doorUnlisted(target, parent.Requester.Backend) {
			refuse(ruleDelegationDoorUnlisted, fmt.Sprintf(noticeDelegationNotAllowed, target))
			return nil, false
		}
		if !g.targetAllows(target, parent.Requester.Backend, parent.Requester.Subject) {
			refuse(ruleDelegationAllowedUsers, fmt.Sprintf(noticeDelegationNotAllowed, target))
			return nil, false
		}
		// Anyone in the room can steer the turn, so everyone who did is checked
		// as the requester is: otherwise an off-list human steers the session
		// into delegating under the requester's name.
		if parent.SteerAuthorsOverflow {
			refuse(ruleDelegationSteerAuthor, noticeDelegationSteerOverflow, "steerAuthors", "over-cap")
			return nil, false
		}
		for _, a := range parent.SteerAuthors {
			if rule := g.authorRefusal(target, a, ruleDelegationSteerAuthor); rule != "" {
				refuse(rule, fmt.Sprintf(noticeDelegationNotAllowed, target), "steerBackend", a.Backend, "steerAuthor", a.Subject)
				return nil, false
			}
		}
		// And everyone whose text reached this incarnation, on any turn: the
		// pod keeps what it was told, so an off-list human's earlier turn can
		// leave the instruction a later on-list turn delegates on. The check
		// above pinned the parent to rec.BusSession, so the set is the parent's
		// incarnation's.
		sessionAuthors, sessionUnknown, _ := rec.sessionAuthorsOf()
		if profileParent {
			sessionAuthors, sessionUnknown = nil, false
		}
		if sessionUnknown {
			refuse(ruleDelegationSessionAuthor, noticeDelegationSessionIncomplete, "sessionAuthors", "incomplete")
			return nil, false
		}
		for _, a := range sessionAuthors {
			if rule := g.authorRefusal(target, a, ruleDelegationSessionAuthor); rule != "" {
				refuse(rule, fmt.Sprintf(noticeDelegationNotAllowed, target), "sessionBackend", a.Backend, "sessionAuthor", a.Subject)
				return nil, false
			}
		}
		return route, true
	}
	routes := make([]*profileRoute, 0, len(req.Entries()))
	for _, e := range req.Entries() {
		route, ok := check(e)
		if !ok {
			return
		}
		routes = append(routes, route)
	}
	authority.Via = &AuthorityVia{TaskID: taskID, Session: session}
	if len(routes) > 1 {
		minting = true
		g.mintBatch(ctx, rec, taskID, parent, req.Tasks, routes, authority, noChild, log)
		return
	}
	route := routes[0]
	addressee := strings.TrimSpace(req.Addressee)

	// The child is a fixed-route task: addressed to platform so steers and a
	// stop reach it, with BusSession left in place for the wake turn.
	// The correlationId is the parent's (the library's child-envelope rule),
	// read off the active task, which is the parent and always carries one.
	prevAddressee, prevActive := rec.Addressee, rec.ActiveTask
	// The parent's rolling line lives on ActiveTask, which the child is
	// about to take; kept on the parent's entry, it still closes on the
	// parent's terminal (relayTerminal).
	setParentLine(rec, taskID, prevActive.StatusMsgID)
	lineNote := delegatedLineNote
	rec.Addressee = addressee
	if route != nil {
		rec.Addressee, lineNote = route.Profile, fmt.Sprintf(delegatedProfileLineNote, route.Profile)
	}
	childID, ok := g.startTaskWith(ctx, rec, taskStart{
		Text:                 req.Text,
		Requester:            *parent.Requester,
		Authority:            authority,
		CorrelationID:        prevActive.CorrelationID,
		Role:                 taskRoleChild,
		ParentTaskID:         taskID,
		Depth:                parent.Depth + 1,
		LineNote:             lineNote,
		Route:                route,
		SteerAuthors:         parent.SteerAuthors,
		SteerAuthorsOverflow: parent.SteerAuthorsOverflow,
		RootTaskID:           parent.rootID(),
		// The parent's link goes into the same record write as the child's
		// entry: written after, a restart or a failed relay write in
		// between would leave the child on record with a parent that looks
		// like it never delegated.
		LinkParent: true,
	})
	if !ok {
		// startTaskWith has said why, in the log and to the room; the
		// parent is still the conversation's task, and a child that never
		// reached the bus leaves no entry in the chain, no link on the
		// parent and no route.
		rec.Addressee, rec.ActiveTask = prevAddressee, prevActive
		setParentLine(rec, taskID, "")
		g.dropFailedChildren(ctx, rec, taskID)
		noChild(notStartedEnd(whyChildOffBus))
		return
	}
	log.Info("delegation minted", "parent", taskID, "child", childID, "addressee", addressee)
}

// noticeBatchChildWithdrawn closes the line of a batch child that reached
// the bus when a later one in its batch did not: the batch is withdrawn whole.
// reasonBatchWithdrawn is the reason token on the terminal audit line of a
// batch child withdrawn because a later child in its batch could not start.
const reasonBatchWithdrawn = "batch-withdrawn"

const noticeBatchChildWithdrawn = "🛑 not run: another task in the same delegation could not be started"

// mintBatch mints a checked batch of two or more children of taskID. Unlike a
// single child, none takes the active slot or the record's addressee: the
// parent keeps both, and each child carries its own addressee on its entry
// (AddresseeFor) and its rolling line there too. The batch's plan
// (BatchSize) is written on the parent before any child is published, and
// each child is linked from the parent in the record write that adds it, so
// a batch cut short by a restart is finished by its request coming back,
// which skips the entries already minted (BatchIndex). A mint that fails partway
// withdraws the batch (withdrawBatch): each child already on the bus is sent
// a cancel and dropped, and the parent records that its request minted no
// child, so the record is as it was before the request. A child whose cancel
// couldn't be published stays on the record, canceled, linked and indexed.
func (g *Gateway) mintBatch(ctx context.Context, rec *SessionRecord, taskID string, parent TaskRef, tasks []lib.DelegateTask,
	routes []*profileRoute, authority Authority, noChild func(string), log *slog.Logger) {
	correlationID := rec.ActiveTask.CorrelationID
	// The plan goes on the parent before any child is published, in its own
	// record write: from here a restart partway through leaves a record that
	// says the batch is unfinished (BatchSize over the children linked), and
	// the request's redelivery or the heal finishes or withdraws it
	// (handleDelegateRequest's resuming case). A plan that can't be written
	// mints nothing.
	if parent.BatchSize == 0 {
		rec.setBatchSize(taskID, len(tasks))
		if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
			g.log.Error("session record write failed for a batch's plan; nothing minted", "conversation", rec.Key, "err", err)
			rec.setBatchSize(taskID, 0)
			noChild(notStartedEnd(whyBatchPlanUnwritten))
			return
		}
	}
	// A batch being finished skips the entries already minted.
	var minted []string
	have := map[int]bool{}
	for _, k := range rec.batchChildren(taskID) {
		have[k.BatchIndex] = true
		minted = append(minted, k.ID)
	}
	for i, e := range tasks {
		if have[i+1] {
			continue
		}
		if g.beforeBatchChild != nil && g.beforeBatchChild(i) {
			log.Warn("delegation batch cut short", "parent", taskID, "minted", len(minted), "tasks", len(tasks))
			return
		}
		addressee, lineNote := strings.TrimSpace(e.Addressee), delegatedLineNote
		if routes[i] != nil {
			addressee, lineNote = routes[i].Profile, fmt.Sprintf(delegatedProfileLineNote, routes[i].Profile)
		}
		childID, ok := g.startTaskWith(ctx, rec, taskStart{
			Text:                 e.Text,
			Requester:            *parent.Requester,
			Authority:            authority,
			CorrelationID:        correlationID,
			Role:                 taskRoleChild,
			ParentTaskID:         taskID,
			Depth:                parent.Depth + 1,
			LineNote:             lineNote,
			Route:                routes[i],
			SteerAuthors:         parent.SteerAuthors,
			SteerAuthorsOverflow: parent.SteerAuthorsOverflow,
			RootTaskID:           parent.rootID(),
			LinkParent:           true,
			Addressee:            addressee,
			KeepActive:           true,
			BatchIndex:           i + 1,
			Target:               batchTarget(batchParams(e, routes[i])),
		})
		if !ok {
			// The parent records that its request ran nothing, whether or
			// not a child whose cancel failed stays on the record: a
			// withdrawn batch wakes nothing (batchChildEnded).
			g.withdrawBatch(ctx, rec, taskID, minted, authority)
			noChild(notStartedEnd(whyChildOffBus))
			log.Warn("delegation batch withdrawn: a child could not be started", "parent", taskID, "minted", len(minted), "tasks", len(tasks))
			return
		}
		minted = append(minted, childID)
		log.Info("delegation minted", "parent", taskID, "child", childID, "addressee", addressee, "batch", len(tasks))
	}
}

// withdrawBatch undoes the children of a batch that minted partway. Each in
// minted is on the bus, so it is sent a cancel (and its stop record, for a
// profile task). One whose cancel went out has its line closed, its terminal
// logged (it wrote an ingress line) and its entry, link and index dropped,
// with the child that failed. One whose cancel could not be published may
// still run, so it stays on the record, marked canceled and indexed, where a
// later stop and the reaper can still find it; it reports how many stayed.
// A canceled child that ends wakes nothing.
func (g *Gateway) withdrawBatch(ctx context.Context, rec *SessionRecord, taskID string, minted []string, authority Authority) int {
	kept := map[string]bool{}
	for _, id := range minted {
		ref, ok := rec.TaskRefFor(id)
		if !ok {
			continue
		}
		g.publishDispatchStop(ctx, ref)
		if !g.publishBatchCancel(ctx, rec, ref, authority, batchCancelWithdrawn) {
			rec.MarkCanceled(id)
			rec.setCancelUnsent(id, true)
			kept[id] = true
			continue
		}
		if ref.StatusMsgID != "" {
			g.editLine(rec.Key, ref.StatusMsgID, noticeBatchChildWithdrawn, "")
		}
		g.logTaskTerminal(rec, ref.Addressee, id, lib.StateCanceled, TerminalFromGateway,
			"reason: "+reasonBatchWithdrawn+" - "+noticeBatchChildWithdrawn)
	}
	keep := rec.Tasks[:0]
	for _, ref := range rec.Tasks {
		if ref.Role == taskRoleChild && ref.ParentTaskID == taskID && !kept[ref.ID] {
			g.retireTaskRoute(ctx, ref.ID)
			continue
		}
		keep = append(keep, ref)
	}
	rec.Tasks = keep
	for i := range rec.Tasks {
		if rec.Tasks[i].ID != taskID {
			continue
		}
		var links []string
		for _, id := range rec.Tasks[i].Children {
			if kept[id] {
				links = append(links, id)
			}
		}
		rec.Tasks[i].Children = links
	}
	return len(kept)
}

// publishBatchCancel sends a batch child its cancel, on its own .in under
// the delegation's authority, and reports whether it went out. what names
// the child in the log lines: batchCancelWithdrawn or batchCancelAsked.
func (g *Gateway) publishBatchCancel(ctx context.Context, rec *SessionRecord, ref TaskRef, authority Authority, what string) bool {
	env, err := lib.NewCancelEnvelope(gatewayParty, ref.ID, rec.ContextID, ref.CorrelationID,
		lib.WithTo(lib.Party{Session: ref.Addressee}),
		lib.WithAuthority(authority.Render(ref.Capability)))
	if err != nil {
		g.log.Error("cancel envelope build failed for "+what, "taskId", ref.ID, "err", err)
		return false
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(ref.Addressee, ref.ID), env); err != nil {
		g.log.Error("cancel publish failed for "+what, "taskId", ref.ID, "err", err)
		return false
	}
	return true
}

// withdrawCutShortBatch withdraws a batch a gateway stopped partway through
// when its request, back again, can't finish it: the turn was stopped, a
// check now refuses an entry, or the request no longer matches the plan.
// Each child already on the bus is sent a cancel (withdrawBatch), and the
// parent records why its request ran nothing. Called under the session
// lock; it writes the record.
func (g *Gateway) withdrawCutShortBatch(ctx context.Context, rec *SessionRecord, taskID string, log *slog.Logger) {
	parent, ok := rec.TaskRefFor(taskID)
	if !ok {
		return
	}
	// The cancels ride the delegation's own authority, as the mint's did;
	// an attribution that can't be read leaves the block empty, and no
	// executor checks a cancel's grants.
	authority, _ := delegationAuthority(parent)
	if authority.Via == nil {
		authority.Via = &AuthorityVia{TaskID: taskID, Session: parent.Addressee}
	}
	kept := g.withdrawBatch(ctx, rec, taskID, parent.Children, authority)
	rec.markDelegationEnd(taskID, notStartedEnd(whyBatchCutShort))
	if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
		g.log.Error("session record write failed after a cut-short batch was withdrawn", "conversation", rec.Key, "err", err)
	}
	log.Warn("delegation batch withdrawn: it was cut short and could not be finished", "parent", taskID,
		"minted", len(parent.Children), "tasks", parent.BatchSize, "kept", kept)
}

// setBatchSize records a batch's plan on its parent's entry (BatchSize).
func (rec *SessionRecord) setBatchSize(taskID string, n int) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].BatchSize = n
		}
	}
}

// batchTargetCap bounds a batch child's Target, in bytes: it names the child
// in the wake and a status, and an entry's parameters are the session's to
// write.
const batchTargetCap = 160

// batchTargetSep and batchTargetKV join a child's parameters in its Target.
const (
	batchTargetSep = ", "
	batchTargetKV  = "="
)

// batchParams is the parameters a batch child runs with: its route's, as
// resolved (a pinned profile's include its cluster), or the entry's own for
// platform, which takes none.
func batchParams(e lib.DelegateTask, route *profileRoute) map[string]string {
	if route != nil {
		return route.Params
	}
	return e.Parameters
}

// delegationAuthority is the authority a gateway-sent message for a child of
// parent rides, as the child's mint did: the parent's stored attribution
// with a via naming the parent and the session it ran on. false when the
// attribution can't be read.
func delegationAuthority(parent TaskRef) (Authority, bool) {
	authority, err := AuthorityFromAttribution(parent.Attribution)
	if err != nil || len(parent.Attribution) == 0 {
		return Authority{}, false
	}
	authority.Via = &AuthorityVia{TaskID: parent.ID, Session: parent.Addressee}
	return authority, true
}

// batchTarget is a batch entry's parameters as the wake and a status name
// the child: each name=value, sorted by name, held to batchTargetCap. Empty
// for an entry with none (platform).
func batchTarget(params map[string]string) string {
	if len(params) == 0 {
		return ""
	}
	parts := make([]string, 0, len(params))
	for _, k := range slices.Sorted(maps.Keys(params)) {
		parts = append(parts, k+batchTargetKV+params[k])
	}
	return truncateRunes(strings.Join(parts, batchTargetSep), batchTargetCap)
}

// What publishBatchCancel's log lines call the child it cancels.
const (
	batchCancelWithdrawn = "a withdrawn batch child"
	batchCancelAsked     = "a batch child that asked for input"
)

// delegateEvidence is what a reader of a task knows about a delegate request
// on its stream.
type delegateEvidence int

const (
	// delegateAbsent: the stream carries no delegate request.
	delegateAbsent delegateEvidence = iota
	// delegateSeen: the stream carries one (read in the fold, or run by
	// this process's relay).
	delegateSeen
	// delegateUnknown: the stream could not be read, and this process never
	// saw the task's events from the start.
	delegateUnknown
)

// settleHandOff writes on a session turn's entry, from what the stream
// shows, why its delegate request minted no child, when nothing on the
// record says so yet: a request on the stream with no child and no reason
// recorded (its outcome was lost with the relay's write), or a stream that
// could not be read for a turn whose request may still be pending. Then
// handOffEnd decides the turn's end the same way for every reader. A turn
// with a child, a reason already, or no request on its stream is left
// alone, as is a task that is not a session turn.
func (g *Gateway) settleHandOff(rec *SessionRecord, taskID string, evidence delegateEvidence) {
	ref, ok := rec.TaskRefFor(taskID)
	if !ok || !g.sessionTurn(ref) || len(ref.Children) > 0 || ref.DelegationEnd != "" {
		return
	}
	switch evidence {
	case delegateSeen:
		rec.markDelegationEnd(taskID, notStartedEnd(whyNoChild))
	case delegateUnknown:
		if rec.mayHaveUnhandledDelegate(taskID) {
			rec.markDelegationEnd(taskID, notStartedEnd(whyRequestUnread))
		}
	}
}

// sessionTurn reports a task the conversation's own session ran: not a
// child, and not addressed to a fixed executor (platform, or the
// install's fixed default).
func (g *Gateway) sessionTurn(ref TaskRef) bool {
	return ref.Role != taskRoleChild && ref.Addressee != "" && ref.Addressee != targetPlatform &&
		ref.Addressee != g.cfg.DefaultAddressee
}

// doorUnlisted reports a delegation to target checked against someone who
// came in through the A2A door when no list for that backend exists. The
// door is the exception to targetAllows's absent-list rule: a chat backend's
// ingress allowlist is a list of people an operator chose, while the door's
// principal map admits programs, so the door delegates only under a list of
// its own (spec-chatops-gateway.md, "Sessions by default"; the CR field that
// would render one is gke-labs#2478).
//
// The inject door (backend "inject") is deliberately not included, and is
// not an oversight to fix: it exists only where an operator arms it on a dev
// or eval install, its principal map already names who may use it, and the
// live runs and evals exercise delegation through it. Its absent list stays
// everyone its map admits, as a chat backend's does.
//
// The door's Google sign-in class (a2aGoogleBackend, a2adoor_google.go) is
// held to the same rule: its callers are verified people, but they reach the
// door through a program, and bnaylor decided (2026-10-07) that a door
// caller delegates only under a list of its own, whichever class verified
// them.
func (g *Gateway) doorUnlisted(target, backend string) bool {
	return (backend == a2aBackend || backend == a2aGoogleBackend) && g.targetAllowed[target][backend] == nil
}

// authorRefusal is the rule a steer author or incarnation-set member refuses
// a delegation to target under, or "" when they pass: the door's rule first,
// then the target's list under the caller's own rule.
func (g *Gateway) authorRefusal(target string, a TaskRequester, rule string) string {
	if g.doorUnlisted(target, a.Backend) {
		return ruleDelegationDoorUnlisted
	}
	if !g.targetAllows(target, a.Backend, a.Subject) {
		return rule
	}
	return ""
}

// wakeTruncatedNote follows the "…" truncateRunes leaves on a wake body cut
// at the cap.
const wakeTruncatedNote = " (truncated; the full result is in the conversation)"

// wakeFenceMax bounds the fence around the child's result in the wake text.
// The fence is one backtick longer than the longest backtick run in the body
// (at least three), so the body goes in verbatim and no line of it can close
// the block early: a CommonMark closing fence must be at least as long as
// the opening one. A body with a run of wakeFenceMax or more backticks has
// each such run broken by a zero-width space before fencing, which keeps the
// fence, and with it the overhead the cap reserves, bounded.
const wakeFenceMax = 16

// wakeResultLabel is the line between the wake's header and the fenced
// result: the text below is the addressee's output, not the user's ask.
const wakeResultLabel = "Result from " + targetPlatform + " (not from the user):"

// wakeAskLabel opens the wake's text when the delegating turn's request is
// on record: the wake's pod starts with no memory, so without it the session
// reads a result to a question it never saw.
const wakeAskLabel = "You were asked:"

// wakeAskCap bounds the request copy on a history entry (TaskRef.Request), in
// bytes, marker included. Smaller than lib.DelegateTextCap on purpose: every
// human turn stores one, the record holds up to taskHistoryCap entries, and
// 16 KiB each would put a full history near the KV's message-size ceiling,
// where 1 KiB keeps it at 50 KiB. A question longer than that is rare, and
// the wake needs the question, not every word of it.
const wakeAskCap = 1024

// wakeAskTruncatedNote follows the "…" truncateRunes leaves on an ask cut at
// wakeAskCap.
const wakeAskTruncatedNote = " (truncated)"

// capAsk is text held to wakeAskCap bytes, cut on a rune boundary and marked.
func capAsk(text string) string {
	if len(text) <= wakeAskCap {
		return text
	}
	return truncateRunes(text, wakeAskCap-len("…")-len(wakeAskTruncatedNote)) + wakeAskTruncatedNote
}

// wakeText is the wake turn's prompt. With the delegating turn's
// request on record it opens with the label and the request fenced - the
// human's text, untrusted like the result, so it is fenced the same way and
// no line of it can close the block and pass for the gateway's own - then
// the header naming the child and its outcome; with none (a legacy entry) the
// header alone opens it. Then, when there is a result or reason, the label
// and the body fenced. Everything after the header line is held to
// lib.DelegateTextCap bytes, label and fences included; the body is cut on a
// rune boundary and marked. The request is held to wakeAskCap before the
// fence, so the whole text is bounded by the two caps and their fences. The
// relay has already posted the whole result to the conversation.
//
// turns are the answers of the child's earlier turns, in order, when it ran
// follow-ups: the first answers the delegated request, and the result
// answers the last follow-up. A completed wake's body is then every answer in
// order, each follow-up's under wakeFollowUpMarker, so the session reads the
// answer to what it asked and not only the last. A child that answered turns
// and then ended otherwise wakes with those answers, then its reason under
// wakeEndMarker. With none the body is the result or the reason alone, as
// before.
func wakeText(state lib.TaskState, childID, ask string, turns []string, result, reason string, fullAnswer, cardsPending bool) string {
	outcome, completed, body := "completed", true, result
	switch state {
	case lib.StateFailed, lib.StateCanceled: // a canceled the gateway did not publish
		outcome, completed, body = "failed", false, reason
	case lib.StateRejected:
		outcome, completed, body = "was rejected", false, reason
	}
	earlier, body := wakeAnswers(turns, outcome, completed, body)
	var text string
	if ask = strings.TrimSpace(ask); ask != "" {
		// Runs broken first, then the cap: each break adds a zero-width
		// space, so capping first could leave the ask over wakeAskCap.
		ask = capAsk(breakBacktickRuns(ask, wakeFenceMax-1))
		fence := wakeFence(ask)
		text = wakeAskLabel + "\n" + fence + "\n" + ask + "\n" + fence + "\n" +
			fmt.Sprintf("You delegated to %s (task %s), which %s.", targetPlatform, childID, outcome)
	} else {
		text = fmt.Sprintf("The task you delegated to %s (task %s) %s.", targetPlatform, childID, outcome)
	}
	extraReserve := 0
	if cardsPending {
		text += "\n" + wakeCardsPendingNote
		extraReserve = len("\n") + len(wakeCardsPendingNote)
	}
	if earlier != "" {
		// wakeAnswers trimmed each answer; last opens with its separator.
		text += "\n" + fenceWakeBody(earlier, body, extraReserve)
	} else if body = strings.TrimSpace(body); body != "" {
		text += "\n" + fenceWakeBody("", body, extraReserve)
	}
	if fullAnswer {
		return text + "\n" + wakeReplyGuideFull
	}
	return text + "\n" + wakeReplyGuide
}

// wakeCardsPendingNote is added to a completed wake when the delegated child
// handed work to kanban cards whose report posts to the conversation.
const wakeCardsPendingNote = "The delegated turn handed work to a Cluster Agent card. That card's report posts into this conversation on its own; if it is not above yet, it is still to come."

// handedOff is cardsPending's other signal: the task published
// lib.ArtifactHandoff, which the hermes-bridge does on every chat route,
// where the mark is Slack-with-KAGE_SLACK_UX only (#3258).
func handedOff(state lib.TaskState, handoff bool) bool {
	return state == lib.StateCompleted && handoff
}

// foldHandedOff reports whether a folded task carries lib.ArtifactHandoff,
// for a terminal a heal or a replay found on the stream.
func foldHandedOff(task *lib.Task) bool {
	art := task.Artifact(lib.ArtifactHandoff)
	return art != nil && lastTextPart(art.Parts) == lib.HandoffCards
}

// cardsPending reports whether a completed task's terminal mark signals that
// asynchronous kanban cards are still running.
func cardsPending(state lib.TaskState, terminal chatmsg.Reading) bool {
	return state == lib.StateCompleted && terminal.Mark != nil && terminal.Mark.Ask != "" && terminal.Mark.Settle == ""
}

// wakeReplyGuide and wakeReplyGuideFull close every wake's text, chosen by
// whether the person asking has seen the child's result (wakeNeedsFullAnswer).
// In a chat the relay has already posted the child's whole result, so the
// wake adds what it means rather than repeating it. A program behind a door
// receives only the wake's reply as the root's deliverable (spec-chatops-
// gateway, "To a program behind a door"), and a chat whose child terminal
// was found by a heal saw only a status card, so there the wake gives the
// whole answer. Either way the wake's pod has no delegate
// tool (delegateToolFor): an interim answer such as "still checking, the
// results will post here" is reported as that, not asked again, which is how
// a wake used to loop to the depth bound with nothing answered.
const (
	wakeReplyGuide     = "The user has already seen what came back above. Reply in a few sentences with what it means for their question, without repeating it. If it failed, or says more is still running or will follow later, say so plainly. You can't delegate on this turn."
	wakeReplyGuideFull = "The person asking hasn't seen what came back above; they see only your reply. Give them the complete answer to their question from it. If it failed, or says more is still running or will follow later, say so plainly. You can't delegate on this turn."
)

// wakeReplyGuideMax is the longer of the two guides, which the result
// body's budget reserves room for.
var wakeReplyGuideMax = max(len(wakeReplyGuide), len(wakeReplyGuideFull))

// wakeFollowUpMarker opens each follow-up's answer in a wake body that
// carries a child's turns, numbered from the first follow-up.
const wakeFollowUpMarker = "(follow-up %d answer)"

// nonTextTurnAnswer stands in, in a wake, for a turn answer with no text, so
// the answers after it keep their numbers.
const nonTextTurnAnswer = "(a non-text answer; see the stream)"

// wakeEndMarker opens a child's reason after its turn answers in a wake
// whose child did not complete, with the header's outcome.
const wakeEndMarker = "(then it %s)"

// wakeTurnsUnread opens a wake body whose child's earlier turn answers could
// not be read: the render state lost some to a restart and the stream's
// replay failed. The answers that survived would be read by position, so the
// first would pass for the answer to the delegated request; none are sent.
const wakeTurnsUnread = "(the answers to any earlier turns could not be read from the stream)"

// withTurnsUnread is body, the result or the reason, after wakeTurnsUnread;
// the stand-in alone for an empty body. The cap applies to the whole, as to
// any wake body (fenceWakeBody).
func withTurnsUnread(body string) string {
	if body = strings.TrimSpace(body); body == "" {
		return wakeTurnsUnread
	}
	return wakeTurnsUnread + "\n\n" + body
}

// wakeAnswers splits a child's answers for fenceWakeBody: earlier, every
// turn answer, each follow-up's under its marker, and last, body (the result
// or the reason) under the last follow-up's marker when completed, else under
// wakeEndMarker, and empty for an empty reason. With no turns earlier is
// empty and last is body untouched.
func wakeAnswers(turns []string, outcome string, completed bool, body string) (earlier, last string) {
	if len(turns) == 0 {
		return "", body
	}
	var b strings.Builder
	for i, t := range turns {
		if t = strings.TrimSpace(t); t == "" {
			t = nonTextTurnAnswer
		}
		if i > 0 {
			b.WriteString("\n\n" + fmt.Sprintf(wakeFollowUpMarker, i) + "\n")
		}
		b.WriteString(t)
	}
	if completed {
		return b.String(), "\n\n" + fmt.Sprintf(wakeFollowUpMarker, len(turns)) + "\n" + strings.TrimSpace(body)
	}
	if body = strings.TrimSpace(body); body == "" {
		return b.String(), ""
	}
	return b.String(), "\n\n" + fmt.Sprintf(wakeEndMarker, outcome) + "\n" + body
}

// fenceWakeBody is the label, the fence, the capped body and the fence. The
// body is earlier then last. Over the cap, earlier is cut first, so the
// newest answer arrives whole when it fits the cap alone; when it does not,
// the joined body is cut from its end as a lone result is. Either cut is
// marked. When cards are pending, extraReserve reserves room for the note.
func fenceWakeBody(earlier, last string, extraReserve int) string {
	earlier = breakBacktickRuns(earlier, wakeFenceMax-1)
	last = breakBacktickRuns(last, wakeFenceMax-1)
	body := earlier + last
	fence := wakeFence(body)
	// label \n fence \n body \n fence, then \n and the closing guide
	// (wakeReplyGuide or wakeReplyGuideFull), which wakeText appends after
	// this block.
	budget := lib.DelegateTextCap - len(wakeResultLabel) - 2*len(fence) - 3 - 1 - wakeReplyGuideMax - extraReserve
	if len(body) > budget {
		if keep := budget - len(last) - len("…") - len(wakeTruncatedNote); earlier != "" && keep >= 0 {
			body = truncateRunes(earlier, keep) + wakeTruncatedNote + last
		} else {
			body = truncateRunes(body, budget-len("…")-len(wakeTruncatedNote)) + wakeTruncatedNote
		}
		fence = wakeFence(body) // a cut has no longer run: it can only shrink
	}
	return wakeResultLabel + "\n" + fence + "\n" + body + "\n" + fence
}

// wakeFence is a backtick fence one longer than body's longest run, at least
// three.
func wakeFence(body string) string {
	longest, run := 0, 0
	for i := 0; i < len(body); i++ {
		if body[i] == '`' {
			run++
			longest = max(longest, run)
		} else {
			run = 0
		}
	}
	return strings.Repeat("`", max(3, longest+1))
}

// breakBacktickRuns inserts a zero-width space after every n consecutive
// backticks, so no run in the body is longer than n.
func breakBacktickRuns(body string, n int) string {
	if !strings.Contains(body, strings.Repeat("`", n+1)) {
		return body
	}
	var b strings.Builder
	run := 0
	for _, r := range body {
		if r == '`' {
			if run == n {
				b.WriteString("​")
				run = 0
			}
			run++
		} else {
			run = 0
		}
		b.WriteRune(r)
	}
	return b.String()
}

// liveChild names a child task of this conversation that has not ended: a
// child entry whose task the gateway still routes. The task index is the
// liveness the relay itself keeps; relayTerminal retires it on the child's
// terminal, from the executor or the supervisor, and healActiveTask on the
// heal that releases one.
//
// A lookup that errors cannot rule the child out, so it counts as live: a
// refused request can be asked again, a second live child cannot be undone.
func (g *Gateway) liveChild(ctx context.Context, rec *SessionRecord) string {
	for _, ref := range rec.Tasks {
		// A child whose cancel went out doesn't hold the conversation: its
		// writes are dropped (applyCanceledEvent), and the heal ends one
		// that never confirms (healStoppedTasks). One marked canceled whose
		// cancel never reached the bus (CancelUnsent) may still run, so it
		// still counts.
		if ref.Role != taskRoleChild || (ref.Canceled && !ref.CancelUnsent) {
			continue
		}
		key, err := g.lookupTaskSession(ctx, ref.ID)
		if err != nil {
			g.log.Error("task index lookup failed; counting the child as live", "taskId", ref.ID, "conversation", rec.Key, "err", err)
			return ref.ID
		}
		if key != "" {
			return ref.ID
		}
	}
	return ""
}

// setParentLine records (or, given "", clears) the delegating turn's
// rolling-line message on its history entry.
func setParentLine(rec *SessionRecord, taskID, statusMsgID string) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].StatusMsgID = statusMsgID
		}
	}
}

// dropFailedChildren removes the entry, the index and the parent's link of a
// child of taskID whose submission never reached the bus. The parent had no
// child on record before (the busy check), so any child entry naming it is
// the failed one, and so is every link it holds. A batch that fails partway
// is withdrawBatch's.
func (g *Gateway) dropFailedChildren(ctx context.Context, rec *SessionRecord, taskID string) {
	kept := rec.Tasks[:0]
	for _, ref := range rec.Tasks {
		if ref.Role != taskRoleChild || ref.ParentTaskID != taskID {
			kept = append(kept, ref)
			continue
		}
		g.retireTaskRoute(ctx, ref.ID)
	}
	rec.Tasks = kept
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].Children = nil
		}
	}
}

// dropFailedWake removes the entry and the index of a wake of childID whose
// submission never reached the bus. A child starts one wake at most, so any
// wake entry naming it is the failed one.
func (g *Gateway) dropFailedWake(ctx context.Context, rec *SessionRecord, childID string) {
	kept := rec.Tasks[:0]
	for _, ref := range rec.Tasks {
		if ref.Role != taskRoleWake || ref.ParentTaskID != childID {
			kept = append(kept, ref)
			continue
		}
		g.retireTaskRoute(ctx, ref.ID)
	}
	rec.Tasks = kept
}

// deferNotice holds a notice for the task until its terminal relays
// (flushNotices). Render state is cache, so a gateway restart in between
// loses it; the audit line is the record.
func (g *Gateway) deferNotice(taskID, notice string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	rs, ok := g.relays[taskID]
	if !ok {
		rs = &relayState{}
		g.relays[taskID] = rs
	}
	rs.notices = append(rs.notices, notice)
}

// flushNotices posts the notices held for a task, once.
func (g *Gateway) flushNotices(conversation string, rs *relayState) {
	g.mu.Lock()
	notices := rs.notices
	rs.notices = nil
	g.mu.Unlock()
	for _, n := range notices {
		g.post(conversation, n)
	}
}

// wakeSession starts the session's next turn on a delegated child's terminal
// (spec-chatops-gateway.md, "Sessions by default"): the live pod or a fresh
// incarnation, as a human turn gets, with gateway-authored text
// carrying the outcome and the delegating
// turn's stored attribution, so the wake runs under the requester who asked.
// Called under the session lock, after the child's result or failure is
// posted and its ActiveTask released: from relayTerminal on the child's
// relayed terminal, and from healActiveTask on a terminal the heal found on
// the stream. The caller writes the record back.
//
// It reports whether the wake reached the bus, and when it did not, why, in
// a phrase for the chain root's terminal reason (observeChildEnd). A stop
// the requester asked for reports false and no reason.
func (g *Gateway) wakeSession(ctx context.Context, rec *SessionRecord, child TaskRef, state lib.TaskState, turns []string, result, reason string, resultPosted, cardsPending bool) (bool, string) {
	log := g.log.With("child", child.ID, "conversation", rec.Key, "state", string(state))
	// The gateway published a cancel for the child: the human said stop,
	// and whatever the executor answered with, waking the session would act
	// against it. A canceled nobody asked for (a supervisor's) is a failure
	// below, and wakes.
	if child.Canceled {
		log.Info("no wake: the child was stopped by its requester")
		return false, ""
	}
	// A later turn holds the conversation (the child was stopped and a human
	// moved on before its end arrived). A fresh incarnation now would retire
	// that turn's pod.
	if rec.ActiveTask != nil {
		log.Info("no wake: another task holds the conversation", "active", rec.ActiveTask.TaskID)
		return false, "another task holds the conversation"
	}
	parent, authority, why, ok := g.wakeParent(rec, child, log)
	if !ok {
		return false, why
	}
	// The delegating turn's request, carried down a longer chain: the
	// human's question, not an intermediate wake's gateway-authored text.
	text := wakeText(state, child.ID, parent.Request, turns, result, reason, wakeNeedsFullAnswer(&parent, resultPosted), cardsPending)
	// The wake reads the child's result, which steers into the child
	// shaped, and continues the parent's turn: it carries both entries'
	// steer authors, so a delegation from it is checked against them.
	var steered TaskRef
	steered.carrySteerAuthors(parent)
	steered.carrySteerAuthors(child)
	return g.startWake(ctx, rec, child, parent, authority, steered, text, log)
}

// wakeParent is what a wake needs from the turn that delegated: its entry,
// and its stored attribution as the wake's authority, with a via naming
// child, whose end starts the wake. false, with the reason for the chain
// root's terminal, when there is no spawner to start a session, or the
// delegating turn's requester is no longer on record (which posts the
// notice saying so).
func (g *Gateway) wakeParent(rec *SessionRecord, child TaskRef, log *slog.Logger) (TaskRef, Authority, string, bool) {
	if g.spawner == nil {
		log.Warn("no wake: no session spawner")
		return TaskRef{}, Authority{}, "no session spawner", false
	}
	parent, ok := rec.TaskRefFor(child.ParentTaskID)
	authority, err := AuthorityFromAttribution(parent.Attribution)
	if !ok || parent.Requester == nil || len(parent.Attribution) == 0 || err != nil {
		log.Warn("no wake: the delegating turn's requester is not on record", "parent", child.ParentTaskID)
		g.post(rec.Key, noticeWakeNoRequester)
		return TaskRef{}, Authority{}, "the delegating turn's " + requesterGone, false
	}
	// The session that delegated: the parent's addressee is the incarnation
	// it ran on, which the mint checked was the record's bus session.
	authority.Via = &AuthorityVia{TaskID: child.ID, Session: parent.Addressee}
	return parent, authority, "", true
}

// startWake starts the wake turn with text on the parent's live pod or a
// fresh incarnation, continuing child's chain: wakeSession's for one child,
// wakeBatch's for a fan-out, whose last child to end is child. steered
// carries the steer authors the wake inherits. It reports whether the wake
// reached the bus, and when it did not, why.
func (g *Gateway) startWake(ctx context.Context, rec *SessionRecord, child, parent TaskRef, authority Authority, steered TaskRef, text string, log *slog.Logger) (bool, string) {
	if rec.Profile == "" {
		rec.Profile = sessionProfile
	}
	// The wake goes to the parent's pod when it is live and can take it
	// (reuseLivePod): the same incarnation, so its author set is the
	// parent's already and nothing is re-seeded. Its adapter runs the wake
	// with the delegate tool off, from the via above.
	if !g.reuseLivePod(ctx, rec) {
		// Otherwise the wake's pod is a fresh incarnation. It reads the
		// transcript primer like any other, and its text also carries the
		// child's result, and the child's ask was written by the parent's
		// incarnation. So the wake's incarnation starts from the parent's
		// set, taken before freshIncarnation rotates it away.
		inherited, inheritedUnknown, inheritedSince := rec.sessionAuthorsOf()
		// The cap holds and the previous pod is retired here; a refusal
		// has posted the standard notice, and the child's result stands
		// as relayed.
		if !g.freshIncarnation(ctx, rec) {
			log.Info("no wake: the session could not be started")
			return false, "the session could not be started"
		}
		rec.seedSessionAuthors(inherited, inheritedUnknown, inheritedSince)
	}
	// The wake is the delegating turn's successor: the chain's correlation
	// id (a task spawned in service of another inherits it) and the child's
	// depth, so depth counts delegations rather than turns.
	wakeID, ok := g.startTaskWith(ctx, rec, taskStart{
		Text:                 text,
		Requester:            *parent.Requester,
		Authority:            authority,
		CorrelationID:        child.CorrelationID,
		Role:                 taskRoleWake,
		ParentTaskID:         child.ID,
		Depth:                child.Depth,
		SteerAuthors:         steered.SteerAuthors,
		SteerAuthorsOverflow: steered.SteerAuthorsOverflow,
		RootTaskID:           child.rootID(),
		Request:              parent.Request,
	})
	if !ok {
		// startTaskWith has said why. A wake that never reached the bus
		// leaves no entry and no route, as a failed child does
		// (dropFailedChildren): otherwise a read of the settled chain
		// follows the child to a wake with nothing on its stream, not to
		// the end observeChildEnd records on the child.
		g.dropFailedWake(ctx, rec, child.ID)
		return false, "the wake could not be started"
	}
	log.Info("session woken", "wake", wakeID, "session", rec.BusSession)
	return true, ""
}

// observeChildEnd announces a delegation chain's root terminal when its
// child ended and no wake runs: the one end the observers are owed, since the
// turn that delegated and the child both ended quietly (observedAs). A stop
// the requester asked for is the root canceled; otherwise the root failed,
// with the child's terminal source and a reason token saying the session was
// not woken and why. Nothing is delivered: the child's result is the wake's
// to digest, and it was posted to the conversation.
func (g *Gateway) observeChildEnd(rec *SessionRecord, child TaskRef, state lib.TaskState, source TerminalSource, reason, why string) {
	g.observeChainEnd(rec, child, child.Canceled, state, source, reason, why)
}

// observeChainEnd is observeChildEnd with whether the chain was stopped
// given rather than read off the child: a fan-out's is its parent's stop, or
// every child's (batchChildEnded).
func (g *Gateway) observeChainEnd(rec *SessionRecord, child TaskRef, stopped bool, state lib.TaskState, source TerminalSource, reason, why string) {
	end := ChainEnd{State: lib.StateCanceled, Source: source, Reason: reason}
	if !stopped {
		end = ChainEnd{State: lib.StateFailed, Source: source,
			Reason: fmt.Sprintf("reason: %s - %s; the delegated task %s ended %s", reasonWakeNotStarted, why, child.ID, state)}
	}
	// On the child's entry, so a read of the settled chain reports the same
	// end (probeConversation); the caller writes the record.
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == child.ID {
			rec.Tasks[i].ChainEnd = &end
		}
	}
	g.observeTaskTerminal(rec, child.rootID(), end.State, end.Source, end.Reason)
	// The chain ends at the child, so its mark settles here, as the chain's
	// end the observers were just told (terminalMark leaves a child's
	// terminal to this or to the wake).
	g.applyTerminalMark(rec, child.ID, end.State)
}

// wakeNeedsFullAnswer reports whether the wake has to give the whole answer
// rather than explain one already shown: when the chain's answer goes to a
// program behind a door, or when the child's result wasn't posted to the
// conversation (a heal found the terminal and posted only a status card).
func wakeNeedsFullAnswer(parent *TaskRef, resultPosted bool) bool {
	return !resultPosted || wakeToDoor(parent)
}

// wakeToDoor reports whether the chain's answer goes to a program behind a
// door (the inject door or the A2A door, by the requester's backend) rather
// than into a chat, where the child's result has already been posted.
func wakeToDoor(parent *TaskRef) bool {
	if parent == nil || parent.Requester == nil {
		return false
	}
	switch parent.Requester.Backend {
	case injectBackend, a2aBackend, a2aGoogleBackend:
		return true
	}
	return false
}
