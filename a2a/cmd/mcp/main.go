// The a2a MCP server: a Model Context Protocol endpoint for one `next`
// install, as a console client of its bus. The operator renders it in its own
// Deployment only when the install opts in, behind a ClusterIP Service, and a
// person reaches it with kubectl port-forward. See package mcp.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/mcp"
)

const (
	exitFailure = 1

	envListen       = "MCP_LISTEN"
	envTokenFile    = "MCP_TOKEN_FILE"
	envBusURL       = "NATS_URL"
	envBusUser      = "MCP_BUS_USER"
	envPasswordFile = "MCP_BUS_PASSWORD_FILE"
	envAgent        = "MCP_AGENT_NAME"
	envNamespace    = "MCP_NAMESPACE"
	envCluster      = "MCP_CLUSTER_NAME"
	// The credential broker and the projected token (audience
	// kubeagents-credential-proxy-mcp) this pod presents to it, as the
	// operator renders them. inspect_pr is registered only when the URL is
	// set.
	envBrokerURL       = "A2A_MCP_BROKER_URL"
	envBrokerTokenFile = "A2A_MCP_BROKER_TOKEN_FILE" // #nosec G101 -- an env var name, not a credential

	defaultListen       = ":8089"
	defaultTokenFile    = "/var/run/secrets/a2a-mcp/token"
	defaultBusUser      = "console"
	defaultPasswordFile = "/var/run/secrets/a2a-mcp-bus/console-password" // #nosec G101 -- a path, not a credential

	networkTCP = "tcp"
	busScheme  = "tls://"
	// lineEndings are trimmed off the password file; a Secret written with
	// `kubectl create secret --from-file` keeps the trailing newline.
	lineEndings = "\r\n"

	readHeaderTimeout = 10 * time.Second
	idleTimeout       = 60 * time.Second
	// writeTimeout bounds one response. Every answer here is one JSON
	// document, and the slowest, connection_status, bounds its own read
	// (mcp.statusTimeout); a streamed answer, if a later tool adds one,
	// has to revisit this.
	writeTimeout  = 30 * time.Second
	shutdownGrace = 5 * time.Second
	// dialTimeout bounds the first connect to the bus.
	dialTimeout = 30 * time.Second
)

func main() {
	os.Exit(run())
}

func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := realMain(ctx, log, os.Getenv); err != nil {
		return exitFailure
	}
	return 0
}

type settings struct {
	listen    string
	tokenFile string
	bus       mcp.BusConfig
	// broker is nil when the install gave this pod no broker, and then
	// inspect_pr is not offered at all: a tool in tools/list that can only
	// fail tells the caller's model something untrue about this install.
	broker *mcp.BrokerClient
}

// configFromEnv reads the settings and the bus password. The token file is
// checked here too, so a pod without one fails at start, visibly, rather
// than serving 401s to everyone.
func configFromEnv(getenv func(string) string) (settings, error) {
	or := func(key, def string) string {
		if v := strings.TrimSpace(getenv(key)); v != "" {
			return v
		}
		return def
	}
	url := strings.TrimSpace(getenv(envBusURL))
	if url == "" {
		return settings{}, fmt.Errorf("%s is required", envBusURL)
	}
	// TLS by construction: the server sends the console password at
	// connect, so a plaintext URL is refused rather than left to the bus to
	// upgrade.
	if !strings.HasPrefix(url, busScheme) {
		return settings{}, fmt.Errorf("%s must be a %s URL, got %q", envBusURL, busScheme, url)
	}
	tokenFile := or(envTokenFile, defaultTokenFile)
	if _, err := mcp.ReadToken(tokenFile); err != nil {
		return settings{}, fmt.Errorf("%s: %w", envTokenFile, err)
	}
	passwordFile := or(envPasswordFile, defaultPasswordFile)
	raw, err := os.ReadFile(passwordFile)
	if err != nil {
		return settings{}, fmt.Errorf("%s: %w", envPasswordFile, err)
	}
	password := strings.TrimRight(string(raw), lineEndings)
	if password == "" {
		return settings{}, fmt.Errorf("%s: %s is empty", envPasswordFile, passwordFile)
	}
	var broker *mcp.BrokerClient
	if brokerURL := strings.TrimSpace(getenv(envBrokerURL)); brokerURL != "" {
		// Set means meant: a broker URL with no usable token is a pod that
		// fails at start, not one that quietly drops the tool.
		brokerTokenFile := strings.TrimSpace(getenv(envBrokerTokenFile))
		if brokerTokenFile == "" {
			return settings{}, fmt.Errorf("%s is set, so %s is required", envBrokerURL, envBrokerTokenFile)
		}
		if _, err := mcp.ReadToken(brokerTokenFile); err != nil {
			return settings{}, fmt.Errorf("%s: %w", envBrokerTokenFile, err)
		}
		broker, err = mcp.NewBrokerClient(mcp.BrokerConfig{URL: brokerURL, TokenFile: brokerTokenFile})
		if err != nil {
			return settings{}, fmt.Errorf("%s: %w", envBrokerURL, err)
		}
	}
	return settings{
		broker:    broker,
		listen:    or(envListen, defaultListen),
		tokenFile: tokenFile,
		bus: mcp.BusConfig{
			URL:      url,
			User:     or(envBusUser, defaultBusUser),
			Password: password,
			Deployment: mcp.Deployment{
				Agent:     getenv(envAgent),
				Namespace: getenv(envNamespace),
				Cluster:   getenv(envCluster),
			},
		},
	}, nil
}

func realMain(ctx context.Context, log *slog.Logger, getenv func(string) string) error {
	s, err := configFromEnv(getenv)
	if err != nil {
		log.Error("mcp config", "err", err)
		return err
	}
	s.bus.Logger = log
	dialCtx, cancel := context.WithTimeout(ctx, dialTimeout)
	bus, err := mcp.DialBus(dialCtx, s.bus)
	cancel()
	if err != nil {
		log.Error("mcp bus", "err", err)
		return err
	}
	defer bus.Close()
	srv, err := mcp.NewServer(toolsFor(bus, s.broker)...)
	if err != nil {
		log.Error("mcp server", "err", err)
		return err
	}
	ln, err := net.Listen(networkTCP, s.listen)
	if err != nil {
		log.Error("mcp listen", "addr", s.listen, "err", err)
		return err
	}
	return serve(ctx, log, ln, mcp.Handler(srv, s.tokenFile, log))
}

// toolsFor is the tools this pod offers: inspect_pr only when it has a
// broker.
func toolsFor(bus mcp.Bus, broker *mcp.BrokerClient) []mcp.Tool {
	tools := []mcp.Tool{mcp.ConnectionStatusTool(bus)}
	if broker != nil {
		tools = append(tools, mcp.InspectPRTool(broker))
	}
	return tools
}

// serve runs until ctx is done or the server fails.
func serve(ctx context.Context, log *slog.Logger, ln net.Listener, h http.Handler) error {
	srv := &http.Server{Handler: h, ReadHeaderTimeout: readHeaderTimeout, IdleTimeout: idleTimeout, WriteTimeout: writeTimeout}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	log.Info("mcp serving", "addr", ln.Addr().String(), "path", mcp.Path)

	select {
	case err := <-errc:
		log.Error("mcp server stopped", "err", err)
		return err
	case <-ctx.Done():
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("mcp shutdown", "err", err)
		return err
	}
	return nil
}
