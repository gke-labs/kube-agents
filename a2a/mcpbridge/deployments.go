package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Deployment naming and defaults shared by the bridge core and the Kubernetes
// adapter in package kube.
const (
	// DefaultMCPPort is the in-cluster a2a-mcp HTTP listener port.
	DefaultMCPPort = 8089
	// MCPServiceSuffix is appended to the PlatformAgent name to form its in-cluster MCP Service.
	MCPServiceSuffix = "-a2a-mcp"
	// MCPTokenSecretSuffix is appended to the PlatformAgent name to form its MCP bearer token Secret.
	MCPTokenSecretSuffix = "-a2a-mcp-token"
	// MCPTokenSecretKey is the Secret data key holding the MCP bearer token.
	MCPTokenSecretKey = "token"
	// EnvMCPToken is the environment variable that overrides the MCP bearer token. EnvMCPTokenFor
	// is its only reader: with one deployment configured the token is that deployment's; with
	// several it is the one EnvMCPTokenDeployment names. Either way it goes only to that
	// deployment's saved target, never to a connection that overrides it.
	EnvMCPToken = "KUBE_AGENTS_MCP_TOKEN"
	// EnvMCPTokenDeployment names the saved deployment profile EnvMCPToken belongs to when more
	// than one deployment is configured: its saved name, or its name before a cross-context
	// rename if only one profile answers to it. Unset, or naming none or several of them, the
	// token is sent nowhere.
	EnvMCPTokenDeployment = "KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT"

	// DefaultUpstreamRepoURL and friends are the canonical upstream GitHub documentation links.
	DefaultUpstreamRepoURL         = "https://github.com/gke-labs/kube-agents"
	DefaultUpstreamDocsURL         = "https://github.com/gke-labs/kube-agents/blob/main/README.md"
	DefaultUpstreamInstallGuideURL = "https://github.com/gke-labs/kube-agents/blob/main/README.md#quick-start"
	DefaultUpstreamUpgradeGuideURL = "https://github.com/gke-labs/kube-agents/blob/main/README.md#updating-the-agent-day-2-ops"
	DefaultUpstreamReleasesURL     = "https://github.com/gke-labs/kube-agents/releases"

	// ChatPlatformGChat and friends name the chat integration a deployment is wired to
	// (the backends PlatformAgent spec.integration declares, plus console-only).
	ChatPlatformGChat       = "gchat"
	ChatPlatformSlack       = "slack"
	ChatPlatformTeams       = "teams"
	ChatPlatformConsoleOnly = "console"

	badgeActive   = "🟢 **Active**"
	badgeStandby  = "⚪ Standby"
	badgeReady    = "✅ Ready"
	badgeNotReady = "⚠️ Not Ready"
)

// FormatDeploymentsMarkdown renders discovered/saved DeploymentProfiles as a rich Markdown table.
// ContextProbeFailure entries are listed after the table (or after the "none found" line) so a
// kubeconfig context that could not be scanned is never reported as having no deployments.
func FormatDeploymentsMarkdown(profiles []DeploymentProfile, activeName string) string {
	deployments := Deployments(profiles)
	failures := ProbeFailures(profiles)
	if len(deployments) == 0 {
		return "No `kube-agents` deployments found in `~/.kube/config` or saved configuration.\n" + formatProbeFailuresMarkdown(failures)
	}
	var sb strings.Builder
	sb.WriteString("## 🛰️ Kube-Agents Deployments\n\n")
	sb.WriteString("| Status | Name | Ready | Mode | Cluster / Location | Namespace | GitHub Repo | Chat | Profiles | Kube Context |\n")
	sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
	for _, p := range deployments {
		activeCol := badgeStandby
		if p.Name == activeName {
			activeCol = badgeActive
		}
		readyCol := badgeNotReady
		if p.Ready {
			readyCol = badgeReady
		}
		clusterLoc := p.ClusterName
		if p.Location != "" {
			clusterLoc = fmt.Sprintf("%s (`%s`)", FirstNonEmptyStr(p.ClusterName, p.PlatformAgentName), p.Location)
		}
		profs := "—"
		if len(p.AgentProfiles) > 0 {
			profs = "`" + strings.Join(p.AgentProfiles, "`, `") + "`"
		}
		fmt.Fprintf(&sb, "| %s | **`%s`** | %s | `%s` | %s | `%s` | `%s` | `%s` | %s | `%s` |\n",
			activeCol,
			p.Name,
			readyCol,
			FirstNonEmptyStr(p.Mode, "next"),
			FirstNonEmptyStr(clusterLoc, "—"),
			p.Namespace,
			FirstNonEmptyStr(p.GitHubRepo, "—"),
			FirstNonEmptyStr(p.ChatPlatform, "console"),
			profs,
			FirstNonEmptyStr(p.KubeContext, p.DirectMCPURL),
		)
	}
	sb.WriteString(formatProbeFailuresMarkdown(failures))
	return sb.String()
}

// formatProbeFailuresMarkdown lists the kubeconfig contexts a scan could not reach, so the
// caller can distinguish "no kube-agents there" from "could not look".
func formatProbeFailuresMarkdown(failures []DeploymentProfile) string {
	if len(failures) == 0 {
		return ""
	}
	var sb strings.Builder
	sb.WriteString("\n⚠️ **Kubeconfig contexts that could not be scanned** (saved profiles for them are kept from the last successful scan):\n\n")
	for _, f := range failures {
		fmt.Fprintf(&sb, "- `%s`: %s\n", f.KubeContext, FirstNonEmptyStr(f.StatusMessage, "unknown error"))
	}
	return sb.String()
}

type cachedCompareResult struct {
	aheadBy  int
	compared bool
}

// cachedReleaseInfo is the process-wide memo of the latest upstream release lookup. Both
// outcomes are remembered: a hit (tag and fetchedAt) and a miss (failedAt), so an offline or
// rate-limited GitHub API costs at most one attempt per releaseCacheTTL instead of a timeout
// on every call.
type cachedReleaseInfo struct {
	tag          string
	commit       string
	htmlURL      string
	publishedAt  string
	fetchedAt    time.Time
	failedAt     time.Time
	dirty        bool
	compareCache map[string]cachedCompareResult
}

var (
	releaseCacheMu sync.Mutex
	releaseCache   cachedReleaseInfo

	// releaseAPIBaseURL and releaseHTTPClient are the GitHub API origin and client the release
	// lookups use; the package tests point them at a local httptest.Server.
	releaseAPIBaseURL = "https://api.github.com"
	releaseHTTPClient = http.DefaultClient
)

const (
	githubLatestReleasePath = "/repos/gke-labs/kube-agents/releases/latest"
	githubCompareAPIFormat  = "/repos/gke-labs/kube-agents/compare/%s...%s"
	releaseCacheTTL         = 10 * time.Minute
	githubAPITimeout        = 3500 * time.Millisecond
	// releaseCacheFileName is the sibling of the config file that persists the release
	// lookup across bridge processes.
	releaseCacheFileName = "latest-release.json"
)

// persistedRelease is the on-disk form of cachedReleaseInfo.
type persistedRelease struct {
	Tag         string    `json:"tag,omitempty"`
	Commit      string    `json:"commit,omitempty"`
	HTMLURL     string    `json:"htmlUrl,omitempty"`
	PublishedAt string    `json:"publishedAt,omitempty"`
	FetchedAt   time.Time `json:"fetchedAt,omitempty"`
	FailedAt    time.Time `json:"failedAt,omitempty"`
}

// ResetReleaseCacheForTest configures the release lookup endpoint/client and clears the
// in-memory cache for deterministic unit tests.
func ResetReleaseCacheForTest(baseURL string, client *http.Client) {
	releaseCacheMu.Lock()
	defer releaseCacheMu.Unlock()
	if baseURL != "" {
		releaseAPIBaseURL = baseURL
	} else {
		releaseAPIBaseURL = "https://api.github.com"
	}
	if client != nil {
		releaseHTTPClient = client
	} else {
		releaseHTTPClient = http.DefaultClient
	}
	releaseCache = cachedReleaseInfo{}
}

// EnrichDeploymentVersionStatus queries the upstream gke-labs/kube-agents GitHub release API
// (with a 10-minute cache of hits and misses) and compares the cluster's installed version/tag
// against the latest upstream release tag to populate UpgradeAvailable and CommitsBehind.
// When no release can be determined the profile keeps whatever an earlier successful check
// recorded; a release is never invented and UpgradeAvailable is never set without a fetched tag.
func EnrichDeploymentVersionStatus(ctx context.Context, prof *DeploymentProfile) {
	enrichDeploymentVersionStatus(ctx, prof, true)
}

// EnrichDeploymentVersionStatusWithOptions is EnrichDeploymentVersionStatus with the network
// attempt made optional: with allowFetch false only cached release information is applied.
func EnrichDeploymentVersionStatusWithOptions(ctx context.Context, prof *DeploymentProfile, allowFetch bool) {
	enrichDeploymentVersionStatus(ctx, prof, allowFetch)
}

// ForceEnrichDeploymentVersionStatus clears both the profile's last-checked timestamp and any
// negative/positive in-memory release cache freshness window so an explicit user refresh
// retries the upstream release check immediately.
func ForceEnrichDeploymentVersionStatus(ctx context.Context, prof *DeploymentProfile) {
	if prof == nil {
		return
	}
	prof.VersionCheckedAt = time.Time{}
	releaseCacheMu.Lock()
	releaseCache.fetchedAt = time.Time{}
	releaseCache.failedAt = time.Time{}
	releaseCacheMu.Unlock()
	enrichDeploymentVersionStatus(ctx, prof, true)
}

func enrichDeploymentVersionStatus(ctx context.Context, prof *DeploymentProfile, allowFetch bool) {
	if prof == nil {
		return
	}
	now := time.Now().UTC()
	installed := strings.TrimSpace(prof.Version)
	recentlyChecked := !prof.VersionCheckedAt.IsZero() && now.Sub(prof.VersionCheckedAt) < releaseCacheTTL
	if recentlyChecked && prof.LatestReleaseTag != "" {
		return
	}

	tag, commit, htmlURL, publishedAt := fetchLatestUpstreamRelease(ctx, allowFetch && !recentlyChecked)
	if tag == "" {
		if allowFetch && !recentlyChecked {
			prof.VersionCheckedAt = now
		}
		return
	}

	prof.LatestReleaseTag = tag
	prof.LatestReleaseCommit = commit
	prof.LatestReleaseURL = FirstNonEmptyStr(htmlURL, DefaultUpstreamReleasesURL)
	prof.LatestReleasePublishedAt = publishedAt
	prof.VersionCheckedAt = now

	cleanInst := strings.TrimPrefix(installed, "v")
	cleanTag := strings.TrimPrefix(tag, "v")
	if installed == "" {
		prof.UpgradeAvailable = false
		prof.CommitsBehind = 0
		return
	}
	if cleanInst == cleanTag {
		prof.UpgradeAvailable = false
		prof.CommitsBehind = 0
		return
	}
	if isHexCommitSHA(commit) && isHexCommitSHA(installed) {
		lowCommit := strings.ToLower(commit)
		lowInst := strings.ToLower(installed)
		if strings.HasPrefix(lowCommit, lowInst) || strings.HasPrefix(lowInst, lowCommit) {
			prof.UpgradeAvailable = false
			prof.CommitsBehind = 0
			return
		}
	}
	if cmp, ok := compareSemver(installed, tag); ok {
		if cmp >= 0 {
			prof.UpgradeAvailable = false
			prof.CommitsBehind = 0
			return
		}
		prof.UpgradeAvailable = true
		if ahead, _ := fetchCommitsBehindStatus(ctx, installed, tag, allowFetch); ahead > 0 {
			prof.CommitsBehind = ahead
		} else {
			prof.CommitsBehind = 0
		}
		return
	}

	ahead, compared := fetchCommitsBehindStatus(ctx, installed, tag, allowFetch)
	if compared && ahead > 0 {
		prof.UpgradeAvailable = true
		prof.CommitsBehind = ahead
	} else {
		prof.UpgradeAvailable = false
		prof.CommitsBehind = 0
	}
}

func isHexCommitSHA(s string) bool {
	s = strings.TrimSpace(s)
	if len(s) < 7 || len(s) > 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return true
}

func parseSemver(v string) (major, minor, patch int, prerelease string, ok bool) {
	v = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(v), "v"))
	if v == "" {
		return 0, 0, 0, "", false
	}
	if plus := strings.IndexByte(v, '+'); plus >= 0 {
		v = v[:plus]
	}
	if dash := strings.IndexByte(v, '-'); dash >= 0 {
		prerelease = v[dash+1:]
		v = v[:dash]
	}
	parts := strings.Split(v, ".")
	if len(parts) != 3 {
		return 0, 0, 0, "", false
	}
	var err error
	if major, err = strconv.Atoi(parts[0]); err != nil || major < 0 {
		return 0, 0, 0, "", false
	}
	if minor, err = strconv.Atoi(parts[1]); err != nil || minor < 0 {
		return 0, 0, 0, "", false
	}
	if patch, err = strconv.Atoi(parts[2]); err != nil || patch < 0 {
		return 0, 0, 0, "", false
	}
	return major, minor, patch, prerelease, true
}

func compareSemver(a, b string) (int, bool) {
	majA, minA, patA, preA, okA := parseSemver(a)
	majB, minB, patB, preB, okB := parseSemver(b)
	if !okA || !okB {
		return 0, false
	}
	if majA != majB {
		if majA < majB {
			return -1, true
		}
		return 1, true
	}
	if minA != minB {
		if minA < minB {
			return -1, true
		}
		return 1, true
	}
	if patA != patB {
		if patA < patB {
			return -1, true
		}
		return 1, true
	}
	if preA == preB {
		return 0, true
	}
	if preA == "" {
		return 1, true
	}
	if preB == "" {
		return -1, true
	}
	if preA < preB {
		return -1, true
	}
	return 1, true
}

// fetchLatestUpstreamRelease returns the latest upstream release from the cache when the last
// attempt (hit or miss) is younger than releaseCacheTTL, otherwise queries GitHub when
// allowFetch permits. A stale hit is still returned when the refresh fails or is not allowed;
// an empty tag means no release is known.
func fetchLatestUpstreamRelease(ctx context.Context, allowFetch bool) (tag, commit, htmlURL, publishedAt string) {
	releaseCacheMu.Lock()
	cached := releaseCache
	releaseCacheMu.Unlock()
	hitFresh := cached.tag != "" && time.Since(cached.fetchedAt) < releaseCacheTTL
	missFresh := !cached.failedAt.IsZero() && time.Since(cached.failedAt) < releaseCacheTTL
	if hitFresh || missFresh || !allowFetch {
		return cached.tag, cached.commit, cached.htmlURL, cached.publishedAt
	}

	reqCtx, cancel := context.WithTimeout(ctx, githubAPITimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, releaseAPIBaseURL+githubLatestReleasePath, nil)
	if err != nil {
		return recordReleaseFetchFailure()
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "kube-agents-mcp/1.0")
	resp, err := releaseHTTPClient.Do(req)
	if err != nil {
		return recordReleaseFetchFailure()
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return recordReleaseFetchFailure()
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return recordReleaseFetchFailure()
	}
	var payload struct {
		TagName         string `json:"tag_name"`
		TargetCommitish string `json:"target_commitish"`
		HTMLURL         string `json:"html_url"`
		PublishedAt     string `json:"published_at"`
	}
	if json.Unmarshal(body, &payload) != nil || strings.TrimSpace(payload.TagName) == "" {
		return recordReleaseFetchFailure()
	}
	tag = strings.TrimSpace(payload.TagName)
	rawCommit := strings.TrimSpace(payload.TargetCommitish)
	if isHexCommitSHA(rawCommit) {
		commit = rawCommit
	}
	htmlURL = strings.TrimSpace(payload.HTMLURL)
	publishedAt = strings.TrimSpace(payload.PublishedAt)

	releaseCacheMu.Lock()
	releaseCache.tag = tag
	releaseCache.commit = commit
	releaseCache.htmlURL = htmlURL
	releaseCache.publishedAt = publishedAt
	releaseCache.fetchedAt = time.Now()
	releaseCache.failedAt = time.Time{}
	releaseCache.dirty = true
	if releaseCache.compareCache == nil {
		releaseCache.compareCache = make(map[string]cachedCompareResult)
	}
	releaseCacheMu.Unlock()
	return tag, commit, htmlURL, publishedAt
}

// recordReleaseFetchFailure caches a failed lookup for releaseCacheTTL and returns whatever
// (possibly stale) release the cache still holds.
func recordReleaseFetchFailure() (tag, commit, htmlURL, publishedAt string) {
	releaseCacheMu.Lock()
	defer releaseCacheMu.Unlock()
	releaseCache.failedAt = time.Now()
	releaseCache.dirty = true
	return releaseCache.tag, releaseCache.commit, releaseCache.htmlURL, releaseCache.publishedAt
}

// releaseCachePathFor returns the release cache file that sits next to store's config file,
// or "" when the store has no on-disk home.
func releaseCachePathFor(store *ConfigStore) string {
	if store == nil || store.Path() == "" {
		return ""
	}
	return filepath.Join(filepath.Dir(store.Path()), releaseCacheFileName)
}

// LoadReleaseCacheForStore primes the in-memory release cache from store's sibling cache file.
func LoadReleaseCacheForStore(store *ConfigStore) {
	loadReleaseCache(releaseCachePathFor(store))
}

// SaveReleaseCacheForStore writes the in-memory release cache to store's sibling cache file.
func SaveReleaseCacheForStore(store *ConfigStore) {
	saveReleaseCache(releaseCachePathFor(store))
}

// loadReleaseCache primes the in-memory release cache from path when the file records a more
// recent hit or miss than memory does. A missing or unreadable file is ignored.
func loadReleaseCache(path string) {
	if path == "" {
		return
	}
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return
	}
	var pr persistedRelease
	if json.Unmarshal(raw, &pr) != nil {
		return
	}
	releaseCacheMu.Lock()
	defer releaseCacheMu.Unlock()
	if pr.Tag != "" && pr.FetchedAt.After(releaseCache.fetchedAt) {
		releaseCache.tag = pr.Tag
		if isHexCommitSHA(pr.Commit) {
			releaseCache.commit = pr.Commit
		} else {
			releaseCache.commit = ""
		}
		releaseCache.htmlURL = pr.HTMLURL
		releaseCache.publishedAt = pr.PublishedAt
		releaseCache.fetchedAt = pr.FetchedAt
	}
	if pr.FailedAt.After(releaseCache.failedAt) {
		releaseCache.failedAt = pr.FailedAt
	}
}

// saveReleaseCache writes the in-memory release cache to path when a lookup ran since it was
// last written; writes are best effort.
func saveReleaseCache(path string) {
	if path == "" {
		return
	}
	releaseCacheMu.Lock()
	if !releaseCache.dirty {
		releaseCacheMu.Unlock()
		return
	}
	pr := persistedRelease{
		Tag:         releaseCache.tag,
		Commit:      releaseCache.commit,
		HTMLURL:     releaseCache.htmlURL,
		PublishedAt: releaseCache.publishedAt,
		FetchedAt:   releaseCache.fetchedAt,
		FailedAt:    releaseCache.failedAt,
	}
	releaseCache.dirty = false
	releaseCacheMu.Unlock()
	if data, err := json.MarshalIndent(pr, "", "  "); err == nil {
		_ = WriteFileAtomic(path, data)
	}
}

func fetchCommitsBehindStatus(ctx context.Context, installedRef, latestTag string, allowFetch bool) (int, bool) {
	key := installedRef + "..." + latestTag
	releaseCacheMu.Lock()
	if releaseCache.compareCache != nil {
		if v, ok := releaseCache.compareCache[key]; ok {
			releaseCacheMu.Unlock()
			return v.aheadBy, v.compared
		}
	}
	releaseCacheMu.Unlock()
	if !allowFetch {
		return 0, false
	}

	reqCtx, cancel := context.WithTimeout(ctx, githubAPITimeout)
	defer cancel()
	compareURL := releaseAPIBaseURL + fmt.Sprintf(githubCompareAPIFormat, url.PathEscape(installedRef), url.PathEscape(latestTag))
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, compareURL, nil)
	if err != nil {
		return 0, false
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "kube-agents-mcp/1.0")
	resp, err := releaseHTTPClient.Do(req)
	if err != nil {
		return 0, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, false
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return 0, false
	}
	var payload struct {
		Status  string `json:"status"`
		AheadBy int    `json:"ahead_by"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return 0, false
	}
	ahead := 0
	if payload.AheadBy > 0 && (payload.Status == "" || payload.Status == "ahead" || payload.Status == "diverged") {
		ahead = payload.AheadBy
	}
	res := cachedCompareResult{
		aheadBy:  ahead,
		compared: true,
	}
	releaseCacheMu.Lock()
	if releaseCache.compareCache == nil {
		releaseCache.compareCache = make(map[string]cachedCompareResult)
	}
	releaseCache.compareCache[key] = res
	releaseCacheMu.Unlock()
	return res.aheadBy, res.compared
}

var (
	envTokenNotSentWarning     sync.Once
	envTokenTargetMovedWarning sync.Once
)

// EnvMCPTokenFor returns the KUBE_AGENTS_MCP_TOKEN override when it belongs to prof, and ""
// otherwise. saved holds the saved deployment profiles by name. With one saved, the token is
// that profile's. With several, it is the profile's KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT names: by
// its saved name, or by the base name it was saved under before a cross-context collision
// renamed it to "<kubeContext>/<name>", as long as exactly one profile answers to it. With the
// selector unset, naming none, or naming more than one, the token is sent nowhere.
//
// The token is bound to its profile's saved target, not its name: prof gets it only when it
// still points where the saved profile does (kube context, namespace, and direct MCP URL). A
// connection that overrides any of them gets no env token, so the target's own Secret is used
// where there is one. Either refusal logs one warning. A token meant for one install is so
// never sent to another.
func EnvMCPTokenFor(prof DeploymentProfile, saved map[string]DeploymentProfile) string {
	token := strings.TrimSpace(os.Getenv(EnvMCPToken))
	if token == "" {
		return ""
	}
	owner, ok := envTokenOwner(saved)
	if !ok || owner != strings.TrimSpace(prof.Name) {
		return ""
	}
	if !sameConnectionTarget(prof, saved[owner]) {
		envTokenTargetMovedWarning.Do(func() {
			slog.Warn(EnvMCPToken+" is not sent: the connection overrides its deployment's saved target",
				"deployment", owner)
		})
		return ""
	}
	return token
}

// envTokenOwner names the saved profile KUBE_AGENTS_MCP_TOKEN belongs to (see EnvMCPTokenFor).
func envTokenOwner(saved map[string]DeploymentProfile) (string, bool) {
	if len(saved) == 1 {
		for name := range saved {
			return name, true
		}
	}
	scope := strings.TrimSpace(os.Getenv(EnvMCPTokenDeployment))
	var matches []string
	if scope != "" {
		for name, p := range saved {
			if name == scope || deploymentBaseName(name, p) == scope {
				matches = append(matches, name)
			}
		}
	}
	if len(matches) != 1 {
		envTokenNotSentWarning.Do(func() {
			slog.Warn(EnvMCPToken+" is not sent to any deployment: more than one is configured, and "+
				EnvMCPTokenDeployment+" does not name exactly one of them", "deployment_selector", scope)
		})
		return "", false
	}
	return matches[0], true
}

// deploymentBaseName is the name a profile saved as name had before a cross-context collision
// renamed it to "<kubeContext>/<name>" (resolveDeploymentKeyLocked), or name itself.
func deploymentBaseName(name string, p DeploymentProfile) string {
	if ctx := strings.TrimSpace(p.KubeContext); ctx != "" {
		return strings.TrimPrefix(name, ctx+"/")
	}
	return name
}

// sameConnectionTarget reports whether prof connects where the saved profile does.
func sameConnectionTarget(prof, saved DeploymentProfile) bool {
	return strings.TrimSpace(prof.KubeContext) == strings.TrimSpace(saved.KubeContext) &&
		strings.TrimSpace(prof.Namespace) == strings.TrimSpace(saved.Namespace) &&
		strings.TrimRight(strings.TrimSpace(prof.DirectMCPURL), "/") == strings.TrimRight(strings.TrimSpace(saved.DirectMCPURL), "/")
}
