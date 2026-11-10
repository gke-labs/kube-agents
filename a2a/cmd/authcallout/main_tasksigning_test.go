package main

import (
	"io"
	"log/slog"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// A malformed task-signing key list stops the callout at startup, naming the
// variable; an absent or well-formed one lets it carry on to the in-cluster
// config a test process does not have.
func TestTheCalloutChecksItsTaskSigningKeysAtStartup(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, _ := kp.PublicKey()

	for name, value := range map[string]string{
		"an empty element":    pub + ",",
		"not a key":           "not-a-key",
		"an account-shaped A": "A" + pub[1:],
		"three keys":          pub + "," + pub + "," + pub,
	} {
		t.Run(name, func(t *testing.T) {
			setEnvBeforeAddressees(t)
			t.Setenv(envReservedAddressees, "platform")
			t.Setenv(envTaskSigningKeys, value)
			if err := run(log); err == nil || !strings.Contains(err.Error(), envTaskSigningKeys) {
				t.Fatalf("run() = %v, want a refusal naming %s", err, envTaskSigningKeys)
			}
		})
	}
	for name, value := range map[string]string{"unset": "", "one key": pub} {
		t.Run(name, func(t *testing.T) {
			setEnvBeforeAddressees(t)
			t.Setenv(envReservedAddressees, "platform")
			t.Setenv(envTaskSigningKeys, value)
			err := run(log)
			if err == nil || strings.Contains(err.Error(), envTaskSigningKeys) || !strings.Contains(err.Error(), "in-cluster config") {
				t.Fatalf("run() = %v, want it past the task-signing guard and stopped at the in-cluster config", err)
			}
		})
	}
}

// The task-credential lifetime never stops startup: a good value, an unset
// one and a bad one all carry on to the in-cluster config. A bad one refuses
// task credentials instead (authcallout's TestAnUnusableLifetimeSetting...).
func TestTheTaskCredentialLifetimeNeverStopsStartup(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	// "6h0m0s" and "1m0s" are how the operator renders it (time.Duration.String).
	for _, value := range []string{"", "6h0m0s", "1m0s", "6h", "90m", "6", "six hours", "0s", "-1h"} {
		t.Run("value "+value, func(t *testing.T) {
			setEnvBeforeAddressees(t)
			t.Setenv(envReservedAddressees, "platform")
			t.Setenv(envTaskCredentialMaxLifetime, value)
			err := run(log)
			if err == nil || strings.Contains(err.Error(), envTaskCredentialMaxLifetime) || !strings.Contains(err.Error(), "in-cluster config") {
				t.Fatalf("run() = %v, want it past the guard and stopped at the in-cluster config", err)
			}
		})
	}
}
