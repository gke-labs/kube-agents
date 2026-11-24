// Package teamsinbound is the Microsoft Teams inbound receiver: the one
// component of an install that accepts connections from the internet.
//
// Bot Framework delivers each Teams message as an HTTPS request to the
// address registered for the bot; it offers no queue to pull and no socket to
// hold open. This receiver is that address. It holds no credential of the
// install's own: it verifies Microsoft's signed token on every request
// (Verifier), pins the tenant, and hands the activity to the credential
// broker's existing Teams queue (POST /v1/chat/teams/events), which the
// gateway's Teams backend already reads. Everything else about Teams stays
// where it is.
package teamsinbound

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strings"
)

// Environment the operator renders for the receiver.
const (
	// EnvListen is the public listener's address (host:port). Microsoft's
	// requests arrive here through the install's own ingress.
	EnvListen = "TEAMS_INBOUND_LISTEN"
	// EnvStatusListen is the status listener's address: /healthz, /readyz and
	// /metrics, for the probes and the collector, never exposed.
	EnvStatusListen = "TEAMS_INBOUND_STATUS_LISTEN"
	// EnvAppID is the bot's Microsoft application (client) id, a GUID: the
	// audience every token must name. Public; the app password stays in
	// the broker.
	EnvAppID = "TEAMS_INBOUND_APP_ID"
	// EnvTenantID is the one Microsoft Entra tenant served, a GUID: an
	// activity from another tenant is refused before the broker sees it.
	EnvTenantID = "TEAMS_INBOUND_TENANT_ID"
	// EnvRelayURL is the credential broker's base URL.
	EnvRelayURL = "TEAMS_INBOUND_RELAY_URL"
	// EnvTokenPath is the pod's projected ServiceAccount token, read on
	// every forward because the kubelet rotates it; it is the receiver's
	// identity to the broker.
	EnvTokenPath = "TEAMS_INBOUND_TOKEN_PATH" // #nosec G101 -- an environment variable name, not a credential
	// EnvOpenIDConfigURL overrides where Microsoft's OpenID configuration is
	// read from; tests point it at a fake. The default is Bot Framework's.
	EnvOpenIDConfigURL = "TEAMS_INBOUND_OPENID_CONFIG_URL"
)

// Defaults.
const (
	// DefaultListen is the public port; 8099 is the gateway's inject door
	// and 8098 its A2A door, so this one is next.
	DefaultListen = ":8100"
	// DefaultStatusListen sits beside the gateway's metrics port (9096) and
	// the chat relay's (9097).
	DefaultStatusListen = ":9094"
	// DefaultOpenIDConfigURL is Bot Framework's published configuration; its
	// jwks_uri names the signing keys.
	DefaultOpenIDConfigURL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
)

// guidPattern is the shape of an application or tenant id. Pinning it at
// boot turns a pasted display name into a refusal with a message rather than
// a receiver that refuses every token.
var guidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// Config is the receiver's settings, read from the environment by FromEnv.
type Config struct {
	Listen          string
	StatusListen    string
	AppID           string
	TenantID        string
	RelayURL        string
	TokenPath       string
	OpenIDConfigURL string
}

// FromEnv reads the receiver's settings. lookup is os.Getenv or a test's
// map. Every required value missing or malformed is an error at boot: the
// operator renders them from a validated CR, so a bad one is a bug to see,
// not a request to serve without.
func FromEnv(lookup func(string) string) (Config, error) {
	if lookup == nil {
		lookup = os.Getenv
	}
	get := func(name string) string { return strings.TrimSpace(lookup(name)) }
	cfg := Config{
		Listen:          get(EnvListen),
		StatusListen:    get(EnvStatusListen),
		AppID:           get(EnvAppID),
		TenantID:        get(EnvTenantID),
		RelayURL:        strings.TrimSuffix(get(EnvRelayURL), "/"),
		TokenPath:       get(EnvTokenPath),
		OpenIDConfigURL: get(EnvOpenIDConfigURL),
	}
	if cfg.Listen == "" {
		cfg.Listen = DefaultListen
	}
	if cfg.StatusListen == "" {
		cfg.StatusListen = DefaultStatusListen
	}
	if cfg.OpenIDConfigURL == "" {
		cfg.OpenIDConfigURL = DefaultOpenIDConfigURL
	}
	var errs []error
	if !guidPattern.MatchString(cfg.AppID) {
		errs = append(errs, fmt.Errorf("%s must be the bot's application (client) id, a GUID", EnvAppID))
	}
	if !guidPattern.MatchString(cfg.TenantID) {
		errs = append(errs, fmt.Errorf("%s must be the tenant id, a GUID; the receiver serves one tenant", EnvTenantID))
	}
	if u, err := url.Parse(cfg.RelayURL); cfg.RelayURL == "" || err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		errs = append(errs, fmt.Errorf("%s must be the credential broker's http(s) base URL", EnvRelayURL))
	}
	if cfg.TokenPath == "" {
		errs = append(errs, fmt.Errorf("%s must name the pod's projected ServiceAccount token", EnvTokenPath))
	}
	if u, err := url.Parse(cfg.OpenIDConfigURL); err != nil || u.Scheme != "https" || u.Host == "" {
		errs = append(errs, fmt.Errorf("%s must be an https URL", EnvOpenIDConfigURL))
	}
	return cfg, errors.Join(errs...)
}
