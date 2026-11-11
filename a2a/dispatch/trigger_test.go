package dispatch

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

const scheduleTriggerJSON = `{"name":"schedule/compliance-audit","origin":"builtin","stream":"compliance-audit"}`

var scheduleTrigger = capability.Trigger{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "compliance-audit"}

// triggeredEnv is submissionEnv with a trigger beside the grant in the
// authority block, as the gateway writes a trigger's submission (#4685).
func triggeredEnv(t *testing.T, profile, taskID, capKey, trigger string, params map[string]string) *lib.Envelope {
	t.Helper()
	env := submissionEnv(t, profile, taskID, capKey, params)
	env.Authority = json.RawMessage(fmt.Sprintf(`{"grants":{"capability":{"key":%q,"revision":1}},"trigger":%s}`, capKey, trigger))
	return env
}

func triggerAdmission(t *testing.T) (*Dispatcher, *fakeVerifier, lib.DispatchConfig, lib.DispatchProfile, holdKey) {
	t.Helper()
	v := &fakeVerifier{}
	d := &Dispatcher{verifier: v, verified: map[holdKey]permit{}}
	prof := testProfile("tse", 1)
	snap := lib.DispatchConfig{Namespace: "kubeagents-system", Profiles: []lib.DispatchProfile{prof}}
	return d, v, snap, prof, holdKey{"tse", "read", lib.SubmitKindTop, "task-1"}
}

// The trigger the submission claims is what the verifier is asked about,
// and admission hands it on for the credential.
func TestAdmissionAsksTheVerifierAboutTheSubmissionsTrigger(t *testing.T) {
	d, v, snap, prof, hk := triggerAdmission(t)
	del := &envDelivery{env: triggeredEnv(t, "tse", "task-1", "cap-a", scheduleTriggerJSON, clusterParams)}
	adm, ref, err := d.admit(context.Background(), del, snap, prof, hk, time.Now())
	if err != nil || ref != nil {
		t.Fatalf("admit: refusal %+v, err %v", ref, err)
	}
	if got := v.triggerList(); len(got) != 1 || got[0] == nil || *got[0] != scheduleTrigger {
		t.Fatalf("verifier asked with triggers %+v; want [%+v]", got, scheduleTrigger)
	}
	if want := (taskcred.Trigger{Name: scheduleTrigger.Name, Origin: scheduleTrigger.Origin, Stream: scheduleTrigger.Stream}); adm.trigger == nil || *adm.trigger != want {
		t.Errorf("admission handed on trigger %+v; want %+v", adm.trigger, scheduleTrigger)
	}

	// No trigger in the block: the verifier is asked with none.
	d2, v2, snap2, prof2, hk2 := triggerAdmission(t)
	del2 := &envDelivery{env: submissionEnv(t, "tse", "task-1", "cap-a", clusterParams)}
	adm2, ref2, err := d2.admit(context.Background(), del2, snap2, prof2, hk2, time.Now())
	if err != nil || ref2 != nil {
		t.Fatalf("admit without a trigger: refusal %+v, err %v", ref2, err)
	}
	if got := v2.triggerList(); len(got) != 1 || got[0] != nil {
		t.Fatalf("verifier asked with triggers %+v; want [nil]", got)
	}
	if adm2.trigger != nil {
		t.Errorf("admission handed on trigger %+v; want none", adm2.trigger)
	}
}

// A malformed trigger is refused at admission, never read as none.
func TestAMalformedTriggerIsRefusedAtAdmission(t *testing.T) {
	for name, trig := range map[string]string{
		"not an object":  `"schedule/a"`,
		"a bad name":     `{"name":"schedule/a.b","origin":"builtin"}`,
		"an unknown key": `{"name":"schedule/a","origin":"builtin","scope":"fleet"}`,
	} {
		d, v, snap, prof, hk := triggerAdmission(t)
		del := &envDelivery{env: triggeredEnv(t, "tse", "task-1", "cap-a", trig, clusterParams)}
		_, ref, err := d.admit(context.Background(), del, snap, prof, hk, time.Now())
		if err != nil || ref == nil || ref.reason != ReasonVerifierRefused {
			t.Errorf("%s: refusal %+v, err %v; want a verifier refusal", name, ref, err)
		}
		if n := len(v.callList()); n != 0 {
			t.Errorf("%s: %d verifier calls; want none for a block that doesn't parse", name, n)
		}
	}
}

// A retry under the same task id that claims another trigger is checked
// again: a permit for one trigger is never stretched over another.
func TestARetryClaimingAnotherTriggerIsCheckedAgain(t *testing.T) {
	d, v, snap, prof, hk := triggerAdmission(t)
	now := time.Now()
	admit := func(env *lib.Envelope) {
		t.Helper()
		if _, ref, err := d.admit(context.Background(), &envDelivery{env: env}, snap, prof, hk, now); err != nil || ref != nil {
			t.Fatalf("admit: refusal %+v, err %v", ref, err)
		}
	}
	admit(triggeredEnv(t, "tse", "task-1", "cap-a", scheduleTriggerJSON, clusterParams))
	admit(triggeredEnv(t, "tse", "task-1", "cap-a", scheduleTriggerJSON, clusterParams)) // unchanged: reused
	if n := len(v.callList()); n != 1 {
		t.Fatalf("%d verifier calls for one submission retried unchanged; want 1", n)
	}
	admit(triggeredEnv(t, "tse", "task-1", "cap-a", `{"name":"schedule/ai-security-audit","origin":"builtin","stream":"ai-security-audit"}`, clusterParams))
	admit(submissionEnv(t, "tse", "task-1", "cap-a", clusterParams)) // the trigger dropped
	if n := len(v.callList()); n != 3 {
		t.Fatalf("%d verifier calls; want each new trigger, and its absence, checked again", n)
	}
}

// The credential-proxy token carries the admitted trigger; the bus
// credential carries none.
func TestTheProxyTokenCarriesTheAdmittedTrigger(t *testing.T) {
	b := newBus(t, "tse-1")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-1", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-1")
	ti.pool.SetUID("tse", "read", "tse-1", "uid-tse-1")
	kp, pub := signingKey(t)
	b.runKeyed(ti, kp, 0)

	if err := b.gateway.Publish(b.ctx, lib.TaskSubmitSubject("tse", "read", lib.SubmitKindTop, "task-1"),
		triggeredEnv(t, "tse", "task-1", "cap-ok", scheduleTriggerJSON, clusterParams)); err != nil {
		t.Fatal(err)
	}
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	confirms := confirmsOf(w)
	if len(confirms) != 1 || confirms[0].ProxyToken == "" {
		t.Fatalf("%d confirms; want one carrying a proxy token", len(confirms))
	}
	proxyC := verifyAs(t, confirms[0].ProxyToken, pub, taskcred.AudienceCredentialProxy)
	want := taskcred.Trigger{Name: scheduleTrigger.Name, Origin: scheduleTrigger.Origin, Stream: scheduleTrigger.Stream}
	if proxyC.Trigger == nil || *proxyC.Trigger != want {
		t.Errorf("proxy token trigger %+v; want %+v", proxyC.Trigger, want)
	}
	natsC := verifyAs(t, confirms[0].TaskCredential, pub, taskcred.AudienceNATS)
	if natsC.Trigger != nil {
		t.Errorf("the bus credential carries a trigger %+v", *natsC.Trigger)
	}
}
