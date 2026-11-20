package lib

import (
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
