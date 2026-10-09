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

package webhook

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation/field"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
)

func proxyResourcesAgent(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		Spec: agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				CredentialProxy: &agentv1alpha1.CredentialProxySpec{Resources: override},
			},
		}},
	}
}

func fieldErrorMessage(t *testing.T, err error, path string) string {
	t.Helper()
	assertFieldError(t, err, path)
	statusErr, ok := err.(*apierrors.StatusError)
	if !ok {
		t.Fatalf("expected *apierrors.StatusError, got %T", err)
	}
	for _, cause := range statusErr.ErrStatus.Details.Causes {
		if cause.Field == path {
			return cause.Message
		}
	}
	return ""
}

// The floor at the operator's defaults: 192Mi + 128Mi + 2 × (128Mi + 6 × 8Mi)
// (docs/designs/credential-proxy-child-memory-budget.md §2.5). Under it the
// broker does not run a smaller budget: it turns the budget off and admits by
// the slot cap alone, and the refusal says so.
func TestCredentialProxyMemoryLimitBelowTheFloorIsRefusedWithTheNumbers(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.limits.memory")
	for _, want := range []string{"a 512Mi memory limit is under the 672Mi floor at which the budget admits 2 commands", "turns the budget off and admits by the slot cap alone"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q does not say %q", msg, want)
		}
	}
}

// A limit under the floor is refused once. It also sits under the default
// 512Mi request, and is set without requests.memory; neither restates it,
// because raising requests.memory cannot make it valid and the
// limit-without-request note is advice about a quantity already refused.
func TestCredentialProxyMemoryLimitBelowTheFloorIsRefusedOnce(t *testing.T) {
	path := field.NewPath("spec", "deployment", "credentialProxy", "resources")
	errs, warnings := controller.ValidateCredentialProxyResources(proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("400Mi")},
	}).Spec.Deployment, path)
	if len(errs) != 1 || errs[0].Field != "spec.deployment.credentialProxy.resources.limits.memory" || !strings.Contains(errs[0].Detail, "672Mi floor") {
		t.Errorf("expected the floor refusal alone, got %v", errs)
	}
	for _, w := range warnings {
		if strings.Contains(w, "resources.limits") {
			t.Errorf("the refused limit drew a band warning: %q", w)
		}
	}
}

// A crossed memory pair is refused once. 4Gi against the default 500m CPU
// request is 8 GiB per vCPU, outside the Autopilot band, but the band warning
// would restate a pair already refused.
func TestCredentialProxyCrossedMemoryPairIsRefusedWithoutABandWarning(t *testing.T) {
	path := field.NewPath("spec", "deployment", "credentialProxy", "resources")
	errs, warnings := controller.ValidateCredentialProxyResources(proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
	}).Spec.Deployment, path)
	if len(errs) != 1 || errs[0].Field != "spec.deployment.credentialProxy.resources.requests.memory" {
		t.Errorf("expected the crossed-pair refusal alone, got %v", errs)
	}
	if len(warnings) != 0 {
		t.Errorf("the crossed pair drew band warnings: %v", warnings)
	}
}

// A limit exactly at the floor is admitted. Set without requests.memory it
// draws the Autopilot clamp note, and nothing else.
func TestCredentialProxyMemoryLimitAtTheFloorIsAdmitted(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("672Mi")},
	}))
	if err != nil {
		t.Fatalf("a limit exactly at the floor was refused: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "limits.memory is set without requests.memory") {
		t.Errorf("expected the limits.memory clamp note alone, got %v", warnings)
	}
}

// The case the field exists for (#2324): the limit raised past the 2Gi default
// and nothing else. Admitted, with one note: GKE Autopilot without bursting sets
// the limit equal to the 512Mi default request, so there the raise has no effect.
func TestCredentialProxyRaisedLimitIsAdmittedWithTheLimitWithoutRequestNote(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
	}))
	if err != nil {
		t.Fatalf("a 3Gi memory limit was refused: %v", err)
	}
	if len(warnings) != 1 {
		t.Fatalf("expected one warning, got %v", warnings)
	}
	for _, want := range []string{
		"spec.deployment.credentialProxy.resources.limits.memory is set without requests.memory",
		"GKE Autopilot without bursting sets a container's limits equal to its requests",
		"runs at the 512Mi request and this limit has no effect",
		"set requests.memory to the same value, or enable bursting",
	} {
		if !strings.Contains(warnings[0], want) {
			t.Errorf("warning %q does not say %q", warnings[0], want)
		}
	}
}

// With requests.memory set beside it the limit stands on Autopilot too, and
// 2Gi per the default 500m request is inside the band.
func TestCredentialProxyLimitWithItsRequestDrawsNoNote(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 0 {
		t.Errorf("expected no warnings, got %v", warnings)
	}
}

// 8Gi against the default 1 CPU limit is 8 GiB per vCPU, but Autopilot applies
// the band to requests only: the limit draws the clamp note and no band text.
func TestCredentialProxyEightGiLimitAloneDrawsTheNoteAndNoBandWarning(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("8Gi")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "limits.memory is set without requests.memory") {
		t.Fatalf("expected the limits.memory note alone, got %v", warnings)
	}
	if strings.Contains(warnings[0], "GiB per vCPU") {
		t.Errorf("warning %q carries band text for a limit", warnings[0])
	}
}

// Each limit set without its request draws a note of its own, naming its
// own request.
func TestCredentialProxyEachLimitWithoutItsRequestIsNoted(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2"), corev1.ResourceMemory: resource.MustParse("4Gi")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 2 {
		t.Fatalf("expected two notes, got %v", warnings)
	}
	if !strings.Contains(warnings[0], "limits.cpu is set without requests.cpu") || !strings.Contains(warnings[0], "at the 500m request") {
		t.Errorf("first note %q does not name the cpu request", warnings[0])
	}
	if !strings.Contains(warnings[1], "limits.memory is set without requests.memory") {
		t.Errorf("second note %q does not name the memory request", warnings[1])
	}
}

// A limit equal to the request Autopilot would set it to changes nothing
// there, so it is not noted.
func TestCredentialProxyLimitEqualToTheRequestIsNotNoted(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")},
	}))
	if err != nil || len(warnings) != 0 {
		t.Errorf("err=%v warnings=%v, expected neither", err, warnings)
	}
}

// A request raised past a limit the CR never wrote: the merged result is what
// is checked, and the error names the operator's default as the other side.
func TestCredentialProxyRequestAboveTheDefaultLimitIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.requests.memory")
	if !strings.Contains(msg, "operator's default 2Gi memory limit") || !strings.Contains(msg, "set limits.memory as well") {
		t.Errorf("message %q does not name the default limit it collides with", msg)
	}
}

// The mirror image: a CPU limit lowered under the request the CR never wrote.
func TestCredentialProxyLimitBelowTheDefaultRequestIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.limits.cpu")
	if !strings.Contains(msg, "operator's default 500m cpu request") {
		t.Errorf("message %q does not name the default request it collides with", msg)
	}
}

func TestCredentialProxyEphemeralStorageRequestAboveItsLimitIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("4Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("3Gi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.requests.ephemeral-storage")
	if !strings.Contains(msg, "3Gi ephemeral-storage limit set beside it") {
		t.Errorf("message %q does not name the limit set in the same override", msg)
	}
}

// The request pair is checked too: it is the pair Autopilot resizes, and the
// case the issue describes is memory raised at a 500m CPU request.
func TestCredentialProxyRequestPairOutsideTheAutopilotBandWarns(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 1 {
		t.Fatalf("expected one warning on the requests pair, got %v", warnings)
	}
	for _, want := range []string{"spec.deployment.credentialProxy.resources.requests", "8.00 GiB per vCPU", "raises the smaller side", "quota preflight"} {
		if !strings.Contains(warnings[0], want) {
			t.Errorf("warning %q does not say %q", warnings[0], want)
		}
	}
	if strings.Contains(warnings[0], "sets the limits equal to the requests") {
		t.Errorf("requests-pair warning %q carries the limits-pair text", warnings[0])
	}
}

// The default 512Mi request against a 2-CPU request is 0.25 GiB per vCPU,
// below the band's lower edge.
func TestCredentialProxyBelowTheAutopilotBandWarns(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
	}))
	if err != nil {
		t.Fatalf("expected admission, got: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "resources.requests") || !strings.Contains(warnings[0], "0.25 GiB per vCPU") {
		t.Errorf("expected one requests-pair warning at 0.25 GiB per vCPU, got %v", warnings)
	}
}

// An empty override block is the field present and saying nothing; the
// defaults are what render, and they pass every check.
func TestCredentialProxyEmptyOverrideIsAdmitted(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	for _, override := range []*corev1.ResourceRequirements{nil, {}} {
		warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(override))
		if err != nil || len(warnings) != 0 {
			t.Errorf("override %v: err=%v warnings=%v, expected neither", override, err, warnings)
		}
	}
}

// claims has nowhere to go: the proxy pod declares no resourceClaims, so the
// render drops the key, and admitting it would show a CR a setting it does not
// have.
func TestCredentialProxyClaimsAreRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Claims: []corev1.ResourceClaim{{Name: "gpu"}},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.claims")
	if !strings.Contains(msg, "declares no resourceClaims") {
		t.Errorf("message %q does not say why claims cannot take effect", msg)
	}
}

// hugepages must have request equal to limit, which the API server enforces
// and this validation would otherwise not; the proxy declares no hugepages, so
// the name is refused outright, on both sides, whatever the quantities.
func TestCredentialProxyHugepagesAreRefusedByName(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{"hugepages-2Mi": resource.MustParse("2Mi")},
		Limits:   corev1.ResourceList{"hugepages-2Mi": resource.MustParse("4Mi")},
	}))
	for _, side := range []string{"requests", "limits"} {
		msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources."+side+".hugepages-2Mi")
		if !strings.Contains(msg, "cpu, memory and ephemeral-storage") {
			t.Errorf("%s: message %q does not name the accepted resources", side, msg)
		}
	}
}

// An extended resource requested with no limit is a Deployment the API server
// refuses as Invalid; refused here by name instead.
func TestCredentialProxyExtendedResourceIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{"nvidia.com/gpu": resource.MustParse("1")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.requests.nvidia.com/gpu")
	if !strings.Contains(msg, "declares no other resource") {
		t.Errorf("message %q does not say the proxy declares nothing else", msg)
	}
}

// The CRD's quantity pattern admits a leading minus; the API server refuses
// the Deployment that carries one.
func TestCredentialProxyNegativeRequestIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("-1Mi")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.requests.memory")
	if !strings.Contains(msg, "must not be negative") {
		t.Errorf("message %q does not refuse the negative quantity", msg)
	}
}

// A zero limit on a name with no default request beside it is not caught by
// the crossed-pair check, and an ephemeral-storage limit of zero evicts the
// pod on its first write.
func TestCredentialProxyZeroLimitIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("0")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.limits.ephemeral-storage")
	if !strings.Contains(msg, "a limit of zero") {
		t.Errorf("message %q does not refuse the zero limit", msg)
	}
}

// 9Pi of memory per 500m CPU is past where memory × 1000 fits an int64; the
// band arithmetic must not wrap into a negative ratio.
func TestCredentialProxyBandArithmeticDoesNotWrapAtPetabytes(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	warnings, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("9Pi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("9Pi")},
	}))
	if err != nil {
		t.Fatalf("a 9Pi pair is representable and above the floor, got: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "18874368.00 GiB per vCPU") {
		t.Errorf("expected one warning at 18874368.00 GiB per vCPU, got %v", warnings)
	}
}

// 10E is a valid quantity and more bytes than an int64 holds, so the
// Downward API cannot hand it to the broker as a byte count.
func TestCredentialProxyUnrepresentableMemoryLimitIsRefused(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("10E")},
	}))
	msg := fieldErrorMessage(t, err, "spec.deployment.credentialProxy.resources.limits.memory")
	if !strings.Contains(msg, "not a representable byte count") || strings.Contains(msg, "floor") {
		t.Errorf("message %q should refuse 10E as unrepresentable and nothing else", msg)
	}
}

// 10E is a valid CPU quantity whose millicore value an int64 cannot hold, so
// MilliValue wraps it to 0 and the scheduler would see no CPU request. Both
// sides are refused as unrepresentable, and the equal pair draws no crossed
// refusal on top.
func TestCredentialProxyUnrepresentableCPUIsRefused(t *testing.T) {
	path := field.NewPath("spec", "deployment", "credentialProxy", "resources")
	errs, warnings := controller.ValidateCredentialProxyResources(proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10E")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10E")},
	}).Spec.Deployment, path)
	if len(errs) != 2 {
		t.Fatalf("expected the two cpu refusals alone, got %v", errs)
	}
	for i, want := range []string{"spec.deployment.credentialProxy.resources.requests.cpu", "spec.deployment.credentialProxy.resources.limits.cpu"} {
		if errs[i].Field != want || !strings.Contains(errs[i].Error(), `Invalid value: "10E": is not a CPU count the scheduler can represent in millicores`) {
			t.Errorf("error %d = %v, want the CPU unrepresentable refusal on %s", i, errs[i], want)
		}
	}
	if len(warnings) != 0 {
		t.Errorf("a refused cpu drew warnings: %v", warnings)
	}
	val := &PlatformAgentCustomValidator{}
	if _, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10E")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10E")},
	})); err == nil || !strings.Contains(err.Error(), "not a CPU count the scheduler can represent in millicores") {
		t.Errorf("the webhook admitted a 10E cpu pair or refused it for another reason: %v", err)
	}
}

// A resource name is cut where it enters the field path, so the ErrorList the
// webhook returns keeps each refusal's reason rather than 33,000 characters of
// the author's key.
func TestCredentialProxyLongResourceNameKeepsTheReason(t *testing.T) {
	val := &PlatformAgentCustomValidator{}
	name := corev1.ResourceName("example.com/" + strings.Repeat("x", 33000))
	_, err := val.ValidateCreate(context.Background(), proxyResourcesAgent(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{name: resource.MustParse("1")},
	}))
	if err == nil {
		t.Fatal("an undeclared resource name was admitted")
	}
	msg := err.Error()
	if len(msg) > len(name) {
		t.Errorf("the refusal is %d characters, longer than the name it should have cut", len(msg))
	}
	for _, want := range []string{"...: Forbidden", "accepts cpu, memory and ephemeral-storage only"} {
		if !strings.Contains(msg, want) {
			t.Errorf("the refusal does not say %q: %q", want, msg)
		}
	}
}
