package controller

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The spec.tools schema is enforced by the API server, not by Go: this runs the
// regenerated CRD in envtest and requires it to admit a valid list and refuse a
// name over 24 characters, a duplicate name, and a 17th entry.
func TestAgentProfileToolsSchemaEnvtest(t *testing.T) {
	cl, _ := startEnvtest(t)
	ctx := context.Background()
	const ns = "agentprofile-tools"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	tool := func(name string) agentv1alpha1.AgentProfileTool {
		return agentv1alpha1.AgentProfileTool{Name: name, Image: "registry.example/" + name + ":1"}
	}
	create := func(name string, tools ...agentv1alpha1.AgentProfileTool) error {
		p := testAgentProfile(ns, name, func(p *agentv1alpha1.AgentProfile) { p.Spec.Tools = tools })
		return cl.Create(ctx, &p)
	}

	if err := create("valid", tool("jq"), tool(strings.Repeat("a", 24))); err != nil {
		t.Fatalf("the API server refused a valid tools list: %v", err)
	}
	if err := create("too-long", tool(strings.Repeat("a", 25))); err == nil {
		t.Error("the API server admitted a 25-character tool name; staging container names would overflow Autopilot's 35")
	}
	if err := create("duplicate", tool("jq"), tool("jq")); err == nil {
		t.Error("the API server admitted a duplicate tool name; two volumes would share one mount path")
	}
	many := make([]agentv1alpha1.AgentProfileTool, 17)
	for i := range many {
		many[i] = tool("t" + strings.Repeat("x", i))
	}
	if err := create("too-many", many...); err == nil {
		t.Error("the API server admitted 17 tools; the CRD caps the list at 16")
	}
	if err := create("bad-name", tool("Not_A_Label")); err == nil {
		t.Error("the API server admitted a tool name that is not a DNS-1123 label")
	}
}
