package controller

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

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
	setProfileSupervisorFormCondition(agent, true, "", metav1.Now())
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
	versions := map[string]string{}
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
		versions[form], _ = got["version"].(string)
	}
	// The version follows the content: it moves when the switch flips, and
	// with the switch unset it is the version an older operator renders.
	if versions[""] == versions[a2aProfileSupervisorFormValue] {
		t.Errorf("flipping the switch left the version at %s", versions[""])
	}
	older, err := desiredDispatchConfigWith(agent, nil, nil, nil)
	if err != nil || older.Version != versions[""] {
		t.Errorf("an unset switch rendered version %s, the config without it %s (%v)", versions[""], older.Version, err)
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

// A reader on an image other than this operator's render holds the install on
// the five-token form, and the condition names it (False,
// ReaderNotOnReleaseImage) so an admin can see why; a rollout still in flight
// is no hold. The latch, once True, is never replaced by a hold.
func TestAReaderOnAnOlderImageHoldsTheInstallAndSaysSo(t *testing.T) {
	ctx := context.Background()
	// An install pinning an older platform-agent image renders that release's
	// bridge: a standing hold, named.
	agent := a2aTestAgent()
	readers := a2aProfileSupervisorReaders(agent)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Image: "registry.example/kube-agents/" + platformAgentImageName + ":v0.0.1-older"}
	pinnedReaders := a2aProfileSupervisorReaders(agent)
	if pinnedReaders[1].rendered == pinnedReaders[1].image {
		t.Fatalf("the pinned agent image rendered this release's bridge %q", pinnedReaders[1].rendered)
	}
	pinned := readerDeployment(agent, readers[1], true)
	pinned.Spec.Template.Spec.Containers[0].Image = pinnedReaders[1].rendered
	r := supervisorFormReconciler(t, readerDeployment(agent, readers[0], true), pinned)
	_, form, heldBy, err := r.a2aProfileSupervisorFormAndHold(ctx, agent)
	if err != nil || form != "" || heldBy != readers[1].name {
		t.Fatalf("a pinned older bridge: form %q heldBy %q err %v, want held by %q", form, heldBy, err, readers[1].name)
	}
	if profileSupervisorFormConditionCurrent(agent, false, heldBy) {
		t.Fatal("a hold with no condition yet read as current, so it would never be written")
	}
	setProfileSupervisorFormCondition(agent, false, heldBy, metav1.Now())
	c := meta.FindStatusCondition(agent.Status.Conditions, a2aProfileSupervisorFormCondition)
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != a2aProfileSupervisorFormHeldReason || !strings.Contains(c.Message, readers[1].name) {
		t.Fatalf("hold condition = %+v", c)
	}

	agent.Spec.Deployment = nil
	r = supervisorFormReconciler(t, readerDeployment(agent, readers[0], false), readerDeployment(agent, readers[1], true))
	if _, _, heldBy, _ := r.a2aProfileSupervisorFormAndHold(ctx, agent); heldBy != "" {
		t.Errorf("a gateway mid-roll was reported as a hold by %q", heldBy)
	}
	// The first pass of an upgrade reads the gateway before this pass applies
	// it: still the older release's image, but this release renders the
	// gateway itself, so that is a rollout to come, not a hold.
	older := readerDeployment(agent, readers[0], true)
	older.Spec.Template.Spec.Containers[0].Image = "registry.example/kube-agents/a2a-gateway:v0.0.1-older"
	r = supervisorFormReconciler(t, older, readerDeployment(agent, readers[1], true))
	if _, form, heldBy, _ := r.a2aProfileSupervisorFormAndHold(ctx, agent); heldBy != "" || form != "" {
		t.Errorf("a gateway not yet re-applied: form %q, reported as a hold by %q", form, heldBy)
	}
	setProfileSupervisorFormCondition(agent, false, "", metav1.Now())
	if meta.FindStatusCondition(agent.Status.Conditions, a2aProfileSupervisorFormCondition) != nil {
		t.Error("the hold condition outlived the hold")
	}

	setProfileSupervisorFormCondition(agent, true, "", metav1.Now())
	setProfileSupervisorFormCondition(agent, false, "anything", metav1.Now())
	if !meta.IsStatusConditionTrue(agent.Status.Conditions, a2aProfileSupervisorFormCondition) {
		t.Error("a hold replaced the latch")
	}
}

// cutoffEnvOf is the cutoff env on the gateway's and the MCP server's
// containers, by workload, with whether each carries it.
func cutoffEnvOf(agent *agentv1alpha1.PlatformAgent) map[string]*corev1.EnvVar {
	out := map[string]*corev1.EnvVar{"gateway": nil, "mcp": nil}
	for name, spec := range map[string]corev1.PodSpec{
		"gateway": buildA2AGatewayDeployment(agent).Spec.Template.Spec,
		"mcp":     buildA2AMCPDeployment(agent).Spec.Template.Spec,
	} {
		for _, c := range spec.Containers {
			for i := range c.Env {
				if c.Env[i].Name == a2aProfileSupervisorCutoffEnvVar {
					out[name] = &c.Env[i]
				}
			}
		}
	}
	return out
}

// readerTemplates is the gateway's and the MCP server's pod templates, as
// JSON, the bytes a roll is decided on.
func readerTemplates(t *testing.T, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	b, err := json.Marshal([]corev1.PodTemplateSpec{
		buildA2AGatewayDeployment(agent).Spec.Template,
		buildA2AMCPDeployment(agent).Spec.Template,
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// Until the install latches the six-token form, neither reader carries the
// cutoff, and its pod template is byte for byte what an install with no
// ProfileSupervisorForm condition renders, held or not: nothing rolls for it
// (#5301 PR 4).
func TestAnUnlatchedInstallRendersNoCutoffAndRollsNothing(t *testing.T) {
	want := readerTemplates(t, a2aTestAgent())
	held := a2aTestAgent()
	setProfileSupervisorFormCondition(held, false, "kube-agents-agent", metav1.Now())
	if !meta.IsStatusConditionFalse(held.Status.Conditions, a2aProfileSupervisorFormCondition) {
		t.Fatal("the hold didn't set the condition False")
	}
	noAnchor := a2aTestAgent()
	meta.SetStatusCondition(&noAnchor.Status.Conditions, metav1.Condition{Type: a2aProfileSupervisorFormCondition,
		Status: metav1.ConditionTrue, Reason: a2aProfileSupervisorFormReason})
	noAnchor.Status.Conditions[len(noAnchor.Status.Conditions)-1].LastTransitionTime = metav1.Time{}
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"no condition":           a2aTestAgent(),
		"held on the five-token": held,
		"latched with no anchor": noAnchor,
	} {
		for workload, env := range cutoffEnvOf(agent) {
			if env != nil {
				t.Errorf("%s: the %s carries %s=%q, want it absent", name, workload, env.Name, env.Value)
			}
		}
		if got := readerTemplates(t, agent); got != want {
			t.Errorf("%s: the reader templates differ from an install with no condition, so they would roll", name)
		}
	}
}

// gateEnvOf is the profile names file env on the gateway's and the MCP
// server's containers, by workload.
func gateEnvOf(agent *agentv1alpha1.PlatformAgent) map[string]string {
	out := map[string]string{}
	for name, spec := range map[string]corev1.PodSpec{
		"gateway": buildA2AGatewayDeployment(agent).Spec.Template.Spec,
		"mcp":     buildA2AMCPDeployment(agent).Spec.Template.Spec,
	} {
		for _, c := range spec.Containers {
			for _, e := range c.Env {
				if e.Name == a2aProfileNamesEnvVar {
					out[name] = e.Value
				}
			}
		}
	}
	return out
}

// Once latched, both readers carry the cutoff: the persisted condition's
// LastTransitionTime plus the margin, RFC3339 in UTC. The latch never moves,
// so a later pass (a later "now") renders the same bytes and the readers roll
// once.
func TestALatchedInstallRendersTheCutoffFromThePersistedLatch(t *testing.T) {
	agent := a2aTestAgent()
	latchedAt := time.Date(2026, 11, 23, 9, 30, 0, 0, time.FixedZone("PST", -8*3600))
	setProfileSupervisorFormCondition(agent, true, "", metav1.NewTime(latchedAt))
	want := "2026-11-23T17:40:00Z"
	for workload, env := range cutoffEnvOf(agent) {
		if env == nil || env.Value != want || env.ValueFrom != nil {
			t.Errorf("the %s's cutoff is %+v, want %s=%s", workload, env, a2aProfileSupervisorCutoffEnvVar, want)
		}
	}
	first := readerTemplates(t, agent)
	setProfileSupervisorFormCondition(agent, true, "", metav1.NewTime(latchedAt.Add(time.Hour)))
	if got := readerTemplates(t, agent); got != first {
		t.Error("a later pass moved the cutoff; the latch's time must hold it")
	}
	if _, err := time.Parse(time.RFC3339, want); err != nil {
		t.Fatalf("the cutoff doesn't parse as the readers parse it: %v", err)
	}
}

// The cutoff comes from the condition as the API server holds it, never from
// this pass's wanted latch: the pass that latches renders the readers as they
// were, and the env arrives one pass later. And the roll it causes doesn't
// reopen the gate: a latched install keeps the six-token form while the
// readers roll onto the cutoff.
func TestTheCutoffFollowsThePersistedLatchAndItsRollKeepsTheForm(t *testing.T) {
	agent := a2aTestAgent()
	var objs []client.Object
	for _, rd := range a2aProfileSupervisorReaders(agent) {
		objs = append(objs, readerDeployment(agent, rd, true))
	}
	job := a2aProvisionJobForm(agent, true)
	job.Status.Succeeded = 1
	r := supervisorFormReconciler(t, append(objs, job)...)
	ctx := context.Background()
	if _, form, err := r.a2aProfileSupervisorFormFor(ctx, agent); err != nil || form != a2aProfileSupervisorFormValue {
		t.Fatalf("form %q err %v, want the latch wanted", form, err)
	}
	for workload, env := range cutoffEnvOf(agent) {
		if env != nil {
			t.Errorf("the pass that wants the latch rendered the %s's cutoff before it was persisted", workload)
		}
	}
	setProfileSupervisorFormCondition(agent, true, "", metav1.Now())
	// The readers now mid-roll onto the cutoff's template.
	objs = nil
	for _, rd := range a2aProfileSupervisorReaders(agent) {
		objs = append(objs, readerDeployment(agent, rd, false))
	}
	r = supervisorFormReconciler(t, objs...)
	if filter, form, err := r.a2aProfileSupervisorFormFor(ctx, agent); err != nil || !filter || form != a2aProfileSupervisorFormValue {
		t.Fatalf("latched, readers rolling onto the cutoff: filter %v form %q err %v, want both kept", filter, form, err)
	}
	for workload, env := range cutoffEnvOf(agent) {
		if env == nil {
			t.Errorf("the persisted latch rendered no cutoff on the %s", workload)
		}
	}
}

// Once latched, both readers mount the profile names file and are pointed at
// it; before, neither does (the unlatched test above holds the templates byte
// for byte). The mount is the operator's ConfigMap, read-only and optional,
// in the reader's own container.
func TestALatchedInstallMountsTheProfileNamesOnBothReaders(t *testing.T) {
	agent := a2aTestAgent()
	if got := gateEnvOf(agent); len(got) != 0 {
		t.Errorf("unlatched readers point at a names file: %v", got)
	}
	setProfileSupervisorFormCondition(agent, true, "", metav1.Now())
	want := a2aProfileNamesMountPath + "/" + a2aProfileNamesKey
	if got := gateEnvOf(agent); got["gateway"] != want || got["mcp"] != want {
		t.Errorf("names file env = %v, want %s on both", got, want)
	}
	for workload, c := range map[string]struct {
		spec      corev1.PodSpec
		container string
	}{
		"gateway": {buildA2AGatewayDeployment(agent).Spec.Template.Spec, a2aGatewayContainerName},
		"mcp":     {buildA2AMCPDeployment(agent).Spec.Template.Spec, a2aMCPContainer},
	} {
		var vol *corev1.Volume
		for i := range c.spec.Volumes {
			if c.spec.Volumes[i].Name == a2aProfileNamesVolume {
				vol = &c.spec.Volumes[i]
			}
		}
		if vol == nil || vol.ConfigMap == nil || vol.ConfigMap.Name != a2aProfileNamesName(agent) || !ptr.Deref(vol.ConfigMap.Optional, false) {
			t.Errorf("%s: names volume = %+v, want the optional %s ConfigMap", workload, vol, a2aProfileNamesName(agent))
		}
		mounted := false
		for _, ct := range c.spec.Containers {
			for _, m := range ct.VolumeMounts {
				if m.Name == a2aProfileNamesVolume {
					if ct.Name != c.container || m.MountPath != a2aProfileNamesMountPath || !m.ReadOnly || m.SubPath != "" {
						t.Errorf("%s: names mount %+v on %s, want read-only %s on %s with no subPath", workload, m, ct.Name, a2aProfileNamesMountPath, c.container)
					}
					mounted = true
				}
			}
		}
		if !mounted {
			t.Errorf("%s: the names file is not mounted", workload)
		}
	}
}

// The profile names file holds exactly the names nats.conf denies the gateway
// the supervisor subjects of (gatewayProfileSupervisorDeny), sorted, once
// each, so a reader's "profile" and the bus's deny are one set; and it is
// written only once latched.
func TestTheProfileNamesAreTheBusDenysNames(t *testing.T) {
	agent := a2aTestAgent()
	names := []string{"fleet-audit", "cto-otter-1a2b", "fleet-audit", "", "bad.name", "wild*"}
	cm, err := buildA2AProfileNamesConfigMap(agent, names)
	if err != nil {
		t.Fatal(err)
	}
	var f a2aProfileNamesFile
	if err := json.Unmarshal([]byte(cm.Data[a2aProfileNamesKey]), &f); err != nil {
		t.Fatalf("names file %q: %v", cm.Data[a2aProfileNamesKey], err)
	}
	var fromDeny []string
	for _, d := range gatewayProfileSupervisorDeny(names) {
		fromDeny = append(fromDeny, strings.TrimSuffix(strings.TrimPrefix(d, a2aProfileSupervisorPrefix), a2aProfileSupervisorSuffix))
	}
	if strings.Join(f.Profiles, ",") != strings.Join(fromDeny, ",") || len(f.Profiles) != 2 {
		t.Errorf("names file %v, the deny's names %v; want the same two", f.Profiles, fromDeny)
	}

	r, cl, _ := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	key := client.ObjectKey{Namespace: agent.Namespace, Name: a2aProfileNamesName(agent)}
	if err := r.applyA2AProfileNames(ctx, agent, names, nil); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &corev1.ConfigMap{}); err == nil {
		t.Error("an unlatched install wrote the profile names")
	}
	setProfileSupervisorFormCondition(agent, true, "", metav1.Now())
	if err := r.applyA2AProfileNames(ctx, agent, names, nil); err != nil {
		t.Fatal(err)
	}
	var got corev1.ConfigMap
	if err := cl.Get(ctx, key, &got); err != nil {
		t.Fatalf("a latched install wrote no profile names: %v", err)
	}
	if got.Data[a2aProfileNamesKey] != cm.Data[a2aProfileNamesKey] {
		t.Errorf("applied %q, want %q", got.Data[a2aProfileNamesKey], cm.Data[a2aProfileNamesKey])
	}
}

// A names render that failed applies nothing and fails the pass: the
// ConfigMap keeps the names last written rather than taking an empty list,
// which would tell both readers no addressee is a profile.
func TestAFailedNamesRenderKeepsTheNamesLastWritten(t *testing.T) {
	agent := a2aTestAgent()
	setProfileSupervisorFormCondition(agent, true, "", metav1.Now())
	r, cl, _ := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	key := client.ObjectKey{Namespace: agent.Namespace, Name: a2aProfileNamesName(agent)}
	if err := r.applyA2AProfileNames(ctx, agent, nil, errors.New("render failed")); err == nil {
		t.Fatal("a failed render applied with no error")
	}
	if err := cl.Get(ctx, key, &corev1.ConfigMap{}); err == nil {
		t.Error("a failed render wrote a profile names ConfigMap")
	}
	if err := r.applyA2AProfileNames(ctx, agent, []string{"fleet-audit"}, nil); err != nil {
		t.Fatal(err)
	}
	if err := r.applyA2AProfileNames(ctx, agent, nil, errors.New("render failed")); err == nil {
		t.Fatal("a failed render applied with no error")
	}
	var got corev1.ConfigMap
	if err := cl.Get(ctx, key, &got); err != nil {
		t.Fatal(err)
	}
	if got.Data[a2aProfileNamesKey] != `{"profiles":["fleet-audit"]}`+"\n" {
		t.Errorf("after a failed render the names are %q, want the names last written", got.Data[a2aProfileNamesKey])
	}
}

// The gate holds against the gateway, so neither reader may write the file
// it takes profile-ness from: the gateway's Role names no write on any
// ConfigMap but its home-channel record, never this one, and cannot write its
// own Deployment (the cutoff env); the MCP server mounts no ServiceAccount
// token, so it has no API access at all.
func TestNeitherReaderCanWriteTheProfileSupervisorGate(t *testing.T) {
	agent := a2aTestAgent()
	writes := map[string]bool{"create": true, "update": true, "patch": true, "delete": true, "deletecollection": true, "*": true}
	for _, rule := range buildA2AGatewayRole(agent).Rules {
		write := false
		for _, v := range rule.Verbs {
			write = write || writes[v]
		}
		if !write {
			continue
		}
		for _, res := range rule.Resources {
			switch res {
			case "configmaps":
				if len(rule.ResourceNames) == 0 {
					t.Errorf("the gateway may write every ConfigMap: %+v", rule)
				}
				for _, n := range rule.ResourceNames {
					if n == a2aProfileNamesName(agent) {
						t.Errorf("the gateway may write the profile names: %+v", rule)
					}
				}
			case "deployments", "*":
				t.Errorf("the gateway may write Deployments: %+v", rule)
			}
		}
	}
	if spec := buildA2AMCPDeployment(agent).Spec.Template.Spec; ptr.Deref(spec.AutomountServiceAccountToken, true) {
		t.Error("the MCP server mounts a ServiceAccount token")
	}
}
