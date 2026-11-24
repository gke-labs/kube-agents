package capability

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

var scheduleTrigger = Trigger{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "compliance-audit"}

func triggeredGrant(trigger *Trigger) Entry {
	e := profileGrant(TierUnattended, "fleet-audit")
	e.Trigger = trigger
	return e
}

// A trigger is set only on an unattended grant, and only in a valid shape.
func TestATriggerIsForTheUnattendedTierAlone(t *testing.T) {
	ok := scheduleTrigger
	if err := triggeredGrant(&ok).Validate(); err != nil {
		t.Fatalf("an unattended grant with a trigger should validate: %v", err)
	}
	if err := triggeredGrant(nil).Validate(); err != nil {
		t.Fatalf("an unattended grant with no trigger should validate: %v", err)
	}
	read := profileGrant(TierRead, "tse")
	read.Trigger = &ok
	mustRefuse(t, read.Validate(), "trigger at a tier that is not unattended")
	bad := Trigger{Name: "schedule/a.b", Origin: "builtin"}
	mustRefuse(t, triggeredGrant(&bad).Validate(), "trigger")
}

// A loaded tier in unattended mode may have any name, but only the built-in
// unattended tier binds a trigger. The broker's report credential (#5619)
// rests on that: taskcred and the Python verifier take a credential with no
// target only at the literal tier "unattended", so a trigger at an
// unattended-mode tier of another name would be a credential they refuse. If
// this ever has to change, those two rules move with it.
func TestOnlyTheBuiltinUnattendedTierBindsATrigger(t *testing.T) {
	restoreTiers(t)
	if skipped := LoadTiers([]TierDefinition{{Name: "nightly", Mode: TierModeUnattended}}); len(skipped) != 0 {
		t.Fatalf("skipped %v", skipped)
	}
	ok := scheduleTrigger
	nightly := profileGrant("nightly", "fleet-audit")
	if err := nightly.Validate(); err != nil {
		t.Fatalf("an unattended-mode grant with no trigger should validate: %v", err)
	}
	nightly.Trigger = &ok
	mustRefuse(t, nightly.Validate(), "trigger at a tier that is not unattended")
}

// profile.run permits only when the request's trigger is the grant's, both
// ways: a different one, one the grant lacks, and one the grant has but the
// request omits are each refused.
func TestTheVerifierMatchesTheTriggerAgainstTheGrant(t *testing.T) {
	s := newStore()
	trig := scheduleTrigger
	withTrigger := s.write(t, rootKey(t, gwTask), triggeredGrant(&trig))
	without := s.write(t, rootKey(t, "gw-task-2"), triggeredGrant(nil))
	nc := serveVerifier(t, s)
	c := clientAs(t, nc, podA)
	ctx := context.Background()

	same := scheduleTrigger
	if err := c.CheckProfileRun(ctx, withTrigger, "fleet-audit", TierUnattended, targetInScope, &same); err != nil {
		t.Fatalf("the grant's own trigger should be permitted: %v", err)
	}
	if err := c.CheckProfileRun(ctx, without, "fleet-audit", TierUnattended, targetInScope, nil); err != nil {
		t.Fatalf("no trigger against a grant with none should be permitted: %v", err)
	}
	other := Trigger{Name: "schedule/compliance-audit", Origin: "user", Stream: "compliance-audit"}
	mustRefuse(t, c.CheckProfileRun(ctx, withTrigger, "fleet-audit", TierUnattended, targetInScope, &other), "trigger is not the grant's")
	stream := Trigger{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "ai-security-audit"}
	mustRefuse(t, c.CheckProfileRun(ctx, withTrigger, "fleet-audit", TierUnattended, targetInScope, &stream), "trigger is not the grant's")
	mustRefuse(t, c.CheckProfileRun(ctx, without, "fleet-audit", TierUnattended, targetInScope, &same), "grant binds no trigger")
	mustRefuse(t, c.CheckProfileRun(ctx, withTrigger, "fleet-audit", TierUnattended, targetInScope, nil), "names no trigger")
}

// A trigger on any other verb is refused rather than ignored.
func TestATriggerOnAnotherVerbIsRefused(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), profileGrant(TierRead, "tse"))
	nc := serveVerifier(t, s)
	trig := scheduleTrigger
	body, err := json.Marshal(Request{Ref: root, Verb: VerbTaskExecute, Resource: targetInScope, Trigger: &trig})
	if err != nil {
		t.Fatal(err)
	}
	got := rawAsk(t, nc, podA, body)
	if got.Allowed || !strings.Contains(got.Reason, "only read for profile.run") {
		t.Errorf("got %+v, want a refusal naming profile.run", got)
	}
}

// A hop carries its parent's trigger unchanged: it can neither change nor
// drop it, so no broker chooses which report stream its chain writes.
func TestAHopCannotChangeTheTrigger(t *testing.T) {
	trig := scheduleTrigger
	other := Trigger{Name: "schedule/ai-security-audit", Origin: "builtin", Stream: "ai-security-audit"}
	for name, hopTrigger := range map[string]*Trigger{"changed": &other, "dropped": nil} {
		s := newStore()
		root := s.write(t, rootKey(t, gwTask), triggeredGrant(&trig))
		hop := triggeredGrant(hopTrigger)
		hop.Delegate = podB
		hop.Parent = &root
		ref := s.write(t, hopKey(t, podA, 0), hop)
		nc := serveVerifier(t, s)
		mustRefuse(t, clientAs(t, nc, podB).CheckProfileRun(context.Background(), ref, "fleet-audit", TierUnattended, targetInScope, hopTrigger), WalkRefused)
		_ = name
	}
	// Control: a hop that keeps the trigger is permitted.
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), triggeredGrant(&trig))
	kept := scheduleTrigger
	hop := triggeredGrant(&kept)
	hop.Delegate = podB
	hop.Parent = &root
	ref := s.write(t, hopKey(t, podA, 0), hop)
	nc := serveVerifier(t, s)
	if err := clientAs(t, nc, podB).CheckProfileRun(context.Background(), ref, "fleet-audit", TierUnattended, targetInScope, &kept); err != nil {
		t.Fatalf("a hop that keeps its parent's trigger should be permitted: %v", err)
	}
}

func TestTriggerFromAuthority(t *testing.T) {
	for name, raw := range map[string]string{
		"no block":        ``,
		"a null block":    `null`,
		"a spaced null":   ` null `,
		"no trigger":      `{"grants":{"capability":{"key":"k","rev":1}}}`,
		"a null trigger":  `{"grants":null,"trigger":null}`,
		"grants and none": `{"grants":null}`,
	} {
		got, err := TriggerFromAuthority(json.RawMessage(raw))
		if err != nil || got != nil {
			t.Errorf("%s: got %+v, %v; want none", name, got, err)
		}
	}
	got, err := TriggerFromAuthority(json.RawMessage(`{"grants":null,"trigger":{"name":"schedule/compliance-audit","origin":"builtin","stream":"compliance-audit"}}`))
	if err != nil || got == nil || *got != scheduleTrigger {
		t.Errorf("got %+v, %v; want %+v", got, err, scheduleTrigger)
	}
	for name, raw := range map[string]string{
		"not an object":        `{"trigger":"schedule/a"}`,
		"a malformed name":     `{"trigger":{"name":"schedule/a.b","origin":"builtin"}}`,
		"an unknown key":       `{"trigger":{"name":"schedule/a","origin":"builtin","scope":"fleet"}}`,
		"an unknown origin":    `{"trigger":{"name":"schedule/a","origin":"root"}}`,
		"the block isn't JSON": `{`,
		// What encoding/json would quietly accept: another reader of the block
		// could then see a different trigger from the one the dispatcher checked.
		"a case-variant outer key":   `{"Trigger":{"name":"schedule/a","origin":"builtin"}}`,
		"a case-folded outer key":    `{"trigger":{"name":"schedule/a","origin":"builtin"},"TRIGGER":null}`,
		"a duplicate outer key":      `{"trigger":{"name":"schedule/a","origin":"builtin"},"trigger":null}`,
		"a case-variant inner key":   `{"trigger":{"NAME":"schedule/a","origin":"builtin"}}`,
		"a long-s inner key":         "{\"trigger\":{\"name\":\"schedule/a\",\"origin\":\"builtin\",\"\u017ftream\":\"a\"}}",
		"a duplicate inner key":      `{"trigger":{"name":"schedule/a","name":"schedule/b","origin":"builtin"}}`,
		"a null inner value":         `{"trigger":{"name":"schedule/a","origin":"builtin","stream":null}}`,
		"a user origin":              `{"trigger":{"name":"schedule/a","origin":"user"}}`,
		"another schedule's stream":  `{"trigger":{"name":"schedule/a","origin":"builtin","stream":"b"}}`,
		"trailing data in the block": `{"trigger":{"name":"schedule/a","origin":"builtin"}} {}`,
		"data after a null block":    `null {}`,
	} {
		if _, err := TriggerFromAuthority(json.RawMessage(raw)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// A built-in schedule's script run (#5622): the grant binds the script with the
// rest of the trigger, so the verifier refuses a claim naming another script,
// naming one the grant doesn't, or naming none where the grant does.
func TestTheVerifierMatchesTheTriggersScriptAgainstTheGrant(t *testing.T) {
	s := newStore()
	scripted := scheduleTrigger
	scripted.Script = "compliance-audit"
	grant := s.write(t, rootKey(t, gwTask), triggeredGrant(&scripted))
	plain := scheduleTrigger
	plainGrant := s.write(t, rootKey(t, "gw-task-3"), triggeredGrant(&plain))
	nc := serveVerifier(t, s)
	c := clientAs(t, nc, podA)
	ctx := context.Background()

	same := scripted
	if err := c.CheckProfileRun(ctx, grant, "fleet-audit", TierUnattended, targetInScope, &same); err != nil {
		t.Fatalf("the grant's own script should be permitted: %v", err)
	}
	other := scripted
	other.Script = "fleet-consistency-drift"
	mustRefuse(t, c.CheckProfileRun(ctx, grant, "fleet-audit", TierUnattended, targetInScope, &other), "trigger is not the grant's")
	none := scheduleTrigger
	mustRefuse(t, c.CheckProfileRun(ctx, grant, "fleet-audit", TierUnattended, targetInScope, &none), "trigger is not the grant's")
	added := scripted
	mustRefuse(t, c.CheckProfileRun(ctx, plainGrant, "fleet-audit", TierUnattended, targetInScope, &added), "trigger is not the grant's")
}

func TestTriggerFromAuthorityReadsAScript(t *testing.T) {
	got, err := TriggerFromAuthority(json.RawMessage(`{"trigger":{"name":"schedule/compliance-audit","origin":"builtin","stream":"compliance-audit","script":"compliance-audit"}}`))
	want := scheduleTrigger
	want.Script = "compliance-audit"
	if err != nil || got == nil || *got != want {
		t.Fatalf("got %+v, %v; want %+v", got, err, want)
	}
	for name, raw := range map[string]string{
		"a path":             `{"trigger":{"name":"schedule/a","origin":"builtin","script":"/opt/scripts/run.sh"}}`,
		"a null script":      `{"trigger":{"name":"schedule/a","origin":"builtin","script":null}}`,
		"a case-variant key": `{"trigger":{"name":"schedule/a","origin":"builtin","SCRIPT":"a"}}`,
		"a duplicate script": `{"trigger":{"name":"schedule/a","origin":"builtin","script":"a","script":"b"}}`,
	} {
		if got, err := TriggerFromAuthority(json.RawMessage(raw)); err == nil {
			t.Errorf("%s: read %+v", name, got)
		}
	}
}
