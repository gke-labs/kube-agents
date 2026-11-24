package controller

import (
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"testing"
)

// The inject door's chat.notify route (lib.NotifySubjectInject) is granted
// only under the operator's eval flag (A2A_INJECT_BACKEND). These hold the
// render to that: off, the subject is nowhere -- no identity's grants, no
// auth-map entry, no account export or import; on, it appears exactly once
// in each place the console's notify subject appears, as that one subject
// and nothing broader; and the other notify grants are the same either way.

// notifyRequestInject is the subject, spelled here independently of the
// render's constant so a renamed constant cannot carry the check with it.
const notifyRequestInject = "chat.notify.inject"

// injectNotifyRender is everything the render produces that a grant could
// hide in: every identity's lists, the callout's auth map, and nats.conf.
type injectNotifyRender struct {
	ids     []a2aIdentity
	authMap string
	conf    string
}

func renderForInjectNotify(t *testing.T) injectNotifyRender {
	t.Helper()
	return renderForInjectNotifyWith(t, a2aTestCalloutKeys(t))
}

// renderForInjectNotifyWith renders with the callout keys given, so two
// renders differ only where the flag moves them.
func renderForInjectNotifyWith(t *testing.T, keys *a2aCalloutKeys) injectNotifyRender {
	t.Helper()
	agent := a2aTestAgent()
	doc, err := renderA2AAuthMap(agent, nil)
	if err != nil {
		t.Fatalf("rendering the auth map: %v", err)
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), keys, nil).Data["nats.conf"])
	return injectNotifyRender{ids: a2aIdentities(agent), authMap: string(raw), conf: conf}
}

// grantsReaching lists "user verb grant" for every grant, across ids, that
// overlaps subject.
func grantsReaching(ids []a2aIdentity, subject string) []string {
	var out []string
	for _, id := range ids {
		for _, g := range id.publish {
			if subjectPatternsOverlap(g, subject) {
				out = append(out, id.user+" publish "+g)
			}
		}
		for _, g := range id.subscribe {
			if subjectPatternsOverlap(g, subject) {
				out = append(out, id.user+" subscribe "+g)
			}
		}
	}
	return out
}

// TestInjectNotifyGrantIsAbsentWithTheEvalFlagOff is (a): the default
// render names the inject notify subject nowhere.
func TestInjectNotifyGrantIsAbsentWithTheEvalFlagOff(t *testing.T) {
	for _, value := range []string{"", "false", "TRUE", "1", "yes"} {
		t.Run("flag="+value, func(t *testing.T) {
			t.Setenv(a2aInjectBackendEnvVar, value)
			if a2aInjectBackendEnabled() {
				t.Fatalf("%s=%q reads as on", a2aInjectBackendEnvVar, value)
			}
			if got := a2aInjectNotifyGrants(); got != nil {
				t.Fatalf("a2aInjectNotifyGrants() = %q with the flag off, want nil", got)
			}
			r := renderForInjectNotify(t)
			// Exact grants only: a wildcard that overlaps the subject is the
			// other notify tests' business, and chat.notify.reply.agent.> is
			// not one. What this pins is the literal grant and its imports.
			for _, id := range r.ids {
				for verb, list := range map[string][]string{"publish": id.publish, "subscribe": id.subscribe} {
					if slices.Contains(list, notifyRequestInject) {
						t.Errorf("%s %s %s with the eval flag off", id.user, verb, notifyRequestInject)
					}
				}
			}
			if strings.Contains(r.authMap, "notify.inject") {
				t.Errorf("the auth map names the inject notify subject with the eval flag off")
			}
			if strings.Contains(r.conf, "notify.inject") {
				t.Errorf("nats.conf names the inject notify subject with the eval flag off")
			}
			if l := a2aInjectNotifyExportLines() + a2aInjectNotifyImportLines(); l != "" {
				t.Errorf("export/import lines with the eval flag off: %q", l)
			}
		})
	}
}

// TestInjectNotifyGrantMirrorsTheConsolesWithTheEvalFlagOn is (b): with the
// flag on, the subject appears exactly once in each place the console's
// notify subject does, held by the same principals, and nowhere broader.
func TestInjectNotifyGrantMirrorsTheConsolesWithTheEvalFlagOn(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	if got := a2aInjectNotifyGrants(); !reflect.DeepEqual(got, []string{notifyRequestInject}) {
		t.Fatalf("a2aInjectNotifyGrants() = %q, want exactly [%s]", got, notifyRequestInject)
	}
	r := renderForInjectNotify(t)

	// Identities: every exact grant of the console's subject has its inject
	// twin, on the same principal and verb, once; and nothing else reaches
	// the inject subject, by literal or by wildcard.
	var console, inject []string
	for _, id := range r.ids {
		for verb, list := range map[string][]string{"publish": id.publish, "subscribe": id.subscribe} {
			for _, g := range list {
				switch g {
				case notifyRequestConsole:
					console = append(console, id.user+" "+verb)
				case notifyRequestInject:
					inject = append(inject, id.user+" "+verb)
				}
			}
		}
	}
	slices.Sort(console)
	slices.Sort(inject)
	if want := []string{"agent publish", "gateway subscribe"}; !reflect.DeepEqual(console, want) {
		t.Fatalf("the console notify grants moved: %q, want %q", console, want)
	}
	if !reflect.DeepEqual(inject, console) {
		t.Errorf("inject notify grants = %q, want the console's %q", inject, console)
	}
	reaching := grantsReaching(r.ids, notifyRequestInject)
	slices.Sort(reaching)
	if want := []string{"agent publish " + notifyRequestInject, "gateway subscribe " + notifyRequestInject}; !reflect.DeepEqual(reaching, want) {
		t.Errorf("grants reaching %s = %q, want exactly %q", notifyRequestInject, reaching, want)
	}
	// No inject-gated grant is a wildcard, or anything but the one subject.
	for _, g := range a2aInjectNotifyGrants() {
		if strings.ContainsAny(g, "*>") || g != notifyRequestInject {
			t.Errorf("inject notify grant %q is not exactly %s", g, notifyRequestInject)
		}
	}

	// The callout's auth map: the agent's publish and the gateway's
	// subscribe, as many times as the console's subject (once each).
	if got, want := strings.Count(r.authMap, `"`+notifyRequestInject+`"`), strings.Count(r.authMap, `"`+notifyRequestConsole+`"`); got != want || got != 2 {
		t.Errorf("the auth map names %s %d times and %s %d times; want twice each (agent publish, gateway subscribe)", notifyRequestInject, got, notifyRequestConsole, want)
	}

	// nats.conf: the gateway's subscribe allow-list, APP's export and
	// TRUSTED's import, each once, each where the console's is.
	if got, want := strings.Count(r.conf, `"`+notifyRequestInject+`"`), strings.Count(r.conf, `"`+notifyRequestConsole+`"`); got != want {
		t.Errorf("nats.conf names %s %d times and %s %d times; want the same", notifyRequestInject, got, notifyRequestConsole, want)
	}
	for _, line := range []string{
		`{ stream: "chat.notify.inject", accounts: [ TRUSTED ] }`,
		`{ stream: { account: APP, subject: "chat.notify.inject" } }`,
	} {
		if n := strings.Count(r.conf, line); n != 1 {
			t.Errorf("nats.conf carries %q %d times, want once", line, n)
		}
	}
	// And the export and import sit beside the console's, in the account
	// blocks the console's do.
	for _, pair := range [][2]string{
		{`{ stream: "chat.notify.console", accounts: [ TRUSTED ] }`, `{ stream: "chat.notify.inject", accounts: [ TRUSTED ] }`},
		{`{ stream: { account: APP, subject: "chat.notify.console" } }`, `{ stream: { account: APP, subject: "chat.notify.inject" } }`},
	} {
		if !strings.Contains(r.conf, pair[0]+"\n      "+pair[1]+"\n") {
			t.Errorf("nats.conf does not carry %q directly after %q", pair[1], pair[0])
		}
	}
	if sub := a2aGrantSubjects(t, r.conf, "gateway", "subscribe"); !slices.Contains(sub, notifyRequestInject) {
		t.Errorf("the gateway's served subscribe allow-list lacks %s: %q", notifyRequestInject, sub)
	}
	for _, user := range []string{a2aBridgeUser, "web", "seed"} {
		for _, section := range []string{"publish", "subscribe"} {
			if list, ok := a2aGrantList(t, r.conf, user, section, "allow"); ok && slices.ContainsFunc(list, func(g string) bool { return subjectPatternsOverlap(g, notifyRequestInject) }) {
				t.Errorf("%s's served %s allow-list reaches %s: %q", user, section, notifyRequestInject, list)
			}
		}
	}
	// The one-writer, one-reader door holds with the subject in it.
	for _, v := range notifyDoorViolations(r.ids) {
		t.Error(v)
	}
}

// TestInjectNotifyFlagLeavesTheOtherNotifyGrantsAlone is (c): the
// console, Slack, Chat and Teams notify grants are the same with the flag on
// as off, and with it on the only change to any identity is the one subject.
func TestInjectNotifyFlagLeavesTheOtherNotifyGrantsAlone(t *testing.T) {
	keys := a2aTestCalloutKeys(t)
	t.Setenv(a2aInjectBackendEnvVar, "")
	off := renderForInjectNotifyWith(t, keys)
	t.Setenv(a2aInjectBackendEnvVar, "true")
	on := renderForInjectNotifyWith(t, keys)

	for _, subject := range []string{notifyRequest, notifyRequestSlack, notifyRequestTeams, notifyRequestConsole, notifyReply} {
		if a, b := grantsReaching(off.ids, subject), grantsReaching(on.ids, subject); !reflect.DeepEqual(a, b) {
			t.Errorf("grants reaching %s changed with the flag: off %q, on %q", subject, a, b)
		}
	}
	if len(off.ids) != len(on.ids) {
		t.Fatalf("the flag changed the identity count: %d off, %d on", len(off.ids), len(on.ids))
	}
	without := func(list []string) []string {
		return slices.DeleteFunc(slices.Clone(list), func(g string) bool { return g == notifyRequestInject })
	}
	for i := range off.ids {
		a, b := off.ids[i], on.ids[i]
		if a.user != b.user {
			t.Fatalf("identity %d is %s off and %s on", i, a.user, b.user)
		}
		if !reflect.DeepEqual(a.publish, without(b.publish)) || !reflect.DeepEqual(a.subscribe, without(b.subscribe)) {
			t.Errorf("%s's grants changed beyond %s with the flag on", a.user, notifyRequestInject)
		}
		if !reflect.DeepEqual(a.denyPublish, b.denyPublish) || !reflect.DeepEqual(a.denySubscribe, b.denySubscribe) {
			t.Errorf("%s's denies changed with the flag on", a.user)
		}
	}
	// nats.conf with the lines naming the inject subject taken out is the
	// default config (list separators aside: the gateway's subscribe list
	// gains an element, so the line before it gains a comma), and exactly
	// three lines name it.
	normalize := func(conf string) ([]string, int) {
		var lines []string
		dropped := 0
		for _, l := range strings.Split(conf, "\n") {
			if strings.Contains(l, notifyRequestInject) {
				dropped++
				continue
			}
			lines = append(lines, strings.TrimSuffix(l, ","))
		}
		return lines, dropped
	}
	offLines, offDropped := normalize(off.conf)
	onLines, onDropped := normalize(on.conf)
	if offDropped != 0 || onDropped != 3 {
		t.Errorf("lines naming %s: %d off, %d on; want 0 and 3 (gateway subscribe, APP export, TRUSTED import)", notifyRequestInject, offDropped, onDropped)
	}
	if !reflect.DeepEqual(offLines, onLines) {
		t.Errorf("nats.conf differs with the flag on beyond the inject notify lines")
	}
}
