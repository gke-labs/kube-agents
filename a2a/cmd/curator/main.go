// Command curator is the Go OKF Curator and Dreamer daemon and CLI for kube-agents.
// It subscribes to NATS JetStream (a2a.topics.shared.annotations on TOPICS-JOURNAL
// and a2a.topics.agent.platform.curator-outcomes), synthesizes and updates Open Knowledge
// Format (OKF) records (DEC-* and LES-*), indexes them verbatim into Hindsight
// (kube-agents-decisions) with in-place status transitions, graduates multi-cluster
// recurrences into fleet-wide decisions, and broadcasts the active digest to
// a2a.topics.shared.blueprint on TOPICS-STATE.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/memory"
	"github.com/nats-io/nats.go"
)

const (
	defaultCLIStoreDir           = "/opt/data/memory/knowledge-store"
	defaultAgentHomeDir          = "/opt/data"
	defaultManagedReposPath      = "/etc/gitops/managed_repos"
	hindsightConfigSubdir        = "hindsight"
	hindsightConfigFilename      = "config.json"
	hindsightConfigEndpointField = "api_url"
	hindsightConfigFallbackField = "url"
	envDecisionHindsightURL      = "DECISION_MEMORY_HINDSIGHT_URL"
	envHindsightAPIURL           = "HINDSIGHT_API_URL"
	envPlatformAgentHome         = "PLATFORM_AGENT_HOME"
	flagNameRepoID               = "repo-id"
	flagNameGuardSeverity        = "guard-severity"
	defaultRecurrenceFloor       = 2
	cliOpTimeout                 = 30 * time.Second
	indexOpTimeout               = 60 * time.Second
	dreamOpTimeout               = 90 * time.Second
	overseerTimeout              = 15 * time.Second
	maxCheckFileBytes            = 4 << 20
	statusReadHeaderTimeout      = 5 * time.Second
	exitCodeBlocked              = 2
	exitCodeNoResponders         = 2
)

const curatorUsage = `curator - Go OKF Decision & Lesson Memory Curator and Dreamer for kube-agents

usage:
  curator daemon [flags]         subscribe to SubjectCuratorOutcomes, index into Hindsight, serve NATS memory queries, and publish blueprint digests
  curator dream [flags]          audit active guards against HEAD, graduate cross-cluster recurring lessons, and sync into Hindsight
  curator emit [flags]           publish a kube-agents.memory.outcome/v1 event and return the synthesized record metadata
  curator pending [flags]        list status:proposed OKF records awaiting pull request creation or review
  curator check [flags] [-- ...] evaluate active and proposed OKF guards against changed files (exits 2 on block)
  curator index [flags]          sync local knowledge/ records into the kube-agents-decisions Hindsight bank
  curator recall [flags] <query> query semantic memory over NATS (with fallback to Hindsight REST)

environment:
  NATS_URL            NATS JetStream bus URL
  A2A_BUS_USER        bus principal (falls back to NATS_USER; exits "no bus identity" when both are unset)
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
	case "dream", "bootstrap":
		return runDream(args[1:])
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
	if u := strings.TrimSpace(os.Getenv(lib.EnvBusUser)); u != "" {
		return u
	}
	return strings.TrimSpace(os.Getenv("NATS_USER"))
}

func readBoundedViaRoot(root *os.Root, rel string) ([]byte, error) {
	f, err := root.Open(rel)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxCheckFileBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > maxCheckFileBytes {
		return nil, fmt.Errorf("file %s exceeds %d-byte limit", rel, maxCheckFileBytes)
	}
	return raw, nil
}

func readFileViaDirRoot(fullPath string) ([]byte, error) {
	clean := filepath.Clean(fullPath)
	root, err := os.OpenRoot(filepath.Dir(clean))
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return readBoundedViaRoot(root, filepath.Base(clean))
}

func connectNATS(ctx context.Context, name string, log *slog.Logger) (*lib.Client, error) {
	natsURL := os.Getenv("NATS_URL")
	if natsURL == "" {
		return nil, errors.New("NATS_URL is not set")
	}
	user := busUser()
	if user == "" {
		return nil, fmt.Errorf("no bus identity: set %s or NATS_USER", lib.EnvBusUser)
	}
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
	return memory.ExtractRepoSlug(u)
}

func defaultRepoID() string {
	if v := strings.TrimSpace(os.Getenv("CURATOR_REPO_ID")); v != "" {
		return v
	}
	statePath := strings.TrimSpace(os.Getenv("GITOPS_STATE_PATH"))
	if statePath == "" {
		statePath = defaultManagedReposPath
	}
	if raw, err := readFileViaDirRoot(statePath); err == nil {
		var entries []struct {
			URL  string `json:"url"`
			Role string `json:"role"`
		}
		if json.Unmarshal(raw, &entries) == nil && len(entries) > 0 {
			for _, e := range entries {
				if strings.EqualFold(strings.TrimSpace(e.Role), "gitops") && strings.TrimSpace(e.URL) != "" {
					return extractRepoSlug(e.URL)
				}
			}
			for _, e := range entries {
				if strings.TrimSpace(e.URL) != "" {
					return extractRepoSlug(e.URL)
				}
			}
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
	if v := strings.TrimSpace(os.Getenv("CURATOR_REPO_DIR")); v != "" {
		return v
	}
	return defaultCLIStoreDir
}

func defaultHindsightURL() string {
	if v := strings.TrimSpace(os.Getenv(envDecisionHindsightURL)); v != "" {
		return v
	}
	if v := strings.TrimSpace(os.Getenv(envHindsightAPIURL)); v != "" {
		return v
	}
	agentHome := strings.TrimRight(strings.TrimSpace(os.Getenv(envPlatformAgentHome)), "/")
	if agentHome == "" {
		agentHome = defaultAgentHomeDir
	}
	cfgPath := filepath.Join(filepath.Clean(agentHome), hindsightConfigSubdir, hindsightConfigFilename)
	if raw, err := readFileViaDirRoot(cfgPath); err == nil && strings.TrimSpace(string(raw)) != "" {
		var parsed map[string]any
		if json.Unmarshal(raw, &parsed) == nil {
			for _, field := range []string{hindsightConfigEndpointField, hindsightConfigFallbackField} {
				if u, ok := parsed[field].(string); ok && strings.TrimSpace(u) != "" {
					return strings.TrimSpace(u)
				}
			}
		}
	}
	return ""
}

func runDaemon(args []string) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return runDaemonContext(ctx, args)
}

func runDaemonContext(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("curator daemon", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	threshold := fs.Int("recurrence-threshold", defaultRecurrenceFloor, "clusters required to graduate a lesson to a fleet-wide decision")
	statusListen := fs.String("status-listen", strings.TrimSpace(os.Getenv("CURATOR_STATUS_LISTEN")), "optional HTTP address serving /healthz, /readyz, and /metrics (for example, :8080)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

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
		VerifiedRepoDir:     strings.TrimSpace(os.Getenv("CURATOR_VERIFIED_REPO_DIR")),
		BrokerURL:           strings.TrimSpace(os.Getenv("CREDENTIAL_PROXY_URL")),
		BrokerTokenFile:     strings.TrimSpace(os.Getenv("CREDENTIAL_PROXY_TOKEN_FILE")),
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

	var ready atomic.Bool
	var statusSrv *http.Server
	if *statusListen != "" {
		ln, lnErr := net.Listen("tcp", *statusListen)
		if lnErr != nil {
			return fmt.Errorf("listen status %s: %w", *statusListen, lnErr)
		}
		mux := http.NewServeMux()
		mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ok\n"))
		})
		mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
			if !ready.Load() {
				http.Error(w, "not ready", http.StatusServiceUnavailable)
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ready\n"))
		})
		mux.Handle("/metrics", cur.MetricsHandler())
		statusSrv = &http.Server{
			Handler:           mux,
			ReadHeaderTimeout: statusReadHeaderTimeout,
		}
		go func() {
			if sErr := statusSrv.Serve(ln); sErr != nil && !errors.Is(sErr, http.ErrServerClosed) {
				log.Error("curator status server failed", "err", sErr)
			}
		}()
		defer func() {
			shCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			_ = statusSrv.Shutdown(shCtx)
			_ = statusSrv.Close()
		}()
	}

	if err := cur.Start(ctx); err != nil {
		return err
	}
	ready.Store(true)
	defer func() {
		ready.Store(false)
		cur.Stop()
	}()

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
	commitSHA := fs.String("commit", "", "commit SHA associated with the proposal or outcome")
	knowledgePR := fs.Bool("knowledge-pr", false, "true when the merged PR is the knowledge/ proposal PR itself")
	reason := fs.String("reason", "", "reviewer or persistence reason")
	initialProposal := fs.String("initial-proposal", "", "superseded initial agent proposal when modified before merge")
	soakWindow := fs.String("soak-window", "", "post-merge soak window (for example, 10m, 24h, or 7d)")
	outcome := fs.String("outcome", "", "evidence outcome marker (for example, backfill)")
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
	guardSeverityExplicit := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == flagNameGuardSeverity {
			guardSeverityExplicit = true
		}
	})

	var ev memory.OutcomeEvent
	if *jsonSpec != "" {
		raw := []byte(*jsonSpec)
		if strings.HasPrefix(*jsonSpec, "@") {
			var err error
			raw, err = readFileViaDirRoot(strings.TrimPrefix(*jsonSpec, "@"))
			if err != nil {
				return err
			}
		}
		if err := json.Unmarshal(raw, &ev); err != nil {
			return fmt.Errorf("parse --json: %w", err)
		}
		if strings.TrimSpace(*outcome) != "" && ev.Outcome == "" {
			ev.Outcome = strings.TrimSpace(*outcome)
		}
		if strings.TrimSpace(*commitSHA) != "" && ev.Commit == "" {
			ev.Commit = strings.TrimSpace(*commitSHA)
		}
		if guardSeverityExplicit {
			ev.GuardSeverity = strings.TrimSpace(*guardSeverity)
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
			Outcome:         strings.TrimSpace(*outcome),
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
			Commit:          strings.TrimSpace(*commitSHA),
			KnowledgePR:     *knowledgePR,
			ReviewerReason:  *reason,
			InitialProposal: *initialProposal,
			SoakWindow:      *soakWindow,
		}
		if guardSeverityExplicit {
			ev.GuardSeverity = strings.TrimSpace(*guardSeverity)
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

	ctx, cancel := context.WithTimeout(context.Background(), cliOpTimeout)
	defer cancel()
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
	client, err := connectNATS(ctx, "emit", log)
	if err != nil {
		return err
	}
	defer client.Close()

	corr := lib.NewCorrelationID()
	outcomeRes, publishedSubj, err := memory.PublishOutcomeWithMeta(ctx, client, ev, corr)
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
		"published":      publishedSubj,
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
	defaultRepo := defaultRepoID()
	fs := flag.NewFlagSet("curator pending", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory (used as local fallback)")
	repoID := fs.String(flagNameRepoID, defaultRepo, "canonical repository identifier")
	if err := fs.Parse(args); err != nil {
		return err
	}
	repoIDExplicit := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == flagNameRepoID {
			repoIDExplicit = true
		}
	})
	wantRepo := ""
	if repoIDExplicit {
		wantRepo = strings.TrimSpace(*repoID)
	}
	ctx, cancel := context.WithTimeout(context.Background(), cliOpTimeout)
	defer cancel()

	if os.Getenv("NATS_URL") != "" {
		log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
		if client, err := connectNATS(ctx, "pending", log); err == nil {
			defer client.Close()
			if resp, qErr := memory.QueryViaNATS(ctx, client, memory.MemoryQueryRequest{
				Schema: memory.MemoryQuerySchema,
				Action: "pending",
				Repo:   wantRepo,
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
		if r.Status != memory.StatusProposed {
			continue
		}
		if wantRepo != "" {
			effRepo := strings.TrimSpace(r.Repo)
			if effRepo == "" {
				effRepo = strings.TrimSpace(defaultRepo)
			}
			if effRepo != "" && !memory.SameRepoSlug(effRepo, wantRepo) {
				continue
			}
		}
		pending = append(pending, r)
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
	if *overseer && strings.TrimSpace(*diffFile) == "" {
		return errors.New("--overseer requires --diff-file")
	}

	repoRoot, err := os.OpenRoot(*repoDir)
	if err != nil {
		return fmt.Errorf("open repo %s: %w", *repoDir, err)
	}
	defer repoRoot.Close()

	knowledgeRepoExplicit := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "knowledge-repo" {
			knowledgeRepoExplicit = true
		}
	})

	kDir := *knowledgeRepo
	if !knowledgeRepoExplicit {
		if _, err := repoRoot.Stat("knowledge"); err == nil {
			kDir = *repoDir
		} else if _, err := repoRoot.Stat(filepath.Join(".kube-agents", "intent.yaml")); err == nil {
			kDir = *repoDir
		}
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
		if rel == "--" {
			continue
		}
		cleanRel := strings.TrimPrefix(filepath.ToSlash(filepath.Clean(rel)), "./")
		if cleanRel == "." || cleanRel == "" || strings.HasPrefix(cleanRel, "/") || cleanRel == ".." || strings.HasPrefix(cleanRel, "../") || strings.Contains(cleanRel, "/../") {
			return fmt.Errorf("invalid changed file path %q", rel)
		}
		raw, err := readBoundedViaRoot(repoRoot, filepath.FromSlash(cleanRel))
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				changed[cleanRel] = nil
				continue
			}
			return fmt.Errorf("read changed file %s: %w", cleanRel, err)
		}
		s := string(raw)
		changed[cleanRel] = &s
	}
	report := memory.CheckChangedFiles(records, changed)
	if *overseer {
		diffRaw, err := readFileViaDirRoot(*diffFile)
		if err != nil {
			return fmt.Errorf("read --diff-file %s: %w", *diffFile, err)
		}
		if len(diffRaw) > 0 {
			ctx, cancel := context.WithTimeout(context.Background(), overseerTimeout)
			defer cancel()
			llmCfg := memory.ResolveLLMConfig()
			byID := map[string]memory.Record{}
			for _, r := range records {
				byID[r.Meta.ID] = r
			}
			appended := false
			for _, v := range llmCfg.JudgeDiff(ctx, records, string(diffRaw)) {
				rec, ok := byID[v.ID]
				if !ok {
					continue
				}
				// Advisory LLM findings are capped at "warn" (memory.md Stage A/B); only
				// deterministic scope / guard.expect checks may block.
				sev := "warn"
				if strings.EqualFold(strings.TrimSpace(v.Severity), "info") {
					sev = "info"
				}
				finding := memory.CheckFinding{
					ID:          v.ID,
					Type:        rec.Meta.Type,
					Status:      memory.StatusActive,
					Title:       rec.Meta.Title,
					Path:        rec.Path,
					Verdict:     "violates",
					Severity:    sev,
					Files:       report.Changed,
					PullRequest: rec.Meta.Evidence.PullRequest,
					BreakingPR:  rec.Meta.Evidence.BreakingPR,
					Incident:    rec.Meta.Evidence.Incident,
					Supersedes:  rec.Meta.Supersedes,
					Guard: &memory.RecordGuard{
						Rule:     v.Rule,
						Severity: sev,
					},
					Body: v.Reason,
				}
				replaced := false
				for i := range report.Findings {
					if report.Findings[i].ID == v.ID && report.Findings[i].Verdict == "related" {
						report.Findings[i] = finding
						replaced = true
						break
					}
				}
				if !replaced {
					report.Findings = append(report.Findings, finding)
				}
				appended = true
			}
			if appended {
				report = memory.BuildCheckReport(report.Changed, report.Findings)
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
		return &exitCodeError{code: exitCodeBlocked}
	}
	return nil
}

func runIndex(args []string) error {
	fs := flag.NewFlagSet("curator index", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	role := fs.String("role", memory.RoleManaged, "repository role: managed|context")
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	if err := fs.Parse(args); err != nil {
		return err
	}
	normRole := strings.ToLower(strings.TrimSpace(*role))
	if normRole == "" {
		normRole = memory.RoleManaged
	}
	if normRole != memory.RoleManaged && normRole != memory.RoleContext {
		return fmt.Errorf("invalid --role %q: must be managed or context", *role)
	}

	records, err := memory.LoadRecords(*repoDir)
	if err != nil {
		return err
	}
	var allRecords []memory.Record
	for _, r := range records {
		if normRole == memory.RoleManaged {
			if len(memory.ValidateRecord(r)) == 0 {
				allRecords = append(allRecords, r)
			}
		} else if strings.TrimSpace(r.Meta.ID) != "" && strings.TrimSpace(r.Meta.Title) != "" {
			allRecords = append(allRecords, r)
		}
	}
	extRecords, _ := memory.LoadSingleRepoExternalDocRecords(*repoDir)
	allRecords = append(allRecords, extRecords...)
	normalized := memory.NormalizeRepoRecords(*repoID, normRole, allRecords)

	writeReceipt := func(backend, bankID, revision string, upserted, deleted []string) error {
		if upserted == nil {
			upserted = []string{}
		}
		if deleted == nil {
			deleted = []string{}
		}
		out := map[string]any{
			"backend":  backend,
			"repo":     *repoID,
			"repo_id":  *repoID,
			"role":     normRole,
			"indexed":  len(upserted),
			"upserted": upserted,
			"deleted":  deleted,
			"revision": revision,
		}
		if bankID != "" {
			out["bank"] = bankID
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(out)
	}

	opCtx, cancel := context.WithTimeout(context.Background(), indexOpTimeout)
	defer cancel()

	if os.Getenv("NATS_URL") != "" {
		log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
		client, cErr := connectNATS(opCtx, "index", log)
		if cErr == nil {
			defer client.Close()
			var resp memory.MemoryQueryResponse
			var nErr error
			if normRole == memory.RoleManaged {
				resp, nErr = memory.IndexViaNATS(opCtx, client, memory.MemoryQueryRequest{
					Schema: memory.MemoryQuerySchema,
					Action: "index",
					Bank:   *bank,
					Repo:   *repoID,
					Role:   memory.RoleManaged,
				})
				if nErr != nil && strings.Contains(nErr.Error(), memory.ErrBrokerNotConfigured) {
					resp, nErr = memory.IndexViaNATS(opCtx, client, memory.MemoryQueryRequest{
						Schema:    memory.MemoryQuerySchema,
						Action:    "index",
						Bank:      *bank,
						Repo:      *repoID,
						Role:      memory.RoleManaged,
						FromLocal: true,
						Records:   normalized,
					})
				}
			} else {
				resp, nErr = memory.IndexViaNATS(opCtx, client, memory.MemoryQueryRequest{
					Schema:    memory.MemoryQuerySchema,
					Action:    "index",
					Bank:      *bank,
					Repo:      *repoID,
					Role:      normRole,
					FromLocal: true,
					Records:   normalized,
				})
			}
			if nErr == nil {
				backend := resp.Backend
				if backend == "" {
					backend = "curator"
				}
				return writeReceipt(backend, *bank, resp.Revision, resp.Upserted, resp.Deleted)
			}
			canFallback := errors.Is(nErr, nats.ErrNoResponders) || errors.Is(nErr, nats.ErrMaxPayload)
			if !canFallback || *hsURL == "" {
				return nErr
			}
		} else if *hsURL == "" {
			return cErr
		}
	}

	if *hsURL != "" {
		hs := memory.NewHindsightClient(*hsURL, *bank)
		upserted, deleted, syncErr := memory.SyncRecordListToHindsight(opCtx, hs, *repoID, normalized)
		if syncErr != nil {
			return syncErr
		}
		return writeReceipt("hindsight", *bank, "", upserted, deleted)
	}

	localIDs := make([]string, 0, len(normalized))
	for _, r := range normalized {
		localIDs = append(localIDs, r.Meta.ID)
	}
	return writeReceipt("okf-local", "", "", localIDs, []string{})
}

func runDream(args []string) error {
	fs := flag.NewFlagSet("curator dream", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	threshold := fs.Int("recurrence-threshold", defaultRecurrenceFloor, "clusters required to graduate a lesson to a fleet-wide decision")
	localOnly := fs.Bool("local", false, "run dream locally against --repo without calling the Curator daemon over NATS")
	natsOnly := fs.Bool("nats-only", false, "require the Curator daemon over NATS and do not fall back to local --repo")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *localOnly && *natsOnly {
		return errors.New("--local and --nats-only cannot both be set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), dreamOpTimeout)
	defer cancel()

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
	var client *lib.Client
	defer func() {
		if client != nil {
			client.Close()
		}
	}()
	if !*localOnly && (os.Getenv("NATS_URL") != "" || *natsOnly) && (strings.TrimSpace(*repoID) != "" || *natsOnly) {
		c, cErr := connectNATS(ctx, "dream", log)
		if cErr == nil {
			client = c
			resp, dErr := memory.DreamViaNATS(ctx, client, memory.MemoryQueryRequest{
				Schema: memory.MemoryQuerySchema,
				Action: "dream",
				Bank:   *bank,
				Repo:   strings.TrimSpace(*repoID),
			})
			if dErr == nil {
				report := memory.DreamReport{
					Repo:           strings.TrimSpace(*repoID),
					Revision:       resp.Revision,
					VerifiedActive: []string{},
					Upserted:       resp.Upserted,
					Deleted:        resp.Deleted,
					Silent:         true,
				}
				if resp.Dream != nil {
					report = *resp.Dream
				}
				enc := json.NewEncoder(os.Stdout)
				enc.SetIndent("", "  ")
				return enc.Encode(report)
			}
			if errors.Is(dErr, nats.ErrNoResponders) {
				if *natsOnly {
					fmt.Fprintln(os.Stderr, "curator dream: no responders")
					return &exitCodeError{code: exitCodeNoResponders}
				}
			} else {
				return dErr
			}
		} else if *natsOnly {
			return cErr
		}
	}

	var hs *memory.HindsightClient
	if *hsURL != "" {
		hs = memory.NewHindsightClient(*hsURL, *bank)
	}
	if client == nil && !*localOnly && os.Getenv("NATS_URL") != "" {
		if c, err := connectNATS(ctx, "dream", log); err == nil {
			client = c
		}
	}
	cur, err := memory.NewCurator(memory.CuratorConfig{
		RepoDir:             *repoDir,
		VerifiedRepoDir:     strings.TrimSpace(os.Getenv("CURATOR_VERIFIED_REPO_DIR")),
		BrokerURL:           strings.TrimSpace(os.Getenv("CREDENTIAL_PROXY_URL")),
		BrokerTokenFile:     strings.TrimSpace(os.Getenv("CREDENTIAL_PROXY_TOKEN_FILE")),
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
	report, err := cur.RunDream(ctx)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(report)
}

func runRecall(args []string) error {
	fs := flag.NewFlagSet("curator recall", flag.ContinueOnError)
	repoDir := fs.String("repo", defaultRepoDir(), "knowledge repository directory (used to enforce --cluster and --path scope filters)")
	repoID := fs.String("repo-id", defaultRepoID(), "canonical repository identifier")
	hsURL := fs.String("hindsight-url", defaultHindsightURL(), "Hindsight API URL (used when --direct-hindsight is set or NATS_URL is unset)")
	bank := fs.String("bank", memory.DefaultBankID, "Hindsight bank ID")
	tagsCSV := fs.String("tags", memory.SharedTag, "comma-separated tags filter")
	tagsMatch := fs.String("tags-match", "all_strict", "tags match mode (all_strict|any_strict)")
	pathFilter := fs.String("path", "", "optional repository path filter")
	clusterFilter := fs.String("cluster", "", "optional cluster filter")
	maxResults := fs.Int("max-results", 0, "optional maximum number of recall results")
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
	ctx, cancel := context.WithTimeout(context.Background(), cliOpTimeout)
	defer cancel()

	if !*directHS && os.Getenv("NATS_URL") != "" {
		log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
		client, err := connectNATS(ctx, "recall", log)
		if err == nil {
			defer client.Close()
			resp, qErr := memory.QueryViaNATS(ctx, client, memory.MemoryQueryRequest{
				Schema:     memory.MemoryQuerySchema,
				Query:      query,
				Bank:       *bank,
				Tags:       tags,
				TagsMatch:  *tagsMatch,
				Path:       *pathFilter,
				Cluster:    *clusterFilter,
				MaxResults: *maxResults,
			})
			if qErr == nil {
				resp.Results = memory.FrameRecallResultsForRepo(resp.Results, *repoID, nil, nil)
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
	hsQuery := query
	if hsQuery == "" {
		if *pathFilter != "" {
			hsQuery = *pathFilter
		} else {
			hsQuery = *clusterFilter
		}
	}
	hs := memory.NewHindsightClient(*hsURL, *bank)
	results, err := hs.RecallBank(ctx, *bank, hsQuery, tags, *tagsMatch)
	if err != nil {
		return err
	}
	byID := map[string]memory.Record{}
	byDocID := map[string]memory.Record{}
	if localRecs, lErr := memory.LoadRecords(*repoDir); lErr == nil {
		for _, r := range localRecs {
			if strings.TrimSpace(r.Meta.Repo) == "" && strings.TrimSpace(*repoID) != "" {
				r.Meta.Repo = strings.TrimSpace(*repoID)
			}
			recRepo := strings.TrimSpace(r.Meta.Repo)
			if recRepo == "" {
				recRepo = strings.TrimSpace(*repoID)
			}
			if recRepo == "" || *repoID == "" || memory.SameRepoSlug(recRepo, *repoID) {
				byID[r.Meta.ID] = r
			}
			byDocID[r.DocumentID(recRepo)] = r
		}
	}
	if extRecs, eErr := memory.LoadExternalDocRecords(*repoDir); eErr == nil {
		for _, r := range extRecs {
			if strings.TrimSpace(r.Meta.Repo) == "" && strings.TrimSpace(*repoID) != "" {
				r.Meta.Repo = strings.TrimSpace(*repoID)
			}
			recRepo := strings.TrimSpace(r.Meta.Repo)
			if recRepo == "" {
				recRepo = strings.TrimSpace(*repoID)
			}
			if recRepo == "" || *repoID == "" || memory.SameRepoSlug(recRepo, *repoID) {
				byID[r.Meta.ID] = r
			}
			byDocID[r.DocumentID(recRepo)] = r
		}
	}
	results = memory.FilterRecallResultsForRepo(results, *repoID, byID, byDocID, *clusterFilter, *pathFilter)
	if *maxResults > 0 && len(results) > *maxResults {
		results = results[:*maxResults]
	}
	results = memory.FrameRecallResultsForRepo(results, *repoID, byID, byDocID)
	if results == nil {
		results = []memory.HindsightRecallResult{}
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
