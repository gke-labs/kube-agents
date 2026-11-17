package controller

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const dispatchDurablesFixturePath = "../../../a2a/lib/testdata/rendered-dispatch-durables.json"

// The config is written compact, and without the fields the dispatcher
// derives: each entry's durables, and the Job template's name (#5479).
func TestTheDispatchConfigIsWrittenCompactWithNoDerivedFields(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})}
	templates := dispatchJobTemplates(agent, profiles, false)
	cm, err := buildA2ADispatchConfigMapWith(agent, profiles, templates, nil)
	if err != nil {
		t.Fatal(err)
	}
	body := cm.Data[a2aDispatchConfigKey]
	if strings.Contains(body, "\n") {
		t.Error("the dispatch config is indented; want compact")
	}
	var doc struct {
		Profiles []map[string]json.RawMessage `json:"profiles"`
	}
	if err := json.Unmarshal([]byte(body), &doc); err != nil || len(doc.Profiles) != 1 {
		t.Fatalf("profiles %+v, err %v", doc.Profiles, err)
	}
	if _, ok := doc.Profiles[0]["durables"]; ok {
		t.Error("the entry lists its durables; the dispatcher derives them")
	}
	var ref map[string]string
	if err := json.Unmarshal(doc.Profiles[0]["jobTemplateRef"], &ref); err != nil {
		t.Fatalf("jobTemplateRef: %v", err)
	}
	if _, ok := ref["name"]; ok || ref["hash"] == "" {
		t.Errorf("jobTemplateRef %v; want the hash alone", ref)
	}
	// The operator still knows the durables it creates and grants.
	if _, durables := dispatcherGrantNames(agent, profiles, nil); len(durables) != 4 {
		t.Errorf("grant names list %d durables; want 4 (two tiers, two kinds)", len(durables))
	}
}

// 2,500 profiles that allow both tiers, named the way the installer names
// cluster agents, with Job mode on: the design's target (#4440). The config
// has to fit one ConfigMap (1 MiB) with room to spare.
func TestTheDispatchConfigFitsTwentyFiveHundredTwoTierProfiles(t *testing.T) {
	withOperatorBusPrincipal(t)
	// With the dispatcher off, as fleetScaleProfileFixtures has it: on, its
	// identity ceiling binds only the oldest few hundred (#5381), and this
	// is the config's own size at the target.
	t.Setenv(a2aProfileDispatcherEnvVar, "false")
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := make([]agentv1alpha1.AgentProfile, 0, 2500)
	for i := range 2500 {
		profiles = append(profiles, testAgentProfile(agent.Namespace, fmt.Sprintf("team-%03d-cluster-%02d", i/25, i%25), func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
		}))
	}
	templates := dispatchJobTemplates(agent, profiles, false)
	cm, err := buildA2ADispatchConfigMapWith(agent, profiles, templates, nil)
	if err != nil {
		t.Fatal(err)
	}
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatal(err)
	}
	refs := 0
	for _, p := range cfg.Profiles {
		if p.JobTemplateRef != nil {
			refs++
		}
	}
	if len(cfg.Profiles) != len(profiles) || refs != len(profiles) {
		t.Fatalf("%d entries, %d with a Job template reference, for %d profiles: the fixture isn't the target", len(cfg.Profiles), refs, len(profiles))
	}
	size := len(cm.Data[a2aDispatchConfigKey])
	const configMapLimit = 1 << 20
	// Measured at 445,568 bytes (42%) with the test profile's three
	// parameters. Held to half a ConfigMap, so the other half is headroom
	// for longer names and parameter lists.
	const target = configMapLimit / 2
	t.Logf("2,500 two-tier profiles: %d bytes, %.0f%% of a ConfigMap", size, 100*float64(size)/configMapLimit)
	if size > target {
		t.Errorf("the dispatch config is %d bytes at 2,500 two-tier profiles; want at most %d (1 MiB is %d)", size, target, configMapLimit)
	}
}

// The durables the operator creates for each profile (desiredProfileDurables,
// held equal here to the config's list), which the dispatcher now derives
// instead of reading: the operator can't import a2a/lib, so this
// fixture, read by lib's TestTheOperatorsDurablesAreTheDerivedOnes, holds the
// two derivations equal. It covers a one-tier profile, a two-tier one, one
// draining a tier (#5638), and a draining profile.
func TestTheDispatchDurablesFixture(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	until := time.Date(2026, 11, 12, 6, 0, 0, 0, time.UTC)
	both := []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	profiles := []agentv1alpha1.AgentProfile{
		testAgentProfile(agent.Namespace, "reader", func(*agentv1alpha1.AgentProfile) {}),
		testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = both }),
		testAgentProfile(agent.Namespace, "narrowing", func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = both
			if p.Annotations == nil {
				p.Annotations = map[string]string{}
			}
			p.Annotations[drainingTiersAnnotation] = fmt.Sprintf("%s=%d", agentv1alpha1.AgentProfileTierUnattended, until.Unix())
		}),
	}
	draining := []a2aDispatchProfile{{Name: "leaving", Tiers: []string{agentv1alpha1.AgentProfileTierRead}, Concurrency: 1, DrainingUntil: until}}
	cfg, err := desiredDispatchConfigDraining(agent, profiles, draining)
	if err != nil {
		t.Fatal(err)
	}
	type entry struct {
		Name          string    `json:"name"`
		Tiers         []string  `json:"tiers"`
		DrainingTiers []string  `json:"drainingTiers,omitempty"`
		DrainingUntil time.Time `json:"drainingUntil,omitzero"`
		Durables      []string  `json:"durables"`
	}
	// The durables the operator actually creates on the bus come from
	// desiredProfileDurables (the AgentProfile controller's SyncDurables),
	// not from the config's in-memory list: hold the two equal here, so the
	// fixture pins what is created, not only what is listed.
	created := map[string][]string{}
	for i := range profiles {
		for _, d := range desiredProfileDurables(&profiles[i]) {
			created[profiles[i].Name] = append(created[profiles[i].Name], d.name)
		}
		slices.Sort(created[profiles[i].Name])
	}
	var out []entry
	for _, p := range cfg.Profiles {
		e := entry{Name: p.Name, Tiers: p.Tiers, DrainingUntil: p.DrainingUntil, Durables: []string{}}
		for _, d := range p.DrainingTiers {
			e.DrainingTiers = append(e.DrainingTiers, d.Tier)
		}
		for _, d := range p.Durables {
			e.Durables = append(e.Durables, d.Name)
		}
		slices.Sort(e.Durables)
		if want, ok := created[p.Name]; ok && !slices.Equal(want, e.Durables) {
			t.Errorf("profile %s: the config lists durables %v, the controller creates %v", p.Name, e.Durables, want)
		}
		out = append(out, e)
	}
	body, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchDurablesFixturePath, string(body)+"\n")
}

// The ceiling's config measure (a2aDispatchConfigShares) charges each entry
// as the operator writes it: no durables, and a Job template referenced by
// its hash alone (#5479). Base plus the shares is at least what is written,
// and over only by the first entry's comma, which the measure's own comment
// promises, for every shape a profile's entry takes. A measure that still
// counted listed durables, a template name, or a template for a tier that
// isn't live, would be hundreds of bytes over here.
func TestTheDispatchConfigMeasureIsTheWrittenShape(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "false") // no cut: measure all of them
	t.Setenv(a2aDispatcherImageEnvVar, testDispatcherImage)
	agent := a2aTestAgent()
	read := []string{agentv1alpha1.AgentProfileTierRead}
	both := []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	until := time.Now().Add(time.Hour).Unix()
	drainUnattended := func(p *agentv1alpha1.AgentProfile) {
		if p.Annotations == nil {
			p.Annotations = map[string]string{}
		}
		p.Annotations[drainingTiersAnnotation] = fmt.Sprintf("%s=%d", agentv1alpha1.AgentProfileTierUnattended, until)
	}
	// A Tier resource whose mode is unattended, as renderableAgentProfiles
	// leaves it on the copy the renderers read (#3659).
	batchTier := func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, "batch"}
		if p.Annotations == nil {
			p.Annotations = map[string]string{}
		}
		p.Annotations[renderedTierModesAnnotation] = "batch=" + tierModeUnattended
	}
	sized := func(tiers []string, mutate ...func(*agentv1alpha1.AgentProfile)) func(*agentv1alpha1.PlatformAgent) []agentv1alpha1.AgentProfile {
		return func(agent *agentv1alpha1.PlatformAgent) []agentv1alpha1.AgentProfile {
			profiles := twoTierProfiles(agent, 30)
			for i := range profiles {
				profiles[i].Spec.AllowedTiers = tiers
				for _, m := range mutate {
					m(&profiles[i])
				}
			}
			return profiles
		}
	}
	for _, tc := range []struct {
		name     string
		jobMode  bool
		profiles func(*agentv1alpha1.PlatformAgent) []agentv1alpha1.AgentProfile
	}{
		{"one tier", false, sized(read)},
		{"one tier, Job mode", true, sized(read)},
		{"two tiers", false, sized(both)},
		{"two tiers, Job mode", true, sized(both)},
		{"draining unattended", false, sized(both, drainUnattended)},
		{"draining unattended, Job mode", true, sized(both, drainUnattended)},
		{"an unattended Tier resource, Job mode", true, sized(nil, batchTier)},
		{"parameter-heavy, Job mode", true, func(agent *agentv1alpha1.PlatformAgent) []agentv1alpha1.AgentProfile {
			return parameterHeavyProfiles(agent, 30)
		}},
		{"mixed, Job mode", true, func(agent *agentv1alpha1.PlatformAgent) []agentv1alpha1.AgentProfile {
			profiles := parameterHeavyProfiles(agent, 30)
			for i := range profiles {
				switch i % 4 {
				case 1:
					profiles[i].Spec.AllowedTiers = read
				case 2:
					drainUnattended(&profiles[i])
				case 3:
					batchTier(&profiles[i])
				}
			}
			return profiles
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
			if tc.jobMode {
				t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
			}
			profiles := tc.profiles(agent)
			var ptrs []*agentv1alpha1.AgentProfile
			for i := range profiles {
				ptrs = append(ptrs, &profiles[i])
			}
			base, shares, err := a2aDispatchConfigShares(agent, ptrs)
			if err != nil {
				t.Fatal(err)
			}
			estimate := base
			for _, n := range shares {
				estimate += n
			}
			for _, imageVolumes := range []bool{true, false} {
				var templates map[string]*corev1.PodTemplateSpec
				if tc.jobMode {
					templates = dispatchJobTemplates(agent, profiles, imageVolumes)
				}
				cm, err := buildA2ADispatchConfigMapWith(agent, profiles, templates, nil)
				if err != nil {
					t.Fatal(err)
				}
				written := len(cm.Data[a2aDispatchConfigKey])
				// The base carries Job mode's labels whether or not any
				// profile takes a template (the operator writes them only
				// when one does): a fixed charge, not a per-entry one.
				slack := 1
				if tc.jobMode && len(templates) == 0 {
					slack += len(`,"jobLabels":`) + jsonLen(dispatchJobLabels(agent))
				}
				if estimate < written {
					t.Errorf("imageVolumes=%v: base plus shares is %d bytes, under the %d written", imageVolumes, estimate, written)
				}
				if estimate-written > slack {
					t.Errorf("imageVolumes=%v: base plus shares is %d bytes, %d over the %d written; want at most %d",
						imageVolumes, estimate, estimate-written, written, slack)
				}
			}
		})
	}
}
