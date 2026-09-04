package profiles

import (
	"embed"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
)

//go:embed *.yaml
var configs embed.FS

// specFieldTable is the field set docs/designs/spec-subagent-profiles.md
// documents for the AgentProfile spec, as JSON paths. It is transcribed from
// the doc's field table plus the two worked profiles, and the test below holds
// the struct to it in both directions: a field the struct grew without the doc
// is an undocumented knob, and a field the doc has without the struct is a
// profile that cannot express what the spec promises.
var specFieldTable = []string{
	"bus.publishTopics",
	"bus.subscribeTopics",
	"clusterRef.cluster",
	"clusterRef.location",
	"clusterRef.projectId",
	"concurrency",
	"description",
	"harness.image",
	"harness.maxTurns",
	"harness.model",
	"identity.serviceAccountName",
	"lifecycle.activeDeadlineSeconds",
	"lifecycle.ttlSecondsAfterFinished",
	"persona.image",
	"queueTimeoutSeconds",
	"resources.limits.cpu",
	"resources.limits.memory",
	"resources.requests.cpu",
	"resources.requests.memory",
}

func TestFieldsMatchTheSpecTable(t *testing.T) {
	got := jsonPaths(reflect.TypeOf(Spec{}), "")
	sort.Strings(got)
	want := append([]string(nil), specFieldTable...)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("the Spec struct and the spec's field table disagree.\n struct: %v\n  table: %v", got, want)
	}
}

// jsonPaths walks a struct's JSON tags into dotted paths, descending into
// nested structs and pointers to them.
func jsonPaths(t reflect.Type, prefix string) []string {
	var out []string
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		tag := strings.Split(f.Tag.Get("json"), ",")[0]
		if tag == "" || tag == "-" {
			continue
		}
		ft := f.Type
		if ft.Kind() == reflect.Ptr {
			ft = ft.Elem()
		}
		if ft.Kind() == reflect.Struct {
			out = append(out, jsonPaths(ft, prefix+tag+".")...)
			continue
		}
		out = append(out, prefix+tag)
	}
	return out
}

// TestShippedProfilesParse - every profile config file in this package parses
// strictly against the field table and passes validation. Only the chat front
// door ships here; specialist profiles land with the components that run them.
func TestShippedProfilesParse(t *testing.T) {
	entries, err := configs.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, e := range entries {
		data, err := configs.ReadFile(e.Name())
		if err != nil {
			t.Fatal(err)
		}
		p, err := Load(e.Name(), data)
		if err != nil {
			t.Errorf("%s: %v", e.Name(), err)
			continue
		}
		seen[p.Name] = true
		t.Logf("%s: %d publish grants, %d subscribe grants, concurrency %d, queue timeout %ds",
			p.Name, len(p.Spec.Bus.PublishTopics), len(p.Spec.Bus.SubscribeTopics),
			p.Spec.Concurrency, p.QueueTimeout())
	}
	if !seen["chat"] {
		t.Error("no chat profile shipped")
	}
}

func TestUnknownFieldIsRefused(t *testing.T) {
	base, err := configs.ReadFile("chat.yaml")
	if err != nil {
		t.Fatal(err)
	}
	// A plausible misspelling of a real grant field. Parsing this quietly
	// would produce a profile whose grants silently do not exist.
	mangled := append(append([]byte(nil), base...), []byte("\nbusGrants:\n  - shared.blueprint\n")...)
	if _, err := Load("chat.yaml", mangled); err == nil {
		t.Fatal("expected an unknown field to be refused")
	}
}

func TestValidateRejects(t *testing.T) {
	valid := func() Spec {
		return Spec{
			Description: "a profile",
			Persona:     Persona{Image: "registry/persona:1"},
			Harness:     Harness{Image: "registry/worker:1"},
			Lifecycle:   Lifecycle{ActiveDeadlineSeconds: 1800, TTLSecondsAfterFinished: 600},
			Concurrency: 1,
			Resources: Resources{
				Requests: ResourceList{CPU: "250m", Memory: "512Mi"},
				Limits:   ResourceList{CPU: "1", Memory: "2Gi"},
			},
		}
	}
	cases := []struct {
		name    string
		profile string
		mutate  func(*Spec)
	}{
		{"no description", "p", func(s *Spec) { s.Description = " " }},
		{"no persona image", "p", func(s *Spec) { s.Persona.Image = "" }},
		{"no harness image", "p", func(s *Spec) { s.Harness.Image = "" }},
		{"no deadline", "p", func(s *Spec) { s.Lifecycle.ActiveDeadlineSeconds = 0 }},
		{"no concurrency", "p", func(s *Spec) { s.Concurrency = 0 }},
		{"partial resources", "p", func(s *Spec) { s.Resources.Limits.Memory = "" }},
		{"dotted profile name", "my.profile", func(s *Spec) {}},
		{"uppercase profile name", "Chat", func(s *Spec) {}},
		{"topic grant with a dotted token", "p", func(s *Spec) {
			s.Bus.PublishTopics = []string{"shared.up.grade"}
		}},
		{"topic grant with no scope", "p", func(s *Spec) {
			s.Bus.SubscribeTopics = []string{"blueprint"}
		}},
		{"topic grant with the subject prefix", "p", func(s *Spec) {
			s.Bus.PublishTopics = []string{"a2a.topics.shared.annotations"}
		}},
		{"cluster ref missing a field", "p", func(s *Spec) {
			s.ClusterRef = &ClusterRef{ProjectID: "p", Cluster: "c"}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			spec := valid()
			tc.mutate(&spec)
			p := &Profile{Name: tc.profile, Spec: spec}
			if err := p.Validate(); err == nil {
				t.Fatalf("expected %s to be refused", tc.name)
			}
		})
	}
}

// TestGrantsRenderOntoProvisionedSubjects - a grant is only real if it names a
// subject the deployment provisions. This is the check that catches a profile
// asking for a topic nobody rendered, which at runtime is an authorization
// violation for a legitimate worker rather than an obvious error.
func TestGrantsRenderOntoProvisionedSubjects(t *testing.T) {
	// The starter topics the operator provisions under mode: next.
	provisioned := map[string]bool{
		"a2a.topics.agent.platform.upgrade-readiness": true,
		"a2a.topics.shared.blueprint":                 true,
		"a2a.topics.shared.annotations":               true,
	}
	entries, err := configs.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		data, err := configs.ReadFile(e.Name())
		if err != nil {
			t.Fatal(err)
		}
		p, err := Load(e.Name(), data)
		if err != nil {
			t.Fatalf("%s: %v", e.Name(), err)
		}
		for _, grant := range append(p.Spec.Bus.PublishTopics, p.Spec.Bus.SubscribeTopics...) {
			subject := "a2a.topics." + grant
			if !provisioned[subject] {
				t.Errorf("%s grants %q, which is %s - not a provisioned topic", p.Name, grant, subject)
			}
		}
	}
}

// TestProfilesAreJSONRoundTrippable - the config files become CR spec blocks
// at stage 3, which means going through the API server's JSON. Every shipped
// profile makes the trip, plus one constructed spec with every optional field
// set (ClusterRef among them), since no shipped profile populates them all.
func TestProfilesAreJSONRoundTrippable(t *testing.T) {
	specs := map[string]Spec{
		"constructed-full": {
			Description: "a profile with every field set",
			Persona:     Persona{Image: "registry/persona:1"},
			Harness:     Harness{Image: "registry/worker:1", Model: "model-default", MaxTurns: 60},
			Bus: Bus{
				PublishTopics:   []string{"agent.cluster.health"},
				SubscribeTopics: []string{"shared.blueprint"},
			},
			Identity:            Identity{ServiceAccountName: "cluster-agent"},
			ClusterRef:          &ClusterRef{ProjectID: "p", Location: "l", Cluster: "c"},
			Lifecycle:           Lifecycle{ActiveDeadlineSeconds: 1800, TTLSecondsAfterFinished: 600},
			QueueTimeoutSeconds: 900,
			Concurrency:         2,
			Resources: Resources{
				Requests: ResourceList{CPU: "250m", Memory: "512Mi"},
				Limits:   ResourceList{CPU: "1", Memory: "2Gi"},
			},
		},
	}
	entries, err := configs.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		data, err := configs.ReadFile(e.Name())
		if err != nil {
			t.Fatal(err)
		}
		p, err := Load(e.Name(), data)
		if err != nil {
			t.Fatal(err)
		}
		specs[e.Name()] = p.Spec
	}
	for name, spec := range specs {
		raw, err := json.Marshal(spec)
		if err != nil {
			t.Fatal(err)
		}
		var back Spec
		if err := json.Unmarshal(raw, &back); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(spec, back) {
			t.Errorf("%s: spec did not survive a JSON round trip:\n before: %+v\n  after: %+v", name, spec, back)
		}
	}
}
