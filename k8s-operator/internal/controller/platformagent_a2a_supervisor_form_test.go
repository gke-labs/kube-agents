package controller

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func readerDeployment(agent *agentv1alpha1.PlatformAgent, name string, rolledOut bool) *appsv1.Deployment {
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: agent.Namespace, Name: name, Generation: 2},
		Spec:       appsv1.DeploymentSpec{Replicas: ptr.To[int32](1)},
		Status:     appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1},
	}
	if !rolledOut {
		// Mid-roll: an old pod still up beside the new one.
		dep.Status.Replicas = 2
	}
	return dep
}

func TestADeploymentIsOnItsTemplateOnlyWhenTheRolloutHasFinished(t *testing.T) {
	agent := a2aTestAgent()
	if !a2aDeploymentFullyOnTemplate(readerDeployment(agent, "x", true)) {
		t.Error("a finished rollout read as unfinished")
	}
	for name, mutate := range map[string]func(*appsv1.Deployment){
		"an old pod left":       func(d *appsv1.Deployment) { d.Status.Replicas = 2 },
		"generation unobserved": func(d *appsv1.Deployment) { d.Status.ObservedGeneration = 1 },
		"a replica not updated": func(d *appsv1.Deployment) { d.Status.UpdatedReplicas = 0 },
		"none available":        func(d *appsv1.Deployment) { d.Status.AvailableReplicas = 0 },
	} {
		d := readerDeployment(agent, "x", true)
		mutate(d)
		if a2aDeploymentFullyOnTemplate(d) {
			t.Errorf("%s: read as rolled out", name)
		}
	}
}

func supervisorFormReconciler(t *testing.T, objs ...client.Object) *PlatformAgentReconciler {
	t.Helper()
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: cl.Scheme()}
}

// The move to the six-token subject (#5301) goes in order: nothing while a
// reader is mid-roll; the relay's third filter once every reader is on its
// template; the dispatcher's switch only once the Job that applies the filter
// has succeeded; and once switched, it stays switched (the latch) even while a
// later roll has a reader mid-way.
func TestTheSupervisorFormMovesInOrderAndLatches(t *testing.T) {
	agent := a2aTestAgent()
	readers := func(rolledOut bool) []client.Object {
		var objs []client.Object
		for _, name := range a2aProfileSupervisorReaders(agent) {
			objs = append(objs, readerDeployment(agent, name, rolledOut))
		}
		return objs
	}
	ctx := context.Background()

	r := supervisorFormReconciler(t, readers(false)...)
	if filter, form, err := r.a2aProfileSupervisorFormFor(ctx, agent); err != nil || filter || form != "" {
		t.Fatalf("a reader mid-roll: filter %v form %q err %v, want neither", filter, form, err)
	}

	r = supervisorFormReconciler(t, readers(true)...)
	if filter, form, err := r.a2aProfileSupervisorFormFor(ctx, agent); err != nil || !filter || form != "" {
		t.Fatalf("readers on template, no Job yet: filter %v form %q err %v, want the filter alone", filter, form, err)
	}

	job := a2aProvisionJobForm(agent, true)
	job.Status.Succeeded = 1
	r = supervisorFormReconciler(t, append(readers(true), job)...)
	if filter, form, err := r.a2aProfileSupervisorFormFor(ctx, agent); err != nil || !filter || form != a2aProfileSupervisorFormValue {
		t.Fatalf("filter Job succeeded: filter %v form %q err %v, want both", filter, form, err)
	}
	if !meta.IsStatusConditionTrue(agent.Status.Conditions, a2aProfileSupervisorFormCondition) {
		t.Fatal("the switch didn't latch")
	}

	r = supervisorFormReconciler(t, readers(false)...)
	if filter, form, err := r.a2aProfileSupervisorFormFor(ctx, agent); err != nil || !filter || form != a2aProfileSupervisorFormValue {
		t.Fatalf("latched, a later roll mid-way: filter %v form %q err %v, want both kept", filter, form, err)
	}
}

// A Job for the two-filter render doesn't count: the switch waits for the one
// that writes the third filter.
func TestTheSwitchWaitsForTheThirdFiltersJob(t *testing.T) {
	agent := a2aTestAgent()
	var objs []client.Object
	for _, name := range a2aProfileSupervisorReaders(agent) {
		objs = append(objs, readerDeployment(agent, name, true))
	}
	old := a2aProvisionJobForm(agent, false)
	old.Status.Succeeded = 1
	if old.Name == a2aProvisionJobForm(agent, true).Name {
		t.Fatal("the third filter didn't change the provision Job's digested name")
	}
	r := supervisorFormReconciler(t, append(objs, old)...)
	if _, form, err := r.a2aProfileSupervisorFormFor(context.Background(), agent); err != nil || form != "" {
		t.Fatalf("form %q err %v; a two-filter Job's success switched the dispatcher", form, err)
	}
}

func TestTheRelayDurableTakesTheThirdFilterOnlyWhenAsked(t *testing.T) {
	agent := a2aTestAgent()
	if strings.Contains(a2aProvisionScript(agent), a2aGatewayProfileSupervisorFilter) {
		t.Error("the default script writes the six-token filter")
	}
	script := a2aProvisionScriptForm(agent, true)
	for _, f := range []string{a2aGatewayEventsFilter, a2aGatewaySupervisorFilter, a2aGatewayProfileSupervisorFilter} {
		if !strings.Contains(script, "--filter='"+f+"'") {
			t.Errorf("the relay durable lacks filter %q", f)
		}
	}
}

func TestTheDispatchConfigCarriesTheSwitchItIsGiven(t *testing.T) {
	agent := a2aTestAgent()
	for _, form := range []string{"", a2aProfileSupervisorFormValue} {
		cm, err := buildA2ADispatchConfigMapForm(agent, nil, nil, nil, form)
		if err != nil {
			t.Fatal(err)
		}
		var got map[string]any
		if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &got); err != nil {
			t.Fatal(err)
		}
		v, present := got["profileSupervisorForm"]
		if form == "" && present {
			t.Errorf("an unset form rendered %v", v)
		}
		if form != "" && v != form {
			t.Errorf("form %q rendered %v", form, v)
		}
	}
}
