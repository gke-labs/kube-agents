package mcp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	githubHost               = "github.com"
	githubAPIBaseURL         = "https://api.github.com"
	prStateMerged            = "MERGED"
	githubPRURLPrefix        = "https://github.com/"
	githubPRURLFormat        = "https://github.com/%s/pull/%d"
	githubAPIPRFormat        = "%s/repos/%s/pulls/%d"
	githubAPIPRFilesFormat   = "%s/repos/%s/pulls/%d/files?per_page=100"
	acceptHeader             = "Accept"
	authHeader               = "Authorization"
	userAgentHeader          = "User-Agent"
	userAgentValue           = "kube-agents-mcp/1.0"
	acceptGitHubJSON         = "application/vnd.github.v3+json"
	acceptGitHubDiff         = "application/vnd.github.v3.diff"
	bearerPrefix             = "Bearer "
	gitCredentialsRelPath    = ".git-credentials"
	envGHToken               = "GH_TOKEN"
	envGitHubToken           = "GITHUB_TOKEN"
	ghBinary                 = "gh"
	ghCmdPR                  = "pr"
	ghSubcmdView             = "view"
	ghSubcmdDiff             = "diff"
	ghFlagRepo               = "--repo"
	ghFlagJSON               = "--json"
	ghJSONFields             = "number,title,state,url,headRefName,baseRefName,additions,deletions,changedFiles,body,author,files"
	prFetchTimeout           = 15 * time.Second
	maxInlineDiffBytes       = 3200
	maxPRDiffReadBytes       = 10 << 20
	prURLRegexPattern        = `https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)`
	prShortRefRegexPattern   = `\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b`
	prNumberOnlyRegexPattern = `^#?(\d+)$`
	summaryFileName          = "PR_SUMMARY.md"
	diffFileNameFormat       = "pr-%d.diff"
	filesSubdirName          = "files"
	patchExt                 = ".patch"
	pathSeparatorReplacement = "__"
	diffHeaderPrefix         = "diff --git "
	bSidePrefix              = " b/"
	maxPatchBaseLen          = 190
	maxPatchSegmentTailLen   = 120
	patchHashPrefixBytes     = 6
	maxHTTPErrorBodyBytes    = 4096
)

var (
	prURLRegex        = regexp.MustCompile(prURLRegexPattern)
	prShortRefRegex   = regexp.MustCompile(prShortRefRegexPattern)
	prNumberOnlyRegex = regexp.MustCompile(prNumberOnlyRegexPattern)
	errInvalidPRRef   = errors.New("invalid pull request reference; provide a GitHub PR URL, owner/repo#number, or PR number")
)

// PRFileChange describes a single file modified in a pull request.
type PRFileChange struct {
	Path      string `json:"path"`
	Additions int    `json:"additions"`
	Deletions int    `json:"deletions"`
	Status    string `json:"status,omitempty"`
	PatchPath string `json:"patchPath,omitempty"`
}

// PRInspection holds the metadata, changed files, unified diff, and local file paths
// for opening a pull request natively in Antigravity's code and diff viewers.
type PRInspection struct {
	Repo            string         `json:"repo"`
	Number          int            `json:"number"`
	Title           string         `json:"title"`
	State           string         `json:"state"`
	Author          string         `json:"author,omitempty"`
	URL             string         `json:"url"`
	HeadRef         string         `json:"headRef"`
	BaseRef         string         `json:"baseRef"`
	Additions       int            `json:"additions"`
	Deletions       int            `json:"deletions"`
	ChangedFiles    int            `json:"changedFiles"`
	Body            string         `json:"body,omitempty"`
	Files           []PRFileChange `json:"files,omitempty"`
	UnifiedDiff     string         `json:"unifiedDiff,omitempty"`
	DiffFilePath    string         `json:"diffFilePath"`
	SummaryFilePath string         `json:"summaryFilePath"`
}

// ExtractPRReferences scans text for GitHub PR URLs and owner/repo#number references
// and returns normalized https://github.com/<owner>/<repo>/pull/<number> URLs.
func ExtractPRReferences(text string) []string {
	var out []string
	seen := make(map[string]bool)
	for _, m := range prURLRegex.FindAllStringSubmatch(text, -1) {
		if len(m) == 3 {
			num, err := strconv.Atoi(m[2])
			if err == nil && num > 0 {
				u := fmt.Sprintf(githubPRURLFormat, m[1], num)
				if !seen[u] {
					seen[u] = true
					out = append(out, u)
				}
			}
		}
	}
	for _, m := range prShortRefRegex.FindAllStringSubmatch(text, -1) {
		if len(m) == 3 {
			num, err := strconv.Atoi(m[2])
			if err == nil && num > 0 {
				u := fmt.Sprintf(githubPRURLFormat, m[1], num)
				if !seen[u] {
					seen[u] = true
					out = append(out, u)
				}
			}
		}
	}
	return out
}

// ParsePRReference resolves a PR URL, owner/repo#123, or #123 into (owner/repo, number).
func ParsePRReference(ref string, defaultRepo string) (string, int, error) {
	trimmed := strings.TrimSpace(ref)
	if trimmed == "" {
		return "", 0, errInvalidPRRef
	}
	if m := prURLRegex.FindStringSubmatch(trimmed); len(m) == 3 {
		num, err := strconv.Atoi(m[2])
		if err == nil && num > 0 {
			return m[1], num, nil
		}
	}
	if m := prShortRefRegex.FindStringSubmatch(trimmed); len(m) == 3 {
		num, err := strconv.Atoi(m[2])
		if err == nil && num > 0 {
			return m[1], num, nil
		}
	}
	if m := prNumberOnlyRegex.FindStringSubmatch(trimmed); len(m) == 2 {
		if strings.TrimSpace(defaultRepo) == "" {
			return "", 0, fmt.Errorf("%w: bare PR number %q requires a repo or connected deployment with githubRepo", errInvalidPRRef, trimmed)
		}
		num, err := strconv.Atoi(m[1])
		if err == nil && num > 0 {
			return strings.TrimSpace(defaultRepo), num, nil
		}
	}
	return "", 0, fmt.Errorf("%w: %q", errInvalidPRRef, trimmed)
}

// InspectPullRequest fetches PR metadata and unified diff, writes local diff/patch files
// for Antigravity's native viewer, and returns the PRInspection.
func InspectPullRequest(
	ctx context.Context,
	runner CommandRunner,
	apiBaseURL string,
	prRef string,
	defaultRepo string,
	outputDir string,
) (*PRInspection, error) {
	repo, num, err := ParsePRReference(prRef, defaultRepo)
	if err != nil {
		return nil, err
	}
	if apiBaseURL == "" {
		apiBaseURL = githubAPIBaseURL
	}

	fetchCtx, cancel := context.WithTimeout(ctx, prFetchTimeout)
	defer cancel()

	token := resolveGitHubToken()
	insp, err := fetchPRViaHTTP(fetchCtx, apiBaseURL, repo, num, token)
	if err != nil {
		httpErr := err
		// The model often hears "PR #220 on example-infra" and guesses an owner. If the guessed
		// repo is not the deployment's but shares its name, retry against the deployment's repo
		// before falling back to the gh CLI (which may not be installed on this host).
		if alt := sameNameRepo(repo, defaultRepo); alt != "" {
			if insp2, err2 := fetchPRViaHTTP(fetchCtx, apiBaseURL, alt, num, token); err2 == nil {
				insp, err, repo = insp2, nil, alt
			}
		}
		if err != nil {
			insp, err = fetchPRViaGHCLI(fetchCtx, runner, repo, num)
			if err != nil {
				var ghErr *githubHTTPError
				if errors.As(httpErr, &ghErr) && ghErr.StatusCode == http.StatusNotFound {
					return nil, fmt.Errorf("pull request %s#%d not found on GitHub (HTTP 404)", repo, num)
				}
				if errors.Is(err, exec.ErrNotFound) {
					return nil, httpErr
				}
				return nil, fmt.Errorf("%w (GitHub API: %v)", err, httpErr)
			}
		}
	}

	explicitOutputDir := strings.TrimSpace(outputDir) != ""
	if !explicitOutputDir {
		home, hErr := os.UserHomeDir()
		if hErr != nil || home == "" {
			outputDir = os.TempDir()
		} else {
			safeRepo := strings.ReplaceAll(repo, "/", "-")
			outputDir = filepath.Join(home, prCacheDirRelPath, fmt.Sprintf("%s-pr-%d", safeRepo, num))
		}
	}
	if err := os.MkdirAll(outputDir, dirPermMode); err != nil {
		if explicitOutputDir {
			return nil, fmt.Errorf("creating PR cache dir %s: %w", outputDir, err)
		}
		return insp, nil
	}

	diffPath := filepath.Join(outputDir, fmt.Sprintf(diffFileNameFormat, num))
	if err := writeFileAtomic(diffPath, []byte(insp.UnifiedDiff)); err != nil {
		if explicitOutputDir {
			return nil, fmt.Errorf("writing unified diff %s: %w", diffPath, err)
		}
		return insp, nil
	}
	insp.DiffFilePath = diffPath

	perFilePatches := splitUnifiedDiffByFile(insp.UnifiedDiff)
	filesDir := filepath.Join(outputDir, filesSubdirName)
	_ = os.MkdirAll(filesDir, dirPermMode)
	for i := range insp.Files {
		fPath := insp.Files[i].Path
		if patch, ok := perFilePatches[fPath]; ok {
			safeName := sanitizePatchFilename(fPath)
			patchFile := filepath.Join(filesDir, safeName)
			if writeFileAtomic(patchFile, []byte(patch)) == nil {
				insp.Files[i].PatchPath = patchFile
			}
		}
	}

	summaryPath := filepath.Join(outputDir, summaryFileName)
	insp.SummaryFilePath = summaryPath
	summaryMD := FormatPRInspectionMarkdown(insp)
	_ = writeFileAtomic(summaryPath, []byte(summaryMD))

	return insp, nil
}

func sanitizePatchFilename(fPath string) string {
	cleaned := strings.TrimLeft(strings.ReplaceAll(fPath, "\\", "/"), "/")
	var safeParts []string
	for _, seg := range strings.Split(cleaned, "/") {
		if seg == "" || seg == "." || seg == ".." {
			continue
		}
		safeParts = append(safeParts, seg)
	}
	if len(safeParts) == 0 {
		safeParts = []string{"file"}
	}
	joined := strings.Join(safeParts, pathSeparatorReplacement)
	if len(joined) > maxPatchBaseLen {
		sum := sha256.Sum256([]byte(fPath))
		hashPrefix := hex.EncodeToString(sum[:patchHashPrefixBytes])
		lastSeg := safeParts[len(safeParts)-1]
		if len(lastSeg) > maxPatchSegmentTailLen {
			lastSeg = lastSeg[len(lastSeg)-maxPatchSegmentTailLen:]
		}
		joined = hashPrefix + pathSeparatorReplacement + lastSeg
	}
	return joined + patchExt
}

// githubHTTPError is a non-2xx GitHub REST response, kept structured so callers can branch on
// the status code instead of on message text.
type githubHTTPError struct {
	Endpoint   string
	StatusCode int
	Body       string
}

func (e *githubHTTPError) Error() string {
	return fmt.Sprintf("GitHub API %s returned HTTP %d: %s", e.Endpoint, e.StatusCode, e.Body)
}

func fetchPRViaHTTP(ctx context.Context, apiBaseURL string, repo string, num int, token string) (*PRInspection, error) {
	prEndpoint := fmt.Sprintf(githubAPIPRFormat, strings.TrimRight(apiBaseURL, "/"), repo, num)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, prEndpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set(acceptHeader, acceptGitHubJSON)
	req.Header.Set(userAgentHeader, userAgentValue)
	if token != "" {
		req.Header.Set(authHeader, bearerPrefix+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, maxHTTPErrorBodyBytes))
		return nil, &githubHTTPError{Endpoint: prEndpoint, StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	var payload struct {
		Number       int    `json:"number"`
		Title        string `json:"title"`
		State        string `json:"state"`
		Merged       bool   `json:"merged"`
		MergedAt     string `json:"merged_at"`
		HTMLURL      string `json:"html_url"`
		Body         string `json:"body"`
		Additions    int    `json:"additions"`
		Deletions    int    `json:"deletions"`
		ChangedFiles int    `json:"changed_files"`
		User         struct {
			Login string `json:"login"`
		} `json:"user"`
		Head struct {
			Ref string `json:"ref"`
		} `json:"head"`
		Base struct {
			Ref string `json:"ref"`
		} `json:"base"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, err
	}

	// GitHub reports merged PRs as state "closed" with merged=true; a user reading "CLOSED" assumes
	// the change was rejected (soak round 13), so surface MERGED explicitly.
	state := strings.ToUpper(payload.State)
	if payload.Merged || strings.TrimSpace(payload.MergedAt) != "" {
		state = prStateMerged
	}
	insp := &PRInspection{
		Repo:         repo,
		Number:       num,
		Title:        payload.Title,
		State:        state,
		Author:       payload.User.Login,
		URL:          firstNonEmptyStr(payload.HTMLURL, fmt.Sprintf(githubPRURLFormat, repo, num)),
		HeadRef:      payload.Head.Ref,
		BaseRef:      payload.Base.Ref,
		Additions:    payload.Additions,
		Deletions:    payload.Deletions,
		ChangedFiles: payload.ChangedFiles,
		Body:         payload.Body,
	}

	filesEndpoint := fmt.Sprintf(githubAPIPRFilesFormat, strings.TrimRight(apiBaseURL, "/"), repo, num)
	if fReq, fErr := http.NewRequestWithContext(ctx, http.MethodGet, filesEndpoint, nil); fErr == nil {
		fReq.Header.Set(acceptHeader, acceptGitHubJSON)
		fReq.Header.Set(userAgentHeader, userAgentValue)
		if token != "" {
			fReq.Header.Set(authHeader, bearerPrefix+token)
		}
		if fResp, dErr := http.DefaultClient.Do(fReq); dErr == nil {
			var fileList []struct {
				Filename  string `json:"filename"`
				Additions int    `json:"additions"`
				Deletions int    `json:"deletions"`
				Status    string `json:"status"`
			}
			if fResp.StatusCode == http.StatusOK && json.NewDecoder(fResp.Body).Decode(&fileList) == nil {
				for _, f := range fileList {
					insp.Files = append(insp.Files, PRFileChange{
						Path:      f.Filename,
						Additions: f.Additions,
						Deletions: f.Deletions,
						Status:    f.Status,
					})
				}
			}
			_ = fResp.Body.Close()
		}
	}

	diffReq, err := http.NewRequestWithContext(ctx, http.MethodGet, prEndpoint, nil)
	if err == nil {
		diffReq.Header.Set(acceptHeader, acceptGitHubDiff)
		diffReq.Header.Set(userAgentHeader, userAgentValue)
		if token != "" {
			diffReq.Header.Set(authHeader, bearerPrefix+token)
		}
		if diffResp, dErr := http.DefaultClient.Do(diffReq); dErr == nil {
			if diffResp.StatusCode == http.StatusOK {
				rawDiff, _ := io.ReadAll(io.LimitReader(diffResp.Body, maxPRDiffReadBytes))
				insp.UnifiedDiff = string(rawDiff)
			}
			_ = diffResp.Body.Close()
		}
	}
	return insp, nil
}

func fetchPRViaGHCLI(ctx context.Context, runner CommandRunner, repo string, num int) (*PRInspection, error) {
	if runner == nil {
		runner = DefaultCommandRunner
	}
	numStr := strconv.Itoa(num)
	out, err := runner(ctx, ghBinary, ghCmdPR, ghSubcmdView, numStr, ghFlagRepo, repo, ghFlagJSON, ghJSONFields)
	if err != nil {
		return nil, fmt.Errorf("fetching PR %s#%d via gh: %w", repo, num, err)
	}
	var ghData struct {
		Number       int    `json:"number"`
		Title        string `json:"title"`
		State        string `json:"state"`
		URL          string `json:"url"`
		HeadRefName  string `json:"headRefName"`
		BaseRefName  string `json:"baseRefName"`
		Additions    int    `json:"additions"`
		Deletions    int    `json:"deletions"`
		ChangedFiles int    `json:"changedFiles"`
		Body         string `json:"body"`
		Author       struct {
			Login string `json:"login"`
		} `json:"author"`
		Files []struct {
			Path      string `json:"path"`
			Additions int    `json:"additions"`
			Deletions int    `json:"deletions"`
		} `json:"files"`
	}
	if err := json.Unmarshal(out, &ghData); err != nil {
		return nil, fmt.Errorf("parsing gh pr view output: %w", err)
	}
	diffOut, _ := runner(ctx, ghBinary, ghCmdPR, ghSubcmdDiff, numStr, ghFlagRepo, repo)
	insp := &PRInspection{
		Repo:         repo,
		Number:       num,
		Title:        ghData.Title,
		State:        ghData.State,
		Author:       ghData.Author.Login,
		URL:          firstNonEmptyStr(ghData.URL, fmt.Sprintf(githubPRURLFormat, repo, num)),
		HeadRef:      ghData.HeadRefName,
		BaseRef:      ghData.BaseRefName,
		Additions:    ghData.Additions,
		Deletions:    ghData.Deletions,
		ChangedFiles: ghData.ChangedFiles,
		Body:         ghData.Body,
		UnifiedDiff:  string(diffOut),
	}
	for _, f := range ghData.Files {
		insp.Files = append(insp.Files, PRFileChange{
			Path:      f.Path,
			Additions: f.Additions,
			Deletions: f.Deletions,
		})
	}
	return insp, nil
}

func resolveGitHubToken() string {
	if t := strings.TrimSpace(os.Getenv(envGHToken)); t != "" {
		return t
	}
	if t := strings.TrimSpace(os.Getenv(envGitHubToken)); t != "" {
		return t
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	root, err := os.OpenRoot(home)
	if err != nil {
		return ""
	}
	defer root.Close()
	raw, err := root.ReadFile(gitCredentialsRelPath)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		u, uErr := url.Parse(line)
		if uErr == nil && strings.EqualFold(u.Host, githubHost) && u.User != nil {
			if tok, ok := u.User.Password(); ok && tok != "" {
				return tok
			}
		}
	}
	return ""
}

func splitUnifiedDiffByFile(diff string) map[string]string {
	out := make(map[string]string)
	if strings.TrimSpace(diff) == "" {
		return out
	}
	chunks := strings.Split(diff, diffHeaderPrefix)
	for _, chunk := range chunks {
		if strings.TrimSpace(chunk) == "" {
			continue
		}
		fullChunk := diffHeaderPrefix + chunk
		firstLineEnd := strings.IndexByte(chunk, '\n')
		firstLine := chunk
		if firstLineEnd >= 0 {
			firstLine = chunk[:firstLineEnd]
		}
		if idx := strings.LastIndex(firstLine, bSidePrefix); idx >= 0 {
			filePath := strings.TrimSpace(firstLine[idx+len(bSidePrefix):])
			if filePath != "" {
				out[filePath] = fullChunk
			}
		}
	}
	return out
}

// sameNameRepo returns defaultRepo when guessed is a different owner/repo with the same repository
// name (e.g. guessed "someone/example-infra", default "example-org/example-infra"), else "".
func sameNameRepo(guessed, defaultRepo string) string {
	g := strings.TrimSpace(guessed)
	d := strings.TrimSpace(defaultRepo)
	if g == "" || d == "" || strings.EqualFold(g, d) {
		return ""
	}
	gi := strings.LastIndex(g, "/")
	di := strings.LastIndex(d, "/")
	if gi < 0 || di < 0 {
		return ""
	}
	if strings.EqualFold(g[gi+1:], d[di+1:]) {
		return d
	}
	return ""
}
