package lib

import (
	"fmt"
	"os"
	"strings"

	"github.com/nats-io/nats.go"
)

// WithUserPassword authenticates as one of the deployment's static per-role
// users and pins the inbox prefix that user is granted.
//
// Both halves are required, and the second one is the trap: push delivery and
// every JetStream API request come back on an inbox subject, and the
// deployment gives each user its own prefix (_INBOX.<user>.>) precisely so
// that no agent can subscribe to another's replies. nats.go's default inbox is
// _INBOX.<nuid>, which that grant does not cover - so a client that sets only
// the password authenticates fine, publishes fine, and then hangs on the first
// reply with an authorization violation the server logs and the client sees as
// a timeout. Every component dials through here so none of them has to
// rediscover that.
//
// This is the static path, and it is no longer the only one: KSATokenNATSOptions
// below is what a workload with a projected ServiceAccount token dials through,
// and it is what the callout resolves. Both are live. What is the same across
// them is the deny-by-default subject lists and the per-user inbox prefix - the
// trap above is identical whichever credential got you in.
func WithUserPassword(user, password string) ClientOption {
	return func(o *clientOptions) {
		if user == "" {
			return
		}
		o.natsOpts = append(o.natsOpts,
			nats.UserInfo(user, password),
			nats.CustomInboxPrefix("_INBOX."+user),
		)
	}
}

// The bus token contract, as the deployment spec states it and as the operator
// renders it: a projected ServiceAccount token, audience-bound to the bus, at a
// fixed path. The spawner writes this contract onto session pods and the worker
// adapter reads it back, so both sides take it from here.
const (
	// BusTokenAudience is the audience the callout demands. A token minted
	// for any other audience — a pod's default token above all — is refused,
	// which is what stops every readable token in the cluster from being a
	// bus credential.
	BusTokenAudience = "a2a-bus"

	// BusTokenPath is where the projected volume delivers the token.
	BusTokenPath = "/var/run/secrets/a2a-bus/token"

	// EnvBusTokenFile lets a client be pointed at a token somewhere else
	// (local runs, tests). Unset means BusTokenPath.
	EnvBusTokenFile = "A2A_BUS_TOKEN_FILE"

	// EnvPodName carries the pod's own name by the downward API. Under the
	// callout a session's grants are derived from the pod name the API
	// server attests, and the client has to pin the matching inbox prefix
	// itself — so it reads the name the kubelet wrote, not one it was told.
	EnvPodName = "A2A_POD_NAME"
)

// WithKSAToken authenticates with a projected ServiceAccount token read from
// path, and pins the inbox prefix _INBOX.<inboxOwner> that the callout grants
// the resulting principal.
//
// The file is read on every connect and reconnect, never cached. The kubelet
// rotates the projected token, and the deployment spec calls a bus restart a
// routine operation — a client that presented its first read forever would
// fail exactly then, with an Authorization Violation that reads like a
// revoked identity. The read happens inside nats.go's connect path, so a file
// that goes missing later surfaces as a failed reconnect attempt (retried on
// the library's backoff) rather than a crash; a file missing at the first
// connect is checked here and named, because that one is a misconfiguration.
//
// inboxOwner must be a single subject token: it becomes part of every reply
// subject, and a dot would split it into tokens the grant does not cover.
// For a session pod it is the pod's own name (EnvPodName), which is also the
// addressee and the thing the callout derived the grant from.
func WithKSAToken(path, inboxOwner string) ClientOption {
	return func(o *clientOptions) {
		opts, err := KSATokenNATSOptions(path, inboxOwner)
		if err != nil {
			o.err = err
			return
		}
		o.natsOpts = append(o.natsOpts, opts...)
	}
}

// KSATokenNATSOptions is the same thing as raw nats.go options, for a caller
// that also opens a plain nats.Conn.
//
// The worker adapter is that caller: it holds a lib client for validated
// publishes and a raw JetStream handle for its consumers, and the two are one
// principal on the bus. Building the options once means the second connection
// cannot end up with the token and not the inbox prefix — a combination that
// authenticates fine and then times out on every JetStream call, which is the
// worst failure shape in this system to read from the outside.
func KSATokenNATSOptions(path, inboxOwner string) ([]nats.Option, error) {
	if path == "" {
		return nil, fmt.Errorf("bus token: no token path")
	}
	if !ValidSubjectToken(inboxOwner) {
		return nil, fmt.Errorf("bus token: inbox owner %q is not a single subject token", inboxOwner)
	}
	if _, err := readToken(path); err != nil {
		return nil, fmt.Errorf("bus token: %w", err)
	}
	return []nats.Option{
		nats.TokenHandler(func() string {
			tok, err := readToken(path)
			if err != nil {
				// No logger here and no error return; an empty token is
				// refused by the server and the reconnect loop tries
				// again, which is the right shape for a transient
				// rotation race.
				return ""
			}
			return tok
		}),
		nats.CustomInboxPrefix("_INBOX." + inboxOwner),
	}, nil
}

// readToken reads and trims a token file. The kubelet writes the token with no
// trailing newline; a hand-written one may carry it, and a newline inside a
// CONNECT frame is a protocol error rather than an auth failure.
func readToken(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("reading the bus token: %w", err)
	}
	tok := strings.TrimSpace(string(raw))
	if tok == "" {
		return "", fmt.Errorf("reading the bus token: %s is empty", path)
	}
	return tok, nil
}
