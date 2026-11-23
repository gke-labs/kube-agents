package controller

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	runtimemetrics "runtime/metrics"
	"strconv"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sruntime "k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	lastAppliedConfigAnnotation = "kubectl.kubernetes.io/last-applied-configuration"
	cacheTestObjectsPerKind     = 500
	cacheSoakObjectsPerKind     = 4000
	cacheSoakObjectsEnv         = "SOAK_OBJECTS_PER_KIND"
	cacheSoakEnabledEnv         = "SOAK"
	cacheSoakEnabledValue       = "1"
	cacheSyncTimeout            = 2 * time.Minute
	liveHeapMetricName          = "/gc/heap/live:bytes"
	procSelfStatusPath          = "/proc/self/status"
	vmRSSField                  = "VmRSS:"
	vmHWMField                  = "VmHWM:"
	kibibyteBytes               = 1024
	mebibyteBytes               = 1024 * 1024
	objectsPerThousand          = 1000
	cacheFixedEpoch             = "2026-10-01T00:00:00Z"
	cacheNamespaceCount         = 25
	cacheAppServiceSpan         = 200
	cacheImageVersionSpan       = 50
	cacheStoreVersionSpan       = 20
	cacheOctetCount             = 256
	cacheServiceMetricsPort     = 9090
	cacheDNSPort                = 53
	cacheProgressDeadlineSecs   = 600
	cacheProbePeriodSecs        = 10
	cacheProbeTimeoutSecs       = 1
	cacheProbeSuccessThreshold  = 1
	cacheProbeFailureThreshold  = 3
	cacheReplicaCount           = 2
	cacheContainerPort          = 8080
	cacheServicePort            = 80
	cacheGracePeriodSeconds     = 30
	cacheHistoryLimit           = 10
	cacheMaxUnavailable         = 1
	cacheFSGroup                = 1000
	cacheConfigMapAppYAMLBytes  = 512
	cacheConfigMapLineBytes     = 36
	cacheUIDLowMask             = 0xffff
	cacheClientQPS              = 1000
	cacheClientBurst            = 2000

	// Per-object live-heap ceilings (in bytes) under ManagerCacheOptions()
	// (managedFields stripped, last-applied-configuration retained). Each
	// ceiling sits above the measured stripped cost with headroom for GC/runtime
	// variance across platforms while catching a regression that re-admits
	// managedFields or duplicates objects in the cache.
	deploymentBytesCeiling         = 11 * kibibyteBytes
	statefulSetBytesCeiling        = 10 * kibibyteBytes
	serviceBytesCeiling            = 4608
	configMapBytesCeiling          = 4608
	serviceAccountBytesCeiling     = 2560
	pvcBytesCeiling                = 4608
	networkPolicyBytesCeiling      = 4608
	pdbBytesCeiling                = 3328
	roleBytesCeiling               = 4352
	roleBindingBytesCeiling        = 2816
	clusterRoleBytesCeiling        = 4096
	clusterRoleBindingBytesCeiling = 2816
	// Across all 12 watched cluster-scaling kinds combined, the average
	// per-object cost under ManagerCacheOptions() is ~3.76 KiB/object (~44.1 MiB
	// per 1,000 objects of every kind, i.e. 12,000 objects total), compared to
	// ~4.60 KiB/object raw and ~2.63 KiB/object with last-applied-configuration
	// also stripped.
	combinedPerObjectBytesCeiling = 4608
)

// The manager's cache keeps no managedFields. Every informer here is
// cluster-wide, so each entry is paid for once per object in the cluster, and
// managedFields is the largest part of an object nothing in this process
// reads: the only managedFields consumer in the repository is the drift
// detector, a separate binary with its own live client.
func TestTheManagerCacheKeepsNoManagedFields(t *testing.T) {
	transform := ManagerCacheOptions().DefaultTransform
	if transform == nil {
		t.Fatal("the manager's cache has no DefaultTransform, so every cached object carries its managedFields")
	}

	deployment := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "platform-agent-gateway",
			Namespace:   "kubeagents-system",
			Annotations: map[string]string{lastAppliedConfigAnnotation: "{}"},
			ManagedFields: []metav1.ManagedFieldsEntry{
				{Manager: "kube-agents-operator", Operation: metav1.ManagedFieldsOperationApply},
				{Manager: "kube-controller-manager", Operation: metav1.ManagedFieldsOperationUpdate},
			},
		},
	}

	out, err := transform(deployment)
	if err != nil {
		t.Fatalf("transforming a Deployment on its way into the cache: %v", err)
	}
	cached, ok := out.(*appsv1.Deployment)
	if !ok {
		t.Fatalf("the transform returned %T, want *appsv1.Deployment", out)
	}
	if len(cached.ManagedFields) != 0 {
		t.Errorf("the cached Deployment kept %d managedFields entries, want none", len(cached.ManagedFields))
	}
	// The annotation stays. It is the other bulk field, but a read-modify-write
	// built from a cached copy that had lost it would delete it from the live
	// object; managedFields is omitempty, so a nil one is simply absent from
	// the request and the server's own entries stand.
	if _, ok := cached.Annotations[lastAppliedConfigAnnotation]; !ok {
		t.Error("the transform dropped last-applied-configuration; an Update built from a cached copy would then remove it from the live object")
	}
	if cached.Name != deployment.Name || cached.Namespace != deployment.Namespace {
		t.Errorf("the transform changed the object's identity: %s/%s", cached.Namespace, cached.Name)
	}
}

// Pods are out of the cache by configuration, not by every reader
// remembering to ask for a live one. The four readers that exist are tested
// individually (pod_reads_are_live_test.go); this is what covers the fifth,
// written by someone who never read those tests.
func TestTheManagerClientNeverServesAPodFromTheCache(t *testing.T) {
	opts := ManagerClientOptions()
	if opts.Cache == nil {
		t.Fatal("the manager's client has no CacheOptions, so every kind is served from the cache")
	}
	for _, obj := range opts.Cache.DisableFor {
		if _, ok := obj.(*corev1.Pod); ok {
			return
		}
	}
	t.Errorf("Pods are not in the client's DisableFor (%T), so a cached Pod read starts a cluster-wide Pod informer", opts.Cache.DisableFor)
}

type watchedKindSpec struct {
	name             string
	path             string
	gv               schema.GroupVersion
	gvk              schema.GroupVersionKind
	gvr              schema.GroupVersionResource
	scope            meta.RESTScope
	perObjectCeiling int64
	prototype        client.Object
	newListObj       func() client.ObjectList
	buildList        func(count int) k8sruntime.Object
}

func watchedClusterScalingKinds() []watchedKindSpec {
	return []watchedKindSpec{
		{
			name:             "Deployment",
			path:             "/apis/apps/v1/deployments",
			gv:               appsv1.SchemeGroupVersion,
			gvk:              appsv1.SchemeGroupVersion.WithKind("Deployment"),
			gvr:              appsv1.SchemeGroupVersion.WithResource("deployments"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: deploymentBytesCeiling,
			prototype:        &appsv1.Deployment{},
			newListObj:       func() client.ObjectList { return &appsv1.DeploymentList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &appsv1.DeploymentList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]appsv1.Deployment, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticDeployment(i)
				}
				return list
			},
		},
		{
			name:             "StatefulSet",
			path:             "/apis/apps/v1/statefulsets",
			gv:               appsv1.SchemeGroupVersion,
			gvk:              appsv1.SchemeGroupVersion.WithKind("StatefulSet"),
			gvr:              appsv1.SchemeGroupVersion.WithResource("statefulsets"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: statefulSetBytesCeiling,
			prototype:        &appsv1.StatefulSet{},
			newListObj:       func() client.ObjectList { return &appsv1.StatefulSetList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &appsv1.StatefulSetList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]appsv1.StatefulSet, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticStatefulSet(i)
				}
				return list
			},
		},
		{
			name:             "Service",
			path:             "/api/v1/services",
			gv:               corev1.SchemeGroupVersion,
			gvk:              corev1.SchemeGroupVersion.WithKind("Service"),
			gvr:              corev1.SchemeGroupVersion.WithResource("services"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: serviceBytesCeiling,
			prototype:        &corev1.Service{},
			newListObj:       func() client.ObjectList { return &corev1.ServiceList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &corev1.ServiceList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]corev1.Service, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticService(i)
				}
				return list
			},
		},
		{
			name:             "ConfigMap",
			path:             "/api/v1/configmaps",
			gv:               corev1.SchemeGroupVersion,
			gvk:              corev1.SchemeGroupVersion.WithKind("ConfigMap"),
			gvr:              corev1.SchemeGroupVersion.WithResource("configmaps"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: configMapBytesCeiling,
			prototype:        &corev1.ConfigMap{},
			newListObj:       func() client.ObjectList { return &corev1.ConfigMapList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &corev1.ConfigMapList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]corev1.ConfigMap, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticConfigMap(i)
				}
				return list
			},
		},
		{
			name:             "ServiceAccount",
			path:             "/api/v1/serviceaccounts",
			gv:               corev1.SchemeGroupVersion,
			gvk:              corev1.SchemeGroupVersion.WithKind("ServiceAccount"),
			gvr:              corev1.SchemeGroupVersion.WithResource("serviceaccounts"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: serviceAccountBytesCeiling,
			prototype:        &corev1.ServiceAccount{},
			newListObj:       func() client.ObjectList { return &corev1.ServiceAccountList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &corev1.ServiceAccountList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]corev1.ServiceAccount, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticServiceAccount(i)
				}
				return list
			},
		},
		{
			name:             "PersistentVolumeClaim",
			path:             "/api/v1/persistentvolumeclaims",
			gv:               corev1.SchemeGroupVersion,
			gvk:              corev1.SchemeGroupVersion.WithKind("PersistentVolumeClaim"),
			gvr:              corev1.SchemeGroupVersion.WithResource("persistentvolumeclaims"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: pvcBytesCeiling,
			prototype:        &corev1.PersistentVolumeClaim{},
			newListObj:       func() client.ObjectList { return &corev1.PersistentVolumeClaimList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &corev1.PersistentVolumeClaimList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]corev1.PersistentVolumeClaim, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticPVC(i)
				}
				return list
			},
		},
		{
			name:             "NetworkPolicy",
			path:             "/apis/networking.k8s.io/v1/networkpolicies",
			gv:               networkingv1.SchemeGroupVersion,
			gvk:              networkingv1.SchemeGroupVersion.WithKind("NetworkPolicy"),
			gvr:              networkingv1.SchemeGroupVersion.WithResource("networkpolicies"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: networkPolicyBytesCeiling,
			prototype:        &networkingv1.NetworkPolicy{},
			newListObj:       func() client.ObjectList { return &networkingv1.NetworkPolicyList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &networkingv1.NetworkPolicyList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]networkingv1.NetworkPolicy, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticNetworkPolicy(i)
				}
				return list
			},
		},
		{
			name:             "PodDisruptionBudget",
			path:             "/apis/policy/v1/poddisruptionbudgets",
			gv:               policyv1.SchemeGroupVersion,
			gvk:              policyv1.SchemeGroupVersion.WithKind("PodDisruptionBudget"),
			gvr:              policyv1.SchemeGroupVersion.WithResource("poddisruptionbudgets"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: pdbBytesCeiling,
			prototype:        &policyv1.PodDisruptionBudget{},
			newListObj:       func() client.ObjectList { return &policyv1.PodDisruptionBudgetList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &policyv1.PodDisruptionBudgetList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]policyv1.PodDisruptionBudget, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticPDB(i)
				}
				return list
			},
		},
		{
			name:             "Role",
			path:             "/apis/rbac.authorization.k8s.io/v1/roles",
			gv:               rbacv1.SchemeGroupVersion,
			gvk:              rbacv1.SchemeGroupVersion.WithKind("Role"),
			gvr:              rbacv1.SchemeGroupVersion.WithResource("roles"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: roleBytesCeiling,
			prototype:        &rbacv1.Role{},
			newListObj:       func() client.ObjectList { return &rbacv1.RoleList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &rbacv1.RoleList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]rbacv1.Role, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticRole(i)
				}
				return list
			},
		},
		{
			name:             "RoleBinding",
			path:             "/apis/rbac.authorization.k8s.io/v1/rolebindings",
			gv:               rbacv1.SchemeGroupVersion,
			gvk:              rbacv1.SchemeGroupVersion.WithKind("RoleBinding"),
			gvr:              rbacv1.SchemeGroupVersion.WithResource("rolebindings"),
			scope:            meta.RESTScopeNamespace,
			perObjectCeiling: roleBindingBytesCeiling,
			prototype:        &rbacv1.RoleBinding{},
			newListObj:       func() client.ObjectList { return &rbacv1.RoleBindingList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &rbacv1.RoleBindingList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]rbacv1.RoleBinding, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticRoleBinding(i)
				}
				return list
			},
		},
		{
			name:             "ClusterRole",
			path:             "/apis/rbac.authorization.k8s.io/v1/clusterroles",
			gv:               rbacv1.SchemeGroupVersion,
			gvk:              rbacv1.SchemeGroupVersion.WithKind("ClusterRole"),
			gvr:              rbacv1.SchemeGroupVersion.WithResource("clusterroles"),
			scope:            meta.RESTScopeRoot,
			perObjectCeiling: clusterRoleBytesCeiling,
			prototype:        &rbacv1.ClusterRole{},
			newListObj:       func() client.ObjectList { return &rbacv1.ClusterRoleList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &rbacv1.ClusterRoleList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]rbacv1.ClusterRole, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticClusterRole(i)
				}
				return list
			},
		},
		{
			name:             "ClusterRoleBinding",
			path:             "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",
			gv:               rbacv1.SchemeGroupVersion,
			gvk:              rbacv1.SchemeGroupVersion.WithKind("ClusterRoleBinding"),
			gvr:              rbacv1.SchemeGroupVersion.WithResource("clusterrolebindings"),
			scope:            meta.RESTScopeRoot,
			perObjectCeiling: clusterRoleBindingBytesCeiling,
			prototype:        &rbacv1.ClusterRoleBinding{},
			newListObj:       func() client.ObjectList { return &rbacv1.ClusterRoleBindingList{} },
			buildList: func(count int) k8sruntime.Object {
				list := &rbacv1.ClusterRoleBindingList{
					ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(count + 1)},
					Items:    make([]rbacv1.ClusterRoleBinding, count),
				}
				for i := range count {
					list.Items[i] = *generateRealisticClusterRoleBinding(i)
				}
				return list
			},
		},
	}
}

func cacheObjectTimestamp(i int) metav1.Time {
	epoch, err := time.Parse(time.RFC3339, cacheFixedEpoch)
	if err != nil {
		panic(err)
	}
	return metav1.NewTime(epoch.Add(time.Duration(i) * time.Minute))
}

func cacheObjectMeta(kind string, i int, namespaced bool, labels map[string]string) metav1.ObjectMeta {
	ns := ""
	if namespaced {
		ns = fmt.Sprintf("team-%02d", i%cacheNamespaceCount)
	}
	return metav1.ObjectMeta{
		Name:              fmt.Sprintf("%s-workload-%05d", strings.ToLower(kind), i),
		Namespace:         ns,
		UID:               types.UID(fmt.Sprintf("a1b2c3d4-%04x-4000-8000-%012x", i&cacheUIDLowMask, i)),
		ResourceVersion:   strconv.Itoa(i + 1),
		Generation:        1,
		CreationTimestamp: cacheObjectTimestamp(i),
		Labels:            labels,
	}
}

func attachLastAppliedAndManagedFields(obj k8sruntime.Object, created metav1.Time, fieldsV1 []metav1.ManagedFieldsEntry) {
	accessor, err := meta.Accessor(obj)
	if err != nil {
		panic(err)
	}
	// Realistic kubectl apply annotation: serialized JSON of the object's spec
	// and metadata before status and managedFields are added.
	raw, err := json.Marshal(obj)
	if err != nil {
		panic(err)
	}
	ann := accessor.GetAnnotations()
	if ann == nil {
		ann = make(map[string]string, 1)
	}
	ann[lastAppliedConfigAnnotation] = string(raw)
	accessor.SetAnnotations(ann)
	accessor.SetManagedFields(fieldsV1)
}

func makeManagedField(manager, apiVersion string, op metav1.ManagedFieldsOperationType, subresource string, at metav1.Time, rawFields string) metav1.ManagedFieldsEntry {
	return metav1.ManagedFieldsEntry{
		Manager:     manager,
		Operation:   op,
		APIVersion:  apiVersion,
		Time:        &at,
		FieldsType:  "FieldsV1",
		FieldsV1:    &metav1.FieldsV1{Raw: []byte(rawFields)},
		Subresource: subresource,
	}
}

func generateRealisticDeployment(i int) *appsv1.Deployment {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name":       appLabel,
		"app.kubernetes.io/instance":   fmt.Sprintf("%s-prod", appLabel),
		"app.kubernetes.io/managed-by": "kubectl-client-side-apply",
	}
	om := cacheObjectMeta("deploy", i, true, labels)
	replicas := int32(cacheReplicaCount)
	revisionHistory := int32(cacheHistoryLimit)
	progressDeadline := int32(cacheProgressDeadlineSecs)
	grace := int64(cacheGracePeriodSeconds)
	fsGroup := int64(cacheFSGroup)
	maxSurge := intstr.FromString("25%")
	maxUnavailable := intstr.FromString("25%")
	dep := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: om,
		Spec: appsv1.DeploymentSpec{
			Replicas:                &replicas,
			RevisionHistoryLimit:    &revisionHistory,
			ProgressDeadlineSeconds: &progressDeadline,
			Selector:                &metav1.LabelSelector{MatchLabels: map[string]string{"app.kubernetes.io/name": appLabel}},
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxUnavailable: &maxUnavailable,
					MaxSurge:       &maxSurge,
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					ServiceAccountName:            fmt.Sprintf("sa-workload-%05d", i),
					TerminationGracePeriodSeconds: &grace,
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: ptr.To(true),
						FSGroup:      &fsGroup,
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					Containers: []corev1.Container{{
						Name:            "app",
						Image:           fmt.Sprintf("us-central1-docker.pkg.dev/example-prod/services/%s:v1.24.%d", appLabel, i%cacheImageVersionSpan),
						ImagePullPolicy: corev1.PullIfNotPresent,
						Ports:           []corev1.ContainerPort{{Name: "http", ContainerPort: cacheContainerPort, Protocol: corev1.ProtocolTCP}},
						Env: []corev1.EnvVar{
							{Name: "SERVICE_NAME", Value: appLabel},
							{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{APIVersion: "v1", FieldPath: "metadata.namespace"}}},
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("250m"),
								corev1.ResourceMemory: resource.MustParse("512Mi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("1"),
								corev1.ResourceMemory: resource.MustParse("1Gi"),
							},
						},
						LivenessProbe: &corev1.Probe{
							ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: "/healthz", Port: intstr.FromInt32(cacheContainerPort), Scheme: corev1.URISchemeHTTP}},
							PeriodSeconds:    cacheProbePeriodSecs,
							TimeoutSeconds:   cacheProbeTimeoutSecs,
							SuccessThreshold: cacheProbeSuccessThreshold,
							FailureThreshold: cacheProbeFailureThreshold,
						},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: "/readyz", Port: intstr.FromInt32(cacheContainerPort), Scheme: corev1.URISchemeHTTP}},
							PeriodSeconds:    cacheProbePeriodSecs,
							TimeoutSeconds:   cacheProbeTimeoutSecs,
							SuccessThreshold: cacheProbeSuccessThreshold,
							FailureThreshold: cacheProbeFailureThreshold,
						},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: ptr.To(false),
							ReadOnlyRootFilesystem:   ptr.To(true),
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
						},
					}},
				},
			},
		},
	}
	attachLastAppliedAndManagedFields(dep, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "apps/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/instance":{},"f:app.kubernetes.io/managed-by":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:progressDeadlineSeconds":{},"f:replicas":{},"f:revisionHistoryLimit":{},"f:selector":{},"f:strategy":{"f:rollingUpdate":{".":{},"f:maxSurge":{},"f:maxUnavailable":{}},"f:type":{}},"f:template":{"f:metadata":{"f:labels":{".":{},"f:app.kubernetes.io/instance":{},"f:app.kubernetes.io/managed-by":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:containers":{"k:{\"name\":\"app\"}":{".":{},"f:env":{".":{},"k:{\"name\":\"POD_NAMESPACE\"}":{".":{},"f:name":{},"f:valueFrom":{".":{},"f:fieldRef":{}}},"k:{\"name\":\"SERVICE_NAME\"}":{".":{},"f:name":{},"f:value":{}}},"f:image":{},"f:imagePullPolicy":{},"f:livenessProbe":{".":{},"f:failureThreshold":{},"f:httpGet":{".":{},"f:path":{},"f:port":{},"f:scheme":{}},"f:periodSeconds":{},"f:successThreshold":{},"f:timeoutSeconds":{}},"f:name":{},"f:ports":{".":{},"k:{\"containerPort\":8080,\"protocol\":\"TCP\"}":{".":{},"f:containerPort":{},"f:name":{},"f:protocol":{}}},"f:readinessProbe":{".":{},"f:failureThreshold":{},"f:httpGet":{".":{},"f:path":{},"f:port":{},"f:scheme":{}},"f:periodSeconds":{},"f:successThreshold":{},"f:timeoutSeconds":{}},"f:resources":{".":{},"f:limits":{".":{},"f:cpu":{},"f:memory":{}},"f:requests":{".":{},"f:cpu":{},"f:memory":{}}},"f:securityContext":{".":{},"f:allowPrivilegeEscalation":{},"f:capabilities":{".":{},"f:drop":{}},"f:readOnlyRootFilesystem":{}}}},"f:securityContext":{".":{},"f:fsGroup":{},"f:runAsNonRoot":{},"f:seccompProfile":{".":{},"f:type":{}}},"f:serviceAccountName":{},"f:terminationGracePeriodSeconds":{}}}}}`),
		makeManagedField("kube-controller-manager", "apps/v1", metav1.ManagedFieldsOperationUpdate, "status", created,
			`{"f:metadata":{"f:annotations":{"f:deployment.kubernetes.io/revision":{}}},"f:status":{"f:availableReplicas":{},"f:conditions":{".":{},"k:{\"type\":\"Available\"}":{".":{},"f:lastTransitionTime":{},"f:lastUpdateTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}},"k:{\"type\":\"Progressing\"}":{".":{},"f:lastTransitionTime":{},"f:lastUpdateTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}}},"f:observedGeneration":{},"f:readyReplicas":{},"f:replicas":{},"f:updatedReplicas":{}}}`),
	})
	dep.Status = appsv1.DeploymentStatus{
		ObservedGeneration: 1,
		Replicas:           replicas,
		UpdatedReplicas:    replicas,
		ReadyReplicas:      replicas,
		AvailableReplicas:  replicas,
		Conditions: []appsv1.DeploymentCondition{
			{Type: appsv1.DeploymentAvailable, Status: corev1.ConditionTrue, LastUpdateTime: created, LastTransitionTime: created, Reason: "MinimumReplicasAvailable", Message: "Deployment has minimum availability."},
			{Type: appsv1.DeploymentProgressing, Status: corev1.ConditionTrue, LastUpdateTime: created, LastTransitionTime: created, Reason: "NewReplicaSetAvailable", Message: "ReplicaSet has successfully progressed."},
		},
	}
	return dep
}

func generateRealisticStatefulSet(i int) *appsv1.StatefulSet {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("stateful-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name":     appLabel,
		"app.kubernetes.io/instance": fmt.Sprintf("%s-store", appLabel),
	}
	om := cacheObjectMeta("sts", i, true, labels)
	replicas := int32(cacheReplicaCount)
	revisionHistory := int32(cacheHistoryLimit)
	grace := int64(cacheGracePeriodSeconds)
	storageClass := "standard-rwo"
	sts := &appsv1.StatefulSet{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "StatefulSet"},
		ObjectMeta: om,
		Spec: appsv1.StatefulSetSpec{
			Replicas:             &replicas,
			ServiceName:          fmt.Sprintf("%s-headless", appLabel),
			PodManagementPolicy:  appsv1.OrderedReadyPodManagement,
			RevisionHistoryLimit: &revisionHistory,
			Selector:             &metav1.LabelSelector{MatchLabels: map[string]string{"app.kubernetes.io/name": appLabel}},
			UpdateStrategy:       appsv1.StatefulSetUpdateStrategy{Type: appsv1.RollingUpdateStatefulSetStrategyType},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					ServiceAccountName:            fmt.Sprintf("sa-sts-%05d", i),
					TerminationGracePeriodSeconds: &grace,
					Containers: []corev1.Container{{
						Name:            "store",
						Image:           fmt.Sprintf("us-central1-docker.pkg.dev/example-prod/stores/%s:v2.4.%d", appLabel, i%cacheStoreVersionSpan),
						ImagePullPolicy: corev1.PullIfNotPresent,
						Ports:           []corev1.ContainerPort{{Name: "tcp", ContainerPort: cacheContainerPort, Protocol: corev1.ProtocolTCP}},
						VolumeMounts:    []corev1.VolumeMount{{Name: "data", MountPath: "/var/lib/data"}},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("500m"),
								corev1.ResourceMemory: resource.MustParse("1Gi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("2"),
								corev1.ResourceMemory: resource.MustParse("2Gi"),
							},
						},
					}},
				},
			},
			VolumeClaimTemplates: []corev1.PersistentVolumeClaim{{
				ObjectMeta: metav1.ObjectMeta{Name: "data"},
				Spec: corev1.PersistentVolumeClaimSpec{
					AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
					StorageClassName: &storageClass,
					Resources: corev1.VolumeResourceRequirements{
						Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("50Gi")},
					},
				},
			}},
		},
	}
	attachLastAppliedAndManagedFields(sts, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "apps/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/instance":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:podManagementPolicy":{},"f:replicas":{},"f:revisionHistoryLimit":{},"f:selector":{},"f:serviceName":{},"f:template":{"f:metadata":{"f:labels":{".":{},"f:app.kubernetes.io/instance":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:containers":{"k:{\"name\":\"store\"}":{".":{},"f:image":{},"f:imagePullPolicy":{},"f:name":{},"f:ports":{".":{},"k:{\"containerPort\":8080,\"protocol\":\"TCP\"}":{".":{},"f:containerPort":{},"f:name":{},"f:protocol":{}}},"f:resources":{".":{},"f:limits":{".":{},"f:cpu":{},"f:memory":{}},"f:requests":{".":{},"f:cpu":{},"f:memory":{}}},"f:volumeMounts":{".":{},"k:{\"mountPath\":\"/var/lib/data\"}":{".":{},"f:mountPath":{},"f:name":{}}}}},"f:serviceAccountName":{},"f:terminationGracePeriodSeconds":{}}},"f:updateStrategy":{"f:type":{}},"f:volumeClaimTemplates":{}}}`),
		makeManagedField("kube-controller-manager", "apps/v1", metav1.ManagedFieldsOperationUpdate, "status", created,
			`{"f:status":{"f:availableReplicas":{},"f:currentReplicas":{},"f:currentRevision":{},"f:observedGeneration":{},"f:readyReplicas":{},"f:replicas":{},"f:updateRevision":{},"f:updatedReplicas":{}}}`),
	})
	sts.Status = appsv1.StatefulSetStatus{
		ObservedGeneration: 1,
		Replicas:           replicas,
		ReadyReplicas:      replicas,
		CurrentReplicas:    replicas,
		UpdatedReplicas:    replicas,
		AvailableReplicas:  replicas,
		CurrentRevision:    fmt.Sprintf("%s-6b8d7f5c4", om.Name),
		UpdateRevision:     fmt.Sprintf("%s-6b8d7f5c4", om.Name),
	}
	return sts
}

func generateRealisticService(i int) *corev1.Service {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name":     appLabel,
		"app.kubernetes.io/instance": fmt.Sprintf("%s-prod", appLabel),
	}
	om := cacheObjectMeta("svc", i, true, labels)
	ip := fmt.Sprintf("10.96.%d.%d", (i/cacheOctetCount)%cacheOctetCount, i%cacheOctetCount)
	ipFamilyPolicy := corev1.IPFamilyPolicySingleStack
	internalPolicy := corev1.ServiceInternalTrafficPolicyCluster
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: om,
		Spec: corev1.ServiceSpec{
			Type:                  corev1.ServiceTypeClusterIP,
			ClusterIP:             ip,
			ClusterIPs:            []string{ip},
			IPFamilies:            []corev1.IPFamily{corev1.IPv4Protocol},
			IPFamilyPolicy:        &ipFamilyPolicy,
			InternalTrafficPolicy: &internalPolicy,
			SessionAffinity:       corev1.ServiceAffinityNone,
			Selector:              map[string]string{"app.kubernetes.io/name": appLabel},
			Ports: []corev1.ServicePort{
				{Name: "http", Protocol: corev1.ProtocolTCP, Port: cacheServicePort, TargetPort: intstr.FromInt32(cacheContainerPort)},
				{Name: "metrics", Protocol: corev1.ProtocolTCP, Port: cacheServiceMetricsPort, TargetPort: intstr.FromInt32(cacheServiceMetricsPort)},
			},
		},
	}
	attachLastAppliedAndManagedFields(svc, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/instance":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:internalTrafficPolicy":{},"f:ipFamilies":{},"f:ipFamilyPolicy":{},"f:ports":{".":{},"k:{\"port\":80,\"protocol\":\"TCP\"}":{".":{},"f:name":{},"f:port":{},"f:protocol":{},"f:targetPort":{}},"k:{\"port\":9090,\"protocol\":\"TCP\"}":{".":{},"f:name":{},"f:port":{},"f:protocol":{},"f:targetPort":{}}},"f:selector":{},"f:sessionAffinity":{},"f:type":{}}}`),
	})
	return svc
}

func generateRealisticConfigMap(i int) *corev1.ConfigMap {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name":      appLabel,
		"app.kubernetes.io/component": "config",
	}
	om := cacheObjectMeta("cm", i, true, labels)
	payload := strings.Repeat("server:\n  timeout: 30s\n  retries: 3\n", cacheConfigMapAppYAMLBytes/cacheConfigMapLineBytes+1)
	cm := &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: om,
		Data: map[string]string{
			"config.yaml":  payload,
			"logging.json": `{"level":"info","format":"json","output":"stderr"}`,
		},
	}
	attachLastAppliedAndManagedFields(cm, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:data":{".":{},"f:config.yaml":{},"f:logging.json":{}},"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/component":{},"f:app.kubernetes.io/name":{}}}}`),
	})
	return cm
}

func generateRealisticServiceAccount(i int) *corev1.ServiceAccount {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("sa", i, true, labels)
	om.Annotations = map[string]string{
		"iam.gke.io/gcp-service-account": fmt.Sprintf("workload-%05d@example-prod.iam.gserviceaccount.com", i),
	}
	sa := &corev1.ServiceAccount{
		TypeMeta:                     metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta:                   om,
		AutomountServiceAccountToken: ptr.To(true),
	}
	attachLastAppliedAndManagedFields(sa, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:automountServiceAccountToken":{},"f:metadata":{"f:annotations":{".":{},"f:iam.gke.io/gcp-service-account":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}}}`),
	})
	return sa
}

func generateRealisticPVC(i int) *corev1.PersistentVolumeClaim {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("stateful-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("pvc", i, true, labels)
	om.Annotations = map[string]string{
		"pv.kubernetes.io/bind-completed":      "yes",
		"pv.kubernetes.io/bound-by-controller": "yes",
	}
	om.Finalizers = []string{"kubernetes.io/pvc-protection"}
	storageClass := "standard-rwo"
	volMode := corev1.PersistentVolumeFilesystem
	qty := resource.MustParse("50Gi")
	pvc := &corev1.PersistentVolumeClaim{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "PersistentVolumeClaim"},
		ObjectMeta: om,
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			StorageClassName: &storageClass,
			VolumeMode:       &volMode,
			VolumeName:       fmt.Sprintf("pvc-a1b2c3d4-%05d", i),
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: qty},
			},
		},
	}
	attachLastAppliedAndManagedFields(pvc, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{},"f:pv.kubernetes.io/bind-completed":{},"f:pv.kubernetes.io/bound-by-controller":{}},"f:finalizers":{".":{},"v:\"kubernetes.io/pvc-protection\"":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:accessModes":{},"f:resources":{"f:requests":{".":{},"f:storage":{}}},"f:storageClassName":{},"f:volumeMode":{},"f:volumeName":{}}}`),
		makeManagedField("kube-controller-manager", "v1", metav1.ManagedFieldsOperationUpdate, "status", created,
			`{"f:status":{"f:accessModes":{},"f:capacity":{".":{},"f:storage":{}},"f:phase":{}}}`),
	})
	pvc.Status = corev1.PersistentVolumeClaimStatus{
		Phase:       corev1.ClaimBound,
		AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
		Capacity:    corev1.ResourceList{corev1.ResourceStorage: qty},
	}
	return pvc
}

func generateRealisticNetworkPolicy(i int) *networkingv1.NetworkPolicy {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("netpol", i, true, labels)
	tcp := corev1.ProtocolTCP
	udp := corev1.ProtocolUDP
	httpPort := intstr.FromInt32(cacheContainerPort)
	dnsPort := intstr.FromInt32(cacheDNSPort)
	np := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: om,
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app.kubernetes.io/name": appLabel}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: &httpPort}},
				From: []networkingv1.NetworkPolicyPeer{{
					PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app.kubernetes.io/part-of": "ingress"}},
				}},
			}},
			Egress: []networkingv1.NetworkPolicyEgressRule{{
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &udp, Port: &dnsPort}},
				To: []networkingv1.NetworkPolicyPeer{{
					NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": "kube-system"}},
				}},
			}},
		},
	}
	attachLastAppliedAndManagedFields(np, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "networking.k8s.io/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:egress":{},"f:ingress":{},"f:podSelector":{},"f:policyTypes":{}}}`),
	})
	return np
}

func generateRealisticPDB(i int) *policyv1.PodDisruptionBudget {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("pdb", i, true, labels)
	maxUnavail := intstr.FromInt32(cacheMaxUnavailable)
	pdb := &policyv1.PodDisruptionBudget{
		TypeMeta:   metav1.TypeMeta{APIVersion: "policy/v1", Kind: "PodDisruptionBudget"},
		ObjectMeta: om,
		Spec: policyv1.PodDisruptionBudgetSpec{
			MaxUnavailable: &maxUnavail,
			Selector:       &metav1.LabelSelector{MatchLabels: map[string]string{"app.kubernetes.io/name": appLabel}},
		},
	}
	attachLastAppliedAndManagedFields(pdb, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "policy/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:spec":{"f:maxUnavailable":{},"f:selector":{}}}`),
		makeManagedField("kube-controller-manager", "policy/v1", metav1.ManagedFieldsOperationUpdate, "status", created,
			`{"f:status":{"f:conditions":{".":{},"k:{\"type\":\"DisruptionAllowed\"}":{".":{},"f:lastTransitionTime":{},"f:message":{},"f:observedGeneration":{},"f:reason":{},"f:status":{},"f:type":{}}},"f:currentHealthy":{},"f:desiredHealthy":{},"f:disruptionsAllowed":{},"f:expectedPods":{},"f:observedGeneration":{}}}`),
	})
	pdb.Status = policyv1.PodDisruptionBudgetStatus{
		ObservedGeneration: 1,
		CurrentHealthy:     cacheReplicaCount,
		DesiredHealthy:     1,
		DisruptionsAllowed: 1,
		ExpectedPods:       cacheReplicaCount,
		Conditions: []metav1.Condition{
			{Type: policyv1.DisruptionAllowedCondition, Status: metav1.ConditionTrue, ObservedGeneration: 1, LastTransitionTime: created, Reason: policyv1.SufficientPodsReason, Message: "The disruption budget has sufficient healthy pods"},
		},
	}
	return pdb
}

func generateRealisticRole(i int) *rbacv1.Role {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("role", i, true, labels)
	role := &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: om,
		Rules: []rbacv1.PolicyRule{
			{APIGroups: []string{""}, Resources: []string{"configmaps", "pods", "services"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{"apps"}, Resources: []string{"deployments", "statefulsets"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{"coordination.k8s.io"}, Resources: []string{"leases"}, Verbs: []string{"get", "list", "watch", "create", "update", "patch"}},
		},
	}
	attachLastAppliedAndManagedFields(role, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "rbac.authorization.k8s.io/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:rules":{}}`),
	})
	return role
}

func generateRealisticRoleBinding(i int) *rbacv1.RoleBinding {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("rb", i, true, labels)
	rb := &rbacv1.RoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: om,
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "Role",
			Name:     fmt.Sprintf("role-workload-%05d", i),
		},
		Subjects: []rbacv1.Subject{{
			Kind:      "ServiceAccount",
			Name:      fmt.Sprintf("sa-workload-%05d", i),
			Namespace: om.Namespace,
		}},
	}
	attachLastAppliedAndManagedFields(rb, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "rbac.authorization.k8s.io/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:roleRef":{},"f:subjects":{}}`),
	})
	return rb
}

func generateRealisticClusterRole(i int) *rbacv1.ClusterRole {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("clusterrole", i, false, labels)
	cr := &rbacv1.ClusterRole{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "ClusterRole"},
		ObjectMeta: om,
		Rules: []rbacv1.PolicyRule{
			{APIGroups: []string{""}, Resources: []string{"namespaces", "nodes", "pods", "services", "endpoints", "configmaps"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{"apps"}, Resources: []string{"deployments", "daemonsets", "replicasets", "statefulsets"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{"networking.k8s.io"}, Resources: []string{"networkpolicies", "ingresses"}, Verbs: []string{"get", "list", "watch"}},
		},
	}
	attachLastAppliedAndManagedFields(cr, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "rbac.authorization.k8s.io/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:rules":{}}`),
	})
	return cr
}

func generateRealisticClusterRoleBinding(i int) *rbacv1.ClusterRoleBinding {
	created := cacheObjectTimestamp(i)
	appLabel := fmt.Sprintf("service-%03d", i%cacheAppServiceSpan)
	labels := map[string]string{
		"app.kubernetes.io/name": appLabel,
	}
	om := cacheObjectMeta("crb", i, false, labels)
	crb := &rbacv1.ClusterRoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "ClusterRoleBinding"},
		ObjectMeta: om,
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     fmt.Sprintf("clusterrole-workload-%05d", i),
		},
		Subjects: []rbacv1.Subject{{
			Kind:      "ServiceAccount",
			Name:      fmt.Sprintf("sa-workload-%05d", i),
			Namespace: fmt.Sprintf("team-%02d", i%cacheNamespaceCount),
		}},
	}
	attachLastAppliedAndManagedFields(crb, created, []metav1.ManagedFieldsEntry{
		makeManagedField("kubectl-client-side-apply", "rbac.authorization.k8s.io/v1", metav1.ManagedFieldsOperationUpdate, "", created,
			`{"f:metadata":{"f:annotations":{".":{},"f:kubectl.kubernetes.io/last-applied-configuration":{}},"f:labels":{".":{},"f:app.kubernetes.io/name":{}}},"f:roleRef":{},"f:subjects":{}}`),
	})
	return crb
}

type encodedKindPayload struct {
	jsonList []byte
}

func startCacheAPIServer(t testing.TB, specs []watchedKindSpec, count int) (*httptest.Server, meta.RESTMapper) {
	t.Helper()
	mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{
		appsv1.SchemeGroupVersion,
		coordinationv1.SchemeGroupVersion,
		corev1.SchemeGroupVersion,
		networkingv1.SchemeGroupVersion,
		policyv1.SchemeGroupVersion,
		rbacv1.SchemeGroupVersion,
	})
	leaseGVK := coordinationv1.SchemeGroupVersion.WithKind("Lease")
	leaseGVR := coordinationv1.SchemeGroupVersion.WithResource("leases")
	mapper.AddSpecific(leaseGVK, leaseGVR, leaseGVR, meta.RESTScopeNamespace)
	routes := make(map[string]encodedKindPayload, len(specs))
	for _, sp := range specs {
		mapper.AddSpecific(sp.gvk, sp.gvr, sp.gvr, sp.scope)
		listObj := sp.buildList(count)
		enc := clientgoscheme.Codecs.LegacyCodec(sp.gv)
		raw, err := k8sruntime.Encode(enc, listObj)
		if err != nil {
			t.Fatalf("encoding %s list: %v", sp.name, err)
		}
		routes[sp.path] = encodedKindPayload{jsonList: raw}
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		payload, ok := routes[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		q := r.URL.Query()
		if q.Get("watch") == "true" {
			if q.Get("sendInitialEvents") == "true" {
				// Return 422 Invalid so client-go falls back to a standard List+Watch.
				w.Header().Set("Content-Type", k8sruntime.ContentTypeJSON)
				w.WriteHeader(http.StatusUnprocessableEntity)
				_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure","message":"sendInitialEvents is forbidden for watch unless the WatchList feature gate is enabled","reason":"Invalid","details":{"kind":"ListOptions","causes":[{"reason":"FieldValueForbidden","message":"sendInitialEvents is forbidden for watch unless the WatchList feature gate is enabled","field":"sendInitialEvents"}]},"code":422}`))
				return
			}
			w.Header().Set("Content-Type", k8sruntime.ContentTypeJSON)
			w.WriteHeader(http.StatusOK)
			if fl, ok := w.(http.Flusher); ok {
				fl.Flush()
			}
			<-r.Context().Done()
			return
		}
		w.Header().Set("Content-Type", k8sruntime.ContentTypeJSON)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(payload.jsonList)
	}))
	t.Cleanup(srv.Close)
	return srv, mapper
}

func readLiveHeapBytes() (live uint64, heapAlloc uint64) {
	runtime.GC()
	runtime.GC()
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	samples := []runtimemetrics.Sample{{Name: liveHeapMetricName}}
	runtimemetrics.Read(samples)
	if samples[0].Value.Kind() == runtimemetrics.KindUint64 {
		return samples[0].Value.Uint64(), ms.HeapAlloc
	}
	return ms.HeapAlloc, ms.HeapAlloc
}

func readProcStatusBytes(field string) uint64 {
	f, err := os.Open(procSelfStatusPath)
	if err != nil {
		return 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if parts := strings.Fields(sc.Text()); len(parts) >= 2 && parts[0] == field {
			kb, _ := strconv.ParseUint(parts[1], 10, 64)
			return kb * kibibyteBytes
		}
	}
	return 0
}

func transformStripManagedFieldsAndLastApplied() toolscache.TransformFunc {
	stripManaged := cache.TransformStripManagedFields()
	return func(in any) (any, error) {
		out, err := stripManaged(in)
		if err != nil {
			return out, err
		}
		if obj, ok := out.(metav1.Object); ok {
			if ann := obj.GetAnnotations(); ann != nil {
				delete(ann, lastAppliedConfigAnnotation)
				if len(ann) == 0 {
					obj.SetAnnotations(nil)
				}
			}
		}
		return out, nil
	}
}

type cacheRunMeasurement struct {
	liveHeapDeltaBytes  int64
	heapAllocDeltaBytes int64
	rssBytes            uint64
	hwmBytes            uint64
}

func measureCacheWithOptions(t testing.TB, srv *httptest.Server, mapper meta.RESTMapper, specs []watchedKindSpec, count int, opts cache.Options) cacheRunMeasurement {
	t.Helper()
	scheme := setupScheme()
	opts.Scheme = scheme
	opts.Mapper = mapper
	cfg := &rest.Config{
		Host:               srv.URL,
		ContentType:        k8sruntime.ContentTypeJSON,
		QPS:                cacheClientQPS,
		Burst:              cacheClientBurst,
		DisableCompression: true,
	}
	liveBefore, allocBefore := readLiveHeapBytes()
	c, err := cache.New(cfg, opts)
	if err != nil {
		t.Fatalf("cache.New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), cacheSyncTimeout)
	defer func() {
		cancel()
		readLiveHeapBytes()
	}()
	for _, sp := range specs {
		if _, err := c.GetInformer(ctx, sp.prototype); err != nil {
			t.Fatalf("GetInformer(%s): %v", sp.name, err)
		}
	}
	go func() {
		_ = c.Start(ctx)
	}()
	if !c.WaitForCacheSync(ctx) {
		t.Fatal("cache did not sync within timeout")
	}
	for _, sp := range specs {
		listObj := sp.newListObj()
		if err := c.List(ctx, listObj); err != nil {
			t.Fatalf("cache.List(%s): %v", sp.name, err)
		}
		if got := meta.LenList(listObj); got != count {
			t.Fatalf("cache.List(%s) returned %d items, want %d", sp.name, got, count)
		}
	}
	liveAfter, allocAfter := readLiveHeapBytes()
	runtime.KeepAlive(c)
	return cacheRunMeasurement{
		liveHeapDeltaBytes:  int64(liveAfter) - int64(liveBefore),
		heapAllocDeltaBytes: int64(allocAfter) - int64(allocBefore),
		rssBytes:            readProcStatusBytes(vmRSSField),
		hwmBytes:            readProcStatusBytes(vmHWMField),
	}
}

// TestManagerCachePerObjectMemoryCost pins the per-object live-heap cost of the
// twelve cluster-scaling resource kinds the manager watches under
// ManagerCacheOptions(), and measures the share taken by managedFields versus
// the retained kubectl.kubernetes.io/last-applied-configuration annotation.
func TestManagerCachePerObjectMemoryCost(t *testing.T) {
	specs := watchedClusterScalingKinds()
	srv, mapper := startCacheAPIServer(t, specs, cacheTestObjectsPerKind)

	for _, sp := range specs {
		t.Run(sp.name, func(t *testing.T) {
			single := []watchedKindSpec{sp}
			mMgr := measureCacheWithOptions(t, srv, mapper, single, cacheTestObjectsPerKind, ManagerCacheOptions())
			perObjBytes := mMgr.liveHeapDeltaBytes / cacheTestObjectsPerKind
			per1kMiB := float64(mMgr.liveHeapDeltaBytes) * objectsPerThousand / float64(cacheTestObjectsPerKind) / mebibyteBytes
			t.Logf("%s (N=%d): liveHeap=%.2f KiB/obj (%.2f MiB/1k), heapAlloc=%.2f KiB/obj, ceiling=%.2f KiB/obj",
				sp.name, cacheTestObjectsPerKind,
				float64(perObjBytes)/kibibyteBytes,
				per1kMiB,
				float64(mMgr.heapAllocDeltaBytes)/float64(cacheTestObjectsPerKind)/kibibyteBytes,
				float64(sp.perObjectCeiling)/kibibyteBytes,
			)
			if perObjBytes <= 0 {
				t.Errorf("%s live-heap delta per object is %d bytes, want > 0", sp.name, perObjBytes)
			}
			if perObjBytes > sp.perObjectCeiling {
				t.Errorf("%s cached cost under ManagerCacheOptions() is %d bytes/object (%.2f KiB/object, %.2f MiB/1k), exceeding the %d bytes/object (%.2f KiB/object) ceiling",
					sp.name, perObjBytes, float64(perObjBytes)/kibibyteBytes, per1kMiB, sp.perObjectCeiling, float64(sp.perObjectCeiling)/kibibyteBytes)
			}
		})
	}

	totalObjects := int64(len(specs) * cacheTestObjectsPerKind)
	mRawAll := measureCacheWithOptions(t, srv, mapper, specs, cacheTestObjectsPerKind, cache.Options{})
	mMgrAll := measureCacheWithOptions(t, srv, mapper, specs, cacheTestObjectsPerKind, ManagerCacheOptions())
	bothOpts := ManagerCacheOptions()
	bothOpts.DefaultTransform = transformStripManagedFieldsAndLastApplied()
	mBothAll := measureCacheWithOptions(t, srv, mapper, specs, cacheTestObjectsPerKind, bothOpts)

	rawPerObj := mRawAll.liveHeapDeltaBytes / totalObjects
	mgrPerObj := mMgrAll.liveHeapDeltaBytes / totalObjects
	bothPerObj := mBothAll.liveHeapDeltaBytes / totalObjects

	managedFieldsSaved := mRawAll.liveHeapDeltaBytes - mMgrAll.liveHeapDeltaBytes
	lastAppliedShare := mMgrAll.liveHeapDeltaBytes - mBothAll.liveHeapDeltaBytes

	t.Logf("ALL 12 KINDS (total=%d objects, %d/kind): raw=%.2f MiB (%.2f KiB/obj) | ManagerCacheOptions=%.2f MiB (%.2f KiB/obj) | stripBoth=%.2f MiB (%.2f KiB/obj) | VmRSS=%.2f MiB VmHWM=%.2f MiB",
		totalObjects, cacheTestObjectsPerKind,
		float64(mRawAll.liveHeapDeltaBytes)/mebibyteBytes, float64(rawPerObj)/kibibyteBytes,
		float64(mMgrAll.liveHeapDeltaBytes)/mebibyteBytes, float64(mgrPerObj)/kibibyteBytes,
		float64(mBothAll.liveHeapDeltaBytes)/mebibyteBytes, float64(bothPerObj)/kibibyteBytes,
		float64(mMgrAll.rssBytes)/mebibyteBytes, float64(mMgrAll.hwmBytes)/mebibyteBytes,
	)
	t.Logf("SHARE BREAKDOWN across %d objects: managedFields stripped saves %.2f MiB (%.1f%% of raw); retained %s takes %.2f MiB (%.1f%% of ManagerCacheOptions cache)",
		totalObjects,
		float64(managedFieldsSaved)/mebibyteBytes,
		100*float64(managedFieldsSaved)/float64(mRawAll.liveHeapDeltaBytes),
		lastAppliedConfigAnnotation,
		float64(lastAppliedShare)/mebibyteBytes,
		100*float64(lastAppliedShare)/float64(mMgrAll.liveHeapDeltaBytes),
	)

	if mgrPerObj > combinedPerObjectBytesCeiling {
		t.Errorf("combined 12-kind cached cost under ManagerCacheOptions() is %d bytes/object (%.2f KiB/object), exceeding the %d bytes/object (%.2f KiB/object) ceiling",
			mgrPerObj, float64(mgrPerObj)/kibibyteBytes, combinedPerObjectBytesCeiling, float64(combinedPerObjectBytesCeiling)/kibibyteBytes)
	}
	if managedFieldsSaved <= 0 {
		t.Errorf("stripping managedFields did not reduce live heap: raw=%d bytes, ManagerCacheOptions=%d bytes",
			mRawAll.liveHeapDeltaBytes, mMgrAll.liveHeapDeltaBytes)
	}
	if lastAppliedShare <= 0 {
		t.Errorf("stripping %s did not reduce live heap: ManagerCacheOptions=%d bytes, stripBoth=%d bytes",
			lastAppliedConfigAnnotation, mMgrAll.liveHeapDeltaBytes, mBothAll.liveHeapDeltaBytes)
	}
}

// TestSoak_ManagerCacheClusterScale runs when SOAK=1 and prints the per-1,000-object
// live heap, HeapAlloc, VmRSS, VmHWM, managedFields share, and
// last-applied-configuration share across thousands of objects per kind.
//
//	SOAK=1 go test -v -run TestSoak_ManagerCacheClusterScale ./internal/controller/
func TestSoak_ManagerCacheClusterScale(t *testing.T) {
	if os.Getenv(cacheSoakEnabledEnv) != cacheSoakEnabledValue {
		t.Skipf("cluster-scale cache soak runs only with %s=%s", cacheSoakEnabledEnv, cacheSoakEnabledValue)
	}
	count := cacheSoakObjectsPerKind
	if raw := os.Getenv(cacheSoakObjectsEnv); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed <= 0 {
			t.Fatalf("invalid %s=%q: %v", cacheSoakObjectsEnv, raw, err)
		}
		count = parsed
	}

	specs := watchedClusterScalingKinds()
	srv, mapper := startCacheAPIServer(t, specs, count)

	t.Logf("%-22s | %10s | %12s | %12s | %10s | %10s", "KIND", "RAW MiB/1k", "CACHED MiB/1k", "NO-ANN MiB/1k", "MF SAVED", "ANN SHARE")
	for _, sp := range specs {
		single := []watchedKindSpec{sp}
		mRaw := measureCacheWithOptions(t, srv, mapper, single, count, cache.Options{})
		mMgr := measureCacheWithOptions(t, srv, mapper, single, count, ManagerCacheOptions())
		bothOpts := ManagerCacheOptions()
		bothOpts.DefaultTransform = transformStripManagedFieldsAndLastApplied()
		mBoth := measureCacheWithOptions(t, srv, mapper, single, count, bothOpts)

		rawMiB1k := float64(mRaw.liveHeapDeltaBytes) * objectsPerThousand / float64(count) / mebibyteBytes
		mgrMiB1k := float64(mMgr.liveHeapDeltaBytes) * objectsPerThousand / float64(count) / mebibyteBytes
		bothMiB1k := float64(mBoth.liveHeapDeltaBytes) * objectsPerThousand / float64(count) / mebibyteBytes
		mfSavedPct := 100 * (rawMiB1k - mgrMiB1k) / rawMiB1k
		annSharePct := 100 * (mgrMiB1k - bothMiB1k) / mgrMiB1k

		t.Logf("%-22s | %10.2f | %12.2f | %12.2f | %9.1f%% | %9.1f%%",
			sp.name, rawMiB1k, mgrMiB1k, bothMiB1k, mfSavedPct, annSharePct)
	}

	totalObjects := int64(len(specs) * count)
	mRawAll := measureCacheWithOptions(t, srv, mapper, specs, count, cache.Options{})
	mMgrAll := measureCacheWithOptions(t, srv, mapper, specs, count, ManagerCacheOptions())
	bothOpts := ManagerCacheOptions()
	bothOpts.DefaultTransform = transformStripManagedFieldsAndLastApplied()
	mBothAll := measureCacheWithOptions(t, srv, mapper, specs, count, bothOpts)

	t.Logf("SOAK TOTAL (%d objects across %d kinds): raw=%.2f MiB (%.2f KiB/obj) | ManagerCacheOptions=%.2f MiB (%.2f KiB/obj) | stripBoth=%.2f MiB (%.2f KiB/obj) | VmRSS=%.2f MiB VmHWM=%.2f MiB",
		totalObjects, len(specs),
		float64(mRawAll.liveHeapDeltaBytes)/mebibyteBytes, float64(mRawAll.liveHeapDeltaBytes)/float64(totalObjects)/kibibyteBytes,
		float64(mMgrAll.liveHeapDeltaBytes)/mebibyteBytes, float64(mMgrAll.liveHeapDeltaBytes)/float64(totalObjects)/kibibyteBytes,
		float64(mBothAll.liveHeapDeltaBytes)/mebibyteBytes, float64(mBothAll.liveHeapDeltaBytes)/float64(totalObjects)/kibibyteBytes,
		float64(mMgrAll.rssBytes)/mebibyteBytes, float64(mMgrAll.hwmBytes)/mebibyteBytes,
	)
}

func BenchmarkManagerCachePerKind(b *testing.B) {
	specs := watchedClusterScalingKinds()
	srv, mapper := startCacheAPIServer(b, specs, cacheTestObjectsPerKind)

	for _, sp := range specs {
		b.Run(sp.name, func(b *testing.B) {
			single := []watchedKindSpec{sp}
			b.ReportAllocs()
			var last cacheRunMeasurement
			for b.Loop() {
				last = measureCacheWithOptions(b, srv, mapper, single, cacheTestObjectsPerKind, ManagerCacheOptions())
			}
			b.ReportMetric(float64(last.liveHeapDeltaBytes)/float64(cacheTestObjectsPerKind), "live-bytes/object")
			b.ReportMetric(float64(last.liveHeapDeltaBytes)*objectsPerThousand/float64(cacheTestObjectsPerKind)/mebibyteBytes, "MiB/1k-objects")
		})
	}
}
