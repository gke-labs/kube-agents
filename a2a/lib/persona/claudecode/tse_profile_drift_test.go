package claudecode

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The GKE Support Agent has two homes: the persona (agents/tse/persona, which
// a profile worker mounts at /persona) and the Hermes tse profile's sources
// (agents/tse/SOUL.md, AGENTS.md and the support-escalation-bundle skill).
// instructions.md is a rewrite of the profile's text, so the two can't be
// compared whole (#6184). These tests compare what must agree: the passages
// both carry word for word, and every red line in AGENTS.md's Scope & Red
// Lines, SOUL.md's Core Truths and the persona's §2, each one either carried
// to the other home or left out there for a reason this file states, so a
// rule added to one of those sections fails here until it is placed; and the
// redaction red line against the skill's Step 2. The skill's own copy
// is held to its source by TestTheTsePersonaSkillsMatchTheirSources.
const tseProfileDir = "../../../../agents/tse"

// tseFlat is tseNormalizeMarkdown with every run of whitespace, line breaks
// included, made one space, so rewrapping a paragraph changes nothing.
func tseFlat(s string) string {
	return strings.Join(strings.Fields(tseNormalizeMarkdown(s)), " ")
}

func tseRead(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(tseProfileDir, name))
	if err != nil {
		t.Fatalf("reading %s: %v", name, err)
	}
	return string(b)
}

// tseSection is text from the line holding heading to the next heading of the
// same or a higher level, exclusive.
func tseSection(t *testing.T, text, heading, file string) string {
	t.Helper()
	lines := strings.Split(text, "\n")
	level := len(heading) - len(strings.TrimLeft(heading, "#"))
	start := -1
	for i, l := range lines {
		if start < 0 {
			if strings.TrimSpace(l) == heading {
				start = i + 1
			}
			continue
		}
		if m := regexp.MustCompile(`^(#+) `).FindStringSubmatch(l); m != nil && len(m[1]) <= level {
			return strings.Join(lines[start:i], "\n")
		}
	}
	if start < 0 {
		t.Fatalf("%s has no %q heading", file, heading)
	}
	return strings.Join(lines[start:], "\n")
}

// tseItems is a section's top-level list items (bullets, or numbered when
// numbered is set), each with its indented continuation lines joined on.
func tseItems(section string, numbered bool) []string {
	marker := regexp.MustCompile(`^[-*+] `)
	if numbered {
		marker = regexp.MustCompile(`^\d+\. `)
	}
	var items []string
	for _, l := range strings.Split(section, "\n") {
		switch {
		case marker.MatchString(l):
			items = append(items, marker.ReplaceAllString(l, ""))
		case len(items) > 0 && strings.HasPrefix(l, "  ") && strings.TrimSpace(l) != "":
			items[len(items)-1] += "\n" + l
		}
	}
	return items
}

// tseLead is an item's bold lead without its trailing punctuation, or its
// whole first line when it has none.
func tseLead(item string) string {
	if m := regexp.MustCompile(`^\*\*(.+?)\*\*`).FindStringSubmatch(item); m != nil {
		return strings.TrimRight(m[1], ".:")
	}
	return strings.SplitN(item, "\n", 2)[0]
}

// tseFirstParagraph is the first paragraph after a file's title.
func tseFirstParagraph(t *testing.T, text, file string) string {
	t.Helper()
	parts := strings.Split(strings.TrimSpace(text), "\n\n")
	if len(parts) < 2 || !strings.HasPrefix(parts[0], "# ") {
		t.Fatalf("%s doesn't open with a title and a paragraph", file)
	}
	return parts[1]
}

// The passages the persona carries word for word: who the agent is, the
// diagnostic loop's opening line, its first and third stages, the Google-side
// escalation criteria of its fourth, the self-audit gate and the tone. The
// second stage and the fourth's customer-resolvable case are rewrites (the
// persona names its own tools and hands patches to whoever asked rather than
// to the Platform Agent), so they are not compared.
func TestTheTsePersonaKeepsTheHermesProfilesSharedText(t *testing.T) {
	soul := tseRead(t, "SOUL.md")
	instructions := tseRead(t, "persona/instructions.md")
	same := func(what, hermes, persona string) {
		t.Helper()
		if strings.TrimSpace(hermes) == "" {
			t.Errorf("%s is gone from SOUL.md; this test compares it with persona/instructions.md", what)
			return
		}
		if tseFlat(hermes) != tseFlat(persona) {
			t.Errorf("%s differs between SOUL.md and persona/instructions.md; change both together:\nSOUL.md:      %q\ninstructions: %q", what, hermes, persona)
		}
	}
	same("who the agent is", tseFirstParagraph(t, soul, "SOUL.md"), tseFirstParagraph(t, instructions, "instructions.md"))

	tone := regexp.MustCompile(`(?s)<tone_preference>.*</tone_preference>`)
	same("the tone", tone.FindString(soul), tone.FindString(instructions))

	hermesGate := tseItems(tseSection(t, soul, "## 2. Pre-Report Self-Audit Gate", "SOUL.md"), true)
	personaGate := tseItems(tseSection(t, instructions, "## 5. Pre-report self-audit gate", "instructions.md"), true)
	if len(hermesGate) != 3 || len(personaGate) != len(hermesGate) {
		t.Fatalf("self-audit gate: SOUL.md has %d questions and instructions.md %d, want the same 3", len(hermesGate), len(personaGate))
	}
	for i := range hermesGate {
		same("self-audit question "+string(rune('1'+i)), hermesGate[i], personaGate[i])
	}

	loop := tseItems(tseSection(t, soul, "## 1. Core Truths", "SOUL.md"), false)[0]
	var hermesStages []string
	for _, l := range strings.Split(loop, "\n") {
		if m := regexp.MustCompile(`^\s+\d+\. (.*)`).FindStringSubmatch(l); m != nil {
			hermesStages = append(hermesStages, m[1])
		}
	}
	personaStages := tseItems(tseSection(t, instructions, "## 3. The 4-stage diagnostic loop", "instructions.md"), true)
	if len(hermesStages) != 4 || len(personaStages) != 4 {
		t.Fatalf("diagnostic loop: SOUL.md has %d stages and instructions.md %d, want 4 each", len(hermesStages), len(personaStages))
	}
	for _, i := range []int{0, 2} {
		same("diagnostic stage "+string(rune('1'+i)), hermesStages[i], strings.SplitN(personaStages[i], "\n", 2)[0])
	}
	same("the diagnostic loop's opening line",
		strings.SplitN(strings.TrimSpace(regexp.MustCompile(`^\*\*[^*]+\*\*\s*`).ReplaceAllString(loop, "")), "\n", 2)[0],
		strings.TrimSpace(strings.SplitN(strings.TrimSpace(tseSection(t, instructions, "## 3. The 4-stage diagnostic loop", "instructions.md")), "\n", 2)[0]))
	escalation := func(text, file string) string {
		m := regexp.MustCompile(`(?m)^\s*- (\*\*Google-Side Escalations.*)$`).FindStringSubmatch(text)
		if m == nil {
			t.Fatalf("%s's fourth stage has no Google-Side Escalations case", file)
		}
		return m[1]
	}
	same("the Google-side escalation criteria", escalation(loop, "SOUL.md"), escalation(personaStages[3], "instructions.md"))
}

// tseRedLineMap places each red line the Hermes profile states. A value
// beginning "persona: " names the persona's §2 bullet that carries it, by its
// lead; "verbatim" means the persona's bullet must equal it; any other value
// is why the persona leaves it out.
var tseRedLineMap = map[string]string{
	// AGENTS.md, Scope & Red Lines.
	"Read-only diagnostics only":           "persona: Read-only",
	"No direct GitOps writes":              "persona: No GitOps write path",
	"Zero unverified internal assumptions": "persona: Zero unverified internal assumptions",
	"Kanban worker":                        "a profile task has no kanban card: the result is the final answer (§6) and milestones are progress lines (§7)",
	"Fail loud, never silent":              "persona: Fail loud, never silent",
	"Skills the image ships are image-owned; do not edit them, report the gap": "persona: Your skills are read-only",
	"Never expose raw passwords, Secret payloads, or GCP/GKE keys.":            "verbatim",
	// SOUL.md, Core Truths.
	"Hypothesis-Driven 4-Stage Diagnostic Loop":             "the persona's §3; its shared stages and escalation criteria are compared in TestTheTsePersonaKeepsTheHermesProfilesSharedText",
	"Grounded, Current Knowledge (No Guessing from Memory)": "the persona's §4, which keeps never answer_query (TestTheTsePersonaStatesItsRedLines)",
	"Read-Only Boundary":                                    "persona: Read-only",
	"Never Wildcard the Project":                            "persona: Never wildcard the project",
}

// The persona's §2 bullets with no Hermes counterpart, and why.
var tsePersonaOnlyRedLines = map[string]string{
	"The target cluster only":       "a profile task names its one target cluster; Hermes reads it from the kanban card",
	"A denial is terminal":          "the broker refuses a profile worker; the Hermes profile reaches the cluster another way",
	"Redact before anything leaves": "the support-escalation-bundle skill's Step 2, stated as a red line (TestTheTseRedactionRedLineCoversTheSkillsRules)",
}

// Every red line in either home is carried to the other or left out on
// purpose. A rule added to AGENTS.md's Scope & Red Lines or SOUL.md's Core
// Truths, or to the persona's §2, fails here until tseRedLineMap or
// tsePersonaOnlyRedLines places it.
func TestEveryTseRedLineIsCarriedOrLeftOutOnPurpose(t *testing.T) {
	agents := tseRead(t, "AGENTS.md")
	soul := tseRead(t, "SOUL.md")
	instructions := tseRead(t, "persona/instructions.md")

	personaBullets := map[string]string{}
	for _, b := range tseItems(tseSection(t, instructions, "## 2. Scope and red lines", "instructions.md"), false) {
		if _, dup := personaBullets[tseLead(b)]; dup {
			t.Errorf("persona/instructions.md §2 has two red lines led %q; give each its own lead", tseLead(b))
		}
		personaBullets[tseLead(b)] = b
	}
	carried := map[string]bool{}
	var hermes []string
	hermes = append(hermes, tseItems(tseSection(t, agents, "## Scope & Red Lines", "AGENTS.md"), false)...)
	hermes = append(hermes, tseItems(tseSection(t, soul, "## 1. Core Truths", "SOUL.md"), false)...)
	for _, b := range hermes {
		lead := tseLead(b)
		place, ok := tseRedLineMap[lead]
		switch {
		case !ok:
			t.Errorf("the Hermes profile states a red line this test doesn't place: %q. Carry it to persona/instructions.md §2 and map it, or say in tseRedLineMap why the persona leaves it out", lead)
		case place == "verbatim":
			if tseFlat(personaBullets[lead]) != tseFlat(b) {
				t.Errorf("red line %q is to be carried word for word, and persona/instructions.md §2 has %q", lead, personaBullets[lead])
			}
			carried[lead] = true
		case strings.HasPrefix(place, "persona: "):
			target := strings.TrimPrefix(place, "persona: ")
			if _, found := personaBullets[target]; !found {
				t.Errorf("red line %q maps to the persona's %q, which persona/instructions.md §2 no longer has", lead, target)
			}
			carried[target] = true
		}
	}
	for lead := range personaBullets {
		if !carried[lead] && tsePersonaOnlyRedLines[lead] == "" {
			t.Errorf("persona/instructions.md §2 states a red line this test doesn't place: %q. Carry it to the Hermes profile and map it, or say in tsePersonaOnlyRedLines why Hermes doesn't need it", lead)
		}
	}
	for lead := range tsePersonaOnlyRedLines {
		if _, found := personaBullets[lead]; !found {
			t.Errorf("tsePersonaOnlyRedLines names %q, which persona/instructions.md §2 no longer has", lead)
		}
	}
}

// tseRedactionTerms maps each thing the skill's Step 2 never includes to the
// words the persona's redaction red line names it by.
var tseRedactionTerms = map[string]string{
	"Kubernetes `Secret` data payloads":                         "Secret payloads",
	"environment variable values containing passwords/API keys": "password or API-key values",
	"bearer tokens": "bearer",
	"OAuth tokens":  "OAuth tokens",
	"private keys":  "private keys",
}

// The persona's redaction red line names everything the skill's Step 2 says
// never to include, and keeps what it says to keep. A category added to the
// skill fails here until the red line names it.
func TestTheTseRedactionRedLineCoversTheSkillsRules(t *testing.T) {
	skill := tseRead(t, "skills/support-escalation-bundle/SKILL.md")
	instructions := tseRead(t, "persona/instructions.md")
	var redLine string
	for _, b := range tseItems(tseSection(t, instructions, "## 2. Scope and red lines", "instructions.md"), false) {
		if tseLead(b) == "Redact before anything leaves" {
			redLine = b
		}
	}
	if redLine == "" {
		t.Fatal("persona/instructions.md §2 has no redaction red line")
	}
	var never, keep string
	for _, b := range tseItems(tseSection(t, skill, "## Step 2: Enforce Strict Redaction Rules", "SKILL.md"), false) {
		switch tseLead(b) {
		case "Never include":
			never = strings.TrimSuffix(strings.TrimSpace(strings.TrimPrefix(b, "**Never include**")), ".")
		case "Keep":
			keep = b
		default:
			t.Errorf("the skill's Step 2 has a rule this test doesn't check, %q; carry it to the persona's redaction red line and check it here", tseLead(b))
		}
	}
	if never == "" || keep == "" {
		t.Fatalf("the skill's Step 2 no longer has its Never include and Keep rules")
	}
	for _, item := range regexp.MustCompile(`,\s*(?:or\s+)?`).Split(never, -1) {
		item = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(item), "or "))
		words, ok := tseRedactionTerms[item]
		if !ok {
			t.Errorf("the skill's Step 2 never includes %q, which the persona's redaction red line doesn't name; name it there and add it to tseRedactionTerms", item)
			continue
		}
		if !strings.Contains(redLine, words) {
			t.Errorf("the persona's redaction red line no longer names %q (the skill's %q)", words, item)
		}
	}
	if !strings.Contains(keep, "resource identifiers") || !strings.Contains(redLine, "Keep the resource identifiers") {
		t.Errorf("the skill and the persona no longer both keep the resource identifiers Google Cloud Support needs")
	}
}
