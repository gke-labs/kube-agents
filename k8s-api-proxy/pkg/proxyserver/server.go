// Package proxyserver is the k8s-api-proxy server: its flags, its environment variables and
// its lifecycle. cmd/k8s-api-proxy calls Run with nothing but its version; another main
// package calls it with Options to add what the flags cannot express.
package proxyserver

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"fmt"
	"log"
	"log/slog"
	"maps"
	"net/http"
	"os"
	"os/signal"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	"go.opentelemetry.io/otel/trace"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/tokenreview"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/upstream"
)

const (
	decimalBase             = 10
	int64Bits               = 64
	metricsUnavailableAlert = "ALERT metrics listener unavailable; kube-proxy serves no /metrics until it restarts, and API proxying is unaffected"
	listenAddrEnv           = "KUBE_PROXY_LISTEN_ADDR"
	listenAddrFlag          = "listen-addr"
	defaultListenAddr       = ":8443"
	metricsPortEnv          = "KUBE_PROXY_METRICS_PORT"
	metricsPortFlag         = "metrics-port"
	tlsCertFileEnv          = "KUBE_PROXY_TLS_CERT_FILE"
	tlsCertFileFlag         = "tls-cert-file"
	defaultTLSCertFile      = "/var/run/secrets/kubeagents/kube-proxy-tls/tls.crt"
	// #nosec G101 -- environment variable and mounted file path names, not credentials.
	tlsKeyFileEnv     = "KUBE_PROXY_TLS_KEY_FILE"
	tlsKeyFileFlag    = "tls-key-file"
	defaultTLSKeyFile = "/var/run/secrets/kubeagents/kube-proxy-tls/tls.key"
	modeEnv           = "KUBE_PROXY_MODE"
	hostClusterIDEnv  = "KUBE_PROXY_HOST_CLUSTER_ID"
	// #nosec G101 -- environment variable name, not a credential.
	redactSecretsEnv  = "KUBE_PROXY_REDACT_SECRETS"
	allowUpgradesEnv  = "KUBE_PROXY_ALLOW_UPGRADE_SUBRESOURCES"
	kubeAPIServerEnv  = "CREDENTIAL_PROXY_KUBE_API_SERVER"
	defaultKubeAPISrv = "https://kubernetes.default.svc"
	kubeCAFileEnv     = "CREDENTIAL_PROXY_KUBE_CA_FILE"
	defaultKubeCAFile = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	// #nosec G101 -- environment variable and mounted ServiceAccount file path names, not credentials.
	kubeTokenFileEnv = "CREDENTIAL_PROXY_KUBE_TOKEN_FILE"
	// #nosec G101 -- standard Kubernetes ServiceAccount mount path, not a credential.
	defaultKubeTokenFile  = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	audienceEnv           = "CREDENTIAL_PROXY_AUDIENCE"
	defaultAudience       = "kubeagents-credential-proxy"
	allowedCallersEnv     = "CREDENTIAL_PROXY_ALLOWED_CALLERS"
	cacheMaxBytesEnv      = "KUBE_PROXY_CACHE_MAX_BYTES"
	cacheMaxEntryBytesEnv = "KUBE_PROXY_CACHE_MAX_ENTRY_BYTES"
	cacheResourceTTLEnv   = "KUBE_PROXY_CACHE_RESOURCE_TTL"
	cacheDiscoveryTTLEnv  = "KUBE_PROXY_CACHE_DISCOVERY_TTL"
	scopeFileEnv          = "KUBE_PROXY_SCOPE_FILE"
	holdFileEnv           = "KUBE_PROXY_HOLD_FILE"
	excludeHostClusterEnv = "KUBE_PROXY_EXCLUDE_HOST_CLUSTER_ID"
	wifServiceAccountEnv  = "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT"
	// #nosec G101 -- environment variable name, not a credential.
	wifTokenFileEnv         = "CREDENTIAL_PROXY_WIF_TOKEN_FILE"
	sessionAudienceEnv      = "CREDENTIAL_PROXY_SESSION_AUDIENCE"
	sessionCallersEnv       = "CREDENTIAL_PROXY_SESSION_CALLERS"
	maxInFlightEnv          = "KUBE_PROXY_MAX_IN_FLIGHT"
	authModeEnv             = "KUBE_PROXY_AUTH_MODE"
	authModeTokenReview     = "token-review"
	authModeNone            = "none"
	upstreamURLEnv          = "KUBE_PROXY_UPSTREAM_URL"
	upstreamURLFlag         = "upstream-url"
	denyStreamingEnv        = "KUBE_PROXY_DENY_STREAMING"
	unauthenticatedUsername = "unauthenticated"
	binaryName              = "k8s-api-proxy"
	// The names of the static upstream's file flags, of their environment variables, and of
	// their attributes in the startup record.
	upstreamTokenFileEnv    = "KUBE_PROXY_UPSTREAM_TOKEN_FILE" // #nosec G101 -- a variable's name, not a credential
	upstreamTokenFileFlag   = "upstream-token-file"            // #nosec G101 -- a flag's name, not a credential
	upstreamCAFileEnv       = "KUBE_PROXY_UPSTREAM_CA_FILE"
	upstreamCAFileFlag      = "upstream-ca-file"
	logKeyUpstreamTokenFile = "upstream_token_file" // #nosec G101 -- a log attribute's name, not a credential
	logKeyUpstreamCAFile    = "upstream_ca_file"
	// devVersion is what a build without the release's -ldflags reports.
	devVersion = "dev"

	// unauthenticatedTokenWarning is logged when --auth-mode=none is combined with
	// --upstream-token-file: the proxy then lends the file's identity to every peer.
	unauthenticatedTokenWarning = "caller authentication is disabled and an upstream token is attached: a request from any peer that can reach the listen address is forwarded with the token file's identity; bind the listener to loopback or restrict who can reach it" // #nosec G101 -- a log message, not a credential

	// defaultMaxInFlight keeps a replica under its 512Mi limit (kubeProxyMemoryLimit in
	// k8s-operator/internal/controller/kube_proxy_manifests.go): at the 500-object pages
	// kubectl reads, a forwarded list held about 9.5 MiB in flight and the proxy was
	// OOMKilled at 43 to 48 in flight.
	defaultMaxInFlight = 24

	// otelShutdownTimeout bounds the flush of buffered spans on exit.
	otelShutdownTimeout = 5 * time.Second
	// readHeaderTimeout closes a connection whose request headers have not
	// arrived in time, so a slow client cannot hold one open indefinitely.
	readHeaderTimeout = 15 * time.Second
	// serverShutdownTimeout is how long in-flight requests get to finish
	// after SIGTERM before their connections are closed. An open watch or
	// log follow never finishes on its own, so it reaches this timeout and
	// its client reconnects to another replica.
	serverShutdownTimeout = 10 * time.Second
)

type unauthenticatedAuthenticator struct{}

func (unauthenticatedAuthenticator) AuthenticateRequest(*http.Request) (*k8sapiproxy.CallerIdentity, error) {
	return &k8sapiproxy.CallerIdentity{Username: unauthenticatedUsername}, nil
}

// Options are what an embedding binary adds to the stock server. Everything else the server
// does comes from its flags and environment variables, which are the same in every binary.
type Options struct {
	// Version is what --version prints and the startup log names after the binary's name:
	// the release or commit the caller was built for. Empty reports a development build.
	Version string
	// Authorize is k8sapiproxy.Config.Authorize; nil leaves it unset, as the stock binary does.
	Authorize func(k8sapiproxy.AuthorizeAttributes) error
	// ExtraCallerHeaders is k8sapiproxy.Config.ExtraCallerHeaders; a name the handler
	// refuses stops Run with the handler's error.
	ExtraCallerHeaders []string
	// AudienceCallers adds TokenReview audiences, each with the caller usernames admitted for
	// it, to the ones the flags and the environment configure: a caller is admitted when its
	// token verifies for a key and its username is in that key's list, and is held to its pod
	// like every TokenReview caller. A key is one audience the server does not already
	// accept: its own (--audience) and the session audience are refused, so an embedder
	// cannot widen who those admit. An empty or padded key, one holding a comma, a key with
	// no callers, a blank caller, and a caller already admitted for another audience stop Run,
	// as does a non-empty map without token-review in --auth-mode. Empty leaves the server's
	// audiences as the stock binary has them.
	AudienceCallers map[string][]string
}

type proxyConfig struct {
	// embedder is not read from a flag or the environment: Run copies its Options here.
	embedder                 Options
	showVersion              bool
	listenAddr               string
	listenAddrSet            bool
	metricsPort              int
	tlsCertFile              string
	tlsKeyFile               string
	modeRaw                  string
	authMode                 string
	upstreamURL              string
	upstreamTokenFile        string
	upstreamCAFile           string
	denyStreaming            bool
	hostClusterID            string
	redactSecrets            bool
	allowUpgradeSubresources bool
	kubeAPIServer            string
	kubeCAFile               string
	kubeTokenFile            string
	audience                 string
	allowedCallers           string
	sessionAudience          string
	sessionCallers           string
	cacheMaxBytes            int64
	cacheMaxEntryBytes       int64
	cacheResourceTTL         time.Duration
	cacheDiscoveryTTL        time.Duration
	scopeFile                string
	holdFile                 string
	excludeHostCluster       string
	universeDomainRaw        string
	containerAPIURLOverride  string
	wifAudience              string
	wifServiceAccount        string
	wifTokenFile             string
	scopedSAPool             string
	scopedSAPoolFile         string
	maxInFlight              int
	jwtIssuer                string
	jwtKeySetURL             string
	jwtAudiences             string
	jwtUsernameClaim         string
	jwtUIDClaim              string
	jwtGroupsClaim           string
	jwtPartitionClaim        string
	jwtRequiredSubPrefix     string
	jwtAllowedPartitions     string
	jwtCAFile                string
	partitionByCaller        string
	partitionByCallerSet     bool
}

func registerFlags(fs *flag.FlagSet, cfg *proxyConfig) {
	cfg.listenAddrSet = strings.TrimSpace(os.Getenv(listenAddrEnv)) != ""
	fs.BoolVar(&cfg.showVersion, "version", false, "Print the release or commit this binary was built for and exit")
	fs.StringVar(&cfg.listenAddr, listenAddrFlag, envOrDefault(listenAddrEnv, defaultListenAddr), "TCP address to listen on")
	fs.IntVar(&cfg.metricsPort, metricsPortFlag, envIntOrDefault(metricsPortEnv, 0), "TCP port for the Prometheus /metrics listener (0 disables the listener)")
	fs.StringVar(&cfg.tlsCertFile, tlsCertFileFlag, envOrDefault(tlsCertFileEnv, defaultTLSCertFile), "TLS server certificate chain PEM file, re-read when it changes; give the same path as --"+tlsKeyFileFlag+" for one PEM file holding the chain and its private key (empty for plain HTTP)")
	fs.StringVar(&cfg.tlsKeyFile, tlsKeyFileFlag, envOrDefault(tlsKeyFileEnv, defaultTLSKeyFile), "TLS server private key PEM file, re-read when it changes; the --"+tlsCertFileFlag+" path when that file holds the key (empty for plain HTTP)")
	fs.StringVar(&cfg.modeRaw, "mode", envOrDefault(modeEnv, string(k8sapiproxy.ModeReadOnlyWithDryRun)), "Proxy policy mode: read-only, read-only-dry-run, or read-write")
	fs.StringVar(&cfg.authMode, authModeFlag, envOrDefault(authModeEnv, authModeTokenReview), "Caller authentication mode: none, or a comma-separated list of token-review and jwt, tried in that order")
	fs.StringVar(&cfg.jwtIssuer, jwtIssuerFlag, envOrDefault(jwtIssuerEnv, ""), "Issuer (iss) a caller's JWT must carry; required with --"+authModeFlag+"=jwt")
	fs.StringVar(&cfg.jwtKeySetURL, jwtKeySetURLFlag, envOrDefault(jwtKeySetURLEnv, ""), "https URL of the issuer's JSON Web Key Set (when empty, discovered from <issuer>/.well-known/openid-configuration, which needs an https --"+jwtIssuerFlag+")")
	fs.StringVar(&cfg.jwtAudiences, jwtAudiencesFlag, envOrDefault(jwtAudiencesEnv, ""), "Comma-separated audiences (aud) accepted in a caller's JWT; required with --"+authModeFlag+"=jwt")
	fs.StringVar(&cfg.jwtUsernameClaim, jwtUsernameClaimFlag, envOrDefault(jwtUsernameClaimEnv, ""), "Claim path of the caller's username (default sub); dots separate nested keys, and \\. is a dot inside a key")
	fs.StringVar(&cfg.jwtUIDClaim, jwtUIDClaimFlag, envOrDefault(jwtUIDClaimEnv, ""), "Claim path of the caller's UID (default sub); when set, a token without it is refused")
	fs.StringVar(&cfg.jwtGroupsClaim, jwtGroupsClaimFlag, envOrDefault(jwtGroupsClaimEnv, ""), "Claim path of the caller's groups, a string or a list of strings (optional)")
	fs.StringVar(&cfg.jwtPartitionClaim, jwtPartitionClaimFlag, envOrDefault(jwtPartitionClaimEnv, ""), "Claim path of the caller's tenant partition; when set, a token without it is refused")
	fs.StringVar(&cfg.jwtRequiredSubPrefix, jwtRequiredSubPrefixFlag, envOrDefault(jwtRequiredSubPrefixEnv, ""), "Prefix a caller JWT's sub must start with (optional)")
	fs.StringVar(&cfg.jwtAllowedPartitions, jwtAllowedPartitionsFlag, envOrDefault(jwtAllowedPartitionsEnv, ""), "Comma-separated --"+jwtPartitionClaimFlag+" values admitted (empty admits any)")
	fs.StringVar(&cfg.jwtCAFile, jwtCAFileFlag, envOrDefault(jwtCAFileEnv, ""), "PEM bundle of the root CAs the JWT issuer's key set and metadata server is verified against, re-read when it changes (empty uses the system roots)")
	fs.StringVar(&cfg.partitionByCaller, partitionByCallerFlag, envOrDefault(partitionByCallerEnv, partitionByNone), "Share cached responses and coalesced fetches only among callers with the same: none (all callers share), partition (--"+jwtPartitionClaimFlag+"), or username")
	fs.StringVar(&cfg.upstreamURL, upstreamURLFlag, envOrDefault(upstreamURLEnv, ""), "Static http:// or https:// upstream URL (skips cloud discovery and defaults bare paths to in-cluster)")
	fs.StringVar(&cfg.upstreamTokenFile, upstreamTokenFileFlag, envOrDefault(upstreamTokenFileEnv, ""), "File holding the bearer token sent to --"+upstreamURLFlag+", which must then be https://; read again when it is rotated (empty sends no credential)")
	fs.StringVar(&cfg.upstreamCAFile, upstreamCAFileFlag, envOrDefault(upstreamCAFileEnv, ""), "PEM bundle of the root CAs the https:// --"+upstreamURLFlag+" server is verified against, re-read when it changes (empty uses the system roots)")
	fs.BoolVar(&cfg.denyStreaming, "deny-streaming", envBoolOrDefault(denyStreamingEnv, false), "Refuse watch requests, pod log follow streams, and protocol upgrades")
	fs.StringVar(&cfg.hostClusterID, "host-cluster-id", envOrDefault(hostClusterIDEnv, ""), "Optional gke_ cluster ID to route through the in-cluster ServiceAccount instead of Google credentials")
	fs.BoolVar(&cfg.redactSecrets, "redact-secrets", envBoolOrDefault(redactSecretsEnv, true), "Redact core/v1 Secret payloads and rewrite Accept headers to PartialObjectMetadata")
	fs.BoolVar(&cfg.allowUpgradeSubresources, "allow-upgrade-subresources", envBoolOrDefault(allowUpgradesEnv, false), "Allow exec, attach, portforward, and proxy subresources when mode is read-write")
	fs.StringVar(&cfg.kubeAPIServer, "kube-api-server", envOrDefault(kubeAPIServerEnv, defaultKubeAPISrv), "Host cluster Kubernetes API server URL")
	fs.StringVar(&cfg.kubeCAFile, "kube-ca-file", envOrDefault(kubeCAFileEnv, defaultKubeCAFile), "Host cluster CA bundle PEM file")
	fs.StringVar(&cfg.kubeTokenFile, "kube-token-file", envOrDefault(kubeTokenFileEnv, defaultKubeTokenFile), "Host cluster ServiceAccount token file")
	fs.StringVar(&cfg.audience, "audience", envOrDefault(audienceEnv, defaultAudience), "Required TokenReview JWT audience")
	fs.StringVar(&cfg.allowedCallers, "allowed-callers", envOrDefault(allowedCallersEnv, ""), "Comma-separated allowlist of caller ServiceAccount usernames")
	fs.Int64Var(&cfg.cacheMaxBytes, "cache-max-bytes", envInt64OrDefault(cacheMaxBytesEnv, k8sapiproxy.DefaultCacheMaxBytes), "Maximum bytes for the in-memory response cache (0 disables caching)")
	fs.Int64Var(&cfg.cacheMaxEntryBytes, "cache-max-entry-bytes", envInt64OrDefault(cacheMaxEntryBytesEnv, k8sapiproxy.DefaultCacheMaxEntryBytes), "Maximum response body bytes eligible for caching")
	fs.DurationVar(&cfg.cacheResourceTTL, "cache-resource-ttl", envDurationOrDefault(cacheResourceTTLEnv, k8sapiproxy.DefaultResourceCacheTTL), "Default TTL for cached resource get and list responses (0s coalesces in-flight requests without post-flight storage)")
	fs.StringVar(&cfg.scopeFile, "scope-file", envOrDefault(scopeFileEnv, ""), "The install's scope.json; every request to a cluster its exclude.clusters names is refused (empty excludes nothing)")
	fs.StringVar(&cfg.holdFile, "hold-file", envOrDefault(holdFileEnv, ""), "The install's hold.json (spec.hold); every request to a cluster it holds is refused, re-read on change (empty holds nothing)")
	fs.StringVar(&cfg.excludeHostCluster, "exclude-host-cluster-id", envOrDefault(excludeHostClusterEnv, ""), "gke_ ID of the cluster the proxy runs in, which in-cluster is checked against the exclusions as; routing is unaffected")
	fs.StringVar(&cfg.universeDomainRaw, "universe-domain", envOrDefault(universeDomainEnv, envOrDefault(googleCloudUniverseEnv, "")), "Google Cloud API universe domain (auto-detected from GKE metadata when unset on GKE, defaulting to googleapis.com)")
	fs.StringVar(&cfg.containerAPIURLOverride, "container-api-url", envOrDefault(containerAPIURLEnv, ""), "Optional override for the GKE Container API base URL (derived from --universe-domain when empty)")
	fs.DurationVar(&cfg.cacheDiscoveryTTL, "cache-discovery-ttl", envDurationOrDefault(cacheDiscoveryTTLEnv, k8sapiproxy.DefaultDiscoveryCacheTTL), "Default TTL for cached discovery responses (0s uses the default)")
	fs.IntVar(&cfg.maxInFlight, "max-in-flight", envIntOrDefault(maxInFlightEnv, defaultMaxInFlight), "Forwarded requests in flight before kube-proxy answers 429 with Retry-After; watches and long-running subresources do not count (0 disables the cap)")
}

func parseFlags(fs *flag.FlagSet, args []string, cfg *proxyConfig) error {
	registerFlags(fs, cfg)
	if err := fs.Parse(args); err != nil {
		return err
	}
	fs.Visit(func(f *flag.Flag) {
		if f.Name == listenAddrFlag {
			cfg.listenAddrSet = true
		}
		if f.Name == partitionByCallerFlag {
			cfg.partitionByCallerSet = true
		}
	})
	return nil
}

func loadEnvOnlyConfig(cfg *proxyConfig) {
	cfg.sessionAudience = os.Getenv(sessionAudienceEnv)
	cfg.sessionCallers = os.Getenv(sessionCallersEnv)
	cfg.wifAudience = os.Getenv(wifAudienceEnv)
	cfg.wifServiceAccount = os.Getenv(wifServiceAccountEnv)
	cfg.wifTokenFile = os.Getenv(wifTokenFileEnv)
	cfg.scopedSAPool = os.Getenv(scopedPoolFlagEnv)
	cfg.scopedSAPoolFile = envOrDefault(scopedPoolFileEnv, defaultScopedPoolFile)
}

func envOrDefault(key, fallback string) string {
	if val := strings.TrimSpace(os.Getenv(key)); val != "" {
		return val
	}
	return fallback
}

func envBoolOrDefault(key string, fallback bool) bool {
	val := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	switch val {
	case "1", "true", "yes":
		return true
	case "0", "false", "no":
		return false
	default:
		return fallback
	}
}

func envIntOrDefault(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		log.Printf("ALERT %s=%q is not an integer; using %d", key, raw, fallback) // #nosec G706 -- %q escapes newlines and control characters, so the value cannot forge a log line
		return fallback
	}
	return v
}

func envInt64OrDefault(key string, fallback int64) int64 {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	v, err := strconv.ParseInt(raw, decimalBase, int64Bits)
	if err != nil {
		log.Printf("warning: %s is not a base-10 integer; using default %d", key, fallback)
		return fallback
	}
	return v
}

func envDurationOrDefault(key string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	v, err := time.ParseDuration(raw)
	if err != nil {
		log.Printf("warning: %s is not a valid duration; using default %s", key, fallback)
		return fallback
	}
	return v
}

func startMetricsListener(ctx context.Context, port int, metrics *k8sapiproxy.Metrics, logger *slog.Logger) {
	if port == 0 {
		logger.Info("metrics listener disabled: --" + metricsPortFlag + " resolved to 0 (its default comes from " + metricsPortEnv + ")")
		return
	}
	srv, err := k8sapiproxy.NewMetricsServer(port, metrics, logger)
	if err != nil {
		logger.Error(metricsUnavailableAlert, "err", err)
		return
	}
	go func() {
		if err := srv.Run(ctx); err != nil {
			logger.Error(metricsUnavailableAlert, "err", err)
		}
	}()
}

func inClusterConfig(apiServer, caFile, tokenFile string) *rest.Config {
	return &rest.Config{
		Host:            apiServer,
		BearerTokenFile: tokenFile,
		TLSClientConfig: rest.TLSClientConfig{CAFile: caFile},
	}
}

// upstreamCAReloadError reports a changed --upstream-ca-file that could not be loaded the
// way Run reports a changed server certificate: a warning and the reload failure counter.
func upstreamCAReloadError(logger *slog.Logger, metrics *k8sapiproxy.Metrics) func(error) {
	return func(err error) {
		logger.Warn("verifying the upstream against the previous CA bundle: the changed --"+upstreamCAFileFlag+" could not be loaded", "err", err)
		metrics.RecordCertificateReloadFailure()
	}
}

func parseAllowedCallers(raw string) ([]string, error) {
	var callers []string
	for _, entry := range strings.Split(raw, ",") {
		if trimmed := strings.TrimSpace(entry); trimmed != "" {
			callers = append(callers, trimmed)
		}
	}
	if len(callers) == 0 {
		return nil, errors.New("--allowed-callers (or CREDENTIAL_PROXY_ALLOWED_CALLERS) is required and must name at least one caller ServiceAccount")
	}
	return callers, nil
}

func parseSessionAudienceCallers(sessionAud, sessionCallersRaw string) (map[string][]string, error) {
	aud := strings.TrimSpace(sessionAud)
	if aud == "" {
		return nil, nil
	}
	callers, err := parseAllowedCallers(sessionCallersRaw)
	if err != nil {
		return nil, errors.New(sessionCallersEnv + " is required when " + sessionAudienceEnv + " is set")
	}
	return map[string][]string{aud: callers}, nil
}

// mergeAudienceCallers returns configured, the audiences the environment binds to callers
// of their own, with the embedder's Options.AudienceCallers added. ownAudience is
// --audience, which is bound to --allowed-callers. An embedder's audience that is already
// configured is an error rather than a union of the two caller lists: a union would let an
// embedder widen who the server's own audience or the session audience admits. With no
// embedder audiences it returns configured itself.
func mergeAudienceCallers(configured, embedder map[string][]string, ownAudience string) (map[string][]string, error) {
	if len(embedder) == 0 {
		return configured, nil
	}
	taken := make(map[string]string, len(configured)+1)
	for _, aud := range splitList(ownAudience) {
		taken[aud] = "--audience (or " + audienceEnv + ")"
	}
	for raw := range configured {
		for _, aud := range splitList(raw) {
			taken[aud] = sessionAudienceEnv
		}
	}
	merged := make(map[string][]string, len(configured)+len(embedder))
	for aud, callers := range configured {
		merged[aud] = callers
	}
	// Sorted, so the same Options stop the binary on the same error every time.
	for _, aud := range slices.Sorted(maps.Keys(embedder)) {
		if aud == "" || aud != strings.TrimSpace(aud) || strings.Contains(aud, listSeparator) {
			return nil, fmt.Errorf("Options.AudienceCallers audience %q is not one audience name: it is empty, padded with whitespace or holds a %q", aud, listSeparator)
		}
		if owner, ok := taken[aud]; ok {
			return nil, fmt.Errorf("Options.AudienceCallers audience %q is already configured by %s; an embedder's audience must be one the server does not otherwise accept", aud, owner)
		}
		if len(embedder[aud]) == 0 {
			return nil, fmt.Errorf("Options.AudienceCallers audience %q names no caller", aud)
		}
		callers := make([]string, 0, len(embedder[aud]))
		for _, caller := range embedder[aud] {
			caller = strings.TrimSpace(caller)
			if caller == "" {
				return nil, fmt.Errorf("Options.AudienceCallers audience %q has a blank caller", aud)
			}
			callers = append(callers, caller)
		}
		merged[aud] = callers
	}
	return merged, nil
}

func buildHandler(
	ctx context.Context,
	cfg proxyConfig,
	mode k8sapiproxy.Mode,
	logger *slog.Logger,
	metrics *k8sapiproxy.Metrics,
	tp trace.TracerProvider,
) (http.Handler, error) {
	if cfg.denyStreaming && cfg.allowUpgradeSubresources {
		return nil, errors.New("--deny-streaming cannot be combined with --allow-upgrade-subresources")
	}

	trimAuthFlags(&cfg)
	modes, err := parseAuthModes(cfg.authMode)
	if err != nil {
		return nil, err
	}
	if err := validateAuthFlags(cfg, modes); err != nil {
		return nil, err
	}
	callerPartition, err := callerPartitionFunc(cfg, modes)
	if err != nil {
		return nil, err
	}

	var authenticators []k8sapiproxy.CallerAuthenticator
	var inCluster *rest.Config
	for _, authMode := range modes {
		switch authMode {
		case authModeTokenReview:
			callers, err := parseAllowedCallers(cfg.allowedCallers)
			if err != nil {
				return nil, err
			}
			inCluster = inClusterConfig(cfg.kubeAPIServer, cfg.kubeCAFile, cfg.kubeTokenFile)
			audienceCallers, err := parseSessionAudienceCallers(cfg.sessionAudience, cfg.sessionCallers)
			if err != nil {
				return nil, err
			}
			audienceCallers, err = mergeAudienceCallers(audienceCallers, cfg.embedder.AudienceCallers, cfg.audience)
			if err != nil {
				return nil, err
			}
			reviewer, err := tokenreview.NewAuthenticator(tokenreview.Config{
				Kubernetes:      inCluster,
				Audience:        cfg.audience,
				AllowedCallers:  callers,
				AudienceCallers: audienceCallers,
				Logger:          logger,
			})
			if err != nil {
				return nil, fmt.Errorf("failed to initialize TokenReview authenticator: %w", err)
			}
			authenticators = append(authenticators, namedAuthenticator{name: authModeTokenReview, next: reviewer})
		case authModeJWT:
			verifier, err := newJWTAuthenticator(cfg, logger)
			if err != nil {
				return nil, err
			}
			authenticators = append(authenticators, namedAuthenticator{name: authModeJWT, next: verifier})
		case authModeNone:
			if !cfg.listenAddrSet || strings.TrimSpace(cfg.listenAddr) == "" {
				return nil, errors.New("--" + listenAddrFlag + " (or " + listenAddrEnv + ") must be set explicitly when --auth-mode=none")
			}
			if strings.TrimSpace(cfg.allowedCallers) != "" {
				return nil, errors.New("--auth-mode=none cannot be combined with --allowed-callers (or CREDENTIAL_PROXY_ALLOWED_CALLERS)")
			}
			if strings.TrimSpace(cfg.sessionAudience) != "" || strings.TrimSpace(cfg.sessionCallers) != "" {
				return nil, errors.New("--auth-mode=none cannot be combined with " + sessionAudienceEnv + " or " + sessionCallersEnv)
			}
			if len(cfg.embedder.AudienceCallers) > 0 {
				return nil, errors.New("--auth-mode=none cannot be combined with Options.AudienceCallers")
			}
			logger.Warn(
				"caller authentication is disabled; isolate the listener at the network boundary",
				"auth_mode", authModeNone,
				"listen_addr", cfg.listenAddr,
				"tls_enabled", cfg.tlsCertFile != "" && cfg.tlsKeyFile != "",
			)
			authenticators = append(authenticators, unauthenticatedAuthenticator{})
		}
	}
	authn := newChainAuthenticator(authenticators)

	var upstreamTransport k8sapiproxy.UpstreamTransport
	// The scoped pool does not route the host's gke_ ID in-cluster, so only the default
	// transport makes it an alias the cache folds into in-cluster.
	var inClusterAlias string
	var defaultClusterID string
	staticURL := strings.TrimSpace(cfg.upstreamURL)
	upstreamTokenFile, upstreamCAFile := strings.TrimSpace(cfg.upstreamTokenFile), strings.TrimSpace(cfg.upstreamCAFile)
	if staticURL == "" && (upstreamTokenFile != "" || upstreamCAFile != "") {
		// Ignored, they would leave the operator believing a credential or a CA applied.
		return nil, errors.New("--" + upstreamTokenFileFlag + " and --" + upstreamCAFileFlag + " require --" + upstreamURLFlag)
	}
	if staticURL != "" {
		if strings.TrimSpace(cfg.hostClusterID) != "" {
			return nil, errors.New("--upstream-url cannot be combined with --host-cluster-id")
		}
		if scopedPoolEnabled(cfg.scopedSAPool) {
			return nil, errors.New("--upstream-url cannot be combined with " + scopedPoolFlagEnv)
		}
		if strings.TrimSpace(cfg.containerAPIURLOverride) != "" {
			return nil, errors.New("--upstream-url cannot be combined with --container-api-url")
		}
		if strings.TrimSpace(cfg.scopeFile) != "" || strings.TrimSpace(cfg.holdFile) != "" || strings.TrimSpace(cfg.excludeHostCluster) != "" {
			return nil, errors.New("--upstream-url cannot be combined with --scope-file, --hold-file or --exclude-host-cluster-id")
		}
		st, err := upstream.NewStatic(upstream.StaticConfig{
			URL:             staticURL,
			BearerTokenFile: upstreamTokenFile,
			CAFile:          upstreamCAFile,
			OnCAReloadError: upstreamCAReloadError(logger, metrics),
		})
		if err != nil {
			return nil, fmt.Errorf("failed to initialize static upstream transport: %w", err)
		}
		if upstreamTokenFile != "" && slices.Contains(modes, authModeNone) {
			logger.Warn(unauthenticatedTokenWarning, "auth_mode", authModeNone, "listen_addr", cfg.listenAddr, logKeyUpstreamTokenFile, upstreamTokenFile)
		}
		if upstreamTokenFile != "" || upstreamCAFile != "" {
			logger.Info("static upstream configured", logKeyUpstreamTokenFile, upstreamTokenFile, logKeyUpstreamCAFile, upstreamCAFile)
		}
		upstreamTransport = st
		defaultClusterID = k8sapiproxy.InClusterID
	} else {
		if inCluster == nil {
			inCluster = inClusterConfig(cfg.kubeAPIServer, cfg.kubeCAFile, cfg.kubeTokenFile)
		}
		probeMetadata := strings.HasPrefix(cfg.hostClusterID, gkeClusterIDPrefix) ||
			strings.HasPrefix(cfg.excludeHostCluster, gkeClusterIDPrefix) ||
			cfg.wifAudience != "" ||
			scopedPoolEnabled(cfg.scopedSAPool)
		resolvedUniverse, err := resolveKubeProxyUniverseDomain(ctx, cfg.universeDomainRaw, probeMetadata, "", nil)
		if err != nil {
			return nil, err
		}
		iamCredentialsURL := iamCredentialsURLForUniverse(resolvedUniverse)
		containerAPIURL := containerAPIURLForUniverse(resolvedUniverse, cfg.containerAPIURLOverride)

		gcpTokens := newGCPTokenSource(gcpTokenConfig{
			WIFAudience:       cfg.wifAudience,
			WIFServiceAccount: cfg.wifServiceAccount,
			WIFTokenFile:      cfg.wifTokenFile,
			STSURL:            stsURLForUniverse(resolvedUniverse),
			IAMCredentialsURL: iamCredentialsURL,
		})

		if scopedPoolEnabled(cfg.scopedSAPool) {
			members, err := loadScopedSAPool(cfg.scopedSAPoolFile)
			if err != nil {
				return nil, fmt.Errorf("failed to load scoped service account pool: %w", err)
			}
			upstreamTransport, err = newScopedPoolTransport(ctx, members, gcpTokens, iamCredentialsURL, nil, inCluster, containerAPIURL)
			if err != nil {
				return nil, fmt.Errorf("failed to initialize scoped upstream transport: %w", err)
			}
		} else {
			upstreamTransport, err = upstream.New(ctx, upstream.Config{
				InCluster:       inCluster,
				HostClusterID:   cfg.hostClusterID,
				TokenSource:     gcpTokens,
				ContainerAPIURL: containerAPIURL,
			})
			if err != nil {
				return nil, fmt.Errorf("failed to initialize upstream transport: %w", err)
			}
			inClusterAlias = cfg.hostClusterID
		}
	}

	var cacheCfg *k8sapiproxy.CacheConfig
	if cfg.cacheMaxBytes > 0 {
		cacheCfg = &k8sapiproxy.CacheConfig{
			MaxBytes:            cfg.cacheMaxBytes,
			MaxEntryBytes:       cfg.cacheMaxEntryBytes,
			DefaultResourceTTL:  cfg.cacheResourceTTL,
			DefaultDiscoveryTTL: cfg.cacheDiscoveryTTL,
			HostClusterID:       inClusterAlias,
		}
	}

	handler, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator:      authn,
		Authorize:          cfg.embedder.Authorize,
		ExtraCallerHeaders: cfg.embedder.ExtraCallerHeaders,
		Upstream:           upstreamTransport,
		Policy: k8sapiproxy.Policy{
			Mode:                     mode,
			RedactSecrets:            cfg.redactSecrets,
			AllowUpgradeSubresources: cfg.allowUpgradeSubresources,
			DenyStreaming:            cfg.denyStreaming,
		},
		DefaultClusterID: defaultClusterID,
		Cache:            cacheCfg,
		CallerPartition:  callerPartition,
		Exclusions:       k8sapiproxy.NewExclusions(cfg.scopeFile, cfg.excludeHostCluster),
		Holds:            k8sapiproxy.NewHolds(cfg.holdFile, cfg.excludeHostCluster),
		Logger:           logger,
		Metrics:          metrics,
		TracerProvider:   tp,
		MaxInFlight:      cfg.maxInFlight,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to initialize k8s-api-proxy handler: %w", err)
	}
	return handler, nil
}

// versionString is what --version prints and the startup log names for a binary built for
// version; an empty version is a build without the release's -ldflags.
func versionString(version string) string {
	if version == "" {
		version = devVersion
	}
	return binaryName + " " + version
}

// assemble is everything Run decides before it starts anything: it registers the flags on
// fs and parses args, and unless they ask for the version alone (versionOnly, with nothing
// else read) it reads the environment-only variables, attaches what opts adds and parses
// the mode. The configuration it returns is the one the handler is built from.
func assemble(fs *flag.FlagSet, args []string, opts Options) (cfg proxyConfig, mode k8sapiproxy.Mode, versionOnly bool, err error) {
	if err := parseFlags(fs, args, &cfg); err != nil {
		return proxyConfig{}, "", false, err
	}
	if cfg.showVersion {
		return cfg, "", true, nil
	}
	loadEnvOnlyConfig(&cfg)
	cfg.embedder = opts

	mode, err = k8sapiproxy.ParseMode(cfg.modeRaw)
	if err != nil {
		return proxyConfig{}, "", false, fmt.Errorf("invalid mode: %w", err)
	}
	return cfg, mode, false, nil
}

// Run is the whole of the server binary's main: it parses args (os.Args[1:]) into
// flag.CommandLine, reads the environment, builds the handler with what opts adds, and
// serves until ctx ends or the process gets SIGINT or SIGTERM, then drains in-flight
// requests. It returns nil after --version or a drained shutdown, and the error the binary
// exits on otherwise; the caller logs it and exits non-zero. A flag that does not parse
// exits the process while flag.CommandLine has its default flag.ExitOnError; under another
// error handling Run returns the parse error and starts nothing. Run registers its flags
// on flag.CommandLine, so a process calls it once, and flags the caller registered there
// first are parsed with the server's.
func Run(ctx context.Context, args []string, opts Options) error {
	cfg, mode, versionOnly, err := assemble(flag.CommandLine, args, opts)
	if err != nil {
		return err
	}
	if versionOnly {
		fmt.Println(versionString(opts.Version))
		return nil
	}

	ctx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	otelShutdown, tp, err := k8sapiproxy.SetupOTel(ctx)
	if err != nil {
		log.Printf("warning: failed to initialize OpenTelemetry tracing, continuing with no-op tracer: %v", err)
		otelShutdown = nil
	}

	// Buffered spans are flushed after a drained shutdown only: on an error the caller exits
	// at once, as the binary always has.
	if err := serve(ctx, cfg, mode, opts.Version, tp); err != nil {
		return err
	}
	if otelShutdown != nil {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), otelShutdownTimeout)
		defer cancel()
		_ = otelShutdown(shutdownCtx)
	}
	return nil
}

// serve builds the handler from cfg and serves it until ctx ends and the drain is over.
func serve(ctx context.Context, cfg proxyConfig, mode k8sapiproxy.Mode, version string, tp trace.TracerProvider) error {
	// TLS needs both paths; either one empty serves plain HTTP. The same path for both
	// means one PEM file holds the certificate chain and its private key.
	certFile, keyFile := cfg.tlsCertFile, cfg.tlsKeyFile
	if certFile == "" || keyFile == "" {
		certFile, keyFile = "", ""
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	metrics := k8sapiproxy.NewMetrics()
	handler, err := buildHandler(ctx, cfg, mode, logger, metrics, tp)
	if err != nil {
		return err
	}
	startMetricsListener(ctx, cfg.metricsPort, metrics, logger)

	srv := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           handler,
		ReadHeaderTimeout: readHeaderTimeout,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
		},
	}

	listen := srv.ListenAndServe
	if certFile != "" {
		serverCert, err := certreload.NewServerCertificate(certFile, keyFile, certreload.Options{
			OnReloadError: func(err error) {
				logger.Warn("serving the previous TLS certificate: the changed certificate or key could not be loaded", "err", err)
				metrics.RecordCertificateReloadFailure()
			},
		})
		if err != nil {
			return fmt.Errorf("failed to load the TLS server certificate: %w", err)
		}
		srv.TLSConfig.GetCertificate = serverCert.GetCertificate
		log.Printf("starting %s (TLS) on %s (mode=%s, authMode=%s, redactSecrets=%v, denyStreaming=%v, metricsPort=%d, cacheMaxBytes=%d)", versionString(version), cfg.listenAddr, mode, cfg.authMode, cfg.redactSecrets, cfg.denyStreaming, cfg.metricsPort, cfg.cacheMaxBytes)
		// Empty file names make the server take certificates from TLSConfig.GetCertificate.
		listen = func() error { return srv.ListenAndServeTLS("", "") }
	} else {
		log.Printf("starting %s (plain HTTP) on %s (mode=%s, authMode=%s, redactSecrets=%v, denyStreaming=%v, metricsPort=%d, cacheMaxBytes=%d)", versionString(version), cfg.listenAddr, mode, cfg.authMode, cfg.redactSecrets, cfg.denyStreaming, cfg.metricsPort, cfg.cacheMaxBytes)
	}
	drainErr, err := serveUntilDrained(ctx, srv, listen, serverShutdownTimeout)
	if err != nil {
		return fmt.Errorf("k8s-api-proxy server exited with error: %w", err)
	}
	if drainErr != nil {
		log.Printf("closed requests still open after the %s shutdown drain (open watches and log follows end this way): %v", serverShutdownTimeout, drainErr)
	}
	return nil
}

// serveUntilDrained runs listen until ctx ends, then stops srv accepting and
// returns only once its in-flight requests finish or timeout passes. Returning
// as soon as listen does would cut them: ListenAndServe returns the moment
// Shutdown starts, not when it ends. Hijacked connections (exec,
// port-forward) are not waited for. err is listen's own failure, such as a
// port already bound; drainErr is Shutdown's, set when timeout cut requests.
func serveUntilDrained(ctx context.Context, srv *http.Server, listen func() error, timeout time.Duration) (drainErr, err error) {
	drained := make(chan error, 1)
	go func() {
		<-ctx.Done()
		// ctx is already done; the drain gets its own deadline.
		shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), timeout)
		defer cancel()
		drained <- srv.Shutdown(shutdownCtx)
	}()
	if err := listen(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return nil, err
	}
	return <-drained, nil
}
