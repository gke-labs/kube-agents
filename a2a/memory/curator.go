package memory

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
)

const (
	defaultCuratorRepoDir  = "/opt/data/memory/knowledge-store"
	defaultCuratorSession  = "agent"
	minRecurrenceThreshold = 2
	maxSeenEnvelopes       = 4096
	maxQueryCacheEntries   = 256
	defaultMaxQueryResults = 8
	queryHandlerTimeout    = 30 * time.Second
	hindsightRecallTimeout = 4 * time.Second
	stateSnapshotFile      = "decisions_state.json"
)

var queryStopwords = map[string]bool{
	"a": true, "about": true, "after": true, "against": true, "all": true,
	"allowed": true, "an": true, "and": true, "any": true, "are": true,
	"as": true, "at": true, "be": true, "because": true, "been": true,
	"before": true, "between": true, "both": true, "but": true, "by": true,
	"can": true, "change": true, "changes": true, "check": true, "could": true,
	"decide": true, "decided": true, "decision": true, "decisions": true,
	"did": true, "do": true, "does": true, "for": true, "from": true,
	"get": true, "got": true, "had": true, "has": true, "have": true,
	"how": true, "if": true, "in": true, "into": true, "is": true,
	"it": true, "its": true, "know": true, "learn": true, "learned": true,
	"lesson": true, "lessons": true, "make": true, "may": true, "me": true,
	"memory": true, "might": true, "must": true, "my": true, "need": true,
	"no": true, "not": true, "of": true, "on": true, "or": true,
	"our": true, "out": true, "over": true, "policy": true, "pr": true,
	"prs": true, "question": true, "recall": true, "record": true,
	"records": true, "remember": true, "rule": true, "rules": true,
	"set": true, "should": true, "so": true, "some": true, "still": true,
	"such": true, "team": true, "tell": true, "than": true, "that": true,
	"the": true, "their": true, "them": true, "then": true, "there": true,
	"these": true, "they": true, "this": true, "those": true, "through": true,
	"to": true, "under": true, "up": true, "us": true, "use": true,
	"used": true, "using": true, "was": true, "we": true, "were": true,
	"what": true, "when": true, "where": true, "which": true, "while": true,
	"who": true, "why": true, "will": true, "with": true, "would": true,
	"you": true, "your": true,
}

// CuratorConfig configures the Go Curator/Dreamer service.
type CuratorConfig struct {
	RepoDir             string
	RepoID              string
	Session             string
	Profile             string
	RecurrenceThreshold int
	Client              *lib.Client
	Hindsight           *HindsightClient
	LLM                 LLMConfig
	Logger              *slog.Logger
}

// Curator ingests learning outcomes from the dedicated SubjectCuratorOutcomes
// NATS subject (and candidate proposals from SubjectAnnotations), maintains OKF
// records in knowledge/, indexes them into Hindsight (kube-agents-decisions)
// with in-place status transitions, promotes multi-cluster recurrences into
// fleet-wide decisions, broadcasts the updated digest on
// a2a.topics.shared.blueprint, and serves semantic memory queries over NATS
// request-reply (SubjectMemoryQuery) with automatic cache invalidation on
// outcome updates.
type Curator struct {
	cfg           CuratorConfig
	log           *slog.Logger
	mu            sync.Mutex
	bankEnsured   bool
	seenEnvelopes map[string]bool
	graduatedIDs  []string
	queryCache    map[string]MemoryQueryResponse
	sub           lib.Subscription
	outcomeSubs   []lib.Subscription
	querySubs     []lib.Subscription
}

// OutcomeProcessResult reports what the Curator changed when handling an OutcomeEvent.
type OutcomeProcessResult struct {
	RecordID           string          `json:"record_id"`
	Status             RecordStatus    `json:"status"`
	Path               string          `json:"path"`
	Branch             string          `json:"branch,omitempty"`
	Title              string          `json:"title,omitempty"`
	PRMarkdown         string          `json:"pr_markdown,omitempty"`
	DocumentID         string          `json:"document_id"`
	GraduatedDecisions []string        `json:"graduated_decisions,omitempty"`
	Blueprint          BlueprintDigest `json:"blueprint"`
}

// NewCurator creates a Curator with the given configuration.
func NewCurator(cfg CuratorConfig) (*Curator, error) {
	if cfg.RepoDir == "" {
		cfg.RepoDir = defaultCuratorRepoDir
	}
	if cfg.Session == "" {
		cfg.Session = defaultCuratorSession
	}
	if cfg.RecurrenceThreshold < minRecurrenceThreshold {
		cfg.RecurrenceThreshold = minRecurrenceThreshold
	}
	log := cfg.Logger
	if log == nil {
		log = slog.Default()
	}
	if err := os.MkdirAll(cfg.RepoDir, recordDirPerm); err != nil {
		return nil, err
	}
	repoRoot, err := os.OpenRoot(cfg.RepoDir)
	if err != nil {
		return nil, err
	}
	defer repoRoot.Close()

	baseRoot := stripKnowledgeLeaf(intentRoots(cfg.RepoDir)[0])
	decSub := resolveKnowledgeSubdirRoot(repoRoot, baseRoot, "decision", "decisions")
	if err := repoRoot.MkdirAll(filepath.FromSlash(decSub), recordDirPerm); err != nil {
		return nil, err
	}
	lesSub := resolveKnowledgeSubdirRoot(repoRoot, baseRoot, "lesson", "lessons")
	if err := repoRoot.MkdirAll(filepath.FromSlash(lesSub), recordDirPerm); err != nil {
		return nil, err
	}
	return &Curator{
		cfg:           cfg,
		log:           log,
		seenEnvelopes: map[string]bool{},
		queryCache:    map[string]MemoryQueryResponse{},
	}, nil
}

func (c *Curator) ensureBankOnceLocked(ctx context.Context) {
	if c.cfg.Hindsight == nil || c.bankEnsured {
		return
	}
	if err := c.cfg.Hindsight.EnsureBank(ctx); err == nil {
		c.bankEnsured = true
	} else {
		c.log.Warn("curator: Hindsight bank setup failed; will retry", "err", err)
	}
}

func (c *Curator) markSeenLocked(envelopeID string) bool {
	if envelopeID == "" {
		return false
	}
	if c.seenEnvelopes[envelopeID] {
		return true
	}
	if len(c.seenEnvelopes) >= maxSeenEnvelopes {
		clear(c.seenEnvelopes)
	}
	c.seenEnvelopes[envelopeID] = true
	return false
}

func isRedirectedJetStreamDelivery(msg *nats.Msg) bool {
	if msg == nil {
		return true
	}
	if strings.HasPrefix(msg.Reply, "$JS.") {
		return true
	}
	if msg.Header != nil {
		if msg.Header.Get("Nats-Stream") != "" || msg.Header.Get("Nats-Subject") != "" {
			return true
		}
	}
	return false
}

// Start subscribes to a2a.topics.shared.annotations, registers the NATS memory
// query responder on SubjectMemoryQuery, and processes both any latest unread
// journal entry on TOPICS-JOURNAL and all live outcome publishes.
func (c *Curator) Start(ctx context.Context) error {
	c.mu.Lock()
	c.ensureBankOnceLocked(ctx)
	c.mu.Unlock()

	if c.cfg.Client == nil {
		return errors.New("curator: NATS client is required for Start")
	}

	sub, err := c.cfg.Client.SubscribeCore(SubjectAnnotations, func(msg *nats.Msg) {
		env, err := lib.ParseEnvelope(msg.Data)
		if err != nil {
			c.log.Warn("curator: ignored malformed envelope on annotations", "err", err)
			return
		}
		c.handleAnnotationEnvelope(context.Background(), env)
	})
	if err != nil {
		return fmt.Errorf("curator subscribe %s: %w", SubjectAnnotations, err)
	}
	c.sub = sub

	// Subscribe to the dedicated Curator outcome subject (_INBOX.agent.curator.outcomes,
	// reserved to the agent principal by NATS auth callout so worker/session pods
	// cannot forge approved, resolved-merged, modified-merged, or refused outcomes).
	outcomeSubjects := []string{SubjectCuratorOutcomes}
	if extraOut := strings.TrimSpace(os.Getenv("CURATOR_OUTCOMES_SUBJECT")); extraOut != "" && extraOut != SubjectCuratorOutcomes {
		outcomeSubjects = append(outcomeSubjects, extraOut)
	}
	for _, outSubj := range outcomeSubjects {
		expectedSubj := outSubj
		oSub, oErr := c.cfg.Client.SubscribeCore(expectedSubj, func(msg *nats.Msg) {
			c.handleCuratorOutcomeMsg(context.Background(), msg, expectedSubj)
		})
		if oErr != nil {
			c.Stop()
			return fmt.Errorf("curator subscribe %s: %w", expectedSubj, oErr)
		}
		c.outcomeSubs = append(c.outcomeSubs, oSub)
	}

	// Register the NATS request-reply responder for semantic memory queries on
	// SubjectMemoryQuery (_INBOX.agent.memory.query, covered by the agent
	// principal's publish and subscribe grants) so callers can query memory over
	// NATS without coupling to a specific storage backend.
	querySubjects := []string{SubjectMemoryQuery}
	if extra := strings.TrimSpace(os.Getenv("CURATOR_QUERY_SUBJECT")); extra != "" && extra != SubjectMemoryQuery {
		querySubjects = append(querySubjects, extra)
	}
	for _, qSubj := range querySubjects {
		expectedSubj := qSubj
		qSub, qErr := c.cfg.Client.SubscribeCore(expectedSubj, func(msg *nats.Msg) {
			c.handleQueryMsg(context.Background(), msg, expectedSubj)
		})
		if qErr != nil {
			c.Stop()
			return fmt.Errorf("curator subscribe %s: %w", expectedSubj, qErr)
		}
		c.querySubs = append(c.querySubs, qSub)
	}

	// Catch up with the latest entry on TOPICS-JOURNAL if present and not yet processed.
	records, err := LoadRecords(c.cfg.RepoDir)
	latestEnv, latestErr := c.cfg.Client.ReadTopicLatest(ctx, lib.StreamTopicsJournal, SubjectAnnotations)
	if latestErr == nil && latestEnv != nil {
		if !shouldSkipStartupEnvelope(latestEnv, records) {
			c.handleAnnotationEnvelope(ctx, latestEnv)
			records, err = LoadRecords(c.cfg.RepoDir)
		}
	} else if latestErr != nil && !errors.Is(latestErr, lib.ErrTopicEmpty) {
		c.log.Debug("curator: initial journal read skipped", "err", latestErr)
	}

	// Publish initial blueprint state if records exist on disk.
	if err == nil && len(records) > 0 {
		_ = c.publishBlueprint(ctx, records, "startup-sync", "")
	}
	return nil
}

func shouldSkipStartupEnvelope(env *lib.Envelope, records []Record) bool {
	ev, ok := ExtractOutcomeEvent(env)
	if !ok {
		return true
	}
	// Shared annotations may only carry candidate proposals, never privileged transitions.
	if ev.Kind != OutcomeProposed {
		return true
	}
	if ev.Kind == OutcomeProposed && ev.RecordID == "" {
		if matchExistingProposedOrActive(ev, records) != "" {
			return true
		}
	}
	return false
}

func normalizeCompareText(s string) string {
	return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(s))), " ")
}

func matchExistingProposedOrActive(ev OutcomeEvent, records []Record) string {
	rtype := ev.RecordType
	if rtype == "" {
		if strings.HasPrefix(ev.RecordID, "LES-") {
			rtype = RecordTypeLesson
		} else {
			rtype = RecordTypeDecision
		}
	}
	wantRule := normalizeCompareText(ev.Rule)
	wantTitle := normalizeCompareText(ev.Title)
	wantSlug := Slug(firstNonEmpty(ev.Title, ev.Rule, ev.Summary))
	prRef := formatPRRef(ev)

	for _, r := range records {
		if r.Meta.Type != rtype {
			continue
		}
		if r.Meta.Status != StatusProposed && r.Meta.Status != StatusActive {
			continue
		}
		if prRef != "" && r.Meta.Evidence.PullRequest == prRef {
			return r.Meta.ID
		}
		var haveRule string
		if r.Meta.Guard != nil {
			haveRule = normalizeCompareText(r.Meta.Guard.Rule)
		}
		haveTitle := normalizeCompareText(r.Meta.Title)
		if wantRule != "" && (haveRule == wantRule || haveTitle == wantRule) {
			return r.Meta.ID
		}
		if wantTitle != "" && (haveTitle == wantTitle || haveRule == wantTitle) {
			return r.Meta.ID
		}
		if wantSlug != "" && wantSlug != "record" {
			if Slug(r.Meta.Title) == wantSlug || (r.Meta.Guard != nil && Slug(r.Meta.Guard.Rule) == wantSlug) {
				return r.Meta.ID
			}
		}
	}
	return ""
}

// Stop unsubscribes from NATS.
func (c *Curator) Stop() {
	if c.sub != nil {
		c.sub.Stop()
		c.sub = nil
	}
	for _, os := range c.outcomeSubs {
		os.Stop()
	}
	c.outcomeSubs = nil
	for _, qs := range c.querySubs {
		qs.Stop()
	}
	c.querySubs = nil
}

func (c *Curator) handleAnnotationEnvelope(ctx context.Context, env *lib.Envelope) {
	if env == nil || env.Kind != lib.KindTopicUpdate {
		return
	}
	c.mu.Lock()
	alreadySeen := c.markSeenLocked(env.EnvelopeID)
	c.mu.Unlock()
	if alreadySeen {
		return
	}

	ev, ok := ExtractOutcomeEvent(env)
	if !ok {
		return
	}
	// Security invariant: worker profiles with publish access to the shared
	// annotations topic may only emit candidate proposals (status:proposed),
	// never privileged lifecycle transitions (approved, modified-merged,
	// resolved-merged, pr-merged-persists, refused, or retired).
	if ev.Kind != OutcomeProposed {
		c.log.Warn("curator: rejected privileged outcome transition on shared annotations topic; must use SubjectCuratorOutcomes",
			"kind", ev.Kind, "record_id", ev.RecordID)
		return
	}
	res, err := c.ProcessOutcome(ctx, ev, env.CorrelationID)
	if err != nil {
		c.log.Error("curator: failed to process proposal event",
			"kind", ev.Kind, "record_id", ev.RecordID, "err", err)
		return
	}
	c.log.Info("curator: processed proposal event",
		"kind", ev.Kind,
		"record_id", res.RecordID,
		"status", res.Status,
	)
}

func (c *Curator) handleCuratorOutcomeMsg(ctx context.Context, msg *nats.Msg, expectedSubj string) {
	if msg == nil || len(msg.Data) == 0 {
		return
	}
	if expectedSubj != "" && msg.Subject != expectedSubj {
		c.log.Warn("curator: rejected outcome message with unexpected subject", "subject", msg.Subject, "expected", expectedSubj)
		return
	}
	if isRedirectedJetStreamDelivery(msg) {
		c.log.Warn("curator: rejected redirected JetStream delivery on outcome subject", "subject", msg.Subject)
		return
	}
	var ev OutcomeEvent
	var corrID string
	if env, err := lib.ParseEnvelope(msg.Data); err == nil && env != nil && env.Kind != "" {
		c.mu.Lock()
		alreadySeen := c.markSeenLocked(env.EnvelopeID)
		c.mu.Unlock()
		if alreadySeen {
			return
		}
		extracted, ok := ExtractOutcomeEvent(env)
		if !ok {
			return
		}
		// Topic-update envelopes from shared streams may never carry privileged transitions,
		// blocking JetStream MSG.NEXT replay even from no_ack consumers.
		if env.Kind == lib.KindTopicUpdate && extracted.Kind != OutcomeProposed {
			c.log.Warn("curator: rejected topic-update envelope carrying privileged outcome", "kind", extracted.Kind)
			return
		}
		ev = extracted
		corrID = env.CorrelationID
	} else {
		if err := json.Unmarshal(msg.Data, &ev); err != nil || !isOutcomeKind(ev.Kind) {
			c.log.Warn("curator: ignored malformed payload on curator outcomes subject")
			return
		}
	}

	res, err := c.ProcessOutcome(ctx, ev, corrID)
	if err != nil {
		c.log.Error("curator: failed to process outcome event",
			"kind", ev.Kind, "record_id", ev.RecordID, "err", err)
		if msg.Reply != "" {
			raw, _ := json.Marshal(MemoryQueryResponse{
				Schema: MemoryQueryReplySchema,
				Error:  err.Error(),
			})
			_ = msg.Respond(raw)
		}
		return
	}
	c.log.Info("curator: processed outcome event",
		"kind", ev.Kind,
		"record_id", res.RecordID,
		"status", res.Status,
		"graduated", res.GraduatedDecisions,
	)
	if msg.Reply != "" {
		raw, _ := json.Marshal(MemoryQueryResponse{
			Schema:  MemoryQueryReplySchema,
			Backend: "curator",
			Bank:    DefaultBankID,
			Outcome: &res,
		})
		_ = msg.Respond(raw)
	}
}

// ExtractOutcomeEvent extracts an OutcomeEvent from a topic-update Envelope on
// a2a.topics.shared.annotations if it carries a DataPart with OutcomeSchema or a valid OutcomeKind.
func ExtractOutcomeEvent(env *lib.Envelope) (OutcomeEvent, bool) {
	var art lib.Artifact
	if err := json.Unmarshal(env.Payload, &art); err != nil {
		return OutcomeEvent{}, false
	}
	var summaryText string
	var ev OutcomeEvent
	foundData := false
	for _, p := range art.Parts {
		switch p.Kind {
		case "text":
			summaryText = p.Text
		case "data":
			if len(p.Data) == 0 {
				continue
			}
			if err := json.Unmarshal(p.Data, &ev); err == nil {
				if ev.Schema == OutcomeSchema || isOutcomeKind(ev.Kind) {
					foundData = true
				}
			}
		}
	}
	if !foundData {
		return OutcomeEvent{}, false
	}
	if ev.Summary == "" && summaryText != "" {
		ev.Summary = summaryText
	}
	if ev.Schema == "" {
		ev.Schema = OutcomeSchema
	}
	return ev, true
}

func isOutcomeKind(k OutcomeKind) bool {
	switch k {
	case OutcomeProposed, OutcomeApproved, OutcomeModifiedMerged, OutcomeResolvedMerged, OutcomePRMergedPersists, OutcomeRefused, OutcomeRetired:
		return true
	}
	return false
}

func gitopsPRsDisabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv("DECISION_MEMORY_GITOPS_PRS")), "false")
}

func isCrashRootCauseEvidence(ev OutcomeEvent) bool {
	combined := strings.ToLower(strings.Join([]string{
		ev.FindingID,
		ev.CheckID,
		ev.Title,
		ev.RootCause,
		ev.Summary,
	}, " "))
	for _, token := range []string{
		"oom", "exit 137", "exitcode 137", "crashloop", "quorum",
		"deadlock", "evict", "outage", "sev-1", "sev-2",
	} {
		if strings.Contains(combined, token) {
			return true
		}
	}
	return false
}

func inferDefaultGuardSeverity(ev OutcomeEvent) string {
	if ev.Guard != nil && strings.TrimSpace(ev.Guard.Severity) != "" {
		return strings.TrimSpace(ev.Guard.Severity)
	}
	if ev.Kind == OutcomeResolvedMerged && isCrashRootCauseEvidence(ev) {
		return "block"
	}
	return "warn"
}

func scopesOverlapPaths(a, b []string) bool {
	if len(a) == 0 || len(b) == 0 {
		return false
	}
	for _, pa := range a {
		if pa == "**" {
			continue
		}
		for _, pb := range b {
			if pb == "**" {
				continue
			}
			if pa == pb || MatchGlob(pa, pb) || MatchGlob(pb, pa) {
				return true
			}
		}
	}
	return false
}

func rejectedGovernedSameSubject(oldRec, activeRec Record) bool {
	if !scopesOverlapPaths(oldRec.Meta.Scope.Paths, activeRec.Meta.Scope.Paths) {
		return false
	}
	for _, oc := range oldRec.Meta.Scope.Checks {
		for _, ac := range activeRec.Meta.Scope.Checks {
			if oc != "" && oc == ac {
				return true
			}
		}
	}
	if oldRec.Meta.Guard != nil && activeRec.Meta.Guard != nil {
		if oldRec.Meta.Guard.Expect != nil && activeRec.Meta.Guard.Expect != nil {
			if oldRec.Meta.Guard.Expect.Path != "" && oldRec.Meta.Guard.Expect.Path == activeRec.Meta.Guard.Expect.Path {
				return true
			}
		}
		oldRule := normalizeCompareText(oldRec.Meta.Guard.Rule)
		newRule := normalizeCompareText(activeRec.Meta.Guard.Rule)
		if oldRule != "" && oldRule == newRule {
			return true
		}
	}
	return false
}

// ProcessOutcome applies one OutcomeEvent to the local knowledge/ directory,
// updates Hindsight (kube-agents-decisions) via document_id replacement, runs
// cross-cluster recurrence graduation if applicable, and publishes the updated
// BlueprintDigest to a2a.topics.shared.blueprint.
func (c *Curator) ProcessOutcome(ctx context.Context, ev OutcomeEvent, correlationID string) (OutcomeProcessResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	repoID := ev.Repo
	if repoID == "" {
		repoID = c.cfg.RepoID
	}
	dateStr := ev.Date
	if len(dateStr) < 10 {
		dateStr = time.Now().UTC().Format("2006-01-02")
	} else {
		dateStr = dateStr[:10]
	}
	year, err := strconv.Atoi(dateStr[:4])
	if err != nil || year < 2000 {
		year = time.Now().UTC().Year()
	}

	records, err := LoadRecords(c.cfg.RepoDir)
	if err != nil {
		return OutcomeProcessResult{}, err
	}
	byID := map[string]Record{}
	for _, r := range records {
		byID[r.Meta.ID] = r
	}

	var targetRec Record
	switch ev.Kind {
	case OutcomeProposed:
		if ev.RecordID == "" {
			if dupID := matchExistingProposedOrActive(ev, records); dupID != "" {
				ev.RecordID = dupID
			}
		}
		if ev.RecordID != "" {
			if existing, ok := byID[ev.RecordID]; ok && (existing.Meta.Status == StatusProposed || existing.Meta.Status == StatusActive) {
				prRef := formatPRRef(ev)
				if prRef == "" || existing.Meta.Evidence.PullRequest == prRef {
					var branch, prMarkdown string
					if existing.Meta.Status == StatusProposed {
						branch = existing.ProposalBranch()
						prMarkdown = existing.RenderForPR()
					}
					digest := BuildBlueprintDigest(repoID, records, "noop:"+existing.Meta.ID, c.graduatedIDs)
					return OutcomeProcessResult{
						RecordID:           existing.Meta.ID,
						Status:             existing.Meta.Status,
						Path:               existing.Path,
						Branch:             branch,
						Title:              existing.Meta.Title,
						PRMarkdown:         prMarkdown,
						DocumentID:         existing.DocumentID(repoID),
						GraduatedDecisions: c.graduatedIDs,
						Blueprint:          digest,
					}, nil
				}
			}
		}
		targetRec = c.buildProposedRecord(ctx, ev, byID, year, dateStr)
	case OutcomeApproved:
		targetRec = c.buildApprovedRecord(ctx, ev, byID, year, dateStr)
	case OutcomeModifiedMerged:
		targetRec = c.buildModifiedMergedRecord(ctx, ev, byID, year, dateStr)
	case OutcomeResolvedMerged:
		targetRec = c.buildResolvedMergedRecord(ctx, ev, byID, year, dateStr)
	case OutcomePRMergedPersists:
		targetRec = c.buildPersistsRecord(ctx, ev, byID, year, dateStr)
	case OutcomeRefused:
		targetRec = c.buildRefusedRecord(ctx, ev, byID, year, dateStr)
	case OutcomeRetired:
		targetRec = c.buildRetiredRecord(ev, byID, year, dateStr)
	default:
		return OutcomeProcessResult{}, fmt.Errorf("unsupported outcome kind %q", ev.Kind)
	}

	if err := SaveRecord(c.cfg.RepoDir, &targetRec); err != nil {
		return OutcomeProcessResult{}, fmt.Errorf("save record %s: %w", targetRec.Meta.ID, err)
	}
	if c.cfg.Hindsight != nil {
		c.ensureBankOnceLocked(ctx)
		if err := c.cfg.Hindsight.RetainRecord(ctx, repoID, targetRec); err != nil {
			return OutcomeProcessResult{}, fmt.Errorf("hindsight retain %s: %w", targetRec.Meta.ID, err)
		}
	}

	// When a record transitions to status:active via a merged knowledge/ PR,
	// reconcile superseded records: (1) explicit Supersedes IDs (including
	// cluster lessons graduated into a fleet-wide DEC-*), and (2) Precedence
	// Rule 3 ("Merged outranks rejected") when governing the same check or field.
	if targetRec.Meta.Status == StatusActive {
		supTargets := map[string]bool{}
		for _, sid := range targetRec.Meta.Supersedes {
			if sid != "" && sid != targetRec.Meta.ID {
				supTargets[sid] = true
			}
		}
		for oid, oldRec := range byID {
			if oid == targetRec.Meta.ID || oldRec.Meta.Status != StatusRejected {
				continue
			}
			if rejectedGovernedSameSubject(oldRec, targetRec) {
				supTargets[oid] = true
			}
		}
		for sid := range supTargets {
			oldRec, ok := byID[sid]
			if !ok || oldRec.Meta.Status == StatusSuperseded {
				continue
			}
			oldRec.Meta.Status = StatusSuperseded
			oldRec.Meta.SupersededBy = targetRec.Meta.ID
			if err := SaveRecord(c.cfg.RepoDir, &oldRec); err != nil {
				return OutcomeProcessResult{}, fmt.Errorf("supersede record %s: %w", sid, err)
			}
			if c.cfg.Hindsight != nil {
				if err := c.cfg.Hindsight.RetainRecord(ctx, repoID, oldRec); err != nil {
					return OutcomeProcessResult{}, fmt.Errorf("hindsight retain superseded %s: %w", sid, err)
				}
			}
		}
	}

	// Check for cross-cluster recurrence graduation (>= RecurrenceThreshold clusters).
	var newlyGraduated []string
	records, err = LoadRecords(c.cfg.RepoDir)
	if err != nil {
		return OutcomeProcessResult{}, err
	}
	if ev.Kind == OutcomeResolvedMerged || ev.Kind == OutcomeApproved || ev.Kind == OutcomeModifiedMerged {
		newlyGraduated, err = c.graduateRecurrencesLocked(ctx, repoID, records, year, dateStr)
		if err != nil {
			return OutcomeProcessResult{}, err
		}
		if len(newlyGraduated) > 0 {
			c.graduatedIDs = append(c.graduatedIDs, newlyGraduated...)
			records, _ = LoadRecords(c.cfg.RepoDir)
		}
	}

	lastDesc := fmt.Sprintf("%s:%s->%s", ev.Kind, targetRec.Meta.ID, targetRec.Meta.Status)
	digest := BuildBlueprintDigest(repoID, records, lastDesc, c.graduatedIDs)
	_ = persistLocalStateSnapshot(c.cfg.RepoDir, digest)
	// Invalidate cached query responses whenever an outcome updates memory state.
	c.queryCache = map[string]MemoryQueryResponse{}
	if c.cfg.Client != nil {
		if err := c.publishBlueprintDigest(ctx, digest, correlationID); err != nil {
			return OutcomeProcessResult{}, fmt.Errorf("publish blueprint: %w", err)
		}
	}

	var branch, prMarkdown string
	if targetRec.Meta.Status == StatusProposed {
		branch = targetRec.ProposalBranch()
		prMarkdown = targetRec.RenderForPR()
	}

	return OutcomeProcessResult{
		RecordID:           targetRec.Meta.ID,
		Status:             targetRec.Meta.Status,
		Path:               targetRec.Path,
		Branch:             branch,
		Title:              targetRec.Meta.Title,
		PRMarkdown:         prMarkdown,
		DocumentID:         targetRec.DocumentID(repoID),
		GraduatedDecisions: newlyGraduated,
		Blueprint:          digest,
	}, nil
}

func (c *Curator) buildProposedRecord(ctx context.Context, ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rtype := ev.RecordType
	if rtype == "" {
		if strings.HasPrefix(ev.RecordID, "LES-") {
			rtype = RecordTypeLesson
		} else {
			rtype = RecordTypeDecision
		}
	}
	rid := ev.RecordID
	if rid == "" {
		rid = NextRecordID(c.cfg.RepoDir, rtype, year)
	}
	existing, hasExisting := byID[rid]

	// When attaching a pull request URL to an already-synthesized record without new
	// rule or summary text, preserve the existing title, guard, and body rather than
	// re-running LLM synthesis on empty fields.
	if hasExisting && ev.Title == "" && ev.Rule == "" && ev.Summary == "" && ev.RootCause == "" && ev.Fix == "" {
		prRef := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest)
		existing.Meta.Evidence.PullRequest = prRef
		existing.Meta.Evidence.BreakingPR = firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR)
		existing.Meta.Evidence.Incident = firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident)
		existing.Meta.Evidence.Commit = firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit)
		existing.Meta.Evidence.FindingID = firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID)
		existing.Meta.Evidence.SoakWindow = firstNonEmpty(ev.SoakWindow, existing.Meta.Evidence.SoakWindow)
		return existing
	}

	synth := c.cfg.LLM.Synthesize(ctx, rtype, ev)
	title := firstNonEmpty(ev.Title, synthTitle(synth), existing.Meta.Title, ev.Rule, ev.Summary, "Proposed operational rule "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)
	guard := mergeGuard(c.cfg.RepoDir, existing.Meta.Guard, ev, synth, scope.Resources)
	prRef := formatPRRef(ev)
	if prRef == "" {
		prRef = existing.Meta.Evidence.PullRequest
	}

	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: StatusProposed,
		Date:   firstNonEmpty(existing.Meta.Date, dateStr),
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest: prRef,
			BreakingPR:  firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:    firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:      firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:   firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
			SoakWindow:  firstNonEmpty(ev.SoakWindow, existing.Meta.Evidence.SoakWindow),
		},
		Supersedes: firstSlice(ev.Supersedes, existing.Meta.Supersedes),
	}
	body := buildRecordBody(rtype, ev, synth, existing.Body, StatusProposed, "")
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
	}
}

func (c *Curator) buildApprovedRecord(ctx context.Context, ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rid := ev.RecordID
	if rid == "" && ev.PullRequest != "" {
		for _, r := range byID {
			if r.Meta.Evidence.PullRequest == ev.PullRequest {
				rid = r.Meta.ID
				break
			}
		}
	}
	if rid == "" {
		rtype := ev.RecordType
		if rtype == "" {
			rtype = RecordTypeDecision
		}
		rid = NextRecordID(c.cfg.RepoDir, rtype, year)
	}
	existing, hasExisting := byID[rid]
	rtype := existing.Meta.Type
	if rtype == "" {
		if ev.RecordType != "" {
			rtype = ev.RecordType
		} else {
			rtype = RecordTypeDecision
		}
	}
	var synth *SynthesizedRecord
	if !hasExisting || strings.TrimSpace(existing.Body) == "" {
		synth = c.cfg.LLM.Synthesize(ctx, rtype, ev)
	}
	title := firstNonEmpty(ev.Title, existing.Meta.Title, synthTitle(synth), "Approved decision "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)
	guard := mergeGuard(c.cfg.RepoDir, existing.Meta.Guard, ev, synth, scope.Resources)
	if guard != nil && guard.Severity == "" {
		g := *guard
		g.Severity = inferDefaultGuardSeverity(ev)
		guard = &g
	}
	status := StatusActive
	if gitopsPRsDisabled() {
		status = StatusProposed
	}
	prRef := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest)
	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: status,
		Date:   firstNonEmpty(existing.Meta.Date, dateStr),
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest:   prRef,
			RemediationPR: firstNonEmpty(ev.RemediationPR, existing.Meta.Evidence.RemediationPR),
			BreakingPR:    firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:      firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:        firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:     firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
			Outcome:       existing.Meta.Evidence.Outcome,
			SoakWindow:    firstNonEmpty(ev.SoakWindow, existing.Meta.Evidence.SoakWindow),
		},
		Supersedes: firstSlice(ev.Supersedes, existing.Meta.Supersedes),
	}
	body := existing.Body
	if !hasExisting || strings.TrimSpace(body) == "" {
		body = buildRecordBody(rtype, ev, synth, "", status, "")
	} else if ev.Summary != "" {
		body = strings.TrimSpace(body) + "\n\n## Approval\n\n" + strings.TrimSpace(ev.Summary)
	}
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
	}
}

func (c *Curator) buildModifiedMergedRecord(ctx context.Context, ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rid := ev.RecordID
	if rid == "" && ev.PullRequest != "" {
		for _, r := range byID {
			if r.Meta.Evidence.PullRequest == ev.PullRequest {
				rid = r.Meta.ID
				break
			}
		}
	}
	if rid == "" {
		rtype := ev.RecordType
		if rtype == "" {
			rtype = RecordTypeDecision
		}
		rid = NextRecordID(c.cfg.RepoDir, rtype, year)
	}
	existing, hasExisting := byID[rid]
	rtype := existing.Meta.Type
	if rtype == "" {
		if ev.RecordType != "" {
			rtype = ev.RecordType
		} else if strings.HasPrefix(rid, "LES-") {
			rtype = RecordTypeLesson
		} else {
			rtype = RecordTypeDecision
		}
	}

	var initialProposal string
	if ev.InitialProposal != "" {
		initialProposal = ev.InitialProposal
	} else if hasExisting && existing.Meta.Guard != nil && existing.Meta.Guard.Rule != "" {
		initialProposal = "initial-proposal: " + existing.Meta.Guard.Rule
	}

	synth := c.cfg.LLM.Synthesize(ctx, rtype, ev)
	title := firstNonEmpty(ev.Title, synthTitle(synth), existing.Meta.Title, "Modified and merged "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)

	// When a human edits a proposal before merging, prefer the new guard from ev/synth
	// over the superseded existing.Meta.Guard. Default severity to "warn" unless
	// explicitly classified as "block".
	var guard *RecordGuard
	if ev.Guard != nil {
		guard = ev.Guard
	} else if synth != nil && synth.Guard != nil && GuardPassesOnHead(c.cfg.RepoDir, synth.Guard, scope.Resources) {
		guard = synth.Guard
	} else if ev.Rule != "" {
		var exp *GuardExpect
		sev := "warn"
		if existing.Meta.Guard != nil {
			if existing.Meta.Guard.Expect != nil {
				e := *existing.Meta.Guard.Expect
				exp = &e
			}
			if existing.Meta.Guard.Severity != "" {
				sev = existing.Meta.Guard.Severity
			}
		}
		guard = &RecordGuard{
			Rule:     ev.Rule,
			Severity: sev,
			Expect:   exp,
		}
	} else {
		guard = existing.Meta.Guard
	}
	if guard != nil && guard.Severity == "" {
		g := *guard
		g.Severity = inferDefaultGuardSeverity(ev)
		guard = &g
	}

	// Only a human edit on the knowledge/ PR itself transitions directly to
	// status:active. An edit-before-merge on an operational remediation PR
	// synthesizes a status:proposed record so a separate knowledge/ PR is opened.
	isKnowledgePR := ev.KnowledgePR || (hasExisting && existing.Meta.Status == StatusProposed && existing.Meta.Evidence.FindingID == "" && ev.RemediationPR == "")
	status := StatusProposed
	if isKnowledgePR && !gitopsPRsDisabled() {
		status = StatusActive
	}

	rawPR := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest)
	pullReq := rawPR
	remediationPR := firstNonEmpty(ev.RemediationPR, existing.Meta.Evidence.RemediationPR)
	if status == StatusProposed && !isKnowledgePR && !gitopsPRsDisabled() {
		remediationPR = firstNonEmpty(remediationPR, rawPR)
		pullReq = ""
	}
	reason := firstNonEmpty(ev.ReviewerReason, ev.Summary, "Reviewer modified proposal before merging.")
	rejected := appendUniqueStr(existing.Meta.Evidence.RejectedProposals, initialProposal)

	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: status,
		Date:   firstNonEmpty(existing.Meta.Date, dateStr),
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest:       pullReq,
			RemediationPR:     remediationPR,
			BreakingPR:        firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:          firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:            firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:         firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
			Outcome:           string(OutcomeModifiedMerged),
			SoakWindow:        firstNonEmpty(ev.SoakWindow, existing.Meta.Evidence.SoakWindow),
			ReviewerReason:    reason,
			RejectedProposals: rejected,
		},
		Supersedes: firstSlice(ev.Supersedes, existing.Meta.Supersedes),
	}
	body := buildRecordBody(rtype, ev, synth, existing.Body, status, reason)
	modSection := "## Reviewer Modification Before Merge\n\n" + reason
	if initialProposal != "" {
		modSection += "\n\nSuperseded initial proposal: `" + initialProposal + "`"
	}
	if !strings.Contains(body, "## Reviewer Modification Before Merge") {
		body = strings.TrimSpace(body) + "\n\n" + modSection
	}
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
	}
}

func (c *Curator) buildResolvedMergedRecord(ctx context.Context, ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rtype := ev.RecordType
	if rtype == "" {
		if strings.HasPrefix(ev.RecordID, "DEC-") {
			rtype = RecordTypeDecision
		} else {
			rtype = RecordTypeLesson
		}
	}
	rid := ev.RecordID
	if rid == "" {
		rid = NextRecordID(c.cfg.RepoDir, rtype, year)
	}
	existing := byID[rid]
	synth := c.cfg.LLM.Synthesize(ctx, rtype, ev)
	title := firstNonEmpty(ev.Title, synthTitle(synth), existing.Meta.Title, ev.Summary, "Verified fix "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)
	guard := mergeGuard(c.cfg.RepoDir, existing.Meta.Guard, ev, synth, scope.Resources)
	// Newly synthesized records from post-merge soak verification always start
	// in status:proposed so a knowledge/ PR is opened and reviewed before
	// reaching status:active. Guard.Severity preserves the intended post-merge
	// severity ("block" for verified crash root causes, "warn" otherwise), while
	// CheckChangedFiles downgrades all status:proposed findings to "warn".
	status := StatusProposed
	if guard != nil && guard.Severity == "" {
		g := *guard
		g.Severity = inferDefaultGuardSeverity(ev)
		guard = &g
	}
	remediationPR := firstNonEmpty(ev.RemediationPR, formatPRRef(ev), existing.Meta.Evidence.RemediationPR, existing.Meta.Evidence.PullRequest, "#0")
	pullReq := ""
	if gitopsPRsDisabled() {
		pullReq = remediationPR
	}

	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: status,
		Date:   dateStr,
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest:   pullReq,
			RemediationPR: remediationPR,
			BreakingPR:    firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:      firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:        firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:     firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
			Outcome:       string(OutcomeResolvedMerged),
			SoakWindow:    firstNonEmpty(ev.SoakWindow, existing.Meta.Evidence.SoakWindow),
		},
		Supersedes: firstSlice(ev.Supersedes, existing.Meta.Supersedes),
	}
	body := buildRecordBody(rtype, ev, synth, existing.Body, status, "")
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
	}
}

func (c *Curator) buildPersistsRecord(ctx context.Context, ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rid := ev.RecordID
	if rid == "" {
		rid = NextRecordID(c.cfg.RepoDir, RecordTypeLesson, year)
	}
	existing := byID[rid]
	synth := c.cfg.LLM.Synthesize(ctx, RecordTypeLesson, ev)
	title := firstNonEmpty(ev.Title, synthTitle(synth), existing.Meta.Title, "Ineffective fix (issue persisted after merge) "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)
	remediationPR := firstNonEmpty(ev.RemediationPR, formatPRRef(ev), existing.Meta.Evidence.RemediationPR, existing.Meta.Evidence.PullRequest, "#0")
	pullReq := ""
	if gitopsPRsDisabled() {
		pullReq = remediationPR
	}
	reason := firstNonEmpty(ev.ReviewerReason, ev.Verification, ev.Summary, "Finding persisted after the pull request merged.")

	status := StatusProposed
	meta := RecordMeta{
		Type:   RecordTypeLesson,
		ID:     rid,
		Title:  title,
		Status: status,
		Date:   dateStr,
		Scope:  scope,
		Evidence: RecordEvidence{
			PullRequest:       pullReq,
			RemediationPR:     remediationPR,
			BreakingPR:        firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:          firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:            firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:         firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
			Outcome:           string(OutcomePRMergedPersists),
			SoakWindow:        firstNonEmpty(ev.SoakWindow, existing.Meta.Evidence.SoakWindow),
			ReviewerReason:    reason,
			RejectedProposals: appendUniqueStr(existing.Meta.Evidence.RejectedProposals, remediationPR),
		},
	}
	body := buildRecordBody(RecordTypeLesson, ev, synth, existing.Body, status, reason)
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
	}
}

func (c *Curator) buildRefusedRecord(ctx context.Context, ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rid := ev.RecordID
	if rid == "" && ev.PullRequest != "" {
		for _, r := range byID {
			if r.Meta.Evidence.PullRequest == ev.PullRequest {
				rid = r.Meta.ID
				break
			}
		}
	}
	existing, hasExisting := byID[rid]
	prRef := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest)
	reason := firstNonEmpty(ev.ReviewerReason, ev.Summary, "Proposal closed unmerged by reviewer.")

	// If an existing proposed record (such as a proposed chat rule DEC-*) is refused,
	// transition that exact record in-place to status:rejected so update_mode:replace
	// overwrites the proposed entry in Hindsight and leaves no stale rule behind.
	if hasExisting && (existing.Meta.Status == StatusProposed || existing.Meta.Status == StatusRejected || existing.Meta.Type == RecordTypeDecision) {
		existing.Meta.Status = StatusRejected
		if existing.Meta.Guard != nil && existing.Meta.Guard.Severity == "block" {
			g := *existing.Meta.Guard
			g.Severity = "warn"
			existing.Meta.Guard = &g
		}
		existing.Meta.Evidence.Outcome = string(OutcomeRefused)
		existing.Meta.Evidence.ReviewerReason = reason
		if prRef != "" {
			existing.Meta.Evidence.RejectedProposals = appendUniqueStr(existing.Meta.Evidence.RejectedProposals, prRef)
		}
		rationaleBlock := "## Rejection Rationale\n\n" + reason
		if !strings.Contains(existing.Body, rationaleBlock) {
			if idx := strings.Index(existing.Body, "\n\n## Rejection Rationale\n\n"); idx >= 0 {
				existing.Body = strings.TrimSpace(existing.Body[:idx])
			}
			existing.Body = strings.TrimSpace(existing.Body) + "\n\n" + rationaleBlock
		}
		return existing
	}

	rtype := ev.RecordType
	if rtype == "" {
		if strings.HasPrefix(rid, "DEC-") {
			rtype = RecordTypeDecision
		} else {
			rtype = RecordTypeLesson
		}
	}
	if rid == "" {
		rid = NextRecordID(c.cfg.RepoDir, rtype, year)
	}
	status := StatusRejected
	synth := c.cfg.LLM.Synthesize(ctx, rtype, ev)
	title := firstNonEmpty(ev.Title, synthTitle(synth), existing.Meta.Title, "Rejected proposal "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)
	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: status,
		Date:   dateStr,
		Scope:  scope,
		Evidence: RecordEvidence{
			PullRequest:       prRef,
			Incident:          ev.Incident,
			FindingID:         ev.FindingID,
			Outcome:           string(OutcomeRefused),
			ReviewerReason:    reason,
			RejectedProposals: appendUniqueStr(nil, prRef),
		},
	}
	body := buildRecordBody(rtype, ev, synth, "", status, reason)
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
	}
}

func (c *Curator) buildRetiredRecord(ev OutcomeEvent, byID map[string]Record, year int, dateStr string) Record {
	rid := ev.RecordID
	if rid == "" && ev.PullRequest != "" {
		for _, r := range byID {
			if r.Meta.Evidence.PullRequest == ev.PullRequest {
				rid = r.Meta.ID
				break
			}
		}
	}
	existing, hasExisting := byID[rid]
	if hasExisting {
		existing.Meta.Status = StatusRetired
		existing.Meta.Evidence.Outcome = string(OutcomeRetired)
		if ev.ReviewerReason != "" {
			existing.Meta.Evidence.ReviewerReason = ev.ReviewerReason
		}
		return existing
	}
	rtype := ev.RecordType
	if rtype == "" {
		rtype = RecordTypeLesson
	}
	if rid == "" {
		rid = NextRecordID(c.cfg.RepoDir, rtype, year)
	}
	return Record{
		Meta: RecordMeta{
			Type:   rtype,
			ID:     rid,
			Title:  firstNonEmpty(ev.Title, "Retired proposal "+rid),
			Status: StatusRetired,
			Date:   dateStr,
			Scope:  mergeScope(RecordScope{}, ev),
			Evidence: RecordEvidence{
				PullRequest:    formatPRRef(ev),
				Outcome:        string(OutcomeRetired),
				ReviewerReason: ev.ReviewerReason,
			},
		},
		Body: firstNonEmpty(ev.ReviewerReason, ev.Summary, "Closed without a durable policy rejection."),
	}
}

func (c *Curator) graduateRecurrencesLocked(ctx context.Context, repoID string, records []Record, year int, dateStr string) ([]string, error) {
	candidates := DetectCrossClusterRecurrences(records, c.cfg.RecurrenceThreshold)
	if len(candidates) == 0 {
		return nil, nil
	}
	byID := map[string]Record{}
	for _, r := range records {
		byID[r.Meta.ID] = r
	}

	var graduated []string
	for _, cand := range candidates {
		decID := NextRecordID(c.cfg.RepoDir, RecordTypeDecision, year)
		synth := c.cfg.LLM.Synthesize(ctx, RecordTypeDecision, cand)
		title := synthTitle(synth)
		if title == "" {
			if cand.YAMLPath != "" {
				title = fmt.Sprintf("Fleet-wide policy for %s (%s %v) graduated from %s",
					cand.YAMLPath, cand.Op, cand.Value, strings.Join(cand.Clusters, ", "))
			} else {
				title = fmt.Sprintf("Fleet-wide policy for check %s graduated from %s",
					cand.CheckID, strings.Join(cand.Clusters, ", "))
			}
		}
		var guard *RecordGuard
		if cand.YAMLPath != "" && cand.Op != "" {
			fileGlob := cand.FileGlob
			if fileGlob == "" && len(cand.Paths) > 0 {
				fileGlob = cand.Paths[0]
			}
			ruleText := cand.Rule
			if ruleText == "" {
				ruleText = fmt.Sprintf("Enforce %s %s %v across all clusters (graduated from %s).",
					cand.YAMLPath, cand.Op, cand.Value, strings.Join(cand.Lessons, ", "))
			}
			sev := "warn"
			for _, lid := range cand.Lessons {
				if l, ok := byID[lid]; ok && l.Meta.Guard != nil && l.Meta.Guard.Severity == "block" {
					sev = "block"
					break
				}
			}
			if synth != nil && synth.Guard != nil && synth.Guard.Severity == "block" {
				sev = "block"
			}
			candidateGuard := &RecordGuard{
				Rule:     ruleText,
				Severity: sev,
				Expect: &GuardExpect{
					File:  fileGlob,
					Path:  cand.YAMLPath,
					Op:    cand.Op,
					Value: cand.Value,
				},
			}
			if GuardPassesOnHead(c.cfg.RepoDir, candidateGuard, cand.Resources) {
				guard = candidateGuard
			} else {
				guard = &RecordGuard{
					Rule:     ruleText,
					Severity: sev,
				}
			}
		}

		var checks []string
		if cand.CheckID != "" {
			checks = []string{cand.CheckID}
		}
		// Graduated fleet-wide decisions always start in status:proposed so a
		// fleet-wide knowledge/ PR is opened and reviewed before activation.
		// Source cluster lessons remain in their existing status until the
		// graduated DEC-* PR merges and ProcessOutcome marks Supersedes.
		decStatus := StatusProposed
		decMeta := RecordMeta{
			Type:   RecordTypeDecision,
			ID:     decID,
			Title:  title,
			Status: decStatus,
			Date:   dateStr,
			Scope: RecordScope{
				Paths:     cand.Paths,
				Resources: cand.Resources,
				Clusters:  []string{"*"},
				Checks:    checks,
			},
			Guard:      guard,
			Supersedes: cand.Lessons,
		}
		decBody := fmt.Sprintf(
			"## Context\n\nRecurring incident pattern observed across %d clusters (%s) in lessons %s.\n\n"+
				"## Decision\n\nGraduated per-cluster lessons %s into a fleet-wide decision (`clusters: [\"*\"]`).\n\n"+
				"## Consequences\n\nAll future changes matching `%s` are checked against `%s` before a pull request can be opened.",
			len(cand.Clusters),
			strings.Join(cand.Clusters, ", "),
			strings.Join(cand.Lessons, ", "),
			strings.Join(cand.Lessons, ", "),
			strings.Join(cand.Paths, ", "),
			decID,
		)
		decRec := Record{Meta: decMeta, Body: decBody}
		if err := SaveRecord(c.cfg.RepoDir, &decRec); err != nil {
			return graduated, err
		}
		if c.cfg.Hindsight != nil {
			if err := c.cfg.Hindsight.RetainRecord(ctx, repoID, decRec); err != nil {
				return graduated, err
			}
		}
		graduated = append(graduated, decID)
	}
	return graduated, nil
}

func (c *Curator) publishBlueprint(ctx context.Context, records []Record, lastEvent, corrID string) error {
	digest := BuildBlueprintDigest(c.cfg.RepoID, records, lastEvent, c.graduatedIDs)
	return c.publishBlueprintDigest(ctx, digest, corrID)
}

func (c *Curator) publishBlueprintDigest(ctx context.Context, digest BlueprintDigest, corrID string) error {
	if c.cfg.Client == nil {
		return nil
	}
	summary := fmt.Sprintf(
		"OKF decision memory digest for %s: %d active, %d proposed, %d rejected, %d superseded (last event: %s)",
		digest.Repo,
		digest.Counts.Active,
		digest.Counts.Proposed,
		digest.Counts.Rejected,
		digest.Counts.Superseded,
		digest.LastOutcomeEvent,
	)
	art, err := lib.NewTopicArtifact("blueprint", summary, digest)
	if err != nil {
		return err
	}
	if corrID == "" {
		corrID = lib.NewCorrelationID()
	}
	return c.cfg.Client.PublishTopic(
		ctx,
		SubjectBlueprint,
		lib.Party{Session: c.cfg.Session, Profile: c.cfg.Profile},
		"",
		"",
		corrID,
		art,
	)
}

func synthTitle(s *SynthesizedRecord) string {
	if s == nil {
		return ""
	}
	return strings.TrimSpace(s.Title)
}

func mergeScope(base RecordScope, ev OutcomeEvent) RecordScope {
	out := base
	if ev.Scope != nil {
		if len(ev.Scope.Paths) > 0 {
			out.Paths = ev.Scope.Paths
		}
		if len(ev.Scope.Resources) > 0 {
			out.Resources = ev.Scope.Resources
		}
		if len(ev.Scope.Clusters) > 0 {
			out.Clusters = ev.Scope.Clusters
		}
		if len(ev.Scope.Checks) > 0 {
			out.Checks = ev.Scope.Checks
		}
	}
	if ev.Cluster != "" && len(out.Clusters) == 0 {
		out.Clusters = []string{ev.Cluster}
	}
	if ev.CheckID != "" && len(out.Checks) == 0 {
		out.Checks = []string{ev.CheckID}
	}
	if len(out.Paths) == 0 && len(out.Resources) == 0 && len(out.Checks) == 0 {
		out.Paths = []string{"**"}
	}
	return out
}

func mergeGuard(repoDir string, existing *RecordGuard, ev OutcomeEvent, synth *SynthesizedRecord, resources []string) *RecordGuard {
	if ev.Guard != nil {
		return ev.Guard
	}
	if existing != nil {
		return existing
	}
	if synth != nil && synth.Guard != nil {
		if GuardPassesOnHead(repoDir, synth.Guard, resources) {
			return synth.Guard
		}
	}
	if ev.Rule != "" {
		return &RecordGuard{
			Rule:     ev.Rule,
			Severity: inferDefaultGuardSeverity(ev),
		}
	}
	return nil
}

func formatPRRef(ev OutcomeEvent) string {
	if ev.PullRequest != "" {
		return ev.PullRequest
	}
	if ev.PRNumber > 0 {
		if ev.Repo != "" {
			return fmt.Sprintf("https://github.com/%s/pull/%d (#%d)", ev.Repo, ev.PRNumber, ev.PRNumber)
		}
		return fmt.Sprintf("#%d", ev.PRNumber)
	}
	return ""
}

func buildRecordBody(rtype RecordType, ev OutcomeEvent, synth *SynthesizedRecord, fallback string, status RecordStatus, reviewerReason string) string {
	if rtype == RecordTypeDecision {
		ctxText := firstNonEmpty(ev.Summary, ev.RootCause)
		decText := firstNonEmpty(ev.Rule, ev.Fix, ev.Title)
		consText := ev.Prevention
		if synth != nil {
			ctxText = firstNonEmpty(synth.Context, ctxText)
			decText = firstNonEmpty(synth.Decision, decText)
			consText = firstNonEmpty(synth.Consequences, consText)
		}
		if ctxText == "" && decText == "" && fallback != "" {
			return fallback
		}
		var lines []string
		lines = append(lines, "## Context", "", firstNonEmpty(ctxText, "Captured from team operational decision."), "")
		lines = append(lines, "## Decision", "", firstNonEmpty(decText, ev.Title), "")
		if consText != "" {
			lines = append(lines, "## Consequences", "", consText, "")
		}
		if status == StatusRejected && reviewerReason != "" {
			lines = append(lines, "## Rejection Rationale", "", reviewerReason, "")
		}
		return strings.TrimSpace(strings.Join(lines, "\n"))
	}

	rcText := firstNonEmpty(ev.RootCause, ev.Summary)
	fixText := firstNonEmpty(ev.Fix, ev.Rule)
	verText := firstNonEmpty(ev.Verification, "Verified post-merge on cluster.")
	prevText := firstNonEmpty(ev.Prevention, ev.Rule)
	if synth != nil {
		rcText = firstNonEmpty(synth.RootCause, rcText)
		fixText = firstNonEmpty(synth.Fix, fixText)
		verText = firstNonEmpty(synth.Verification, verText)
		prevText = firstNonEmpty(synth.Prevention, prevText)
	}
	if rcText == "" && fixText == "" && fallback != "" {
		return fallback
	}
	var lines []string
	lines = append(lines,
		"## What happened", "", firstNonEmpty(rcText, ev.Title), "",
		"## What fixed it", "", firstNonEmpty(fixText, "Resolved via GitOps pull request."), "",
		"## How we know it worked", "", verText, "",
		"## Prevent it next time", "", firstNonEmpty(prevText, "Enforce OKF guard before submitting changes."),
	)
	if reviewerReason != "" {
		lines = append(lines, "", "## Reviewer / Outcome Note", "", reviewerReason)
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

func firstSlice(a, b []string) []string {
	if len(a) > 0 {
		return a
	}
	return b
}

func (c *Curator) handleQueryMsg(ctx context.Context, msg *nats.Msg, expectedSubj string) {
	if msg == nil || msg.Reply == "" {
		return
	}
	if expectedSubj != "" && msg.Subject != expectedSubj {
		c.log.Warn("curator: rejected query message with unexpected subject", "subject", msg.Subject, "expected", expectedSubj)
		return
	}
	if isRedirectedJetStreamDelivery(msg) {
		c.log.Warn("curator: rejected redirected JetStream delivery on query subject", "subject", msg.Subject)
		return
	}
	var req MemoryQueryRequest
	if err := json.Unmarshal(msg.Data, &req); err != nil {
		resp := MemoryQueryResponse{
			Schema: MemoryQueryReplySchema,
			Error:  fmt.Sprintf("invalid query payload: %v", err),
		}
		raw, _ := json.Marshal(resp)
		_ = msg.Respond(raw)
		return
	}
	if (req.Action == "propose" || req.Action == "resolve") && expectedSubj != SubjectMemoryQuery {
		resp := MemoryQueryResponse{
			Schema: MemoryQueryReplySchema,
			Error:  fmt.Sprintf("action %q is restricted to %s", req.Action, SubjectMemoryQuery),
		}
		raw, _ := json.Marshal(resp)
		_ = msg.Respond(raw)
		return
	}
	qCtx, cancel := context.WithTimeout(ctx, queryHandlerTimeout)
	defer cancel()
	resp := c.HandleQuery(qCtx, req)
	raw, err := json.Marshal(resp)
	if err != nil {
		return
	}
	_ = msg.Respond(raw)
}

// HandleQuery answers a MemoryQueryRequest against the configured semantic
// memory backend (Hindsight + local OKF records) with in-memory caching that is
// automatically invalidated whenever ProcessOutcome updates the store.
func (c *Curator) HandleQuery(ctx context.Context, req MemoryQueryRequest) MemoryQueryResponse {
	switch req.Action {
	case "propose", "resolve":
		if req.Outcome == nil {
			return MemoryQueryResponse{
				Schema: MemoryQueryReplySchema,
				Error:  "outcome payload is required for action " + req.Action,
			}
		}
		res, err := c.ProcessOutcome(ctx, *req.Outcome, "")
		if err != nil {
			return MemoryQueryResponse{
				Schema: MemoryQueryReplySchema,
				Error:  err.Error(),
			}
		}
		return MemoryQueryResponse{
			Schema:  MemoryQueryReplySchema,
			Backend: "curator",
			Bank:    DefaultBankID,
			Outcome: &res,
		}
	case "pending":
		c.mu.Lock()
		records, err := LoadRecords(c.cfg.RepoDir)
		repoID := c.cfg.RepoID
		c.mu.Unlock()
		if err != nil {
			return MemoryQueryResponse{
				Schema: MemoryQueryReplySchema,
				Error:  err.Error(),
			}
		}
		digest := BuildBlueprintDigest(repoID, records, "pending-query", nil)
		var pending []BlueprintRecordDigest
		for _, rec := range digest.Records {
			if rec.Status == StatusProposed {
				pending = append(pending, rec)
			}
		}
		return MemoryQueryResponse{
			Schema:  MemoryQueryReplySchema,
			Backend: "curator",
			Bank:    DefaultBankID,
			Records: pending,
			Results: []HindsightRecallResult{},
		}
	}

	tags := append([]string(nil), req.Tags...)
	if len(tags) == 0 {
		tags = []string{SharedTag}
	}
	sort.Strings(tags)
	tagsMatch := req.TagsMatch
	if tagsMatch == "" {
		tagsMatch = "all_strict"
	}
	bank := req.Bank
	if bank == "" {
		bank = DefaultBankID
		if c.cfg.Hindsight != nil && c.cfg.Hindsight.bankID != "" {
			bank = c.cfg.Hindsight.bankID
		}
	}
	cacheKey := fmt.Sprintf("%s|%s|%s|%s|%s|%s",
		bank, strings.TrimSpace(req.Query), strings.Join(tags, ","), tagsMatch, req.Path, req.Cluster)

	c.mu.Lock()
	if cached, ok := c.queryCache[cacheKey]; ok {
		c.mu.Unlock()
		cached.Cached = true
		return cached
	}
	c.mu.Unlock()

	backend := "okf-local"
	var results []HindsightRecallResult
	var hsErr error

	c.mu.Lock()
	records, _ := LoadRecords(c.cfg.RepoDir)
	if extRecords, err := LoadExternalDocRecords(c.cfg.RepoDir); err == nil && len(extRecords) > 0 {
		records = append(records, extRecords...)
	}
	repoID := c.cfg.RepoID
	c.mu.Unlock()

	type scoredMatch struct {
		score  int
		digest BlueprintRecordDigest
		result HindsightRecallResult
	}
	var scored []scoredMatch
	qLower := strings.ToLower(strings.TrimSpace(req.Query))
	for _, r := range records {
		recTags := r.Tags()
		if !matchTagSlice(recTags, tags, tagsMatch) {
			continue
		}
		if req.Cluster != "" && !matchesClusterScope(r.Meta.Scope.Clusters, req.Cluster) {
			continue
		}
		if req.Path != "" && !matchesAnyPathGlob(r.Meta.Scope.Paths, req.Path) {
			continue
		}
		rendered := r.RenderForIndex()
		score := 1
		if qLower != "" {
			score = scoreRecordQueryMatch(r, rendered, qLower)
			if score <= 0 {
				continue
			}
		}
		var guardRule string
		if r.Meta.Guard != nil {
			guardRule = r.Meta.Guard.Rule
		}
		var branch, prMarkdown string
		if r.Meta.Status == StatusProposed {
			branch = r.ProposalBranch()
			prMarkdown = r.RenderForPR()
		}
		scored = append(scored, scoredMatch{
			score: score,
			digest: BlueprintRecordDigest{
				ID:             r.Meta.ID,
				Type:           r.Meta.Type,
				Status:         r.Meta.Status,
				Title:          r.Meta.Title,
				Path:           r.Path,
				Branch:         branch,
				PRMarkdown:     prMarkdown,
				Clusters:       r.Meta.Scope.Clusters,
				Checks:         r.Meta.Scope.Checks,
				Paths:          r.Meta.Scope.Paths,
				GuardRule:      guardRule,
				PullRequest:    r.Meta.Evidence.PullRequest,
				RemediationPR:  r.Meta.Evidence.RemediationPR,
				FindingID:      r.Meta.Evidence.FindingID,
				Outcome:        r.Meta.Evidence.Outcome,
				SoakWindow:     r.Meta.Evidence.SoakWindow,
				ReviewerReason: r.Meta.Evidence.ReviewerReason,
				Supersedes:     r.Meta.Supersedes,
				SupersededBy:   r.Meta.SupersededBy,
			},
			result: HindsightRecallResult{
				ID:         r.Meta.ID,
				DocumentID: r.DocumentID(repoID),
				Text:       rendered,
				Tags:       recTags,
			},
		})
	}

	sort.SliceStable(scored, func(i, j int) bool {
		if scored[i].score != scored[j].score {
			return scored[i].score > scored[j].score
		}
		return scored[i].digest.ID < scored[j].digest.ID
	})

	maxRecs := req.MaxResults
	if maxRecs <= 0 {
		maxRecs = defaultMaxQueryResults
	}
	if len(scored) > maxRecs {
		scored = scored[:maxRecs]
	}

	var matchedDigests []BlueprintRecordDigest
	var fallbackResults []HindsightRecallResult
	for _, s := range scored {
		matchedDigests = append(matchedDigests, s.digest)
		fallbackResults = append(fallbackResults, s.result)
	}

	if c.cfg.Hindsight != nil && strings.TrimSpace(req.Query) != "" {
		hsCtx, hsCancel := context.WithTimeout(ctx, hindsightRecallTimeout)
		results, hsErr = c.cfg.Hindsight.Recall(hsCtx, req.Query, tags, tagsMatch)
		hsCancel()
		if hsErr == nil {
			backend = "hindsight"
		}
	}

	if len(fallbackResults) > 0 {
		seenID := map[string]bool{}
		merged := make([]HindsightRecallResult, 0, len(fallbackResults)+len(results))
		for _, fb := range fallbackResults {
			seenID[fb.ID] = true
			if fb.DocumentID != "" {
				seenID[fb.DocumentID] = true
			}
			merged = append(merged, fb)
		}
		for _, hr := range results {
			if (hr.ID != "" && seenID[hr.ID]) || (hr.DocumentID != "" && seenID[hr.DocumentID]) {
				continue
			}
			merged = append(merged, hr)
		}
		results = merged
	}
	if results == nil {
		results = []HindsightRecallResult{}
	}

	resp := MemoryQueryResponse{
		Schema:  MemoryQueryReplySchema,
		Backend: backend,
		Bank:    bank,
		Query:   req.Query,
		Tags:    tags,
		Cached:  false,
		Results: results,
		Records: matchedDigests,
	}
	if hsErr != nil && len(results) == 0 {
		resp.Error = hsErr.Error()
		return resp
	}

	c.mu.Lock()
	if len(c.queryCache) >= maxQueryCacheEntries {
		clear(c.queryCache)
	}
	c.queryCache[cacheKey] = resp
	c.mu.Unlock()
	return resp
}

// InvalidateCache clears the Curator's in-memory query cache.
func (c *Curator) InvalidateCache() {
	c.mu.Lock()
	c.queryCache = map[string]MemoryQueryResponse{}
	c.mu.Unlock()
}

// PublishOutcome sends an OutcomeEvent to the Curator on its dedicated NATS
// subjects (SubjectMemoryQuery request-reply, falling back to
// SubjectCuratorOutcomes), never publishing privileged transitions onto
// SubjectAnnotations.
func PublishOutcome(ctx context.Context, client *lib.Client, ev OutcomeEvent) (OutcomeProcessResult, error) {
	if client == nil {
		return OutcomeProcessResult{}, errors.New("NATS client is nil")
	}
	if ev.Schema == "" {
		ev.Schema = OutcomeSchema
	}
	action := "resolve"
	if ev.Kind == OutcomeProposed {
		action = "propose"
	}
	if qResp, qErr := querySubjectViaNATS(ctx, client, SubjectMemoryQuery, MemoryQueryRequest{
		Schema:  MemoryQuerySchema,
		Action:  action,
		Outcome: &ev,
	}); qErr == nil && qResp.Outcome != nil {
		return *qResp.Outcome, nil
	}
	raw, err := json.Marshal(ev)
	if err != nil {
		return OutcomeProcessResult{}, fmt.Errorf("marshal outcome event: %w", err)
	}
	outSubj := SubjectCuratorOutcomes
	if extraOut := strings.TrimSpace(os.Getenv("CURATOR_OUTCOMES_SUBJECT")); extraOut != "" {
		outSubj = extraOut
	}
	nc := client.Conn()
	msg, reqErr := nc.RequestWithContext(ctx, outSubj, raw)
	if reqErr != nil {
		return OutcomeProcessResult{}, fmt.Errorf("curator outcome request on %s: %w", outSubj, reqErr)
	}
	var resp MemoryQueryResponse
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		return OutcomeProcessResult{}, fmt.Errorf("decode curator outcome reply: %w", err)
	}
	if resp.Error != "" {
		return OutcomeProcessResult{}, errors.New(resp.Error)
	}
	if resp.Outcome != nil {
		return *resp.Outcome, nil
	}
	return OutcomeProcessResult{RecordID: ev.RecordID}, nil
}

// QueryViaNATS sends a MemoryQueryRequest over NATS to the Curator's memory
// responder on SubjectMemoryQuery (or CURATOR_QUERY_SUBJECT when configured)
// and returns the decoded MemoryQueryResponse.
func QueryViaNATS(ctx context.Context, client *lib.Client, req MemoryQueryRequest) (MemoryQueryResponse, error) {
	subj := SubjectMemoryQuery
	if extra := strings.TrimSpace(os.Getenv("CURATOR_QUERY_SUBJECT")); extra != "" {
		subj = extra
	}
	return querySubjectViaNATS(ctx, client, subj, req)
}

func querySubjectViaNATS(ctx context.Context, client *lib.Client, subj string, req MemoryQueryRequest) (MemoryQueryResponse, error) {
	if client == nil {
		return MemoryQueryResponse{}, errors.New("NATS client is nil")
	}
	if req.Schema == "" {
		req.Schema = MemoryQuerySchema
	}
	rawReq, err := json.Marshal(req)
	if err != nil {
		return MemoryQueryResponse{}, fmt.Errorf("marshal memory query: %w", err)
	}
	nc := client.Conn()
	msg, err := nc.RequestWithContext(ctx, subj, rawReq)
	if err != nil {
		return MemoryQueryResponse{}, fmt.Errorf("nats memory query on %s: %w", subj, err)
	}
	var resp MemoryQueryResponse
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		return MemoryQueryResponse{}, fmt.Errorf("decode memory query reply: %w", err)
	}
	if resp.Error != "" {
		return resp, errors.New(resp.Error)
	}
	return resp, nil
}

func matchTagSlice(have, want []string, mode string) bool {
	if len(want) == 0 {
		return true
	}
	set := map[string]bool{}
	for _, t := range have {
		set[t] = true
	}
	if mode == "any_strict" {
		for _, w := range want {
			if set[w] {
				return true
			}
		}
		return false
	}
	for _, w := range want {
		if !set[w] {
			return false
		}
	}
	return true
}

func matchesClusterScope(clusters []string, target string) bool {
	if len(clusters) == 0 {
		return true
	}
	for _, c := range clusters {
		if c == "*" || c == target {
			return true
		}
	}
	return false
}

func matchesAnyPathGlob(patterns []string, target string) bool {
	if len(patterns) == 0 {
		return false
	}
	for _, p := range patterns {
		if MatchGlob(p, target) {
			return true
		}
	}
	return false
}

func tokenizeQueryTerms(qLower string) []string {
	rawTokens := strings.FieldsFunc(qLower, func(r rune) bool {
		return !((r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' || r == '_')
	})
	seen := map[string]bool{}
	var keep []string
	for _, tok := range rawTokens {
		tok = strings.Trim(tok, "-_")
		if len(tok) <= 1 || queryStopwords[tok] {
			continue
		}
		if !seen[tok] {
			seen[tok] = true
			keep = append(keep, tok)
		}
	}
	return keep
}

func termMatchesText(lowerText, term string) bool {
	if strings.Contains(lowerText, term) {
		return true
	}
	if len(term) > 3 && strings.HasSuffix(term, "s") {
		stem := strings.TrimSuffix(term, "s")
		if strings.Contains(lowerText, stem) {
			return true
		}
	}
	return false
}

func scoreRecordQueryMatch(r Record, rendered, qLower string) int {
	lowerText := strings.ToLower(rendered)
	lowerTitle := strings.ToLower(r.Meta.Title + " " + r.Meta.ID)
	if strings.Contains(lowerText, qLower) {
		return 100
	}
	terms := tokenizeQueryTerms(qLower)
	if len(terms) == 0 {
		return 1
	}
	matched := 0
	score := 0
	for _, t := range terms {
		if termMatchesText(lowerText, t) {
			matched++
			score += 10
			if termMatchesText(lowerTitle, t) {
				score += 5
			}
		}
	}
	if matched == 0 {
		return 0
	}
	if len(terms) >= 3 && matched*3 < len(terms) {
		return 0
	}
	return score
}

func persistLocalStateSnapshot(repoDir string, digest BlueprintDigest) error {
	raw, err := json.MarshalIndent(digest, "", "  ")
	if err != nil {
		return err
	}
	if dataDir := strings.TrimSpace(os.Getenv("DATA_DIR")); dataDir != "" {
		if dataRoot, dErr := os.OpenRoot(filepath.Clean(dataDir)); dErr == nil {
			defer dataRoot.Close()
			if err := dataRoot.MkdirAll("memory", recordDirPerm); err == nil {
				return dataRoot.WriteFile(filepath.Join("memory", stateSnapshotFile), raw, recordFilePerm)
			}
		}
	}
	root, err := os.OpenRoot(repoDir)
	if err != nil {
		return err
	}
	defer root.Close()
	return root.WriteFile(stateSnapshotFile, raw, recordFilePerm)
}

func docStatusIsGitGoverned(doc HindsightDocument) bool {
	if doc.DocumentMetadata != nil {
		switch RecordStatus(strings.TrimSpace(doc.DocumentMetadata["status"])) {
		case StatusActive, StatusSuperseded:
			return true
		case StatusProposed, StatusRejected, StatusRetired:
			return false
		}
	}
	for _, t := range doc.Tags {
		switch t {
		case "status:proposed", "status:rejected", "status:retired":
			return false
		case "status:active", "status:superseded":
			return true
		}
	}
	return false
}

// SyncRecordsToHindsight upserts all valid local OKF records and external
// documentation records into Hindsight and deletes only removed status:active or
// status:superseded documents for repoID, preserving pending status:proposed and
// closed status:rejected or status:retired records in Hindsight.
func SyncRecordsToHindsight(ctx context.Context, hs *HindsightClient, repoID, repoDir string) ([]string, []string, error) {
	if hs == nil {
		return nil, nil, errors.New("hindsight client is nil")
	}
	if err := hs.EnsureBank(ctx); err != nil {
		return nil, nil, err
	}
	records, err := LoadRecords(repoDir)
	if err != nil {
		return nil, nil, err
	}
	keepDocIDs := map[string]bool{}
	var upserted []string
	for _, r := range records {
		if len(ValidateRecord(r)) > 0 {
			continue
		}
		if err := hs.RetainRecord(ctx, repoID, r); err != nil {
			return upserted, nil, err
		}
		keepDocIDs[r.DocumentID(repoID)] = true
		upserted = append(upserted, r.Meta.ID)
	}
	if extRecords, err := LoadExternalDocRecords(repoDir); err == nil {
		for _, r := range extRecords {
			if err := hs.RetainRecord(ctx, repoID, r); err != nil {
				return upserted, nil, err
			}
			keepDocIDs[r.DocumentID(repoID)] = true
			upserted = append(upserted, r.Meta.ID)
		}
	}

	prefix := DocPrefix
	if repoID != "" {
		prefix = DocPrefix + repoID + ":"
	}
	var deleted []string
	docs, err := hs.ListDocuments(ctx)
	if err != nil {
		return upserted, nil, err
	}
	for _, doc := range docs {
		if !strings.HasPrefix(doc.ID, prefix) {
			continue
		}
		if repoID == "" && strings.Count(strings.TrimPrefix(doc.ID, DocPrefix), ":") > 0 {
			continue
		}
		if keepDocIDs[doc.ID] || !docStatusIsGitGoverned(doc) {
			continue
		}
		if err := hs.DeleteDocument(ctx, doc.ID); err != nil {
			return upserted, deleted, err
		}
		deleted = append(deleted, doc.ID)
	}
	return upserted, deleted, nil
}
