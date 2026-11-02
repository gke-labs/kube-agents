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
	"strings"
	"sync"
	"time"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
)

const (
	cloudPlatformScope       = "https://www.googleapis.com/auth/cloud-platform"
	defaultSTSURL            = "https://sts.googleapis.com/v1/token"
	defaultIAMCredentialsURL = "https://iamcredentials.googleapis.com/v1"
	tokenHTTPTimeout         = 10 * time.Second
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

	iamURL := fmt.Sprintf("%s/projects/-/serviceAccounts/%s:generateAccessToken",
		strings.TrimRight(s.cfg.IAMCredentialsURL, "/"),
		url.PathEscape(s.cfg.WIFServiceAccount),
	)
	iamPayload, _ := json.Marshal(map[string]any{
		"scope": []string{cloudPlatformScope},
	})
	iamReq, err := http.NewRequestWithContext(ctx, http.MethodPost, iamURL, bytes.NewReader(iamPayload))
	if err != nil {
		return nil, err
	}
	iamReq.Header.Set("Authorization", "Bearer "+stsParsed.AccessToken)
	iamReq.Header.Set("Content-Type", "application/json")

	iamResp, err := s.client.Do(iamReq)
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
