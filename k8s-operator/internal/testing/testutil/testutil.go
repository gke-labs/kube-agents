package testutil

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"github.com/google/go-cmp/cmp"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/apiutil"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/yaml"
)

// reconcileIterations bounds a golden's reconcile loop; a settled run takes a
// pass per step of the bring-up it waits through (the callout serving, the
// provisioning Job completing, the bridge joining the agent pod), so it gets more.
const (
	reconcileIterations        = 5
	settledReconcileIterations = 12
)

// TrackingClient wraps a client.Client and intercepts all mutation operations
// (Create, Update, Patch) to collect modified resources in their insertion/generation order.
type TrackingClient struct {
	client.Client
	objects   []client.Object
	indices   map[string]int
	scheme    *runtime.Scheme
	mutated   bool
	ignoreKey string
}

func NewTrackingClient(inner client.Client, scheme *runtime.Scheme, ignoreObj client.Object) *TrackingClient {
	gvk, err := apiutil.GVKForObject(ignoreObj, scheme)
	var ignoreKey string
	if err == nil {
		ignoreKey = fmt.Sprintf("%s/%s/%s", gvk.Kind, ignoreObj.GetNamespace(), ignoreObj.GetName())
	}
	return &TrackingClient{
		Client:    inner,
		indices:   make(map[string]int),
		scheme:    scheme,
		ignoreKey: ignoreKey,
	}
}

func (c *TrackingClient) ResetMutated() {
	c.mutated = false
}

func (c *TrackingClient) IsMutated() bool {
	return c.mutated
}

func (c *TrackingClient) track(obj client.Object) {
	copied := obj.DeepCopyObject().(client.Object)

	// Populate TypeMeta dynamically using the scheme
	gvk, err := apiutil.GVKForObject(copied, c.scheme)
	if err == nil {
		copied.GetObjectKind().SetGroupVersionKind(gvk)
	}

	key := fmt.Sprintf("%s/%s/%s",
		copied.GetObjectKind().GroupVersionKind().Kind,
		copied.GetNamespace(),
		copied.GetName(),
	)

	if key == c.ignoreKey {
		c.mutated = true // Still mark as mutated for the loop if the target CR itself is updated (e.g. finalizers)
		return
	}

	// Clean volatile fields for comparison
	cleanCopied := copied.DeepCopyObject().(client.Object)
	cleanCopied.SetResourceVersion("")
	cleanCopied.SetUID("")
	cleanCopied.SetCreationTimestamp(metav1.Time{})

	if idx, exists := c.indices[key]; exists {
		existingClean := c.objects[idx].DeepCopyObject().(client.Object)
		existingClean.SetResourceVersion("")
		existingClean.SetUID("")
		existingClean.SetCreationTimestamp(metav1.Time{})

		if reflect.DeepEqual(cleanCopied, existingClean) {
			// No semantic change, don't mark as mutated, just update the stored object
			c.objects[idx] = copied
			return
		}
		c.objects[idx] = copied
	} else {
		c.indices[key] = len(c.objects)
		c.objects = append(c.objects, copied)
	}
	c.mutated = true
}

func (c *TrackingClient) untrack(obj client.Object) {
	c.mutated = true
	gvk, err := apiutil.GVKForObject(obj, c.scheme)
	var kind string
	if err == nil {
		kind = gvk.Kind
	} else {
		kind = obj.GetObjectKind().GroupVersionKind().Kind
	}
	key := fmt.Sprintf("%s/%s/%s", kind, obj.GetNamespace(), obj.GetName())
	if idx, exists := c.indices[key]; exists {
		c.objects = append(c.objects[:idx], c.objects[idx+1:]...)
		delete(c.indices, key)
		for i := idx; i < len(c.objects); i++ {
			k := fmt.Sprintf("%s/%s/%s",
				c.objects[i].GetObjectKind().GroupVersionKind().Kind,
				c.objects[i].GetNamespace(),
				c.objects[i].GetName(),
			)
			c.indices[k] = i
		}
	}
}

func (c *TrackingClient) Create(ctx context.Context, obj client.Object, opts ...client.CreateOption) error {
	err := c.Client.Create(ctx, obj, opts...)
	if err == nil {
		c.track(obj)
	}
	return err
}

func (c *TrackingClient) Update(ctx context.Context, obj client.Object, opts ...client.UpdateOption) error {
	err := c.Client.Update(ctx, obj, opts...)
	if err == nil {
		c.track(obj)
	}
	return err
}

func (c *TrackingClient) Patch(ctx context.Context, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
	// Server-Side Apply fallback for fake client
	// NOTE: This fallback replaces the entire object with the patch object 'obj'.
	// If partial patches are used in the future, this will cause data loss of other fields.
	if patch.Type() == types.ApplyPatchType {
		key := client.ObjectKeyFromObject(obj)
		existing := obj.DeepCopyObject().(client.Object)
		err := c.Client.Get(ctx, key, existing)
		if err != nil {
			if errors.IsNotFound(err) {
				createErr := c.Client.Create(ctx, obj)
				if createErr == nil {
					c.track(obj)
				}
				return createErr
			}
			return err
		}
		obj.SetResourceVersion(existing.GetResourceVersion())
		updateErr := c.Client.Update(ctx, obj)
		if updateErr == nil {
			c.track(obj)
		}
		return updateErr
	}

	err := c.Client.Patch(ctx, obj, patch, opts...)
	if err == nil {
		c.track(obj)
	}
	return err
}

func (c *TrackingClient) Delete(ctx context.Context, obj client.Object, opts ...client.DeleteOption) error {
	err := c.Client.Delete(ctx, obj, opts...)
	if err == nil {
		c.untrack(obj)
	}
	return err
}

func (c *TrackingClient) GetObjects() []client.Object {
	return c.objects
}

// RunGoldenTest runs the complete data-driven golden file integration test for the PlatformAgent.
func RunGoldenTest(
	t *testing.T,
	inputPath, expectedPath string,
	update bool,
	scheme *runtime.Scheme,
	newAgent func() client.Object,
	newReconciler func(c client.Client, s *runtime.Scheme) reconcile.Reconciler,
) {
	runGoldenTest(t, inputPath, expectedPath, update, scheme, newAgent, newReconciler, false)
}

// RunSettledGoldenTest is RunGoldenTest against a cluster that brings up what
// it is given: between reconcile passes every Deployment and StatefulSet is
// marked ready and every Job complete, so the golden is the steady state rather
// than the first pass. Next mode needs it: the A2A gateway waits for the
// callout to serve and the hermes bridge for the provisioning Job, so a
// first-pass golden leaves out the pods an installed cluster runs.
func RunSettledGoldenTest(
	t *testing.T,
	inputPath, expectedPath string,
	update bool,
	scheme *runtime.Scheme,
	newAgent func() client.Object,
	newReconciler func(c client.Client, s *runtime.Scheme) reconcile.Reconciler,
) {
	runGoldenTest(t, inputPath, expectedPath, update, scheme, newAgent, newReconciler, true)
}

func runGoldenTest(
	t *testing.T,
	inputPath, expectedPath string,
	update bool,
	scheme *runtime.Scheme,
	newAgent func() client.Object,
	newReconciler func(c client.Client, s *runtime.Scheme) reconcile.Reconciler,
	settle bool,
) {
	t.Run(filepath.Base(inputPath), func(t *testing.T) {
		// Read input CRD
		// #nosec G304 -- Test utility reading test fixture files
		inputData, err := os.ReadFile(inputPath)
		if err != nil {
			t.Fatalf("failed to read input file: %v", err)
		}

		// Parse CRD
		agent := newAgent()
		if err := yaml.Unmarshal(inputData, agent); err != nil {
			t.Fatalf("failed to unmarshal input CRD: %v", err)
		}

		ctx := context.Background()

		// Run the operator reconciliation loop
		resources, err := runOperatorReconcile(ctx, scheme, agent, newReconciler, settle)
		if err != nil {
			t.Fatalf("operator reconciliation failed: %v", err)
		}

		// Clean up volatile metadata and marshal resources to YAML string
		output := CleanAndMarshalResources(t, resources)

		// Compare with expected golden files
		CompareGolden(t, output, expectedPath, update)
	})
}

// RunOperatorReconcile simulates a single reconciler execution run against a fake API server for the PlatformAgent.
func RunOperatorReconcile(
	ctx context.Context,
	scheme *runtime.Scheme,
	agent client.Object,
	newReconciler func(c client.Client, s *runtime.Scheme) reconcile.Reconciler,
) ([]client.Object, error) {
	return runOperatorReconcile(ctx, scheme, agent, newReconciler, false)
}

func runOperatorReconcile(
	ctx context.Context,
	scheme *runtime.Scheme,
	agent client.Object,
	newReconciler func(c client.Client, s *runtime.Scheme) reconcile.Reconciler,
	settle bool,
) ([]client.Object, error) {
	fakeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(agent).
		Build()

	trackerClient := NewTrackingClient(fakeClient, scheme, agent)

	reconciler := newReconciler(trackerClient, scheme)

	req := reconcile.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.GetName(),
			Namespace: agent.GetNamespace(),
		},
	}

	maxIterations := reconcileIterations
	if settle {
		maxIterations = settledReconcileIterations
	}
	stabilized := false
	for i := 0; i < maxIterations; i++ {
		trackerClient.ResetMutated()
		_, err := reconciler.Reconcile(ctx, req)
		if err != nil {
			return nil, err
		}
		if settle {
			brought, err := bringUp(ctx, fakeClient)
			if err != nil {
				return nil, err
			}
			if brought {
				continue
			}
		}
		if !trackerClient.IsMutated() {
			stabilized = true
			break
		}
	}
	if !stabilized {
		return nil, fmt.Errorf("reconciliation did not stabilize after %d iterations; possible infinite loop", maxIterations)
	}

	return trackerClient.GetObjects(), nil
}

// hashPlaceholder replaces content-hash annotation values in golden files.
const hashPlaceholder = "<SHA256_FOR_TESTS>"

// sha256Hex matches the hex encoding of a SHA-256 digest.
var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// a2aConfigHashKey is the one hash annotation cut short: next mode's
// a2aConfigHashLength is 16 characters, and a2aConfigHex is its shape.
const a2aConfigHashKey = "kubeagents.x-k8s.io/a2a-config-hash"

var a2aConfigHex = regexp.MustCompile(`^[0-9a-f]{16}$`)

// redactHashAnnotations replaces every `…-hash` annotation value with a
// placeholder, asserting first that it looks like a SHA-256 digest. The digests
// change whenever any rendered config does, so pinning them in the golden files
// only produces churn; their shape is what the test cares about.
func redactHashAnnotations(t *testing.T, annotations map[string]string) {
	for key, value := range annotations {
		if !strings.HasSuffix(key, "-hash") {
			continue
		}
		want := sha256Hex
		if key == a2aConfigHashKey {
			want = a2aConfigHex
		}
		if !want.MatchString(value) {
			t.Errorf("annotation %s = %q, want hex matching %s", key, value, want)
		}
		annotations[key] = hashPlaceholder
	}
}

// generatedPlaceholder replaces generated credential and certificate bytes in
// golden files; pemBegin is how a generated certificate is recognised outside a Secret.
const (
	generatedPlaceholder = "<GENERATED_FOR_TESTS>"
	pemBegin             = "-----BEGIN "
)

// renderedConfigKeys are Secret keys holding a config the operator renders
// around generated values: nats.conf carries the bus passwords and the
// callout's public keys, and the rest of it (accounts, permissions, allow
// lists) is exactly what a golden should pin.
var renderedConfigKeys = map[string]bool{"nats.conf": true}

// generatedValues maps every value of every other Secret in the render to a
// placeholder naming where it came from, for redacting a rendered config.
func generatedValues(resources []client.Object) *strings.Replacer {
	var pairs []string
	for _, res := range resources {
		secret, ok := res.(*corev1.Secret)
		if !ok {
			continue
		}
		for key, value := range secret.Data {
			if renderedConfigKeys[key] || len(value) == 0 {
				continue
			}
			pairs = append(pairs, string(value), "<GENERATED:"+secret.Name+"/"+key+">")
		}
	}
	return strings.NewReplacer(pairs...)
}

// redactGeneratedPublicKeys replaces the value of every env var named
// *_PUBLIC_KEY or *_PUBLIC_KEYS: next mode derives them from seeds it
// generates on first reconcile (the task-signing key reaches the callout as
// A2A_TASK_SIGNING_PUBLIC_KEYS), so they differ on every run. The seeds sit in
// Secrets and are redacted there; a public half rendered as a literal is not.
func redactGeneratedPublicKeys(containers []corev1.Container) {
	for i := range containers {
		for j := range containers[i].Env {
			env := &containers[i].Env[j]
			if env.Value != "" && (strings.HasSuffix(env.Name, "_PUBLIC_KEY") || strings.HasSuffix(env.Name, "_PUBLIC_KEYS")) {
				env.Value = generatedPlaceholder
			}
		}
	}
}

// CleanAndMarshalResources cleans up volatile runtime fields and marshals objects to a multi-doc YAML string
func CleanAndMarshalResources(t *testing.T, resources []client.Object) string {
	generated := generatedValues(resources)
	var renderedManifests []string
	for _, res := range resources {
		// Clean up runtime fields that controller-runtime sets
		res.SetResourceVersion("")
		res.SetUID("")
		res.SetCreationTimestamp(metav1.Time{})

		redactHashAnnotations(t, res.GetAnnotations())

		switch obj := res.(type) {
		case *appsv1.Deployment:
			redactHashAnnotations(t, obj.Spec.Template.Annotations)
			redactGeneratedPublicKeys(obj.Spec.Template.Spec.Containers)
		case *appsv1.StatefulSet:
			redactHashAnnotations(t, obj.Spec.Template.Annotations)
		case *corev1.ConfigMap:
			// Omit the massive leader_elect.py script from golden files for readability
			if _, exists := obj.Data["leader_elect.py"]; exists {
				obj.Data["leader_elect.py"] = "<OMITTED_FOR_TESTS>"
			}
			// A next-mode render publishes its generated CA bundle here; the
			// bytes differ on every run. Only a value that is wholly PEM.
			for key, value := range obj.Data {
				if strings.HasPrefix(value, pemBegin) {
					obj.Data[key] = generatedPlaceholder
				}
			}
		case *corev1.Secret:
			// Next mode generates its bus credentials, keys and CA on first
			// reconcile: random on every run, and key material either way.
			// The golden pins which keys each Secret carries, not their values,
			// except for a rendered config (renderedConfigKeys), which keeps
			// every byte but the generated values it interpolates.
			for key, value := range obj.Data {
				if renderedConfigKeys[key] {
					// As text, so the golden shows the config and its
					// placeholders rather than a base64 blob.
					if obj.StringData == nil {
						obj.StringData = map[string]string{}
					}
					obj.StringData[key] = generated.Replace(string(value))
					delete(obj.Data, key)
					continue
				}
				obj.Data[key] = []byte(generatedPlaceholder)
			}
			for key, value := range obj.StringData {
				if renderedConfigKeys[key] {
					obj.StringData[key] = generated.Replace(value)
					continue
				}
				obj.StringData[key] = generatedPlaceholder
			}
		}

		// A settled run (RunSettledGoldenTest) writes workload status itself;
		// the golden is what the operator wrote, so status stays out of it.
		switch obj := res.(type) {
		case *appsv1.Deployment:
			obj.Status = appsv1.DeploymentStatus{}
		case *appsv1.StatefulSet:
			obj.Status = appsv1.StatefulSetStatus{}
		case *batchv1.Job:
			obj.Status = batchv1.JobStatus{}
		}

		yamlBytes, err := yaml.Marshal(res)
		if err != nil {
			t.Fatalf("failed to marshal resource: %v", err)
		}
		renderedManifests = append(renderedManifests, string(yamlBytes))
	}
	return strings.Join(renderedManifests, "---\n")
}

// CompareGolden compares actual output with expected golden file semantically
func CompareGolden(t *testing.T, actual string, expectedPath string, update bool) {
	if update {
		expectedDir := filepath.Dir(expectedPath)
		if err := os.MkdirAll(expectedDir, 0750); err != nil { // #nosec G301 -- Test utility directory creation
			t.Fatalf("failed to create expected directory: %v", err)
		}
		if err := os.WriteFile(expectedPath, []byte(actual), 0600); err != nil { // #nosec G306 -- Test utility writing golden file
			t.Fatalf("failed to write expected golden file: %v", err)
		}
		t.Logf("updated golden file: %s", expectedPath)
		return
	}

	// #nosec G304 -- Test utility reading golden files
	expectedData, err := os.ReadFile(expectedPath)
	if err != nil {
		if os.IsNotExist(err) {
			t.Fatalf("expected golden file %s does not exist. Run tests with -update flag to generate it.", expectedPath)
		}
		t.Fatalf("failed to read expected golden file: %v", err)
	}

	expectedDocs := parseMultiDocYAML(t, string(expectedData))
	actualDocs := parseMultiDocYAML(t, actual)

	if diff := cmp.Diff(expectedDocs, actualDocs); diff != "" {
		t.Errorf("manifests mismatch (-expected +actual):\n%s", diff)
	}
}

func parseMultiDocYAML(t *testing.T, data string) []interface{} {
	data = strings.ReplaceAll(data, "\r\n", "\n")
	parts := strings.Split(data, "\n---\n")
	var docs []interface{}
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		var doc interface{}
		if err := yaml.Unmarshal([]byte(part), &doc); err != nil {
			t.Fatalf("failed to parse YAML doc: %v\nContent:\n%s", err, part)
		}
		docs = append(docs, doc)
	}
	return docs
}

// bringUp does what a cluster's controllers would between two reconcile
// passes: every Deployment and StatefulSet reports all its replicas ready on
// its current generation, and every Job reports complete. It writes status
// through the fake client directly, not the tracking one, so the golden holds
// what the operator wrote. It reports whether it changed anything.
func bringUp(ctx context.Context, c client.Client) (bool, error) {
	changed := false
	var deps appsv1.DeploymentList
	if err := c.List(ctx, &deps); err != nil {
		return false, err
	}
	for i := range deps.Items {
		d := &deps.Items[i]
		want := int32(1)
		if d.Spec.Replicas != nil {
			want = *d.Spec.Replicas
		}
		s := d.Status
		if s.ObservedGeneration == d.Generation && s.ReadyReplicas == want && s.UpdatedReplicas == want && s.AvailableReplicas == want && s.Replicas == want {
			continue
		}
		d.Status = appsv1.DeploymentStatus{ObservedGeneration: d.Generation, Replicas: want, ReadyReplicas: want, UpdatedReplicas: want, AvailableReplicas: want}
		if err := c.Status().Update(ctx, d); err != nil {
			return false, err
		}
		changed = true
	}
	var sets appsv1.StatefulSetList
	if err := c.List(ctx, &sets); err != nil {
		return false, err
	}
	for i := range sets.Items {
		s := &sets.Items[i]
		want := int32(1)
		if s.Spec.Replicas != nil {
			want = *s.Spec.Replicas
		}
		st := s.Status
		if st.ObservedGeneration == s.Generation && st.ReadyReplicas == want && st.UpdatedReplicas == want && st.AvailableReplicas == want && st.Replicas == want {
			continue
		}
		s.Status = appsv1.StatefulSetStatus{ObservedGeneration: s.Generation, Replicas: want, ReadyReplicas: want, UpdatedReplicas: want, AvailableReplicas: want, CurrentReplicas: want}
		if err := c.Status().Update(ctx, s); err != nil {
			return false, err
		}
		changed = true
	}
	var jobs batchv1.JobList
	if err := c.List(ctx, &jobs); err != nil {
		return false, err
	}
	for i := range jobs.Items {
		j := &jobs.Items[i]
		if j.Status.Succeeded > 0 {
			continue
		}
		now := metav1.Now()
		j.Status = batchv1.JobStatus{
			Succeeded:      1,
			StartTime:      &now,
			CompletionTime: &now,
			Conditions: []batchv1.JobCondition{
				{Type: batchv1.JobSuccessCriteriaMet, Status: corev1.ConditionTrue},
				{Type: batchv1.JobComplete, Status: corev1.ConditionTrue},
			},
		}
		if err := c.Status().Update(ctx, j); err != nil {
			return false, err
		}
		changed = true
	}
	return changed, nil
}
