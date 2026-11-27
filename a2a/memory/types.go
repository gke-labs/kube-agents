// Package memory implements the Operational Knowledge Framework (OKF) Curator and
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
	"io"
	"io/fs"
	"net"
	"net/url"
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

	// SubjectMemoryQuery is the dedicated NATS request-reply subject used to query
	// semantic memory over NATS without coupling callers to a specific storage backend.
	SubjectMemoryQuery = "a2a.memory.query"

	// SubjectMemoryReplyPrefix is the NATS reply subject prefix used by callers
	// when sending request-reply queries to the Curator (a2a.memory.reply.<caller>.>).
	SubjectMemoryReplyPrefix = "a2a.memory.reply."

	// SubjectCuratorOutcomes is the durable journal-class NATS outcome subject
	// within the agent principal's reserved a2a.topics.agent.> namespace on TOPICS-JOURNAL.
	SubjectCuratorOutcomes = "a2a.topics.agent.platform.curator-outcomes"

	// TagTrustUntrusted marks recalled records that originate from untrusted sources.
	TagTrustUntrusted = "trust:untrusted"

	// UntrustedSourceBackfill identifies records created from pull request history backfill.
	UntrustedSourceBackfill = "backfill"

	// UntrustedSourceContext identifies records loaded from context repositories.
	UntrustedSourceContext = "context"

	// UntrustedSourceExternal identifies records explicitly flagged as untrusted.
	UntrustedSourceExternal = "external"

	maxSlugLen              = 60
	minRepoSlugSegments     = 2
	proposalBranchPrefix    = "platform-agent/memory/"
	defaultPodStateDir      = "/etc/gitops"
	defaultManagedReposFile = "managed_repos"
	defaultContextReposFile = "context_repos"
	gitSuffix               = ".git"
	schemeDelimiter         = "://"
	githubHost              = "github.com"
	githubWWWHost           = "www.github.com"
	githubSSHHost           = "ssh.github.com"
	repoTagPrefix           = "repo:"
	idTagPrefix             = "id:"
	roleTagPrefix           = "role:"
	outcomeTagBackfill      = "outcome:backfill"
	recordDirPerm           = 0o750
	recordFilePerm          = 0o600
	maxRecordFileBytes      = 4 << 20
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

	SeverityBlock = "block"
	SeverityWarn  = "warn"
	SeverityInfo  = "info"

	RoleManaged = "managed"
	RoleContext = "context"
)

// OutcomeEvent is the structured payload carried inside the DataPart of an
// a2a.topics.shared.annotations topic-update artifact.
type OutcomeEvent struct {
	Schema          string       `json:"schema,omitempty"`
	Kind            OutcomeKind  `json:"kind"`
	Outcome         string       `json:"outcome,omitempty"`
	RecordID        string       `json:"record_id,omitempty"`
	RecordType      RecordType   `json:"record_type,omitempty"`
	Repo            string       `json:"repo,omitempty"`
	Cluster         string       `json:"cluster,omitempty"`
	CheckID         string       `json:"check_id,omitempty"`
	FindingID       string       `json:"finding_id,omitempty"`
	Title           string       `json:"title,omitempty"`
	Summary         string       `json:"summary,omitempty"`
	RootCause       string       `json:"root_cause,omitempty"`
	Fix             string       `json:"fix,omitempty"`
	Verification    string       `json:"verification,omitempty"`
	Prevention      string       `json:"prevention,omitempty"`
	Rule            string       `json:"rule,omitempty"`
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
	GuardSeverity   string       `json:"guard_severity,omitempty"`
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

// DeclareItem represents one entry in an OKF record's `declares:` list.
// It supports declared posture fields (Check, Namespace, Object, Cluster, Expires)
// and backward-compatible manifest assertion fields (File, Path, Op, Value, Severity, Rule).
type DeclareItem struct {
	Check     string `json:"check,omitempty" yaml:"check,omitempty"`
	Namespace string `json:"namespace,omitempty" yaml:"namespace,omitempty"`
	Object    string `json:"object,omitempty" yaml:"object,omitempty"`
	Cluster   string `json:"cluster,omitempty" yaml:"cluster,omitempty"`
	Expires   string `json:"expires,omitempty" yaml:"expires,omitempty"`
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
	Repo         string         `json:"repo,omitempty" yaml:"repo,omitempty"`
	Role         string         `json:"role,omitempty" yaml:"role,omitempty"`
	Untrusted    bool           `json:"untrusted,omitempty" yaml:"untrusted,omitempty"`
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
	Repo           string       `json:"repo,omitempty"`
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
	Schema        string        `json:"schema,omitempty"`
	Action        string        `json:"action,omitempty"`
	CorrelationID string        `json:"correlation_id,omitempty"`
	Query         string        `json:"query"`
	Bank          string        `json:"bank,omitempty"`
	Tags          []string      `json:"tags,omitempty"`
	TagsMatch     string        `json:"tags_match,omitempty"`
	Path          string        `json:"path,omitempty"`
	Cluster       string        `json:"cluster,omitempty"`
	MaxResults    int           `json:"max_results,omitempty"`
	Repo          string        `json:"repo,omitempty"`
	Role          string        `json:"role,omitempty"`
	FromLocal     bool          `json:"from_local,omitempty"`
	BatchID       string        `json:"batch_id,omitempty"`
	BatchIndex    int           `json:"batch_index,omitempty"`
	BatchTotal    int           `json:"batch_total,omitempty"`
	Records       []Record      `json:"records,omitempty"`
	Outcome       *OutcomeEvent `json:"outcome,omitempty"`
}

// MemoryQueryResponse is the NATS reply payload returned by the Curator's
// memory query responder.
type MemoryQueryResponse struct {
	Schema   string                  `json:"schema"`
	OK       bool                    `json:"ok,omitempty"`
	Backend  string                  `json:"backend"`
	Bank     string                  `json:"bank"`
	Query    string                  `json:"query"`
	Repo     string                  `json:"repo,omitempty"`
	Revision string                  `json:"revision,omitempty"`
	Upserted []string                `json:"upserted,omitempty"`
	Deleted  []string                `json:"deleted,omitempty"`
	Tags     []string                `json:"tags,omitempty"`
	Cached   bool                    `json:"cached"`
	Results  []HindsightRecallResult `json:"results"`
	Records  []BlueprintRecordDigest `json:"records,omitempty"`
	Outcome  *OutcomeProcessResult   `json:"outcome,omitempty"`
	Dream    *DreamReport            `json:"dream,omitempty"`
	Error    string                  `json:"error,omitempty"`
}

var (
	reRecordID        = regexp.MustCompile(`^(DEC|LES)-\d{4}-\d{3,}$`)
	reSafeDocRecordID = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,80}$`)
	reExternalDocID   = regexp.MustCompile(`(?i)\b(DEC-\d{4}-\d{3,}|LES-\d{4}-\d{3,}|ADR-\d{1,5}|RFC-\d{1,5}|RUN-\d{1,5}|BP-\d{1,5}|TEN-\d{1,5}|MET-\d{1,5}|ESC-\d{1,5}|OBS-\d{1,5})\b`)
	reLeadingNum      = regexp.MustCompile(`^0*(\d{1,5})(?:[-_.\s]+(.*))?$`)
	reSlugNonAln      = regexp.MustCompile(`[^a-z0-9]+`)
	externalDocDirs   = []string{
		"knowledge/runbook",
		"knowledge/runbooks",
		"knowledge/cluster-blueprint",
		"knowledge/cluster-blueprints",
		"knowledge/blueprints",
		"knowledge/tenancy-model",
		"knowledge/tenancy-models",
		"knowledge/tenancy",
		"knowledge/metric-definition",
		"knowledge/metric-definitions",
		"knowledge/metrics",
		"knowledge/escalation",
		"knowledge/escalations",
		"knowledge/observation",
		"knowledge/observations",
		"docs/adr",
		"docs/adrs",
		"docs/decisions",
		"docs/rfcs",
		"docs/runbooks",
		"docs/playbooks",
		"adr",
		"decisions",
		"rfcs",
		"runbooks",
		"playbooks",
	}
)

// Slug formats a title into a URL-safe filename suffix (up to 60 characters).
func Slug(s string) string {
	cleaned := strings.Trim(reSlugNonAln.ReplaceAllString(strings.ToLower(s), "-"), "-")
	if len(cleaned) > maxSlugLen {
		cleaned = strings.TrimRight(cleaned[:maxSlugLen], "-")
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

// IsUntrusted reports whether this record comes from an untrusted source.
func (r Record) IsUntrusted() bool {
	return r.Meta.Untrusted ||
		strings.EqualFold(strings.TrimSpace(r.Meta.Role), RoleContext) ||
		strings.EqualFold(strings.TrimSpace(r.Meta.Evidence.Outcome), UntrustedSourceBackfill)
}

// UntrustedSource returns the untrusted source label ("context", "backfill", or "external"), or "".
func (r Record) UntrustedSource() string {
	if strings.EqualFold(strings.TrimSpace(r.Meta.Role), RoleContext) {
		return UntrustedSourceContext
	}
	if strings.EqualFold(strings.TrimSpace(r.Meta.Evidence.Outcome), UntrustedSourceBackfill) {
		return UntrustedSourceBackfill
	}
	if r.Meta.Untrusted {
		return UntrustedSourceExternal
	}
	return ""
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
	if repo := strings.TrimSpace(r.Meta.Repo); repo != "" {
		hasRepoTag := false
		for existing := range seen {
			if strings.HasPrefix(existing, repoTagPrefix) {
				hasRepoTag = true
				break
			}
		}
		if !hasRepoTag {
			seen[repoTagPrefix+repo] = struct{}{}
		}
	}
	if role := strings.TrimSpace(r.Meta.Role); role != "" {
		seen[roleTagPrefix+role] = struct{}{}
	}
	if strings.EqualFold(strings.TrimSpace(r.Meta.Evidence.Outcome), UntrustedSourceBackfill) {
		seen[outcomeTagBackfill] = struct{}{}
	}
	if r.IsUntrusted() {
		seen[TagTrustUntrusted] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for t := range seen {
		out = append(out, t)
	}
	sort.Strings(out)
	return out
}

func cloneRecord(r Record) Record {
	cp := r
	if r.Meta.Declares != nil {
		cp.Meta.Declares = append([]DeclareItem(nil), r.Meta.Declares...)
	}
	if r.Meta.Scope.Paths != nil {
		cp.Meta.Scope.Paths = append([]string(nil), r.Meta.Scope.Paths...)
	}
	if r.Meta.Scope.Resources != nil {
		cp.Meta.Scope.Resources = append([]string(nil), r.Meta.Scope.Resources...)
	}
	if r.Meta.Scope.Clusters != nil {
		cp.Meta.Scope.Clusters = append([]string(nil), r.Meta.Scope.Clusters...)
	}
	if r.Meta.Scope.Checks != nil {
		cp.Meta.Scope.Checks = append([]string(nil), r.Meta.Scope.Checks...)
	}
	if r.Meta.Guard != nil {
		g := *r.Meta.Guard
		if r.Meta.Guard.Expect != nil {
			exp := *r.Meta.Guard.Expect
			g.Expect = &exp
		}
		cp.Meta.Guard = &g
	}
	if r.Meta.Evidence.RejectedProposals != nil {
		cp.Meta.Evidence.RejectedProposals = append([]string(nil), r.Meta.Evidence.RejectedProposals...)
	}
	if r.Meta.Supersedes != nil {
		cp.Meta.Supersedes = append([]string(nil), r.Meta.Supersedes...)
	}
	return cp
}

func isValidDocRecordID(id string) bool {
	return id != "." && id != ".." && reSafeDocRecordID.MatchString(id)
}

func clampRecordStatus(s RecordStatus, defaultStatus RecordStatus) RecordStatus {
	switch RecordStatus(strings.ToLower(strings.TrimSpace(string(s)))) {
	case StatusActive:
		return StatusActive
	case StatusProposed:
		return StatusProposed
	case StatusRejected:
		return StatusRejected
	case StatusSuperseded:
		return StatusSuperseded
	case StatusRetired:
		return StatusRetired
	default:
		return defaultStatus
	}
}

func fallbackRecordIDPrefix(rtype RecordType, relPath string) string {
	switch rtype {
	case RecordTypeLesson:
		return "LES"
	case RecordTypeRunbook:
		return "RUN"
	case RecordTypeClusterBlueprint:
		return "BP"
	case RecordTypeTenancyModel:
		return "TEN"
	case RecordTypeMetricDefinition:
		return "MET"
	case RecordTypeEscalation:
		return "ESC"
	case RecordTypeObservation:
		return "OBS"
	case RecordTypeDecision:
		if strings.Contains(strings.ToLower(relPath), "rfc") {
			return "RFC"
		}
		return "ADR"
	default:
		_, prefix := inferExternalDocTypeAndPrefix(nil, relPath)
		return prefix
	}
}

func sanitizeUntrustedRecordID(r Record) string {
	cand := strings.TrimSpace(r.Meta.ID)
	if isValidDocRecordID(cand) {
		return cand
	}
	prefix := fallbackRecordIDPrefix(r.Meta.Type, r.Path)
	if r.Path != "" {
		stem := strings.TrimSuffix(filepath.Base(r.Path), filepath.Ext(r.Path))
		if m := reExternalDocID.FindStringSubmatch(stem); len(m) > 1 {
			if upper := strings.ToUpper(m[1]); isValidDocRecordID(upper) {
				return upper
			}
		} else if nm := reLeadingNum.FindStringSubmatch(stem); len(nm) > 1 {
			if n, err := strconv.Atoi(nm[1]); err == nil {
				return fmt.Sprintf("%s-%04d", prefix, n)
			}
		}
	}
	seed := strings.TrimSpace(r.Path)
	if seed == "" {
		seed = r.Meta.Title + "\x00" + cand + "\x00" + r.Body
	}
	sum := sha256.Sum256([]byte(seed))
	return fmt.Sprintf("%s-%s", prefix, strings.ToUpper(hex.EncodeToString(sum[:])[:6]))
}

// NormalizeRepoRecords returns a deep copy of recs stamped with repository and role metadata.
// When role is "context", it marks records untrusted, strips machine assertions, and downgrades blocking guards.
func NormalizeRepoRecords(repoID, role string, recs []Record) []Record {
	if len(recs) == 0 {
		return []Record{}
	}
	cleanRepo := strings.TrimSpace(repoID)
	normRole := strings.ToLower(strings.TrimSpace(role))
	if normRole != RoleManaged && normRole != RoleContext {
		normRole = ""
	}
	seenIDs := make(map[string]bool, len(recs))
	out := make([]Record, 0, len(recs))
	for _, r := range recs {
		cp := cloneRecord(r)
		if cleanRepo != "" {
			cp.Meta.Repo = cleanRepo
		}
		if normRole != "" {
			cp.Meta.Role = normRole
		}
		if normRole == RoleContext {
			cp.Meta.Untrusted = true
		}
		rawID := strings.TrimSpace(cp.Meta.ID)
		if normRole == RoleContext || cp.IsUntrusted() || !isValidDocRecordID(rawID) {
			cp.Meta.ID = sanitizeUntrustedRecordID(cp)
			if seenIDs[cp.Meta.ID] && !isValidDocRecordID(rawID) {
				sum := sha256.Sum256([]byte(fmt.Sprintf("%s\x00%s\x00%d", cp.Path, rawID, len(out))))
				base := cp.Meta.ID
				if len(base) > 75 {
					base = strings.TrimRight(base[:75], "._-")
				}
				cp.Meta.ID = fmt.Sprintf("%s-%s", base, strings.ToUpper(hex.EncodeToString(sum[:])[:4]))
			}
		} else {
			cp.Meta.ID = rawID
		}
		cp.Meta.Status = clampRecordStatus(cp.Meta.Status, StatusProposed)
		seenIDs[cp.Meta.ID] = true
		if normRole == RoleContext {
			for i := range cp.Meta.Declares {
				cp.Meta.Declares[i].File = ""
				cp.Meta.Declares[i].Path = ""
				cp.Meta.Declares[i].Op = ""
				cp.Meta.Declares[i].Value = nil
				if strings.EqualFold(strings.TrimSpace(cp.Meta.Declares[i].Severity), SeverityBlock) {
					cp.Meta.Declares[i].Severity = SeverityWarn
				}
			}
			if cp.Meta.Guard != nil {
				cp.Meta.Guard.Expect = nil
				if strings.EqualFold(strings.TrimSpace(cp.Meta.Guard.Severity), SeverityBlock) {
					cp.Meta.Guard.Severity = SeverityWarn
				}
				if strings.TrimSpace(cp.Meta.Guard.Rule) == "" && strings.TrimSpace(cp.Meta.Guard.Severity) == "" {
					cp.Meta.Guard = nil
				} else {
					cp.Meta.Status = StatusProposed
				}
			}
		}
		out = append(out, cp)
	}
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
	return proposalBranchPrefix + r.Meta.ID
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
	repoRoot, err := os.OpenRoot(repoDir)
	if err != nil {
		return []string{"knowledge"}
	}
	defer repoRoot.Close()

	raw, err := repoRoot.ReadFile(filepath.Join(".kube-agents", "intent.yaml"))
	if err != nil {
		return []string{"knowledge"}
	}
	var parsed struct {
		Paths []string `yaml:"paths"`
	}
	if yaml.Unmarshal(raw, &parsed) != nil {
		return []string{"knowledge"}
	}
	var roots []string
	seen := map[string]bool{}
	for _, p := range parsed.Paths {
		clean := strings.Trim(filepath.ToSlash(strings.TrimSpace(p)), "/")
		if clean == "" || clean == "." || strings.Contains(clean, "..") || seen[clean] {
			continue
		}
		seen[clean] = true
		roots = append(roots, clean)
	}
	if len(roots) == 0 {
		return []string{"knowledge"}
	}
	return roots
}

func stripKnowledgeLeaf(root string) string {
	for _, leaf := range []string{"/decision", "/decisions", "/lesson", "/lessons"} {
		if strings.HasSuffix(root, leaf) {
			return strings.TrimSuffix(root, leaf)
		}
	}
	return root
}

// LoadRecords reads all OKF decision and lesson records from knowledge/ (and any paths in .kube-agents/intent.yaml) under repoDir.
func LoadRecords(repoDir string) ([]Record, error) {
	repoRoot, err := os.OpenRoot(repoDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer repoRoot.Close()

	var out []Record
	seenPaths := map[string]bool{}
	seenSub := map[string]bool{}
	var subDirs []string
	addSub := func(s string) {
		clean := strings.Trim(filepath.ToSlash(s), "/")
		if clean != "" && !seenSub[clean] {
			seenSub[clean] = true
			subDirs = append(subDirs, clean)
		}
	}
	for _, root := range intentRoots(repoDir) {
		addSub(root)
		base := stripKnowledgeLeaf(root)
		addSub(base)
		for _, leaf := range []string{"decision", "lesson", "decisions", "lessons"} {
			addSub(filepath.ToSlash(filepath.Join(base, leaf)))
		}
	}
	for _, sub := range subDirs {
		entries, err := fs.ReadDir(repoRoot.FS(), sub)
		if err != nil {
			continue
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
			raw, err := readBoundedFromRoot(repoRoot, filepath.FromSlash(rel))
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

func stripRepoPrefixForDocType(relPath string) string {
	slashPath := filepath.ToSlash(relPath)
	if _, after, ok := strings.Cut(slashPath, ":"); ok {
		slashPath = after
	}
	if first, rest, ok := strings.Cut(slashPath, "/"); ok && strings.Contains(first, "__") {
		slashPath = rest
	}
	return slashPath
}

func inferExternalDocTypeAndPrefix(fm map[string]any, relPath string) (RecordType, string) {
	innerPath := stripRepoPrefixForDocType(relPath)
	if fm != nil {
		if tStr, ok := fm["type"].(string); ok {
			switch strings.ToLower(strings.TrimSpace(tStr)) {
			case "decision", "adr", "rfc":
				if strings.Contains(strings.ToLower(innerPath), "rfc") || strings.EqualFold(strings.TrimSpace(tStr), "rfc") {
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
	lower := strings.ToLower(innerPath)
	dirSlash := strings.ToLower(filepath.ToSlash(filepath.Dir(innerPath)))
	switch {
	case dirSlash == "docs/decisions" || strings.HasPrefix(dirSlash, "docs/decisions/") ||
		dirSlash == "decisions" || strings.HasPrefix(dirSlash, "decisions/"):
		return RecordTypeDecision, "ADR"
	case dirSlash == "docs/runbooks" || strings.HasPrefix(dirSlash, "docs/runbooks/") ||
		dirSlash == "runbooks" || strings.HasPrefix(dirSlash, "runbooks/") ||
		dirSlash == "docs/playbooks" || strings.HasPrefix(dirSlash, "docs/playbooks/") ||
		dirSlash == "playbooks" || strings.HasPrefix(dirSlash, "playbooks/"):
		return RecordTypeRunbook, "RUN"
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
	rec, _ := parseExternalDocRecord(text, relPath)
	return rec
}

func parseExternalDocRecord(text, relPath string) (rec Record, explicitID bool) {
	if parsed, err := ParseRecord(text, relPath); err == nil && len(ValidateRecord(parsed)) == 0 && isValidDocRecordID(parsed.Meta.ID) {
		return parsed, true
	}
	var fm map[string]any
	body := strings.TrimSpace(text)
	if strings.HasPrefix(text, "---\n") || strings.HasPrefix(text, "---\r\n") {
		rest := text[4:]
		if strings.HasPrefix(text, "---\r\n") {
			rest = text[5:]
		}
		if idx := strings.Index(rest, "\n---"); idx >= 0 {
			rawFM := rest[:idx]
			after := rest[idx+4:]
			body = strings.TrimSpace(strings.TrimLeft(after, "\r\n"))
			_ = yaml.Unmarshal([]byte(rawFM), &fm)
		}
	}
	stem := strings.TrimSuffix(filepath.Base(relPath), filepath.Ext(relPath))
	rtype, prefix := inferExternalDocTypeAndPrefix(fm, relPath)
	recID := ""
	if fm != nil {
		if idStr, ok := fm["id"].(string); ok {
			cand := strings.TrimSpace(idStr)
			if isValidDocRecordID(cand) {
				recID = cand
				explicitID = true
			}
		}
	}
	if recID == "" {
		if m := reExternalDocID.FindStringSubmatch(stem); len(m) > 1 && isValidDocRecordID(strings.ToUpper(m[1])) {
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
		if rawStatus, hasStatus := fm["status"]; hasStatus && rawStatus != nil {
			if stStr, ok := rawStatus.(string); ok {
				switch strings.ToLower(strings.TrimSpace(stStr)) {
				case "", "active", "accepted", "approved":
					status = StatusActive
				case "proposed", "draft":
					status = StatusProposed
				case "rejected":
					status = StatusRejected
				case "superseded", "deprecated", "obsolete":
					status = StatusSuperseded
				case "retired":
					status = StatusRetired
				default:
					status = StatusProposed
				}
			} else {
				status = StatusProposed
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
	}, explicitID
}

var (
	reSafeRepoSegment = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)
	reSCPRemote       = regexp.MustCompile(`^(?:[^/@]+@)?([^/:]+):(.+)$`)
	reHostname        = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$`)
)

func isSafeRepoSegment(seg string) bool {
	if seg == "." || seg == ".." || seg == gitSuffix || strings.HasPrefix(seg, "-") {
		return false
	}
	return reSafeRepoSegment.MatchString(seg)
}

func isGitHubHost(host string) bool {
	switch strings.ToLower(strings.TrimSpace(host)) {
	case githubHost, githubWWWHost, githubSSHHost:
		return true
	}
	return false
}

func isValidRepoHost(host string) bool {
	if host == "" {
		return false
	}
	if net.ParseIP(host) != nil {
		return true
	}
	if strings.HasPrefix(host, ".") || strings.HasSuffix(host, ".") || strings.HasPrefix(host, "-") || strings.HasSuffix(host, "-") || strings.Contains(host, "..") {
		return false
	}
	for _, ch := range host {
		if !((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '.' || ch == '-') {
			return false
		}
	}
	return true
}

func cleanRepoSlugPath(rawPath string) string {
	trimmed := strings.Trim(rawPath, "/")
	if trimmed == "" {
		return ""
	}
	parts := strings.Split(trimmed, "/")
	segments := make([]string, 0, len(parts))
	for _, part := range parts {
		if part == "" {
			continue
		}
		if part == "." || part == ".." {
			return ""
		}
		segments = append(segments, part)
	}
	if len(segments) == 0 {
		return ""
	}
	last := segments[len(segments)-1]
	if strings.HasSuffix(last, gitSuffix) {
		stem := strings.TrimSuffix(last, gitSuffix)
		if stem == "" || stem == "." || stem == ".." {
			return ""
		}
		segments[len(segments)-1] = stem
	}
	if len(segments) < minRepoSlugSegments {
		return ""
	}
	for _, seg := range segments {
		if !isSafeRepoSegment(seg) {
			return ""
		}
	}
	return strings.Join(segments, "/")
}

// ExtractRepoSlug normalizes a Git remote URL or host-qualified slug into a
// canonical repository identifier ("owner/repo" for GitHub, "<host>/owner/repo"
// for non-GitHub hosts). It returns "" when the input is malformed or empty.
func ExtractRepoSlug(u string) string {
	s := strings.TrimRight(strings.TrimSpace(u), "/")
	if s == "" {
		return ""
	}
	var host, rawPath string
	if strings.Contains(s, schemeDelimiter) {
		parsed, err := url.Parse(s)
		if err != nil || parsed.Hostname() == "" || parsed.Opaque != "" {
			return ""
		}
		host = strings.ToLower(parsed.Hostname())
		rawPath = parsed.Path
	} else {
		hadUserinfo := false
		slash := strings.Index(s, "/")
		prefix := s
		if slash >= 0 {
			prefix = s[:slash]
		}
		if at := strings.LastIndex(prefix, "@"); at >= 0 {
			hadUserinfo = true
			s = s[at+1:]
		}
		if s == "" || strings.HasPrefix(s, ":") || strings.HasPrefix(s, "/") {
			return ""
		}
		if colon := strings.Index(s, ":"); colon > 0 {
			slash = strings.Index(s, "/")
			if slash < 0 || colon < slash {
				portPart := s[colon+1:]
				if slash > colon {
					portPart = s[colon+1 : slash]
				}
				isPort := portPart != ""
				for _, ch := range portPart {
					if ch < '0' || ch > '9' {
						isPort = false
						break
					}
				}
				hostCandidate := strings.ToLower(s[:colon])
				if isPort {
					if slash <= colon {
						return ""
					}
					host = hostCandidate
					rawPath = s[slash+1:]
				} else {
					if !strings.Contains(hostCandidate, ".") {
						return ""
					}
					host = hostCandidate
					rawPath = s[colon+1:]
				}
			} else {
				return ""
			}
		} else {
			if first, rest, ok := strings.Cut(s, "/"); ok {
				rest = strings.TrimLeft(rest, "/")
				if strings.Contains(rest, "/") && strings.Contains(first, ".") {
					host = strings.ToLower(first)
					rawPath = rest
				} else if !hadUserinfo {
					rawPath = s
				}
			}
		}
		if hadUserinfo && host == "" {
			return ""
		}
	}
	if host != "" && !isValidRepoHost(host) {
		return ""
	}
	cleanPath := cleanRepoSlugPath(rawPath)
	if cleanPath == "" {
		return ""
	}
	if host == "" || isGitHubHost(host) {
		return cleanPath
	}
	return host + "/" + cleanPath
}

// CanonicalRepoID normalizes a repository URL or slug to a lowercase canonical
// identifier for case-insensitive cross-format comparison.
func CanonicalRepoID(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}
	if slug := ExtractRepoSlug(trimmed); slug != "" {
		return strings.ToLower(slug)
	}
	if strings.Contains(trimmed, schemeDelimiter) || strings.Contains(trimmed, "@") || strings.Contains(trimmed, ":") {
		return ""
	}
	cleaned := strings.Trim(trimmed, "/")
	if cleaned == "" || cleaned == "." || cleaned == ".." || strings.Contains(cleaned, "/") {
		return ""
	}
	if !isSafeRepoSegment(cleaned) {
		return ""
	}
	return strings.ToLower(cleaned)
}

// SameRepoSlug reports whether two repository identifiers refer to the same
// canonical repository (for example, "github.com/acme/infra" and "acme/infra").
func SameRepoSlug(a, b string) bool {
	ca := CanonicalRepoID(a)
	cb := CanonicalRepoID(b)
	return ca != "" && ca == cb
}

func trimGitRepoPath(p string) string {
	p = strings.Trim(p, "/")
	if strings.HasSuffix(p, ".git") {
		trimmed := strings.TrimSuffix(p, ".git")
		if trimmed != "" && !strings.HasSuffix(trimmed, "/") {
			p = trimmed
		}
	}
	return strings.Trim(p, "/")
}

// workspaceCloneDirName mirrors agents/platform/scripts/gitops_workspace.py's
// workspace_path naming: GitHub and bare owner/name map to owner__name, while
// non-GitHub hosts (including URLs with ports or nested GitLab subgroups) map
// to <host>%2F<path> without port segments.
func workspaceCloneDirName(repoURL string) string {
	text := strings.TrimSpace(repoURL)
	if text == "" || len(text) > 256 {
		return ""
	}
	var host, rawPath string
	if strings.Contains(text, "://") {
		parsed, err := url.Parse(text)
		if err != nil || parsed.Hostname() == "" {
			return ""
		}
		host = strings.ToLower(parsed.Hostname())
		rawPath = parsed.Path
	} else if m := reSCPRemote.FindStringSubmatch(text); len(m) == 3 {
		host = strings.ToLower(m[1])
		rawPath = m[2]
	} else {
		rawPath = text
	}
	rawPath = trimGitRepoPath(rawPath)
	if host == "" && strings.Contains(rawPath, "/") {
		first, rest, _ := strings.Cut(rawPath, "/")
		rest = strings.Trim(rest, "/")
		firstLower := strings.ToLower(first)
		if rest != "" && (firstLower == "github.com" || firstLower == "gitlab.com" || firstLower == "bitbucket.org" || reHostname.MatchString(firstLower)) {
			host = firstLower
			rawPath = rest
		}
	}
	if rawPath == "" {
		return ""
	}
	segments := strings.Split(rawPath, "/")
	if len(segments) < 2 {
		return ""
	}
	for _, seg := range segments {
		if !isSafeRepoSegment(seg) {
			return ""
		}
	}
	if host == "github.com" || host == "www.github.com" || host == "ssh.github.com" {
		host = ""
	}
	if host != "" {
		return strings.ReplaceAll(host+"/"+strings.Join(segments, "/"), "/", "%2F")
	}
	return strings.Join(segments, "__")
}

func contextReposStatePath() string {
	if statePath := strings.TrimSpace(os.Getenv("GITOPS_STATE_PATH")); statePath != "" {
		return filepath.Join(filepath.Dir(filepath.Clean(statePath)), defaultContextReposFile)
	}
	return filepath.Join(defaultPodStateDir, defaultContextReposFile)
}

func contextRepoCloneDirs(repoURL string) []string {
	dirName := workspaceCloneDirName(repoURL)
	if dirName == "" || strings.Contains(dirName, "..") || strings.Contains(dirName, "/") {
		return nil
	}
	agentHome := strings.TrimRight(strings.TrimSpace(os.Getenv("PLATFORM_AGENT_HOME")), "/")
	if agentHome == "" {
		agentHome = "/opt/data"
	}
	gitopsRoot := filepath.Join(filepath.Clean(agentHome), "gitops")
	root, err := os.OpenRoot(gitopsRoot)
	if err != nil {
		return nil
	}
	defer root.Close()

	var matches []string
	if st, err := root.Stat(dirName); err == nil && st.IsDir() {
		matches = append(matches, filepath.Join(gitopsRoot, dirName))
	}
	if leases, err := fs.ReadDir(root.FS(), "."); err == nil {
		for _, lease := range leases {
			if !lease.IsDir() {
				continue
			}
			candRel := filepath.Join(lease.Name(), dirName)
			if st, err := root.Stat(candRel); err == nil && st.IsDir() {
				matches = append(matches, filepath.Join(gitopsRoot, candRel))
			}
		}
	}
	return matches
}

func readBoundedFromRoot(root *os.Root, rel string) ([]byte, error) {
	f, err := root.Open(rel)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxRecordFileBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > maxRecordFileBytes {
		return nil, fmt.Errorf("record file %s exceeds %d-byte limit", rel, maxRecordFileBytes)
	}
	return raw, nil
}

func readFileViaRoot(fullPath string) ([]byte, error) {
	clean := filepath.Clean(fullPath)
	root, err := os.OpenRoot(filepath.Dir(clean))
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return readBoundedFromRoot(root, filepath.Base(clean))
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
	if rawBytes, err := readFileViaRoot(contextReposStatePath()); err == nil && len(bytes.TrimSpace(rawBytes)) > 0 {
		var parsed []any
		if json.Unmarshal(rawBytes, &parsed) == nil {
			for _, item := range parsed {
				switch v := item.(type) {
				case string:
					if strings.TrimSpace(v) != "" {
						candidates = append(candidates, strings.TrimSpace(v))
						candidates = append(candidates, contextRepoCloneDirs(v)...)
					}
				case map[string]any:
					if _, refused := v["refused_ref"]; refused {
						continue
					}
					for _, k := range []string{"local_path", "worktree", "path"} {
						if p, ok := v[k].(string); ok && strings.TrimSpace(p) != "" {
							candidates = append(candidates, strings.TrimSpace(p))
						}
					}
					if u, ok := v["url"].(string); ok && strings.TrimSpace(u) != "" {
						candidates = append(candidates, contextRepoCloneDirs(u)...)
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
		r, err := os.OpenRoot(abs)
		if err == nil {
			_ = r.Close()
			seen[abs] = true
			out = append(out, abs)
		}
	}
	return out
}

type externalDocRoot struct {
	dir    string
	prefix string
}

// LoadSingleRepoExternalDocRecords scans external documentation directories inside repoDir only.
func LoadSingleRepoExternalDocRecords(repoDir string) ([]Record, error) {
	return loadExternalDocRecordsFromRoots(repoDir, []externalDocRoot{{dir: repoDir, prefix: ""}})
}

// LoadExternalDocRecords scans ADR and RFC directories in repoDir and any configured extra repos.
func LoadExternalDocRecords(repoDir string, extraRepos ...string) ([]Record, error) {
	roots := []externalDocRoot{{dir: repoDir, prefix: ""}}
	for _, extra := range configuredExtraRepos(repoDir, extraRepos) {
		roots = append(roots, externalDocRoot{dir: extra, prefix: filepath.Base(extra)})
	}
	return loadExternalDocRecordsFromRoots(repoDir, roots)
}

func loadExternalDocRecordsFromRoots(repoDir string, roots []externalDocRoot) ([]Record, error) {
	seenIDs := map[string]bool{}
	if existing, err := LoadRecords(repoDir); err == nil {
		for _, r := range existing {
			seenIDs[r.Meta.ID] = true
		}
	}
	var out []Record
	seenFiles := map[string]bool{}
	for _, root := range roots {
		var rootRecs []Record
		if root.prefix != "" {
			if extraRecs, err := LoadRecords(root.dir); err == nil {
				for _, r := range extraRecs {
					absFile := filepath.Join(root.dir, filepath.FromSlash(r.Path))
					if seenFiles[absFile] {
						continue
					}
					seenFiles[absFile] = true
					rel := filepath.ToSlash(filepath.Join(root.prefix, r.Path))
					r.Path = rel
					r.Meta.ID = sanitizeUntrustedRecordID(r)
					r.Meta.Status = clampRecordStatus(r.Meta.Status, StatusProposed)
					if seenIDs[r.Meta.ID] {
						sum := sha256.Sum256([]byte(rel))
						base := r.Meta.ID
						if len(base) > 75 {
							base = strings.TrimRight(base[:75], "._-")
						}
						r.Meta.ID = fmt.Sprintf("%s-%s", base, strings.ToUpper(hex.EncodeToString(sum[:])[:4]))
					}
					seenIDs[r.Meta.ID] = true
					rootRecs = append(rootRecs, r)
				}
			}
		}
		rRoot, err := os.OpenRoot(root.dir)
		if err != nil {
			if root.prefix != "" && len(rootRecs) > 0 {
				rootRecs = NormalizeRepoRecords(root.prefix, RoleContext, rootRecs)
			}
			out = append(out, rootRecs...)
			continue
		}
		for _, sub := range externalDocDirs {
			entries, err := fs.ReadDir(rRoot.FS(), filepath.ToSlash(sub))
			if err != nil {
				continue
			}
			for _, e := range entries {
				if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
					continue
				}
				lower := strings.ToLower(e.Name())
				if lower == "index.md" || lower == "readme.md" || lower == "template.md" || lower == "state_of_being.md" || strings.HasPrefix(lower, "0000-") {
					continue
				}
				innerRel := filepath.ToSlash(filepath.Join(sub, e.Name()))
				absFile := filepath.Join(root.dir, filepath.FromSlash(innerRel))
				if seenFiles[absFile] {
					continue
				}
				raw, err := readBoundedFromRoot(rRoot, filepath.FromSlash(innerRel))
				if err != nil {
					continue
				}
				seenFiles[absFile] = true
				rel := innerRel
				if root.prefix != "" {
					rel = filepath.ToSlash(filepath.Join(root.prefix, innerRel))
				}
				rec, explicitID := parseExternalDocRecord(string(raw), rel)
				if seenIDs[rec.Meta.ID] {
					if explicitID && root.prefix == "" {
						continue
					}
					sum := sha256.Sum256([]byte(rel))
					base := rec.Meta.ID
					if len(base) > 75 {
						base = strings.TrimRight(base[:75], "._-")
					}
					rec.Meta.ID = fmt.Sprintf("%s-%s", base, strings.ToUpper(hex.EncodeToString(sum[:])[:4]))
				}
				seenIDs[rec.Meta.ID] = true
				rootRecs = append(rootRecs, rec)
			}
		}
		_ = rRoot.Close()
		if root.prefix != "" && len(rootRecs) > 0 {
			rootRecs = NormalizeRepoRecords(root.prefix, RoleContext, rootRecs)
		}
		out = append(out, rootRecs...)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Meta.ID < out[j].Meta.ID })
	return out, nil
}

func resolveKnowledgeSubdirRoot(repoRoot *os.Root, baseRoot, singularLeaf, pluralLeaf string) string {
	singular := filepath.ToSlash(filepath.Join(baseRoot, singularLeaf))
	if st, err := repoRoot.Stat(filepath.FromSlash(singular)); err == nil && st.IsDir() {
		return singular
	}
	plural := filepath.ToSlash(filepath.Join(baseRoot, pluralLeaf))
	if st, err := repoRoot.Stat(filepath.FromSlash(plural)); err == nil && st.IsDir() {
		return plural
	}
	return singular
}

// SaveRecord writes a Record to repoDir and removes any older file with the same ID if the slug changed.
func SaveRecord(repoDir string, rec *Record) error {
	if !reRecordID.MatchString(rec.Meta.ID) {
		return fmt.Errorf("invalid record ID %q", rec.Meta.ID)
	}
	if err := os.MkdirAll(repoDir, recordDirPerm); err != nil {
		return err
	}
	existingRecords, _ := LoadRecords(repoDir)

	repoRoot, err := os.OpenRoot(repoDir)
	if err != nil {
		return err
	}
	defer repoRoot.Close()

	baseRoot := stripKnowledgeLeaf(intentRoots(repoDir)[0])
	sub := resolveKnowledgeSubdirRoot(repoRoot, baseRoot, "decision", "decisions")
	if rec.Meta.Type == RecordTypeLesson {
		sub = resolveKnowledgeSubdirRoot(repoRoot, baseRoot, "lesson", "lessons")
	}
	if rec.Path == "" {
		rec.Path = filepath.ToSlash(filepath.Join(sub, fmt.Sprintf("%s-%s.md", rec.Meta.ID, Slug(rec.Meta.Title))))
	} else {
		clean := filepath.ToSlash(filepath.Clean(rec.Path))
		if strings.HasPrefix(clean, "/") || clean == ".." || strings.HasPrefix(clean, "../") || strings.Contains(clean, "/../") {
			return fmt.Errorf("invalid record path %q", rec.Path)
		}
		rec.Path = clean
	}
	parentRel := filepath.FromSlash(filepath.Dir(rec.Path))
	if parentRel != "." && parentRel != "" {
		if err := repoRoot.MkdirAll(parentRel, recordDirPerm); err != nil {
			return err
		}
	}
	rendered, err := RenderRecord(rec.Meta, rec.Body)
	if err != nil {
		return err
	}
	if err := repoRoot.WriteFile(filepath.FromSlash(rec.Path), []byte(rendered), recordFilePerm); err != nil {
		return err
	}
	for _, old := range existingRecords {
		if old.Meta.ID == rec.Meta.ID && old.Path != "" && old.Path != rec.Path {
			_ = repoRoot.Remove(filepath.FromSlash(old.Path))
		}
	}
	return nil
}

// NextRecordID allocates the next sequential DEC-YYYY-NNN or LES-YYYY-NNN ID in repoDir.
func NextRecordID(repoDir string, rtype RecordType, year int) string {
	return NextRecordIDWithKnown(repoDir, rtype, year, nil)
}

// NextRecordIDWithKnown allocates the next sequential DEC-YYYY-NNN or LES-YYYY-NNN ID
// considering both local records in repoDir and any additional known IDs recovered from
// Hindsight or the TOPICS-STATE blueprint digest across pod restarts.
func NextRecordIDWithKnown(repoDir string, rtype RecordType, year int, extraIDs map[string]bool) string {
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
	for id := range extraIDs {
		if rest, ok := strings.CutPrefix(id, pat); ok {
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
			Repo:           r.Meta.Repo,
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
