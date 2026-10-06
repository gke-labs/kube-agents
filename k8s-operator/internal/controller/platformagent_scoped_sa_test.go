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
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apiextensions-apiserver/pkg/apis/apiextensions"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	structuralschema "k8s.io/apiextensions-apiserver/pkg/apiserver/schema"
	schemacel "k8s.io/apiextensions-apiserver/pkg/apiserver/schema/cel"
	"k8s.io/apimachinery/pkg/util/validation/field"
	celconfig "k8s.io/apiserver/pkg/apis/cel"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The exit criterion this file exists for is that the project-to-account
// mapping is visible in a rendered manifest rather than inferred from the
// broker's behaviour. So these assertions are about what an operator can read
// off `kubectl get configmap` and `kubectl get deployment`, not about internal
// helpers.

func scopedAgent(enabled bool, accounts ...agentv1alpha1.ScopedServiceAccount) *agentv1alpha1.PlatformAgent {
	agent := brokerPodAgent()
	if agent.Spec.Security == nil {
		agent.Spec.Security = &agentv1alpha1.SecuritySpec{}
	}
	agent.Spec.Security.ScopedServiceAccountPool = &agentv1alpha1.ScopedServiceAccountPoolSpec{
		Enabled:         enabled,
		ServiceAccounts: accounts,
	}
	return agent
}

func account(project, email string) agentv1alpha1.ScopedServiceAccount {
	return agentv1alpha1.ScopedServiceAccount{
		ProjectID:           project,
		ServiceAccountEmail: email,
	}
}

type renderedPool struct {
	Version         int `json:"version"`
	ServiceAccounts []struct {
		ProjectID           string `json:"projectId"`
		ServiceAccountEmail string `json:"serviceAccountEmail"`
	} `json:"serviceAccounts"`
}

// Counts as well as reads, unlike the (string, bool) envValue beside it: the
// plugin-override case is specifically about a second copy of a variable being
// appended, and a helper returning the first match would report success on
// exactly the input that matters.
func envValueCount(envVars []corev1.EnvVar, name string) (string, int) {
	value, count := "", 0
	for _, env := range envVars {
		if env.Name == name {
			value, count = env.Value, count+1
		}
	}
	return value, count
}

func TestTheMappingIsRenderedIntoAConfigMapTheBrokerCanRead(t *testing.T) {
	agent := scopedAgent(true,
		account("proj", "ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com"),
	)

	cm := buildCredentialProxyPolicyConfigMap(agent)
	raw, ok := cm.Data[scopedSAPoolKey]
	if !ok {
		t.Fatalf("no %s key in the rendered ConfigMap; keys were %v", scopedSAPoolKey, cm.Data)
	}

	var pool renderedPool
	if err := json.Unmarshal([]byte(raw), &pool); err != nil {
		t.Fatalf("the rendered mapping is not the JSON the broker parses: %v (%s)", err, raw)
	}
	// Version 2 is the per-project key. The broker refuses version 1, whose
	// entries carried a cluster tuple, so a stale render is a crashloop naming
	// the version rather than a silently wrong lookup.
	if pool.Version != 2 {
		t.Errorf("version = %d, want 2", pool.Version)
	}
	if len(pool.ServiceAccounts) != 1 {
		t.Fatalf("got %d accounts, want 1", len(pool.ServiceAccounts))
	}
	entry := pool.ServiceAccounts[0]
	if entry.ProjectID != "proj" {
		t.Errorf("the project id did not survive rendering: %+v", entry)
	}
	if entry.ServiceAccountEmail != "ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com" {
		t.Errorf("serviceAccountEmail = %q", entry.ServiceAccountEmail)
	}
	// Byte-for-byte: the broker's parser and the Terraform composition agree
	// on this document, and a key the Go struct happened to add or rename
	// would pass every field check above.
	want := `{"version":2,"serviceAccounts":[{"projectId":"proj","serviceAccountEmail":"ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com"}]}`
	if raw != want {
		t.Errorf("rendered pool =\n %s\nwant\n %s", raw, want)
	}
}

func TestTheMappingRendersInAStableOrder(t *testing.T) {
	// The ConfigMap is hashed into the Pod template annotation, so an unstable
	// render would roll the broker every reconcile — and because the broker
	// reads this file only at startup, a rollout loop here is not cosmetic.
	forward := scopedAgent(true,
		account("a-proj", "ka-a-proj-11111111@host.iam.gserviceaccount.com"),
		account("b-proj", "ka-b-proj-22222222@host.iam.gserviceaccount.com"),
		account("a-proj-2", "ka-a-proj-2-33333333@host.iam.gserviceaccount.com"),
	)
	reversed := scopedAgent(true,
		forward.Spec.Security.ScopedServiceAccountPool.ServiceAccounts[2],
		forward.Spec.Security.ScopedServiceAccountPool.ServiceAccounts[1],
		forward.Spec.Security.ScopedServiceAccountPool.ServiceAccounts[0],
	)

	first := buildCredentialProxyPolicyConfigMap(forward).Data[scopedSAPoolKey]
	second := buildCredentialProxyPolicyConfigMap(reversed).Data[scopedSAPoolKey]
	if first != second {
		t.Errorf("reordering the CR changed the rendered mapping:\n %s\n %s", first, second)
	}

	var pool renderedPool
	if err := json.Unmarshal([]byte(first), &pool); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// Byte order on the project id: "a-proj" < "a-proj-2" < "b-proj".
	want := []string{"a-proj", "a-proj-2", "b-proj"}
	for i, project := range want {
		if pool.ServiceAccounts[i].ProjectID != project {
			t.Errorf("entry %d is %q, want %q (sorted by projectId, byte order)",
				i, pool.ServiceAccounts[i].ProjectID, project)
		}
	}
}

func TestWithThePoolAbsentThereIsNoMappingAndTheFlagSaysSo(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{}

	if _, ok := buildCredentialProxyPolicyConfigMap(agent).Data[scopedSAPoolKey]; ok {
		t.Errorf("a mapping was rendered for an agent that configured none")
	}

	envVars := buildCredentialProxyEnv(agent)
	// Explicitly "0", not absent. The broker's own default is also off, so this
	// changes no behaviour — what it buys is that the credential mode an install
	// is in can be read off the Deployment instead of inferred from an absence.
	if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "0" || count != 1 {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL = %q (x%d), want exactly one \"0\"", value, count)
	}
	if _, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE"); count != 0 {
		t.Errorf("a pool file was named for an agent with no pool")
	}

	for _, mount := range buildCredentialProxyContainer(agent).VolumeMounts {
		if mount.SubPath == scopedSAPoolKey {
			t.Errorf("the pool is mounted by SubPath but the ConfigMap has no such key; the container cannot start")
		}
	}
}

// TestADisabledPoolWithMembersRendersNothing is the arming rule: `enabled`
// arms the broker, the list does not. The composition writes the list from
// Terraform's output whether or not the pool is on, so a list that armed the
// broker by being non-empty would arm it on every install that provisioned an
// account, and the explicit switch would be decoration.
func TestADisabledPoolWithMembersRendersNothing(t *testing.T) {
	agent := scopedAgent(false,
		account("proj", "ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com"),
	)

	if raw, ok := buildCredentialProxyPolicyConfigMap(agent).Data[scopedSAPoolKey]; ok {
		t.Errorf("a disabled pool rendered a mapping: %s", raw)
	}

	envVars := buildCredentialProxyEnv(agent)
	if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "0" || count != 1 {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL = %q (x%d), want exactly one \"0\"", value, count)
	}
	if _, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE"); count != 0 {
		t.Errorf("a pool file was named for an agent whose pool is disabled")
	}

	for _, mount := range buildCredentialProxyContainer(agent).VolumeMounts {
		if mount.SubPath == scopedSAPoolKey {
			t.Errorf("a disabled pool is mounted by SubPath but the ConfigMap has no such key; the container cannot start")
		}
	}
}

func TestWithThePoolEnabledTheFlagAndTheMountAppearTogether(t *testing.T) {
	agent := scopedAgent(true,
		account("proj", "ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com"),
	)

	envVars := buildCredentialProxyEnv(agent)
	if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "1" || count != 1 {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL = %q (x%d), want exactly one \"1\"", value, count)
	}
	path, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE")
	if count != 1 || path != scopedSAPoolMountPath {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE = %q (x%d), want %q", path, count, scopedSAPoolMountPath)
	}

	mounts := buildCredentialProxyContainer(agent).VolumeMounts
	var mounted *corev1.VolumeMount
	for i := range mounts {
		if mounts[i].MountPath == path {
			mounted = &mounts[i]
		}
	}
	if mounted == nil {
		t.Fatalf("the broker is told to read %s and nothing mounts it there", path)
	}
	if mounted.SubPath != scopedSAPoolKey || mounted.Name != "credential-proxy-policy" {
		t.Errorf("mount = %+v, want SubPath %q on the policy ConfigMap volume", *mounted, scopedSAPoolKey)
	}
	if !mounted.ReadOnly {
		t.Errorf("the mapping is mounted writable; it is the list of identities the broker may become")
	}
}

func TestAPluginCannotDisableTheScopedServiceAccountPool(t *testing.T) {
	// Same argument as TestAPluginCannotDisableCallerAuthentication. A plugin
	// that could set these would put the broker back on the agent's own
	// project-wide identity, or point it at a mapping naming an account it
	// would rather be — either of which is the whole control, switched off by
	// a field further down the same CR.
	agent := scopedAgent(true,
		account("proj", "ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com"),
	)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL", Value: "0"},
		{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE", Value: "/tmp/mine.json"},
	}}

	envVars := buildCredentialProxyEnv(agent)
	for name, want := range map[string]string{
		"CREDENTIAL_PROXY_SCOPED_SA_POOL":      "1",
		"CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE": scopedSAPoolMountPath,
	} {
		value, count := envValueCount(envVars, name)
		if count != 1 || value != want {
			t.Errorf("%s = %q (x%d), want exactly one %q", name, value, count, want)
		}
	}
}

// TestARepeatedProjectIsRejectedAtAdmission pins the marker, not the prose.
//
// The broker refuses to start on a mapping that resolves one project to two
// accounts, which is the right call -- resolving it by last-wins would make the
// account a request gets depend on the order the operator happened to type. But
// refusing at startup means the operator sees a crashloop, several layers away
// from the copy-pasted CR entry that caused it, and the CR itself applied
// cleanly.
//
// `x-kubernetes-list-type: map` keyed on projectId moves that to `kubectl
// apply`. Asserted against the generated CRD rather than against the Go marker
// comment, because the marker is only worth anything once controller-gen has
// turned it into schema -- a typo'd marker is a comment.
func TestARepeatedProjectIsRejectedAtAdmission(t *testing.T) {
	crd, err := os.ReadFile(filepath.Join(
		"..", "..", "config", "crd", "bases", "kubeagents.x-k8s.io_platformagents.yaml",
	))
	if err != nil {
		t.Fatalf("reading the generated CRD: %v", err)
	}

	// The field appears once per served version; every one of them has to carry
	// the keys, so the count is checked rather than the first hit.
	blocks := regexp.MustCompile(
		`(?s)scopedServiceAccountPool:.*?serviceAccounts:.*?\n(\s+)x-kubernetes-list-type: (\w+)`,
	).FindAllStringSubmatch(string(crd), -1)
	if len(blocks) == 0 {
		t.Fatal("scopedServiceAccountPool.serviceAccounts carries no x-kubernetes-list-type; a repeated " +
			"project is admitted and the broker crashloops on it")
	}
	for _, block := range blocks {
		if block[2] != "map" {
			t.Errorf("x-kubernetes-list-type is %q, want \"map\"", block[2])
		}
	}

	keysRE := regexp.MustCompile(
		`(?s)scopedServiceAccountPool:.*?serviceAccounts:.*?x-kubernetes-list-map-keys:(.*?)x-kubernetes-list-type`,
	)
	match := keysRE.FindStringSubmatch(string(crd))
	if match == nil {
		t.Fatal("scopedServiceAccountPool.serviceAccounts declares no list-map keys")
	}
	keys := match[1]
	if !strings.Contains(keys, "- projectId") {
		t.Error("projectId is not a list-map key, so one project may appear twice and the broker sees a duplicate")
	}
	// serviceAccountEmail must NOT be a key: keyed on it too, one project
	// pointed at two accounts is a legal list again, which is the case the
	// broker refuses. Neither may the retired cluster fields: a key the entry
	// no longer carries would make every entry equal and the list a singleton.
	for _, retired := range []string{"serviceAccountEmail", "location", "clusterName"} {
		if strings.Contains(keys, "- "+retired) {
			t.Errorf("%s is a list-map key; the pool is keyed on the project alone", retired)
		}
	}
}

// TestAnArmedEmptyPoolIsRejectedAtAdmission pins the CEL rule. The broker
// refuses to start on an empty pool, so `enabled: true` with no members is a
// crashloop; the rule turns it into a `kubectl apply` error naming the field.
func TestAnArmedEmptyPoolIsRejectedAtAdmission(t *testing.T) {
	crd, err := os.ReadFile(filepath.Join(
		"..", "..", "config", "crd", "bases", "kubeagents.x-k8s.io_platformagents.yaml",
	))
	if err != nil {
		t.Fatalf("reading the generated CRD: %v", err)
	}
	block := regexp.MustCompile(
		`(?s)scopedServiceAccountPool:.*?x-kubernetes-validations:(.*?)\n\s+serviceAccountAnnotations:`,
	).FindStringSubmatch(string(crd))
	if block == nil {
		t.Fatal("scopedServiceAccountPool carries no x-kubernetes-validations; an armed empty pool is admitted and the broker crashloops")
	}
	// prettier folds the long rule and message across lines; the schema the
	// API server loads is the unfolded string, so compare that.
	rules := strings.Join(strings.Fields(block[1]), " ")
	if !strings.Contains(rules, "!has(self.enabled) || !self.enabled ||") || !strings.Contains(rules, "size(self.serviceAccounts) > 0") {
		t.Errorf("the validation does not tie enabled to a non-empty list:\n%s", rules)
	}
	if !strings.Contains(rules, "scopedServiceAccountPool.enabled requires at least one serviceAccounts entry") {
		t.Errorf("the refusal does not name the field and the fix:\n%s", rules)
	}
}

// TestThePoolAdmissionRuleEvaluatesAsDocumented runs the pool's CEL rule
// through the same validator the API server uses, over every shape a CR can
// carry the block in. The substring test above pins the rule's text; this one
// pins what the rule does, which is the part that went wrong: this validator
// applies no defaults, so a block written without the key — the shape an
// install that provisions the mapping before arming the pool produces — has
// no `enabled` field for CEL to read, and an unguarded `!self.enabled` fails
// with `no such key: enabled` under the "armed pool" message instead of
// admitting. On the API server the field's default makes the key present
// before CEL runs; the `has()` guard is what keeps the rule's meaning the same
// in both places.
func TestThePoolAdmissionRuleEvaluatesAsDocumented(t *testing.T) {
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
	pool := served.Properties["spec"].Properties["security"].Properties["scopedServiceAccountPool"]
	if len(pool.XValidations) == 0 {
		t.Fatal("scopedServiceAccountPool carries no x-kubernetes-validations")
	}
	var internal apiextensions.JSONSchemaProps
	if err := apiextensionsv1.Convert_v1_JSONSchemaProps_To_apiextensions_JSONSchemaProps(&pool, &internal, nil); err != nil {
		t.Fatalf("converting the pool schema: %v", err)
	}
	structural, err := structuralschema.NewStructural(&internal)
	if err != nil {
		t.Fatalf("the pool schema is not structural: %v", err)
	}
	validator := schemacel.NewValidator(structural, true, celconfig.PerCallLimit)
	if validator == nil {
		t.Fatal("the pool schema compiled to no CEL validator")
	}
	fldPath := field.NewPath("spec", "security", "scopedServiceAccountPool")

	member := map[string]interface{}{
		"projectId":           "example-project",
		"serviceAccountEmail": "ka-example-project-1a2b3c4d@example-project.iam.gserviceaccount.com",
	}
	const refusal = "requires at least one serviceAccounts entry"
	cases := []struct {
		name     string
		obj      map[string]interface{}
		admitted bool
	}{
		{"empty block", map[string]interface{}{}, true},
		{"mapping only, empty", map[string]interface{}{"serviceAccounts": []interface{}{}}, true},
		{"disarmed", map[string]interface{}{"enabled": false}, true},
		{"disarmed with mapping", map[string]interface{}{"enabled": false, "serviceAccounts": []interface{}{member}}, true},
		{"armed with mapping", map[string]interface{}{"enabled": true, "serviceAccounts": []interface{}{member}}, true},
		{"armed, no mapping", map[string]interface{}{"enabled": true}, false},
		{"armed, empty mapping", map[string]interface{}{"enabled": true, "serviceAccounts": []interface{}{}}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			errs, _ := validator.Validate(context.Background(), fldPath, structural, tc.obj, nil, celconfig.RuntimeCELCostBudget)
			if tc.admitted {
				if len(errs) != 0 {
					t.Errorf("admitted shape %v was refused: %v", tc.obj, errs.ToAggregate())
				}
				return
			}
			if len(errs) == 0 {
				t.Fatalf("refused shape %v was admitted", tc.obj)
			}
			if got := errs.ToAggregate().Error(); !strings.Contains(got, refusal) {
				t.Errorf("refusal does not name the fix: %s", got)
			}
		})
	}
}

// TestTheFlagTheKeyAndTheMountAgreeOnEveryShape is the invariant the tests
// above only spot-check at their own end of it.
//
// Three things have to move together on every render: the ConfigMap carries the
// key, the broker container is told to read that path, and something mounts the
// key there. Any two out of three is a broker that will not start, and the
// failure is a crashloop several layers from the CR field that caused it — the
// SubPath names a key the ConfigMap does not have, so the kubelet cannot
// populate the volume; or the flag is armed and `build_pool` finds no file.
//
// Written as a sweep over the shapes rather than as another single case,
// because what went wrong here once already was a builder that got two of the
// three right. Mutating any one of the three leaves the goldens and the
// per-property tests above green in at least one direction.
//
// "armed, empty" is the shape the CEL rule refuses at admission. It is swept
// anyway: a CR that predates the rule, or one applied with validation off,
// still reaches the builders, and the broker's own refusal of an empty pool
// is the diagnosable failure, where a SubPath naming a missing key is not.
func TestTheFlagTheKeyAndTheMountAgreeOnEveryShape(t *testing.T) {
	absent := brokerPodAgent()
	absent.Spec.Security = &agentv1alpha1.SecuritySpec{}
	shapes := map[string]*agentv1alpha1.PlatformAgent{
		"pool absent":      absent,
		"disabled, empty":  scopedAgent(false),
		"disabled, member": scopedAgent(false, account("proj", "ka-proj-1a2b3c4d@host.iam.gserviceaccount.com")),
		"armed, empty":     scopedAgent(true),
		"armed, one":       scopedAgent(true, account("proj", "ka-proj-1a2b3c4d@host.iam.gserviceaccount.com")),
		"armed, two":       scopedAgent(true, account("proj", "ka-proj-11111111@host.iam.gserviceaccount.com"), account("other", "ka-other-22222222@host.iam.gserviceaccount.com")),
		"plugin override": func() *agentv1alpha1.PlatformAgent {
			agent := scopedAgent(true, account("proj", "ka-proj-1a2b3c4d@host.iam.gserviceaccount.com"))
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: []corev1.EnvVar{
				{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL", Value: "0"},
			}}
			return agent
		}(),
	}
	for name, agent := range shapes {
		t.Run(name, func(t *testing.T) {
			_, keyPresent := buildCredentialProxyPolicyConfigMap(agent).Data[scopedSAPoolKey]

			flagValue, flagCount := envValueCount(buildCredentialProxyEnv(agent), "CREDENTIAL_PROXY_SCOPED_SA_POOL")
			if flagCount != 1 {
				t.Fatalf("CREDENTIAL_PROXY_SCOPED_SA_POOL appears %d times, want exactly 1", flagCount)
			}
			armed := flagValue == "1"

			mounted := false
			for _, mount := range buildCredentialProxyContainer(agent).VolumeMounts {
				if mount.SubPath == scopedSAPoolKey {
					mounted = true
				}
			}

			if armed != keyPresent {
				t.Errorf("flag armed=%v but the ConfigMap key present=%v; armed with no "+
					"mapping is a broker that refuses to start, and a mapping with the "+
					"flag off is a file nothing reads", armed, keyPresent)
			}
			if mounted != keyPresent {
				t.Errorf("SubPath mount present=%v but the ConfigMap key present=%v; a "+
					"SubPath naming a key the ConfigMap does not carry stops the "+
					"container starting", mounted, keyPresent)
			}
		})
	}
}

func TestTheReservedListNamesThePoolVariablesInItsOwnRight(t *testing.T) {
	// Not a duplicate of the test above, and the difference is the whole point.
	//
	// `mergeCredentialProxyEnv` reserves every name already in `managed`, and
	// the operator puts both pool variables there, so the end-to-end assertion
	// passes with the two names deleted from the explicit list — measured, by
	// deleting them. That leaves the list entries looking load-bearing while
	// nothing checks them.
	//
	// They are worth keeping for the reason the read-only kill switch is listed
	// twice: `CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE` is only in `managed` when a
	// pool is configured, so on an install with no pool it is the explicit list
	// or nothing. This calls the merge with an empty managed list, which is the
	// only shape in which the entries are the thing under test.
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL", Value: "0"},
		{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE", Value: "/tmp/mine.json"},
		{Name: "HARMLESS_PLUGIN_SETTING", Value: "kept"},
	})
	for _, name := range []string{
		"CREDENTIAL_PROXY_SCOPED_SA_POOL",
		"CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE",
	} {
		if _, count := envValueCount(merged, name); count != 0 {
			t.Errorf("%s survived the merge from spec.deployment.env", name)
		}
	}
	if value, count := envValueCount(merged, "HARMLESS_PLUGIN_SETTING"); count != 1 || value != "kept" {
		t.Errorf("the merge dropped an unrelated plugin variable; the reserved list is too wide")
	}
}

// The A2A gateway's broker-side variables are reserved whether or not the
// operator renders them (it does, under next with Chat), so a CR cannot decide
// who holds the a2a-chat role or arm a second Chat consumer through
// spec.deployment.env. Called with an empty managed list for the same reason
// as the test above: on an install where the render does not set them, the
// explicit list is the only thing that reserves them.
func TestTheReservedListNamesTheA2AChatVariables(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE", Value: "kubeagents-credential-proxy"},
		{Name: "A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME", Value: "projects/p/subscriptions/theirs"},
		// The legacy name too: under next with Chat the render no longer
		// sets it, so the managed-names loop stops protecting it, and a CR
		// that set it would arm a second relay instance on whatever the
		// broker's credential can pull, or refuse the broker's start.
		{Name: "GOOGLE_CHAT_SUBSCRIPTION_NAME", Value: "projects/p/subscriptions/theirs"},
		{Name: "CREDENTIAL_PROXY_SESSION_AUDIENCE", Value: "kubeagents-credential-proxy"},
		{Name: "CREDENTIAL_PROXY_SESSION_CALLERS", Value: "system:serviceaccount:ns:theirs"},
		{Name: "HARMLESS_PLUGIN_SETTING", Value: "kept"},
	})
	for _, name := range []string{
		"CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE",
		"A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME",
		"GOOGLE_CHAT_SUBSCRIPTION_NAME",
		"CREDENTIAL_PROXY_SESSION_AUDIENCE",
		"CREDENTIAL_PROXY_SESSION_CALLERS",
	} {
		if _, count := envValueCount(merged, name); count != 0 {
			t.Errorf("%s survived the merge from spec.deployment.env", name)
		}
	}
	if value, count := envValueCount(merged, "HARMLESS_PLUGIN_SETTING"); count != 1 || value != "kept" {
		t.Errorf("the merge dropped an unrelated plugin variable; the reserved list is too wide")
	}
}

func TestTheMappingReachesTheRenderedBrokerPod(t *testing.T) {
	// The tests above check the container builder. This one checks the Pod it
	// ends up in: a SubPath mount is only satisfiable if the Pod also declares
	// the volume it names, and the two are assembled by different functions.
	agent := scopedAgent(true, account("proj", "ka-proj-1a2b3c4d@host-proj.iam.gserviceaccount.com"))

	if value, _ := envValueCount(buildCredentialProxyEnv(agent), "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "1" {
		t.Errorf("the broker does not arm the pool: %q", value)
	}

	pod := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec
	var mount *corev1.VolumeMount
	for i := range pod.Containers[0].VolumeMounts {
		if pod.Containers[0].VolumeMounts[i].SubPath == scopedSAPoolKey {
			mount = &pod.Containers[0].VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("the broker is armed with no mapping mounted; it will refuse to start")
	}
	for _, volume := range pod.Volumes {
		if volume.Name == mount.Name {
			return
		}
	}
	t.Errorf("the mount names volume %q and the Pod declares no such volume", mount.Name)
}
