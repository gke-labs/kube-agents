package testutil

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/google/go-cmp/cmp"
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
	t.Run(filepath.Base(inputPath), func(t *testing.T) {
		// Read input CRD
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
		resources, err := RunOperatorReconcile(ctx, scheme, agent, newReconciler)
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

	maxIterations := 5
	stabilized := false
	for i := 0; i < maxIterations; i++ {
		trackerClient.ResetMutated()
		_, err := reconciler.Reconcile(ctx, req)
		if err != nil {
			return nil, err
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

// CleanAndMarshalResources cleans up volatile runtime fields and marshals objects to a multi-doc YAML string
func CleanAndMarshalResources(t *testing.T, resources []client.Object) string {
	var renderedManifests []string
	for _, res := range resources {
		// Clean up runtime fields that controller-runtime sets
		res.SetResourceVersion("")
		res.SetUID("")
		res.SetCreationTimestamp(metav1.Time{})

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
		if err := os.MkdirAll(expectedDir, 0755); err != nil {
			t.Fatalf("failed to create expected directory: %v", err)
		}
		if err := os.WriteFile(expectedPath, []byte(actual), 0644); err != nil {
			t.Fatalf("failed to write expected golden file: %v", err)
		}
		t.Logf("updated golden file: %s", expectedPath)
		return
	}

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
