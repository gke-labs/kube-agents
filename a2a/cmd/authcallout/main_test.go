package main

import (
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"

	"github.com/nats-io/nkeys"
)

// setRequiredEnv sets every variable run reads before the reserved principal
// list, so the list is the first thing that can refuse.
func setRequiredEnv(t *testing.T) {
	t.Helper()
	kp, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	seed, _ := kp.Seed()
	t.Setenv(envNamespace, "kubeagents-system")
	t.Setenv(envAuthMapName, "agent-a2a-authmap")
	t.Setenv(envAudience, "kube-agents-bus")
	t.Setenv(envIssuerSeed, string(seed))
}

// The callout refuses to start without the static principal names: missing,
// empty, or malformed. Each refusal names the variable, so the log says why
// the pod is crash-looping.
func TestTheCalloutRefusesToStartWithoutReservedPrincipals(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	cases := map[string]*string{
		"missing":   nil,
		"empty":     ptr(""),
		"malformed": ptr("gateway,,web"),
	}
	for name, value := range cases {
		t.Run(name, func(t *testing.T) {
			setRequiredEnv(t)
			t.Setenv("KUBERNETES_SERVICE_HOST", "")
			if value == nil {
				// t.Setenv first so the original value is restored after.
				t.Setenv(envReservedPrincipals, "")
				if err := os.Unsetenv(envReservedPrincipals); err != nil {
					t.Fatalf("Unsetenv: %v", err)
				}
			} else {
				t.Setenv(envReservedPrincipals, *value)
			}
			err := run(log)
			if err == nil || !strings.Contains(err.Error(), envReservedPrincipals) {
				t.Fatalf("run() = %v, want a refusal naming %s", err, envReservedPrincipals)
			}
		})
	}
}

// With the list present the guard lets startup carry on, to the in-cluster
// config a test process does not have. That is the control: the refusals above
// are the list, not an earlier variable this test forgot to set.
//
// The service host is blanked so the in-cluster config fails even when the
// test itself runs in a pod; otherwise run would go on to serve and watch.
func TestTheCalloutStartsPastTheGuardWithReservedPrincipals(t *testing.T) {
	setRequiredEnv(t)
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	t.Setenv(envReservedPrincipals, "callout,gateway,bridge,seed,web,console,sys")
	// run reads the addressee list next; set so the control reaches the
	// in-cluster config.
	t.Setenv(envReservedAddressees, "platform")
	err := run(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err == nil {
		t.Fatal("run() succeeded outside a cluster")
	}
	if strings.Contains(err.Error(), envReservedPrincipals) {
		t.Fatalf("a well-formed list was refused: %v", err)
	}
	if !strings.Contains(err.Error(), "in-cluster config") {
		t.Fatalf("run() stopped somewhere other than the in-cluster config: %v", err)
	}
}

func ptr(s string) *string { return &s }
