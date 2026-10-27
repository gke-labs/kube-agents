/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Command web-console serves a browser chat page for the Platform Agent from
// inside the cluster, for installs that have no Slack or Google Chat yet.
//
// It reaches one upstream, the Hermes gateway API on the agent Service
// (:8642), and holds the gateway's API key server-side; the key never reaches
// the browser. The console has no login of its own. Its access boundary is the
// one the chart builds around it: a ClusterIP Service, a deny-all ingress
// NetworkPolicy, and therefore `kubectl port-forward`, which needs
// pods/portforward on the namespace. Three checks in this file keep that
// boundary from being bypassed through the operator's own browser:
//
//   - every route but /healthz refuses a Host header that is not loopback,
//     which defeats DNS rebinding against the forwarded port;
//   - POST /api/chat and /api/chat/stream require a custom header and a
//     JSON body, so a page on another origin cannot send a turn without a
//     CORS preflight this server never answers;
//   - every response forbids framing, so another page cannot overlay the
//     console and trick a click into sending a turn.
//
// Each console thread has its own Hermes session ID with a "web-console-"
// prefix, minted by the page. A turn may also name an event triage or
// scheduled session, to reply in the thread the cluster opened. That ID is
// checked against Hermes' record of the session (source and title) before
// the turn, and such a session is never created or recreated here. Slack,
// Google Chat and every other caller's session are refused.
//
// A turn reaches the Planning Agent, the same front door a chat message
// reaches. When it delegates work to a kanban card, the card's result arrives
// later as a new assistant message on the same session, after the turn's own
// reply has been returned. The page polls GET /api/sessions/{id}/messages for
// those, through a route limited to this console's own session IDs.
//
// Three read routes sit beside the chat. GET /api/insights (insights.go)
// returns the model, LiteLLM's token and spend counters, the agent's identity
// as recorded at install, and the cluster. GET /api/sessions/{id}/transcript
// (activity.go) returns the text of an event triage, scheduled check or
// console session, and refuses every other session, Slack and Google Chat
// included. GET /api/channels/{name}/posts (channels.go) lists the event
// triage sessions as #alerts and the scheduled checks as #scheduled.
//
// POST /api/chat/stream (stream.go) runs the same turn as POST /api/chat and
// relays one status line per agent step as server-sent events, then the
// reply. The page falls back to /api/chat when the route is missing.
//
// The console is not read-only. GET /api/environment (environment.go) and
// GET /api/release (assist.go) read the cluster and the published releases;
// POST /api/assist sends a feedback draft to the inference gateway; and
// /api/repositories, /api/patches and /api/models each have a POST form as
// well. Every POST route takes the loopback Host check and the console request
// header.
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	defaultListenPort = "8080"
	defaultHermesURL  = "http://127.0.0.1:8642"

	envListenPort   = "PORT"
	envHermesURL    = "HERMES_URL"
	envAPIServerKey = "API_SERVER_KEY"
	envClusterName  = "CLUSTER_NAME"
	envProjectID    = "PROJECT_ID"
	envLocation     = "LOCATION"

	// The model, usage and identity sources the insights banner reads; see
	// insights.go.
	envModelName        = "MODEL_NAME"
	envModelProvider    = "MODEL_PROVIDER"
	envLiteLLMPeersHost = "LITELLM_PEERS_HOST"
	envAgentKSA         = "AGENT_KSA"
	envAgentGSA         = "AGENT_GSA"
	envAgentRoles       = "AGENT_ROLES"
	envChatPlatform     = "CHAT_PLATFORM"
	// agentRolesSeparator splits AGENT_ROLES, which the chart joins with it.
	agentRolesSeparator = ","
	// envGitOpsRepos is a JSON list of the repositories declared at install
	// ([{"forge","provider","host","repository","role"}]), rendered by the
	// chart from platformAgent.integration. The page lists them as the
	// targets the agent can open pull requests on; nothing here grants
	// access, the credential proxy does.
	envGitOpsRepos = "GITOPS_REPOS"
	// maxGitOpsRepos bounds the list read from the environment.
	maxGitOpsRepos = 50

	// turnTimeout matches the in-tree callers of the same endpoint
	// (session_kv_server.py's _start_agent_turn): a diagnostic turn that
	// calls tools routinely runs for minutes.
	turnTimeout = 300 * time.Second
	// serverWriteTimeout must outlast a turn or a streamed turn, or the server
	// cuts the response off while Hermes is still answering.
	serverWriteTimeout   = streamTurnCeiling + streamWriteGrace + 30*time.Second
	serverReadTimeout    = 30 * time.Second
	serverHeaderTimeout  = 10 * time.Second
	serverIdleTimeout    = 120 * time.Second
	shutdownTimeout      = 5 * time.Second
	probeTimeout         = 3 * time.Second
	sessionCreateTimeout = 15 * time.Second
	sessionListTimeout   = 10 * time.Second
	messagesTimeout      = 10 * time.Second
	sessionLookupTimeout = 10 * time.Second

	// defaultDelegationPollInterval is how often runTurn polls session
	// messages while waiting for a kanban_create card filed during the turn
	// to reach a terminal status.
	defaultDelegationPollInterval = 1 * time.Second
	// defaultDelegationSynthesisGracePolls is how many extra polls
	// awaitDelegatedKanbanReply waits after all cards reach a terminal status
	// for the Planning Agent's wake turn to post its synthesis reply before
	// falling back to the raw card result summaries.
	defaultDelegationSynthesisGracePolls = 4

	// agentBusyWindow is how recently an agent session must have been
	// active for an unanswered newest message to count as a turn in
	// progress. Older than this, the turn is taken to have died, and a
	// reply is let through.
	agentBusyWindow = 10 * time.Minute
	// agentBusyPageLimit is how many of the newest messages the busy check
	// reads.
	agentBusyPageLimit = 5
	agentBusyDetail    = "The agent is still working on this thread; try again once it has answered, or after the 10-minute window passes."

	// maxRequestBodyBytes bounds a chat request. A turn is a typed message,
	// and the container's memory limit is 128Mi.
	maxRequestBodyBytes = 64 << 10
	// maxUpstreamErrorBytes bounds how much of a failed upstream response is
	// read back for the error message.
	maxUpstreamErrorBytes = 4 << 10
	// maxUpstreamBodyBytes bounds a successful upstream response.
	maxUpstreamBodyBytes = 8 << 20

	recentSessionsLimit = 20
	// messagesPageLimit is how many messages one Hermes page holds. A poll
	// pages back from the newest message until it reaches the page's mark.
	messagesPageLimit = 50
	// messagesMaxPages caps how far back one poll pages. A turn writes about
	// two rows per tool call, so this covers a turn of a few hundred tool
	// calls; anything older than that is skipped, not shown.
	messagesMaxPages = 10
	// roleAssistant and roleUser are the Hermes message roles a poll returns.
	// The page shows assistant messages; it reads user messages only to find
	// where its own turn starts, so it can tell the turn's working messages
	// from a delegated result that landed before it.
	roleAssistant = "assistant"
	roleUser      = "user"
	roleTool      = "tool"

	sessionIDPrefix = "web-console-"

	// consoleHeader is the custom header the page sends with every turn. A
	// cross-origin page cannot set it without a preflight, and this server
	// answers no preflight.
	consoleHeader      = "X-Kube-Agents-Console"
	consoleHeaderValue = "1"

	contentTypeJSON = "application/json"
	// cspFrameAncestorsNone is sent on every response with X-Frame-Options:
	// DENY; browsers that know CSP use the first, older ones the second.
	cspFrameAncestorsNone = "frame-ancestors 'none'"
	xFrameOptionsDeny     = "DENY"
	bearerPrefix          = "Bearer "
	logPrefix             = "[web-console] "
)

var (
	//go:embed static/*
	staticFS embed.FS

	sessionIDPattern = regexp.MustCompile(`^` + regexp.QuoteMeta(sessionIDPrefix) + `[0-9a-f]{32}$`)

	// loopbackHosts are the Host values `kubectl port-forward` produces. No
	// other Host is accepted: the Service is ClusterIP behind a deny-all
	// NetworkPolicy, so a port-forward is the only way in.
	loopbackHosts = map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true}

	errSessionNotFound = errors.New("hermes session not found")

	// browseSchemes are the repository URL schemes a browser opens as they
	// are; any other scheme (ssh, git) is a clone remote to reduce.
	browseSchemes = map[string]bool{"http": true, "https": true}
	// scpRemotePattern is git's scp-like remote, [user@]host:path.
	scpRemotePattern = regexp.MustCompile(`^(?:[^@/:]+@)?([^@/:]+):(.+)$`)
	// portPathPattern is the "3000/owner/repo" tail of a schemeless
	// host:port/path, which scpRemotePattern would otherwise take for scp.
	portPathPattern = regexp.MustCompile(`^[0-9]+/`)
)

// config is what the console reads from its environment at startup.
type config struct {
	ListenPort   string
	HermesURL    string
	APIServerKey string
	ClusterName  string
	ProjectID    string
	Location     string

	ModelName        string
	ModelProvider    string
	LiteLLMPeersHost string
	AgentKSA         string
	AgentGSA         string
	AgentRoles       []string
	ChatPlatform     string
	Repositories     []repositoryInfo

	// What GET /api/environment reports about the install; see environment.go.
	ChartVersion      string
	AppVersion        string
	ImageTag          string
	HelmRevision      string
	PodNamespace      string
	PlatformAgentName string

	// LiteLLMURL is the inference gateway POST /api/assist sends a feedback
	// draft to; empty turns the route off. ReleaseCheck and ReleaseRepo
	// drive GET /api/release; GitHubAPIBase is the API root (tests point it
	// at a fake). See assist.go.
	LiteLLMURL    string
	AssistModel   string
	ReleaseCheck  bool
	ReleaseRepo   string
	GitHubAPIBase string
}

func loadConfig() config {
	cfg := config{
		ListenPort:   envOr(envListenPort, defaultListenPort),
		HermesURL:    strings.TrimRight(envOr(envHermesURL, defaultHermesURL), "/"),
		APIServerKey: strings.TrimSpace(os.Getenv(envAPIServerKey)),
		ClusterName:  os.Getenv(envClusterName),
		ProjectID:    os.Getenv(envProjectID),
		Location:     os.Getenv(envLocation),

		ModelName:        os.Getenv(envModelName),
		ModelProvider:    os.Getenv(envModelProvider),
		LiteLLMPeersHost: strings.TrimSpace(os.Getenv(envLiteLLMPeersHost)),
		AgentKSA:         os.Getenv(envAgentKSA),
		AgentGSA:         os.Getenv(envAgentGSA),
		AgentRoles:       splitList(os.Getenv(envAgentRoles), agentRolesSeparator),
		ChatPlatform:     strings.TrimSpace(os.Getenv(envChatPlatform)),
		Repositories:     parseRepositories(os.Getenv(envGitOpsRepos)),

		ChartVersion:      strings.TrimSpace(os.Getenv(envKubeAgentsChartVersion)),
		AppVersion:        strings.TrimSpace(os.Getenv(envKubeAgentsAppVersion)),
		ImageTag:          strings.TrimSpace(os.Getenv(envKubeAgentsImageTag)),
		HelmRevision:      strings.TrimSpace(os.Getenv(envHelmReleaseRevision)),
		PodNamespace:      strings.TrimSpace(os.Getenv(envPodNamespace)),
		PlatformAgentName: envOr(envPlatformAgentName, defaultPlatformAgentName),

		LiteLLMURL:    strings.TrimRight(strings.TrimSpace(os.Getenv(envLiteLLMURL)), "/"),
		AssistModel:   envOr(envAssistModel, defaultAssistModel),
		ReleaseCheck:  envBool(envReleaseCheck, true),
		ReleaseRepo:   envOr(envReleaseRepo, defaultReleaseRepo),
		GitHubAPIBase: gitHubAPIBase,
	}
	if !releaseRepoPat.MatchString(cfg.ReleaseRepo) {
		log.Printf(logPrefix+"%s=%q is not owner/name; using %s", envReleaseRepo, cfg.ReleaseRepo, defaultReleaseRepo)
		cfg.ReleaseRepo = defaultReleaseRepo
	}
	return cfg
}

// envBool reads an on/off setting; unset or unrecognised values give the
// fallback.
func envBool(key string, fallback bool) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	}
	return fallback
}

// transportWithCA is an HTTP transport that trusts the cluster's CA.
func transportWithCA(ca []byte) http.RoundTripper {
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	return &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}
}

// parseRepositories reads GITOPS_REPOS. A malformed value yields an empty
// list and a log line, never a crash: the list is informational.
func parseRepositories(raw string) []repositoryInfo {
	out := []repositoryInfo{}
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return out
	}
	var listed []repositoryInfo
	if err := json.Unmarshal([]byte(raw), &listed); err != nil {
		log.Printf(logPrefix+"%s is not a JSON list of repositories; ignoring it: %v", envGitOpsRepos, err)
		return out
	}
	for _, r := range listed {
		r.Repository = strings.TrimSpace(r.Repository)
		if r.Repository == "" {
			continue
		}
		if r.Provider == "" {
			r.Provider = "github"
		}
		if r.Forge == "" {
			r.Forge = r.Provider
		}
		if r.Role == "" {
			r.Role = "gitops"
		}
		if r.URL == "" {
			r.URL = repositoryURL(r)
		}
		out = append(out, r)
		if len(out) >= maxGitOpsRepos {
			break
		}
	}
	return out
}

// repositoryURL is the browse URL for a repository the chart declared, in
// each spelling values.yaml admits: an http(s) URL is returned as it is; an
// ssh:// or git:// remote, an scp remote (git@host:owner/repo) and a
// schemeless host/owner/repo are reduced to host and path and browsed over
// https; owner/name goes on the declared host or the provider's default.
func repositoryURL(r repositoryInfo) string {
	repo := strings.TrimSuffix(r.Repository, ".git")
	host := r.Host
	if scheme, _, isURL := strings.Cut(repo, "://"); isURL {
		if browseSchemes[strings.ToLower(scheme)] {
			return repo
		}
		u, err := url.Parse(repo)
		if err != nil || u.Hostname() == "" {
			return ""
		}
		host, repo = u.Hostname(), u.Path
	} else if m := scpRemotePattern.FindStringSubmatch(repo); m != nil && !portPathPattern.MatchString(m[2]) {
		host, repo = m[1], m[2]
	} else if first, rest, ok := strings.Cut(repo, "/"); ok && strings.Contains(first, ".") && strings.Contains(rest, "/") && (host == "" || first == host) {
		host, repo = first, rest
	}
	scheme := "https"
	if host == "" {
		switch r.Provider {
		case "gitlab":
			host = "gitlab.com"
		case "gitea":
			return ""
		default:
			host = "github.com"
		}
	}
	if strings.HasSuffix(host, ".svc.cluster.local") || strings.Contains(host, ".svc.cluster.local:") {
		scheme = "http"
	}
	return scheme + "://" + host + "/" + strings.Trim(repo, "/")
}

// splitList splits a separated list, trimming each item and dropping empty
// ones, so an unset variable yields an empty list.
func splitList(raw, sep string) []string {
	out := []string{}
	for _, item := range strings.Split(raw, sep) {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// server holds the console's handlers and the mutable state it keeps: which
// sessions have a turn in flight, the last LiteLLM usage read, and the
// session summaries already computed.
type server struct {
	cfg           config
	client        *http.Client
	createTimeout time.Duration

	mu       sync.Mutex
	inflight map[string]bool

	// resolver finds the LiteLLM replicas and fetchMetrics reads one of
	// them. Tests replace both.
	resolver     hostResolver
	fetchMetrics metricsFetcher
	peerPort     string
	usage        usageCache
	model        modelCache
	fetchModel   func(ctx context.Context, baseURL string) (modelInfo, error)

	summaries summaryCache

	// kube reads the Kubernetes API for /api/environment; nil when the pod
	// has no service account token. environment caches the last read.
	kube        *kubeClient
	environment environmentCache

	// githubClient reads the GitHub releases API for /api/release with its
	// own deadline; releaseInfo caches the last answer for an hour.
	githubClient *http.Client
	releaseInfo  releaseCache

	// liveTagMu guards lastLiveImageTag, the most recent non-empty
	// spec.deployment.tag observed on the PlatformAgent CR.
	liveTagMu        sync.Mutex
	lastLiveImageTag string

	// streamCeiling bounds one streamed turn; streamTurnCeiling, or less
	// in tests.
	streamCeiling time.Duration

	// delegationPollInterval is how often runTurn polls session messages
	// when a synchronous /api/chat turn files a kanban_create card; tests
	// lower it.
	delegationPollInterval time.Duration

	// reposMu guards the dynamically loaded / updated GitOps & IAM repositories.
	reposMu        sync.RWMutex
	cachedRepos    []repositoryInfo
	cachedTopology string
	cachedForges   []forgeInfo

	// patchesMu guards the dynamically loaded / updated applied PRs & patches.
	patchesMu        sync.RWMutex
	cachedAppliedPRs []AppliedPR
	patchesLoaded    bool
}

func newServer(cfg config) *server {
	s := &server{
		cfg:           cfg,
		client:        &http.Client{}, // deadlines come from each request's context
		createTimeout: sessionCreateTimeout,
		inflight:      map[string]bool{},
		resolver:      net.DefaultResolver,
		peerPort:      litellmMetricsPort,
		summaries: summaryCache{
			entries: map[string]summaryEntry{},
		},
		githubClient:           &http.Client{Timeout: releaseTimeout},
		streamCeiling:          streamTurnCeiling,
		delegationPollInterval: defaultDelegationPollInterval,
		kube:                   loadKubeClient(),
	}
	s.fetchMetrics = s.fetchPeerMetrics
	s.fetchModel = s.fetchLiveModel
	return s
}

func (s *server) routes() http.Handler {
	static, err := fs.Sub(staticFS, "static")
	if err != nil {
		log.Fatalf(logPrefix+"embedded static tree is missing: %v", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.Handle("GET /", s.requireLocalHost(http.FileServer(http.FS(static))))
	mux.Handle("GET /api/status", s.requireLocalHost(http.HandlerFunc(s.handleStatus)))
	mux.Handle("GET /api/sessions/recent", s.requireLocalHost(http.HandlerFunc(s.handleRecentSessions)))
	mux.Handle("GET /api/sessions/{id}/messages", s.requireLocalHost(http.HandlerFunc(s.handleSessionMessages)))
	mux.Handle("GET /api/sessions/{id}/transcript", s.requireLocalHost(http.HandlerFunc(s.handleTranscript)))
	mux.Handle("GET /api/channels/{name}/posts", s.requireLocalHost(http.HandlerFunc(s.handleChannelPosts)))
	mux.Handle("GET /api/insights", s.requireLocalHost(http.HandlerFunc(s.handleInsights)))
	mux.Handle("GET /api/repositories", s.requireLocalHost(http.HandlerFunc(s.handleRepositoriesGet)))
	mux.Handle("POST /api/repositories", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleRepositoriesSet))))
	mux.Handle("POST /api/repositories/connect", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleRepositoriesConnect))))
	mux.Handle("POST /api/repositories/disconnect", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleRepositoriesDisconnect))))
	mux.Handle("POST /api/repositories/propose-pr", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleRepositoriesProposePR))))
	mux.Handle("GET /api/patches", s.requireLocalHost(http.HandlerFunc(s.handlePatchesGet)))
	mux.Handle("POST /api/patches", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handlePatchesPost))))
	mux.Handle("GET /api/models", s.requireLocalHost(http.HandlerFunc(s.handleModelsGet)))
	mux.Handle("POST /api/models", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleModelsPost))))
	mux.Handle("POST /api/models/test", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleModelsTest))))
	mux.Handle("GET /api/environment", s.requireLocalHost(http.HandlerFunc(s.handleEnvironment)))
	mux.Handle("GET /api/release", s.requireLocalHost(http.HandlerFunc(s.handleRelease)))
	mux.Handle("POST /api/assist", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleAssist))))
	mux.Handle("POST /api/chat", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleChat))))
	mux.Handle("POST /api/chat/stream", s.requireLocalHost(s.requireConsoleRequest(http.HandlerFunc(s.handleChatStream))))
	return denyFraming(mux)
}

// denyFraming stops any other page from loading the console in a frame.
func denyFraming(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", cspFrameAncestorsNone)
		w.Header().Set("X-Frame-Options", xFrameOptionsDeny)
		next.ServeHTTP(w, r)
	})
}

// requireLocalHost refuses a request whose Host is not one the operator's own
// port-forward would send. A rebinding DNS name resolving to 127.0.0.1 still
// carries its own name in Host, so it is refused here.
func (s *server) requireLocalHost(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !loopbackHosts[hostOnly(r.Host)] {
			writeError(w, http.StatusForbidden, "host_not_allowed",
				"The web console only answers on localhost. Reach it with kubectl port-forward.")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// requireConsoleRequest refuses a state-changing request that a page on
// another origin could have sent.
func (s *server) requireConsoleRequest(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(consoleHeader) != consoleHeaderValue {
			writeError(w, http.StatusForbidden, "missing_console_header", "Request did not come from the console page.")
			return
		}
		if !strings.HasPrefix(r.Header.Get("Content-Type"), contentTypeJSON) {
			writeError(w, http.StatusUnsupportedMediaType, "json_required", "Request body must be JSON.")
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" {
			u, err := url.Parse(origin)
			if err != nil || !strings.EqualFold(u.Host, r.Host) {
				writeError(w, http.StatusForbidden, "cross_origin", "Cross-origin requests are refused.")
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

func hostOnly(hostport string) string {
	host := hostport
	if h, _, err := net.SplitHostPort(hostport); err == nil {
		host = h
	}
	return strings.ToLower(strings.Trim(host, "[]"))
}

func (s *server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type upstreamStatus struct {
	Healthy bool   `json:"healthy"`
	Detail  string `json:"detail"`
}

type statusResponse struct {
	Cluster      string         `json:"cluster"`
	Project      string         `json:"project"`
	Location     string         `json:"location"`
	ChatPlatform string         `json:"chat_platform,omitempty"`
	Agent        upstreamStatus `json:"agent"`
}

func (s *server) handleStatus(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), probeTimeout)
	defer cancel()
	agent := upstreamStatus{}
	resp, err := s.hermes(ctx, http.MethodGet, "/health", nil)
	switch {
	case err != nil:
		agent.Detail = "agent gateway unreachable: " + err.Error()
	default:
		drainAndClose(resp)
		agent.Healthy = resp.StatusCode == http.StatusOK
		agent.Detail = fmt.Sprintf("agent gateway answered HTTP %d", resp.StatusCode)
	}
	writeJSON(w, http.StatusOK, statusResponse{
		Cluster:      s.cfg.ClusterName,
		Project:      s.cfg.ProjectID,
		Location:     s.cfg.Location,
		ChatPlatform: s.cfg.ChatPlatform,
		Agent:        agent,
	})
}

// recentSession is the subset of a Hermes session row the page shows.
// Hermes' message preview is left out on purpose. Kind is added here. Summary
// is set only on channel posts (channels.go), for the kinds summaryKinds
// lists, so a Slack or Google Chat session shows its title and nothing else.
type recentSession struct {
	ID           string          `json:"id"`
	Title        string          `json:"title"`
	Source       string          `json:"source"`
	StartedAt    *float64        `json:"started_at"`
	LastActive   *float64        `json:"last_active"`
	MessageCount *int            `json:"message_count"`
	Console      bool            `json:"console"`
	Kind         string          `json:"kind"`
	Summary      *sessionSummary `json:"summary,omitempty"`
}

func (s *server) handleRecentSessions(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), sessionListTimeout)
	defer cancel()
	sessions, failure := s.listSessions(ctx, fmt.Sprintf("/api/sessions?limit=%d", recentSessionsLimit))
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": sessions})
}

// listSessions reads one Hermes session list and sets each row's kind.
func (s *server) listSessions(ctx context.Context, path string) ([]recentSession, *upstreamFailure) {
	resp, err := s.hermes(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, &upstreamFailure{http.StatusBadGateway, "agent_unreachable", "Could not reach the agent gateway: " + err.Error()}
	}
	defer drainAndClose(resp)
	if resp.StatusCode != http.StatusOK {
		return nil, &upstreamFailure{http.StatusBadGateway, "agent_error", upstreamErrorDetail(resp)}
	}
	var listed struct {
		Data []recentSession `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&listed); err != nil {
		return nil, &upstreamFailure{http.StatusBadGateway, "agent_bad_response", "Agent gateway returned an unreadable session list."}
	}
	sessions := make([]recentSession, 0, len(listed.Data))
	for _, sess := range listed.Data {
		sess.Console = sessionIDPattern.MatchString(sess.ID)
		sess.Kind = classifySession(sess.ID, sess.Source, sess.Title)
		sessions = append(sessions, sess)
	}
	return sessions, nil
}

// messageCard carries the fields the page lifts out of an automated prompt
// (event watcher, scheduler, or kanban task notification) so the thread can
// show a compact card instead of a raw routing template.
type messageCard struct {
	Kind     string   `json:"kind,omitempty"`
	Subject  string   `json:"subject,omitempty"`
	Resource string   `json:"resource,omitempty"`
	Reason   string   `json:"reason,omitempty"`
	Warning  string   `json:"warning,omitempty"`
	TaskID   string   `json:"task_id,omitempty"`
	Title    string   `json:"title,omitempty"`
	Assignee string   `json:"assignee,omitempty"`
	Status   string   `json:"status,omitempty"`
	Summary  string   `json:"summary,omitempty"`
	Skills   []string `json:"skills,omitempty"`
}

// messageStep is one tool or skill invocation recorded before an assistant
// message in a session transcript or message poll. A kanban_create call is a
// delegation: the work continues on a card after the reply, and TaskID (from
// the tool's result row) lets the page follow it until the card's completion
// notification lands on the session.
type messageStep struct {
	Tool       string `json:"tool,omitempty"`
	Title      string `json:"title"`
	Detail     string `json:"detail,omitempty"`
	Skill      string `json:"skill,omitempty"`
	Delegation bool   `json:"delegation,omitempty"`
	TaskID     string `json:"task_id,omitempty"`
	TaskTitle  string `json:"task_title,omitempty"`
	Assignee   string `json:"assignee,omitempty"`
}

// sessionMessage is one user or assistant message from a poll of the page's
// own session or a session transcript. ID is Hermes' message row ID, which
// increases in insertion order. Author names who wrote the row ("user",
// "agent", "event_watcher", "scheduler", or "kanban").
type sessionMessage struct {
	ID        int64         `json:"id"`
	Role      string        `json:"role"`
	Author    string        `json:"author,omitempty"`
	Content   string        `json:"content"`
	Timestamp *float64      `json:"timestamp,omitempty"`
	Card      *messageCard  `json:"card,omitempty"`
	Steps     []messageStep `json:"steps,omitempty"`
	Skills    []string      `json:"skills,omitempty"`
}

type sessionMessagesResponse struct {
	SessionID string           `json:"session_id"`
	LatestID  int64            `json:"latest_id"`
	Messages  []sessionMessage `json:"messages"`
}

// handleSessionMessages returns the user and assistant messages that have
// text on one of this console's sessions with an ID above ?after=, oldest
// first. Tool rows and tool-call-only assistant rows are folded into Steps
// and Skills on the next assistant message. LatestID is the highest message
// ID read, of any role, so the page can move its mark past messages it does
// not show.
func (s *server) handleSessionMessages(w http.ResponseWriter, r *http.Request) {
	sid := r.PathValue("id")
	if !sessionIDPattern.MatchString(sid) {
		writeError(w, http.StatusBadRequest, "invalid_session_id", "Session ID was not issued by this console.")
		return
	}
	var after int64
	if raw := r.URL.Query().Get("after"); raw != "" {
		v, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || v < 0 {
			writeError(w, http.StatusBadRequest, "invalid_after", "after must be a non-negative message ID.")
			return
		}
		after = v
	}
	ctx, cancel := context.WithTimeout(r.Context(), messagesTimeout)
	defer cancel()
	rows, _, failure := s.pageMessages(ctx, sid, after, nil)
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return
	}
	out := sessionMessagesResponse{SessionID: sid, LatestID: after, Messages: []sessionMessage{}}
	var pendingSteps []messageStep
	var pendingSkills []string
	for _, m := range rows {
		if m.ID > out.LatestID {
			out.LatestID = m.ID
		}
		if m.ID <= after {
			continue
		}
		steps, skills := extractMessageToolActivity(m)
		if len(steps) > 0 && len(pendingSteps) < maxStepsPerTurn {
			rem := maxStepsPerTurn - len(pendingSteps)
			if len(steps) > rem {
				steps = steps[:rem]
			}
			pendingSteps = append(pendingSteps, steps...)
		}
		pendingSkills = mergeSkills(pendingSkills, skills...)
		if !hasText(m) {
			continue
		}
		msg := s.buildSessionMessage(kindConsole, sid, false, m)
		if m.Role == roleAssistant {
			if len(pendingSteps) > 0 {
				msg.Steps = foldDelegations(pendingSteps)
				pendingSteps = nil
			}
			msg.Skills = mergeSkills(msg.Skills, pendingSkills...)
			pendingSkills = nil
		}
		out.Messages = append(out.Messages, msg)
	}
	writeJSON(w, http.StatusOK, out)
}

// pageMessages pages back from a session's newest message until a page
// reaches after, enough reports that the rows read so far suffice, or the
// session runs out. A long turn writes many tool rows, so one page can stop
// short of the mark. The rows come back oldest first, and reachedStart
// reports whether the last page ran the session dry (fewer than
// messagesPageLimit rows). A nil enough reads until after or the page cap.
func (s *server) pageMessages(ctx context.Context, sid string, after int64, enough func([]hermesMessage) bool) ([]hermesMessage, bool, *upstreamFailure) {
	var rows []hermesMessage
	reachedStart := false
	for page := 0; page < messagesMaxPages; page++ {
		path := fmt.Sprintf("/api/sessions/%s/messages?order=latest&limit=%d&offset=%d",
			url.PathEscape(sid), messagesPageLimit, page*messagesPageLimit)
		listed, failure := s.fetchMessagesPage(ctx, path)
		if failure != nil {
			return nil, false, failure
		}
		rows = append(listed, rows...)
		if len(listed) < messagesPageLimit {
			reachedStart = true
			break
		}
		if listed[0].ID <= after || (enough != nil && enough(rows)) {
			break
		}
	}
	return rows, reachedStart, nil
}

// hasText reports whether a message is a user or assistant row with text,
// the only rows the page shows. Tool rows and tool-call-only assistant rows
// are not.
func hasText(m hermesMessage) bool {
	return (m.Role == roleAssistant || m.Role == roleUser) && m.Content != nil && strings.TrimSpace(*m.Content) != ""
}

// hermesMessage is one row of Hermes' session messages list.
type hermesMessage struct {
	ID        int64           `json:"id"`
	Role      string          `json:"role"`
	Content   *string         `json:"content"`
	Timestamp *float64        `json:"timestamp"`
	ToolName  *string         `json:"tool_name,omitempty"`
	ToolCalls json.RawMessage `json:"tool_calls,omitempty"`
}

// upstreamFailure is an error response to send when a Hermes call fails.
type upstreamFailure struct {
	status  int
	code    string
	message string
}

// fetchMessagesPage reads one page of a session's messages, oldest first.
func (s *server) fetchMessagesPage(ctx context.Context, path string) ([]hermesMessage, *upstreamFailure) {
	resp, err := s.hermes(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, &upstreamFailure{http.StatusBadGateway, "agent_unreachable", "Could not reach the agent gateway: " + err.Error()}
	}
	defer drainAndClose(resp)
	if resp.StatusCode == http.StatusNotFound {
		return nil, &upstreamFailure{http.StatusNotFound, "session_not_found", "The agent has no record of this session."}
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &upstreamFailure{http.StatusBadGateway, "agent_error", upstreamErrorDetail(resp)}
	}
	var listed struct {
		Data []hermesMessage `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&listed); err != nil {
		return nil, &upstreamFailure{http.StatusBadGateway, "agent_bad_response", "Agent gateway returned unreadable messages."}
	}
	return listed.Data, nil
}

type chatRequest struct {
	Message   string `json:"message"`
	SessionID string `json:"session_id,omitempty"`
}

type chatResponse struct {
	SessionID string `json:"session_id"`
	Reply     string `json:"reply"`
	BeforeID  int64  `json:"before_id,omitempty"`
	ReplyID   int64  `json:"reply_id,omitempty"`
	Created   bool   `json:"created,omitempty"`
}

func (s *server) handleChat(w http.ResponseWriter, r *http.Request) {
	sid, msg, agentSession, ok := s.beginTurn(w, r)
	if !ok {
		return
	}
	defer s.release(sid)

	created := false
	beforeID, exists := s.latestMessageID(r.Context(), sid)
	if !exists && !agentSession {
		if err := s.createSession(r.Context(), sid); err != nil {
			writeError(w, http.StatusBadGateway, "session_create_failed", err.Error())
			return
		}
		created = true
	}

	ctx, cancel := context.WithTimeout(r.Context(), turnTimeout)
	defer cancel()
	reply, err := s.runTurn(ctx, sid, msg)
	if errors.Is(err, errSessionNotFound) && !agentSession {
		// The agent pod's session store does not have this ID, for instance
		// after the pod was replaced. Recreate it and run the turn once more.
		beforeID = 0
		if createErr := s.createSession(ctx, sid); createErr != nil {
			writeError(w, http.StatusBadGateway, "session_create_failed", createErr.Error())
			return
		}
		created = true
		reply, err = s.runTurn(ctx, sid, msg)
	}
	if err != nil {
		status, body := turnFailure(ctx, err, sid)
		writeJSON(w, status, body)
		return
	}
	replyID := s.replyMessageID(r.Context(), sid, beforeID, reply)
	writeJSON(w, http.StatusOK, chatResponse{SessionID: sid, Reply: reply, BeforeID: beforeID, ReplyID: replyID, Created: created})
}

// latestMessageID reads the session's newest message row ID, or 0 when the
// session is empty or the read fails. The second return value is false only
// when Hermes reports 404 for the session, so handleChat creates a fresh
// client-minted session before its first turn.
func (s *server) latestMessageID(parent context.Context, sid string) (int64, bool) {
	ctx, cancel := context.WithTimeout(parent, messagesTimeout)
	defer cancel()
	path := fmt.Sprintf("/api/sessions/%s/messages?order=latest&limit=1&offset=0", url.PathEscape(sid))
	listed, failure := s.fetchMessagesPage(ctx, path)
	if failure != nil {
		return 0, failure.status != http.StatusNotFound
	}
	if len(listed) == 0 {
		return 0, true
	}
	return listed[len(listed)-1].ID, true
}

// replyMessageID finds the row ID of the assistant reply that just finished
// after beforeID by matching its text, so a wake that landed between the end
// of runTurn and this read is not mistaken for the turn's own reply. Falls
// back to the newest row ID when no assistant row matches.
func (s *server) replyMessageID(parent context.Context, sid string, beforeID int64, reply string) int64 {
	ctx, cancel := context.WithTimeout(parent, messagesTimeout)
	defer cancel()
	want := strings.TrimSpace(reply)
	var newestID int64
	var matchedID int64
	for page := 0; page < messagesMaxPages; page++ {
		path := fmt.Sprintf("/api/sessions/%s/messages?order=latest&limit=%d&offset=%d",
			url.PathEscape(sid), messagesPageLimit, page*messagesPageLimit)
		listed, failure := s.fetchMessagesPage(ctx, path)
		if failure != nil || len(listed) == 0 {
			break
		}
		if page == 0 {
			newestID = listed[len(listed)-1].ID
		}
		for i := len(listed) - 1; i >= 0; i-- {
			m := listed[i]
			if m.ID <= beforeID {
				break
			}
			emptyReply := want == "" && (m.Content == nil || strings.TrimSpace(*m.Content) == "")
			textReply := want != "" && m.Content != nil && strings.TrimSpace(*m.Content) == want
			if m.Role == roleAssistant && (textReply || emptyReply) {
				matchedID = m.ID
				break
			}
		}
		if matchedID > 0 || len(listed) < messagesPageLimit || listed[0].ID <= beforeID {
			break
		}
	}
	if matchedID > 0 {
		return matchedID
	}
	return newestID
}

// beginTurn reads and checks a chat request and claims the session's one
// in-flight turn. The page mints console session IDs; the caller creates the
// Hermes session when it does not exist yet. When ok
// is false it has written the error response. The caller releases the claim.
//
// A request may name one of this console's sessions, or reply into an event
// triage or scheduled session the cluster opened (agentSession). The second
// kind is checked against Hermes' record of the session and never created:
// a reply into a session that does not exist is a 404, not a new session.
// Every other session, Slack and Google Chat included, is refused.
func (s *server) beginTurn(w http.ResponseWriter, r *http.Request) (sid, msg string, agentSession, ok bool) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req chatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeError(w, http.StatusRequestEntityTooLarge, "message_too_large", "Message is too large.")
			return "", "", false, false
		}
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return "", "", false, false
	}
	msg = strings.TrimSpace(req.Message)
	if msg == "" {
		writeError(w, http.StatusBadRequest, "empty_message", "Message cannot be empty.")
		return "", "", false, false
	}

	sid = req.SessionID
	switch {
	case sessionIDPattern.MatchString(sid):
	case strings.HasPrefix(sid, sessionIDPrefix):
		// Console IDs have one shape. A malformed one was not issued here.
		writeError(w, http.StatusBadRequest, "invalid_session_id", "Session ID was not issued by this console.")
		return "", "", false, false
	case transcriptIDPattern.MatchString(sid):
		if !s.allowAgentSessionReply(w, r.Context(), sid) {
			return "", "", false, false
		}
		agentSession = true
	default:
		writeError(w, http.StatusBadRequest, "invalid_session_id", "Session ID was not issued by this console.")
		return "", "", false, false
	}
	if !s.claim(sid) {
		writeError(w, http.StatusConflict, "turn_in_progress", "This session already has a turn running. Wait for its reply.")
		return "", "", false, false
	}
	return sid, msg, agentSession, true
}

// allowAgentSessionReply checks that sid names an event triage or scheduled
// session the gateway API created, and writes the refusal when it does not.
func (s *server) allowAgentSessionReply(w http.ResponseWriter, parent context.Context, sid string) bool {
	ctx, cancel := context.WithTimeout(parent, sessionLookupTimeout)
	defer cancel()
	sess, failure := s.lookupSession(ctx, sid)
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return false
	}
	if kind := classifySession(sid, sess.Source, sess.Title); kind != kindEventTriage && kind != kindScheduled {
		writeError(w, http.StatusForbidden, "session_not_allowed",
			"The console posts only into its own sessions, event triage and scheduled checks.")
		return false
	}
	if s.cfg.ChatPlatform != "" {
		writeError(w, http.StatusForbidden, "session_not_allowed",
			fmt.Sprintf("Alert and scheduled threads are read-only in the console when %s is enabled; reply in %s.", s.cfg.ChatPlatform, s.cfg.ChatPlatform))
		return false
	}
	busy, failure := s.agentSessionBusy(ctx, sid, sess, time.Now())
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return false
	}
	if busy {
		writeError(w, http.StatusConflict, "agent_busy", agentBusyDetail)
		return false
	}
	return true
}

// agentSessionBusy guesses whether another writer has a turn running on an
// agent session: the event watcher's own triage turn, a reply from the chat
// thread the alert went to, or a kanban card's wake. Hermes does not
// serialize turns on one session and does not say which sessions have a run
// in flight, so the guess is this: the newest message is not an assistant
// reply with text, and the session was active within agentBusyWindow. A
// session with no messages yet is not busy.
func (s *server) agentSessionBusy(ctx context.Context, sid string, sess hermesSession, now time.Time) (bool, *upstreamFailure) {
	if sess.LastActive == nil || now.Sub(time.Unix(0, int64(*sess.LastActive*float64(time.Second)))) > agentBusyWindow {
		return false, nil
	}
	newest, failure := s.fetchMessagesPage(ctx, fmt.Sprintf("/api/sessions/%s/messages?order=latest&limit=%d&offset=0",
		url.PathEscape(sid), agentBusyPageLimit))
	if failure != nil {
		return false, failure
	}
	if len(newest) == 0 {
		return false, nil
	}
	last := newest[len(newest)-1]
	return !(last.Role == roleAssistant && hasText(last)), nil
}

// turnFailure maps a failed turn to the status and error body the page
// shows. The body carries the session ID so the page keeps the session.
func turnFailure(turnCtx context.Context, err error, sid string) (int, errorResponse) {
	var ue *upstreamError
	switch {
	case errors.Is(err, errSessionNotFound):
		return http.StatusNotFound, errorResponse{Error: "session_not_found", Detail: "The agent has no record of this session.", SessionID: sid}
	case errors.As(err, &ue) && ue.status == http.StatusTooManyRequests,
		errors.As(err, &ue) && ue.status == http.StatusServiceUnavailable:
		return http.StatusServiceUnavailable, errorResponse{Error: "agent_busy", Detail: ue.Error(), SessionID: sid}
	case errors.Is(turnCtx.Err(), context.DeadlineExceeded):
		return http.StatusGatewayTimeout, errorResponse{Error: "turn_timeout",
			Detail:    fmt.Sprintf("The agent did not answer within %s. It may still be working; ask for a status update.", turnTimeout),
			SessionID: sid}
	default:
		return http.StatusBadGateway, errorResponse{Error: "agent_error", Detail: err.Error(), SessionID: sid}
	}
}

func (s *server) claim(sid string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.inflight[sid] {
		return false
	}
	s.inflight[sid] = true
	return true
}

func (s *server) release(sid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.inflight, sid)
}

// createSession creates the Hermes session row. Hermes answers 409 when the
// ID exists, which is the outcome this wanted.
func (s *server) createSession(parent context.Context, sid string) error {
	ctx, cancel := context.WithTimeout(parent, s.createTimeout)
	defer cancel()
	// Titles are unique in Hermes, so the title carries the ID.
	body := map[string]string{"session_id": sid, "title": "Web console " + strings.TrimPrefix(sid, sessionIDPrefix)}
	resp, err := s.hermes(ctx, http.MethodPost, "/api/sessions", body)
	if err != nil {
		return fmt.Errorf("could not reach the agent gateway: %w", err)
	}
	defer drainAndClose(resp)
	if resp.StatusCode == http.StatusCreated || resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusConflict {
		return nil
	}
	return &upstreamError{status: resp.StatusCode, detail: upstreamErrorDetail(resp)}
}

// runTurn sends one message on a session and returns the agent's reply, per
// Hermes' POST /api/sessions/{id}/chat: request {"message": ...}, response
// {"message": {"role": "assistant", "content": ...}}. When the turn files one
// or more kanban_create cards on the session, runTurn waits for those cards to
// reach a terminal status and returns the completed card report (or a
// subsequent assistant synthesis message) instead of the routing stub.
func (s *server) runTurn(ctx context.Context, sid, msg string) (string, error) {
	var beforeID int64
	if latest, failure := s.fetchMessagesPage(ctx, fmt.Sprintf("/api/sessions/%s/messages?order=latest&limit=1&offset=0", url.PathEscape(sid))); failure == nil && len(latest) > 0 {
		beforeID = latest[len(latest)-1].ID
	}

	resp, err := s.hermes(ctx, http.MethodPost, "/api/sessions/"+url.PathEscape(sid)+"/chat", map[string]string{"message": msg})
	if err != nil {
		return "", err
	}
	defer drainAndClose(resp)
	if resp.StatusCode == http.StatusNotFound {
		return "", errSessionNotFound
	}
	if resp.StatusCode != http.StatusOK {
		return "", &upstreamError{status: resp.StatusCode, detail: upstreamErrorDetail(resp)}
	}
	var out struct {
		Message json.RawMessage `json:"message"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&out); err != nil {
		return "", fmt.Errorf("agent gateway returned an unreadable reply: %w", err)
	}
	var m struct {
		Content string `json:"content"`
	}
	if err := json.Unmarshal(out.Message, &m); err != nil {
		return "", fmt.Errorf("agent gateway reply has no message object: %w", err)
	}
	return s.awaitDelegatedKanbanReply(ctx, sid, beforeID, m.Content, nil)
}

// awaitDelegatedKanbanReply inspects the messages written during a turn
// (ID > beforeID) for kanban_create calls. When none were filed, it returns
// initialReply immediately with no polling delay. When one or more cards were
// filed, it polls session messages until every delegated card reaches a
// terminal status and returns either the subsequent assistant synthesis reply
// or the joined card result summaries. When out is non-nil (streamed turns),
// it sends a status and step event when delegation is detected and a keepalive
// on each poll tick. If ctx expires before all cards settle, it returns
// initialReply when non-empty.
func (s *server) awaitDelegatedKanbanReply(ctx context.Context, sid string, beforeID int64, initialReply string, out *sseWriter) (string, error) {
	rows, _, failure := s.pageMessages(ctx, sid, beforeID, nil)
	if failure != nil {
		log.Printf(logPrefix+"session %s: initial delegation message read failed (%s): %s", sid, failure.code, failure.message)
		return initialReply, nil
	}
	reply, settled, allTerminal, delegated := s.resolveDelegatedTurn(rows, beforeID)
	if !delegated {
		return initialReply, nil
	}
	if settled {
		return reply, nil
	}

	lastStatus := s.delegatedWaitStatus(rows, beforeID)
	if out != nil && lastStatus != "" {
		out.send(eventStatus, statusEvent{Text: lastStatus})
		out.send(eventStep, stepEvent{
			ID:    "kanban_wait",
			Kind:  stepKindTool,
			Title: lastStatus,
			State: stepStateRunning,
			Tool:  toolKanbanCreate,
		})
	}
	finishStep := func(state string) {
		if out != nil && lastStatus != "" {
			out.send(eventStep, stepEvent{
				ID:    "kanban_wait",
				Kind:  stepKindTool,
				Title: lastStatus,
				State: state,
				Tool:  toolKanbanCreate,
			})
		}
	}

	interval := s.delegationPollInterval
	if interval <= 0 {
		interval = defaultDelegationPollInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	gracePolls := 0
	if allTerminal {
		gracePolls = 1
	}

	for {
		select {
		case <-ctx.Done():
			if allTerminal && strings.TrimSpace(reply) != "" {
				finishStep(stepStateDone)
				return reply, nil
			}
			finishStep(stepStateFailed)
			if strings.TrimSpace(initialReply) != "" {
				return initialReply, nil
			}
			return "", ctx.Err()
		case <-ticker.C:
			if out != nil {
				out.keepalive()
			}
			rows, _, failure = s.pageMessages(ctx, sid, beforeID, nil)
			if failure != nil {
				continue
			}
			reply, settled, allTerminal, _ = s.resolveDelegatedTurn(rows, beforeID)
			if settled {
				finishStep(stepStateDone)
				return reply, nil
			}
			if allTerminal {
				gracePolls++
				if gracePolls >= defaultDelegationSynthesisGracePolls {
					finishStep(stepStateDone)
					return reply, nil
				}
			}
			if out != nil {
				if nextStatus := s.delegatedWaitStatus(rows, beforeID); nextStatus != "" && nextStatus != lastStatus {
					lastStatus = nextStatus
					out.send(eventStatus, statusEvent{Text: lastStatus})
					out.send(eventStep, stepEvent{
						ID:    "kanban_wait",
						Kind:  stepKindTool,
						Title: lastStatus,
						State: stepStateRunning,
						Tool:  toolKanbanCreate,
					})
				}
			}
		}
	}
}

// delegatedWaitStatus returns a short human-readable status line naming the
// delegated Kanban task IDs that are still in flight (or noting synthesis once
// all tasks have reached a terminal status).
func (s *server) delegatedWaitStatus(rows []hermesMessage, beforeID int64) string {
	var orderedIDs []string
	pending := map[string]bool{}
	completed := map[string]bool{}
	for _, m := range rows {
		if m.ID <= beforeID {
			continue
		}
		if m.Role == roleTool && m.Content != nil && (m.ToolName == nil || *m.ToolName == toolKanbanCreate) {
			if id := kanbanCreateTaskID(*m.Content); id != "" && !pending[id] {
				pending[id] = true
				orderedIDs = append(orderedIDs, id)
			}
		}
		if m.Role == roleUser && m.Content != nil {
			if card, ok := s.parseKanbanCard(*m.Content); ok && pending[card.TaskID] && isTerminalKanbanStatus(card.Status) {
				completed[card.TaskID] = true
			}
		}
	}
	var waiting []string
	for _, id := range orderedIDs {
		if !completed[id] {
			waiting = append(waiting, id)
		}
	}
	if len(waiting) > 0 {
		return fmt.Sprintf("Waiting for task %s to finish", strings.Join(waiting, ", "))
	}
	if len(orderedIDs) > 0 {
		return fmt.Sprintf("Waiting for task %s to finish", strings.Join(orderedIDs, ", "))
	}
	return ""
}

// resolveDelegatedTurn scans messages with ID > beforeID for kanban_create
// tool results and their matching [kanban] Task <id> <status>. notifications.
// It returns (reply, settled, allTerminal, delegated), where settled is true
// once all cards are terminal and the Planning Agent's wake turn has replied
// (either with a synthesis message or [SILENT]).
func (s *server) resolveDelegatedTurn(rows []hermesMessage, beforeID int64) (string, bool, bool, bool) {
	var orderedIDs []string
	pending := map[string]bool{}
	completed := map[string]*messageCard{}
	var lastCardMsgID int64
	var synthesis string
	var wakeSilent bool

	for _, m := range rows {
		if m.ID <= beforeID {
			continue
		}
		if m.Role == roleTool && m.Content != nil && (m.ToolName == nil || *m.ToolName == toolKanbanCreate) {
			if id := kanbanCreateTaskID(*m.Content); id != "" && !pending[id] {
				pending[id] = true
				orderedIDs = append(orderedIDs, id)
			}
		}
		if m.Role == roleUser && m.Content != nil {
			if card, ok := s.parseKanbanCard(*m.Content); ok && pending[card.TaskID] && isTerminalKanbanStatus(card.Status) {
				completed[card.TaskID] = card
				if m.ID > lastCardMsgID {
					lastCardMsgID = m.ID
					synthesis = ""
					wakeSilent = false
				}
			}
		}
		if m.Role == roleAssistant && m.Content != nil && lastCardMsgID > 0 && m.ID > lastCardMsgID {
			if text := strings.TrimSpace(*m.Content); text != "" {
				if strings.EqualFold(text, "[SILENT]") {
					wakeSilent = true
				} else {
					synthesis = text
				}
			}
		}
	}

	if len(orderedIDs) == 0 {
		return "", false, false, false
	}
	for _, id := range orderedIDs {
		if _, ok := completed[id]; !ok {
			return "", false, false, true
		}
	}
	if synthesis != "" {
		return synthesis, true, true, true
	}
	var summaries []string
	for _, id := range orderedIDs {
		card := completed[id]
		if sum := strings.TrimSpace(card.Summary); sum != "" {
			summaries = append(summaries, sum)
		} else {
			summaries = append(summaries, fmt.Sprintf("Task %s %s.", id, strings.TrimSpace(card.Status)))
		}
	}
	fallback := strings.Join(summaries, "\n\n")
	return fallback, wakeSilent, true, true
}

func isTerminalKanbanStatus(status string) bool {
	s := strings.ToLower(strings.TrimSpace(status))
	for _, term := range []string{"completed", "done", "failed", "blocked", "cancelled", "canceled", "aborted", "gave_up", "gave up"} {
		if s == term || strings.HasPrefix(s, term+";") || strings.HasPrefix(s, term+":") || strings.HasPrefix(s, term+".") || strings.HasPrefix(s, term+" ") {
			return true
		}
	}
	return false
}

// hermes sends one request to the gateway with the API key attached.
func (s *server) hermes(ctx context.Context, method, path string, body any) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, s.cfg.HermesURL+path, reader)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", contentTypeJSON)
	}
	if s.cfg.APIServerKey != "" {
		req.Header.Set("Authorization", bearerPrefix+s.cfg.APIServerKey)
	}
	return s.client.Do(req)
}

type upstreamError struct {
	status int
	detail string
}

func (e *upstreamError) Error() string {
	return fmt.Sprintf("agent gateway answered HTTP %d: %s", e.status, e.detail)
}

// upstreamErrorDetail reads the message out of a Hermes error body, which is
// OpenAI-shaped: {"error": {"message": ..., "code": ...}}.
func upstreamErrorDetail(resp *http.Response) string {
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamErrorBytes))
	var parsed struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &parsed) == nil && parsed.Error.Message != "" {
		return parsed.Error.Message
	}
	if text := strings.TrimSpace(string(raw)); text != "" {
		return text
	}
	return http.StatusText(resp.StatusCode)
}

func drainAndClose(resp *http.Response) {
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxUpstreamErrorBytes))
	_ = resp.Body.Close()
}

type errorResponse struct {
	Error  string `json:"error"`
	Detail string `json:"detail"`
	// SessionID names the session a failed turn ran on.
	SessionID string `json:"session_id,omitempty"`
}

func writeError(w http.ResponseWriter, status int, code, detail string) {
	writeJSON(w, status, errorResponse{Error: code, Detail: detail})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", contentTypeJSON)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	cfg := loadConfig()
	if cfg.APIServerKey == "" {
		log.Printf(logPrefix + "API_SERVER_KEY is empty; the agent gateway will refuse every turn")
	}
	s := newServer(cfg)

	srv := &http.Server{
		Addr:              ":" + cfg.ListenPort,
		Handler:           s.routes(),
		ReadHeaderTimeout: serverHeaderTimeout,
		ReadTimeout:       serverReadTimeout,
		WriteTimeout:      serverWriteTimeout,
		IdleTimeout:       serverIdleTimeout,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		log.Printf(logPrefix+"listening on %s, agent gateway %s", srv.Addr, cfg.HermesURL)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf(logPrefix+"server error: %v", err)
		}
	}()
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
