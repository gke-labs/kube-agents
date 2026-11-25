package lib

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func goodTriggerSubmit() TriggerSubmit {
	return TriggerSubmit{
		Trigger:        TriggerClaim{Name: "schedule/compliance-audit", Origin: TriggerOriginBuiltin, Stream: "compliance-audit"},
		IdempotencyKey: "schedule/kubeagents-system/compliance-audit/1763100000",
		Schedule:       ScheduleRef{Namespace: "kubeagents-system", Name: "compliance-audit"},
		Profile:        "fleet-audit",
		Prompt:         "Run the compliance audit.",
		Skills:         []string{"fleet-audit"},
		Delivery:       ScheduleDelivery{Mode: ScheduleDeliveryChat, SilentWhenClean: true, JobKey: "compliance-audit"},
	}
}

func TestTheTriggerSubmitSubjectRoundTrips(t *testing.T) {
	subject := TriggerSubmitSubject(TriggerSourceSchedule)
	if subject != "a2a.trigger.schedule.submit" {
		t.Fatalf("subject = %q", subject)
	}
	if got, err := ParseTriggerSubmitSubject(subject); err != nil || got != "schedule" {
		t.Fatalf("parse = %q, %v", got, err)
	}
	for _, bad := range []string{"a2a.trigger.submit", "a2a.trigger.a.b.submit", "a2a.trigger.Schedule.submit", "a2a.tasks.schedule.submit", "a2a.trigger.schedule.in"} {
		if _, err := ParseTriggerSubmitSubject(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

func TestAWellFormedTriggerSubmitPasses(t *testing.T) {
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, goodTriggerSubmit()); err != nil {
		t.Fatal(err)
	}
	runNow := goodTriggerSubmit()
	runNow.IdempotencyKey = "run-now/kubeagents-system/compliance-audit/1763100123"
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, runNow); err != nil {
		t.Fatalf("run-now key: %v", err)
	}
	scripted := goodTriggerSubmit()
	scripted.Trigger.Script = "compliance-audit"
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, scripted); err != nil {
		t.Fatalf("a script-run trigger (#5622): %v", err)
	}
}

func TestAMalformedTriggerSubmitIsRefused(t *testing.T) {
	for name, mutate := range map[string]func(*TriggerSubmit){
		"another source's trigger":     func(s *TriggerSubmit) { s.Trigger.Name = "event-watcher/compliance-audit" },
		"a user origin":                func(s *TriggerSubmit) { s.Trigger.Origin = "user" },
		"a bare source as name":        func(s *TriggerSubmit) { s.Trigger.Name, s.Trigger.Stream = "schedule", "" },
		"a stream not its own":         func(s *TriggerSubmit) { s.Trigger.Stream = "stockout-prevention" },
		"no stream":                    func(s *TriggerSubmit) { s.Trigger.Stream = "" },
		"a schedule the trigger isn't": func(s *TriggerSubmit) { s.Schedule.Name = "other" },
		"a key for another schedule":   func(s *TriggerSubmit) { s.IdempotencyKey = "schedule/kubeagents-system/other/1" },
		"a key of another kind":        func(s *TriggerSubmit) { s.IdempotencyKey = "manual/kubeagents-system/compliance-audit/1" },
		"a key with no token":          func(s *TriggerSubmit) { s.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/" },
		"a key too long": func(s *TriggerSubmit) {
			s.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/" + strings.Repeat("9", 600)
		},
		"a bad profile":    func(s *TriggerSubmit) { s.Profile = "Fleet.Audit" },
		"an empty prompt":  func(s *TriggerSubmit) { s.Prompt = "  " },
		"a long prompt":    func(s *TriggerSubmit) { s.Prompt = strings.Repeat("x", TriggerSubmitPromptMaxLen+1) },
		"too many skills":  func(s *TriggerSubmit) { s.Skills = make([]string, TriggerSubmitSkillsMax+1) },
		"a bad skill":      func(s *TriggerSubmit) { s.Skills = []string{"../x"} },
		"a bad parameter":  func(s *TriggerSubmit) { s.Parameters = map[string]string{"X": "y"} },
		"a short deadline": func(s *TriggerSubmit) { s.ActiveDeadlineSeconds = 30 },
		"too many turns":   func(s *TriggerSubmit) { s.MaxTurns = TriggerSubmitMaxTurnsMax + 1 },
		"a bad delivery":   func(s *TriggerSubmit) { s.Delivery.Mode = "" },
		"a script path":    func(s *TriggerSubmit) { s.Trigger.Script = "/opt/kube-agents/scripts/x.py" },
		"a dotted script":  func(s *TriggerSubmit) { s.Trigger.Script = "audit.py" },
	} {
		t.Run(name, func(t *testing.T) {
			s := goodTriggerSubmit()
			mutate(&s)
			if err := ValidateTriggerSubmit(TriggerSourceSchedule, s); err == nil {
				t.Error("accepted")
			}
		})
	}
	if err := ValidateTriggerSubmit("event-watcher", goodTriggerSubmit()); err == nil {
		t.Error("a source other than schedule submitted")
	}
}

func TestScheduleDeliveryIsChatOrLocalWithALabelKey(t *testing.T) {
	for _, d := range []ScheduleDelivery{{Mode: "chat", JobKey: "a"}, {Mode: "local", JobKey: "compliance-audit"}} {
		if err := ValidateScheduleDelivery(d); err != nil {
			t.Errorf("%+v: %v", d, err)
		}
	}
	for _, d := range []ScheduleDelivery{
		{Mode: "", JobKey: "a"},
		{Mode: "all", JobKey: "a"},
		{Mode: "chat", JobKey: ""},
		{Mode: "chat", JobKey: "has.dot"},
		{Mode: "chat", JobKey: "Upper"},
		{Mode: "chat", JobKey: strings.Repeat("a", 64)},
	} {
		if err := ValidateScheduleDelivery(d); err == nil {
			t.Errorf("%+v accepted", d)
		}
	}
}

// TestAThreadDeliveryNamesItsThreadAndOnlyItDoes: mode thread names its
// platform, chat and thread, with an a2a- incident source or none; no other
// mode names any of them.
func TestAThreadDeliveryNamesItsThreadAndOnlyItDoes(t *testing.T) {
	good := ScheduleDelivery{Mode: ScheduleDeliveryThread, JobKey: "cluster-triage", Platform: NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "1763100000.000100"}
	withSource := good
	withSource.IncidentSource = "a2a-triage"
	for _, d := range []ScheduleDelivery{good, withSource} {
		if err := ValidateScheduleDelivery(d); err != nil {
			t.Errorf("%+v: %v", d, err)
		}
	}
	for name, edit := range map[string]func(*ScheduleDelivery){
		"no platform":                   func(d *ScheduleDelivery) { d.Platform = "" },
		"no chat":                       func(d *ScheduleDelivery) { d.ChatID = "" },
		"no thread":                     func(d *ScheduleDelivery) { d.ThreadID = "" },
		"a padded thread":               func(d *ScheduleDelivery) { d.ThreadID = " 1763100000.000100" },
		"a control character":           func(d *ScheduleDelivery) { d.ThreadID = "1763100000.000100\n" },
		"an overlong chat":              func(d *ScheduleDelivery) { d.ChatID = strings.Repeat("C", 257) },
		"a source outside a2a-":         func(d *ScheduleDelivery) { d.IncidentSource = "kanban" },
		"a source with an upper letter": func(d *ScheduleDelivery) { d.IncidentSource = "a2a-Triage" },
		"an overlong source":            func(d *ScheduleDelivery) { d.IncidentSource = "a2a-" + strings.Repeat("a", 29) },
		"the scheduled reports' source": func(d *ScheduleDelivery) { d.IncidentSource = IncidentSourceSchedule },
		"a bad job key":                 func(d *ScheduleDelivery) { d.JobKey = "has.dot" },
		"chat mode naming a thread":     func(d *ScheduleDelivery) { d.Mode = ScheduleDeliveryChat },
		"local mode naming a source": func(d *ScheduleDelivery) {
			*d = ScheduleDelivery{Mode: ScheduleDeliveryLocal, JobKey: "a", IncidentSource: "a2a-triage"}
		},
	} {
		d := good
		edit(&d)
		if err := ValidateScheduleDelivery(d); err == nil {
			t.Errorf("%s: %+v accepted", name, d)
		}
	}
	if got := good.IncidentSourceOf(); got != ScheduleDeliveryThreadSource {
		t.Errorf("default source = %q", got)
	}
	if got := withSource.IncidentSourceOf(); got != "a2a-triage" {
		t.Errorf("source = %q", got)
	}
}

func TestATriggerReplyIsOnlyInItsSourcesNamespace(t *testing.T) {
	good := TriggerReplySubject("schedule", "abc123")
	if good != "a2a.trigger.reply.schedule.abc123" || !ValidTriggerReplySubject("schedule", good) {
		t.Fatalf("%q is not valid", good)
	}
	for _, bad := range []string{
		"", "_INBOX.operator.abc", "a2a.trigger.reply.schedule.", "a2a.trigger.reply.schedule.a.b",
		"a2a.trigger.reply.schedule.*", "a2a.trigger.reply.schedule.>", "a2a.trigger.reply.event-watcher.abc",
	} {
		if ValidTriggerReplySubject("schedule", bad) {
			t.Errorf("%q accepted", bad)
		}
	}
}

// goodEventTriggerSubmit is an event source's submission: a trigger named
// <source>/<kind>, no schedule, and a key <source>/<kind>/<token>.
func goodEventTriggerSubmit() TriggerSubmit {
	return TriggerSubmit{
		Trigger:        TriggerClaim{Name: "event-watcher/k8s-event", Origin: TriggerOriginBuiltin},
		IdempotencyKey: "event-watcher/k8s-event/k8s-evt-1a2b3c4d",
		Profile:        "cluster",
		Prompt:         "Triage the event.",
		Parameters:     map[string]string{"project": "acme-prod", "location": "us-central1", "cluster": "web"},
		Delivery:       ScheduleDelivery{Mode: ScheduleDeliveryChat, JobKey: "k8s-event"},
	}
}

func TestAWellFormedEventTriggerSubmitPasses(t *testing.T) {
	if err := ValidateTriggerSubmit("event-watcher", goodEventTriggerSubmit()); err != nil {
		t.Fatal(err)
	}
}

func TestAMalformedEventTriggerSubmitIsRefused(t *testing.T) {
	for name, mutate := range map[string]func(*TriggerSubmit){
		"another source's trigger": func(s *TriggerSubmit) { s.Trigger.Name = "drift-detector/k8s-event" },
		"a bare source as name":    func(s *TriggerSubmit) { s.Trigger.Name = "event-watcher" },
		"a dotted kind":            func(s *TriggerSubmit) { s.Trigger.Name = "event-watcher/k8s.event" },
		"a user origin":            func(s *TriggerSubmit) { s.Trigger.Origin = "user" },
		"a stream":                 func(s *TriggerSubmit) { s.Trigger.Stream = "k8s-event" },
		"a script":                 func(s *TriggerSubmit) { s.Trigger.Script = "k8s-event" },
		"a schedule":               func(s *TriggerSubmit) { s.Schedule = ScheduleRef{Namespace: "kubeagents-system", Name: "k8s-event"} },
		"a key for another kind":   func(s *TriggerSubmit) { s.IdempotencyKey = "event-watcher/gitops-drift/k8s-evt-1a2b3c4d" },
		"a key for another source": func(s *TriggerSubmit) { s.IdempotencyKey = "drift-detector/k8s-event/k8s-evt-1a2b3c4d" },
		"a schedule-shaped key":    func(s *TriggerSubmit) { s.IdempotencyKey = "schedule/kubeagents-system/k8s-event/1" },
		"a key with no token":      func(s *TriggerSubmit) { s.IdempotencyKey = "event-watcher/k8s-event/" },
		"a key with a space":       func(s *TriggerSubmit) { s.IdempotencyKey = "event-watcher/k8s-event/a b" },
		"a key with a slash":       func(s *TriggerSubmit) { s.IdempotencyKey = "event-watcher/k8s-event/a/b" },
		"a key with a colon":       func(s *TriggerSubmit) { s.IdempotencyKey = "event-watcher/k8s-event/a:b" },
		"a key too long": func(s *TriggerSubmit) {
			s.IdempotencyKey = "event-watcher/k8s-event/" + strings.Repeat("9", 600)
		},
		"an empty prompt": func(s *TriggerSubmit) { s.Prompt = "" },
		"a bad delivery":  func(s *TriggerSubmit) { s.Delivery.Mode = "" },
	} {
		t.Run(name, func(t *testing.T) {
			s := goodEventTriggerSubmit()
			mutate(&s)
			if err := ValidateTriggerSubmit("event-watcher", s); err == nil {
				t.Error("accepted")
			}
		})
	}
	// The schedule source keeps its own shape: an event-shaped submission
	// from it is refused.
	s := goodEventTriggerSubmit()
	s.Trigger.Name, s.IdempotencyKey = "schedule/k8s-event", "schedule/k8s-event/x"
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, s); err == nil {
		t.Error("the schedule source submitted without a schedule")
	}
}

// TestTheOperatorsTriggerSubmissionIsALibTriggerSubmit: the bytes the
// operator sends for a built-in schedule's run (written by its
// TestTheTriggerSubmitFixtureIsWhatTheOperatorSends, since the modules can't
// import each other) decode strictly into lib.TriggerSubmit and pass the
// gateway's check, so the two spellings can't drift apart.
// The script run's fixture (rendered-trigger-submit-script.json) is held the
// same way, and carries its script on the trigger.
func TestTheOperatorsTriggerSubmissionIsALibTriggerSubmit(t *testing.T) {
	for file, script := range map[string]string{
		"testdata/rendered-trigger-submit.json":        "",
		"testdata/rendered-trigger-submit-script.json": "compliance-audit",
	} {
		s := readOperatorTriggerSubmit(t, file)
		if err := ValidateTriggerSubmit(TriggerSourceSchedule, s); err != nil {
			t.Fatalf("%s: the operator's submission fails the gateway's check: %v", file, err)
		}
		if s.Trigger.Script != script {
			t.Errorf("%s: script = %q, want %q", file, s.Trigger.Script, script)
		}
	}
}

// An operator rolled back past spec.script reads a script schedule with no
// prompt and no script (its types have no such field), and sends an empty
// prompt with no script: the gateway refuses that, so the rollback never runs
// a script schedule as a model turn.
func TestARolledBackOperatorsScriptScheduleIsRefused(t *testing.T) {
	s := readOperatorTriggerSubmit(t, "testdata/rendered-trigger-submit-script.json")
	s.Trigger.Script, s.Prompt = "", ""
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, s); err == nil || !strings.Contains(err.Error(), "prompt is empty") {
		t.Fatalf("an empty-prompt submission without its script: err = %v, want the empty prompt refused", err)
	}
}

// readOperatorTriggerSubmit decodes an operator fixture strictly, as the
// gateway does.
func readOperatorTriggerSubmit(t *testing.T, file string) TriggerSubmit {
	t.Helper()
	raw, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	var s TriggerSubmit
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&s); err != nil {
		t.Fatalf("%s: the operator's submission doesn't decode as lib.TriggerSubmit: %v", file, err)
	}
	return s
}

// The prompt bound counts characters, as the CRD's maxLength does: a prompt at
// the bound in non-ASCII text passes, one character over is refused.
func TestThePromptBoundCountsCharacters(t *testing.T) {
	s := goodTriggerSubmit()
	s.Prompt = strings.Repeat("監", TriggerSubmitPromptMaxLen)
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, s); err != nil {
		t.Fatalf("a prompt of %d characters (%d bytes) was refused: %v", TriggerSubmitPromptMaxLen, len(s.Prompt), err)
	}
	s.Prompt += "監"
	if err := ValidateTriggerSubmit(TriggerSourceSchedule, s); err == nil || !strings.Contains(err.Error(), "characters") {
		t.Fatalf("a prompt one character over: err = %v", err)
	}
}

// The stop subject parses back to its source, and anything else is refused.
func TestTheTriggerStopSubjectParsesToItsSource(t *testing.T) {
	if got, err := ParseTriggerStopSubject(TriggerStopSubject("schedule")); err != nil || got != "schedule" {
		t.Fatalf("parse = %q, %v", got, err)
	}
	for _, bad := range []string{"a2a.trigger.schedule.status", "a2a.trigger.a.b.stop", "a2a.trigger..stop", "a2a.other.schedule.stop"} {
		if _, err := ParseTriggerStopSubject(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
	if err := ValidateTriggerStop("triage", TriggerStop{Schedule: ScheduleRef{Namespace: "ns", Name: "n"}, TaskID: "t"}); err == nil {
		t.Error("a stop from a source other than the schedule validated")
	}
}
