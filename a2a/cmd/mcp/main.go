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
	// writeTimeout bounds one response, so it must outlast the slowest call
	// a caller may ask for: send_message waits for the gateway's submission
	// and then up to the longest answer wait (mcp.MaxTurnWait), which also
	// outlasts inspect_pr's own bound (mcp.InspectTimeout). A streamed
	// answer, if a later tool adds one, has to revisit this.
	writeTimeout = mcp.MaxTurnWait + time.Minute
	// shutdownGrace is how long a stop waits for the requests in flight,
	// whose contexts the stop has already ended (serve), so a stop is a
	// clean drain well inside the pod's default 30 s termination grace.
	shutdownGrace = 10 * time.Second
	// dialTimeout bounds one attempt at the first connect to the bus.
	dialTimeout = 30 * time.Second
	// The first connect is retried, from dialBackoff doubling to
	// maxDialBackoff, rather than exiting: on a fresh install the bus comes
	// up after this pod, and a crash loop says nothing a log line does not.
	// The pod's startup probe bounds how long it may retry before it is
	// restarted.
	dialBackoff    = time.Second
	maxDialBackoff = 30 * time.Second
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
	// busCredentialPath is where the bus password is read from, not the
	// password: the errors below name it so the operator can find the mount,
	// and realMain logs them. CodeQL's go/clear-text-logging takes any
	// password-shaped identifier as a credential, path or not, so the path
	// keeps a name outside that heuristic; password, the value, keeps its
	// name and is never formatted into an error. mcp.BusConfig.PasswordFile
	// keeps its name because nothing reads it into a log or an error; a
	// log line that names it would refire the alert there.
	busCredentialPath := or(envPasswordFile, defaultPasswordFile)
	raw, err := os.ReadFile(busCredentialPath)
	if err != nil {
		return settings{}, fmt.Errorf("%s: %w", envPasswordFile, err)
	}
	password := strings.TrimRight(string(raw), lineEndings)
	if password == "" {
		return settings{}, fmt.Errorf("%s: %s is empty", envPasswordFile, busCredentialPath)
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
			URL:          url,
			User:         or(envBusUser, defaultBusUser),
			Password:     password,
			PasswordFile: busCredentialPath,
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
	bus, err := dialWithRetry(ctx, log, dialBackoff, func(ctx context.Context) (*mcp.NATSBus, error) {
		dialCtx, cancel := context.WithTimeout(ctx, dialTimeout)
		defer cancel()
		return mcp.DialBus(dialCtx, s.bus)
	})
	if err != nil {
		// Stopped while still waiting for the bus (a rollout or a drain
		// during an outage): a clean stop, as serve treats one.
		if ctx.Err() != nil && errors.Is(err, ctx.Err()) {
			log.Info("stopped before the bus was reachable")
			return nil
		}
		log.Error("mcp bus", "err", err)
		return err
	}
	defer bus.Close()
	convs := mcp.NewConversations()
	srv, err := mcp.NewServer(toolsFor(bus, convs, s.broker)...)
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
// toolBus is what the tools read and send through: the status read and the
// turn tools' bus.
type toolBus interface {
	mcp.Bus
	mcp.TurnBus
	mcp.TopicsBus
}

func toolsFor(bus toolBus, convs *mcp.Conversations, broker *mcp.BrokerClient) []mcp.Tool {
	tools := []mcp.Tool{
		mcp.ConnectionStatusTool(bus),
		mcp.SendMessageTool(bus, convs),
		mcp.TaskControlTool(bus, convs),
		mcp.ManageSessionTool(bus, convs),
		mcp.ReadTopicsTool(bus),
	}
	if broker != nil {
		tools = append(tools, mcp.InspectPRTool(broker))
	}
	return tools
}

// dialWithRetry calls dial until it succeeds, the bus refuses it for good
// (mcp.DialRefused), or ctx is done, waiting backoff after the first failure
// and doubling it to maxDialBackoff, and logs each failure with the wait
// before the next attempt.
func dialWithRetry[B any](ctx context.Context, log *slog.Logger, backoff time.Duration, dial func(context.Context) (B, error)) (B, error) {
	for {
		b, err := dial(ctx)
		if err == nil {
			return b, nil
		}
		// A refused credential or an untrusted certificate is no transient:
		// fail now, under its own cause, so the pod restarts with that in its
		// log rather than retrying for minutes as "not reachable yet".
		if mcp.DialRefused(err) {
			var zero B
			return zero, fmt.Errorf("the bus refused the connection (check the console credential and the bus CA): %w", err)
		}
		log.Warn("the bus is not reachable yet; retrying", "in", backoff, "err", err)
		select {
		case <-ctx.Done():
			var zero B
			return zero, ctx.Err()
		case <-time.After(backoff):
		}
		backoff = min(2*backoff, maxDialBackoff)
	}
}

// serve runs until ctx is done or the server fails. Every request's context
// derives from ctx, so a stop ends the requests in flight rather than waiting
// them out past shutdownGrace.
func serve(ctx context.Context, log *slog.Logger, ln net.Listener, h http.Handler) error {
	srv := &http.Server{Handler: h, ReadHeaderTimeout: readHeaderTimeout, IdleTimeout: idleTimeout, WriteTimeout: writeTimeout,
		BaseContext: func(net.Listener) context.Context { return ctx }}
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
