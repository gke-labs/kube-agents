package dispatch

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	recheckNS     = "kube-agents"
	recheckProd   = capability.Scope("project/p1/location/us-east1/cluster/prod")
	recheckDev    = capability.Scope("project/p1/location/us-east1/cluster/dev")
	recheckPodSes = "s-brave-otter" // a session pod's name, not a profile
)

// envWithVia is a submission whose authority carries a capability and via.
func envWithVia(t *testing.T, via string) *lib.Envelope {
	t.Helper()
	auth := `{"grants":{"capability":{"key":"cap-ok","revision":1}}`
	if via != "" {
		auth += `,"via":` + via
	}
	auth += `}`
	env, err := lib.NewMessageEnvelope(lib.Party{Session: "gateway", AgentType: "a2a-gateway"}, "task-c", "ctx-c", "corr-c",
		json.RawMessage(`{"role":"user","messageId":"m-c","parts":[{"kind":"text","text":"x"}]}`), lib.WithAuthority(json.RawMessage(auth)))
	if err != nil {
		t.Fatal(err)
	}
	return env
}

func TestAProfileParentsChildIsReCheckedAgainstItsParentsChainAndScope(t *testing.T) {
	snap := testDispatchConfig(6, testProfile("a", 2), testProfile("b", 2), testProfile("c", 2))
	nsScope := capability.NamespaceScope(recheckNS)
	via := func(session string, chain []string, scope capability.Scope) string {
		b, err := json.Marshal(capability.Via{TaskID: "task-p", Session: session, ProfileChain: chain, Scope: scope})
		if err != nil {
			t.Fatal(err)
		}
		return string(b)
	}
	cases := []struct {
		name   string
		kind   string
		via    string
		prof   string
		target capability.Scope
		reason string
	}{
		{"a top task isn't re-checked", lib.SubmitKindTop, via("a", nil, ""), "b", recheckProd, ""},
		{"no via (a human's top task shape)", lib.SubmitKindChild, "", "b", recheckProd, ""},
		{"a session's child", lib.SubmitKindChild, via(recheckPodSes, nil, ""), "b", recheckProd, ""},
		{"an older gateway's profile-parent child (no scope)", lib.SubmitKindChild, via("a", nil, ""), "b", recheckProd, ReasonParentUnchecked},
		{"head self-nests once", lib.SubmitKindChild, via("a", nil, nsScope), "a", recheckProd, ""},
		{"a child doesn't nest its own profile", lib.SubmitKindChild, via("a", []string{"a"}, nsScope), "a", recheckProd, ReasonParentCycle},
		{"A → B → A", lib.SubmitKindChild, via("b", []string{"a"}, nsScope), "a", recheckProd, ReasonParentCycle},
		{"A → B → C", lib.SubmitKindChild, via("b", []string{"a"}, nsScope), "c", recheckProd, ""},
		{"inside a cluster parent", lib.SubmitKindChild, via("a", nil, recheckProd), "b", recheckProd, ""},
		{"another cluster", lib.SubmitKindChild, via("a", nil, recheckProd), "b", recheckDev, ReasonOutsideParentScope},
		{"targetless under a cluster parent", lib.SubmitKindChild, via("a", nil, recheckProd), "b", nsScope, ReasonOutsideParentScope},
		{"install-scoped parent, any cluster", lib.SubmitKindChild, via("a", nil, nsScope), "b", recheckDev, ""},
	}
	for _, tc := range cases {
		prof := testProfile(tc.prof, 2)
		ref := profileParentRefusal(envWithVia(t, tc.via), snap, prof, tc.kind, tc.target)
		got := ""
		if ref != nil {
			got = ref.reason
		}
		if got != tc.reason {
			t.Errorf("%s: refusal %q, want %q", tc.name, got, tc.reason)
		}
	}
}

func TestViaFromAuthorityReadsTheParentsChainAndScope(t *testing.T) {
	v, err := capability.ViaFromAuthority(json.RawMessage(`{"via":{"taskId":"t","session":"a","profileChain":["x"],"scope":"namespace/n"}}`))
	if err != nil || v == nil || v.Session != "a" || len(v.ProfileChain) != 1 || v.Scope != "namespace/n" {
		t.Fatalf("via %+v, err %v", v, err)
	}
	if v, err := capability.ViaFromAuthority(json.RawMessage(`{"grants":null}`)); err != nil || v != nil {
		t.Fatalf("no via: %+v, err %v", v, err)
	}
	if _, err := capability.ViaFromAuthority(json.RawMessage(`{`)); err == nil {
		t.Fatal("a block that doesn't parse read as no via")
	}
}

// Through admission end to end: an older gateway's child of a profile parent
// (via names the profile, no parent scope) is rejected, visibly and once,
// and never offered.
func TestAnUncheckedProfileParentChildIsRejectedAtAdmission(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 200*time.Millisecond)
	b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	env := submissionEnv(t, "tse", "task-unchecked", "cap-ok", map[string]string{})
	env.Authority = json.RawMessage(fmt.Sprintf(`{"grants":{"capability":{"key":"cap-ok","revision":1}},"via":{"taskId":"task-p","session":%q}}`, "tse"))
	if err := b.gateway.Publish(b.ctx, lib.TaskSubmitSubject("tse", "read", lib.SubmitKindChild, "task-unchecked"), env); err != nil {
		t.Fatal(err)
	}
	ts := b.waitTerminal("task-unchecked", 10*time.Second)
	if ts.state != lib.StateRejected || !hasReason(ts, ReasonParentUnchecked) {
		t.Fatalf("terminal %+v, want rejected %s", ts, ReasonParentUnchecked)
	}
	if n := w.offerCount(); n != 0 {
		t.Fatalf("an unchecked child was offered (%d offers)", n)
	}
}
