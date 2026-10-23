package main

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	gkehttpstransport "github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/transport/gkehttps"
)

const (
	metricsUnavailableAlert = "ALERT metrics listener unavailable; kube-proxy serves no /metrics until it restarts, and API proxying is unaffected"
)

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
		log.Printf("%s: KUBE_PROXY_METRICS_PORT=%q is not an integer", metricsUnavailableAlert, raw)
		return 0
	}
	return v
}

func startMetricsListener(ctx context.Context, port int, metrics *k8sapiproxy.Metrics, logger *slog.Logger) {
	if port == 0 {
		logger.Info("metrics listener disabled: KUBE_PROXY_METRICS_PORT is unset")
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

func main() {
	defaultCluster := envOrDefault("KUBE_PROXY_DEFAULT_CLUSTER", envOrDefault("KUBE_CONTEXT_NAME", "in-cluster"))

	var (
		listenAddr      = flag.String("listen-addr", envOrDefault("KUBE_PROXY_LISTEN_ADDR", ":8443"), "TCP address to listen on")
		metricsPort     = flag.Int("metrics-port", envIntOrDefault("KUBE_PROXY_METRICS_PORT", 0), "TCP port for the Prometheus /metrics listener (0 disables the listener)")
		tlsCertFile     = flag.String("tls-cert-file", envOrDefault("KUBE_PROXY_TLS_CERT_FILE", "/var/run/secrets/kubeagents/kube-proxy-tls/tls.crt"), "TLS server certificate PEM file (empty for plain HTTP)")
		tlsKeyFile      = flag.String("tls-key-file", envOrDefault("KUBE_PROXY_TLS_KEY_FILE", "/var/run/secrets/kubeagents/kube-proxy-tls/tls.key"), "TLS server private key PEM file")
		modeRaw         = flag.String("mode", envOrDefault("KUBE_PROXY_MODE", string(k8sapiproxy.ModeReadOnlyWithDryRun)), "Proxy policy mode: read-only, read-only-dry-run, or read-write")
		defaultClusterF          = flag.String("default-cluster", defaultCluster, "Default cluster ID when /clusters/{id} prefix is omitted")
		redactSecrets            = flag.Bool("redact-secrets", envBoolOrDefault("KUBE_PROXY_REDACT_SECRETS", true), "Redact core/v1 Secret payloads and rewrite Accept headers to PartialObjectMetadata")
		allowUpgradeSubresources = flag.Bool("allow-upgrade-subresources", envBoolOrDefault("KUBE_PROXY_ALLOW_UPGRADE_SUBRESOURCES", false), "Allow exec, attach, portforward, and proxy subresources when mode is read-write")
		kubeAPIServer            = flag.String("kube-api-server", envOrDefault("CREDENTIAL_PROXY_KUBE_API_SERVER", "https://kubernetes.default.svc"), "Host cluster Kubernetes API server URL")
		kubeCAFile      = flag.String("kube-ca-file", envOrDefault("CREDENTIAL_PROXY_KUBE_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"), "Host cluster CA bundle PEM file")
		kubeTokenFile   = flag.String("kube-token-file", envOrDefault("CREDENTIAL_PROXY_KUBE_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"), "Host cluster ServiceAccount token file")
		audience        = flag.String("audience", envOrDefault("CREDENTIAL_PROXY_AUDIENCE", "kubeagents-credential-proxy"), "Required TokenReview JWT audience")
		allowedCallers  = flag.String("allowed-callers", envOrDefault("CREDENTIAL_PROXY_ALLOWED_CALLERS", ""), "Comma-separated allowlist of caller ServiceAccount usernames")
	)
	flag.Parse()

	mode, err := k8sapiproxy.ParseMode(*modeRaw)
	if err != nil {
		log.Fatalf("invalid mode: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	otelShutdown, tp, err := k8sapiproxy.SetupOTel(ctx)
	if err != nil {
		log.Printf("warning: failed to initialize OpenTelemetry tracing, continuing with no-op tracer: %v", err)
	} else if otelShutdown != nil {
		defer func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = otelShutdown(shutdownCtx)
		}()
	}

	var callers []string
	for _, entry := range strings.Split(*allowedCallers, ",") {
		if trimmed := strings.TrimSpace(entry); trimmed != "" {
			callers = append(callers, trimmed)
		}
	}
	if len(callers) == 0 {
		log.Fatalf("--allowed-callers (or CREDENTIAL_PROXY_ALLOWED_CALLERS) is required and must name at least one caller ServiceAccount")
	}

	authn, err := k8sapiproxy.NewTokenReviewAuthenticator(k8sapiproxy.TokenReviewConfig{
		APIServerURL:   *kubeAPIServer,
		CAFile:         *kubeCAFile,
		TokenFile:      *kubeTokenFile,
		Audience:       *audience,
		AllowedCallers: callers,
	})
	if err != nil {
		log.Fatalf("failed to initialize TokenReview authenticator: %v", err)
	}

	gcpTokens := gkehttpstransport.NewGCPTokenSource(gkehttpstransport.GCPTokenConfig{
		WIFAudience:       os.Getenv("CREDENTIAL_PROXY_WIF_AUDIENCE"),
		WIFServiceAccount: os.Getenv("CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT"),
		WIFTokenFile:      os.Getenv("CREDENTIAL_PROXY_WIF_TOKEN_FILE"),
	})

	upstream, err := gkehttpstransport.NewTransport(gkehttpstransport.Config{
		HostClusterID:      *defaultClusterF,
		InClusterServerURL: *kubeAPIServer,
		InClusterCAFile:    *kubeCAFile,
		InClusterTokenFile: *kubeTokenFile,
		TokenProvider:      gcpTokens,
	})
	if err != nil {
		log.Fatalf("failed to initialize upstream gke-https transport: %v", err)
	}

	metrics := k8sapiproxy.NewMetrics()
	startMetricsListener(ctx, *metricsPort, metrics, slog.Default())
	auditSink := k8sapiproxy.NewJSONAuditSink(os.Stdout)

	handler, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator:            authn,
		Upstream:                 upstream,
		DefaultClusterID:         *defaultClusterF,
		Mode:                     mode,
		RedactSecrets:            *redactSecrets,
		AllowUpgradeSubresources: *allowUpgradeSubresources,
		Metrics:                  metrics,
		TracerProvider:           tp,
		AuditSink:                auditSink,
	})
	if err != nil {
		log.Fatalf("failed to initialize k8s-api-proxy handler: %v", err)
	}

	srv := &http.Server{
		Addr:              *listenAddr,
		Handler:           handler,
		ReadHeaderTimeout: 15 * time.Second,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
		},
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	if *tlsCertFile != "" && *tlsKeyFile != "" {
		log.Printf("starting k8s-api-proxy (TLS) on %s (mode=%s, defaultCluster=%s, redactSecrets=%v, metricsPort=%d)", *listenAddr, mode, *defaultClusterF, *redactSecrets, *metricsPort)
		err = srv.ListenAndServeTLS(*tlsCertFile, *tlsKeyFile)
	} else {
		log.Printf("starting k8s-api-proxy (plain HTTP) on %s (mode=%s, defaultCluster=%s, redactSecrets=%v, metricsPort=%d)", *listenAddr, mode, *defaultClusterF, *redactSecrets, *metricsPort)
		err = srv.ListenAndServe()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("k8s-api-proxy server exited with error: %v", err)
	}
}
