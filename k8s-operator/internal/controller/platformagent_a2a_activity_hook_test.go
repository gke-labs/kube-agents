/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
	k8syaml "sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The pod-wide activity hook and its signing key render together, and only
// on a next install that declares a bridge sidecar: a today install, or a
// next install with no bridge to post to, renders exactly what it did before.
func TestActivityHookRendersOnlyWithABridgeOnNext(t *testing.T) {
	bridge := []corev1.Container{{Name: "hermes-bridge", Image: "bridge:dev", Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: "2"}}}}
	other := []corev1.Container{{Name: "log-shipper", Image: "shipper:dev"}}
	cases := []struct {
		name     string
		mode     *string
		sidecars []corev1.Container
		want     bool
	}{
		{"today with a bridge", nil, bridge, false},
		{"next without sidecars", ptr.To("next"), nil, false},
		{"next with another sidecar", ptr.To("next"), other, false},
		{"next with a bridge", ptr.To("next"), bridge, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			agent.Spec.Mode = tc.mode
			if tc.sidecars != nil {
				agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: tc.sidecars}
			}

			var cfg struct {
				Hooks *struct {
					Outbound []map[string]any `json:"outbound"`
				} `json:"hooks"`
			}
			if err := k8syaml.Unmarshal([]byte(renderConfigYAML(agent, nil)), &cfg); err != nil {
				t.Fatalf("config.yaml: %v", err)
			}
			dep := buildDeployment(agent, "", "", "", "", nil, renderOptions{})
			agentC := brokerContainerNamed(dep.Spec.Template.Spec.Containers, "platform-agent")
			if agentC == nil {
				t.Fatal("no platform-agent container")
			}
			var env *corev1.EnvVar
			for i := range agentC.Env {
				if agentC.Env[i].Name == a2aActivitySecretEnvVar {
					env = &agentC.Env[i]
				}
			}

			if !tc.want {
				if cfg.Hooks != nil || env != nil {
					t.Fatalf("rendered hooks=%v env=%v, want neither", cfg.Hooks, env)
				}
				return
			}
			if cfg.Hooks == nil || len(cfg.Hooks.Outbound) != 1 {
				t.Fatalf("hooks = %+v, want one outbound entry", cfg.Hooks)
			}
			wantEntry := map[string]any{
				"name":       "a2a-bridge-activity",
				"url":        "http://127.0.0.1:8651/hermes/tool-events",
				"events":     []any{"pre_tool_call", "post_tool_call"},
				"secret_env": "A2A_ACTIVITY_SECRET",
				"timeout":    float64(10),
			}
			if got := cfg.Hooks.Outbound[0]; !reflect.DeepEqual(got, wantEntry) {
				t.Fatalf("hook entry = %#v, want %#v", got, wantEntry)
			}
			ref := env.ValueFrom
			if env.Value != "" || ref == nil || ref.SecretKeyRef == nil ||
				ref.SecretKeyRef.Name != "test-agent-a2a-nats-creds" || ref.SecretKeyRef.Key != "bridge-activity-key" ||
				ref.SecretKeyRef.Optional == nil || !*ref.SecretKeyRef.Optional {
				t.Fatalf("signing key env = %+v, want an optional ref to the creds Secret's bridge-activity-key", env)
			}
		})
	}
}

// The creds Secret carries the signing key, so an install created before the
// key existed gets it filled on its next reconcile like any missing key.
func TestCredsKeysIncludeTheActivityKey(t *testing.T) {
	for _, k := range a2aCredsKeys {
		if k == "bridge-activity-key" {
			return
		}
	}
	t.Fatalf("a2aCredsKeys = %v, missing bridge-activity-key", a2aCredsKeys)
}
