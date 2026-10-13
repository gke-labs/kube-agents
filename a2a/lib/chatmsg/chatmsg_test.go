package chatmsg

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// full is a message that uses every field within its caps, the spec's own
// example: each rule test breaks one thing in a copy of it.
func full() Message {
	return Message{
		Schema:   Schema,
		Headline: "3 of 12 clusters are behind the GKE stable channel",
		Note:     "checked 12 of 12",
		Detail:   "Auto-upgrade is off on all three.",
		Tone:     ToneAttention,
		Rows:     []Row{{Text: "`prod-eu` is on 1.30, stable is 1.31", Severity: "major", Detail: "since 9/12"}},
		After:    []string{"2 more are in the ledger issue."},
		Fold: &Fold{
			Title: "why",
			Text:  "Each cluster's channel and version, as `gcloud` reported them.",
			Table: &Table{Columns: []string{"cluster", "channel", "version"}, Rows: [][]string{{"prod-eu", "none", "1.30.4"}}},
		},
		Choices: []Choice{{Text: "Look at the first one", Reply: "Look at the first one: prod-eu", Primary: true}},
		Links:   []Link{{Text: "Ledger issue #231", URL: "https://github.com/o/r/issues/231"}},
		Plan: &Plan{Title: "Upgrade readiness", Items: []PlanItem{{
			ID: "t-1", Title: "Read cluster versions", Status: StatusComplete,
			Steps: []string{"listed 12 clusters"}, StepCount: 1, Result: "12 read",
		}}},
		Mark: &Mark{Ask: "upgrade", Settle: SettleDone},
	}
}

func dataPart(t *testing.T, m Message) lib.Part {
	t.Helper()
	data, err := Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	return lib.Part{Kind: "data", Data: data}
}

func rawPart(raw string) lib.Part {
	return lib.Part{Kind: "data", Data: json.RawMessage(raw)}
}

func text(s string) lib.Part {
	return lib.Part{Kind: "text", Text: s}
}

func ruleField(err error) string {
	var re *RuleError
	if errors.As(err, &re) {
		return re.Field
	}
	return ""
}

func TestTheSpecExampleIsValid(t *testing.T) {
	for _, c := range []Carrier{Result, Turn, Progress, Notice, Question, Notify, Terminal} {
		if layout, mark := Validate(full(), c); layout != nil || mark != nil {
			t.Errorf("%s: Validate(full) = %v, %v", c, layout, mark)
		}
	}
}

// TestEveryRule breaks one rule per case and expects Validate to name the
// field it broke. The caps are tested at the cap (valid) and one past it.
func TestEveryRule(t *testing.T) {
	long := func(n int) string { return strings.Repeat("a", n) }
	// A multi-byte rune: caps count code points, not bytes.
	wide := func(n int) string { return strings.Repeat("é", n) }
	cases := []struct {
		name  string
		edit  func(*Message)
		field string // "" means valid
	}{
		{"schema absent", func(m *Message) { m.Schema = "" }, "schema"},
		{"schema of another version", func(m *Message) { m.Schema = "kube-agents.chat/v2" }, "schema"},

		{"headline at cap", func(m *Message) { m.Headline = wide(headlineMax) }, ""},
		{"headline over cap", func(m *Message) { m.Headline = wide(headlineMax + 1) }, "headline"},
		{"headline two lines", func(m *Message) { m.Headline = "one\ntwo" }, "headline"},
		{"note at cap", func(m *Message) { m.Note = long(noteMax) }, ""},
		{"note over cap", func(m *Message) { m.Note = long(noteMax + 1) }, "note"},
		{"note two lines", func(m *Message) { m.Note = "one\r\ntwo" }, "note"},
		{"detail at cap, many lines", func(m *Message) { m.Detail = long(detailMax-2) + "\n\n" }, ""},
		{"detail over cap", func(m *Message) { m.Detail = long(detailMax + 1) }, "detail"},

		{"tone success", func(m *Message) { m.Tone = ToneSuccess }, ""},
		{"tone critical", func(m *Message) { m.Tone = ToneCritical }, ""},
		{"tone absent", func(m *Message) { m.Tone = "" }, ""},
		{"tone outside the set", func(m *Message) { m.Tone = "warning" }, "tone"},

		{"rows at cap", func(m *Message) { m.Rows = make([]Row, rowsMax); fillRows(m.Rows) }, ""},
		{"rows over cap", func(m *Message) { m.Rows = make([]Row, rowsMax+1); fillRows(m.Rows) }, "rows"},
		{"row text empty", func(m *Message) { m.Rows[0].Text = "" }, "rows[0].text"},
		{"row text over cap", func(m *Message) { m.Rows[0].Text = long(rowTextMax + 1) }, "rows[0].text"},
		{"row text two lines", func(m *Message) { m.Rows[0].Text = "a\nb" }, "rows[0].text"},
		{"row severity token", func(m *Message) { m.Rows[0].Severity = "sev-1" }, ""},
		{"row severity at 16", func(m *Message) { m.Rows[0].Severity = long(16) }, ""},
		{"row severity over 16", func(m *Message) { m.Rows[0].Severity = long(17) }, "rows[0].severity"},
		{"row severity uppercase", func(m *Message) { m.Rows[0].Severity = "Major" }, "rows[0].severity"},
		{"row severity with a space", func(m *Message) { m.Rows[0].Severity = "very bad" }, "rows[0].severity"},
		{"row detail over cap", func(m *Message) { m.Rows[0].Detail = long(rowTextMax + 1) }, "rows[0].detail"},
		{"row detail two lines", func(m *Message) { m.Rows[0].Detail = "a\nb" }, "rows[0].detail"},

		{"after at cap", func(m *Message) { m.After = []string{"1", "2", "3", "4", "5"} }, ""},
		{"after over cap (G1)", func(m *Message) { m.After = []string{"1", "2", "3", "4", "5", "6"} }, "after"},
		{"after line over cap", func(m *Message) { m.After = []string{long(afterLineMax + 1)} }, "after[0]"},
		{"after line two lines", func(m *Message) { m.After = []string{"a\nb"} }, "after[0]"},

		{"fold title over cap", func(m *Message) { m.Fold.Title = long(foldTitleMax + 1) }, "fold.title"},
		{"fold text at cap", func(m *Message) { m.Fold.Text = long(foldTextMax) }, ""},
		{"fold text over cap", func(m *Message) { m.Fold.Text = long(foldTextMax + 1) }, "fold.text"},
		{"fold without a table", func(m *Message) { m.Fold.Table = nil }, ""},
		{"table with no columns", func(m *Message) { m.Fold.Table = &Table{} }, "fold.table.columns"},
		{"table over column cap", func(m *Message) {
			m.Fold.Table = &Table{Columns: strings.Split("a,b,c,d,e,f,g,h,i,j,k", ",")}
		}, "fold.table.columns"},
		{"column empty", func(m *Message) { m.Fold.Table.Columns[1] = "" }, "fold.table.columns[1]"},
		{"column over cap", func(m *Message) { m.Fold.Table.Columns[1] = long(columnMax + 1) }, "fold.table.columns[1]"},
		{"table over row cap", func(m *Message) {
			m.Fold.Table.Rows = make([][]string, tableRowsMax+1)
			for i := range m.Fold.Table.Rows {
				m.Fold.Table.Rows[i] = []string{"a", "b", "c"}
			}
		}, "fold.table.rows"},
		{"table row too narrow", func(m *Message) { m.Fold.Table.Rows[0] = []string{"a", "b"} }, "fold.table.rows[0]"},
		{"table row too wide", func(m *Message) { m.Fold.Table.Rows[0] = []string{"a", "b", "c", "d"} }, "fold.table.rows[0]"},
		{"cell over cap", func(m *Message) { m.Fold.Table.Rows[0][2] = long(cellMax + 1) }, "fold.table.rows[0][2]"},

		{"choices over cap", func(m *Message) {
			m.Choices = make([]Choice, choicesMax+1)
			for i := range m.Choices {
				m.Choices[i] = Choice{Text: "option " + string(rune('a'+i))}
			}
		}, "choices"},
		{"choice text empty", func(m *Message) { m.Choices[0] = Choice{} }, "choices[0].text"},
		{"choice text at cap", func(m *Message) { m.Choices[0] = Choice{Text: long(buttonTextMax)} }, ""},
		{"choice text over cap", func(m *Message) { m.Choices[0] = Choice{Text: long(buttonTextMax + 1)} }, "choices[0].text"},
		{"reply over cap", func(m *Message) { m.Choices[0].Reply = m.Choices[0].Text + long(replyMax) }, "choices[0].reply"},
		{"reply without its text", func(m *Message) { m.Choices[0].Reply = "apply Option B" }, "choices[0].reply"},
		{"reply that leads with its text (G2)", func(m *Message) {
			m.Choices[0] = Choice{Text: "Roll back", Reply: "Roll back — apply Option B: Roll back the deploy"}
		}, ""},

		{"links over cap", func(m *Message) {
			m.Links = make([]Link, linksMax+1)
			for i := range m.Links {
				m.Links[i] = Link{Text: "doc", URL: "https://example.com/"}
			}
		}, "links"},
		{"link text empty", func(m *Message) { m.Links[0].Text = "" }, "links[0].text"},
		{"link text over cap", func(m *Message) { m.Links[0].Text = long(buttonTextMax + 1) }, "links[0].text"},
		{"link url empty", func(m *Message) { m.Links[0].URL = "" }, "links[0].url"},
		{"link url javascript", func(m *Message) { m.Links[0].URL = "javascript:alert(1)" }, "links[0].url"},
		{"link url ftp", func(m *Message) { m.Links[0].URL = "ftp://example.com/" }, "links[0].url"},
		{"link url uppercase scheme", func(m *Message) { m.Links[0].URL = "HTTPS://example.com/" }, ""},
		{"link url http", func(m *Message) { m.Links[0].URL = "http://example.com:8080/x?y=1#z" }, ""},
		{"link url no host", func(m *Message) { m.Links[0].URL = "https:///path" }, "links[0].url"},
		{"link url userinfo", func(m *Message) { m.Links[0].URL = "https://console.cloud.google.com@evil.example/" }, "links[0].url"},
		{"link url space", func(m *Message) { m.Links[0].URL = "https://example.com/a b" }, "links[0].url"},
		{"link url control character", func(m *Message) { m.Links[0].URL = "https://example.com/\x07" }, "links[0].url"},
		{"link url backslash in the host", func(m *Message) { m.Links[0].URL = `https://good.example\evil.example/` }, "links[0].url"},
		{"link url backslash in the path", func(m *Message) { m.Links[0].URL = `https://example.com/a\b` }, ""},
		// Read as the presenter's urlsplit reads them: neither a bad escape
		// nor a port that is not a number refuses the url.
		{"link url bad escape", func(m *Message) { m.Links[0].URL = "https://a.example/%zz" }, ""},
		{"link url port not a number", func(m *Message) { m.Links[0].URL = "https://a.example:abc/" }, ""},
		{"link url ipv6 host", func(m *Message) { m.Links[0].URL = "https://[::1]:8443/x" }, ""},
		{"link url empty ipv6 host", func(m *Message) { m.Links[0].URL = "https://[]/x" }, "links[0].url"},
		{"link url port alone", func(m *Message) { m.Links[0].URL = "https://:443/x" }, "links[0].url"},
		{"link url no authority", func(m *Message) { m.Links[0].URL = "https:example.com" }, "links[0].url"},
		// The host is what precedes the first colon, as urlsplit reads it,
		// and a bracket holds only an IPv6 or IPvFuture literal. Each
		// verdict below is slack_presenter._safe_link_url's on the same url.
		{"link url colons with no host", func(m *Message) { m.Links[0].URL = "https://::/" }, "links[0].url"},
		{"link url port before a colon", func(m *Message) { m.Links[0].URL = "https://:a:b/" }, "links[0].url"},
		{"link url empty port", func(m *Message) { m.Links[0].URL = "https://a.example:/" }, ""},
		{"link url two colons after a host", func(m *Message) { m.Links[0].URL = "https://a:b:c/" }, ""},
		{"link url word in brackets", func(m *Message) { m.Links[0].URL = "https://[evil]/" }, "links[0].url"},
		{"link url ipv4 in brackets", func(m *Message) { m.Links[0].URL = "https://[1.2.3.4]/" }, "links[0].url"},
		{"link url closing bracket alone", func(m *Message) { m.Links[0].URL = "https://x]/" }, "links[0].url"},
		{"link url opening bracket unclosed", func(m *Message) { m.Links[0].URL = "https://[::1/" }, "links[0].url"},
		{"link url text after the brackets", func(m *Message) { m.Links[0].URL = "https://[::1]x/" }, "links[0].url"},
		{"link url text before the brackets", func(m *Message) { m.Links[0].URL = "https://x[::1]/" }, "links[0].url"},
		{"link url ipv6 with a zone", func(m *Message) { m.Links[0].URL = "https://[fe80::1%25eth0]/" }, ""},
		{"link url ipvfuture", func(m *Message) { m.Links[0].URL = "https://[v1.x]/" }, ""},
		// One reading of the url decides both rules: a label naming the host
		// of a url Go's url.Parse would refuse is still honest.
		{"label naming the host of a url with a bad escape", func(m *Message) {
			m.Links[0] = Link{Text: "https://a.example", URL: "https://a.example/%zz"}
		}, ""},
		{"label naming the host of a url with a word for a port", func(m *Message) {
			m.Links[0] = Link{Text: "https://A.example/x", URL: "https://a.example:abc/"}
		}, ""},
		{"label naming another host than an ipv6 url's", func(m *Message) {
			m.Links[0] = Link{Text: "https://good.example", URL: "https://[::1]/"}
		}, "links[0].text"},
		{"link url at cap", func(m *Message) { m.Links[0].URL = "https://example.com/" + long(urlMax-20) }, ""},
		{"link url over cap", func(m *Message) { m.Links[0].URL = "https://example.com/" + long(urlMax-19) }, "links[0].url"},
		{"label naming the url's host", func(m *Message) {
			m.Links[0] = Link{Text: "https://github.com/o/r", URL: "https://github.com/o/r/pull/1"}
		}, ""},
		{"label naming another host", func(m *Message) {
			m.Links[0] = Link{Text: "https://good.example", URL: "https://evil.example/"}
		}, "links[0].text"},
		{"label with marks naming another host", func(m *Message) {
			m.Links[0] = Link{Text: "**https**://good.example", URL: "https://evil.example/"}
		}, "links[0].text"},
		{"label with a bare hostname is prose", func(m *Message) {
			m.Links[0] = Link{Text: "good.example", URL: "https://evil.example/"}
		}, ""},

		{"primary on a link alone", func(m *Message) { m.Choices[0].Primary = false; m.Links[0].Primary = true }, ""},
		{"primary on two choices", func(m *Message) {
			m.Choices = append(m.Choices, Choice{Text: "Skip it", Primary: true})
		}, "primary"},
		{"primary on a choice and a link", func(m *Message) { m.Links[0].Primary = true }, "primary"},

		{"plan title over cap", func(m *Message) { m.Plan.Title = long(planTitleMax + 1) }, "plan.title"},
		{"plan items over cap", func(m *Message) {
			m.Plan.Items = make([]PlanItem, planItemsMax+1)
			for i := range m.Plan.Items {
				m.Plan.Items[i] = PlanItem{ID: "t-" + string(rune('a'+i)), Status: StatusPending}
			}
		}, "plan.items"},
		{"item id empty", func(m *Message) { m.Plan.Items[0].ID = "" }, "plan.items[0].id"},
		{"item id over cap", func(m *Message) { m.Plan.Items[0].ID = long(itemIDMax + 1) }, "plan.items[0].id"},
		{"item id repeated", func(m *Message) {
			m.Plan.Items = append(m.Plan.Items, PlanItem{ID: "t-1", Status: StatusPending})
		}, "plan.items[1].id"},
		{"item title over cap", func(m *Message) { m.Plan.Items[0].Title = long(itemTitleMax + 1) }, "plan.items[0].title"},
		{"item status pending", func(m *Message) { m.Plan.Items[0].Status = StatusPending }, ""},
		{"item status in_progress", func(m *Message) { m.Plan.Items[0].Status = StatusInProgress }, ""},
		{"item status error", func(m *Message) { m.Plan.Items[0].Status = StatusError }, ""},
		{"item status absent", func(m *Message) { m.Plan.Items[0].Status = "" }, "plan.items[0].status"},
		{"item status outside the set", func(m *Message) { m.Plan.Items[0].Status = "running" }, "plan.items[0].status"},
		{"item steps over cap", func(m *Message) {
			m.Plan.Items[0].Steps = []string{"1", "2", "3", "4", "5", "6", "7"}
		}, "plan.items[0].steps"},
		{"item step over cap", func(m *Message) { m.Plan.Items[0].Steps = []string{long(stepMax + 1)} }, "plan.items[0].steps[0]"},
		{"item step two lines", func(m *Message) { m.Plan.Items[0].Steps = []string{"a\nb"} }, "plan.items[0].steps[0]"},
		{"item stepCount negative", func(m *Message) { m.Plan.Items[0].StepCount = -1 }, "plan.items[0].stepCount"},
		{"item stepCount past the kept steps", func(m *Message) { m.Plan.Items[0].StepCount = 40 }, ""},
		{"item note over cap", func(m *Message) { m.Plan.Items[0].Note = long(itemNoteMax + 1) }, "plan.items[0].note"},
		{"item result over cap", func(m *Message) { m.Plan.Items[0].Result = long(itemResultMax + 1) }, "plan.items[0].result"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := full()
			tc.edit(&m)
			layout, mark := Validate(m, Progress)
			if mark != nil {
				t.Fatalf("mark error %v from a layout edit", mark)
			}
			if got := ruleField(layout); got != tc.field || (tc.field != "" && layout == nil) {
				t.Errorf("Validate = %v (field %q), want field %q", layout, got, tc.field)
			}
		})
	}
}

func fillRows(rows []Row) {
	for i := range rows {
		rows[i] = Row{Text: "row"}
	}
}

// TestChoicesTheGatewayWouldActOn: a click posts the choice as the
// clicker's own message, so one the gateway reads as a command is refused,
// in text or reply, judged against the superset (the wide status ask)
// whatever the carrier.
func TestChoicesTheGatewayWouldActOn(t *testing.T) {
	commands := []string{
		"stop", "Stop!", "cancel", "abort", "CANCEL.",
		"status", "any update?", "what is it doing", "how is it going",
		"what is the agent doing", // the wide form only
		"/session", "/session off", "/SESSION what is running",
		"delegate check the nodes", "Delegate: fix it",
	}
	for _, cmd := range commands {
		m := full()
		m.Choices = []Choice{{Text: cmd}}
		if layout, _ := Validate(m, Result); ruleField(layout) != "choices[0].text" {
			t.Errorf("choice text %q: Validate = %v, want a choices[0].text refusal", cmd, layout)
		}
	}
	// The reply is judged on its own: a harmless label whose reply is a
	// command is refused.
	replies := map[string]string{
		"Cancel it": "Cancel it", // valid: a stop word matches only alone
		"off":       "/session off",
		"fix":       "delegate: fix prod-eu",
		"going":     "how is it going",
	}
	for label, reply := range replies {
		m := full()
		m.Choices = []Choice{{Text: label, Reply: reply}}
		layout, _ := Validate(m, Notify)
		want := "choices[0].reply"
		if label == "Cancel it" {
			want = ""
		}
		if got := ruleField(layout); got != want {
			t.Errorf("reply %q: Validate = %v, want field %q", reply, layout, want)
		}
	}
	// A reply that is the command itself and contains the text.
	m := full()
	m.Choices = []Choice{{Text: "stop", Reply: "stop"}}
	if layout, _ := Validate(m, Notice); ruleField(layout) != "choices[0].text" {
		t.Errorf("Validate = %v, want the text refused first", layout)
	}
	m.Choices = []Choice{{Text: "any", Reply: "any update?"}}
	if layout, _ := Validate(m, Notice); ruleField(layout) != "choices[0].reply" {
		t.Errorf("Validate = %v, want a choices[0].reply refusal", layout)
	}
	m.Choices = []Choice{{Text: "session", Reply: "/session off"}}
	if layout, _ := Validate(m, Notice); ruleField(layout) != "choices[0].reply" {
		t.Errorf("Validate = %v, want a choices[0].reply refusal", layout)
	}
	// Words that only resemble a command are fine.
	for _, ok := range []string{"Stop the deploy", "Status page", "delegated work", "Cancel the rollout of v2", "Try again"} {
		m := full()
		m.Choices = []Choice{{Text: ok}}
		if layout, _ := Validate(m, Result); layout != nil {
			t.Errorf("choice %q: Validate = %v, want valid", ok, layout)
		}
	}
}

func TestMarkRules(t *testing.T) {
	cases := []struct {
		name  string
		mark  Mark
		field string
	}{
		{"empty is the whole state with nothing set", Mark{}, ""},
		{"ask alone", Mark{Ask: "question"}, ""},
		{"settle alone", Mark{Settle: SettleBlocked}, ""},
		{"failed", Mark{Ask: "incident", Settle: SettleFailed}, ""},
		{"ask uppercase", Mark{Ask: "Upgrade"}, "mark.ask"},
		{"ask over 16", Mark{Ask: strings.Repeat("a", 17)}, "mark.ask"},
		{"ask an emoji name", Mark{Ask: "arrow_up"}, "mark.ask"},
		{"settle outside the set", Mark{Settle: "canceled"}, "mark.settle"},
	}
	for _, tc := range cases {
		m := full()
		m.Mark = &tc.mark
		layout, mark := Validate(m, Result)
		if layout != nil {
			t.Errorf("%s: layout error %v from a mark edit", tc.name, layout)
		}
		if got := ruleField(mark); got != tc.field {
			t.Errorf("%s: mark error %v, want field %q", tc.name, mark, tc.field)
		}
	}
}

// TestParse: unknown fields are ignored so v1 can grow; a mark that does
// not decode is left nil and reported as the mark error, the rest kept; any
// other type error fails the whole message.
func TestParse(t *testing.T) {
	m, err := Parse([]byte(`{"schema":"kube-agents.chat/v1","headline":"h","future":{"x":1},"mark":"upgrade"}`))
	if err != nil {
		t.Fatal(err)
	}
	if m.Headline != "h" || m.Mark != nil {
		t.Fatalf("Parse = %+v", m)
	}
	if layout, mark := Validate(m, Result); layout != nil || ruleField(mark) != "mark" {
		t.Errorf("Validate = %v, %v; want only a mark error", layout, mark)
	}
	m, err = Parse([]byte(`{"schema":"kube-agents.chat/v1","mark":null}`))
	if err != nil || m.Mark != nil {
		t.Errorf("a null mark: Parse = %+v, %v; want absent", m, err)
	}
	m, err = Parse([]byte(`{"schema":"kube-agents.chat/v1","mark":{}}`))
	if err != nil || m.Mark == nil || *m.Mark != (Mark{}) {
		t.Errorf("an empty mark: Parse = %+v, %v; want present and empty", m, err)
	}
	// Keys match exactly, at every depth: a key spelled in another case is
	// unknown and ignored, never read as the field, and never fails the
	// message by its type.
	m, err = Parse([]byte(`{"schema":"kube-agents.chat/v1","Headline":"x","MARK":"bad",` +
		`"rows":[{"Text":"y","text":"row"}],"plan":{"items":[{"id":"a","status":"pending","StepCount":"z"}]},` +
		`"mark":{"Ask":"Not A Token","settle":"done"}}`))
	if err != nil {
		t.Fatalf("Parse refused a message for keys it should ignore: %v", err)
	}
	if m.Headline != "" || m.Rows[0].Text != "row" || m.Plan.Items[0].StepCount != 0 || m.Mark == nil || *m.Mark != (Mark{Settle: SettleDone}) {
		t.Errorf("Parse read a key spelled in another case: %+v (mark %+v)", m, m.Mark)
	}
	if layout, mark := Validate(m, Progress); layout != nil || mark != nil {
		t.Errorf("Validate = %v, %v", layout, mark)
	}
	for _, bad := range []string{
		`[]`, `"x"`, `null`, `{"schema":"kube-agents.chat/v1","rows":"r"}`,
		`{"schema":"kube-agents.chat/v1","plan":{"items":[{"id":"a","status":"pending","stepCount":1.5}]}}`,
	} {
		if _, err := Parse([]byte(bad)); err == nil {
			t.Errorf("Parse(%s) = nil error", bad)
		}
	}
}

func TestEncodeRoundTrips(t *testing.T) {
	data, err := Encode(full())
	if err != nil {
		t.Fatal(err)
	}
	m, err := Parse(data)
	if err != nil {
		t.Fatal(err)
	}
	again, err := Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	if string(again) != string(data) {
		t.Errorf("round trip changed the object:\n%s\n%s", data, again)
	}
	// An empty mark survives as {}, the whole state with nothing set.
	data, err = Encode(Message{Mark: &Mark{}})
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"schema":"kube-agents.chat/v1","mark":{}}`; string(data) != want {
		t.Errorf("Encode = %s, want %s", data, want)
	}
}

func TestFallbackSelection(t *testing.T) {
	parts := []lib.Part{text("first "), rawPart(`{"tool":"x"}`), text("second")}
	cases := []struct {
		c    Carrier
		want string
	}{
		{Result, "first second"},
		{Turn, "first second"},
		{Notice, "first second"},
		{Question, "first second"},
		{Progress, "second"},
		{Terminal, ""},
		{Notify, ""},
	}
	for _, tc := range cases {
		if got := Fallback(tc.c, parts); got != tc.want {
			t.Errorf("Fallback(%s) = %q, want %q", tc.c, got, tc.want)
		}
	}
	if got := ReadNotify("the text", nil).Fallback; got != "the text" {
		t.Errorf("ReadNotify fallback = %q", got)
	}
}

func TestReadRendersAValidMessage(t *testing.T) {
	r := Read(Notice, []lib.Part{text("3 of 12 clusters are behind."), dataPart(t, full())})
	if r.Message == nil || len(r.Problems) != 0 {
		t.Fatalf("Read = %+v", r)
	}
	if r.Mark == nil || r.Mark.Ask != "upgrade" {
		t.Errorf("mark = %+v", r.Mark)
	}
	if r.Message.Plan != nil {
		t.Error("a plan is read only in progress and on a notify; a notice's is dropped from the layout")
	}
	if r := Read(Progress, []lib.Part{text("plan"), dataPart(t, full())}); r.Message == nil || r.Message.Plan == nil {
		t.Errorf("progress lost its plan: %+v", r)
	}
	raw, _ := Encode(full())
	if r := ReadNotify("the text", raw); r.Message == nil || r.Message.Plan == nil {
		t.Errorf("a notify lost its plan: %+v", r)
	}
}

func TestReadIgnoresOtherDataParts(t *testing.T) {
	parts := []lib.Part{
		text("done"),
		rawPart(`{"steerNotice":{"steer":"queued","envelopeId":"e"}}`),
		rawPart(`{"schema":"kube-agents.topic/v1","x":1}`),
		rawPart(`not json`),
	}
	if r := Read(Result, parts); r.Message != nil || len(r.Problems) != 0 || r.Fallback != "done" {
		t.Errorf("Read = %+v; want the text alone and no problem", r)
	}
}

// TestAssertion27 is the spec's assertion 27: a chat message is never
// emitted without its text fallback, except the mark-only one on a terminal
// status-update, and a result or turn carries one only on its last chunk,
// with the artifact's text as folded per A2A chunking rules as its fallback.
func TestAssertion27(t *testing.T) {
	t.Run("the emitter refuses a missing fallback", func(t *testing.T) {
		for _, c := range []Carrier{Progress, Notice, Question} {
			if _, err := Parts(c, "  ", full()); ruleField(err) != "fallback" {
				t.Errorf("Parts(%s, blank) = %v, want a fallback refusal", c, err)
			}
			parts, err := Parts(c, "the answer", full())
			if err != nil || len(parts) != 2 || parts[0].Text != "the answer" {
				t.Errorf("Parts(%s) = %+v, %v", c, parts, err)
			}
		}
		if _, err := Parts(Result, "x", full()); err == nil {
			t.Error("Parts(result) accepted: a result's fallback is the folded artifact, not a part beside it")
		}
		if _, err := Parts(Terminal, "x", full()); err == nil {
			t.Error("Parts(terminal) accepted: a terminal carries mark alone")
		}
	})
	t.Run("the renderer refuses a missing fallback", func(t *testing.T) {
		for _, c := range []Carrier{Result, Turn, Progress, Notice, Question} {
			r := Read(c, []lib.Part{dataPart(t, full())})
			if r.Message != nil || r.Mark != nil || ruleField(firstProblem(r)) != "fallback" {
				t.Errorf("Read(%s) with no text = %+v", c, r)
			}
		}
		if r := ReadNotify("", mustEncode(t, full())); r.Message != nil || ruleField(firstProblem(r)) != "fallback" {
			t.Errorf("ReadNotify with no text = %+v", r)
		}
	})
	t.Run("a terminal carries mark alone with no fallback", func(t *testing.T) {
		part, err := MarkPart(Mark{Ask: "upgrade"})
		if err != nil {
			t.Fatal(err)
		}
		r := Read(Terminal, []lib.Part{part})
		if r.Message != nil || r.Mark == nil || r.Mark.Ask != "upgrade" || len(r.Problems) != 0 {
			t.Errorf("Read(terminal) = %+v", r)
		}
		// Only the mark is read and validated: a layout field over its cap
		// is not looked at, a bad mark is.
		m := full()
		m.Headline = strings.Repeat("a", headlineMax+1)
		if r := Read(Terminal, []lib.Part{dataPart(t, m)}); r.Mark == nil || len(r.Problems) != 0 || r.Message != nil {
			t.Errorf("Read(terminal) read past the mark: %+v", r)
		}
		// Nor is a layout field's type: it cannot cost the mark.
		raw := `{"schema":"kube-agents.chat/v1","rows":"x","plan":7,"mark":{"settle":"failed"}}`
		if r := Read(Terminal, []lib.Part{rawPart(raw)}); r.Mark == nil || r.Mark.Settle != SettleFailed || len(r.Problems) != 0 {
			t.Errorf("Read(terminal, a mistyped layout field) = %+v", r)
		}
		if r := Read(Terminal, []lib.Part{rawPart(`{"schema":"kube-agents.chat/v2","mark":{"settle":"failed"}}`)}); r.Mark != nil {
			t.Errorf("Read(terminal) applied a mark under an unknown schema: %+v", r)
		}
		if _, err := MarkPart(Mark{Settle: "nope"}); ruleField(err) != "mark.settle" {
			t.Errorf("MarkPart(bad) = %v", err)
		}
	})
	t.Run("a result carries one only on its last chunk", func(t *testing.T) {
		chunk := func(parts []lib.Part, appendTo, last bool) lib.ArtifactUpdate {
			return lib.ArtifactUpdate{Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: parts}, Append: appendTo, LastChunk: last}
		}
		good := []lib.ArtifactUpdate{
			chunk([]lib.Part{text("3 of 12 clusters ")}, false, false),
			chunk([]lib.Part{text("are behind.")}, true, false),
			chunk([]lib.Part{text(""), dataPart(t, full())}, true, true),
		}
		for _, c := range []Carrier{Result, Turn} {
			r := ReadArtifact(c, good)
			if r.Message == nil || r.Fallback != "3 of 12 clusters are behind." || len(r.Problems) != 0 {
				t.Errorf("ReadArtifact(%s) = %+v", c, r)
			}
		}
		early := []lib.ArtifactUpdate{
			chunk([]lib.Part{text("3 of 12 "), dataPart(t, full())}, false, false),
			chunk([]lib.Part{text("are behind.")}, true, true),
		}
		r := ReadArtifact(Result, early)
		if r.Message != nil || r.Mark != nil || ruleField(firstProblem(r)) != "chunk" || r.Fallback != "3 of 12 are behind." {
			t.Errorf("ReadArtifact(chat message on a middle chunk) = %+v", r)
		}
		// A chunk that does not append replaces what came before, as A2A
		// folds it, and so does the fallback.
		replaced := []lib.ArtifactUpdate{
			chunk([]lib.Part{text("draft")}, false, false),
			chunk([]lib.Part{text("final"), dataPart(t, full())}, false, true),
		}
		if r := ReadArtifact(Result, replaced); r.Fallback != "final" || r.Message == nil {
			t.Errorf("ReadArtifact(replace) = %+v", r)
		}
	})
}

// TestAssertion28 is the validator's half of assertion 28: a chat message
// that breaks a rule yields the fallback and nothing from the chat message,
// with the reason; a bad mark alone drops only the mark; and a choice the
// gateway would act on as a command (a stop word, cancel included; a
// status ask; /session; the delegate prefix) is refused. The mention half
// ("no field renders as a mention") is the renderer's escaping, tested
// where the renderer lives.
func TestAssertion28(t *testing.T) {
	const fallback = "3 of 12 clusters are behind the GKE stable channel."
	breaks := map[string]func(*Message){
		"a cap":              func(m *Message) { m.Headline = strings.Repeat("a", headlineMax+1) },
		"a tone":             func(m *Message) { m.Tone = "blue" },
		"a status":           func(m *Message) { m.Plan.Items[0].Status = "done" },
		"an unsafe url":      func(m *Message) { m.Links[0].URL = "javascript:alert(1)" },
		"a misnaming label":  func(m *Message) { m.Links[0].Text = "https://good.example" },
		"a reply":            func(m *Message) { m.Choices[0].Reply = "something else" },
		"a stop word":        func(m *Message) { m.Choices[0] = Choice{Text: "stop"} },
		"cancel":             func(m *Message) { m.Choices[0] = Choice{Text: "Cancel"} },
		"a status ask":       func(m *Message) { m.Choices[0] = Choice{Text: "any update?"} },
		"/session":           func(m *Message) { m.Choices[0] = Choice{Text: "/session"} },
		"the delegate word":  func(m *Message) { m.Choices[0] = Choice{Text: "delegate fix prod-eu"} },
		"two primaries":      func(m *Message) { m.Links[0].Primary = true },
		"an unknown version": func(m *Message) { m.Schema = "kube-agents.chat/v9" },
	}
	for name, edit := range breaks {
		m := full()
		edit(&m)
		r := Read(Progress, []lib.Part{text(fallback), dataPart(t, m)})
		if r.Message != nil || r.Mark != nil {
			t.Errorf("%s: something of the chat message survived: %+v", name, r)
		}
		if r.Fallback != fallback || len(r.Problems) != 1 || r.Problems[0].Error() == "" {
			t.Errorf("%s: Read = %+v; want the fallback and one reason", name, r)
		}
	}

	t.Run("a bad mark drops alone", func(t *testing.T) {
		m := full()
		m.Mark = &Mark{Ask: "Not A Token"}
		r := Read(Result, []lib.Part{text(fallback), dataPart(t, m)})
		if r.Message == nil || r.Mark != nil || r.Message.Mark != nil || ruleField(firstProblem(r)) != "mark.ask" {
			t.Errorf("Read = %+v", r)
		}
		raw := `{"schema":"kube-agents.chat/v1","headline":"h","mark":["x"]}`
		r = Read(Result, []lib.Part{text(fallback), rawPart(raw)})
		if r.Message == nil || r.Message.Headline != "h" || r.Mark != nil || ruleField(firstProblem(r)) != "mark" {
			t.Errorf("Read(undecodable mark) = %+v", r)
		}
	})
	t.Run("two chat messages in one part list", func(t *testing.T) {
		r := Read(Notice, []lib.Part{text(fallback), dataPart(t, full()), dataPart(t, full())})
		if r.Message != nil || r.Mark != nil || ruleField(firstProblem(r)) != "parts" {
			t.Errorf("Read = %+v", r)
		}
	})
	t.Run("a chat message that is not an object of the schema", func(t *testing.T) {
		r := Read(Notice, []lib.Part{text(fallback), rawPart(`{"schema":"kube-agents.chat/v1","rows":{}}`)})
		if r.Message != nil || len(r.Problems) != 1 {
			t.Errorf("Read = %+v", r)
		}
	})
}

func firstProblem(r Reading) error {
	if len(r.Problems) == 0 {
		return nil
	}
	return r.Problems[0]
}

func mustEncode(t *testing.T, m Message) json.RawMessage {
	t.Helper()
	data, err := Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestMarkOnly(t *testing.T) {
	if !(Message{Schema: Schema, Mark: &Mark{}}).MarkOnly() {
		t.Error("a message holding only mark is mark-only")
	}
	if full().MarkOnly() {
		t.Error("the full example is not mark-only")
	}
}
