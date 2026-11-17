package testing

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"sigs.k8s.io/yaml"
)

// A fresh install renders the gateway's pod template it settles on, so the
// agent Deployment rolls once (#5962). PlatformAgent and PlatformAgentSettled
// render the same CR (examples/platformagent.yaml): the first after one pass,
// with the bus not yet provisioned, the second reconciled until settled. The
// operator used to add the bridge sidecar, and the agent's activity-hook key,
// only once BusProvisioned was True, so the two templates differed and every
// fresh install rolled the gateway a second time, a pass after the
// provisioning Job. TestAgentsGolden holds each render to its golden; this
// holds the two goldens' gateway templates equal.
func TestAFreshInstallRendersTheGatewayItSettlesOn(t *testing.T) {
	first := gatewayTemplate(t, filepath.Join("testdata", "platform", "expected", "platformagent.yaml"))
	settled := gatewayTemplate(t, filepath.Join("testdata", "platform", "expected", "platformagent-settled.yaml"))
	if !reflect.DeepEqual(first, settled) {
		a, _ := yaml.Marshal(first)
		b, _ := yaml.Marshal(settled)
		t.Fatalf("the gateway's pod template changes between the first pass and the settled render, so a fresh install rolls it twice\nfirst pass:\n%s\nsettled:\n%s", a, b)
	}
}

// gatewayTemplate is spec.template of the platformagent-gateway Deployment
// in a golden file.
func gatewayTemplate(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, doc := range bytes.Split(data, []byte("\n---\n")) {
		var obj map[string]any
		if err := yaml.Unmarshal(doc, &obj); err != nil || obj == nil {
			continue
		}
		meta, _ := obj["metadata"].(map[string]any)
		if obj["kind"] != "Deployment" || meta["name"] != "platformagent-gateway" {
			continue
		}
		spec, _ := obj["spec"].(map[string]any)
		tmpl, _ := spec["template"].(map[string]any)
		if tmpl == nil {
			t.Fatalf("%s: the gateway Deployment has no spec.template", path)
		}
		return tmpl
	}
	t.Fatalf("%s: no platformagent-gateway Deployment", path)
	return nil
}
