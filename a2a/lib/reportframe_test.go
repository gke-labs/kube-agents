package lib

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// The golden file holds the hermes-bridge's frame output (incidentReply)
// from before the frame moved here, for each case: FrameReport has to give
// the same bytes, so a session pod and a bridge pod show a report alike.
func TestFrameReport_MatchesTheBridgeGolden(t *testing.T) {
	raw, err := os.ReadFile("testdata/reportframe-golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name, Report, User, Want string
	}
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("golden file has no cases")
	}
	for _, c := range cases {
		got := FrameReport(c.Report, c.User)
		if got != c.Want {
			t.Errorf("%s: FrameReport differs from the bridge's output\n got %q\nwant %q", c.Name, got, c.Want)
		}
		assertReportFrameIntact(t, c.Name, got, c.User)
	}
}

// assertReportFrameIntact checks a framed prompt's shape: exactly one open
// and one close marker ahead of the user's words, the open first, the report
// between them carrying no marker or neutralised token, and userText after
// the close. The user's words are the person's own and are not neutralised,
// so the counts stop where they start.
func assertReportFrameIntact(t *testing.T, name, framed, userText string) {
	t.Helper()
	frame := strings.TrimSuffix(framed, ReportFrameUser+userText)
	if n := strings.Count(frame, "<untrusted_report>"); n != 1 {
		t.Errorf("%s: %d open markers, want 1:\n%q", name, n, framed)
	}
	if n := strings.Count(frame, "</untrusted_report>"); n != 1 {
		t.Errorf("%s: %d close markers, want 1:\n%q", name, n, framed)
	}
	if n := strings.Count(frame, "[SECURITY NOTICE:"); n != 1 {
		t.Errorf("%s: %d security notices, want 1:\n%q", name, n, framed)
	}
	if !strings.HasPrefix(framed, ReportFrameOpen) {
		t.Errorf("%s: does not open with the frame:\n%q", name, framed)
	}
	closeAt := strings.Index(framed, ReportFrameClose)
	if closeAt < 0 {
		t.Fatalf("%s: no close:\n%q", name, framed)
	}
	if rest := framed[closeAt+len(ReportFrameClose):]; rest != ReportFrameUser+userText {
		t.Errorf("%s: after the close %q, want the user's words %q", name, rest, ReportFrameUser+userText)
	}
	body := framed[len(ReportFrameOpen):closeAt]
	for _, tok := range reportTokens {
		if tok.Pattern.MatchString(body) {
			t.Errorf("%s: report body still carries %s:\n%q", name, tok.Pattern, body)
		}
	}
}

func TestReportTokensIsACopy(t *testing.T) {
	got := ReportTokens()
	got[0].Replacement = "changed"
	if reportTokens[0].Replacement == "changed" {
		t.Fatal("ReportTokens shares the list NeutralizeReport applies")
	}
}
