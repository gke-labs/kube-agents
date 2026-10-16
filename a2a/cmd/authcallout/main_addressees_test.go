package main

import (
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"

	"github.com/nats-io/nkeys"
)

// setEnvBeforeAddressees sets every variable run reads before the reserved
// addressee list, the reserved principal list included, so the addressee list
// is the first thing that can refuse.
func setEnvBeforeAddressees(t *testing.T) {
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
	t.Setenv(envReservedPrincipals, "callout,gateway,bridge,seed,web,console,sys")
	// Blanked so the in-cluster config fails even when the test itself runs
	// in a pod; otherwise run would go on to serve and watch.
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
}

// The callout refuses to start without the reserved addressees: missing, empty,
// or malformed. Each refusal names the variable, so the log says why the pod is
// crash-looping.
func TestTheCalloutRefusesToStartWithoutReservedAddressees(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	malformed, empty := "platform,,chat", ""
	for name, value := range map[string]*string{
		"missing":   nil,
		"empty":     &empty,
		"malformed": &malformed,
	} {
		t.Run(name, func(t *testing.T) {
			setEnvBeforeAddressees(t)
			if value == nil {
				// t.Setenv first so the original value is restored after.
				t.Setenv(envReservedAddressees, "")
				if err := os.Unsetenv(envReservedAddressees); err != nil {
					t.Fatalf("Unsetenv: %v", err)
				}
			} else {
				t.Setenv(envReservedAddressees, *value)
			}
			err := run(log)
			if err == nil || !strings.Contains(err.Error(), envReservedAddressees) {
				t.Fatalf("run() = %v, want a refusal naming %s", err, envReservedAddressees)
			}
		})
	}
}

// With the list present the guard lets startup carry on, to the in-cluster
// config a test process does not have. That is the control: the refusals above
// are the list, not an earlier variable this test forgot to set.
func TestTheCalloutStartsPastTheGuardWithReservedAddressees(t *testing.T) {
	setEnvBeforeAddressees(t)
	t.Setenv(envReservedAddressees, "platform")
	err := run(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err == nil {
		t.Fatal("run() succeeded outside a cluster")
	}
	if strings.Contains(err.Error(), envReservedAddressees) {
		t.Fatalf("a well-formed list was refused: %v", err)
	}
	if !strings.Contains(err.Error(), "in-cluster config") {
		t.Fatalf("run() stopped somewhere other than the in-cluster config: %v", err)
	}
}
