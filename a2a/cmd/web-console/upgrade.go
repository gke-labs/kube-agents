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

// POST /api/upgrade and GET /api/upgrade/status: in-cluster release upgrade
// runner for the web console.
//
// When triggered from the Upgrade dialog, POST /api/upgrade launches a
// single-run Pod (kube-agents-upgrade-runner) in the console's namespace that
// executes the release-pinned upgrade.sh script against the cluster, while
// GET /api/upgrade/status tails the pod's phase and container logs so the UI
// can monitor progress live and refresh the browser when the rollout completes.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
	// Embedded zone data so AUTO_UPGRADE_WINDOW can name an IANA zone in a
	// distroless image that ships no /usr/share/zoneinfo.
	_ "time/tzdata"
)

const (
	envUpgradeRunnerImage       = "UPGRADE_RUNNER_IMAGE"
	envUpgradeRunnerSA          = "UPGRADE_RUNNER_SA"
	envAutoUpgrade              = "AUTO_UPGRADE"
	envAutoUpgradeWindow        = "AUTO_UPGRADE_WINDOW"
	envAutoUpgradeMinReleaseAge = "AUTO_UPGRADE_MIN_RELEASE_AGE"

	defaultUpgradeRunnerImage       = "gcr.io/google.com/cloudsdktool/google-cloud-cli:alpine"
	defaultUpgradeRunnerSA          = "kube-agents-upgrader"
	defaultAutoUpgradeMinReleaseAge = 72 * time.Hour
	upgradeRunnerPodName            = "kube-agents-upgrade-runner"
	autoUpgradeAnnotation           = "kubeagents.x-k8s.io/auto-upgrade"
	triggeredByAnnotation           = "kubeagents.x-k8s.io/triggered-by"
	upgradeTimeout                  = 15 * time.Second
	autoUpgradeCheckInterval        = 15 * time.Minute
	maxUpgradeLogBytes              = 64 << 10

	// kanbanBoardPath is the Hermes kanban board read (columns of cards by
	// status) the automatic upgrade checks for running cards before it
	// restarts the gateway.
	kanbanBoardPath = "/api/plugins/kanban/board"
)

type upgradeStartRequest struct {
	Target string `json:"target"`
}

type upgradeAutoRequest struct {
	Enabled bool `json:"enabled"`
}

type upgradeStatusResponse struct {
	State       string `json:"state"` // "idle", "running", "succeeded", "failed"
	Target      string `json:"target,omitempty"`
	Phase       string `json:"phase,omitempty"`
	Command     string `json:"command,omitempty"`
	Logs        string `json:"logs,omitempty"`
	PodName     string `json:"pod_name,omitempty"`
	StartedAt   string `json:"started_at,omitempty"`
	FinishedAt  string `json:"finished_at,omitempty"`
	Detail      string `json:"detail,omitempty"`
	AutoUpgrade bool   `json:"auto_upgrade"`
	TriggeredBy string `json:"triggered_by,omitempty"`
}

type kubePodResource struct {
	Metadata struct {
		Name              string            `json:"name"`
		Namespace         string            `json:"namespace"`
		CreationTimestamp string            `json:"creationTimestamp"`
		Annotations       map[string]string `json:"annotations"`
	} `json:"metadata"`
	Status struct {
		Phase             string `json:"phase"`
		StartTime         string `json:"startTime"`
		ContainerStatuses []struct {
			Name  string `json:"name"`
			Ready bool   `json:"ready"`
			State struct {
				Waiting *struct {
					Reason  string `json:"reason"`
					Message string `json:"message"`
				} `json:"waiting"`
				Running *struct {
					StartedAt string `json:"startedAt"`
				} `json:"running"`
				Terminated *struct {
					ExitCode   int    `json:"exitCode"`
					Reason     string `json:"reason"`
					Message    string `json:"message"`
					StartedAt  string `json:"startedAt"`
					FinishedAt string `json:"finishedAt"`
				} `json:"terminated"`
			} `json:"state"`
		} `json:"containerStatuses"`
	} `json:"status"`
}

func (k *kubeClient) doJSON(ctx context.Context, method, path string, body any, out any) (int, error) {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, k.base+path, reader)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", bearerPrefix+k.bearer())
	req.Header.Set("Accept", contentTypeJSON)
	if body != nil {
		if method == http.MethodPatch {
			req.Header.Set("Content-Type", "application/merge-patch+json")
		} else {
			req.Header.Set("Content-Type", contentTypeJSON)
		}
	}
	resp, err := k.client.Do(req)
	if err != nil {
		return 0, err
	}
	defer drainAndClose(resp)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamErrorBytes))
		return resp.StatusCode, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if out != nil {
		if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(out); err != nil {
			return resp.StatusCode, err
		}
	}
	return resp.StatusCode, nil
}

func (k *kubeClient) getText(ctx context.Context, path string, maxBytes int64) (int, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, k.base+path, nil)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("Authorization", bearerPrefix+k.bearer())
	resp, err := k.client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer drainAndClose(resp)
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes))
	if err != nil {
		return resp.StatusCode, "", err
	}
	return resp.StatusCode, string(raw), nil
}

func (s *server) upgradeCommandString(target string) string {
	cmd := fmt.Sprintf("curl -fsSL https://raw.githubusercontent.com/%s/%s/upgrade.sh | bash -s -- --non-interactive", s.cfg.ReleaseRepo, target)
	if s.cfg.ProjectID != "" {
		cmd += fmt.Sprintf(" --gcp-project-id=%q", s.cfg.ProjectID)
	}
	if s.cfg.ClusterName != "" {
		cmd += fmt.Sprintf(" --gke-cluster-name=%q", s.cfg.ClusterName)
	}
	if s.cfg.Location != "" {
		cmd += fmt.Sprintf(" --gcp-region=%q", s.cfg.Location)
	}
	return cmd
}

func (s *server) handleUpgradeStart(w http.ResponseWriter, r *http.Request) {
	if s.kube == nil || s.cfg.PodNamespace == "" {
		writeError(w, http.StatusNotImplemented, "upgrade_unavailable", "This console has no Kubernetes API credentials to launch an upgrade container.")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req upgradeStartRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil && err != io.EOF {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), upgradeTimeout)
	defer cancel()

	target := strings.TrimPrefix(strings.TrimSpace(req.Target), "v")
	if target == "" {
		rel := s.currentRelease(ctx)
		target = strings.TrimPrefix(strings.TrimSpace(rel.Latest), "v")
	}
	if !releaseTagPattern.MatchString(target) {
		writeError(w, http.StatusBadRequest, "invalid_target", "target must be a valid semantic release version (e.g. 0.12.0).")
		return
	}

	st, code, err := s.startUpgradeRunner(ctx, target, "manual")
	if err != nil {
		writeError(w, code, "upgrade_launch_failed", "Could not start upgrade container: "+err.Error())
		return
	}
	writeJSON(w, code, st)
}

func (s *server) startUpgradeRunner(ctx context.Context, target, triggeredBy string) (upgradeStatusResponse, int, error) {
	if s.kube == nil || s.cfg.PodNamespace == "" {
		return upgradeStatusResponse{}, http.StatusNotImplemented, fmt.Errorf("Kubernetes API client is not configured")
	}
	cur := s.readUpgradeStatus(ctx)
	if cur.State == "running" {
		return cur, http.StatusAccepted, nil
	}
	ns := s.cfg.PodNamespace
	podPath := fmt.Sprintf("/api/v1/namespaces/%s/pods/%s", ns, upgradeRunnerPodName)
	if cur.State == "succeeded" || cur.State == "failed" {
		_, _ = s.kube.doJSON(ctx, http.MethodDelete, podPath+"?gracePeriodSeconds=0", nil, nil)
		// Wait briefly for the old pod object to clear.
		for i := 0; i < 15; i++ {
			var existing kubePodResource
			code, err := s.kube.doJSON(ctx, http.MethodGet, podPath, nil, &existing)
			if err != nil && code == http.StatusNotFound {
				break
			}
			time.Sleep(200 * time.Millisecond)
		}
	}

	runnerImage := envOr(envUpgradeRunnerImage, defaultUpgradeRunnerImage)
	runnerSA := envOr(envUpgradeRunnerSA, defaultUpgradeRunnerSA)
	cmdDisplay := s.upgradeCommandString(target)

	podSpec := s.buildUpgradeRunnerPod(ns, target, cmdDisplay, runnerImage, runnerSA)
	if meta, ok := podSpec["metadata"].(map[string]any); ok {
		if ann, ok := meta["annotations"].(map[string]string); ok && triggeredBy != "" {
			ann[triggeredByAnnotation] = triggeredBy
		}
	}
	var created kubePodResource
	if _, err := s.kube.doJSON(ctx, http.MethodPost, fmt.Sprintf("/api/v1/namespaces/%s/pods", ns), podSpec, &created); err != nil {
		return upgradeStatusResponse{}, http.StatusBadGateway, err
	}

	return upgradeStatusResponse{
		State:       "running",
		Target:      target,
		Phase:       "Starting upgrade container in " + ns + "…",
		Command:     cmdDisplay,
		PodName:     upgradeRunnerPodName,
		StartedAt:   created.Metadata.CreationTimestamp,
		AutoUpgrade: s.isAutoUpgradeEnabled(ctx),
		TriggeredBy: triggeredBy,
	}, http.StatusAccepted, nil
}

func (s *server) buildUpgradeRunnerPod(ns, target, cmdDisplay, image, saName string) map[string]any {
	script := `set -euo pipefail
echo "==========================================================================="
echo "🚀 Launching In-Cluster Upgrade Container for kube-agents ${TARGET_VERSION}"
echo "==========================================================================="
echo "Command: ${UPGRADE_DISPLAY_CMD}"
echo ""

if command -v apk >/dev/null 2>&1; then
  echo ">>> Installing required CLI tools (kubectl, helm, jq, python3, openssl) <<<"
  apk add --no-cache python3 openssl jq helm kubectl >/dev/null
fi

export HOME=/tmp
export KUBECONFIG=/tmp/kubeconfig
SA_DIR="/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN="$(cat "${SA_DIR}/token")"
CTX="gke_${PROJECT_ID}_${LOCATION}_${CLUSTER_NAME}"
kubectl config set-cluster "${CTX}" \
  --server="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}" \
  --certificate-authority="${SA_DIR}/ca.crt" >/dev/null
kubectl config set-credentials upgrader --token="${TOKEN}" >/dev/null
kubectl config set-context "${CTX}" --cluster="${CTX}" --user=upgrader --namespace="${POD_NAMESPACE}" >/dev/null
kubectl config use-context "${CTX}" >/dev/null

# Recover API_SERVER_KEY from the live Secret into install.env so write_tfvars_from_state succeeds.
LIVE_API_KEY="$(kubectl get secret platform-agent-secrets -n "${POD_NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)"

# Prepare install.env from cluster configuration so upgrade.sh can validate and render values.
cat > /tmp/install.env <<EOF
GCP_PROJECT_ID="${PROJECT_ID}"
GKE_CLUSTER_NAME="${CLUSTER_NAME}"
GCP_REGION="${LOCATION}"
CLUSTER_TOPOLOGY="single"
MODEL_PROVIDER="${MODEL_PROVIDER:-vertex_ai}"
MODEL_NAME="${MODEL_NAME:-gemini-3.5-flash}"
MEMORY="off"
SKIP_CERT_MANAGER="false"
API_SERVER_KEY="${LIVE_API_KEY}"
EOF
export KUBE_AGENTS_INSTALL_ENV="/tmp/install.env"

# Provide a gcloud wrapper that uses the in-cluster kubeconfig for get-credentials
# and cluster shape checks while forwarding other subcommands to real gcloud.
REAL_GCLOUD="$(command -v gcloud || true)"
mkdir -p /tmp/bin
cat > /tmp/bin/gcloud <<'GCLOUD_EOF'
#!/usr/bin/env bash
if [[ "$*" == *"--help"* ]]; then
  echo "--dns-endpoint"
  exit 0
fi
if [[ "$*" == *"container clusters get-credentials"* ]]; then
  echo "Fetching cluster endpoint and auth data (using in-cluster credentials)."
  echo "kubeconfig entry generated for ${CLUSTER_NAME}."
  exit 0
fi
if [[ "$*" == *"container clusters describe"* && "$*" == *"autopilot.enabled"* ]]; then
  echo "True"
  exit 0
fi
if [[ "$*" == *"container clusters describe"* && "$*" == *"controlPlaneEndpointsConfig"* ]]; then
  echo "true"
  exit 0
fi
if [[ "$*" == *"storage"* ]]; then
  echo "HTTPError 404: No such object" >&2
  exit 1
fi
if [[ -n "${REAL_GCLOUD_BIN:-}" && -x "${REAL_GCLOUD_BIN}" ]]; then
  exec "${REAL_GCLOUD_BIN}" "$@"
fi
exit 0
GCLOUD_EOF
chmod +x /tmp/bin/gcloud
export REAL_GCLOUD_BIN="${REAL_GCLOUD}"
export PATH="/tmp/bin:${PATH}"

echo ">>> Downloading and executing release-pinned upgrade.sh (${TARGET_VERSION}) <<<"
curl -fsSL "https://raw.githubusercontent.com/${RELEASE_REPO}/${TARGET_VERSION}/upgrade.sh" -o /tmp/upgrade.sh
chmod +x /tmp/upgrade.sh

/tmp/upgrade.sh \
  --non-interactive \
  --upgrade-mode=operator \
  --drop-undeclared-values \
  --gcp-project-id="${PROJECT_ID}" \
  --gke-cluster-name="${CLUSTER_NAME}" \
  --gcp-region="${LOCATION}"

# Ensure no stale CREDENTIAL_PROXY_IMAGE env override blocks operator tag propagation.
kubectl set env deployment/kube-agents-controller-manager -n "${POD_NAMESPACE}" CREDENTIAL_PROXY_IMAGE- 2>/dev/null || true

/tmp/upgrade.sh \
  --non-interactive \
  --upgrade-mode=harness \
  --drop-undeclared-values \
  --gcp-project-id="${PROJECT_ID}" \
  --gke-cluster-name="${CLUSTER_NAME}" \
  --gcp-region="${LOCATION}"

echo ">>> Updating Web Console release metadata to ${TARGET_VERSION} <<<"
kubectl patch deployment kube-agents-web-console -n "${POD_NAMESPACE}" --type=strategic -p "{
  \"metadata\": {\"labels\": {\"app.kubernetes.io/version\": \"${TARGET_VERSION}\"}},
  \"spec\": {\"template\": {\"spec\": {\"containers\": [{
    \"name\": \"web-console\",
    \"env\": [
      {\"name\": \"KUBE_AGENTS_IMAGE_TAG\", \"value\": \"${TARGET_VERSION}\"},
      {\"name\": \"KUBE_AGENTS_CHART_VERSION\", \"value\": \"${TARGET_VERSION}\"},
      {\"name\": \"KUBE_AGENTS_APP_VERSION\", \"value\": \"v${TARGET_VERSION}\"}
    ]
  }]}}}
}" || true

echo ">>> 🎉 Upgrade to ${TARGET_VERSION} Complete! <<<"
`
	return map[string]any{
		"apiVersion": "v1",
		"kind":       "Pod",
		"metadata": map[string]any{
			"name":      upgradeRunnerPodName,
			"namespace": ns,
			"labels": map[string]string{
				"app.kubernetes.io/name":      "kube-agents-upgrade-runner",
				"app.kubernetes.io/component": "upgrader",
				"app.kubernetes.io/part-of":   "kube-agents",
			},
			"annotations": map[string]string{
				"kubeagents.x-k8s.io/target-version":  target,
				"kubeagents.x-k8s.io/upgrade-command": cmdDisplay,
			},
		},
		"spec": map[string]any{
			"serviceAccountName":           saName,
			"automountServiceAccountToken": true,
			"restartPolicy":                "Never",
			"securityContext": map[string]any{
				"seccompProfile": map[string]string{"type": "RuntimeDefault"},
			},
			"containers": []map[string]any{
				{
					"name":            "upgrader",
					"image":           image,
					"imagePullPolicy": "Always",
					"command":         []string{"/bin/bash", "-c", script},
					"env": []map[string]string{
						{"name": "TARGET_VERSION", "value": target},
						{"name": "RELEASE_REPO", "value": s.cfg.ReleaseRepo},
						{"name": "UPGRADE_DISPLAY_CMD", "value": cmdDisplay},
						{"name": "PROJECT_ID", "value": s.cfg.ProjectID},
						{"name": "CLUSTER_NAME", "value": s.cfg.ClusterName},
						{"name": "LOCATION", "value": s.cfg.Location},
						{"name": "MODEL_PROVIDER", "value": s.cfg.ModelProvider},
						{"name": "MODEL_NAME", "value": s.cfg.ModelName},
						{"name": "POD_NAMESPACE", "value": ns},
					},
					"resources": map[string]any{
						"requests": map[string]string{
							"cpu":               "250m",
							"memory":            "512Mi",
							"ephemeral-storage": "1Gi",
						},
						"limits": map[string]string{
							"cpu":               "1000m",
							"memory":            "1Gi",
							"ephemeral-storage": "2Gi",
						},
					},
				},
			},
		},
	}
}

func (s *server) handleUpgradeStatus(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), upgradeTimeout)
	defer cancel()
	writeJSON(w, http.StatusOK, s.readUpgradeStatus(ctx))
}

func (s *server) readUpgradeStatus(ctx context.Context) upgradeStatusResponse {
	st := s.readRunnerStatus(ctx)
	if st.AutoUpgrade && st.State != "running" && st.Detail == "" {
		s.mu.Lock()
		st.Detail = s.autoUpgradeHold
		s.mu.Unlock()
	}
	return st
}

// readRunnerStatus reports the runner pod's state, phase, and log tail.
func (s *server) readRunnerStatus(ctx context.Context) upgradeStatusResponse {
	auto := s.isAutoUpgradeEnabled(ctx)
	if s.kube == nil || s.cfg.PodNamespace == "" {
		return upgradeStatusResponse{State: "idle", Detail: "Kubernetes API client is not configured.", AutoUpgrade: auto}
	}
	ns := s.cfg.PodNamespace
	podPath := fmt.Sprintf("/api/v1/namespaces/%s/pods/%s", ns, upgradeRunnerPodName)
	var pod kubePodResource
	code, err := s.kube.doJSON(ctx, http.MethodGet, podPath, nil, &pod)
	if err != nil {
		if code == http.StatusNotFound {
			return upgradeStatusResponse{State: "idle", AutoUpgrade: auto}
		}
		return upgradeStatusResponse{State: "idle", Detail: err.Error(), AutoUpgrade: auto}
	}

	target := pod.Metadata.Annotations["kubeagents.x-k8s.io/target-version"]
	cmdDisplay := pod.Metadata.Annotations["kubeagents.x-k8s.io/upgrade-command"]
	triggeredBy := pod.Metadata.Annotations[triggeredByAnnotation]
	out := upgradeStatusResponse{
		Target:      target,
		Command:     cmdDisplay,
		PodName:     pod.Metadata.Name,
		StartedAt:   pod.Status.StartTime,
		AutoUpgrade: auto,
		TriggeredBy: triggeredBy,
	}
	if out.StartedAt == "" {
		out.StartedAt = pod.Metadata.CreationTimestamp
	}

	// Read container logs if available.
	logPath := fmt.Sprintf("/api/v1/namespaces/%s/pods/%s/log?tailLines=150", ns, upgradeRunnerPodName)
	if logCode, logs, logErr := s.kube.getText(ctx, logPath, maxUpgradeLogBytes); logErr == nil && logCode == http.StatusOK {
		out.Logs = logs
		out.Phase = latestUpgradeBanner(logs)
	}

	switch pod.Status.Phase {
	case "Succeeded":
		out.State = "succeeded"
		if out.Phase == "" {
			out.Phase = fmt.Sprintf("Upgrade to %s complete!", target)
		}
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.State.Terminated != nil {
				out.FinishedAt = cs.State.Terminated.FinishedAt
			}
		}
		if target != "" {
			s.noteUpgradedVersion(target)
		}
	case "Failed":
		out.State = "failed"
		if out.Phase == "" {
			out.Phase = "Upgrade container exited with an error."
		}
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.State.Terminated != nil {
				out.FinishedAt = cs.State.Terminated.FinishedAt
				if cs.State.Terminated.Message != "" {
					out.Detail = cs.State.Terminated.Message
				}
			}
		}
	default:
		out.State = "running"
		if out.Phase == "" {
			out.Phase = "Preparing upgrade container (" + pod.Status.Phase + ")…"
			for _, cs := range pod.Status.ContainerStatuses {
				if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" {
					out.Phase = "Container waiting: " + cs.State.Waiting.Reason
				}
			}
		}
	}
	return out
}

func latestUpgradeBanner(logs string) string {
	lines := strings.Split(logs, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if strings.HasPrefix(line, ">>>") && strings.HasSuffix(line, "<<<") {
			return strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(line, ">>>"), "<<<"))
		}
	}
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if strings.HasPrefix(line, "✓") || strings.HasPrefix(line, "ℹ") || strings.HasPrefix(line, "Waiting for") || strings.HasPrefix(line, "Release ") {
			return line
		}
	}
	return ""
}

func (s *server) liveInstallVersion(ctx context.Context) (string, string) {
	s.mu.Lock()
	imageTag, chartVersion := s.cfg.ImageTag, s.cfg.ChartVersion
	s.mu.Unlock()
	if s.kube != nil && s.cfg.PodNamespace != "" {
		var pa struct {
			Spec struct {
				Deployment struct {
					Tag string `json:"tag"`
				} `json:"deployment"`
			} `json:"spec"`
		}
		if err := s.kube.get(ctx, fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName), &pa); err == nil {
			if tag := strings.TrimSpace(pa.Spec.Deployment.Tag); tag != "" {
				imageTag = tag
				if releaseTagPattern.MatchString(tag) {
					chartVersion = strings.TrimPrefix(tag, "v")
				}
			}
		}
	}
	return imageTag, chartVersion
}

func (s *server) noteUpgradedVersion(target string) {
	s.mu.Lock()
	changed := s.cfg.ImageTag != target
	s.cfg.ImageTag = target
	s.cfg.ChartVersion = target
	s.cfg.AppVersion = "v" + target
	s.mu.Unlock()
	if changed {
		s.releaseInfo.mu.Lock()
		s.releaseInfo.value = nil
		s.releaseInfo.mu.Unlock()
		s.environment.mu.Lock()
		s.environment.value = nil
		s.environment.mu.Unlock()
	}
}

func (s *server) isAutoUpgradeEnabled(ctx context.Context) bool {
	if s.kube != nil && s.cfg.PodNamespace != "" {
		var pa struct {
			Metadata struct {
				Annotations map[string]string `json:"annotations"`
			} `json:"metadata"`
		}
		if err := s.kube.get(ctx, fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName), &pa); err == nil {
			if raw, ok := pa.Metadata.Annotations[autoUpgradeAnnotation]; ok {
				switch strings.ToLower(strings.TrimSpace(raw)) {
				case "1", "true", "yes", "on":
					return true
				case "0", "false", "no", "off":
					return false
				}
			}
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.autoUpgradeSet {
		return s.autoUpgradeEnabled
	}
	return s.cfg.AutoUpgrade
}

func (s *server) setAutoUpgrade(ctx context.Context, enabled bool) error {
	s.mu.Lock()
	s.autoUpgradeSet = true
	s.autoUpgradeEnabled = enabled
	s.cfg.AutoUpgrade = enabled
	if enabled {
		s.lastAutoUpgradeTarget = ""
	} else {
		s.autoUpgradeHold = ""
	}
	s.mu.Unlock()

	if s.kube != nil && s.cfg.PodNamespace != "" {
		patch := map[string]any{
			"metadata": map[string]any{
				"annotations": map[string]string{
					autoUpgradeAnnotation: strconv.FormatBool(enabled),
				},
			},
		}
		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		if _, err := s.kube.doJSON(ctx, http.MethodPatch, path, patch, nil); err != nil {
			log.Printf(logPrefix+"could not persist %s=%t on PlatformAgent (keeping in-memory setting): %v", autoUpgradeAnnotation, enabled, err)
			return err
		}
	}
	return nil
}

func (s *server) handleUpgradeAutoGet(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), upgradeTimeout)
	defer cancel()
	writeJSON(w, http.StatusOK, map[string]any{
		"auto_upgrade": s.isAutoUpgradeEnabled(ctx),
	})
}

func (s *server) handleUpgradeAutoSet(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req upgradeAutoRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), releaseWalkTimeout+upgradeTimeout)
	defer cancel()

	_ = s.setAutoUpgrade(ctx, req.Enabled)
	if req.Enabled {
		_, st := s.maybeTriggerAutoUpgrade(ctx)
		st.AutoUpgrade = true
		writeJSON(w, http.StatusOK, st)
		return
	}
	st := s.readUpgradeStatus(ctx)
	st.AutoUpgrade = false
	writeJSON(w, http.StatusOK, st)
}

// maybeTriggerAutoUpgrade is the automatic upgrade path (triggered-by=auto):
// the 15-minute loop, GET /api/release, and switching the toggle on all go
// through it. The manual Upgrade button (POST /api/upgrade) does not.
//
// An upgrade restarts the single-replica gateway, which takes every chat
// surface offline until the new pod is ready, and upgrade.sh rewrites the
// agent's runtime files. So before it starts the runner, the automatic path
// requires all three of:
//
//   - now is inside AUTO_UPGRADE_WINDOW, when that is set (unset: any time);
//   - the newest release was published at least AUTO_UPGRADE_MIN_RELEASE_AGE
//     ago (default 72h; 0 turns the check off);
//   - no kanban card is in the running column. When the board cannot be
//     read the check fails closed and the upgrade waits.
//
// A held upgrade is retried on the next tick; the reason is reported as the
// status Detail.
//
// TODO(#3407): this policy belongs in the operator, which owns the gateway
// rollout; the console should only request an upgrade.
func (s *server) maybeTriggerAutoUpgrade(ctx context.Context) (bool, upgradeStatusResponse) {
	if !s.isAutoUpgradeEnabled(ctx) {
		s.noteAutoUpgradeHold("")
		return false, s.readUpgradeStatus(ctx)
	}
	rel := s.currentRelease(ctx)
	target := strings.TrimPrefix(strings.TrimSpace(rel.Latest), "v")
	if rel.Status != "behind" || target == "" || !releaseTagPattern.MatchString(target) {
		s.noteAutoUpgradeHold("")
		return false, s.readUpgradeStatus(ctx)
	}
	cur := s.readUpgradeStatus(ctx)
	if cur.State == "running" {
		return false, cur
	}
	s.mu.Lock()
	alreadyTried := s.lastAutoUpgradeTarget == target
	s.mu.Unlock()
	if alreadyTried && (cur.State == "failed" || cur.State == "succeeded") {
		return false, cur
	}
	if hold := s.autoUpgradeHoldReason(ctx, target, rel.LatestAt); hold != "" {
		s.noteAutoUpgradeHold(hold)
		cur.Detail = hold
		return false, cur
	}
	s.noteAutoUpgradeHold("")
	s.mu.Lock()
	s.lastAutoUpgradeTarget = target
	s.mu.Unlock()

	st, code, err := s.startUpgradeRunner(ctx, target, "auto")
	if err != nil || (code != http.StatusAccepted && code != http.StatusOK) {
		log.Printf(logPrefix+"auto-upgrade to %s failed to start: %v", target, err)
		return false, s.readUpgradeStatus(ctx)
	}
	log.Printf(logPrefix+"auto-upgrade triggered for release %s", target)
	return true, st
}

func (s *server) runAutoUpgradeLoop(ctx context.Context) {
	timer := time.NewTimer(10 * time.Second)
	defer timer.Stop()
	ticker := time.NewTicker(autoUpgradeCheckInterval)
	defer ticker.Stop()

	checkOnce := func() {
		checkCtx, cancel := context.WithTimeout(ctx, releaseWalkTimeout+upgradeTimeout)
		defer cancel()
		if !s.isAutoUpgradeEnabled(checkCtx) {
			return
		}
		// Force a fresh GitHub release check if the cached result is older than the auto-upgrade check interval.
		s.releaseInfo.mu.Lock()
		if s.releaseInfo.value != nil && time.Since(s.releaseInfo.readAt) >= autoUpgradeCheckInterval {
			s.releaseInfo.value = nil
		}
		s.releaseInfo.mu.Unlock()
		_, _ = s.maybeTriggerAutoUpgrade(checkCtx)
	}

	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			checkOnce()
		case <-ticker.C:
			checkOnce()
		}
	}
}

func (s *server) clock() time.Time {
	if s.now != nil {
		return s.now()
	}
	return time.Now()
}

// autoUpgradeHoldReason returns why an automatic upgrade to target must
// wait, or "" when it may start. The checks run cheapest first, so the
// gateway is only asked about running cards inside the window.
func (s *server) autoUpgradeHoldReason(ctx context.Context, target, publishedAt string) string {
	now := s.clock()
	if w := s.cfg.AutoUpgradeWindow; w != nil && !w.contains(now) {
		return fmt.Sprintf("Automatic upgrade to %s is waiting for the maintenance window (%s=%q).", target, envAutoUpgradeWindow, w.raw)
	}
	if minAge := s.cfg.AutoUpgradeMinReleaseAge; minAge > 0 {
		published, err := time.Parse(time.RFC3339, strings.TrimSpace(publishedAt))
		if err != nil {
			return fmt.Sprintf("Automatic upgrade to %s is waiting: GitHub did not report when the release was published, so %s=%s cannot be checked.", target, envAutoUpgradeMinReleaseAge, minAge)
		}
		if eligible := published.Add(minAge); now.Before(eligible) {
			return fmt.Sprintf("Automatic upgrade to %s is waiting until %s, %s after the release was published (%s).", target, eligible.UTC().Format(time.RFC3339), minAge, envAutoUpgradeMinReleaseAge)
		}
	}
	running, err := s.runningKanbanCards(ctx)
	if err != nil {
		return fmt.Sprintf("Automatic upgrade to %s is waiting: could not confirm the agents are idle (GET %s: %v).", target, kanbanBoardPath, err)
	}
	if running > 0 {
		return fmt.Sprintf("Automatic upgrade to %s is waiting for %d running kanban card%s to finish.", target, running, plural(running))
	}
	return ""
}

// noteAutoUpgradeHold records the current hold reason and logs it when it
// changes, so a hold that lasts many ticks is logged once.
func (s *server) noteAutoUpgradeHold(reason string) {
	s.mu.Lock()
	changed := s.autoUpgradeHold != reason
	s.autoUpgradeHold = reason
	s.mu.Unlock()
	if changed && reason != "" {
		log.Print(logPrefix + reason)
	}
}

// runningKanbanCards counts the cards in the running column of the Hermes
// kanban board. Any answer it cannot read is an error, so the caller fails
// closed.
func (s *server) runningKanbanCards(ctx context.Context) (int, error) {
	resp, err := s.hermes(ctx, http.MethodGet, kanbanBoardPath, nil)
	if err != nil {
		return 0, err
	}
	defer drainAndClose(resp)
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var board struct {
		Columns []struct {
			Name  string            `json:"name"`
			Tasks []json.RawMessage `json:"tasks"`
		} `json:"columns"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&board); err != nil {
		return 0, fmt.Errorf("unreadable board: %v", err)
	}
	if len(board.Columns) == 0 {
		return 0, fmt.Errorf("board answer has no columns")
	}
	running := 0
	for _, c := range board.Columns {
		if c.Name == "running" {
			running += len(c.Tasks)
		}
	}
	return running, nil
}

// loadAutoUpgradePolicy reads AUTO_UPGRADE_WINDOW and
// AUTO_UPGRADE_MIN_RELEASE_AGE once at startup. A window that does not
// parse is logged and treated as unset (no time restriction); a release age
// that does not parse is logged and replaced with the 72h default.
func loadAutoUpgradePolicy() (*upgradeWindow, time.Duration) {
	var window *upgradeWindow
	if raw := strings.TrimSpace(os.Getenv(envAutoUpgradeWindow)); raw != "" {
		w, err := parseUpgradeWindow(raw)
		if err != nil {
			log.Printf(logPrefix+"%s=%q is not a window (want e.g. \"Sat,Sun 02:00-06:00 America/Toronto\"): %v; automatic upgrades are not limited to a window", envAutoUpgradeWindow, raw, err)
		} else {
			window = w
		}
	}
	minAge := defaultAutoUpgradeMinReleaseAge
	if raw := strings.TrimSpace(os.Getenv(envAutoUpgradeMinReleaseAge)); raw != "" {
		d, err := time.ParseDuration(raw)
		if err != nil || d < 0 {
			log.Printf(logPrefix+"%s=%q is not a non-negative Go duration; using %s", envAutoUpgradeMinReleaseAge, raw, defaultAutoUpgradeMinReleaseAge)
		} else {
			minAge = d
		}
	}
	return window, minAge
}

// upgradeWindow is a weekly maintenance window: "[DAYS ]HH:MM-HH:MM[ ZONE]".
// DAYS is a comma list of day names or ranges (Mon, Sat,Sun, Mon-Fri); none
// means every day. ZONE is an IANA zone name; none means UTC. When the end
// is earlier than the start the window crosses midnight, and DAYS names the
// day it opens: "Sat 23:00-02:00" includes Sunday 01:30.
type upgradeWindow struct {
	raw        string
	days       [7]bool // indexed by time.Weekday
	start, end int     // minutes after midnight; end may be 24*60
	loc        *time.Location
}

var weekdayNames = map[string]time.Weekday{
	"sun": time.Sunday, "sunday": time.Sunday,
	"mon": time.Monday, "monday": time.Monday,
	"tue": time.Tuesday, "tues": time.Tuesday, "tuesday": time.Tuesday,
	"wed": time.Wednesday, "wednesday": time.Wednesday,
	"thu": time.Thursday, "thur": time.Thursday, "thurs": time.Thursday, "thursday": time.Thursday,
	"fri": time.Friday, "friday": time.Friday,
	"sat": time.Saturday, "saturday": time.Saturday,
}

func parseUpgradeWindow(raw string) (*upgradeWindow, error) {
	fields := strings.Fields(raw)
	if len(fields) == 0 || len(fields) > 3 {
		return nil, fmt.Errorf("want \"[DAYS ]HH:MM-HH:MM[ ZONE]\"")
	}
	w := &upgradeWindow{raw: strings.Join(fields, " "), loc: time.UTC}
	i := 0
	if !strings.Contains(fields[0], ":") {
		if err := w.parseDays(fields[0]); err != nil {
			return nil, err
		}
		i++
	} else {
		for d := range w.days {
			w.days[d] = true
		}
	}
	if i >= len(fields) {
		return nil, fmt.Errorf("missing HH:MM-HH:MM time range")
	}
	from, to, ok := strings.Cut(fields[i], "-")
	if !ok {
		return nil, fmt.Errorf("time range %q is not HH:MM-HH:MM", fields[i])
	}
	var err error
	if w.start, err = parseClock(from, false); err != nil {
		return nil, err
	}
	if w.end, err = parseClock(to, true); err != nil {
		return nil, err
	}
	if w.start == w.end {
		return nil, fmt.Errorf("time range %q is empty", fields[i])
	}
	i++
	if i < len(fields) {
		loc, err := time.LoadLocation(fields[i])
		if err != nil {
			return nil, fmt.Errorf("unknown time zone %q", fields[i])
		}
		w.loc = loc
		i++
	}
	if i != len(fields) {
		return nil, fmt.Errorf("unexpected %q", fields[i])
	}
	return w, nil
}

func (w *upgradeWindow) parseDays(spec string) error {
	for _, part := range strings.Split(spec, ",") {
		part = strings.ToLower(strings.TrimSpace(part))
		if part == "" {
			return fmt.Errorf("empty day in %q", spec)
		}
		from, to, isRange := strings.Cut(part, "-")
		first, ok := weekdayNames[from]
		if !ok {
			return fmt.Errorf("unknown day %q", from)
		}
		last := first
		if isRange {
			if last, ok = weekdayNames[to]; !ok {
				return fmt.Errorf("unknown day %q", to)
			}
		}
		for d := first; ; d = (d + 1) % 7 {
			w.days[d] = true
			if d == last {
				break
			}
		}
	}
	return nil
}

// parseClock reads HH:MM as minutes after midnight; 24:00 is accepted only
// as an end.
func parseClock(v string, isEnd bool) (int, error) {
	hh, mm, ok := strings.Cut(strings.TrimSpace(v), ":")
	h, herr := strconv.Atoi(hh)
	m, merr := strconv.Atoi(mm)
	if !ok || herr != nil || merr != nil || len(mm) != 2 || h < 0 || m < 0 || m > 59 {
		return 0, fmt.Errorf("time %q is not HH:MM", v)
	}
	if h == 24 && m == 0 && isEnd {
		return 24 * 60, nil
	}
	if h > 23 {
		return 0, fmt.Errorf("time %q is not HH:MM", v)
	}
	return h*60 + m, nil
}

// contains reports whether t falls inside the window.
func (w *upgradeWindow) contains(t time.Time) bool {
	lt := t.In(w.loc)
	m := lt.Hour()*60 + lt.Minute()
	day := lt.Weekday()
	if w.start < w.end {
		return w.days[day] && m >= w.start && m < w.end
	}
	// Crosses midnight: the late part belongs to the day the window opens,
	// the early part to the day before.
	if m >= w.start {
		return w.days[day]
	}
	if m < w.end {
		return w.days[(day+6)%7]
	}
	return false
}

func init() {
	_ = os.Getenv
}
