// Package memory implements the Operational Knowledge Format (OKF) Curator and
// Dreamer for kube-agents: NATS JetStream outcome ingestion on
// a2a.topics.shared.annotations, deterministic YAML guard evaluation, cross-cluster
// recurrence graduation, single-shot LiteLLM synthesis, Hindsight bank indexing
// (kube-agents-decisions), and blueprint digest publishing on
// a2a.topics.shared.blueprint.
package memory

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"sigs.k8s.io/yaml"
)

const (
	// OutcomeSchema is the DataPart schema published to a2a.topics.shared.annotations.
	OutcomeSchema = "kube-agents.memory.outcome/v1"

	// BlueprintSchema is the DataPart schema published to a2a.topics.shared.blueprint.
	BlueprintSchema = "kube-agents.memory.blueprint/v1"

	// DefaultBankID is the Hindsight bank for operational knowledge records.
	DefaultBankID = "kube-agents-decisions"

	// SharedTag marks organizational records in Hindsight.
	SharedTag = "scope:shared"

	// DocPrefix prefixes every OKF document_id in Hindsight.
	DocPrefix = "okf:"

	// RecordStrategy is the Hindsight retain strategy name for verbatim OKF chunks.
	RecordStrategy = "record"

	// SubjectAnnotations is the journal-class topic for learning outcomes.
	SubjectAnnotations = "a2a.topics.shared.annotations"

	// SubjectBlueprint is the state-class topic for the active knowledge digest.
	SubjectBlueprint = "a2a.topics.shared.blueprint"

	// MemoryQuerySchema is the request payload schema for NATS memory queries.
	MemoryQuerySchema = "kube-agents.memory.query/v1"

	// MemoryQueryReplySchema is the reply payload schema for NATS memory queries.
	MemoryQueryReplySchema = "kube-agents.memory.query.reply/v1"

	// SubjectMemoryQuery is the NATS request-reply subject within the agent
	// principal's granted _INBOX.agent.> namespace used to query semantic memory
	// over NATS without coupling callers to a specific storage backend.
	SubjectMemoryQuery = "_INBOX.agent.memory.query"

	// SubjectMemoryQueryPublic is the top-level NATS request-reply subject for
	// semantic memory queries when bus policy grants a2a.memory.query.
	SubjectMemoryQueryPublic = "a2a.memory.query"

	// SubjectCuratorOutcomes is the dedicated NATS outcome subject within the
	// agent principal's reserved _INBOX.agent.> namespace. Session pods and
	// AgentProfile worker pods are refused _INBOX.agent.> by NATS auth callout,
	// preventing worker pods from forging approved, resolved-merged,
	// modified-merged, or refused outcome transitions.
	SubjectCuratorOutcomes = "_INBOX.agent.curator.outcomes"

	// SubjectCuratorOutcomesPublic is an optional top-level Curator outcome
	// subject when bus policy explicitly grants a2a.curator.outcomes.
	SubjectCuratorOutcomesPublic = "a2a.curator.outcomes"
)

// OutcomeKind classifies a learning event processed by the Curator.
type OutcomeKind string

const (
	OutcomeProposed         OutcomeKind = "proposed"
	OutcomeApproved         OutcomeKind = "approved"
	OutcomeModifiedMerged   OutcomeKind = "modified-merged"
	OutcomeResolvedMerged   OutcomeKind = "resolved-merged"
	OutcomePRMergedPersists OutcomeKind = "pr-merged-persists"
	OutcomeRefused          OutcomeKind = "refused"
	OutcomeRetired          OutcomeKind = "retired"
)

// RecordType classifies an OKF record ("decision", "lesson", "runbook", "cluster-blueprint", "tenancy-model", "metric-definition", "escalation", or "observation").
type RecordType string

const (
	RecordTypeDecision         RecordType = "decision"
	RecordTypeLesson           RecordType = "lesson"
	RecordTypeRunbook          RecordType = "runbook"
	RecordTypeClusterBlueprint RecordType = "cluster-blueprint"
	RecordTypeTenancyModel     RecordType = "tenancy-model"
	RecordTypeMetricDefinition RecordType = "metric-definition"
	RecordTypeEscalation       RecordType = "escalation"
	RecordTypeObservation      RecordType = "observation"
)

// RecordStatus tracks the lifecycle of an OKF record.
type RecordStatus string

const (
	StatusProposed   RecordStatus = "proposed"
	StatusActive     RecordStatus = "active"
	StatusRejected   RecordStatus = "rejected"
	StatusSuperseded RecordStatus = "superseded"
	StatusRetired    RecordStatus = "retired"
)

// OutcomeEvent is the structured payload carried inside the DataPart of an
// a2a.topics.shared.annotations topic-update artifact.
type OutcomeEvent struct {
	Schema         string       `json:"schema,omitempty"`
	Kind           OutcomeKind  `json:"kind"`
	RecordID       string       `json:"record_id,omitempty"`
	RecordType     RecordType   `json:"record_type,omitempty"`
	Repo           string       `json:"repo,omitempty"`
	Cluster        string       `json:"cluster,omitempty"`
	CheckID        string       `json:"check_id,omitempty"`
	FindingID      string       `json:"finding_id,omitempty"`
	Title          string       `json:"title,omitempty"`
	Summary        string       `json:"summary,omitempty"`
	RootCause      string       `json:"root_cause,omitempty"`
	Fix            string       `json:"fix,omitempty"`
	Verification   string       `json:"verification,omitempty"`
	Prevention     string       `json:"prevention,omitempty"`
	Rule           string       `json:"rule,omitempty"`
	PullRequest     string       `json:"pull_request,omitempty"`
	RemediationPR   string       `json:"remediation_pr,omitempty"`
	KnowledgePR     bool         `json:"knowledge_pr,omitempty"`
	PRNumber        int          `json:"pr_number,omitempty"`
	BreakingPR      string       `json:"breaking_pr,omitempty"`
	Incident        string       `json:"incident,omitempty"`
	Commit          string       `json:"commit,omitempty"`
	ReviewerReason  string       `json:"reviewer_reason,omitempty"`
	InitialProposal string       `json:"initial_proposal,omitempty"`
	MergedAt        string       `json:"merged_at,omitempty"`
	SoakWindow      string       `json:"soak_window,omitempty"`
	Date            string       `json:"date,omitempty"`
	Scope           *RecordScope `json:"scope,omitempty"`
	Guard           *RecordGuard `json:"guard,omitempty"`
	Supersedes      []string     `json:"supersedes,omitempty"`
}

// RecordScope defines which GitOps paths, Kubernetes resources, clusters, and
// checks a record governs.
type RecordScope struct {
	Paths     []string `json:"paths,omitempty" yaml:"paths,omitempty"`
	Resources []string `json:"resources,omitempty" yaml:"resources,omitempty"`
	Clusters  []string `json:"clusters,omitempty" yaml:"clusters,omitempty"`
	Checks    []string `json:"checks,omitempty" yaml:"checks,omitempty"`
}

// GuardExpect is a machine-checkable assertion on a file or YAML AST path.
type GuardExpect struct {
	File  string `json:"file" yaml:"file"`
	Path  string `json:"path" yaml:"path"`
	Op    string `json:"op" yaml:"op"`
	Value any    `json:"value" yaml:"value"`
}

// RecordGuard holds the human-readable rule and optional machine assertion.
type RecordGuard struct {
	Rule     string       `json:"rule,omitempty" yaml:"rule,omitempty"`
	Severity string       `json:"severity,omitempty" yaml:"severity,omitempty"`
	Expect   *GuardExpect `json:"expect,omitempty" yaml:"expect,omitempty"`
}

// RecordEvidence links a record to pull requests, commits, and incidents.
type RecordEvidence struct {
	PullRequest       string   `json:"pull_request,omitempty" yaml:"pull_request,omitempty"`
	RemediationPR     string   `json:"remediation_pr,omitempty" yaml:"remediation_pr,omitempty"`
	BreakingPR        string   `json:"breaking_pr,omitempty" yaml:"breaking_pr,omitempty"`
	Incident          string   `json:"incident,omitempty" yaml:"incident,omitempty"`
	Commit            string   `json:"commit,omitempty" yaml:"commit,omitempty"`
	MergedBy          string   `json:"merged_by,omitempty" yaml:"merged_by,omitempty"`
	FindingID         string   `json:"finding_id,omitempty" yaml:"finding_id,omitempty"`
	Outcome           string   `json:"outcome,omitempty" yaml:"outcome,omitempty"`
	SoakWindow        string   `json:"soak_window,omitempty" yaml:"soak_window,omitempty"`
	ReviewerReason    string   `json:"reviewer_reason,omitempty" yaml:"reviewer_reason,omitempty"`
	RejectedProposals []string `json:"rejected_proposals,omitempty" yaml:"rejected_proposals,omitempty"`
}

// DeclareItem represents one entry in an OKF record's unified `declares:` list (06 §5).
// It combines fleet-audit declared posture (Check, Namespace, Object, Cluster) with optional
// manifest assertion fields (File, Path, Op, Value, Severity, Rule).
type DeclareItem struct {
	Check     string `json:"check,omitempty" yaml:"check,omitempty"`
	Namespace string `json:"namespace,omitempty" yaml:"namespace,omitempty"`
	Object    string `json:"object,omitempty" yaml:"object,omitempty"`
	Cluster   string `json:"cluster,omitempty" yaml:"cluster,omitempty"`
	File      string `json:"file,omitempty" yaml:"file,omitempty"`
	Path      string `json:"path,omitempty" yaml:"path,omitempty"`
	Op        string `json:"op,omitempty" yaml:"op,omitempty"`
	Value     any    `json:"value,omitempty" yaml:"value,omitempty"`
	Severity  string `json:"severity,omitempty" yaml:"severity,omitempty"`
	Rule      string `json:"rule,omitempty" yaml:"rule,omitempty"`
}

// RecordMeta is the YAML frontmatter of an OKF Markdown file.
type RecordMeta struct {
	Type         RecordType     `json:"type" yaml:"type"`
	ID           string         `json:"id" yaml:"id"`
	Title        string         `json:"title" yaml:"title"`
	Status       RecordStatus   `json:"status" yaml:"status"`
	Date         string         `json:"date,omitempty" yaml:"date,omitempty"`
	Declares     []DeclareItem  `json:"declares,omitempty" yaml:"declares,omitempty"`
	Scope        RecordScope    `json:"scope,omitempty" yaml:"scope,omitempty"`
	Guard        *RecordGuard   `json:"guard,omitempty" yaml:"guard,omitempty"`
	Evidence     RecordEvidence `json:"evidence,omitempty" yaml:"evidence,omitempty"`
	Supersedes   []string       `json:"supersedes,omitempty" yaml:"supersedes,omitempty"`
	SupersededBy string         `json:"superseded_by,omitempty" yaml:"superseded_by,omitempty"`
}

// Record is a parsed OKF decision or lesson file.
type Record struct {
	Path string     `json:"path"`
	Meta RecordMeta `json:"meta"`
	Body string     `json:"body"`
}

// BlueprintCounts summarizes record counts by status.
type BlueprintCounts struct {
	Active     int `json:"active"`
	Proposed   int `json:"proposed"`
	Rejected   int `json:"rejected"`
	Superseded int `json:"superseded"`
	Retired    int `json:"retired"`
}

// BlueprintRecordDigest is a compact entry in the blueprint state digest.
type BlueprintRecordDigest struct {
	ID             string       `json:"id"`
	Type           RecordType   `json:"type"`
	Status         RecordStatus `json:"status"`
	Title          string       `json:"title"`
	Path           string       `json:"path"`
	Branch         string       `json:"branch,omitempty"`
	PRMarkdown     string       `json:"pr_markdown,omitempty"`
	Clusters       []string     `json:"clusters,omitempty"`
	Checks         []string     `json:"checks,omitempty"`
	Paths          []string     `json:"paths,omitempty"`
	GuardRule      string       `json:"guard_rule,omitempty"`
	PullRequest    string       `json:"pull_request,omitempty"`
	RemediationPR  string       `json:"remediation_pr,omitempty"`
	FindingID      string       `json:"finding_id,omitempty"`
	Outcome        string       `json:"outcome,omitempty"`
	SoakWindow     string       `json:"soak_window,omitempty"`
	ReviewerReason string       `json:"reviewer_reason,omitempty"`
	Supersedes     []string     `json:"supersedes,omitempty"`
	SupersededBy   string       `json:"superseded_by,omitempty"`
}

// BlueprintDigest is the structured payload published to a2a.topics.shared.blueprint.
type BlueprintDigest struct {
	Schema             string                  `json:"schema"`
	Repo               string                  `json:"repo"`
	UpdatedAt          time.Time               `json:"updated_at"`
	LastOutcomeEvent   string                  `json:"last_outcome_event,omitempty"`
	Counts             BlueprintCounts         `json:"counts"`
	ActiveIDs          []string                `json:"active_ids"`
	ProposedIDs        []string                `json:"proposed_ids"`
	RejectedIDs        []string                `json:"rejected_ids"`
	SupersededIDs      []string                `json:"superseded_ids"`
	GraduatedDecisions []string                `json:"graduated_decisions,omitempty"`
	Records            []BlueprintRecordDigest `json:"records"`
}

// MemoryQueryRequest is the NATS request payload sent to SubjectMemoryQuery
// (or SubjectMemoryQueryPublic) to query or update the semantic memory layer over NATS.
type MemoryQueryRequest struct {
	Schema     string        `json:"schema,omitempty"`
	Action     string        `json:"action,omitempty"`
	Query      string        `json:"query"`
	Bank       string        `json:"bank,omitempty"`
	Tags       []string      `json:"tags,omitempty"`
	TagsMatch  string        `json:"tags_match,omitempty"`
	Path       string        `json:"path,omitempty"`
	Cluster    string        `json:"cluster,omitempty"`
	MaxResults int           `json:"max_results,omitempty"`
	Outcome    *OutcomeEvent `json:"outcome,omitempty"`
}

// MemoryQueryResponse is the NATS reply payload returned by the Curator's
// memory query responder.
type MemoryQueryResponse struct {
	Schema  string                  `json:"schema"`
	Backend string                  `json:"backend"`
	Bank    string                  `json:"bank"`
	Query   string                  `json:"query"`
	Tags    []string                `json:"tags,omitempty"`
	Cached  bool                    `json:"cached"`
	Results []HindsightRecallResult `json:"results"`
	Records []BlueprintRecordDigest `json:"records,omitempty"`
	Outcome *OutcomeProcessResult   `json:"outcome,omitempty"`
	Error   string                  `json:"error,omitempty"`
}

var (
	reRecordID      = regexp.MustCompile(`^(DEC|LES)-\d{4}-\d{3,}$`)
	reExternalDocID = regexp.MustCompile(`(?i)\b(DEC-\d{4}-\d{3,}|LES-\d{4}-\d{3,}|ADR-\d{1,5}|RFC-\d{1,5}|RUN-\d{1,5}|BP-\d{1,5}|TEN-\d{1,5}|MET-\d{1,5}|ESC-\d{1,5}|OBS-\d{1,5})\b`)
	reLeadingNum    = regexp.MustCompile(`^0*(\d{1,5})(?:[-_.\s]+(.*))?$`)
	reSlugNonAln    = regexp.MustCompile(`[^a-z0-9]+`)
	externalDocDirs = []string{
		"knowledge/runbooks",
		"knowledge/cluster-blueprints",
		"knowledge/blueprints",
		"knowledge/tenancy-models",
		"knowledge/tenancy",
		"knowledge/metric-definitions",
		"knowledge/metrics",
		"knowledge/escalations",
		"knowledge/observations",
		"docs/adr",
		"docs/adrs",
		"docs/rfcs",
		"docs/runbooks",
		"docs/playbooks",
		"adr",
		"rfcs",
		"runbooks",
		"playbooks",
	}
)

// Slug formats a title into a URL-safe filename suffix (up to 60 characters).
func Slug(s string) string {
	cleaned := strings.Trim(reSlugNonAln.ReplaceAllString(strings.ToLower(s), "-"), "-")
	if len(cleaned) > 60 {
		cleaned = strings.TrimRight(cleaned[:60], "-")
	}
	if cleaned == "" {
		return "record"
	}
	return cleaned
}

// DocumentID returns the canonical Hindsight document_id for a record.
// Keying by record ID ("okf:<repo>:<id>") ensures in-place status transitions
// (proposed -> active, proposed -> rejected, active -> superseded) overwrite
// the exact same Hindsight document via update_mode: replace.
func (r Record) DocumentID(repoID string) string {
	key := r.Meta.ID
	if key == "" {
		key = r.Path
	}
	if repoID == "" {
		return fmt.Sprintf("%s%s", DocPrefix, key)
	}
	return fmt.Sprintf("%s%s:%s", DocPrefix, repoID, key)
}

// Tags returns the sorted Hindsight tag list for this record.
func (r Record) Tags() []string {
	seen := map[string]struct{}{
		SharedTag:                         {},
		"kind:" + string(r.Meta.Type):     {},
		"status:" + string(r.Meta.Status): {},
	}
	if r.Meta.ID != "" {
		seen["id:"+r.Meta.ID] = struct{}{}
	}
	for _, c := range r.Meta.Scope.Clusters {
		if c != "" {
			seen["cluster:"+c] = struct{}{}
		}
	}
	for _, ch := range r.Meta.Scope.Checks {
		if ch != "" {
			seen["check:"+ch] = struct{}{}
		}
	}
	for _, res := range r.Meta.Scope.Resources {
		if res != "" {
			seen["res:"+res] = struct{}{}
		}
	}
	if r.Meta.Guard != nil && r.Meta.Guard.Severity != "" {
		seen["guard:"+r.Meta.Guard.Severity] = struct{}{}
	}
	if r.Meta.Evidence.PullRequest != "" {
		seen["pr:"+r.Meta.Evidence.PullRequest] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for t := range seen {
		out = append(out, t)
	}
	sort.Strings(out)
	return out
}

// RenderForIndex renders the self-describing text chunk stored in Hindsight.
func (r Record) RenderForIndex() string {
	var lines []string
	lines = append(lines, fmt.Sprintf("%s (%s, %s, %s): %s",
		r.Meta.ID, r.Meta.Type, r.Meta.Status, r.Meta.Date, r.Meta.Title))

	var governs []string
	governs = append(governs, r.Meta.Scope.Resources...)
	governs = append(governs, r.Meta.Scope.Paths...)
	if len(governs) > 0 {
		lines = append(lines, "Governs: "+strings.Join(governs, ", "))
	}
	if len(r.Meta.Scope.Clusters) > 0 {
		lines = append(lines, "Clusters: "+strings.Join(r.Meta.Scope.Clusters, ", "))
	}
	if r.Meta.Guard != nil && r.Meta.Guard.Rule != "" {
		sev := r.Meta.Guard.Severity
		if sev == "" {
			sev = "warn"
		}
		lines = append(lines, fmt.Sprintf("Guard (%s): %s", sev, r.Meta.Guard.Rule))
	}
	if r.Meta.Evidence.PullRequest != "" {
		lines = append(lines, "Pull request: "+r.Meta.Evidence.PullRequest)
	}
	if len(r.Meta.Evidence.RejectedProposals) > 0 {
		lines = append(lines, "Rejected proposals: "+strings.Join(r.Meta.Evidence.RejectedProposals, ", "))
	}
	if r.Meta.Evidence.ReviewerReason != "" {
		lines = append(lines, "Reviewer reason: "+r.Meta.Evidence.ReviewerReason)
	}
	if len(r.Meta.Supersedes) > 0 {
		lines = append(lines, "Supersedes: "+strings.Join(r.Meta.Supersedes, ", "))
	}
	if r.Meta.SupersededBy != "" {
		lines = append(lines, "Superseded by: "+r.Meta.SupersededBy)
	}
	lines = append(lines, "", strings.TrimSpace(r.Body))
	return strings.Join(lines, "\n")
}

// ContentSHA256 returns the hex SHA-256 digest of RenderForIndex().
func (r Record) ContentSHA256() string {
	sum := sha256.Sum256([]byte(r.RenderForIndex()))
	return hex.EncodeToString(sum[:])
}

// ParseRecord parses an OKF Markdown document with YAML frontmatter.
func ParseRecord(text, relPath string) (Record, error) {
	trimmed := strings.TrimPrefix(text, "\ufeff")
	if !strings.HasPrefix(trimmed, "---\n") && !strings.HasPrefix(trimmed, "---\r\n") {
		return Record{}, fmt.Errorf("%s: missing YAML frontmatter delimiter", relPath)
	}
	rest := trimmed[4:]
	idx := strings.Index(rest, "\n---")
	if idx < 0 {
		return Record{}, fmt.Errorf("%s: unclosed YAML frontmatter delimiter", relPath)
	}
	fmRaw := rest[:idx]
	body := rest[idx+4:]
	body = strings.TrimLeft(body, "\r\n")

	var meta RecordMeta
	if err := yaml.Unmarshal([]byte(fmRaw), &meta); err != nil {
		return Record{}, fmt.Errorf("%s: invalid YAML frontmatter: %w", relPath, err)
	}
	if meta.Status == "" {
		meta.Status = StatusActive
	}
	normalizeDeclaresIntoMeta(&meta)
	return Record{
		Path: relPath,
		Meta: meta,
		Body: strings.TrimSpace(body),
	}, nil
}

func appendUniqueStr(slice []string, val string) []string {
	val = strings.TrimSpace(val)
	if val == "" {
		return slice
	}
	for _, s := range slice {
		if s == val {
			return slice
		}
	}
	return append(slice, val)
}

func normalizeDeclaresIntoMeta(meta *RecordMeta) {
	if meta == nil || len(meta.Declares) == 0 {
		return
	}
	for _, d := range meta.Declares {
		if d.Check != "" {
			meta.Scope.Checks = appendUniqueStr(meta.Scope.Checks, d.Check)
		}
		if d.Cluster != "" {
			meta.Scope.Clusters = appendUniqueStr(meta.Scope.Clusters, d.Cluster)
		}
		if d.File != "" {
			meta.Scope.Paths = appendUniqueStr(meta.Scope.Paths, d.File)
		}
		if d.Object != "" {
			res := strings.TrimSpace(d.Object)
			if strings.Count(res, "/") == 1 && d.Namespace != "" {
				parts := strings.SplitN(res, "/", 2)
				res = fmt.Sprintf("%s/%s/%s", parts[0], strings.TrimSpace(d.Namespace), parts[1])
			}
			meta.Scope.Resources = appendUniqueStr(meta.Scope.Resources, res)
		}
		if d.File != "" && d.Path != "" && d.Op != "" && meta.Guard == nil {
			sev := strings.TrimSpace(d.Severity)
			if sev == "" {
				sev = "block"
			}
			meta.Guard = &RecordGuard{
				Rule:     strings.TrimSpace(d.Rule),
				Severity: sev,
				Expect: &GuardExpect{
					File:  strings.TrimSpace(d.File),
					Path:  strings.TrimSpace(d.Path),
					Op:    strings.TrimSpace(d.Op),
					Value: d.Value,
				},
			}
		}
	}
}

// RenderRecord serializes a Record back to Markdown with YAML frontmatter.
func RenderRecord(meta RecordMeta, body string) (string, error) {
	raw, err := yaml.Marshal(meta)
	if err != nil {
		return "", fmt.Errorf("marshal frontmatter: %w", err)
	}
	var buf bytes.Buffer
	buf.WriteString("---\n")
	buf.Write(raw)
	buf.WriteString("---\n\n")
	buf.WriteString(strings.TrimSpace(body))
	buf.WriteString("\n")
	return buf.String(), nil
}

// ProposalBranch returns the canonical Git branch name used by the platform
// agent when opening a pull request for this record.
func (r Record) ProposalBranch() string {
	if r.Meta.ID == "" {
		return ""
	}
	return "platform-agent/memory/" + r.Meta.ID
}

// RenderForPR renders the Markdown document to commit on a proposal pull request
// branch. In Git, a proposed record is committed with status: active and preserves
// its classified guard severity ("warn" by default, or "block" for verified crash
// invariants) so merging the pull request activates the record on the default
// branch without requiring a second post-merge commit.
func (r Record) RenderForPR() string {
	meta := r.Meta
	meta.Status = StatusActive
	if r.Meta.Guard != nil {
		g := *r.Meta.Guard
		if g.Severity == "" {
			g.Severity = "warn"
		}
		meta.Guard = &g
	}
	rendered, err := RenderRecord(meta, r.Body)
	if err != nil {
		return ""
	}
	return rendered
}

// ValidateRecord checks structural validity of an OKF record.
func ValidateRecord(r Record) []string {
	var errs []string
	if r.Meta.Type != RecordTypeDecision && r.Meta.Type != RecordTypeLesson {
		errs = append(errs, fmt.Sprintf("type must be decision or lesson, got %q", r.Meta.Type))
	}
	if !reRecordID.MatchString(r.Meta.ID) {
		errs = append(errs, fmt.Sprintf("id %q must match (DEC|LES)-YYYY-NNN", r.Meta.ID))
	}
	switch r.Meta.Status {
	case StatusProposed, StatusActive, StatusRejected, StatusSuperseded, StatusRetired:
	default:
		errs = append(errs, fmt.Sprintf("invalid status %q", r.Meta.Status))
	}
	if strings.TrimSpace(r.Meta.Title) == "" {
		errs = append(errs, "title is required")
	}
	if len(r.Meta.Scope.Paths) == 0 && len(r.Meta.Scope.Resources) == 0 && len(r.Meta.Scope.Checks) == 0 {
		errs = append(errs, "scope requires at least one of paths, resources, or checks")
	}
	if r.Meta.Guard != nil {
		if sev := r.Meta.Guard.Severity; sev != "" && sev != "block" && sev != "warn" && sev != "info" {
			errs = append(errs, fmt.Sprintf("invalid guard.severity %q", sev))
		}
		if exp := r.Meta.Guard.Expect; exp != nil {
			if exp.File == "" || exp.Path == "" || exp.Op == "" {
				errs = append(errs, "guard.expect requires file, path, and op")
			} else if !isValidOp(exp.Op) {
				errs = append(errs, fmt.Sprintf("invalid guard.expect.op %q", exp.Op))
			} else if exp.Path != "$text" {
				if _, err := parsePathTokens(exp.Path); err != nil {
					errs = append(errs, fmt.Sprintf("invalid guard.expect.path: %v", err))
				}
			}
		}
	}
	if r.Meta.Type == RecordTypeLesson && r.Meta.Status == StatusActive && r.Meta.Evidence.PullRequest == "" {
		errs = append(errs, "an active lesson requires evidence.pull_request")
	}
	if r.Meta.Status == StatusSuperseded && r.Meta.SupersededBy == "" {
		errs = append(errs, "a superseded record requires superseded_by")
	}
	return errs
}

// intentRoots returns the OKF knowledge roots configured in .kube-agents/intent.yaml (defaulting to ["knowledge"]).
func intentRoots(repoDir string) []string {
	roots := []string{"knowledge"}
	intentPath := filepath.Join(repoDir, ".kube-agents", "intent.yaml")
	raw, err := os.ReadFile(intentPath)
	if err != nil {
		return roots
	}
	var parsed struct {
		Paths []string `yaml:"paths"`
	}
	if yaml.Unmarshal(raw, &parsed) != nil {
		return roots
	}
	seen := map[string]bool{"knowledge": true}
	for _, p := range parsed.Paths {
		clean := strings.Trim(filepath.ToSlash(strings.TrimSpace(p)), "/")
		if clean != "" && !seen[clean] {
			seen[clean] = true
			roots = append(roots, clean)
		}
	}
	return roots
}

// LoadRecords reads all OKF decision and lesson records from knowledge/ (and any paths in .kube-agents/intent.yaml) under repoDir.
func LoadRecords(repoDir string) ([]Record, error) {
	var out []Record
	seenPaths := map[string]bool{}
	var subDirs []string
	for _, root := range intentRoots(repoDir) {
		subDirs = append(subDirs, root)
		for _, leaf := range []string{"decisions", "lessons"} {
			subDirs = append(subDirs, filepath.ToSlash(filepath.Join(root, leaf)))
		}
	}
	for _, sub := range subDirs {
		dir := filepath.Join(repoDir, filepath.FromSlash(sub))
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		for _, e := range entries {
			if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
				continue
			}
			lower := strings.ToLower(e.Name())
			if lower == "index.md" || lower == "readme.md" || lower == "state_of_being.md" {
				continue
			}
			rel := filepath.ToSlash(filepath.Join(sub, e.Name()))
			if seenPaths[rel] {
				continue
			}
			seenPaths[rel] = true
			full := filepath.Join(dir, e.Name())
			raw, err := os.ReadFile(full)
			if err != nil {
				return nil, err
			}
			rec, err := ParseRecord(string(raw), rel)
			if err != nil {
				continue
			}
			out = append(out, rec)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Meta.ID < out[j].Meta.ID })
	return out, nil
}

func inferExternalDocTypeAndPrefix(fm map[string]any, relPath string) (RecordType, string) {
	if fm != nil {
		if tStr, ok := fm["type"].(string); ok {
			switch strings.ToLower(strings.TrimSpace(tStr)) {
			case "decision", "adr", "rfc":
				if strings.Contains(strings.ToLower(relPath), "rfc") || strings.EqualFold(strings.TrimSpace(tStr), "rfc") {
					return RecordTypeDecision, "RFC"
				}
				return RecordTypeDecision, "ADR"
			case "lesson":
				return RecordTypeLesson, "LES"
			case "runbook", "playbook":
				return RecordTypeRunbook, "RUN"
			case "cluster-blueprint", "blueprint":
				return RecordTypeClusterBlueprint, "BP"
			case "tenancy-model", "tenancy":
				return RecordTypeTenancyModel, "TEN"
			case "metric-definition", "metric":
				return RecordTypeMetricDefinition, "MET"
			case "escalation":
				return RecordTypeEscalation, "ESC"
			case "observation":
				return RecordTypeObservation, "OBS"
			}
		}
	}
	lower := strings.ToLower(filepath.ToSlash(relPath))
	switch {
	case strings.Contains(lower, "runbook") || strings.Contains(lower, "playbook"):
		return RecordTypeRunbook, "RUN"
	case strings.Contains(lower, "blueprint"):
		return RecordTypeClusterBlueprint, "BP"
	case strings.Contains(lower, "tenancy"):
		return RecordTypeTenancyModel, "TEN"
	case strings.Contains(lower, "metric"):
		return RecordTypeMetricDefinition, "MET"
	case strings.Contains(lower, "escalation"):
		return RecordTypeEscalation, "ESC"
	case strings.Contains(lower, "observation"):
		return RecordTypeObservation, "OBS"
	case strings.Contains(lower, "lesson"):
		return RecordTypeLesson, "LES"
	case strings.Contains(lower, "rfc"):
		return RecordTypeDecision, "RFC"
	default:
		return RecordTypeDecision, "ADR"
	}
}

// ParseExternalDocRecord normalizes a Markdown ADR, RFC, runbook, or OKF knowledge document into a Record.
func ParseExternalDocRecord(text, relPath string) Record {
	if rec, err := ParseRecord(text, relPath); err == nil && len(ValidateRecord(rec)) == 0 {
		return rec
	}
	var fm map[string]any
	body := strings.TrimSpace(text)
	if strings.HasPrefix(text, "---\n") {
		if idx := strings.Index(text[4:], "\n---\n"); idx >= 0 {
			rawFM := text[4 : 4+idx]
			body = strings.TrimSpace(text[4+idx+5:])
			_ = yaml.Unmarshal([]byte(rawFM), &fm)
		}
	}
	stem := strings.TrimSuffix(filepath.Base(relPath), filepath.Ext(relPath))
	rtype, prefix := inferExternalDocTypeAndPrefix(fm, relPath)
	recID := ""
	if fm != nil {
		if idStr, ok := fm["id"].(string); ok {
			recID = strings.TrimSpace(idStr)
		}
	}
	if recID == "" {
		if m := reExternalDocID.FindStringSubmatch(stem); len(m) > 1 {
			recID = strings.ToUpper(m[1])
		} else if nm := reLeadingNum.FindStringSubmatch(stem); len(nm) > 1 {
			if n, err := strconv.Atoi(nm[1]); err == nil {
				recID = fmt.Sprintf("%s-%04d", prefix, n)
			}
		}
	}
	if recID == "" {
		sum := sha256.Sum256([]byte(relPath))
		recID = fmt.Sprintf("%s-%s", prefix, strings.ToUpper(hex.EncodeToString(sum[:])[:6]))
	}

	title := ""
	if fm != nil {
		if tStr, ok := fm["title"].(string); ok {
			title = strings.TrimSpace(tStr)
		}
	}
	if title == "" {
		for _, line := range strings.Split(body, "\n") {
			s := strings.TrimSpace(line)
			if strings.HasPrefix(s, "# ") {
				title = strings.TrimSpace(strings.TrimPrefix(s, "# "))
				break
			}
		}
	}
	if title == "" {
		title = strings.ReplaceAll(strings.ReplaceAll(stem, "-", " "), "_", " ")
	}

	status := StatusActive
	if fm != nil {
		if stStr, ok := fm["status"].(string); ok {
			switch strings.ToLower(strings.TrimSpace(stStr)) {
			case "proposed", "draft":
				status = StatusProposed
			case "rejected":
				status = StatusRejected
			case "superseded", "deprecated", "obsolete":
				status = StatusSuperseded
			case "retired":
				status = StatusRetired
			}
		}
	}
	clusters := []string{"*"}
	paths := []string{"**"}
	var resources []string
	if fm != nil {
		if sc, ok := fm["scope"].(map[string]any); ok {
			if cl, ok := sc["clusters"].([]any); ok && len(cl) > 0 {
				clusters = nil
				for _, v := range cl {
					clusters = append(clusters, fmt.Sprint(v))
				}
			}
			if pl, ok := sc["paths"].([]any); ok && len(pl) > 0 {
				paths = nil
				for _, v := range pl {
					paths = append(paths, fmt.Sprint(v))
				}
			}
			if rl, ok := sc["resources"].([]any); ok && len(rl) > 0 {
				for _, v := range rl {
					resources = append(resources, fmt.Sprint(v))
				}
			}
		}
	}
	created := time.Now().UTC().Format("2006-01-02")
	if fm != nil {
		if cStr, ok := fm["created"].(string); ok && strings.TrimSpace(cStr) != "" {
			created = strings.TrimSpace(cStr)
		} else if dStr, ok := fm["date"].(string); ok && strings.TrimSpace(dStr) != "" {
			created = strings.TrimSpace(dStr)
		}
	}
	return Record{
		Meta: RecordMeta{
			ID:     recID,
			Type:   rtype,
			Status: status,
			Title:  title,
			Date:   created,
			Scope: RecordScope{
				Clusters:  clusters,
				Paths:     paths,
				Resources: resources,
			},
		},
		Body: body,
		Path: filepath.ToSlash(relPath),
	}
}

func configuredExtraRepos(primaryRepo string, extraRepos []string) []string {
	var candidates []string
	candidates = append(candidates, extraRepos...)
	if envExtra := strings.TrimSpace(os.Getenv("DECISION_MEMORY_EXTRA_REPOS")); envExtra != "" {
		for _, item := range strings.Split(envExtra, ",") {
			if s := strings.TrimSpace(item); s != "" {
				candidates = append(candidates, s)
			}
		}
	}
	if rawJSON := strings.TrimSpace(os.Getenv("REPOSITORIES_JSON")); rawJSON != "" {
		var parsed []any
		if err := json.Unmarshal([]byte(rawJSON), &parsed); err == nil {
			for _, item := range parsed {
				switch v := item.(type) {
				case string:
					if strings.TrimSpace(v) != "" {
						candidates = append(candidates, strings.TrimSpace(v))
					}
				case map[string]any:
					for _, k := range []string{"local_path", "worktree", "path"} {
						if p, ok := v[k].(string); ok && strings.TrimSpace(p) != "" {
							candidates = append(candidates, strings.TrimSpace(p))
							break
						}
					}
				}
			}
		}
	}
	primaryAbs, _ := filepath.Abs(primaryRepo)
	seen := map[string]bool{primaryAbs: true}
	var out []string
	for _, c := range candidates {
		abs, err := filepath.Abs(c)
		if err != nil || seen[abs] {
			continue
		}
		if st, err := os.Stat(abs); err == nil && st.IsDir() {
			seen[abs] = true
			out = append(out, abs)
		}
	}
	return out
}

// LoadExternalDocRecords scans ADR and RFC directories in repoDir and any configured extra repos.
func LoadExternalDocRecords(repoDir string, extraRepos ...string) ([]Record, error) {
	roots := []struct {
		dir    string
		prefix string
	}{{dir: repoDir, prefix: ""}}
	for _, extra := range configuredExtraRepos(repoDir, extraRepos) {
		roots = append(roots, struct {
			dir    string
			prefix string
		}{dir: extra, prefix: filepath.Base(extra)})
	}
	seenIDs := map[string]bool{}
	if existing, err := LoadRecords(repoDir); err == nil {
		for _, r := range existing {
			seenIDs[r.Meta.ID] = true
		}
	}
	var out []Record
	for _, root := range roots {
		if root.prefix != "" {
			if extraRecs, err := LoadRecords(root.dir); err == nil {
				for _, r := range extraRecs {
					if seenIDs[r.Meta.ID] {
						continue
					}
					seenIDs[r.Meta.ID] = true
					r.Path = filepath.ToSlash(filepath.Join(root.prefix, r.Path))
					out = append(out, r)
				}
			}
		}
		for _, sub := range externalDocDirs {
			dir := filepath.Join(root.dir, filepath.FromSlash(sub))
			entries, err := os.ReadDir(dir)
			if err != nil {
				continue
			}
			for _, e := range entries {
				if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
					continue
				}
				lower := strings.ToLower(e.Name())
				if lower == "index.md" || lower == "readme.md" || lower == "template.md" || strings.HasPrefix(lower, "0000-") {
					continue
				}
				full := filepath.Join(dir, e.Name())
				raw, err := os.ReadFile(full)
				if err != nil {
					continue
				}
				rel := filepath.ToSlash(filepath.Join(sub, e.Name()))
				if root.prefix != "" {
					rel = filepath.ToSlash(filepath.Join(root.prefix, rel))
				}
				rec := ParseExternalDocRecord(string(raw), rel)
				if seenIDs[rec.Meta.ID] {
					continue
				}
				seenIDs[rec.Meta.ID] = true
				out = append(out, rec)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Meta.ID < out[j].Meta.ID })
	return out, nil
}

// SaveRecord writes a Record to repoDir and removes any older file with the same ID if the slug changed.
func SaveRecord(repoDir string, rec *Record) error {
	sub := "knowledge/decisions"
	if rec.Meta.Type == RecordTypeLesson {
		sub = "knowledge/lessons"
	}
	if rec.Path == "" {
		rec.Path = filepath.ToSlash(filepath.Join(sub, fmt.Sprintf("%s-%s.md", rec.Meta.ID, Slug(rec.Meta.Title))))
	}
	full := filepath.Join(repoDir, filepath.FromSlash(rec.Path))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return err
	}
	rendered, err := RenderRecord(rec.Meta, rec.Body)
	if err != nil {
		return err
	}
	return os.WriteFile(full, []byte(rendered), 0o644)
}

// NextRecordID allocates the next sequential DEC-YYYY-NNN or LES-YYYY-NNN ID in repoDir.
func NextRecordID(repoDir string, rtype RecordType, year int) string {
	prefix := "DEC"
	if rtype == RecordTypeLesson {
		prefix = "LES"
	}
	if year <= 0 {
		year = time.Now().UTC().Year()
	}
	pat := fmt.Sprintf("%s-%d-", prefix, year)
	maxN := 0
	records, _ := LoadRecords(repoDir)
	for _, r := range records {
		if rest, ok := strings.CutPrefix(r.Meta.ID, pat); ok {
			if n, err := strconv.Atoi(rest); err == nil && n > maxN {
				maxN = n
			}
		}
	}
	return fmt.Sprintf("%s-%d-%03d", prefix, year, maxN+1)
}

// BuildBlueprintDigest constructs a BlueprintDigest from the current records in repoDir.
func BuildBlueprintDigest(repoID string, records []Record, lastEvent string, graduated []string) BlueprintDigest {
	digest := BlueprintDigest{
		Schema:             BlueprintSchema,
		Repo:               repoID,
		UpdatedAt:          time.Now().UTC(),
		LastOutcomeEvent:   lastEvent,
		ActiveIDs:          []string{},
		ProposedIDs:        []string{},
		RejectedIDs:        []string{},
		SupersededIDs:      []string{},
		GraduatedDecisions: graduated,
		Records:            make([]BlueprintRecordDigest, 0, len(records)),
	}
	for _, r := range records {
		switch r.Meta.Status {
		case StatusActive:
			digest.Counts.Active++
			digest.ActiveIDs = append(digest.ActiveIDs, r.Meta.ID)
		case StatusProposed:
			digest.Counts.Proposed++
			digest.ProposedIDs = append(digest.ProposedIDs, r.Meta.ID)
		case StatusRejected:
			digest.Counts.Rejected++
			digest.RejectedIDs = append(digest.RejectedIDs, r.Meta.ID)
		case StatusSuperseded:
			digest.Counts.Superseded++
			digest.SupersededIDs = append(digest.SupersededIDs, r.Meta.ID)
		case StatusRetired:
			digest.Counts.Retired++
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
		digest.Records = append(digest.Records, BlueprintRecordDigest{
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
		})
	}
	return digest
}
