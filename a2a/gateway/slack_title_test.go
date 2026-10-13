package gateway

import (
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

// Ported from SessionTest in agents/platform/scripts/test_slack_status.py, so
// the two paths title a thread alike.

// Characters agents.sessions.rename answered invalid_name for, one per call,
// on a live workspace (2026-10-02). Each one alone refuses the whole title.
const slackRenameRefused = "/:·…∕—–‒―−•→×<>#@\\*~`%;[]{}+$^‘’²½Ⅳ " +
	"ªºʰ𝐀©®™⌘←∑°§¶€£¥ﬁǅ〜、。！？¿¡«»⬆〒🀄⌚⏰⭐〽ㅋำ‍​\U0001f1ef"

// Words and symbols it accepted on the same workspace.
var slackRenameAccepted = []string{
	"नमस्ते", "ไม่", "q̃", "क़", "بً", "ラーメン", "々", "ʻ", "ꙮ", "ᄀ", "٣", "𠀀",
	"✅", "🚨", "⚠️", "★", "✓", "👍\U0001f3fd", "1️⃣",
}

func TestSlackSessionTitle(t *testing.T) {
	cases := map[string]string{
		"<@U1> is <#C1|prod> ok: <https://a.b/c>":                                                "is prod ok",
		"is <@U123> ok with <#C1|ops> and <https://x.example/a|the doc> or <https://y.example>?": "is ok with ops and the doc or ?",
		"check <!here> <!subteam^S1|@oncall> kube-system/coredns: now":                           "check kube-system-coredns, now",
		"café/bar restarts":                 "café-bar restarts",
		"kube-system／coredns restarts":       "kube-system-coredns restarts",
		"kube‐system restarts":               "kube-system restarts",
		"kube‑system restarts":               "kube-system restarts",
		"pods ↂ/x ok":                        "pods x ok",
		"⭐️ deploy":                          "deploy",
		"#️⃣ rollout":                        "rollout",
		"‼️⃣/x now":                          "!! x now",
		"⚠️ deploy":                          "⚠️ deploy",
		"pods — restarts":                    "pods - restarts",
		"wait…":                              "wait...",
		"check /metrics on a/b":              "check metrics on a-b",
		"what’s down?":                       "what's down?",
		"cpu at 90% × 3":                     "cpu at 90 percent x 3",
		"café":                               "café",
		"café":                              "café",
		"x² ﬁle":                             "x2 file",
		"are pods ok、yes。":                   "are pods ok, yes.",
		"👨‍👩 🇯🇵 team":                        "👨👩 team",
		"Restart " + strings.Repeat("n", 90): "Restart " + strings.Repeat("n", 69) + slackTitleEllipsis,
		strings.Repeat("word ", 40):          strings.TrimRight(strings.Repeat("word ", 15), " ") + slackTitleEllipsis,
		"Check the platform-agent-host cluster: are any pods outside kube-system not Running, and is any node under memory pressure?": "Check the platform-agent-host cluster, are any pods outside kube-system not...",
	}
	for ask, want := range cases {
		if got := slackSessionTitle(ask); got != want {
			t.Errorf("slackSessionTitle(%q) = %q, want %q", ask, got, want)
		}
	}
	for _, left := range []string{"है", "ใช่", "कि", "q̃"} {
		if got, want := slackSessionTitle(left+"/coredns ok"), left+"-coredns ok"; got != want {
			t.Errorf("a slash after a word ending in a mark: got %q, want %q", got, want)
		}
	}
	if got := utf8.RuneCountInString(slackSessionTitle(strings.Repeat("x", 200))); got > slackTitleMax {
		t.Errorf("title of %d code points, over the %d cap", got, slackTitleMax)
	}
}

func TestSlackSessionTitleRefusedAndAccepted(t *testing.T) {
	for _, refused := range slackRenameRefused {
		if got := slackSessionTitle("pods " + string(refused) + " restarts"); strings.ContainsRune(got, refused) {
			t.Errorf("U+%04X survived: %q", refused, got)
		}
	}
	for _, accepted := range slackRenameAccepted {
		if got, want := slackSessionTitle("pods "+accepted+" ok"), "pods "+accepted+" ok"; got != want {
			t.Errorf("accepted %q: got %q, want %q", accepted, got, want)
		}
	}
}

// Every letter Slack refused has a compatibility form (ª, ﬁ, 𝐀, ำ). This
// checks the output against that pattern, not against the rule that built it,
// over the BMP and the two supplementary planes that hold letters and emoji.
func TestSlackSessionTitleNoCompatibilityForm(t *testing.T) {
	const checkedEnd = 0x2FFFF
	seen := map[rune]bool{}
	for code := rune(0); code <= checkedEnd; code++ {
		if code >= 0xD800 && code < 0xE000 {
			continue
		}
		for _, r := range slackSessionTitle("a" + string(code) + "b") {
			seen[r] = true
		}
	}
	for r := range seen {
		if norm.NFKC.String(string(r)) != string(r) {
			t.Errorf("U+%04X has a compatibility form and survived", r)
		}
	}
}

func TestSlackSessionTitleLinearOnUnclosedMarkup(t *testing.T) {
	const slackMessageMax = 40_000
	for _, unit := range []string{"<", "<!", "<@", "<#A|", "<a|"} {
		ask := strings.Repeat(unit, slackMessageMax/len(unit))
		start := time.Now()
		slackSessionTitle(ask)
		if d := time.Since(start); d > time.Second {
			t.Errorf("%q x %d took %v", unit, slackMessageMax/len(unit), d)
		}
	}
}
