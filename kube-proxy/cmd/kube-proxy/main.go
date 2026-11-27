// Command kube-proxy is the kube-agents build of k8s-api-proxy: the same server, with the
// task credential gate as its Authorize hook when the environment configures one, the
// gate's workers as the callers admitted for the profile audience, and a limit on what each
// of those workers may have open at once.
package main

import (
	"context"
	"errors"
	"fmt"
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

	// workersNotAdmittedFormat is the startup error of a gate whose workers the server
	// cannot admit for the profile audience. Its arguments are the profile audience's
	// variable, the identity map's, and the server's own error.
	workersNotAdmittedFormat = "kube-proxy: the task credential gate is on, and the server cannot admit the workers %[2]s names for the audience in %[1]s: %[3]w. " +
		"Set %[1]s to an audience that is neither --audience nor CREDENTIAL_PROXY_SESSION_AUDIENCE, " +
		"keep every ServiceAccount %[2]s names out of --allowed-callers and CREDENTIAL_PROXY_SESSION_CALLERS, " +
		"and keep token-review in --auth-mode; or unset %[2]s to turn the gate off"
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
		log.Fatal(startupError(err, opts))
	}
}

// startupError is the error the binary exits on for err, the server's. The server names
// Options.AudienceCallers when it refuses the gate's workers, a field no operator set, so
// that error is wrapped to name the two variables the audience and its callers came from
// and what to change. Any other error is the server's own.
func startupError(err error, opts proxyserver.Options) error {
	var refused *proxyserver.AudienceCallersError
	if len(opts.AudienceCallers) == 0 || !errors.As(err, &refused) {
		return err
	}
	return fmt.Errorf(workersNotAdmittedFormat, taskgate.ProfileAudienceEnv, taskgate.IdentitiesEnv, err)
}

// options are what this binary adds to the stock server: its version, the task credential
// gate the environment asks for, and the credential's header among the ones never
// forwarded, gate or no gate. With the gate on, the server also accepts the profile
// audience, from exactly the ServiceAccounts the gate's identity map names: a worker the
// proxy admits for that audience is always one the gate checks, and each of them is held to
// the gate's worker limits, which no other caller gets. With the gate off it accepts no
// audience and limits no caller the stock server does not.
//
// A malformed gate configuration is an error, except when args ask only for the version or
// the usage text: those answer whatever the environment holds. The server then gets a hook
// that refuses every request, no profile audience and no worker limits, so if args were misread and it serves
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
		opts.CallerLimits = cfg.CallerLimits()
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
