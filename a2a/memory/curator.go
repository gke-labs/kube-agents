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

// Curator ingests learning outcomes from NATS JetStream (a2a.topics.shared.annotations),
// maintains OKF records in knowledge/, indexes them into Hindsight (kube-agents-decisions)
// with in-place status transitions, promotes multi-cluster recurrences into fleet-wide
// decisions, broadcasts the updated digest on a2a.topics.shared.blueprint, and serves
// semantic memory queries over NATS request-reply (SubjectMemoryQuery) with automatic
// cache invalidation on outcome updates.
type Curator struct {
	cfg           CuratorConfig
	log           *slog.Logger
	mu            sync.Mutex
	seenEnvelopes map[string]bool
	graduatedIDs  []string
	queryCache    map[string]MemoryQueryResponse
	sub           lib.Subscription
	querySubs     []lib.Subscription
}

// OutcomeProcessResult reports what the Curator changed when handling an OutcomeEvent.
type OutcomeProcessResult struct {
	RecordID           string          `json:"record_id"`
	Status             RecordStatus    `json:"status"`
	Path               string          `json:"path"`
	DocumentID         string          `json:"document_id"`
	GraduatedDecisions []string        `json:"graduated_decisions,omitempty"`
	Blueprint          BlueprintDigest `json:"blueprint"`
}

// NewCurator creates a Curator with the given configuration.
func NewCurator(cfg CuratorConfig) (*Curator, error) {
	if cfg.RepoDir == "" {
		cfg.RepoDir = "/opt/data/memory/knowledge-store"
	}
	if cfg.RepoID == "" {
		cfg.RepoID = "default/infra"
	}
	if cfg.Session == "" {
		cfg.Session = "agent"
	}
	if cfg.RecurrenceThreshold < 2 {
		cfg.RecurrenceThreshold = 2
	}
	log := cfg.Logger
	if log == nil {
		log = slog.Default()
	}
	if err := os.MkdirAll(filepath.Join(cfg.RepoDir, "knowledge/decisions"), 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Join(cfg.RepoDir, "knowledge/lessons"), 0o755); err != nil {
		return nil, err
	}
	return &Curator{
		cfg:           cfg,
		log:           log,
		seenEnvelopes: map[string]bool{},
		queryCache:    map[string]MemoryQueryResponse{},
	}, nil
}

// Start subscribes to a2a.topics.shared.annotations, registers the NATS memory
// query responder on SubjectMemoryQuery (and SubjectMemoryQueryPublic when
// permitted by bus grants), and processes both any latest unread journal entry
// on TOPICS-JOURNAL and all live outcome publishes.
func (c *Curator) Start(ctx context.Context) error {
	if c.cfg.Hindsight != nil {
		if err := c.cfg.Hindsight.EnsureBank(ctx); err != nil {
			c.log.Warn("curator: initial Hindsight bank setup failed; will retry on events", "err", err)
		}
	}
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

	// Register the NATS request-reply responder for semantic memory queries on
	// SubjectMemoryQuery (_INBOX.agent.memory.query, covered by the agent
	// principal's publish and subscribe grants) so callers can query memory over
	// NATS without coupling to a specific storage backend.
	querySubjects := []string{SubjectMemoryQuery}
	if extra := strings.TrimSpace(os.Getenv("CURATOR_QUERY_SUBJECT")); extra != "" && extra != SubjectMemoryQuery {
		querySubjects = append(querySubjects, extra)
	}
	for _, qSubj := range querySubjects {
		qSub, qErr := c.cfg.Client.SubscribeCore(qSubj, func(msg *nats.Msg) {
			c.handleQueryMsg(context.Background(), msg)
		})
		if qErr != nil {
			c.Stop()
			return fmt.Errorf("curator subscribe %s: %w", qSubj, qErr)
		}
		c.querySubs = append(c.querySubs, qSub)
	}

	// Catch up with the latest entry on TOPICS-JOURNAL if present and not yet processed.
	latestEnv, err := c.cfg.Client.ReadTopicLatest(ctx, lib.StreamTopicsJournal, SubjectAnnotations)
	if err == nil && latestEnv != nil {
		c.handleAnnotationEnvelope(ctx, latestEnv)
	} else if err != nil && !errors.Is(err, lib.ErrTopicEmpty) {
		c.log.Debug("curator: initial journal read skipped", "err", err)
	}

	// Publish initial blueprint state if records exist on disk.
	records, err := LoadRecords(c.cfg.RepoDir)
	if err == nil && len(records) > 0 {
		_ = c.publishBlueprint(ctx, records, "startup-sync", "")
	}
	return nil
}

// Stop unsubscribes from NATS.
func (c *Curator) Stop() {
	if c.sub != nil {
		c.sub.Stop()
		c.sub = nil
	}
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
	if c.seenEnvelopes[env.EnvelopeID] {
		c.mu.Unlock()
		return
	}
	c.seenEnvelopes[env.EnvelopeID] = true
	c.mu.Unlock()

	ev, ok := ExtractOutcomeEvent(env)
	if !ok {
		return
	}
	res, err := c.ProcessOutcome(ctx, ev, env.CorrelationID)
	if err != nil {
		c.log.Error("curator: failed to process outcome event",
			"kind", ev.Kind, "record_id", ev.RecordID, "err", err)
		return
	}
	c.log.Info("curator: processed outcome event",
		"kind", ev.Kind,
		"record_id", res.RecordID,
		"status", res.Status,
		"graduated", res.GraduatedDecisions,
	)
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
	case OutcomeProposed, OutcomeApproved, OutcomeResolvedMerged, OutcomePRMergedPersists, OutcomeRefused:
		return true
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
		targetRec = c.buildProposedRecord(ctx, ev, byID, year, dateStr)
	case OutcomeApproved:
		targetRec = c.buildApprovedRecord(ctx, ev, byID, year, dateStr)
	case OutcomeResolvedMerged:
		targetRec = c.buildResolvedMergedRecord(ctx, ev, byID, year, dateStr)
	case OutcomePRMergedPersists:
		targetRec = c.buildPersistsRecord(ctx, ev, byID, year, dateStr)
	case OutcomeRefused:
		targetRec = c.buildRefusedRecord(ctx, ev, byID, year, dateStr)
	default:
		return OutcomeProcessResult{}, fmt.Errorf("unsupported outcome kind %q", ev.Kind)
	}

	if err := SaveRecord(c.cfg.RepoDir, &targetRec); err != nil {
		return OutcomeProcessResult{}, fmt.Errorf("save record %s: %w", targetRec.Meta.ID, err)
	}
	if c.cfg.Hindsight != nil {
		_ = c.cfg.Hindsight.EnsureBank(ctx)
		if err := c.cfg.Hindsight.RetainRecord(ctx, repoID, targetRec); err != nil {
			return OutcomeProcessResult{}, fmt.Errorf("hindsight retain %s: %w", targetRec.Meta.ID, err)
		}
	}

	// Check for cross-cluster recurrence graduation (>= RecurrenceThreshold clusters).
	var newlyGraduated []string
	records, err = LoadRecords(c.cfg.RepoDir)
	if err != nil {
		return OutcomeProcessResult{}, err
	}
	if ev.Kind == OutcomeResolvedMerged || ev.Kind == OutcomeApproved {
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
	// Invalidate cached query responses whenever an outcome updates memory state.
	c.queryCache = map[string]MemoryQueryResponse{}
	if c.cfg.Client != nil {
		if err := c.publishBlueprintDigest(ctx, digest, correlationID); err != nil {
			return OutcomeProcessResult{}, fmt.Errorf("publish blueprint: %w", err)
		}
	}

	return OutcomeProcessResult{
		RecordID:           targetRec.Meta.ID,
		Status:             targetRec.Meta.Status,
		Path:               targetRec.Path,
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
	existing := byID[rid]

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
		Date:   dateStr,
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest: prRef,
			BreakingPR:  firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:    firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:      firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:   firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
		},
		Supersedes: ev.Supersedes,
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
	synth := c.cfg.LLM.Synthesize(ctx, rtype, ev)
	title := firstNonEmpty(ev.Title, existing.Meta.Title, synthTitle(synth), "Approved decision "+rid)
	scope := mergeScope(existing.Meta.Scope, ev)
	guard := mergeGuard(c.cfg.RepoDir, existing.Meta.Guard, ev, synth, scope.Resources)
	if guard != nil && guard.Severity == "" {
		guard.Severity = "block"
	}
	prRef := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest)
	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: StatusActive,
		Date:   firstNonEmpty(existing.Meta.Date, dateStr),
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest: prRef,
			BreakingPR:  firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:    firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:      firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:   firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
		},
		Supersedes: firstSlice(ev.Supersedes, existing.Meta.Supersedes),
	}
	body := existing.Body
	if !hasExisting || strings.TrimSpace(body) == "" {
		body = buildRecordBody(rtype, ev, synth, "", StatusActive, "")
	} else if ev.Summary != "" {
		body = strings.TrimSpace(body) + "\n\n## Approval\n\n" + strings.TrimSpace(ev.Summary)
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
	prRef := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest, "#0")

	meta := RecordMeta{
		Type:   rtype,
		ID:     rid,
		Title:  title,
		Status: StatusActive,
		Date:   dateStr,
		Scope:  scope,
		Guard:  guard,
		Evidence: RecordEvidence{
			PullRequest: prRef,
			BreakingPR:  firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:    firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:      firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:   firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
		},
		Supersedes: firstSlice(ev.Supersedes, existing.Meta.Supersedes),
	}
	body := buildRecordBody(rtype, ev, synth, existing.Body, StatusActive, "")
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
	prRef := firstNonEmpty(formatPRRef(ev), existing.Meta.Evidence.PullRequest, "#0")
	reason := firstNonEmpty(ev.ReviewerReason, ev.Verification, ev.Summary, "Finding persisted after the pull request merged.")

	meta := RecordMeta{
		Type:   RecordTypeLesson,
		ID:     rid,
		Title:  title,
		Status: StatusActive,
		Date:   dateStr,
		Scope:  scope,
		Evidence: RecordEvidence{
			PullRequest:       prRef,
			BreakingPR:        firstNonEmpty(ev.BreakingPR, existing.Meta.Evidence.BreakingPR),
			Incident:          firstNonEmpty(ev.Incident, existing.Meta.Evidence.Incident),
			Commit:            firstNonEmpty(ev.Commit, existing.Meta.Evidence.Commit),
			FindingID:         firstNonEmpty(ev.FindingID, existing.Meta.Evidence.FindingID),
			ReviewerReason:    reason,
			RejectedProposals: appendUnique(existing.Meta.Evidence.RejectedProposals, prRef),
		},
	}
	body := buildRecordBody(RecordTypeLesson, ev, synth, existing.Body, StatusActive, reason)
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
	if hasExisting && (existing.Meta.Status == StatusProposed || existing.Meta.Type == RecordTypeDecision) {
		existing.Meta.Status = StatusRejected
		existing.Meta.Evidence.ReviewerReason = reason
		if prRef != "" {
			existing.Meta.Evidence.RejectedProposals = appendUnique(existing.Meta.Evidence.RejectedProposals, prRef)
		}
		existing.Body = strings.TrimSpace(existing.Body) + "\n\n## Rejection Rationale\n\n" + reason
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
	if rtype == RecordTypeLesson && ev.RecordID == "" {
		// Standalone refused troubleshooting PR recorded as a negative lesson.
		status = StatusRejected
	}
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
			ReviewerReason:    reason,
			RejectedProposals: appendUnique(nil, prRef),
		},
	}
	body := buildRecordBody(rtype, ev, synth, "", status, reason)
	return Record{
		Path: existing.Path,
		Meta: meta,
		Body: body,
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
			guard = &RecordGuard{
				Rule:     ruleText,
				Severity: "block",
				Expect: &GuardExpect{
					File:  fileGlob,
					Path:  cand.YAMLPath,
					Op:    cand.Op,
					Value: cand.Value,
				},
			}
		}

		var checks []string
		if cand.CheckID != "" {
			checks = []string{cand.CheckID}
		}
		decMeta := RecordMeta{
			Type:   RecordTypeDecision,
			ID:     decID,
			Title:  title,
			Status: StatusActive,
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

		// Transition each superseded cluster lesson in-place to status:superseded.
		for _, lid := range cand.Lessons {
			les, ok := byID[lid]
			if !ok {
				continue
			}
			les.Meta.Status = StatusSuperseded
			les.Meta.SupersededBy = decID
			if err := SaveRecord(c.cfg.RepoDir, &les); err != nil {
				return graduated, err
			}
			if c.cfg.Hindsight != nil {
				if err := c.cfg.Hindsight.RetainRecord(ctx, repoID, les); err != nil {
					return graduated, err
				}
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
		sev := "block"
		if ev.Kind == OutcomeProposed {
			sev = "warn"
		}
		return &RecordGuard{
			Rule:     ev.Rule,
			Severity: sev,
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

func appendUnique(list []string, item string) []string {
	if item == "" {
		return list
	}
	for _, x := range list {
		if x == item {
			return list
		}
	}
	return append(list, item)
}

func (c *Curator) handleQueryMsg(ctx context.Context, msg *nats.Msg) {
	if msg == nil || msg.Reply == "" {
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
	qCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
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
	if c.cfg.Hindsight != nil && strings.TrimSpace(req.Query) != "" {
		backend = "hindsight"
		results, hsErr = c.cfg.Hindsight.Recall(ctx, req.Query, tags, tagsMatch)
	}

	c.mu.Lock()
	records, _ := LoadRecords(c.cfg.RepoDir)
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
		scored = append(scored, scoredMatch{
			score: score,
			digest: BlueprintRecordDigest{
				ID:             r.Meta.ID,
				Type:           r.Meta.Type,
				Status:         r.Meta.Status,
				Title:          r.Meta.Title,
				Path:           r.Path,
				Clusters:       r.Meta.Scope.Clusters,
				Checks:         r.Meta.Scope.Checks,
				Paths:          r.Meta.Scope.Paths,
				GuardRule:      guardRule,
				PullRequest:    r.Meta.Evidence.PullRequest,
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
		maxRecs = 8
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
		if c.cfg.Hindsight == nil {
			backend = "okf-local"
		}
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

// QueryViaNATS sends a MemoryQueryRequest over NATS to the Curator's memory
// responder on SubjectMemoryQuery and returns the decoded MemoryQueryResponse.
func QueryViaNATS(ctx context.Context, client *lib.Client, req MemoryQueryRequest) (MemoryQueryResponse, error) {
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
	msg, err := nc.RequestWithContext(ctx, SubjectMemoryQuery, rawReq)
	if err != nil {
		return MemoryQueryResponse{}, fmt.Errorf("nats memory query on %s: %w", SubjectMemoryQuery, err)
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

func recordMatchesQueryText(rendered, qLower string) bool {
	return scoreRecordQueryMatch(Record{}, rendered, qLower) > 0
}
