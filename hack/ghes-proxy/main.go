// Copyright 2026 The Kubernetes Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// ghes-proxy is an HTTPS forwarding proxy that has the shape of a GitHub
// Enterprise Server: the REST API at /api/v3, GraphQL at /api/graphql and git
// smart HTTP at /<owner>/<repo>.git. It forwards each request to github.com
// and gives the answers back with the URLs a GitHub Enterprise Server at the
// public host would give. The eval lanes use it to test kube-agents' GitHub
// Enterprise Server support against real github.com data. See README.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type options struct {
	listen   string
	certFile string
	keyFile  string
	cfg      Config
}

// hostPortRE is a bare hostname with an optional port.
var hostPortRE = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*(?::[0-9]{1,5})?$`)

func envOr(getenv func(string) string, name, def string) string {
	if v := getenv(name); v != "" {
		return v
	}
	return def
}

// parseOptions reads the flags; each flag's default comes from its
// environment variable, when set.
func parseOptions(args []string, getenv func(string) string, stderr io.Writer) (options, error) {
	fs := flag.NewFlagSet("ghes-proxy", flag.ContinueOnError)
	fs.SetOutput(stderr)
	var o options
	maxRewrite := envOr(getenv, "GHES_PROXY_MAX_REWRITE_BYTES", strconv.Itoa(DefaultMaxRewriteBytes))
	fs.StringVar(&o.listen, "listen", envOr(getenv, "GHES_PROXY_LISTEN", ":8443"), "address to listen on for HTTPS (env GHES_PROXY_LISTEN)")
	fs.StringVar(&o.certFile, "cert-file", envOr(getenv, "GHES_PROXY_CERT_FILE", "/etc/ghes-proxy/tls/tls.crt"), "server certificate, PEM (env GHES_PROXY_CERT_FILE)")
	fs.StringVar(&o.keyFile, "key-file", envOr(getenv, "GHES_PROXY_KEY_FILE", "/etc/ghes-proxy/tls/tls.key"), "server private key, PEM (env GHES_PROXY_KEY_FILE)")
	fs.StringVar(&o.cfg.PublicHost, "public-host", getenv("GHES_PROXY_PUBLIC_HOST"), "the name clients use for the proxy, host or host:port (env GHES_PROXY_PUBLIC_HOST, required)")
	fs.StringVar(&o.cfg.UpstreamAPIHost, "upstream-api-host", envOr(getenv, "GHES_PROXY_UPSTREAM_API_HOST", "api.github.com"), "the upstream API host (env GHES_PROXY_UPSTREAM_API_HOST)")
	fs.StringVar(&o.cfg.UpstreamWebHost, "upstream-web-host", envOr(getenv, "GHES_PROXY_UPSTREAM_WEB_HOST", "github.com"), "the upstream git and web host (env GHES_PROXY_UPSTREAM_WEB_HOST)")
	fs.StringVar(&o.cfg.EnterpriseVersion, "enterprise-version", envOr(getenv, "GHES_PROXY_ENTERPRISE_VERSION", DefaultEnterpriseVersion), "the X-GitHub-Enterprise-Version header value (env GHES_PROXY_ENTERPRISE_VERSION)")
	fs.StringVar(&maxRewrite, "max-rewrite-bytes", maxRewrite, "the largest JSON body read whole to rewrite (env GHES_PROXY_MAX_REWRITE_BYTES)")
	if err := fs.Parse(args); err != nil {
		return o, err
	}
	if fs.NArg() != 0 {
		return o, fmt.Errorf("unexpected arguments: %v", fs.Args())
	}
	n, err := strconv.ParseInt(maxRewrite, 10, 64)
	if err != nil || n <= 0 || n > MaxMaxRewriteBytes {
		return o, fmt.Errorf("max-rewrite-bytes %q is not an integer from 1 to %d", maxRewrite, MaxMaxRewriteBytes)
	}
	o.cfg.MaxRewriteBytes = n
	for name, v := range map[string]string{
		"public-host":       o.cfg.PublicHost,
		"upstream-api-host": o.cfg.UpstreamAPIHost,
		"upstream-web-host": o.cfg.UpstreamWebHost,
	} {
		if !hostPortRE.MatchString(v) {
			return o, fmt.Errorf("%s %q is not a hostname with an optional port (no scheme, no path)", name, v)
		}
	}
	return o, nil
}

func main() {
	logger := log.New(os.Stderr, "", log.LstdFlags|log.LUTC)
	o, err := parseOptions(os.Args[1:], os.Getenv, os.Stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return
		}
		logger.Fatalf("ghes-proxy: %v", err)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	srv := &http.Server{
		Addr:              o.listen,
		Handler:           NewHandler(o.cfg, transport, logger),
		ReadHeaderTimeout: 30 * time.Second,
		IdleTimeout:       120 * time.Second,
		// No read or write timeout for the whole request: a pack can take
		// minutes to stream. The server's own errors (TLS handshakes, bad
		// requests) carry no header value. The kubelet's TCP probes close
		// before a TLS hello, which the server logs as a handshake EOF; that
		// line is dropped (see probeNoiseFilter).
		ErrorLog: log.New(probeNoiseFilter{logger.Writer()}, "", log.LstdFlags|log.LUTC),
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	logger.Printf("ghes-proxy: listening on %s as %s, upstream %s and %s, version %s",
		o.listen, o.cfg.PublicHost, o.cfg.UpstreamAPIHost, o.cfg.UpstreamWebHost, o.cfg.EnterpriseVersion)
	start := func() error { return srv.ListenAndServeTLS(o.certFile, o.keyFile) }
	if err := serve(ctx, srv, start, shutdownGrace); err != nil {
		logger.Fatalf("ghes-proxy: %v", err)
	}
}

// shutdownGrace is how long a stop waits for the requests in flight; the
// Deployment's terminationGracePeriodSeconds must be longer.
const shutdownGrace = 25 * time.Second

// serve runs start until ctx is done, then shuts srv down and returns only
// after the requests in flight have finished or grace has passed.
// ListenAndServe returns as soon as Shutdown closes the listener, so main
// must wait for Shutdown itself, or the process exits mid-request.
func serve(ctx context.Context, srv *http.Server, start func() error, grace time.Duration) error {
	done := make(chan error, 1)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), grace)
		defer cancel()
		done <- srv.Shutdown(shutdown)
	}()
	if err := start(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return <-done
}

// probeNoiseFilter drops the server's "TLS handshake error from <addr>: EOF"
// lines: a TCP probe connects and closes without a TLS hello, every period,
// and nothing else produces that exact line. Every other handshake error (a
// bad certificate, a protocol mismatch) carries its cause and still goes
// through. The probes stay TCP because the proxy answers 404 to every path it
// does not forward, so it has no health path to probe over HTTPS.
type probeNoiseFilter struct{ w io.Writer }

func (f probeNoiseFilter) Write(p []byte) (int, error) {
	line := strings.TrimRight(string(p), "\n")
	if strings.Contains(line, "http: TLS handshake error from ") && strings.HasSuffix(line, ": EOF") {
		return len(p), nil
	}
	return f.w.Write(p)
}
