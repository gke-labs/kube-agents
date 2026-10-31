package controller

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"k8s.io/apiextensions-apiserver/pkg/apis/apiextensions"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	structuralschema "k8s.io/apiextensions-apiserver/pkg/apiserver/schema"
	schemacel "k8s.io/apiextensions-apiserver/pkg/apiserver/schema/cel"
	apiservervalidation "k8s.io/apiextensions-apiserver/pkg/apiserver/validation"
	"k8s.io/apimachinery/pkg/util/validation/field"
	celconfig "k8s.io/apiserver/pkg/apis/cel"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// managedEnvValue returns the value the managed .env pins for key, and whether
// the key is there at all.
func managedEnvValue(env, key string) (string, bool) {
	for _, line := range strings.Split(strings.TrimSpace(env), "\n") {
		if k, v, ok := strings.Cut(line, "="); ok && k == key {
			return v, true
		}
	}
	return "", false
}

// The stall watch's knobs are pinned on every install, empty when the CR does not set
// them: a key the managed .env leaves out is a key the agent can write into its own
// .env, and each of these narrows what the watch alerts on.
func TestStallWatchKnobsArePinnedEmptyWithoutTheBlock(t *testing.T) {
	noHarness := newTestPlatformAgent()
	noBlock := newTestPlatformAgent()
	noBlock.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	emptyBlock := newTestPlatformAgent()
	emptyBlock.Spec.Harness = &agentv1alpha1.HarnessSpec{StallWatch: &agentv1alpha1.StallWatchSpec{}}
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"no harness":  noHarness,
		"no block":    noBlock,
		"empty block": emptyBlock,
	} {
		t.Run(name, func(t *testing.T) {
			env := renderManagedEnv(agent)
			for _, key := range []string{stallWatchExcludeNamespacesEnvKey, stallWatchMinAgeEnvKey, stallWatchKindsEnvKey} {
				got, found := managedEnvValue(env, key)
				if !found {
					t.Errorf("%s is not pinned, so the agent can write it into its own .env:\n%s", key, env)
				}
				if got != "" {
					t.Errorf("%s = %q with no value on the CR, want empty", key, got)
				}
			}
		})
	}
}

func TestStallWatchKnobsArePinnedFromTheBlock(t *testing.T) {
	minAge := int32(45)
	agent := newTestPlatformAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{StallWatch: &agentv1alpha1.StallWatchSpec{
		ExcludeNamespaces:  []string{"payments-canary", "load-test"},
		MinStallAgeMinutes: &minAge,
		Kinds:              []string{"deployments", "gateways.gateway.networking.k8s.io"},
	}}
	env := renderManagedEnv(agent)
	for key, want := range map[string]string{
		stallWatchExcludeNamespacesEnvKey: "payments-canary,load-test",
		stallWatchMinAgeEnvKey:            "45",
		stallWatchKindsEnvKey:             "deployments,gateways.gateway.networking.k8s.io",
	} {
		if got, _ := managedEnvValue(env, key); got != want {
			t.Errorf("%s = %q, want %q:\n%s", key, got, want, env)
		}
	}

	// Zero is a value, not an absence: it renders as 0, which the script reads the
	// same as empty.
	zero := int32(0)
	agent.Spec.Harness.StallWatch = &agentv1alpha1.StallWatchSpec{MinStallAgeMinutes: &zero}
	if got, _ := managedEnvValue(renderManagedEnv(agent), stallWatchMinAgeEnvKey); got != "0" {
		t.Errorf("%s = %q for minStallAgeMinutes: 0, want \"0\"", stallWatchMinAgeEnvKey, got)
	}

	// The knobs travel in the managed .env, which is in the hashed config ConfigMap, so
	// a change to them rolls the pod.
	if got := buildConfigMapData(agent, nil)[managedEnvKey]; got != renderManagedEnv(agent) {
		t.Errorf("the config ConfigMap does not carry the managed .env with the stall watch pins")
	}
}

// stallWatchSchema reads spec.harness.stallWatch from the generated CRD, the schema
// the API server enforces, rather than trusting the markers beside the Go fields.
func stallWatchSchema(t *testing.T) *apiextensions.JSONSchemaProps {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(
		"..", "..", "config", "crd", "bases", "kubeagents.x-k8s.io_platformagents.yaml",
	))
	if err != nil {
		t.Fatalf("reading the generated CRD: %v", err)
	}
	var crd apiextensionsv1.CustomResourceDefinition
	if err := yaml.Unmarshal(raw, &crd); err != nil {
		t.Fatalf("parsing the generated CRD: %v", err)
	}
	var served *apiextensionsv1.JSONSchemaProps
	for i := range crd.Spec.Versions {
		if crd.Spec.Versions[i].Served && crd.Spec.Versions[i].Schema != nil {
			served = crd.Spec.Versions[i].Schema.OpenAPIV3Schema
			break
		}
	}
	if served == nil {
		t.Fatal("the CRD serves no version with a schema")
	}
	block, ok := served.Properties["spec"].Properties["harness"].Properties["stallWatch"]
	if !ok {
		t.Fatal("the CRD has no spec.harness.stallWatch")
	}
	var internal apiextensions.JSONSchemaProps
	if err := apiextensionsv1.Convert_v1_JSONSchemaProps_To_apiextensions_JSONSchemaProps(&block, &internal, nil); err != nil {
		t.Fatalf("converting the stallWatch schema: %v", err)
	}
	return &internal
}

// TestStallWatchAdmissionAsDocumented runs the block's schema (patterns, bounds and
// the CEL rule on kinds) through the validators the API server uses, over the shapes
// the CRD reference describes.
func TestStallWatchAdmissionAsDocumented(t *testing.T) {
	schema := stallWatchSchema(t)
	openAPI, _, err := apiservervalidation.NewSchemaValidator(schema)
	if err != nil {
		t.Fatalf("building the OpenAPI validator: %v", err)
	}
	structural, err := structuralschema.NewStructural(schema)
	if err != nil {
		t.Fatalf("the stallWatch schema is not structural: %v", err)
	}
	cel := schemacel.NewValidator(structural, true, celconfig.PerCallLimit)
	if cel == nil {
		t.Fatal("the stallWatch schema compiled to no CEL validator")
	}
	fldPath := field.NewPath("spec", "harness", "stallWatch")

	many := func(n int, prefix string) []interface{} {
		out := make([]interface{}, n)
		for i := range out {
			out[i] = prefix + strconv.Itoa(i)
		}
		return out
	}
	cases := []struct {
		name     string
		obj      map[string]interface{}
		admitted bool
	}{
		{"empty block", map[string]interface{}{}, true},
		{"every field", map[string]interface{}{
			"excludeNamespaces":  []interface{}{"payments-canary", "load-test"},
			"minStallAgeMinutes": int64(45),
			"kinds":              []interface{}{"deployments", "gateways.gateway.networking.k8s.io", "certificates.cert-manager.io"},
		}, true},
		{"kinds all alone", map[string]interface{}{"kinds": []interface{}{"all"}}, true},
		{"kinds all with another", map[string]interface{}{"kinds": []interface{}{"all", "deployments"}}, false},
		{"kinds naming secrets", map[string]interface{}{"kinds": []interface{}{"deployments", "secrets"}}, false},
		{"kinds naming a configmap short name", map[string]interface{}{"kinds": []interface{}{"cm"}}, false},
		{"kinds naming bare leases", map[string]interface{}{"kinds": []interface{}{"leases"}}, false},
		{"kinds naming bare endpointslices", map[string]interface{}{"kinds": []interface{}{"endpointslices"}}, false},
		{"kinds naming bare controllerrevisions", map[string]interface{}{"kinds": []interface{}{"controllerrevisions"}}, false},
		{"kinds naming qualified leases", map[string]interface{}{"kinds": []interface{}{"leases.coordination.k8s.io"}}, false},
		{"kinds naming a group resource that only starts like one", map[string]interface{}{"kinds": []interface{}{"leasesets.example.com"}}, true},
		{"kinds upper case", map[string]interface{}{"kinds": []interface{}{"Deployments"}}, false},
		{"kinds with a name", map[string]interface{}{"kinds": []interface{}{"deployments/web"}}, false},
		{"kinds comma list in one item", map[string]interface{}{"kinds": []interface{}{"deployments,jobs"}}, false},
		{"kinds at the cap", map[string]interface{}{"kinds": many(20, "k")}, true},
		{"kinds past the cap", map[string]interface{}{"kinds": many(21, "k")}, false},
		{"namespace upper case", map[string]interface{}{"excludeNamespaces": []interface{}{"Payments"}}, false},
		{"namespace with a dot", map[string]interface{}{"excludeNamespaces": []interface{}{"payments.prod"}}, false},
		{"namespace glob", map[string]interface{}{"excludeNamespaces": []interface{}{"tenant-*"}}, false},
		{"namespace too long", map[string]interface{}{"excludeNamespaces": []interface{}{strings.Repeat("a", 64)}}, false},
		{"namespaces at the cap", map[string]interface{}{"excludeNamespaces": many(100, "n")}, true},
		{"namespaces past the cap", map[string]interface{}{"excludeNamespaces": many(101, "n")}, false},
		{"min age zero", map[string]interface{}{"minStallAgeMinutes": int64(0)}, true},
		{"min age one day", map[string]interface{}{"minStallAgeMinutes": int64(1440)}, true},
		{"min age negative", map[string]interface{}{"minStallAgeMinutes": int64(-1)}, false},
		{"min age past one day", map[string]interface{}{"minStallAgeMinutes": int64(1441)}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			errs := apiservervalidation.ValidateCustomResource(fldPath, tc.obj, openAPI)
			celErrs, _ := cel.Validate(context.Background(), fldPath, structural, tc.obj, nil, celconfig.RuntimeCELCostBudget)
			errs = append(errs, celErrs...)
			if tc.admitted && len(errs) != 0 {
				t.Errorf("admitted shape was refused: %v", errs.ToAggregate())
			}
			if !tc.admitted && len(errs) == 0 {
				t.Errorf("refused shape %v was admitted", tc.obj)
			}
		})
	}

	// The CEL rule's refusal names the fix.
	errs, _ := cel.Validate(context.Background(), fldPath, structural,
		map[string]interface{}{"kinds": []interface{}{"deployments", "all"}}, nil, celconfig.RuntimeCELCostBudget)
	if got := errs.ToAggregate(); got == nil || !strings.Contains(got.Error(), "all must be the only item") {
		t.Errorf("the kinds refusal does not say all must be the only item: %v", got)
	}
}
