package controller

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func readerDeployment(agent *agentv1alpha1.PlatformAgent, rd a2aSupervisorReader, rolledOut bool) *appsv1.Deployment {
	container := rd.container
	if container == "" {
		container = "main"
	}
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: agent.Namespace, Name: rd.name, Generation: 2},
		Spec: appsv1.DeploymentSpec{Replicas: ptr.To[int32](1), Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{
			Containers: []corev1.Container{{Name: container, Image: rd.image}}}}},
		Status: appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1},
	}
	if !rolledOut {
		// Mid-roll: an old pod still up beside the new one.
		dep.Status.Replicas = 2
	}
	return dep
}

func TestADeploymentIsOnItsTemplateOnlyWhenTheRolloutHasFinished(t *testing.T) {
	agent := a2aTestAgent()
	rd := a2aSupervisorReader{name: "x", image: "img"}
	if !a2aDeploymentFullyOnTemplate(readerDeployment(agent, rd, true)) {
		t.Error("a finished rollout read as unfinished")
	}
	for name, mutate := range map[string]func(*appsv1.Deployment){
		"an old pod left":       func(d *appsv1.Deployment) { d.Status.Replicas = 2 },
		"generation unobserved": func(d *appsv1.Deployment) { d.Status.ObservedGeneration = 1 },
		"a replica not updated": func(d *appsv1.Deployment) { d.Status.UpdatedReplicas = 0 },
		"none available":        func(d *appsv1.Deployment) { d.Status.AvailableReplicas = 0 },
	} {
		d := readerDeployment(agent, rd, true)
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
		for _, rd := range a2aProfileSupervisorReaders(agent) {
			objs = append(objs, readerDeployment(agent, rd, rolledOut))
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
	// The status writers save the latch (setProfileSupervisorFormCondition;
	// TestTheSupervisorFormLatchIsPersistedWhenNothingElseChanged); here it
	// is applied as they would.
	setProfileSupervisorFormCondition(agent, true, metav1.Now())
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
	for _, rd := range a2aProfileSupervisorReaders(agent) {
		objs = append(objs, readerDeployment(agent, rd, true))
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
	// The run fails unless the durable reads the third filter back, since the
	// switch waits on this Job's success.
	if !strings.Contains(script, "consumer info TASKS "+a2aGatewayRelayDurable+" --json") || !strings.Contains(script, "exit 1") {
		t.Error("the script doesn't fail a run whose relay durable lacks the third filter")
	}
	if strings.Contains(a2aProvisionScript(agent), "consumer info TASKS "+a2aGatewayRelayDurable+" --json") {
		t.Error("the default script reads the relay back for a filter it doesn't write")
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

// A reader that is fully rolled out but on an image this operator doesn't
// render (an older release's, left by a pass that failed before re-applying
// it, or a pinned agent image's bridge) keeps the gate closed; a StatefulSet
// agent is read by its own rollout; an MCP server that isn't deployed doesn't
// hold the gate.
func TestTheGateReadsEachReadersImageAndWorkloadKind(t *testing.T) {
	agent := a2aTestAgent()
	ctx := context.Background()
	readers := a2aProfileSupervisorReaders(agent)

	old := readerDeployment(agent, readers[0], true)
	old.Spec.Template.Spec.Containers[0].Image = "registry/a2a-gateway:older"
	r := supervisorFormReconciler(t, old, readerDeployment(agent, readers[1], true))
	if ok, name, err := r.a2aSupervisorReadersOnTemplate(ctx, agent); err != nil || ok || name != readers[0].name {
		t.Errorf("an older gateway image fully rolled out: ok %v blocker %q err %v, want closed on the gateway", ok, name, err)
	}

	bridge := readers[1]
	sts := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Namespace: agent.Namespace, Name: bridge.name, Generation: 1},
		Spec: appsv1.StatefulSetSpec{Replicas: ptr.To[int32](2), Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{
			Containers: []corev1.Container{{Name: bridge.container, Image: bridge.image}}}}},
		Status: appsv1.StatefulSetStatus{ObservedGeneration: 1, Replicas: 2, UpdatedReplicas: 2, ReadyReplicas: 2, CurrentRevision: "r1", UpdateRevision: "r1"},
	}
	r = supervisorFormReconciler(t, readerDeployment(agent, readers[0], true), sts)
	if ok, name, err := r.a2aSupervisorReadersOnTemplate(ctx, agent); err != nil || !ok {
		t.Errorf("a StatefulSet agent fully rolled out: ok %v blocker %q err %v, want open", ok, name, err)
	}
	sts.Status.CurrentRevision = "r0"
	r = supervisorFormReconciler(t, readerDeployment(agent, readers[0], true), sts)
	if ok, _, _ := r.a2aSupervisorReadersOnTemplate(ctx, agent); ok {
		t.Error("a StatefulSet agent mid-roll opened the gate")
	}

	t.Setenv(a2aMCPDoorEnvVar, "true")
	if len(a2aProfileSupervisorReaders(agent)) != 3 {
		t.Fatal("the MCP door didn't add the MCP reader")
	}
	r = supervisorFormReconciler(t, readerDeployment(agent, readers[0], true), readerDeployment(agent, readers[1], true))
	if ok, name, err := r.a2aSupervisorReadersOnTemplate(ctx, agent); err != nil || !ok {
		t.Errorf("MCP door on but MCP not deployed: ok %v blocker %q err %v, want open", ok, name, err)
	}
}
