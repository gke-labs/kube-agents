package gateway

import (
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A2A_GATEWAY_BUS_AUTH (#5301): password by default, so a running install is
// unchanged until the operator flips it; token reads the bus token from the
// lib contract's path; anything else refuses the boot.
func TestTheGatewaysBusAuthDefaultsToPassword(t *testing.T) {
	setBaseEnv(t)
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.BusAuth != BusAuthPassword || cfg.BusTokenFile != "" {
		t.Errorf("bus auth = %q, token file %q; want password and none", cfg.BusAuth, cfg.BusTokenFile)
	}
}

func TestTokenBusAuthReadsTheBusTokenContract(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("SESSION_KV_SALT", "install-salt")
	t.Setenv(envGatewayBusAuth, BusAuthToken)
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.BusTokenFile != lib.BusTokenPath {
		t.Errorf("token file = %q, want %q", cfg.BusTokenFile, lib.BusTokenPath)
	}
	t.Setenv(lib.EnvBusTokenFile, "/tmp/elsewhere")
	if cfg, err = FromEnv(); err != nil || cfg.BusTokenFile != "/tmp/elsewhere" {
		t.Errorf("token file = %q, %v; want the override", cfg.BusTokenFile, err)
	}
}

func TestAnUnknownBusAuthRefusesTheBoot(t *testing.T) {
	setBaseEnv(t)
	t.Setenv(envGatewayBusAuth, "jwt")
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), envGatewayBusAuth) {
		t.Fatalf("err = %v, want a refusal naming %s", err, envGatewayBusAuth)
	}
}

// The salt trap: on token auth the derived fallback is refused even with a
// password still set, because that password goes a release after the flip
// and every pseudonym would re-key with it.
func TestTokenBusAuthRefusesTheDerivedSalt(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("NATS_PASSWORD", "bus-password")
	t.Setenv(envGatewayBusAuth, BusAuthToken)
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), "SESSION_KV_SALT") {
		t.Fatalf("err = %v, want the provisioned-salt refusal", err)
	}
	t.Setenv("A2A_ATTRIBUTION_SALT", "explicit")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if string(cfg.AttributionSalt) != "explicit" {
		t.Errorf("salt = %q, want the explicit one", cfg.AttributionSalt)
	}
}
