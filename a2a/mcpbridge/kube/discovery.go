package kube

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"

	// Register client-go auth plugins (gcp, oidc, exec) for kubeconfig authentication.
	_ "k8s.io/client-go/plugin/pkg/client/auth"
)

const (
	contextProbeTimeout      = 10 * time.Second
	credsFetchTimeout        = 10 * time.Second
	k8sConditionReady        = "Ready"
	k8sStatusTrue            = "True"
	maxDiscoveryWorkers      = 8
	crdAbsPathPlatformAgents = "/apis/kubeagents.x-k8s.io/v1alpha1/platformagents"
	crdAbsPathAgentProfiles  = "/apis/kubeagents.x-k8s.io/v1alpha1/agentprofiles"
)

var errEmptyMCPToken = errors.New("empty token in Secret")

type k8sPlatformAgentList struct {
	Items []k8sPlatformAgentItem `json:"items"`
}

type k8sPlatformAgentItem struct {
	Metadata struct {
		Name      string            `json:"name"`
		Namespace string            `json:"namespace"`
		Labels    map[string]string `json:"labels"`
	} `json:"metadata"`
	Spec struct {
		Mode       string `json:"mode"`
		Deployment struct {
			Tag string `json:"tag"`
		} `json:"deployment"`
		Harness struct {
			ProjectID   string `json:"projectId"`
			ClusterName string `json:"clusterName"`
			Location    string `json:"location"`
		} `json:"harness"`
		Integration struct {
			GitHub struct {
				GitRepo string `json:"gitRepo"`
			} `json:"github"`
			GoogleChat map[string]any `json:"googleChat"`
			Slack      map[string]any `json:"slack"`
			Teams      map[string]any `json:"teams"`
		} `json:"integration"`
	} `json:"spec"`
	Status struct {
		Conditions []struct {
			Type    string `json:"type"`
			Status  string `json:"status"`
			Reason  string `json:"reason"`
			Message string `json:"message"`
		} `json:"conditions"`
		Usage struct {
			ClustersMonitored         int `json:"clustersMonitored"`
			ToolExecutionsTotal       int `json:"toolExecutionsTotal"`
			RemediationsProposedTotal int `json:"remediationsProposedTotal"`
			RemediationsMergedTotal   int `json:"remediationsMergedTotal"`
		} `json:"usage"`
	} `json:"status"`
}

type k8sAgentProfileList struct {
	Items []struct {
		Metadata struct {
			Name      string `json:"name"`
			Namespace string `json:"namespace"`
		} `json:"metadata"`
		Spec struct {
			ClusterRef *struct {
				Cluster   string `json:"cluster"`
				Location  string `json:"location"`
				ProjectID string `json:"projectId"`
			} `json:"clusterRef,omitempty"`
		} `json:"spec"`
	} `json:"items"`
}

func restConfigForKubeContext(kubeCtx string) (*rest.Config, error) {
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	overrides := &clientcmd.ConfigOverrides{CurrentContext: strings.TrimSpace(kubeCtx)}
	clientCfg := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loadingRules, overrides)
	return clientCfg.ClientConfig()
}

func listKubeContextsClientGo(trimmedFilter string) ([]string, error) {
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	rawCfg, err := loadingRules.Load()
	if err == nil && trimmedFilter == "" {
		// client-go skips a kubeconfig file that does not exist and loads an empty config
		// without an error. A full scan must not read that as "no contexts": the caller prunes
		// every saved kube profile on a clean empty scan.
		err = noKubeconfigFileError(loadingRules.GetLoadingPrecedence())
	}
	if err != nil {
		if trimmedFilter != "" {
			return []string{trimmedFilter}, nil
		}
		return nil, fmt.Errorf("loading kubeconfig contexts: %w", err)
	}
	names := make([]string, 0, len(rawCfg.Contexts))
	for name := range rawCfg.Contexts {
		names = append(names, name)
	}
	contexts := mcpbridge.MatchKubeContexts(names, trimmedFilter)
	if len(contexts) == 0 && trimmedFilter != "" {
		contexts = []string{trimmedFilter}
	}
	return contexts, nil
}

// noKubeconfigFileError reports that none of the kubeconfig files the loading rules look at
// (KUBECONFIG's list, or ~/.kube/config) exists, or nil when one does.
func noKubeconfigFileError(precedence []string) error {
	var looked []string
	for _, path := range precedence {
		if strings.TrimSpace(path) == "" {
			continue
		}
		if _, err := os.Stat(path); err == nil || !errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		looked = append(looked, path)
	}
	if len(looked) == 0 {
		return errors.New("no kubeconfig file is configured (KUBECONFIG and HOME are unset)")
	}
	return fmt.Errorf("no kubeconfig file found (looked for %s)", strings.Join(looked, ", "))
}

// DiscoverDeployments scans Kubernetes contexts (or a single filtered context) in parallel
// for PlatformAgent resources and returns discovered DeploymentProfiles. Discovery is
// metadata-only and never reads cluster Secrets.
func DiscoverDeployments(ctx context.Context, kubeContextFilter string) ([]mcpbridge.DeploymentProfile, error) {
	trimmedFilter := strings.TrimSpace(kubeContextFilter)
	contexts, err := listKubeContextsClientGo(trimmedFilter)
	if err != nil {
		return nil, err
	}
	if len(contexts) == 0 {
		return nil, nil
	}

	type result struct {
		profiles []mcpbridge.DeploymentProfile
		err      error
	}
	results := make([]result, len(contexts))
	sem := make(chan struct{}, maxDiscoveryWorkers)
	var wg sync.WaitGroup

	for i, kctx := range contexts {
		wg.Add(1)
		go func(idx int, kubeCtx string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			profiles, probeErr := probeKubeContext(ctx, kubeCtx)
			results[idx] = result{profiles: profiles, err: probeErr}
		}(i, kctx)
	}
	wg.Wait()

	var all []mcpbridge.DeploymentProfile
	var failures []mcpbridge.DeploymentProfile
	for i, r := range results {
		if r.err != nil {
			failures = append(failures, mcpbridge.ContextProbeFailure(contexts[i], r.err))
			continue
		}
		all = append(all, r.profiles...)
	}
	sort.Slice(all, func(i, j int) bool {
		if all[i].Ready != all[j].Ready {
			return all[i].Ready
		}
		if all[i].Name != all[j].Name {
			return all[i].Name < all[j].Name
		}
		return all[i].KubeContext < all[j].KubeContext
	})
	all = append(all, failures...)
	if len(failures) == len(contexts) {
		return all, mcpbridge.NewDiscoveryError(failures)
	}
	return all, nil
}

func probeKubeContext(ctx context.Context, kubeCtx string) ([]mcpbridge.DeploymentProfile, error) {
	probeCtx, cancel := context.WithTimeout(ctx, contextProbeTimeout)
	defer cancel()

	restCfg, err := restConfigForKubeContext(kubeCtx)
	if err != nil {
		return nil, fmt.Errorf("loading kubeconfig: %w", err)
	}
	restCfg.Timeout = contextProbeTimeout
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return nil, fmt.Errorf("creating client: %w", err)
	}
	out, err := clientset.Discovery().RESTClient().Get().AbsPath(crdAbsPathPlatformAgents).DoRaw(probeCtx)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("listing PlatformAgents: %s", describeAPIError(err, out))
	}
	var paList k8sPlatformAgentList
	if err := json.Unmarshal(out, &paList); err != nil {
		return nil, fmt.Errorf("decoding PlatformAgent list: %w", err)
	}
	if len(paList.Items) == 0 {
		return nil, nil
	}

	// Only a context with a PlatformAgent pays for the AgentProfile list, which gets its own
	// deadline rather than what the PlatformAgent list left.
	profCtx, profCancel := context.WithTimeout(ctx, contextProbeTimeout)
	defer profCancel()
	profOut, profErr := clientset.Discovery().RESTClient().Get().AbsPath(crdAbsPathAgentProfiles).DoRaw(profCtx)
	profilesUnknown := profErr != nil && !apierrors.IsNotFound(profErr)

	profilesByNS := make(map[string][]string)
	if profErr == nil && len(profOut) > 0 {
		var apList k8sAgentProfileList
		if json.Unmarshal(profOut, &apList) != nil {
			profilesUnknown = true
		} else {
			for _, item := range apList.Items {
				if item.Spec.ClusterRef != nil {
					continue
				}
				profilesByNS[item.Metadata.Namespace] = append(profilesByNS[item.Metadata.Namespace], item.Metadata.Name)
			}
		}
	}

	aliasCounts := make(map[string]int, len(paList.Items))
	for _, item := range paList.Items {
		alias := item.Spec.Harness.ClusterName
		if alias == "" {
			alias = item.Metadata.Name
		}
		aliasCounts[alias]++
	}

	now := time.Now().UTC()
	var discovered []mcpbridge.DeploymentProfile
	for _, item := range paList.Items {
		ns := item.Metadata.Namespace
		paName := item.Metadata.Name
		alias := item.Spec.Harness.ClusterName
		if alias == "" {
			alias = paName
		}
		if aliasCounts[alias] > 1 && ns != "" {
			alias = ns + "/" + alias
		}
		ready := false
		statusMsg := ""
		for _, cond := range item.Status.Conditions {
			if cond.Type == k8sConditionReady {
				ready = (cond.Status == k8sStatusTrue)
				statusMsg = cond.Message
				break
			}
		}
		chatPlat := mcpbridge.ChatPlatformConsoleOnly
		switch {
		case len(item.Spec.Integration.GoogleChat) > 0:
			chatPlat = mcpbridge.ChatPlatformGChat
		case len(item.Spec.Integration.Slack) > 0:
			chatPlat = mcpbridge.ChatPlatformSlack
		case len(item.Spec.Integration.Teams) > 0:
			chatPlat = mcpbridge.ChatPlatformTeams
		}
		version := item.Spec.Deployment.Tag
		helmChart := ""
		if item.Metadata.Labels != nil {
			helmChart = strings.TrimSpace(item.Metadata.Labels["helm.sh/chart"])
		}

		prof := mcpbridge.DeploymentProfile{
			Name:                 alias,
			KubeContext:          kubeCtx,
			Namespace:            ns,
			PlatformAgentName:    paName,
			Mode:                 item.Spec.Mode,
			Version:              version,
			HelmChartVersion:     helmChart,
			ProjectID:            item.Spec.Harness.ProjectID,
			ClusterName:          item.Spec.Harness.ClusterName,
			Location:             item.Spec.Harness.Location,
			GitHubRepo:           item.Spec.Integration.GitHub.GitRepo,
			ChatPlatform:         chatPlat,
			MCPService:           paName + mcpbridge.MCPServiceSuffix,
			MCPPort:              mcpbridge.DefaultMCPPort,
			TokenSecretName:      paName + mcpbridge.MCPTokenSecretSuffix,
			Ready:                ready,
			StatusMessage:        statusMsg,
			AgentProfiles:        profilesByNS[ns],
			AgentProfilesUnknown: profilesUnknown,
			ClustersMonitored:    item.Status.Usage.ClustersMonitored,
			ToolExecutionsTotal:  item.Status.Usage.ToolExecutionsTotal,
			RemediationsProposed: item.Status.Usage.RemediationsProposedTotal,
			RemediationsMerged:   item.Status.Usage.RemediationsMergedTotal,
			LastDiscoveredAt:     now,
		}
		discovered = append(discovered, prof)
	}
	return discovered, nil
}

func describeAPIError(err error, body []byte) string {
	var st metav1.Status
	if len(body) > 0 && json.Unmarshal(body, &st) == nil && strings.TrimSpace(st.Message) != "" {
		return strings.TrimSpace(st.Message)
	}
	return err.Error()
}

// PopulateDeploymentCredentials reads the in-cluster a2a-mcp bearer token from the
// deployment's <agent>-a2a-mcp-token Secret (key "token") into prof.MCPToken (held in
// memory only). It does not read KUBE_AGENTS_MCP_TOKEN: the bridge applies that override,
// through mcpbridge.EnvMCPTokenFor, before it asks for the Secret.
func PopulateDeploymentCredentials(ctx context.Context, prof *mcpbridge.DeploymentProfile) error {
	if prof == nil {
		return nil
	}
	if prof.KubeContext == "" || prof.Namespace == "" {
		return nil
	}
	secretName := strings.TrimSpace(prof.TokenSecretName)
	if secretName == "" && prof.PlatformAgentName != "" {
		secretName = prof.PlatformAgentName + mcpbridge.MCPTokenSecretSuffix
	}
	if secretName == "" {
		return nil
	}
	fetchCtx, cancel := context.WithTimeout(ctx, credsFetchTimeout)
	defer cancel()

	restCfg, err := restConfigForKubeContext(prof.KubeContext)
	if err != nil {
		return fmt.Errorf("loading kubeconfig for %s: %w", prof.KubeContext, err)
	}
	restCfg.Timeout = credsFetchTimeout
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return fmt.Errorf("creating k8s client for %s: %w", prof.KubeContext, err)
	}
	secret, err := clientset.CoreV1().Secrets(prof.Namespace).Get(fetchCtx, secretName, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("fetching MCP token Secret %s/%s: %w", prof.Namespace, secretName, err)
	}
	raw, ok := secret.Data[mcpbridge.MCPTokenSecretKey]
	if !ok {
		return fmt.Errorf("MCP token Secret %s/%s missing %q key: %w", prof.Namespace, secretName, mcpbridge.MCPTokenSecretKey, errEmptyMCPToken)
	}
	token := strings.TrimSpace(string(raw))
	if token == "" {
		return fmt.Errorf("MCP token Secret %s/%s has empty %q: %w", prof.Namespace, secretName, mcpbridge.MCPTokenSecretKey, errEmptyMCPToken)
	}
	prof.MCPToken = token
	return nil
}
