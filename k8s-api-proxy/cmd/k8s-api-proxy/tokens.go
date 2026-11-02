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
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/upstream"
)

const (
	cloudPlatformScope = "https://www.googleapis.com/auth/cloud-platform"
	defaultSTSURL      = "https://sts.googleapis.com/v1/token"
	// #nosec G101 -- public IAM Credentials API URL, not a credential.
	defaultIAMCredentialsURL = "https://iamcredentials.googleapis.com/v1"
	tokenHTTPTimeout         = 10 * time.Second

	// #nosec G101 -- environment variable and ConfigMap path names, not credentials.
	scopedPoolFlagEnv          = "CREDENTIAL_PROXY_SCOPED_SA_POOL"
	scopedPoolFileEnv          = "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE"
	defaultScopedPoolFile      = "/etc/credential-proxy/scoped-sa-pool.json"
	scopedPoolVersion          = 2
	maxScopedPoolBytes         = 1 << 20
	maxNameComponentLength     = 63
	defaultScopedTokenLifetime = "900s"
)

var (
	scopedComponentRE      = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*\z`)
	scopedServiceAccountRE = regexp.MustCompile(`^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]{6,30}\.iam\.gserviceaccount\.com\z`)
)

type gcpTokenConfig struct {
	WIFAudience       string
	WIFServiceAccount string
	WIFTokenFile      string
	STSURL            string
	IAMCredentialsURL string
	HTTPClient        *http.Client
	DefaultSource     func(ctx context.Context) (oauth2.TokenSource, error)
}

// gcpTokenSource implements oauth2.TokenSource. When WIF environment variables are set, it
// exchanges the projected JWT via STS and IAMCredentials. Otherwise it resolves Application
// Default Credentials lazily on first Token() call so kind clusters (which use only
// in-cluster and have no Google credentials) start without error.
type gcpTokenSource struct {
	cfg    gcpTokenConfig
	client *http.Client

	mu  sync.Mutex
	adc oauth2.TokenSource
}

func newGCPTokenSource(cfg gcpTokenConfig) oauth2.TokenSource {
	if cfg.STSURL == "" {
		cfg.STSURL = defaultSTSURL
	}
	if cfg.IAMCredentialsURL == "" {
		cfg.IAMCredentialsURL = defaultIAMCredentialsURL
	}
	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: tokenHTTPTimeout}
	}
	if cfg.DefaultSource == nil {
		cfg.DefaultSource = func(ctx context.Context) (oauth2.TokenSource, error) {
			return google.DefaultTokenSource(ctx, cloudPlatformScope)
		}
	}
	return oauth2.ReuseTokenSource(nil, &gcpTokenSource{
		cfg:    cfg,
		client: client,
	})
}

func (s *gcpTokenSource) Token() (*oauth2.Token, error) {
	ctx, cancel := context.WithTimeout(context.Background(), tokenHTTPTimeout)
	defer cancel()

	if s.cfg.WIFAudience != "" && s.cfg.WIFServiceAccount != "" && s.cfg.WIFTokenFile != "" {
		return s.mintWIFToken(ctx)
	}

	s.mu.Lock()
	adc := s.adc
	if adc == nil {
		var err error
		adc, err = s.cfg.DefaultSource(ctx)
		if err != nil {
			s.mu.Unlock()
			return nil, fmt.Errorf("default Google credentials: %w", err)
		}
		s.adc = adc
	}
	s.mu.Unlock()
	return adc.Token()
}

func (s *gcpTokenSource) mintWIFToken(ctx context.Context) (*oauth2.Token, error) {
	jwtBytes, err := os.ReadFile(s.cfg.WIFTokenFile)
	if err != nil {
		return nil, fmt.Errorf("reading WIF token file: %w", err)
	}
	subjectToken := strings.TrimSpace(string(jwtBytes))
	if subjectToken == "" {
		return nil, errors.New("WIF token file is empty")
	}

	form := url.Values{}
	form.Set("grant_type", "urn:ietf:params:oauth:grant-type:token-exchange")
	form.Set("audience", s.cfg.WIFAudience)
	form.Set("scope", cloudPlatformScope)
	form.Set("requested_token_type", "urn:ietf:params:oauth:token-type:access_token")
	form.Set("subject_token_type", "urn:ietf:params:oauth:token-type:jwt")
	form.Set("subject_token", subjectToken)

	stsReq, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.STSURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	stsReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	stsResp, err := s.client.Do(stsReq)
	if err != nil {
		return nil, fmt.Errorf("STS token exchange failed: %w", err)
	}
	defer stsResp.Body.Close()

	stsBody, err := io.ReadAll(io.LimitReader(stsResp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if stsResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("STS token exchange returned HTTP %d", stsResp.StatusCode)
	}

	var stsParsed struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.Unmarshal(stsBody, &stsParsed); err != nil || stsParsed.AccessToken == "" {
		return nil, errors.New("failed to decode STS access_token")
	}

	return generateIAMAccessToken(ctx, s.client, s.cfg.IAMCredentialsURL, s.cfg.WIFServiceAccount, stsParsed.AccessToken, "")
}

func generateIAMAccessToken(ctx context.Context, client *http.Client, iamCredentialsURL, targetSA, bearerToken, lifetime string) (*oauth2.Token, error) {
	iamURL := fmt.Sprintf("%s/projects/-/serviceAccounts/%s:generateAccessToken",
		strings.TrimRight(iamCredentialsURL, "/"),
		url.PathEscape(targetSA),
	)
	payload := map[string]any{
		"scope": []string{cloudPlatformScope},
	}
	if lifetime != "" {
		payload["lifetime"] = lifetime
	}
	iamPayload, _ := json.Marshal(payload)
	iamReq, err := http.NewRequestWithContext(ctx, http.MethodPost, iamURL, bytes.NewReader(iamPayload))
	if err != nil {
		return nil, err
	}
	iamReq.Header.Set("Authorization", "Bearer "+bearerToken)
	iamReq.Header.Set("Content-Type", "application/json")

	iamResp, err := client.Do(iamReq)
	if err != nil {
		return nil, fmt.Errorf("IAM generateAccessToken failed: %w", err)
	}
	defer iamResp.Body.Close()

	iamBody, err := io.ReadAll(io.LimitReader(iamResp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if iamResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("IAM generateAccessToken returned HTTP %d", iamResp.StatusCode)
	}

	var iamParsed struct {
		AccessToken string `json:"accessToken"`
		ExpireTime  string `json:"expireTime"`
	}
	if err := json.Unmarshal(iamBody, &iamParsed); err != nil || iamParsed.AccessToken == "" {
		return nil, errors.New("failed to decode IAM accessToken")
	}

	exp, err := time.Parse(time.RFC3339, iamParsed.ExpireTime)
	if err != nil {
		exp = time.Now().Add(30 * time.Minute)
	}
	return &oauth2.Token{
		AccessToken: iamParsed.AccessToken,
		TokenType:   "Bearer",
		Expiry:      exp,
	}, nil
}

// scopedPoolEnabled reports whether CREDENTIAL_PROXY_SCOPED_SA_POOL arms the
// per-project service account pool, matching scoped_sa_pool.pool_enabled.
func scopedPoolEnabled(raw string) bool {
	switch strings.TrimSpace(strings.ToLower(raw)) {
	case "", "0", "false", "no", "off":
		return false
	default:
		return true
	}
}

// loadScopedSAPool reads and validates a version-2 scoped-sa-pool.json file,
// returning a map from GCP project ID to target service account email.
func loadScopedSAPool(path string) (map[string]string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("pool file is unreadable: %s (%w)", path, err)
	}
	if info.Size() > maxScopedPoolBytes {
		return nil, fmt.Errorf("pool file is implausibly large: %s", path)
	}
	// #nosec G304 -- path is the operator-mounted CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE ConfigMap path.
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("pool file is unreadable: %s (%w)", path, err)
	}
	return parseScopedSAPool(data)
}

func parseScopedSAPool(data []byte) (map[string]string, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil || raw == nil {
		return nil, errors.New("pool file must contain a JSON object")
	}
	var version int
	if vRaw, ok := raw["version"]; !ok || json.Unmarshal(vRaw, &version) != nil || version != scopedPoolVersion {
		return nil, fmt.Errorf("unsupported pool file version (expected %d)", scopedPoolVersion)
	}
	saRaw, ok := raw["serviceAccounts"]
	if !ok {
		return nil, errors.New("pool file must carry a serviceAccounts list")
	}
	var entries []map[string]any
	if err := json.Unmarshal(saRaw, &entries); err != nil || entries == nil {
		return nil, errors.New("pool file must carry a serviceAccounts list")
	}

	members := make(map[string]string, len(entries))
	for i, entry := range entries {
		if entry == nil {
			return nil, fmt.Errorf("serviceAccounts[%d] is not an object", i)
		}
		projectID, ok := entry["projectId"].(string)
		if !ok || !scopedComponentRE.MatchString(projectID) || len(projectID) > maxNameComponentLength {
			return nil, fmt.Errorf("serviceAccounts[%d]: projectId is not a valid GKE project ID", i)
		}
		email, ok := entry["serviceAccountEmail"].(string)
		if !ok || !scopedServiceAccountRE.MatchString(email) {
			return nil, fmt.Errorf("serviceAccounts[%d] has no well-formed serviceAccountEmail", i)
		}
		if _, dup := members[projectID]; dup {
			return nil, fmt.Errorf("serviceAccounts[%d] repeats %s; a project maps to one account", i, projectID)
		}
		members[projectID] = email
	}
	if len(members) == 0 {
		return nil, fmt.Errorf("pool file lists no service accounts; every scoped request would be refused. Set %s=0 to run on the ambient credential instead", scopedPoolFlagEnv)
	}
	return members, nil
}

type impersonatedTokenSource struct {
	base              oauth2.TokenSource
	targetSA          string
	iamCredentialsURL string
	client            *http.Client
}

func newImpersonatedTokenSource(base oauth2.TokenSource, targetSA, iamCredentialsURL string, client *http.Client) oauth2.TokenSource {
	if iamCredentialsURL == "" {
		iamCredentialsURL = defaultIAMCredentialsURL
	}
	if client == nil {
		client = &http.Client{Timeout: tokenHTTPTimeout}
	}
	return oauth2.ReuseTokenSource(nil, &impersonatedTokenSource{
		base:              base,
		targetSA:          targetSA,
		iamCredentialsURL: iamCredentialsURL,
		client:            client,
	})
}

func (s *impersonatedTokenSource) Token() (*oauth2.Token, error) {
	srcTok, err := s.base.Token()
	if err != nil {
		return nil, fmt.Errorf("base credential for %s: %w", s.targetSA, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), tokenHTTPTimeout)
	defer cancel()
	return generateIAMAccessToken(ctx, s.client, s.iamCredentialsURL, s.targetSA, srcTok.AccessToken, defaultScopedTokenLifetime)
}

// scopedPoolTransport routes each gke_<project>_<location>_<cluster> request through
// a per-project upstream.Transport backed by that project's scoped service account,
// and refuses any cluster whose project has no entry in the pool.
type scopedPoolTransport struct {
	projects map[string]k8sapiproxy.UpstreamTransport
}

func newScopedPoolTransport(
	ctx context.Context,
	members map[string]string,
	baseTokens oauth2.TokenSource,
	iamCredentialsURL string,
	httpClient *http.Client,
	inCluster *rest.Config,
	containerAPIURL string,
) (k8sapiproxy.UpstreamTransport, error) {
	if len(members) == 0 {
		return nil, errors.New("scoped service account pool has no members")
	}
	projects := make(map[string]k8sapiproxy.UpstreamTransport, len(members))
	for projectID, saEmail := range members {
		ts := newImpersonatedTokenSource(baseTokens, saEmail, iamCredentialsURL, httpClient)
		tr, err := upstream.New(ctx, upstream.Config{
			InCluster:       inCluster,
			TokenSource:     ts,
			ContainerAPIURL: containerAPIURL,
		})
		if err != nil {
			return nil, fmt.Errorf("scoped transport for project %s: %w", projectID, err)
		}
		projects[projectID] = tr
	}
	return &scopedPoolTransport{projects: projects}, nil
}

func (s *scopedPoolTransport) RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	restID, ok := strings.CutPrefix(clusterID, "gke_")
	parts := strings.Split(restID, "_")
	if !ok || len(parts) != 3 ||
		!scopedComponentRE.MatchString(parts[0]) || len(parts[0]) > maxNameComponentLength ||
		!scopedComponentRE.MatchString(parts[1]) || len(parts[1]) > maxNameComponentLength ||
		!scopedComponentRE.MatchString(parts[2]) || len(parts[2]) > maxNameComponentLength {
		return nil, fmt.Errorf("scoped service account pool is armed and cluster %q is not a gke_PROJECT_LOCATION_NAME cluster", clusterID)
	}
	project, location, cluster := parts[0], parts[1], parts[2]
	tr, mapped := s.projects[project]
	if !mapped {
		return nil, fmt.Errorf(
			"no scoped service account for project %s (cluster projects/%s/locations/%s/clusters/%s): refused; the proxy will not fall back to the ambient credential. Declare the project in spec.scope and apply, or exclude the cluster",
			project, project, location, cluster,
		)
	}
	return tr.RoundTripCluster(ctx, clusterID, req)
}
