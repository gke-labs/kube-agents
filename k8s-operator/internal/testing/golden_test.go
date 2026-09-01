package testing

import (
	"flag"
	"path/filepath"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/testutil"
)

var update = flag.Bool("update", false, "update golden files")

// newTestScheme builds a Scheme for one subtest. Per-subtest and not a shared
// package-level var, because a Scheme is not safe to hand to concurrent fake
// clients: controller-runtime's fake client lazily registers types it has not
// seen (fake.(*fakeClient).addToSchemeIfUnknownAndUnstructuredOrPartial calls
// Scheme.AddKnownTypeWithName). That function does take c.schemeLock, which is
// why this looks safe at a glance -- but schemeLock is a field on the client,
// so it orders writes within one fake client and nothing at all across the
// three siblings here that were handed the same Scheme.
//
// The readers are wider than the writers. Every SSA Create rebuilds a
// RESTMapper over the Scheme (testrestmapper.TestOnlyStaticRESTMapper ->
// Scheme.PrioritizedVersionsForGroup), so a single lazy write races the whole
// of every sibling's reconcile, not just their own lazy writes. Measured on the
// parent commit: 23 of 25 race-detector runs report it, and 7 of 40 plain runs
// die outright with `fatal error: concurrent map writes`.
//
// A fatal error is not a recoverable panic: it killed the whole binary and
// printed whichever goroutine happened to be mid-reconcile, so it read as a
// defect in whatever the reader had just touched rather than as test-harness
// contention (#918).
//
// Registering more types up front is the tempting narrower fix, and for today's
// code it does work -- FQDNNetworkPolicy is the only type the golden path
// reaches lazily, and pre-registering it takes the race to 0 of 25. It is not
// the fix taken, because it holds only until the next unregistered type, and
// the lazy path fires precisely on the types nobody thought to register.
// Isolation does not depend on that guess: a Scheme reachable from one
// goroutine cannot be raced on.
func newTestScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = agentv1alpha1.AddToScheme(s)
	_ = corev1.AddToScheme(s)
	_ = appsv1.AddToScheme(s)
	_ = networkingv1.AddToScheme(s)
	_ = policyv1.AddToScheme(s)
	_ = rbacv1.AddToScheme(s)
	return s
}

func TestAgentsGolden(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name          string
		inputPath     string
		expectedPath  string
		newAgent      func() client.Object
		newReconciler func(client.Client, *runtime.Scheme) reconcile.Reconciler
	}{
		{
			name:         "PlatformAgent",
			inputPath:    filepath.Join("..", "..", "examples", "platformagent.yaml"),
			expectedPath: filepath.Join("testdata", "platform", "expected", "platformagent.yaml"),
			newAgent:     func() client.Object { return &agentv1alpha1.PlatformAgent{} },
			newReconciler: func(c client.Client, s *runtime.Scheme) reconcile.Reconciler {
				return &controller.PlatformAgentReconciler{Client: c, Scheme: s}
			},
		},
		{
			name:         "PlatformAgentTaggedImage",
			inputPath:    filepath.Join("testdata", "platform", "platformagent-tagged.yaml"),
			expectedPath: filepath.Join("testdata", "platform", "expected", "platformagent-tagged.yaml"),
			newAgent:     func() client.Object { return &agentv1alpha1.PlatformAgent{} },
			newReconciler: func(c client.Client, s *runtime.Scheme) reconcile.Reconciler {
				return &controller.PlatformAgentReconciler{Client: c, Scheme: s}
			},
		},
		{
			name:         "PlatformAgentCustomCollector",
			inputPath:    filepath.Join("testdata", "platform", "platformagent-telemetry.yaml"),
			expectedPath: filepath.Join("testdata", "platform", "expected", "platformagent-telemetry.yaml"),
			newAgent:     func() client.Object { return &agentv1alpha1.PlatformAgent{} },
			newReconciler: func(c client.Client, s *runtime.Scheme) reconcile.Reconciler {
				return &controller.PlatformAgentReconciler{Client: c, Scheme: s}
			},
		},
		{
			// The gate on. Diff this against platformagent-tagged.yaml to see
			// exactly what splitting the credential broker into its own Pod
			// changes, and nothing else.
			name:         "PlatformAgentSplitCredentialBroker",
			inputPath:    filepath.Join("testdata", "platform", "platformagent-split-broker.yaml"),
			expectedPath: filepath.Join("testdata", "platform", "expected", "platformagent-split-broker.yaml"),
			newAgent:     func() client.Object { return &agentv1alpha1.PlatformAgent{} },
			newReconciler: func(c client.Client, s *runtime.Scheme) reconcile.Reconciler {
				return &controller.PlatformAgentReconciler{Client: c, Scheme: s}
			},
		},
		{
			// The scoped service account pool on. Diff this against
			// platformagent-tagged.yaml and the whole of what
			// spec.security.scopedServiceAccounts renders is a ConfigMap key,
			// a SubPath mount and two environment variables — which is the
			// point of the fixture. The exit criterion for that work is that
			// the cluster-to-account mapping is readable off a manifest rather
			// than inferred from what the broker does at runtime, and a golden
			// file is the only artifact that can hold that claim honestly.
			name:         "PlatformAgentScopedServiceAccounts",
			inputPath:    filepath.Join("testdata", "platform", "platformagent-scoped-sa.yaml"),
			expectedPath: filepath.Join("testdata", "platform", "expected", "platformagent-scoped-sa.yaml"),
			newAgent:     func() client.Object { return &agentv1alpha1.PlatformAgent{} },
			newReconciler: func(c client.Client, s *runtime.Scheme) reconcile.Reconciler {
				return &controller.PlatformAgentReconciler{Client: c, Scheme: s}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			testutil.RunGoldenTest(
				t,
				tt.inputPath,
				tt.expectedPath,
				*update,
				newTestScheme(),
				tt.newAgent,
				tt.newReconciler,
			)
		})
	}
}
