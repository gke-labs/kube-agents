// Command curator is the Go OKF Curator and Dreamer daemon and CLI for kube-agents.
// It subscribes to NATS JetStream (a2a.topics.shared.annotations on TOPICS-JOURNAL),
// synthesizes and updates Operational Knowledge Format (OKF) records (DEC-* and LES-*),
// indexes them verbatim into Hindsight (kube-agents-decisions) with in-place status
// transitions (status:proposed -> status:active | status:rejected | status:superseded),
// graduates multi-cluster recurrences into fleet-wide decisions, and broadcasts the
// active digest to a2a.topics.shared.blueprint on TOPICS-STATE.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/memory"
)

const curatorUsage = `curator - Go OKF Decision & Lesson Memory Curator for kube-agents

usage:
  curator daemon [flags]         subscribe to SubjectCuratorOutcomes, index into Hindsight, serve NATS memory queries, and publish blueprint digests
  curator emit [flags]           publish a kube-agents.memory.outcome/v1 event and return the synthesized record metadata
  curator pending [flags]        list status:proposed OKF records awaiting pull request creation or review
  curator check [flags]          evaluate active and proposed OKF guards against changed files (exits 2 on block)
  curator index [flags]          sync local knowledge/ records into the kube-agents-decisions Hindsight bank
  curator recall [flags] <query> query semantic memory over NATS (with fallback to Hindsight REST)

environment:
  NATS_URL            NATS JetStream bus URL
  A2A_BUS_USER        bus principal (default: agent)
  A2A_BUS_TOKEN_FILE  projected SA token path (default: /var/run/secrets/a2a-bus/token)
  HINDSIGHT_API_URL   Hindsight REST endpoint (for example, http://hindsight-api.kubeagents-system.svc.cluster.local:8888)
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		var exitErr *exitCodeError
		if errors.As(err, &exitErr) {
			os.Exit(exitErr.code)
		}
		fmt.Fprintf(os.Stderr, "curator: %v\n", err)
		os.Exit(1)
	}
}

type exitCodeError struct {
	code int
}

func (e *exitCodeError) Error() string {
	return fmt.Sprintf("exit %d", e.code)
}

func run(args []string) error {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, curatorUsage)
		return errors.New("no subcommand")
	}
	switch args[0] {
	case "daemon", "serve":
		return runDaemon(args[1:])
	case "emit", "publish":
		return runEmit(args[1:])
	case "pending":
		return runPending(args[1:])
	case "check":
		return runCheck(args[1:])
	case "index":
		return runIndex(args[1:])
	case "recall", "query":
		return runRecall(args[1:])
	case "-h", "--help", "help":
		fmt.Print(curatorUsage)
		return nil
	default:
		fmt.Fprint(os.Stderr, curatorUsage)
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

func busUser() string {
	if u := os.Getenv(lib.EnvBusUser); u != "" {
		return u
	}
	if u := os.Getenv("NATS_USER"); u != "" {
		return u
	}
	return "agent"
}

func connectNATS(ctx context.Context, name string, log *slog.Logger) (*lib.Client, error) {
	natsURL := os.Getenv("NATS_URL")
	if natsURL == "" {
		return nil, errors.New("NATS_URL is not set")
	}
	user := busUser()
	auth := lib.WithUserPassword(user, os.Getenv("NATS_PASSWORD"))
	if path := os.Getenv(lib.EnvBusTokenFile); path != "" {
		auth = lib.WithKSAToken(path, user)
	} else if _, err := os.Stat(lib.BusTokenPath); err == nil {
		auth = lib.WithKSAToken(lib.BusTokenPath, user)
	}
	return lib.Connect(ctx, natsURL,
		lib.WithName("a2a-curator-"+name),
		lib.WithLogger(log),
		auth,
	)
}

func extractRepoSlug(u string) string {
	u = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(u), ".git"))
	for _, host := range []string{"github.com/", "gitlab.com/"} {
		if idx := strings.Index(u, host); idx >= 0 {
			return strings.Trim(u[idx+len(host):], "/")
		}
	}
	return u
}

func defaultRepoID() string {
	if v := os.Getenv("CURATOR_REPO_ID"); v != "" {
		return v
	}
	statePath := os.Getenv("GITOPS_STATE_PATH")
	if statePath == "" {
		statePath = "/etc/gitops/managed_repos"
	}
	if raw, err := os.ReadFile(statePath); err == nil {
		var entries []struct {
			URL string `json:"url"`
		}
		if json.Unmarshal(raw, &entries) == nil && len(entries) > 0 && entries[0].URL != "" {
			return extractRepoSlug(entries[0].URL)
		}
		for _, line := range strings.Split(string(raw), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			fields := strings.Fields(line)
			if len(fields) > 0 {
				return extractRepoSlug(fields[0])
			}
		}
	}
	return ""
}

func defaultRepoDir() string {
	if v := os.Getenv("CURATOR_REPO_DIR"); v != "" {
		return v
	}
	return "/opt/data/memory/knowledge-store"
}

func defaultHindsightURL() string {
	if v := os.Getenv("DECISION_MEMORY_HINDSIGHT_URL"); v != "" {
		return v
	}
	return os.Getenv("HINDSIGHT_API_URL")
}

func runDaemon(args []string) error {
	fs := flag.NewFlagSet("curator daemon", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	threshold := fs.Int("recurrence-threshold", 2, "clusters required to graduate a lesson to a fleet-wide decision")
	if err := fs.Parse(args); err != nil {
		return err
	}

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client, err := connectNATS(ctx, "daemon", log)
	if err != nil {
		return err
	}
	defer client.Close()

	var hs *memory.HindsightClient
	if *hsURL != "" {
		hs = memory.NewHindsightClient(*hsURL, *bank)
	}

	cur, err := memory.NewCurator(memory.CuratorConfig{
		RepoDir:             *repoDir,
		RepoID:              *repoID,
		Session:             busUser(),
		Profile:             os.Getenv("A2A_PROFILE"),
		RecurrenceThreshold: *threshold,
		Client:              client,
		Hindsight:           hs,
		LLM:                 memory.ResolveLLMConfig(),
		Logger:              log,
	})
	if err != nil {
		return err
	}
	if err := cur.Start(ctx); err != nil {
		return err
	}
	defer cur.Stop()

	log.Info("curator daemon started",
		"repo_dir", *repoDir,
		"repo_id", *repoID,
		"hindsight_url", *hsURL,
		"bank", *bank,
	)
	<-ctx.Done()
	log.Info("curator daemon shutting down")
	return nil
}

func runEmit(args []string) error {
	fs := flag.NewFlagSet("curator emit", flag.ContinueOnError)
	kind := fs.String("kind", string(memory.OutcomeProposed), "outcome kind: proposed|approved|modified-merged|resolved-merged|pr-merged-persists|refused|retired")
	recordID := fs.String("id", "", "record ID (for example, DEC-2026-004 or LES-2026-011)")
	recordType := fs.String("type", "", "record type: decision|lesson")
	repo := fs.String("repo", defaultRepoID(), "repository identifier")
	cluster := fs.String("cluster", "", "cluster name")
	checkID := fs.String("check-id", "", "audit check ID")
	findingID := fs.String("finding-id", "", "audit finding ID")
	title := fs.String("title", "", "record title")
	summary := fs.String("summary", "", "human summary text")
	rule := fs.String("rule", "", "guard rule text")
	pr := fs.String("pr", "", "pull request reference")
	remediationPR := fs.String("remediation-pr", "", "operational remediation pull request reference")
	knowledgePR := fs.Bool("knowledge-pr", false, "true when the merged PR is the knowledge/ proposal PR itself")
	reason := fs.String("reason", "", "reviewer or persistence reason")
	initialProposal := fs.String("initial-proposal", "", "superseded initial agent proposal when modified before merge")
	soakWindow := fs.String("soak-window", "", "post-merge soak window (for example, 10m, 24h, or 7d)")
	pathsCSV := fs.String("paths", "", "comma-separated governed file globs")
	guardFile := fs.String("guard-file", "", "guard expect file glob")
	guardPath := fs.String("guard-path", "", "guard expect YAML path")
	guardOp := fs.String("guard-op", "", "guard expect operator")
	guardValue := fs.String("guard-value", "", "guard expect value")
	guardSeverity := fs.String("guard-severity", "warn", "guard severity: warn|block|info")
	jsonSpec := fs.String("json", "", "raw OutcomeEvent JSON (inline or @file)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	var ev memory.OutcomeEvent
	if *jsonSpec != "" {
		raw := []byte(*jsonSpec)
		if strings.HasPrefix(*jsonSpec, "@") {
			var err error
			raw, err = os.ReadFile(strings.TrimPrefix(*jsonSpec, "@"))
			if err != nil {
				return err
			}
		}
		if err := json.Unmarshal(raw, &ev); err != nil {
			return fmt.Errorf("parse --json: %w", err)
		}
	} else {
		var paths []string
		for _, p := range strings.Split(*pathsCSV, ",") {
			if s := strings.TrimSpace(p); s != "" {
				paths = append(paths, s)
			}
		}
		ev = memory.OutcomeEvent{
			Schema:          memory.OutcomeSchema,
			Kind:            memory.OutcomeKind(*kind),
			RecordID:        *recordID,
			RecordType:      memory.RecordType(*recordType),
			Repo:            *repo,
			Cluster:         *cluster,
			CheckID:         *checkID,
			FindingID:       *findingID,
			Title:           *title,
			Summary:         *summary,
			Rule:            *rule,
			PullRequest:     *pr,
			RemediationPR:   *remediationPR,
			KnowledgePR:     *knowledgePR,
			ReviewerReason:  *reason,
			InitialProposal: *initialProposal,
			SoakWindow:      *soakWindow,
		}
		if len(paths) > 0 || *cluster != "" || *checkID != "" {
			ev.Scope = &memory.RecordScope{Paths: paths}
			if *cluster != "" {
				ev.Scope.Clusters = []string{*cluster}
			}
			if *checkID != "" {
				ev.Scope.Checks = []string{*checkID}
			}
		}
		if *guardFile != "" && *guardPath != "" && *guardOp != "" {
			ev.Guard = &memory.RecordGuard{
				Rule:     *rule,
				Severity: *guardSeverity,
				Expect: &memory.GuardExpect{
					File:  *guardFile,
					Path:  *guardPath,
					Op:    *guardOp,
					Value: *guardValue,
				},
			}
		}
	}
	if ev.Schema == "" {
		ev.Schema = memory.OutcomeSchema
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
	client, err := connectNATS(ctx, "emit", log)
	if err != nil {
		return err
	}
	defer client.Close()

	corr := lib.NewCorrelationID()
	outcomeRes, err := memory.PublishOutcome(ctx, client, ev)
	if err != nil {
		return err
	}
	if ev.RecordID == "" && outcomeRes.RecordID != "" {
		ev.RecordID = outcomeRes.RecordID
	}
	if ev.Title == "" && outcomeRes.Title != "" {
		ev.Title = outcomeRes.Title
	}
	out := map[string]any{
		"published":      memory.SubjectCuratorOutcomes,
		"correlation_id": corr,
		"event":          ev,
	}
	if outcomeRes.RecordID != "" {
		out["record_id"] = outcomeRes.RecordID
		out["status"] = outcomeRes.Status
		out["path"] = outcomeRes.Path
		out["branch"] = outcomeRes.Branch
		out["title"] = outcomeRes.Title
		out["pr_markdown"] = outcomeRes.PRMarkdown
		out["document_id"] = outcomeRes.DocumentID
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

func runPending(args []string) error {
	fs := flag.NewFlagSet("curator pending", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory (used as local fallback)")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if os.Getenv("NATS_URL") != "" {
		log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
		if client, err := connectNATS(ctx, "pending", log); err == nil {
			defer client.Close()
			if resp, qErr := memory.QueryViaNATS(ctx, client, memory.MemoryQueryRequest{
				Schema: memory.MemoryQuerySchema,
				Action: "pending",
			}); qErr == nil {
				enc := json.NewEncoder(os.Stdout)
				enc.SetIndent("", "  ")
				return enc.Encode(map[string]any{
					"repo":    *repoID,
					"records": resp.Records,
				})
			}
		}
	}

	records, err := memory.LoadRecords(*repoDir)
	if err != nil {
		return err
	}
	digest := memory.BuildBlueprintDigest(*repoID, records, "local-pending", nil)
	var pending []memory.BlueprintRecordDigest
	for _, r := range digest.Records {
		if r.Status == memory.StatusProposed {
			pending = append(pending, r)
		}
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(map[string]any{
		"repo":    *repoID,
		"records": pending,
	})
}

func runCheck(args []string) error {
	fs := flag.NewFlagSet("curator check", flag.ContinueOnError)
	repoDir := fs.String("repo", ".", "target repository directory")
	knowledgeRepo := fs.String("knowledge-repo", defaultRepoDir(), "directory holding knowledge/ records")
	markdownOnly := fs.Bool("markdown", false, "print only markdown output")
	overseer := fs.Bool("overseer", false, "run LLM Overseer judgment on natural-language active records against --diff-file")
	diffFile := fs.String("diff-file", "", "optional file containing git diff text for --overseer judgment")
	if err := fs.Parse(args); err != nil {
		return err
	}
	kDir := *knowledgeRepo
	if _, err := os.Stat(filepath.Join(*repoDir, "knowledge")); err == nil {
		kDir = *repoDir
	} else if _, err := os.Stat(filepath.Join(*repoDir, ".kube-agents", "intent.yaml")); err == nil {
		kDir = *repoDir
	}
	records, err := memory.LoadRecords(kDir)
	if err != nil {
		return err
	}
	for _, r := range records {
		if errs := memory.ValidateRecord(r); len(errs) > 0 && r.Meta.Status == memory.StatusActive {
			return fmt.Errorf("invalid active record %s (%s): %s", r.Meta.ID, r.Path, strings.Join(errs, "; "))
		}
	}
	changed := map[string]*string{}
	for _, rel := range fs.Args() {
		full := filepath.Join(*repoDir, rel)
		if raw, err := os.ReadFile(full); err == nil {
			s := string(raw)
			changed[filepath.ToSlash(rel)] = &s
		} else {
			changed[filepath.ToSlash(rel)] = nil
		}
	}
	report := memory.CheckChangedFiles(records, changed)
	if *overseer && *diffFile != "" {
		if diffRaw, err := os.ReadFile(*diffFile); err == nil && len(diffRaw) > 0 {
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			llmCfg := memory.ResolveLLMConfig()
			for _, v := range llmCfg.JudgeDiff(ctx, records, string(diffRaw)) {
				sev := v.Severity
				if sev == "" {
					sev = "block"
				}
				report.Findings = append(report.Findings, memory.CheckFinding{
					ID:       v.ID,
					Type:     memory.RecordTypeDecision,
					Status:   memory.StatusActive,
					Title:    v.Rule,
					Verdict:  "violates",
					Severity: sev,
					Guard: &memory.RecordGuard{
						Rule:     v.Rule,
						Severity: sev,
					},
					Body: v.Reason,
				})
				if sev == "block" {
					report.Verdict = "block"
					report.Status = "found"
				}
			}
		}
	}
	if *markdownOnly {
		fmt.Println(report.Markdown)
	} else {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(report)
	}
	if report.Verdict == "block" {
		return &exitCodeError{code: 2}
	}
	return nil
}

func runIndex(args []string) error {
	fs := flag.NewFlagSet("curator index", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *hsURL == "" {
		return errors.New("--hindsight-url or HINDSIGHT_API_URL is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	hs := memory.NewHindsightClient(*hsURL, *bank)
	if err := hs.EnsureBank(ctx); err != nil {
		return err
	}
	records, err := memory.LoadRecords(*repoDir)
	if err != nil {
		return err
	}
	var upserted []string
	for _, r := range records {
		if len(memory.ValidateRecord(r)) > 0 {
			continue
		}
		if err := hs.RetainRecord(ctx, *repoID, r); err != nil {
			return err
		}
		upserted = append(upserted, r.Meta.ID)
	}
	if extRecords, err := memory.LoadExternalDocRecords(*repoDir); err == nil {
		for _, r := range extRecords {
			if err := hs.RetainRecord(ctx, *repoID, r); err != nil {
				return err
			}
			upserted = append(upserted, r.Meta.ID)
		}
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(map[string]any{
		"bank":     *bank,
		"repo":     *repoID,
		"upserted": upserted,
	})
}

func runRecall(args []string) error {
	fs := flag.NewFlagSet("curator recall", flag.ContinueOnError)
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL (used when --direct-hindsight is set or NATS_URL is unset)")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	tagsCSV := fs.String("tags", memory.SharedTag, "comma-separated tags filter")
	tagsMatch := fs.String("tags-match", "all_strict", "tags match mode (all_strict|any_strict)")
	pathFilter := fs.String("path", "", "optional repository path filter")
	clusterFilter := fs.String("cluster", "", "optional cluster filter")
	queryFlag := fs.String("query", "", "optional query string (alternative to positional args)")
	directHS := fs.Bool("direct-hindsight", false, "query Hindsight HTTP REST directly instead of NATS request-reply")
	if err := fs.Parse(args); err != nil {
		return err
	}
	query := strings.TrimSpace(*queryFlag)
	if query == "" {
		query = strings.TrimSpace(strings.Join(fs.Args(), " "))
	}
	if query == "" && *pathFilter == "" && *clusterFilter == "" {
		return errors.New("recall requires a query string, --path, or --cluster")
	}
	var tags []string
	for _, t := range strings.Split(*tagsCSV, ",") {
		if s := strings.TrimSpace(t); s != "" {
			tags = append(tags, s)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if !*directHS && os.Getenv("NATS_URL") != "" {
		log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
		client, err := connectNATS(ctx, "recall", log)
		if err == nil {
			defer client.Close()
			resp, qErr := memory.QueryViaNATS(ctx, client, memory.MemoryQueryRequest{
				Schema:    memory.MemoryQuerySchema,
				Query:     query,
				Bank:      *bank,
				Tags:      tags,
				TagsMatch: *tagsMatch,
				Path:      *pathFilter,
				Cluster:   *clusterFilter,
			})
			if qErr == nil {
				enc := json.NewEncoder(os.Stdout)
				enc.SetIndent("", "  ")
				return enc.Encode(resp)
			}
			if *hsURL == "" {
				return qErr
			}
		} else if *hsURL == "" {
			return err
		}
	}

	if *hsURL == "" {
		return errors.New("NATS_URL or --hindsight-url (HINDSIGHT_API_URL) is required")
	}
	hs := memory.NewHindsightClient(*hsURL, *bank)
	results, err := hs.Recall(ctx, query, tags, *tagsMatch)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(memory.MemoryQueryResponse{
		Schema:  memory.MemoryQueryReplySchema,
		Backend: "hindsight-direct",
		Bank:    *bank,
		Query:   query,
		Tags:    tags,
		Cached:  false,
		Results: results,
	})
}
