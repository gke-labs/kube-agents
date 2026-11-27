// Command kube-proxy is the kube-agents build of k8s-api-proxy: the same server, with the
// task credential gate as its Authorize hook when the environment configures one, and the
// gate's workers as the callers admitted for the profile audience.
package main

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"os"
	"strconv"
	"strings"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/proxyserver"
	"github.com/gke-labs/kube-agents/kube-proxy/taskgate"
)

const (
	// versionFlag is the server's flag that prints the build and exits.
	versionFlag = "version"
	// flagTerminator ends flag parsing: nothing after it is a flag.
	flagTerminator = "--"
	// flagPrefix starts a flag, once or twice.
	flagPrefix = "-"
	// flagValueSeparator joins a flag to a value given in the same argument.
	flagValueSeparator = "="
)

// helpFlags are the names the flag package answers with the usage text.
var helpFlags = []string{"h", "help"}

// errGateUnconfigured is why refuseAll refuses: the gate's configuration did not load.
var errGateUnconfigured = errors.New("kube-proxy: the task credential gate's configuration is malformed, so every request is refused")

// version is the commit the kube-proxy image was built from, stamped by
// deploy/kube-proxy/Dockerfile with -ldflags "-X main.version=..." from KUBE_AGENTS_VERSION,
// which every image build passes as it does for the platform-agent image. A build without
// the stamp leaves it empty, which proxyserver reports as a development build.
var version string

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	opts, err := options(os.Args[1:], os.Getenv, logger)
	if err != nil {
		log.Fatal(err)
	}
	if err := proxyserver.Run(context.Background(), os.Args[1:], opts); err != nil {
		log.Fatal(err)
	}
}

// options are what this binary adds to the stock server: its version, the task credential
// gate the environment asks for, and the credential's header among the ones never
// forwarded, gate or no gate. With the gate on, the server also accepts the profile
// audience, from exactly the ServiceAccounts the gate's identity map names: a worker the
// proxy admits for that audience is always one the gate checks. With the gate off it
// accepts no audience the stock server does not.
//
// A malformed gate configuration is an error, except when args ask only for the version or
// the usage text: those answer whatever the environment holds. The server then gets a hook
// that refuses every request and no profile audience, so if args were misread and it serves
// after all, it serves nothing rather than serving ungated.
func options(args []string, getenv func(string) string, logger *slog.Logger) (proxyserver.Options, error) {
	opts := proxyserver.Options{
		Version:            version,
		ExtraCallerHeaders: taskgate.CallerHeaders(),
	}
	cfg, err := taskgate.LoadConfig(getenv)
	if err != nil {
		if !asksForVersionOrHelp(args) {
			return proxyserver.Options{}, err
		}
		opts.Authorize = refuseAll
		return opts, nil
	}
	if cfg != nil {
		opts.Authorize = taskgate.FromConfig(*cfg, logger)
		opts.AudienceCallers = map[string][]string{cfg.ProfileAudience: cfg.WorkerUsernames()}
	}
	return opts, nil
}

// asksForVersionOrHelp reports whether args hold the version flag, set true, or a help
// flag, before the flag terminator. It does not know which flags take a value, so a value
// spelled like one of these flags counts too; options makes that harmless.
func asksForVersionOrHelp(args []string) bool {
	for _, arg := range args {
		if arg == flagTerminator {
			return false
		}
		name, ok := strings.CutPrefix(arg, flagPrefix)
		if !ok {
			continue
		}
		name = strings.TrimPrefix(name, flagPrefix)
		name, value, hasValue := strings.Cut(name, flagValueSeparator)
		for _, help := range helpFlags {
			if name == help {
				return true
			}
		}
		if name != versionFlag {
			continue
		}
		if !hasValue {
			return true
		}
		if set, err := strconv.ParseBool(value); err == nil && set {
			return true
		}
	}
	return false
}

// refuseAll is the Authorize hook of a binary whose gate configuration did not load.
func refuseAll(k8sapiproxy.AuthorizeAttributes) error {
	return errGateUnconfigured
}
