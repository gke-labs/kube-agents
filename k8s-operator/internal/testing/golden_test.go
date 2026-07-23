package testing

import (
	"flag"
	"path/filepath"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/testutil"
)

var (
	update     = flag.Bool("update", false, "update golden files")
	testScheme = runtime.NewScheme()
)

func init() {
	_ = agentv1alpha1.AddToScheme(testScheme)
	_ = corev1.AddToScheme(testScheme)
	_ = appsv1.AddToScheme(testScheme)
	_ = networkingv1.AddToScheme(testScheme)
	_ = rbacv1.AddToScheme(testScheme)
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
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			testutil.RunGoldenTest(
				t,
				tt.inputPath,
				tt.expectedPath,
				*update,
				testScheme,
				tt.newAgent,
				tt.newReconciler,
			)
		})
	}
}
