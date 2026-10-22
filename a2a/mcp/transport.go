package mcp

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

const (
	defaultBusUser            = "console"
	tcpNetwork                = "tcp"
	consoleConfigFetchTimeout = 5 * time.Second
	consoleConfigMaxBodyBytes = 64 * 1024
	authModeToken             = "token"
	authModeBearer            = "bearer"
	googleOAuthTokenPrefix    = "ya29."
	jwtHeaderPrefix           = "eyJ"
)

var consoleConfigHTTPClient = &http.Client{
	Timeout: consoleConfigFetchTimeout,
}

// resolveDirectNATSURL normalizes a direct NATS / WebSocket / Console HTTP URL into a
// dialable NATS URL (nats://, tls://, ws://, wss://) and auto-populates credentials from
// <base>/config.json when BusAuthValue is unset.
func resolveDirectNATSURL(ctx context.Context, prof *DeploymentProfile) string {
	if prof == nil {
		return ""
	}
	raw := strings.TrimSpace(prof.DirectNATSURL)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" && scheme != "ws" && scheme != "wss" {
		return raw
	}

	httpScheme := "http"
	wsScheme := "ws"
	if scheme == "https" || scheme == "wss" {
		httpScheme = "https"
		wsScheme = "wss"
	}

	basePath := strings.TrimSuffix(u.Path, "/")
	basePath = strings.TrimSuffix(basePath, "/bus")
	basePath = strings.TrimSuffix(basePath, "/config.json")

	if prof.BusAuthValue == "" {
		cfgURL := *u
		cfgURL.Scheme = httpScheme
		cfgURL.Path = basePath + "/config.json"
		fetchConsoleConfig(ctx, cfgURL.String(), prof)
	}

	wsURL := *u
	wsURL.Scheme = wsScheme
	if u.Path == "" || u.Path == "/" || strings.HasSuffix(u.Path, "/config.json") {
		wsURL.Path = basePath + "/bus"
	}
	return wsURL.String()
}

func fetchConsoleConfig(ctx context.Context, cfgURL string, prof *DeploymentProfile) {
	reqCtx, cancel := context.WithTimeout(ctx, consoleConfigFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, cfgURL, nil)
	if err != nil {
		return
	}
	resp, err := consoleConfigHTTPClient.Do(req)
	if err != nil {
		return
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return
	}
	var cfg struct {
		User  string `json:"user"`
		Pass  string `json:"pass"`
		Token string `json:"token"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, consoleConfigMaxBodyBytes)).Decode(&cfg) != nil {
		return
	}
	if strings.TrimSpace(cfg.User) != "" && (prof.BusUser == "" || prof.BusUser == defaultBusUser) {
		prof.BusUser = strings.TrimSpace(cfg.User)
	}
	if strings.TrimSpace(cfg.Token) != "" {
		prof.BusAuthValue = strings.TrimSpace(cfg.Token)
		if prof.BusUser == "" {
			prof.BusUser = authModeToken
		}
	} else if strings.TrimSpace(cfg.Pass) != "" {
		prof.BusAuthValue = strings.TrimSpace(cfg.Pass)
	}
}

// refreshDeploymentCredentials re-fetches credentials when an initial NATS dial fails with
// cached credentials (e.g. after a Web Console restart).
// Returns true if BusAuthValue changed.
func refreshDeploymentCredentials(ctx context.Context, prof *DeploymentProfile) bool {
	if prof == nil {
		return false
	}
	prevAuth := prof.BusAuthValue
	if prof.DirectNATSURL != "" {
		prof.BusAuthValue = ""
		_ = resolveDirectNATSURL(ctx, prof)
		if prof.BusAuthValue == "" {
			prof.BusAuthValue = prevAuth
			return false
		}
		return prof.BusAuthValue != prevAuth
	}
	return false
}

func buildNATSAuthOption(user, authVal string) nats.Option {
	authVal = strings.TrimSpace(authVal)
	if authVal == "" {
		return nil
	}
	if strings.EqualFold(user, authModeToken) || strings.EqualFold(user, authModeBearer) || isBearerTokenValue(authVal) {
		return nats.Token(authVal)
	}
	return nats.UserInfo(user, authVal)
}

func isBearerTokenValue(v string) bool {
	trimmed := strings.TrimSpace(v)
	if strings.HasPrefix(trimmed, googleOAuthTokenPrefix) {
		return true
	}
	if strings.HasPrefix(trimmed, jwtHeaderPrefix) && strings.Count(trimmed, ".") == 2 {
		return true
	}
	return false
}
