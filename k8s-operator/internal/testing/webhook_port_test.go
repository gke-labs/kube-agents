package testing

import (
	"bufio"
	"bytes"
	"io"
	"os"
	"path/filepath"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	"sigs.k8s.io/yaml"

	agentwebhook "github.com/gke-labs/kube-agents/k8s-operator/internal/webhook"
)

// TestWebhookPortsMatchDefault pins the manager's container port and the webhook Service's
// targetPort to agentwebhook.DefaultPort.
//
// The port is deliberately 10250 so GKE's automatic control-plane-to-node firewall rule
// reaches it (see DefaultPort). If any of the three copies drifts, the API server dials a
// port nothing is listening on, and because both webhook configurations use
// failurePolicy=Fail, every PlatformAgent create, update, and delete starts failing with a
// timeout — the outage this port choice exists to prevent.
func TestWebhookPortsMatchDefault(t *testing.T) {
	t.Parallel()

	t.Run("manager containerPort", func(t *testing.T) {
		t.Parallel()

		var deployment appsv1.Deployment
		decodeManifest(t, filepath.Join("..", "..", "config", "manager", "manager.yaml"), "Deployment", &deployment)

		port, ok := findContainerPort(deployment, "webhook-server")
		if !ok {
			t.Fatal("no container port named \"webhook-server\" in config/manager/manager.yaml")
		}
		if port != int32(agentwebhook.DefaultPort) {
			t.Errorf("manager containerPort = %d, want %d (agentwebhook.DefaultPort)", port, agentwebhook.DefaultPort)
		}
	})

	t.Run("service targetPort", func(t *testing.T) {
		t.Parallel()

		var service corev1.Service
		decodeManifest(t, filepath.Join("..", "..", "config", "webhook", "service.yaml"), "Service", &service)

		if len(service.Spec.Ports) != 1 {
			t.Fatalf("config/webhook/service.yaml has %d ports, want exactly 1", len(service.Spec.Ports))
		}
		if got := service.Spec.Ports[0].TargetPort.IntValue(); got != agentwebhook.DefaultPort {
			t.Errorf("webhook Service targetPort = %d, want %d (agentwebhook.DefaultPort)", got, agentwebhook.DefaultPort)
		}
		// The *WebhookConfiguration clientConfig resolves to the Service port, so 443 is
		// what the API server addresses even though targetPort is what crosses the VPC.
		if got := service.Spec.Ports[0].Port; got != 443 {
			t.Errorf("webhook Service port = %d, want 443", got)
		}
	})
}

// decodeManifest reads a possibly multi-document manifest and decodes the first document of
// the given kind into out.
func decodeManifest(t *testing.T, path, kind string, out any) {
	t.Helper()

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}

	reader := utilyaml.NewYAMLReader(bufio.NewReader(bytes.NewReader(raw)))
	for {
		doc, err := reader.Read()
		if err == io.EOF {
			break
		} else if err != nil {
			t.Fatalf("splitting %s into documents: %v", path, err)
		}

		var probe struct {
			Kind string `json:"kind"`
		}
		if err := yaml.Unmarshal(doc, &probe); err != nil {
			t.Fatalf("decoding %s: %v", path, err)
		}
		if probe.Kind != kind {
			continue
		}

		if err := yaml.Unmarshal(doc, out); err != nil {
			t.Fatalf("unmarshalling %s from %s: %v", kind, path, err)
		}
		return
	}

	t.Fatalf("no %s document found in %s", kind, path)
}

func findContainerPort(deployment appsv1.Deployment, name string) (int32, bool) {
	for _, container := range deployment.Spec.Template.Spec.Containers {
		for _, port := range container.Ports {
			if port.Name == name {
				return port.ContainerPort, true
			}
		}
	}
	return 0, false
}
