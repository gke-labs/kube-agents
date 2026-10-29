package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

const (
	appliedPRsAnnotation = "kubeagents.x-k8s.io/applied-prs"
	patchesTimeout       = 10 * time.Second
)

// AppliedPR describes a pull request or patch applied directly to the cluster
// install on top of the base release tag (e.g. 0.13.0+).
type AppliedPR struct {
	PR        int    `json:"pr"`
	Issue     int    `json:"issue,omitempty"`
	Title     string `json:"title"`
	URL       string `json:"url"`
	IssueURL  string `json:"issue_url,omitempty"`
	Component string `json:"component,omitempty"`
	AppliedAt string `json:"applied_at,omitempty"`
}

type patchesResponse struct {
	IsPatched      bool        `json:"is_patched"`
	RunningBase    string      `json:"running_base"`
	RunningDisplay string      `json:"running_display"`
	AppliedPRs     []AppliedPR `json:"applied_prs"`
}

type patchActionRequest struct {
	Action     string      `json:"action"` // "add", "remove", or "set"
	PR         int         `json:"pr"`
	Issue      int         `json:"issue,omitempty"`
	Title      string      `json:"title"`
	URL        string      `json:"url,omitempty"`
	IssueURL   string      `json:"issue_url,omitempty"`
	Component  string      `json:"component,omitempty"`
	AppliedPRs []AppliedPR `json:"applied_prs,omitempty"`
}

func (s *server) getAppliedPRs(ctx context.Context) []AppliedPR {
	s.patchesMu.RLock()
	if s.patchesLoaded {
		prs := make([]AppliedPR, len(s.cachedAppliedPRs))
		copy(prs, s.cachedAppliedPRs)
		s.patchesMu.RUnlock()
		return prs
	}
	s.patchesMu.RUnlock()

	s.patchesMu.Lock()
	defer s.patchesMu.Unlock()
	if s.patchesLoaded {
		prs := make([]AppliedPR, len(s.cachedAppliedPRs))
		copy(prs, s.cachedAppliedPRs)
		return prs
	}

	prs, ok := s.readAppliedPRsFromCluster(ctx)
	if !ok {
		return []AppliedPR{}
	}
	if prs == nil {
		prs = []AppliedPR{}
	}
	s.cachedAppliedPRs = prs
	s.patchesLoaded = true
	out := make([]AppliedPR, len(prs))
	copy(out, prs)
	return out
}

func (s *server) readAppliedPRsFromCluster(ctx context.Context) ([]AppliedPR, bool) {
	if s.kube == nil || s.cfg.PodNamespace == "" {
		return nil, true
	}
	var pa struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
	}
	path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
	if err := s.kube.get(ctx, path, &pa); err != nil {
		return nil, false
	}
	raw := pa.Metadata.Annotations[appliedPRsAnnotation]
	if strings.TrimSpace(raw) == "" {
		return nil, true
	}
	var prs []AppliedPR
	if err := json.Unmarshal([]byte(raw), &prs); err != nil {
		log.Printf(logPrefix+"could not parse %s annotation: %v", appliedPRsAnnotation, err)
		return nil, true
	}
	return prs, true
}

func (s *server) saveAppliedPRs(ctx context.Context, prs []AppliedPR) error {
	rawJSON, err := json.Marshal(prs)
	if err != nil {
		return fmt.Errorf("marshal applied prs: %w", err)
	}

	if s.kube != nil && s.cfg.PodNamespace != "" {
		patch := map[string]any{
			"metadata": map[string]any{
				"annotations": map[string]string{
					appliedPRsAnnotation: string(rawJSON),
				},
			},
		}
		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		if _, err := s.kube.doJSON(ctx, http.MethodPatch, path, patch, nil); err != nil {
			log.Printf(logPrefix+"could not persist %s on PlatformAgent: %v", appliedPRsAnnotation, err)
			return err
		}
	}

	s.patchesMu.Lock()
	s.cachedAppliedPRs = prs
	s.patchesLoaded = true
	s.patchesMu.Unlock()

	// Invalidate releaseInfo cache so /api/release reflects new state immediately.
	s.releaseInfo.mu.Lock()
	s.releaseInfo.value = nil
	s.releaseInfo.mu.Unlock()

	return nil
}

func isSafeHTTPURL(u string) bool {
	return strings.HasPrefix(u, "https://") || strings.HasPrefix(u, "http://")
}

func (s *server) resolveRunningPatchDisplay(ctx context.Context, prs []AppliedPR) (string, string) {
	running, chart := s.liveInstallVersion(ctx)
	runningTag, _, _ := runningVersion(running, chart)
	if runningTag == "" || runningTag == "unknown" {
		runningTag = strings.TrimPrefix(s.cfg.ImageTag, "v")
	}
	if runningTag == "" {
		runningTag = "unknown"
	}
	runningDisplay := runningTag
	if len(prs) > 0 && runningTag != "unknown" {
		runningDisplay = runningTag + "+"
	}
	return runningTag, runningDisplay
}

func (s *server) handlePatchesGet(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), patchesTimeout)
	defer cancel()

	prs := s.getAppliedPRs(ctx)
	runningTag, runningDisplay := s.resolveRunningPatchDisplay(ctx, prs)

	writeJSON(w, http.StatusOK, patchesResponse{
		IsPatched:      len(prs) > 0,
		RunningBase:    runningTag,
		RunningDisplay: runningDisplay,
		AppliedPRs:     prs,
	})
}

func (s *server) handlePatchesPost(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req patchActionRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), patchesTimeout)
	defer cancel()

	action := strings.ToLower(strings.TrimSpace(req.Action))
	if action == "" {
		action = "add"
	}

	var current []AppliedPR
	if action != "set" {
		if s.kube != nil && s.cfg.PodNamespace != "" {
			live, ok := s.readAppliedPRsFromCluster(ctx)
			if !ok {
				writeError(w, http.StatusInternalServerError, "read_failed", "Could not read current applied PRs from cluster before updating.")
				return
			}
			if live == nil {
				live = []AppliedPR{}
			}
			current = live
		} else {
			current = s.getAppliedPRs(ctx)
		}
	}
	repo := s.cfg.ReleaseRepo
	if repo == "" {
		repo = defaultReleaseRepo
	}

	var updated []AppliedPR

	switch action {
	case "set":
		updated = req.AppliedPRs
		if updated == nil {
			updated = []AppliedPR{}
		}
		for i := range updated {
			updated[i].URL = strings.TrimSpace(updated[i].URL)
			if !isSafeHTTPURL(updated[i].URL) && updated[i].PR > 0 {
				updated[i].URL = fmt.Sprintf("https://github.com/%s/pull/%d", repo, updated[i].PR)
			}
			updated[i].IssueURL = strings.TrimSpace(updated[i].IssueURL)
			if !isSafeHTTPURL(updated[i].IssueURL) {
				if updated[i].Issue > 0 {
					updated[i].IssueURL = fmt.Sprintf("https://github.com/%s/issues/%d", repo, updated[i].Issue)
				} else {
					updated[i].IssueURL = ""
				}
			}
			if updated[i].AppliedAt == "" {
				updated[i].AppliedAt = time.Now().UTC().Format(time.RFC3339)
			}
		}

	case "remove":
		if req.PR <= 0 {
			writeError(w, http.StatusBadRequest, "invalid_pr", "A positive PR number is required to remove an applied PR.")
			return
		}
		updated = make([]AppliedPR, 0, len(current))
		for _, p := range current {
			if p.PR != req.PR {
				updated = append(updated, p)
			}
		}

	case "add":
		if req.PR <= 0 {
			writeError(w, http.StatusBadRequest, "invalid_pr", "A positive PR number is required.")
			return
		}
		title := strings.TrimSpace(req.Title)
		if title == "" {
			title = fmt.Sprintf("PR #%d", req.PR)
		}
		urlStr := strings.TrimSpace(req.URL)
		if !isSafeHTTPURL(urlStr) {
			urlStr = fmt.Sprintf("https://github.com/%s/pull/%d", repo, req.PR)
		}
		issueURL := strings.TrimSpace(req.IssueURL)
		if !isSafeHTTPURL(issueURL) {
			if req.Issue > 0 {
				issueURL = fmt.Sprintf("https://github.com/%s/issues/%d", repo, req.Issue)
			} else {
				issueURL = ""
			}
		}
		component := strings.TrimSpace(req.Component)
		if component == "" {
			component = "system"
		}
		appliedAt := time.Now().UTC().Format(time.RFC3339)

		newPR := AppliedPR{
			PR:        req.PR,
			Issue:     req.Issue,
			Title:     title,
			URL:       urlStr,
			IssueURL:  issueURL,
			Component: component,
			AppliedAt: appliedAt,
		}

		found := false
		updated = make([]AppliedPR, len(current))
		copy(updated, current)
		for i, p := range updated {
			if p.PR == req.PR {
				updated[i] = newPR
				found = true
				break
			}
		}
		if !found {
			updated = append(updated, newPR)
		}

	default:
		writeError(w, http.StatusBadRequest, "invalid_action", fmt.Sprintf("Action %q not recognized; must be 'add', 'remove', or 'set'.", action))
		return
	}

	if err := s.saveAppliedPRs(ctx, updated); err != nil {
		writeError(w, http.StatusInternalServerError, "persist_failed", fmt.Sprintf("Could not persist applied PRs on cluster: %v", err))
		return
	}

	runningTag, runningDisplay := s.resolveRunningPatchDisplay(ctx, updated)

	writeJSON(w, http.StatusOK, patchesResponse{
		IsPatched:      len(updated) > 0,
		RunningBase:    runningTag,
		RunningDisplay: runningDisplay,
		AppliedPRs:     updated,
	})
}
