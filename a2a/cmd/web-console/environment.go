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

// GET /api/environment: the install, in numbers and versions only.
//
// A feedback report needs to say what kind of environment the console ran
// in without naming the customer's resources. So this route returns
// versions (Kubernetes, kube-agents chart and images, Hermes), dates (when
// kube-agents was installed, when it last rolled out), and counts (nodes,
// namespaces, workloads, pods). It never returns the name of the customer's
// cluster, project, namespaces or workloads. The one exception is the
// install block's ownership fields: the kube-agents Helm release, the
// namespace it was installed into, and the Argo CD Application that owns it,
// which the upgrade dialog needs to print a command that works. Counts come from
// list calls whose items are discarded here, read with the console's own
// service account, which the chart grants list on those kinds and get on the
// PlatformAgent. Where a read is refused the field is left empty and the
// response says so, so an install without the RBAC still answers.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	envKubeAgentsChartVersion = "KUBE_AGENTS_CHART_VERSION"
	envKubeAgentsAppVersion   = "KUBE_AGENTS_APP_VERSION"
	envKubeAgentsImageTag     = "KUBE_AGENTS_IMAGE_TAG"
	envHelmReleaseRevision    = "HELM_RELEASE_REVISION"
	envPodNamespace           = "POD_NAMESPACE"
	envPlatformAgentName      = "PLATFORM_AGENT_NAME"
	envKubernetesHost         = "KUBERNETES_SERVICE_HOST"
	envKubernetesPort         = "KUBERNETES_SERVICE_PORT"

	defaultPlatformAgentName = "platform-agent"
	serviceAccountDir        = "/var/run/secrets/kubernetes.io/serviceaccount"

	// environmentTimeout bounds one refresh; environmentCacheTTL is how long
	// a refresh is served before the API is read again.
	environmentTimeout  = 8 * time.Second
	environmentCacheTTL = 60 * time.Second
	// countPageLimit is the page size when the API does not report
	// remainingItemCount and the list must be walked; countMaxPages bounds
	// the walk so a very large cluster yields a floor, flagged as such.
	countPageLimit = 500
	countMaxPages  = 20

	platformAgentsAPIPath = "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/%s/platformagents/%s"
	// The metadata that says who owns the install; see installManagedBy.
	helmReleaseNameAnnotation      = "meta.helm.sh/release-name"
	helmReleaseNamespaceAnnotation = "meta.helm.sh/release-namespace"
	managedByLabel                 = "app.kubernetes.io/managed-by"
	argoCDInstanceLabel            = "argocd.argoproj.io/instance"
	// Argo CD's annotation tracking stamps this instead of the instance
	// label; its value is "<app>:<group>/<kind>:<namespace>/<name>".
	argoCDTrackingAnnotation = "argocd.argoproj.io/tracking-id"
	replicaSetsAPIPath       = "/apis/apps/v1/namespaces/%s/replicasets"

	// The kube-agents Deployments whose newest ReplicaSet dates the last
	// upgrade, by how the chart and operator name them: the operator is
	// "<release>-controller-manager" (operator-deployment.yaml), the
	// gateway and credential proxy are "<agent>-gateway" and
	// "<agent>-credential-proxy" (the operator's manifests), and LiteLLM's
	// name is fixed (litellm.yaml).
	defaultHelmReleaseName          = "kube-agents"
	controllerManagerSuffix         = "-controller-manager"
	gatewayDeploymentSuffix         = "-gateway"
	credentialProxyDeploymentSuffix = "-credential-proxy"
	litellmDeploymentName           = "litellm"
)

// countedKinds are the list calls behind the workload counts. The install
// namespace's own objects are counted too; they are part of the cluster.
var countedKinds = []struct {
	key  string
	path string
}{
	{"nodes", "/api/v1/nodes"},
	{"namespaces", "/api/v1/namespaces"},
	{"pods", "/api/v1/pods"},
	{"deployments", "/apis/apps/v1/deployments"},
	{"statefulsets", "/apis/apps/v1/statefulsets"},
	{"daemonsets", "/apis/apps/v1/daemonsets"},
	{"cronjobs", "/apis/batch/v1/cronjobs"},
}

// rolloutDeployments are the kube-agents Deployments whose newest ReplicaSet
// dates the last upgrade, named from this install's PlatformAgent and Helm
// release rather than the defaults. The console and any front proxy are
// left out: a console-only redeploy is not an upgrade of the agent.
func rolloutDeployments(agentName, helmRelease string) map[string]bool {
	if agentName == "" {
		agentName = defaultPlatformAgentName
	}
	if helmRelease == "" {
		helmRelease = defaultHelmReleaseName
	}
	return map[string]bool{
		helmRelease + controllerManagerSuffix:       true,
		agentName + gatewayDeploymentSuffix:         true,
		agentName + credentialProxyDeploymentSuffix: true,
		litellmDeploymentName:                       true,
	}
}

type environmentVersions struct {
	Kubernetes      string `json:"kubernetes,omitempty"`
	KubernetesMinor string `json:"kubernetes_minor,omitempty"`
	Platform        string `json:"platform,omitempty"`
	ChartVersion    string `json:"chart_version,omitempty"`
	AppVersion      string `json:"app_version,omitempty"`
	ImageTag        string `json:"image_tag,omitempty"`
	HelmRevision    string `json:"helm_revision,omitempty"`
	Hermes          string `json:"hermes,omitempty"`
}

type environmentInstall struct {
	InstalledAt     string `json:"installed_at,omitempty"`
	LastRolloutAt   string `json:"last_rollout_at,omitempty"`
	LastReconcileAt string `json:"last_reconcile_at,omitempty"`
	Phase           string `json:"phase,omitempty"`
	Generation      int64  `json:"generation,omitempty"`
	// ManagedBy says what owns the PlatformAgent, read from its labels and
	// annotations: "argocd" (an Argo CD Application label), "helm" (Helm's
	// release annotations; install.sh and the Terraform composition both
	// end in Helm), or "unknown". The upgrade dialog picks its path by it.
	ManagedBy        string `json:"managed_by,omitempty"`
	HelmRelease      string `json:"helm_release,omitempty"`
	ReleaseNamespace string `json:"release_namespace,omitempty"`
	ArgoCDApp        string `json:"argocd_app,omitempty"`
}

type environmentResponse struct {
	Versions environmentVersions `json:"versions"`
	Install  environmentInstall  `json:"install"`
	// Counts holds nodes, namespaces, pods, deployments, statefulsets,
	// daemonsets and cronjobs. A missing key was not readable.
	Counts map[string]int64 `json:"counts"`
	// Floor names counts that hit countMaxPages and are a lower bound.
	Floor []string `json:"floor,omitempty"`
	// Unavailable names what could not be read and why, without secrets.
	Unavailable map[string]string `json:"unavailable,omitempty"`
	ReadAt      string            `json:"read_at"`
}

// installManagedBy reads who owns the install off the PlatformAgent's
// metadata. Argo CD wins over Helm: an Argo-managed install also carries
// Helm's labels when the Application renders the chart, and the upgrade
// path is the Application, not a helm command.
func installManagedBy(labels, annotations map[string]string) (managedBy, helmRelease, releaseNamespace, argoApp string) {
	helmRelease = annotations[helmReleaseNameAnnotation]
	releaseNamespace = annotations[helmReleaseNamespaceAnnotation]
	if app := labels[argoCDInstanceLabel]; app != "" {
		return "argocd", helmRelease, releaseNamespace, app
	}
	if app, _, _ := strings.Cut(annotations[argoCDTrackingAnnotation], ":"); app != "" {
		return "argocd", helmRelease, releaseNamespace, app
	}
	if helmRelease != "" || strings.EqualFold(labels[managedByLabel], "Helm") {
		return "helm", helmRelease, releaseNamespace, ""
	}
	return "unknown", "", "", ""
}

// kubeClient reads the API from inside the pod. Tests replace it.
type kubeClient struct {
	base  string
	token string
	// tokenPath, when set, is re-read on every request: the kubelet rotates
	// the projected token on disk before it expires.
	tokenPath string
	client    *http.Client
}

// bearer returns the current service-account token, falling back to the one
// read at startup when the file cannot be read.
func (k *kubeClient) bearer() string {
	if k.tokenPath != "" {
		if b, err := os.ReadFile(k.tokenPath); err == nil {
			if tok := strings.TrimSpace(string(b)); tok != "" {
				return tok
			}
		}
	}
	return k.token
}

type environmentCache struct {
	refresh sync.Mutex
	mu      sync.Mutex
	value   *environmentResponse
	readAt  time.Time
}

// loadKubeClient builds the in-cluster client, or nil when the pod has no
// service account token mounted (the chart's default before this route).
func loadKubeClient() *kubeClient {
	host, port := os.Getenv(envKubernetesHost), os.Getenv(envKubernetesPort)
	if host == "" || port == "" {
		return nil
	}
	tokenPath := serviceAccountDir + "/token"
	token, err := os.ReadFile(tokenPath)
	if err != nil {
		return nil
	}
	client := &http.Client{}
	if ca, err := os.ReadFile(serviceAccountDir + "/ca.crt"); err == nil {
		client.Transport = transportWithCA(ca)
	}
	return &kubeClient{base: "https://" + host + ":" + port, token: strings.TrimSpace(string(token)), tokenPath: tokenPath, client: client}
}

func (k *kubeClient) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, k.base+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", bearerPrefix+k.bearer())
	req.Header.Set("Accept", contentTypeJSON)
	resp, err := k.client.Do(req)
	if err != nil {
		return err
	}
	defer drainAndClose(resp)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(out)
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

func (s *server) liveInstallVersion(ctx context.Context) (string, string) {
	// The live image tag comes from the PlatformAgent when present; the
	// chart version remains what the chart stamped at install time. When a
	// PlatformAgent read fails, fall back to the last live tag observed so
	// a transient API error does not re-key the release cache to the
	// startup env tag.
	imageTag, chartVersion := s.cfg.ImageTag, s.cfg.ChartVersion
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
				s.liveTagMu.Lock()
				s.lastLiveImageTag = tag
				s.liveTagMu.Unlock()
			}
		} else {
			s.liveTagMu.Lock()
			if s.lastLiveImageTag != "" {
				imageTag = s.lastLiveImageTag
			}
			s.liveTagMu.Unlock()
		}
	}
	return imageTag, chartVersion
}

// count returns how many objects a list has, reading as little as it can:
// one item plus remainingItemCount when the server reports it, otherwise
// pages of countPageLimit up to countMaxPages. floor is true when the walk
// stopped at the cap.
func (k *kubeClient) count(ctx context.Context, path string) (n int64, floor bool, err error) {
	type listMeta struct {
		Metadata struct {
			Continue           string `json:"continue"`
			RemainingItemCount *int64 `json:"remainingItemCount"`
		} `json:"metadata"`
		Items []json.RawMessage `json:"items"`
	}
	var first listMeta
	if err := k.get(ctx, path+"?limit=1", &first); err != nil {
		return 0, false, err
	}
	if first.Metadata.RemainingItemCount != nil {
		return int64(len(first.Items)) + *first.Metadata.RemainingItemCount, false, nil
	}
	if first.Metadata.Continue == "" {
		return int64(len(first.Items)), false, nil
	}
	n = 0
	cont := ""
	for page := 0; page < countMaxPages; page++ {
		var l listMeta
		q := fmt.Sprintf("?limit=%d", countPageLimit)
		if cont != "" {
			q += "&continue=" + cont
		}
		if err := k.get(ctx, path+q, &l); err != nil {
			return n, true, err
		}
		n += int64(len(l.Items))
		if l.Metadata.Continue == "" {
			return n, false, nil
		}
		cont = l.Metadata.Continue
	}
	return n, true, nil
}

func (s *server) handleEnvironment(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), environmentTimeout)
	defer cancel()
	writeJSON(w, http.StatusOK, s.currentEnvironment(ctx))
}

func (s *server) currentEnvironment(ctx context.Context) environmentResponse {
	s.environment.mu.Lock()
	if s.environment.value != nil && time.Since(s.environment.readAt) < environmentCacheTTL {
		v := *s.environment.value
		s.environment.mu.Unlock()
		return v
	}
	s.environment.mu.Unlock()
	s.environment.refresh.Lock()
	defer s.environment.refresh.Unlock()
	s.environment.mu.Lock()
	if s.environment.value != nil && time.Since(s.environment.readAt) < environmentCacheTTL {
		v := *s.environment.value
		s.environment.mu.Unlock()
		return v
	}
	s.environment.mu.Unlock()
	v := s.readEnvironment(ctx)
	if ctx.Err() != nil {
		// The caller left or the refresh ran out of time: the reads that
		// failed say nothing about the install, so the answer is not kept.
		return v
	}
	s.environment.mu.Lock()
	s.environment.value = &v
	s.environment.readAt = time.Now()
	s.environment.mu.Unlock()
	return v
}

func (s *server) readEnvironment(ctx context.Context) environmentResponse {
	out := environmentResponse{
		Versions: environmentVersions{
			ChartVersion: s.cfg.ChartVersion,
			AppVersion:   s.cfg.AppVersion,
			ImageTag:     s.cfg.ImageTag,
			HelmRevision: s.cfg.HelmRevision,
		},
		Counts:      map[string]int64{},
		Unavailable: map[string]string{},
		ReadAt:      time.Now().UTC().Format(time.RFC3339),
	}
	// Hermes' own version, from the health route the console already trusts.
	var health struct {
		Version string `json:"version"`
	}
	if resp, err := s.hermes(ctx, http.MethodGet, "/health/detailed", nil); err == nil {
		if resp.StatusCode == http.StatusOK {
			_ = json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&health)
		}
		drainAndClose(resp)
	}
	out.Versions.Hermes = health.Version

	k := s.kube
	if k == nil {
		out.Unavailable["kubernetes"] = "no service account token is mounted; the chart grants one when webConsole.environment.enabled is true"
		return out
	}
	var ver struct {
		GitVersion string `json:"gitVersion"`
		Major      string `json:"major"`
		Minor      string `json:"minor"`
		Platform   string `json:"platform"`
	}
	if err := k.get(ctx, "/version", &ver); err != nil {
		out.Unavailable["kubernetes"] = "version: " + err.Error()
	} else {
		out.Versions.Kubernetes = ver.GitVersion
		out.Versions.KubernetesMinor = strings.TrimRight(ver.Major, "+") + "." + strings.TrimRight(ver.Minor, "+")
		out.Versions.Platform = ver.Platform
	}
	for _, kind := range countedKinds {
		n, floor, err := k.count(ctx, kind.path)
		if err != nil {
			// A walk that failed part-way is not a floor; report the
			// failure rather than a partial count.
			out.Unavailable[kind.key] = err.Error()
			continue
		}
		out.Counts[kind.key] = n
		if floor {
			out.Floor = append(out.Floor, kind.key)
		}
	}
	ns := s.cfg.PodNamespace
	if ns == "" {
		out.Unavailable["install"] = envPodNamespace + " is not set"
		return out
	}
	var pa struct {
		Metadata struct {
			CreationTimestamp string            `json:"creationTimestamp"`
			Generation        int64             `json:"generation"`
			Labels            map[string]string `json:"labels"`
			Annotations       map[string]string `json:"annotations"`
		} `json:"metadata"`
		Spec struct {
			Deployment struct {
				Tag string `json:"tag"`
			} `json:"deployment"`
		} `json:"spec"`
		Status struct {
			Phase             string `json:"phase"`
			LastReconcileTime string `json:"lastReconcileTime"`
		} `json:"status"`
	}
	if err := k.get(ctx, fmt.Sprintf(platformAgentsAPIPath, ns, s.cfg.PlatformAgentName), &pa); err != nil {
		out.Unavailable["install"] = "platformagent: " + err.Error()
	} else {
		out.Install.InstalledAt = pa.Metadata.CreationTimestamp
		out.Install.Generation = pa.Metadata.Generation
		out.Install.Phase = pa.Status.Phase
		out.Install.LastReconcileAt = pa.Status.LastReconcileTime
		out.Install.ManagedBy, out.Install.HelmRelease, out.Install.ReleaseNamespace, out.Install.ArgoCDApp = installManagedBy(pa.Metadata.Labels, pa.Metadata.Annotations)
		if tag := strings.TrimSpace(pa.Spec.Deployment.Tag); tag != "" {
			out.Versions.ImageTag = tag
		}
	}
	var rs struct {
		Items []struct {
			Metadata struct {
				CreationTimestamp string `json:"creationTimestamp"`
				OwnerReferences   []struct {
					Kind string `json:"kind"`
					Name string `json:"name"`
				} `json:"ownerReferences"`
			} `json:"metadata"`
		} `json:"items"`
	}
	if err := k.get(ctx, fmt.Sprintf(replicaSetsAPIPath, ns), &rs); err != nil {
		out.Unavailable["last_rollout"] = "replicasets: " + err.Error()
	} else {
		var stamps []string
		rollouts := rolloutDeployments(s.cfg.PlatformAgentName, out.Install.HelmRelease)
		for _, item := range rs.Items {
			for _, o := range item.Metadata.OwnerReferences {
				if o.Kind == "Deployment" && rollouts[o.Name] {
					stamps = append(stamps, item.Metadata.CreationTimestamp)
				}
			}
		}
		sort.Strings(stamps)
		if len(stamps) > 0 {
			out.Install.LastRolloutAt = stamps[len(stamps)-1]
		}
	}
	if len(out.Unavailable) == 0 {
		out.Unavailable = nil
	}
	return out
}
