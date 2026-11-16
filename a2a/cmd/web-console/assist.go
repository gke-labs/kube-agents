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

// POST /api/assist: the model edits a feedback draft for clarity.
//
// The page sends the title and body a user typed; the model returns them
// with spelling, grammar and structure fixed and nothing added. The call
// goes to the same LiteLLM gateway the agent uses (in the cluster, the same
// Vertex project), with one message and no tools, so it costs one
// completion and never reaches the agent's session store. The text leaves
// the browser only to that gateway. The route takes the same request
// checks as a chat turn: the console header, a JSON body, same origin.
//
// GET /api/release: the version this install runs and the newest release
// on GitHub. The chart stamps its version and appVersion only when a
// release is published; a checkout installed from source still carries the
// placeholder 0.1.0, so the running version is read from the image tag: a
// release tag when the install came from a release, a commit when it came
// from source. The release check is one anonymous read of the GitHub API
// per hour, which an installation may turn off.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	envLiteLLMURL      = "LITELLM_URL"
	envAssistModel     = "ASSIST_MODEL"
	envReleaseCheck    = "RELEASE_CHECK"
	envReleaseRepo     = "RELEASE_REPO"
	defaultAssistModel = "model-default"
	defaultReleaseRepo = "gke-labs/kube-agents"
	// chartPlaceholderVersion is what Chart.yaml carries before
	// release-publish.yml stamps it; it says nothing about the build.
	chartPlaceholderVersion = "0.1.0"

	assistTimeout = 60 * time.Second
	// assistMaxChars bounds the draft the model is asked to edit.
	assistMaxChars = 12000
	// assistMaxTokens bounds the reply; a polished draft is about as long
	// as the draft.
	assistMaxTokens    = 4096
	assistSystemPrompt = `You are an editor for feedback reports sent to the kube-agents project (bug reports, feature requests, questions, usage reports).
Edit the user's draft for spelling, grammar, clarity and a plain, direct style. Keep every fact, number, name, path, command and code exactly as written. Do not add claims, steps, causes or details the draft does not contain; where a section is empty, leave it empty. Keep the Markdown headings and their order. Keep the same language as the draft. Keep the title to one line under 100 characters.
Reply with one JSON object and nothing else: {"title": string, "body": string, "notes": [string]} where notes lists, in a few short items, what you changed. If nothing needed changing, return the text unchanged and notes ["No changes needed."].`

	// releaseTimeout bounds one GitHub call; releaseWalkTimeout bounds the
	// whole check, which for a build from source is one call per release.
	releaseTimeout     = 6 * time.Second
	releaseWalkTimeout = 25 * time.Second
	releaseCacheTTL    = time.Hour
	// releaseFailureCacheTTL is how long an answer whose GitHub read failed
	// (an anonymous rate limit, an egress blip, the walk's own deadline) is
	// served before GitHub is asked again: long enough that every page load
	// does not repeat a refused call, short enough not to pin the failure.
	releaseFailureCacheTTL = time.Minute
	// releaseListLimit is how many published releases a build from source
	// is placed against, newest first; each costs one compare call per hour.
	releaseListLimit = 10
	// releaseStampSlack is how many commits a build may lack from a release
	// tag and still count as containing it. release-publish.yml cuts each
	// release as main plus one "chore(release): stamp release version"
	// commit, so a build from main is always exactly one commit short of
	// the release it was made after.
	releaseStampSlack = 1
	gitHubAPIBase     = "https://api.github.com"
	gitHubAccept      = "application/vnd.github+json"
	gitHubUserAgent   = "kube-agents-web-console"
	// The compare statuses GitHub reports for base...head.
	gitHubCompareIdentical = "identical"
	gitHubCompareAhead     = "ahead"
	gitHubCompareBehind    = "behind"
	gitHubCompareDiverged  = "diverged"

	// errCodeDraftTooLong is the error code for a draft over assistMaxChars
	// or over the request body limit.
	errCodeDraftTooLong = "draft_too_long"
)

var (
	// releaseTagPattern is a release tag as the project cuts them (0.9.0),
	// with or without a leading v.
	releaseTagPattern = regexp.MustCompile(`^v?(\d+)\.(\d+)\.(\d+)$`)
	commitSHAPattern  = regexp.MustCompile(`^[0-9a-f]{7,40}$`)
	releaseRepoPat    = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
)

type assistRequest struct {
	Kind  string `json:"kind"`
	Title string `json:"title"`
	Body  string `json:"body"`
	// Scope is "all", "title" or "body": which fields to edit.
	Scope string `json:"scope"`
}

type assistResponse struct {
	Title string   `json:"title"`
	Body  string   `json:"body"`
	Notes []string `json:"notes"`
	Model string   `json:"model,omitempty"`
}

func (s *server) handleAssist(w http.ResponseWriter, r *http.Request) {
	if s.cfg.LiteLLMURL == "" {
		writeError(w, http.StatusNotImplemented, "assist_unavailable", "This console has no inference gateway configured for drafting help.")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req assistRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		// A body past the reader's limit is a draft too long, not bad JSON.
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeError(w, http.StatusRequestEntityTooLarge, errCodeDraftTooLong, fmt.Sprintf("The draft is over %d characters.", assistMaxChars))
			return
		}
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}
	req.Title, req.Body = strings.TrimSpace(req.Title), strings.TrimSpace(req.Body)
	if req.Title == "" && req.Body == "" {
		writeError(w, http.StatusBadRequest, "empty_draft", "There is nothing to edit yet.")
		return
	}
	// The limit is in characters, as the message says, not UTF-8 bytes.
	if utf8.RuneCountInString(req.Title)+utf8.RuneCountInString(req.Body) > assistMaxChars {
		writeError(w, http.StatusRequestEntityTooLarge, errCodeDraftTooLong, fmt.Sprintf("The draft is over %d characters.", assistMaxChars))
		return
	}
	switch req.Scope {
	case "", "all":
		req.Scope = "all"
	case "title", "body":
	default:
		writeError(w, http.StatusBadRequest, "invalid_scope", "scope must be all, title or body.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), assistTimeout)
	defer cancel()
	out, err := s.assist(ctx, req)
	if err != nil {
		writeError(w, http.StatusBadGateway, "assist_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, out)
}

// assist asks the gateway for the edited draft. The scope narrows what the
// model may change; the other field is returned as sent.
func (s *server) assist(ctx context.Context, req assistRequest) (assistResponse, error) {
	var user strings.Builder
	fmt.Fprintf(&user, "Report kind: %s\nEdit scope: %s\n\n", req.Kind, req.Scope)
	if req.Scope != "body" {
		fmt.Fprintf(&user, "TITLE:\n%s\n\n", req.Title)
	}
	if req.Scope != "title" {
		fmt.Fprintf(&user, "BODY:\n%s\n", req.Body)
	}
	model := s.cfg.AssistModel
	if model == "" {
		model = defaultAssistModel
	}
	completion, err := s.assistCompletion(ctx, model, user.String(), true)
	if err != nil && errors.Is(err, errAssistRejected) {
		// Some models behind the gateway (Gemma on Vertex, some MaaS
		// endpoints) refuse response_format; the prompt asks for JSON
		// anyway and parseAssistReply reads it out of a fence.
		completion, err = s.assistCompletion(ctx, model, user.String(), false)
	}
	if err != nil {
		return assistResponse{}, err
	}
	out, err := parseAssistReply(completion.Choices[0].Message.Content)
	if err != nil {
		return assistResponse{}, err
	}
	// The scope is a promise to the page: an out-of-scope field comes back
	// as it was sent, whatever the model returned.
	if req.Scope == "body" || out.Title == "" {
		out.Title = req.Title
	}
	if req.Scope == "title" || out.Body == "" {
		out.Body = req.Body
	}
	if out.Notes == nil {
		out.Notes = []string{}
	}
	out.Model = completion.Model
	return out, nil
}

// errAssistRejected is a 400 from the gateway on the first attempt, which
// may be the model refusing response_format rather than the prompt. Other
// 4xx answers (an unknown model, an auth refusal, a rate limit) say nothing
// about response_format and are not retried.
var errAssistRejected = errors.New("the inference gateway rejected the request")

type assistCompletionReply struct {
	Model   string `json:"model"`
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
}

// assistCompletion makes one chat completion call. jsonMode asks for a
// JSON object through response_format; off, the prompt alone asks for it.
func (s *server) assistCompletion(ctx context.Context, model, user string, jsonMode bool) (assistCompletionReply, error) {
	var completion assistCompletionReply
	payload := map[string]any{
		"model":       model,
		"temperature": 0.2,
		"max_tokens":  assistMaxTokens,
		"messages": []map[string]string{
			{"role": "system", "content": assistSystemPrompt},
			{"role": "user", "content": user},
		},
	}
	if jsonMode {
		payload["response_format"] = map[string]string{"type": "json_object"}
	}
	b, _ := json.Marshal(payload)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.LiteLLMURL+"/v1/chat/completions", bytes.NewReader(b))
	if err != nil {
		return completion, err
	}
	httpReq.Header.Set("Content-Type", contentTypeJSON)
	resp, err := s.client.Do(httpReq)
	if err != nil {
		return completion, fmt.Errorf("could not reach the inference gateway: %w", err)
	}
	defer drainAndClose(resp)
	if resp.StatusCode != http.StatusOK {
		detail := fmt.Errorf("inference gateway answered HTTP %d: %s", resp.StatusCode, upstreamErrorDetail(resp))
		if jsonMode && resp.StatusCode == http.StatusBadRequest {
			return completion, fmt.Errorf("%w: %v", errAssistRejected, detail)
		}
		return completion, detail
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&completion); err != nil || len(completion.Choices) == 0 {
		return completion, errors.New("the inference gateway returned no completion")
	}
	return completion, nil
}

// parseAssistReply reads the JSON object out of the model's reply, with or
// without a code fence around it.
func parseAssistReply(text string) (assistResponse, error) {
	var out assistResponse
	t := strings.TrimSpace(text)
	if i := strings.Index(t, "{"); i >= 0 {
		if j := strings.LastIndex(t, "}"); j > i {
			t = t[i : j+1]
		}
	}
	if err := json.Unmarshal([]byte(t), &out); err != nil {
		return out, errors.New("the model's reply was not the JSON object the console asked for")
	}
	return out, nil
}

// --- Release check ---

type releaseResponse struct {
	// Running is the release this install runs, as the page prints it: the
	// release tag, or for a build from source the newest release that build
	// contains. Commits are never shown; an operator reasons in releases.
	Running string `json:"running"`
	// RunningKind is "release" for an image built from a release tag,
	// "source" for a build from source resolved to the release it contains,
	// or "unknown" when the image tag is neither.
	RunningKind string `json:"running_kind"`
	// RunningDisplay is the display label for the running version, e.g. "0.13.0+"
	// when custom PRs/patches are applied.
	RunningDisplay string `json:"running_display,omitempty"`
	// ChartVersion is the chart's stamped version, or "" when the chart
	// still carries the placeholder (a source checkout).
	ChartVersion string `json:"chart_version,omitempty"`
	Latest       string `json:"latest,omitempty"`
	LatestURL    string `json:"latest_url,omitempty"`
	LatestAt     string `json:"latest_at,omitempty"`
	// Status is "latest", "behind", "ahead", "unknown" or "disabled";
	// Detail says how far or why not. "ahead" is a build newer than the
	// newest published release.
	Status     string      `json:"status"`
	Detail     string      `json:"detail,omitempty"`
	CheckedAt  string      `json:"checked_at,omitempty"`
	IsPatched  bool        `json:"is_patched"`
	AppliedPRs []AppliedPR `json:"applied_prs,omitempty"`
}

type releaseCache struct {
	refresh sync.Mutex
	mu      sync.Mutex
	value   *releaseResponse
	readAt  time.Time
	// imageTag and chartVersion are the live install version the cached
	// answer was classified from. A request that reads a different live
	// version (a PlatformAgent tag edited in place, an Argo sync) misses.
	imageTag     string
	chartVersion string
	// failed marks an answer whose GitHub read failed; it is served for
	// releaseFailureCacheTTL instead of releaseCacheTTL.
	failed bool
}

// fresh returns the cached answer when it was classified from this live
// version and is inside its TTL. The caller holds c.mu.
func (c *releaseCache) fresh(imageTag, chartVersion string) (releaseResponse, bool) {
	if c.value == nil || c.imageTag != imageTag || c.chartVersion != chartVersion {
		return releaseResponse{}, false
	}
	ttl := releaseCacheTTL
	if c.failed {
		ttl = releaseFailureCacheTTL
	}
	if time.Since(c.readAt) >= ttl {
		return releaseResponse{}, false
	}
	return *c.value, true
}

// gitHubRelease is the part of a GitHub release the check reads.
type gitHubRelease struct {
	TagName     string `json:"tag_name"`
	HTMLURL     string `json:"html_url"`
	PublishedAt string `json:"published_at"`
	Prerelease  bool   `json:"prerelease"`
	Draft       bool   `json:"draft"`
}

// gitHubCompare is the part of a compare answer the check reads: how the
// head (this build) stands against the base (a release tag).
type gitHubCompare struct {
	Status   string `json:"status"`
	AheadBy  int    `json:"ahead_by"`
	BehindBy int    `json:"behind_by"`
}

func (s *server) handleRelease(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), releaseWalkTimeout+time.Second)
	defer cancel()
	writeJSON(w, http.StatusOK, s.currentRelease(ctx))
}

// runningVersion classifies the image tag: a release tag, a commit to
// resolve against the release list, or neither. The chart version is used
// only when it was stamped by a release build.
func runningVersion(imageTag, chartVersion string) (running, kind, chart string) {
	chart = chartVersion
	if chartVersion == chartPlaceholderVersion || chartVersion == "" {
		chart = ""
	}
	tag := strings.TrimSpace(imageTag)
	switch {
	case releaseTagPattern.MatchString(tag):
		return strings.TrimPrefix(tag, "v"), "release", chart
	case commitSHAPattern.MatchString(tag):
		return tag, "commit", chart
	case chart != "" && releaseTagPattern.MatchString(chart):
		return strings.TrimPrefix(chart, "v"), "release", chart
	case tag != "":
		return tag, "unknown", chart
	}
	return "unknown", "unknown", chart
}

// currentRelease reads the live install version on every call and serves
// the cached answer only when it was classified from that version, so a
// tag changed outside the console shows on the next request. A failed
// GitHub read is kept for releaseFailureCacheTTL, and one the caller
// abandoned (a closed tab, an expired request) is not kept at all.
func (s *server) currentRelease(ctx context.Context) releaseResponse {
	imageTag, chartVersion := s.liveInstallVersion(ctx)
	s.releaseInfo.mu.Lock()
	if v, ok := s.releaseInfo.fresh(imageTag, chartVersion); ok {
		s.releaseInfo.mu.Unlock()
		return v
	}
	s.releaseInfo.mu.Unlock()
	s.releaseInfo.refresh.Lock()
	defer s.releaseInfo.refresh.Unlock()
	s.releaseInfo.mu.Lock()
	if v, ok := s.releaseInfo.fresh(imageTag, chartVersion); ok {
		s.releaseInfo.mu.Unlock()
		return v
	}
	s.releaseInfo.mu.Unlock()
	v, err := s.classifyRelease(ctx, imageTag, chartVersion)
	if err != nil && ctx.Err() != nil {
		return v
	}
	s.releaseInfo.mu.Lock()
	s.releaseInfo.value = &v
	s.releaseInfo.readAt = time.Now()
	s.releaseInfo.imageTag, s.releaseInfo.chartVersion = imageTag, chartVersion
	s.releaseInfo.failed = err != nil
	s.releaseInfo.mu.Unlock()
	return v
}

// readRelease reads the live install version and classifies it, uncached.
func (s *server) readRelease(ctx context.Context) releaseResponse {
	imageTag, chartVersion := s.liveInstallVersion(ctx)
	v, _ := s.classifyRelease(ctx, imageTag, chartVersion)
	return v
}

// classifyRelease classifies the given live install version against
// GitHub's release list. The error is non-nil when a GitHub read failed, in
// which case the answer still carries the running version and the reason.
func (s *server) classifyRelease(ctx context.Context, imageTag, chartVersion string) (releaseResponse, error) {
	running, kind, chart := runningVersion(imageTag, chartVersion)
	out := releaseResponse{Running: running, RunningKind: kind, ChartVersion: chart, Status: "unknown", RunningDisplay: running}
	if kind == "commit" {
		// Until the release list resolves it, a commit has no release to show.
		out.Running, out.RunningKind = "unknown", "source"
		out.RunningDisplay = "unknown"
	}
	prs := s.getAppliedPRs(ctx)
	if len(prs) > 0 {
		out.IsPatched = true
		out.AppliedPRs = prs
		if out.Running != "unknown" {
			out.RunningDisplay = out.Running + "+"
		}
	}
	if !s.cfg.ReleaseCheck {
		out.Status = "disabled"
		out.Detail = "Release checks are off (" + envReleaseCheck + ")."
		if kind == "commit" {
			out.Detail += " A build from source cannot be named without them."
		}
		if out.IsPatched {
			out.Detail += fmt.Sprintf(" %d PR patch%s applied directly.", len(prs), plural(len(prs)))
		}
		return out, nil
	}
	ctx, cancel := context.WithTimeout(ctx, releaseWalkTimeout)
	defer cancel()
	var releases []gitHubRelease
	if err := s.github(ctx, fmt.Sprintf("/repos/%s/releases?per_page=%d", s.cfg.ReleaseRepo, releaseListLimit), &releases); err != nil {
		out.Detail = "Could not read the releases from GitHub: " + err.Error()
		return out, err
	}
	published := releases[:0:0]
	for _, r := range releases {
		if !r.Draft && !r.Prerelease && releaseTagPattern.MatchString(r.TagName) {
			published = append(published, r)
		}
	}
	if len(published) == 0 {
		out.Detail = "GitHub lists no published release for " + s.cfg.ReleaseRepo + "."
		return out, nil
	}
	// GitHub lists releases newest-created first, which is not the highest
	// version once a patch to an older line is published.
	latest := published[0]
	for _, r := range published[1:] {
		if compareReleases(strings.TrimPrefix(latest.TagName, "v"), strings.TrimPrefix(r.TagName, "v")) < 0 {
			latest = r
		}
	}
	out.Latest, out.LatestURL, out.LatestAt = strings.TrimPrefix(latest.TagName, "v"), latest.HTMLURL, latest.PublishedAt
	out.CheckedAt = time.Now().UTC().Format(time.RFC3339)
	switch kind {
	case "release":
		switch compareReleases(running, out.Latest) {
		case 0:
			if out.IsPatched {
				out.Status, out.Detail = "latest", fmt.Sprintf("This install runs release %s+ (%d PR%s applied); up to date with latest release %s.", running, len(prs), plural(len(prs)), out.Latest)
			} else {
				out.Status, out.Detail = "latest", "This install runs the newest release."
			}
		case -1:
			if out.IsPatched {
				out.Status, out.Detail = "behind", fmt.Sprintf("Release %s is available; this install runs %s+ (%d PR%s applied).", out.Latest, running, len(prs), plural(len(prs)))
			} else {
				out.Status, out.Detail = "behind", fmt.Sprintf("Release %s is available; this install runs %s.", out.Latest, running)
			}
		default:
			if out.IsPatched {
				out.Status, out.Detail = "ahead", fmt.Sprintf("This install runs %s+ (%d PR%s applied), newer than the latest published release %s.", running, len(prs), plural(len(prs)), out.Latest)
			} else {
				out.Status, out.Detail = "ahead", fmt.Sprintf("This install runs %s, newer than the latest published release %s.", running, out.Latest)
			}
		}
	case "commit":
		// Place the tag this answer was classified from, not the startup
		// environment's, which differs once the PlatformAgent tag moves.
		return out, s.resolveSourceBuild(ctx, &out, published, strings.TrimSpace(imageTag))
	default:
		out.Detail = fmt.Sprintf("The image tag is neither a release tag nor a commit; latest release is %s.", out.Latest)
	}
	return out, nil
}

// resolveSourceBuild names a build from source by the newest release it
// contains, walking the published releases newest first and asking GitHub
// how the build stands against each tag. A build contains a release when
// it lacks at most the release's own stamp commit (releaseStampSlack),
// whether GitHub reports it diverged from the tag or, for the commit the
// release was cut from, behind it. A build that contains the latest
// release is ahead of it; one that contains
// an older release runs that release and is behind the latest; one that
// contains none is older than every release listed. The error is a failed
// compare call.
func (s *server) resolveSourceBuild(ctx context.Context, out *releaseResponse, published []gitHubRelease, sha string) error {
	for _, rel := range published {
		var cmp gitHubCompare
		path := "/repos/" + s.cfg.ReleaseRepo + "/compare/" + url.PathEscape(rel.TagName) + "..." + url.PathEscape(sha)
		if err := s.github(ctx, path, &cmp); err != nil {
			out.Detail = fmt.Sprintf("A build from source; latest release is %s. Could not place the build against release %s: %s", out.Latest, strings.TrimPrefix(rel.TagName, "v"), err.Error())
			return err
		}
		version := strings.TrimPrefix(rel.TagName, "v")
		lacksOnlyStamp := (cmp.Status == gitHubCompareDiverged || cmp.Status == gitHubCompareBehind) && cmp.BehindBy <= releaseStampSlack
		contains := cmp.Status == gitHubCompareIdentical || cmp.Status == gitHubCompareAhead || lacksOnlyStamp
		if !contains {
			// The build is older than this release; try the next older one.
			continue
		}
		out.Running = version
		if out.IsPatched {
			out.RunningDisplay = version + "+"
		} else {
			out.RunningDisplay = version
		}
		switch {
		case cmp.AheadBy == 0 && version == out.Latest:
			out.Status, out.Detail = "latest", fmt.Sprintf("This build is the commit release %s was cut from.", version)
		case cmp.AheadBy == 0:
			out.Status, out.Detail = "behind", fmt.Sprintf("This build is the commit release %s was cut from; release %s is available.", version, out.Latest)
		case version == out.Latest:
			out.Status, out.Detail = "ahead", fmt.Sprintf("A build from source %d commits newer than the latest release %s: a pre-release build.", cmp.AheadBy, out.Latest)
		default:
			out.Status, out.Detail = "behind", fmt.Sprintf("A build from source made after release %s (%d commits later); release %s is available.", version, cmp.AheadBy, out.Latest)
		}
		return nil
	}
	oldest := strings.TrimPrefix(published[len(published)-1].TagName, "v")
	out.Running = "older than " + oldest
	if out.IsPatched {
		out.RunningDisplay = out.Running + "+"
	} else {
		out.RunningDisplay = out.Running
	}
	out.Status, out.Detail = "behind", fmt.Sprintf("A build from source older than every release checked (back to %s); release %s is available.", oldest, out.Latest)
	return nil
}

func (s *server) github(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.GitHubAPIBase+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", gitHubAccept)
	req.Header.Set("User-Agent", gitHubUserAgent)
	resp, err := s.githubClient.Do(req)
	if err != nil {
		return err
	}
	defer drainAndClose(resp)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(out)
}

// compareReleases orders two release tags numerically: -1, 0 or 1.
func compareReleases(a, b string) int {
	pa, pb := releaseTagPattern.FindStringSubmatch(a), releaseTagPattern.FindStringSubmatch(b)
	if pa == nil || pb == nil {
		return strings.Compare(a, b)
	}
	for i := 1; i <= 3; i++ {
		x, _ := strconv.Atoi(pa[i])
		y, _ := strconv.Atoi(pb[i])
		if x != y {
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
