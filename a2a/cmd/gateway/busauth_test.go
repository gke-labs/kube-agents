package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/gateway"
)

func applied(t *testing.T, opts []nats.Option) nats.Options {
	t.Helper()
	o := nats.GetDefaultOptions()
	for _, opt := range opts {
		if err := opt(&o); err != nil {
			t.Fatal(err)
		}
	}
	return o
}

// On token auth the gateway presents its ServiceAccount token and no user, and
// keeps _INBOX.gateway, which the callout's map entry grants; on password it
// is the static user, as before (#5301).
func TestTheGatewayDialsAsItsBusAuthSays(t *testing.T) {
	tok := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tok, []byte("ksa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	opts, err := gatewayNATSOptions(&gateway.Config{BusAuth: gateway.BusAuthToken, BusTokenFile: tok, NATSUser: "gateway", NATSPassword: "pw"})
	if err != nil {
		t.Fatal(err)
	}
	o := applied(t, opts)
	if o.TokenHandler == nil || o.TokenHandler() != "ksa-token" {
		t.Error("token auth does not present the bus token")
	}
	if o.User != "" || o.Password != "" {
		t.Errorf("token auth also presents user %q; the server would take the static path", o.User)
	}
	if o.InboxPrefix != "_INBOX.gateway" {
		t.Errorf("inbox prefix = %q, want _INBOX.gateway", o.InboxPrefix)
	}

	opts, err = gatewayNATSOptions(&gateway.Config{BusAuth: gateway.BusAuthPassword, NATSUser: "gateway", NATSPassword: "pw"})
	if err != nil {
		t.Fatal(err)
	}
	o = applied(t, opts)
	if o.User != "gateway" || o.Password != "pw" || o.TokenHandler != nil || o.InboxPrefix != "_INBOX.gateway" {
		t.Errorf("password auth = user %q, token handler set %v, inbox %q", o.User, o.TokenHandler != nil, o.InboxPrefix)
	}
}

// A token mount missing at boot is a misconfiguration, named before dialing.
func TestTokenBusAuthWithNoTokenFails(t *testing.T) {
	if _, err := gatewayNATSOptions(&gateway.Config{BusAuth: gateway.BusAuthToken, BusTokenFile: filepath.Join(t.TempDir(), "absent")}); err == nil {
		t.Fatal("token auth with no token file dialed anyway")
	}
}
