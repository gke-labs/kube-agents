// The a2a console server: serves the console page, hands it the bus
// credential, and proxies its websocket to the bus. The operator renders it
// under spec.mode: next behind a ClusterIP Service, and a person reaches it
// with kubectl port-forward. See package console for what it answers.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/console"
)

const (
	// exitFailure is the one non-zero exit code: config, listen and serve
	// failures all leave through it, each logged where it was found.
	exitFailure = 1

	envListen       = "CONSOLE_LISTEN"
	envStaticDir    = "CONSOLE_STATIC_DIR"
	envBusURL       = "CONSOLE_BUS_URL"
	envUser         = "CONSOLE_USER"
	envPasswordFile = "CONSOLE_PASSWORD_FILE"
	envAllowedHosts = "CONSOLE_ALLOWED_HOSTS"

	defaultListen       = ":8080"
	defaultStaticDir    = "/srv/console"
	defaultUser         = "console"
	defaultPasswordFile = "/var/run/secrets/a2a-console/console-password" // #nosec G101 -- a path, not a credential
	defaultAllowedHosts = "localhost:8080,127.0.0.1:8080"

	hostListSeparator = ","
	networkTCP        = "tcp"

	// readHeaderTimeout bounds a slow client's headers. There is no
	// WriteTimeout or ReadTimeout on purpose: either one would cut every
	// proxied websocket at the deadline.
	readHeaderTimeout = 10 * time.Second
	// shutdownGrace is how long in-flight plain requests get on SIGTERM.
	// Proxied websockets are hijacked, so Shutdown does not wait for them;
	// the page's bus client reconnects to the next pod.
	shutdownGrace = 5 * time.Second
)

func main() {
	os.Exit(run())
}

// run owns what a test cannot: the process logger, the signal context and
// the exit code.
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
	listen  string
	console console.Config
}

func configFromEnv(getenv func(string) string) (settings, error) {
	or := func(key, def string) string {
		if v := getenv(key); v != "" {
			return v
		}
		return def
	}
	raw := getenv(envBusURL)
	if raw == "" {
		return settings{}, fmt.Errorf("%s is required", envBusURL)
	}
	bus, err := url.Parse(raw)
	if err != nil {
		return settings{}, fmt.Errorf("%s: %w", envBusURL, err)
	}
	var hosts []string
	for _, h := range strings.Split(or(envAllowedHosts, defaultAllowedHosts), hostListSeparator) {
		if h = strings.TrimSpace(h); h != "" {
			hosts = append(hosts, h)
		}
	}
	return settings{
		listen: or(envListen, defaultListen),
		console: console.Config{
			StaticDir:    or(envStaticDir, defaultStaticDir),
			BusURL:       bus,
			User:         or(envUser, defaultUser),
			PasswordFile: or(envPasswordFile, defaultPasswordFile),
			AllowedHosts: hosts,
		},
	}, nil
}

func realMain(ctx context.Context, log *slog.Logger, getenv func(string) string) error {
	s, err := configFromEnv(getenv)
	if err != nil {
		log.Error("console config", "err", err)
		return err
	}
	h, err := console.NewHandler(s.console, log)
	if err != nil {
		log.Error("console config", "err", err)
		return err
	}
	ln, err := net.Listen(networkTCP, s.listen)
	if err != nil {
		log.Error("console listen", "addr", s.listen, "err", err)
		return err
	}
	return serve(ctx, log, ln, h)
}

// serve runs until ctx is done or the server fails.
func serve(ctx context.Context, log *slog.Logger, ln net.Listener, h http.Handler) error {
	srv := &http.Server{Handler: h, ReadHeaderTimeout: readHeaderTimeout}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	log.Info("console serving", "addr", ln.Addr().String())

	select {
	case err := <-errc:
		log.Error("console server stopped", "err", err)
		return err
	case <-ctx.Done():
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("console shutdown", "err", err)
		return err
	}
	if err := <-errc; err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
