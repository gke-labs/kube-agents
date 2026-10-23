package gkehttps

import (
	"bytes"
	"context"
	"encoding/base64"
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
)

var gkeSegmentPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*\z`)

// GKEClusterCoordinates holds the parsed project, location, and cluster name from a gke_ context ID.
type GKEClusterCoordinates struct {
	Project  string
	Location string
	Cluster  string
}

// ParseGKEClusterID parses and validates a "gke_<project>_<location>_<cluster>" identifier.
func ParseGKEClusterID(clusterID string) (*GKEClusterCoordinates, error) {
	if !strings.HasPrefix(clusterID, "gke_") {
		return nil, fmt.Errorf("cluster ID %q does not start with gke_", clusterID)
	}
	rest := strings.TrimPrefix(clusterID, "gke_")
	parts := strings.Split(rest, "_")
	if len(parts) != 3 {
		return nil, fmt.Errorf("invalid GKE cluster ID %q: expected gke_<project>_<location>_<cluster>", clusterID)
	}
	for _, part := range parts {
		if !gkeSegmentPattern.MatchString(part) {
			return nil, fmt.Errorf("invalid segment %q in GKE cluster ID %q", part, clusterID)
		}
	}
	return &GKEClusterCoordinates{
		Project:  parts[0],
		Location: parts[1],
		Cluster:  parts[2],
	}, nil
}

// ResolvedEndpoint describes the control-plane URL and root CA bundle for a cluster.
type ResolvedEndpoint struct {
	ServerURL string
	CACertPEM []byte
}

// TokenProvider supplies bearer access tokens for upstream cluster and Google Cloud API requests.
type TokenProvider interface {
	AccessToken(ctx context.Context) (string, error)
}

// GCPTokenConfig configures GCPTokenSource.
type GCPTokenConfig struct {
	WIFAudience       string
	WIFServiceAccount string
	WIFTokenFile      string
	MetadataURL       string
	STSURL            string
	IAMCredentialsURL string
	HTTPClient        *http.Client
}

// GCPTokenSource mints and caches Google Cloud access tokens from either Workload Identity Federation
// or the Compute Engine metadata server.
type GCPTokenSource struct {
	cfg    GCPTokenConfig
	client *http.Client

	mu        sync.Mutex
	token     string
	expiresAt time.Time
}

// NewGCPTokenSource constructs a GCPTokenSource.
func NewGCPTokenSource(cfg GCPTokenConfig) *GCPTokenSource {
	if cfg.MetadataURL == "" {
		cfg.MetadataURL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
	}
	if cfg.STSURL == "" {
		cfg.STSURL = "https://sts.googleapis.com/v1/token"
	}
	if cfg.IAMCredentialsURL == "" {
		cfg.IAMCredentialsURL = "https://iamcredentials.googleapis.com/v1"
	}
	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	return &GCPTokenSource{
		cfg:    cfg,
		client: client,
	}
}

// AccessToken returns a valid Google Cloud OAuth2 access token.
func (s *GCPTokenSource) AccessToken(ctx context.Context) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now()
	if s.token != "" && now.Add(60*time.Second).Before(s.expiresAt) {
		return s.token, nil
	}

	var (
		tok string
		exp time.Time
		err error
	)
	if s.cfg.WIFAudience != "" && s.cfg.WIFServiceAccount != "" && s.cfg.WIFTokenFile != "" {
		tok, exp, err = s.mintWIFAccessToken(ctx)
	} else {
		tok, exp, err = s.fetchMetadataToken(ctx)
	}
	if err != nil {
		return "", err
	}

	s.token = tok
	s.expiresAt = exp
	return tok, nil
}

func (s *GCPTokenSource) fetchMetadataToken(ctx context.Context) (string, time.Time, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.MetadataURL, nil)
	if err != nil {
		return "", time.Time{}, err
	}
	req.Header.Set("Metadata-Flavor", "Google")

	resp, err := s.client.Do(req)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("metadata server token request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", time.Time{}, err
	}
	if resp.StatusCode != http.StatusOK {
		return "", time.Time{}, fmt.Errorf("metadata server returned HTTP %d", resp.StatusCode)
	}

	var parsed struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", time.Time{}, fmt.Errorf("failed to parse metadata token response: %w", err)
	}
	if parsed.AccessToken == "" {
		return "", time.Time{}, errors.New("metadata server returned empty access_token")
	}
	ttl := time.Duration(parsed.ExpiresIn) * time.Second
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	return parsed.AccessToken, time.Now().Add(ttl), nil
}

func (s *GCPTokenSource) mintWIFAccessToken(ctx context.Context) (string, time.Time, error) {
	jwtBytes, err := os.ReadFile(s.cfg.WIFTokenFile)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("failed to read WIF token file: %w", err)
	}
	subjectToken := strings.TrimSpace(string(jwtBytes))
	if subjectToken == "" {
		return "", time.Time{}, errors.New("WIF token file is empty")
	}

	form := url.Values{}
	form.Set("grant_type", "urn:ietf:params:oauth:grant-type:token-exchange")
	form.Set("audience", s.cfg.WIFAudience)
	form.Set("scope", "https://www.googleapis.com/auth/cloud-platform")
	form.Set("requested_token_type", "urn:ietf:params:oauth:token-type:access_token")
	form.Set("subject_token_type", "urn:ietf:params:oauth:token-type:jwt")
	form.Set("subject_token", subjectToken)

	stsReq, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.STSURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", time.Time{}, err
	}
	stsReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	stsResp, err := s.client.Do(stsReq)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("STS token exchange failed: %w", err)
	}
	defer stsResp.Body.Close()

	stsBody, err := io.ReadAll(io.LimitReader(stsResp.Body, 1<<20))
	if err != nil {
		return "", time.Time{}, err
	}
	if stsResp.StatusCode != http.StatusOK {
		return "", time.Time{}, fmt.Errorf("STS token exchange returned HTTP %d", stsResp.StatusCode)
	}

	var stsParsed struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.Unmarshal(stsBody, &stsParsed); err != nil || stsParsed.AccessToken == "" {
		return "", time.Time{}, errors.New("failed to decode STS access_token")
	}

	iamURL := fmt.Sprintf("%s/projects/-/serviceAccounts/%s:generateAccessToken",
		strings.TrimRight(s.cfg.IAMCredentialsURL, "/"),
		url.PathEscape(s.cfg.WIFServiceAccount),
	)
	iamPayload, _ := json.Marshal(map[string]any{
		"scope": []string{"https://www.googleapis.com/auth/cloud-platform"},
	})
	iamReq, err := http.NewRequestWithContext(ctx, http.MethodPost, iamURL, bytes.NewReader(iamPayload))
	if err != nil {
		return "", time.Time{}, err
	}
	iamReq.Header.Set("Authorization", "Bearer "+stsParsed.AccessToken)
	iamReq.Header.Set("Content-Type", "application/json")

	iamResp, err := s.client.Do(iamReq)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("IAM generateAccessToken failed: %w", err)
	}
	defer iamResp.Body.Close()

	iamBody, err := io.ReadAll(io.LimitReader(iamResp.Body, 1<<20))
	if err != nil {
		return "", time.Time{}, err
	}
	if iamResp.StatusCode != http.StatusOK {
		return "", time.Time{}, fmt.Errorf("IAM generateAccessToken returned HTTP %d", iamResp.StatusCode)
	}

	var iamParsed struct {
		AccessToken string `json:"accessToken"`
		ExpireTime  string `json:"expireTime"`
	}
	if err := json.Unmarshal(iamBody, &iamParsed); err != nil || iamParsed.AccessToken == "" {
		return "", time.Time{}, errors.New("failed to decode IAM accessToken")
	}

	exp, err := time.Parse(time.RFC3339, iamParsed.ExpireTime)
	if err != nil {
		exp = time.Now().Add(30 * time.Minute)
	}
	return iamParsed.AccessToken, exp, nil
}

type gkeClusterResponse struct {
	Endpoint   string `json:"endpoint"`
	MasterAuth struct {
		ClusterCACertificate string `json:"clusterCaCertificate"`
	} `json:"masterAuth"`
	PrivateClusterConfig struct {
		EnablePrivateEndpoint bool   `json:"enablePrivateEndpoint"`
		PrivateEndpoint       string `json:"privateEndpoint"`
	} `json:"privateClusterConfig"`
	ControlPlaneEndpointsConfig struct {
		DNSEndpointConfig struct {
			Endpoint             string `json:"endpoint"`
			AllowExternalTraffic bool   `json:"allowExternalTraffic"`
		} `json:"dnsEndpointConfig"`
		IPEndpointsConfig *struct {
			Enabled         bool   `json:"enabled"`
			PublicEndpoint  string `json:"publicEndpoint"`
			PrivateEndpoint string `json:"privateEndpoint"`
		} `json:"ipEndpointsConfig"`
	} `json:"controlPlaneEndpointsConfig"`
}

// DiscoverGKEEndpoint queries container.googleapis.com to resolve the control-plane URL and CA certificate for coords.
func DiscoverGKEEndpoint(ctx context.Context, client *http.Client, baseURL string, tokenProvider TokenProvider, coords *GKEClusterCoordinates) (*ResolvedEndpoint, error) {
	if baseURL == "" {
		baseURL = "https://container.googleapis.com"
	}
	token, err := tokenProvider.AccessToken(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to acquire GCP access token for GKE discovery: %w", err)
	}

	apiURL := fmt.Sprintf("%s/v1/projects/%s/locations/%s/clusters/%s",
		strings.TrimRight(baseURL, "/"),
		url.PathEscape(coords.Project),
		url.PathEscape(coords.Location),
		url.PathEscape(coords.Cluster),
	)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, apiURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("GKE cluster discovery request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GKE cluster discovery returned HTTP %d for %s/%s/%s", resp.StatusCode, coords.Project, coords.Location, coords.Cluster)
	}

	var cluster gkeClusterResponse
	if err := json.Unmarshal(body, &cluster); err != nil {
		return nil, fmt.Errorf("failed to decode GKE cluster response: %w", err)
	}

	return selectGKEEndpoint(&cluster)
}

func selectGKEEndpoint(cluster *gkeClusterResponse) (*ResolvedEndpoint, error) {
	dnsCfg := cluster.ControlPlaneEndpointsConfig.DNSEndpointConfig
	ipCfg := cluster.ControlPlaneEndpointsConfig.IPEndpointsConfig

	if dnsCfg.Endpoint != "" && dnsCfg.AllowExternalTraffic {
		return &ResolvedEndpoint{
			ServerURL: normalizeHTTPSURL(dnsCfg.Endpoint),
		}, nil
	}
	if ipCfg != nil && !ipCfg.Enabled && dnsCfg.Endpoint != "" {
		return &ResolvedEndpoint{
			ServerURL: normalizeHTTPSURL(dnsCfg.Endpoint),
		}, nil
	}

	host := strings.TrimSpace(cluster.Endpoint)
	if host == "" && ipCfg != nil {
		if ipCfg.PublicEndpoint != "" {
			host = ipCfg.PublicEndpoint
		} else if ipCfg.PrivateEndpoint != "" {
			host = ipCfg.PrivateEndpoint
		}
	}
	if host == "" && cluster.PrivateClusterConfig.PrivateEndpoint != "" {
		host = cluster.PrivateClusterConfig.PrivateEndpoint
	}
	if host == "" && dnsCfg.Endpoint != "" {
		return &ResolvedEndpoint{
			ServerURL: normalizeHTTPSURL(dnsCfg.Endpoint),
		}, nil
	}
	if host == "" {
		return nil, errors.New("GKE cluster metadata contains no usable control-plane endpoint")
	}

	var caPEM []byte
	if cluster.MasterAuth.ClusterCACertificate != "" {
		decoded, err := base64.StdEncoding.DecodeString(cluster.MasterAuth.ClusterCACertificate)
		if err != nil {
			return nil, fmt.Errorf("failed to base64-decode clusterCaCertificate: %w", err)
		}
		caPEM = decoded
	}

	return &ResolvedEndpoint{
		ServerURL: normalizeHTTPSURL(host),
		CACertPEM: caPEM,
	}, nil
}

func normalizeHTTPSURL(endpoint string) string {
	trimmed := strings.TrimSpace(endpoint)
	if strings.HasPrefix(trimmed, "https://") || strings.HasPrefix(trimmed, "http://") {
		return strings.TrimRight(trimmed, "/")
	}
	return "https://" + strings.TrimRight(trimmed, "/")
}
