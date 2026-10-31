package lib

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The dispatcher's names line up with the subjects they read: a durable's
// filter matches its kind's submissions and nothing else, and the lease
// filter finds a task's lease whichever pod holds it.
func TestTheDispatchNamesMatchWhatTheyRead(t *testing.T) {
	if got := ProfileDurableName("tse", "read", SubmitKindTop); got != "profile-tse-read-top" {
		t.Errorf("durable name = %q", got)
	}
	filter := ProfileDurableFilter("tse", "read", SubmitKindTop)
	if !subjectMatches(filter, TaskSubmitSubject("tse", "read", SubmitKindTop, "t1")) {
		t.Errorf("%s does not match its own submissions", filter)
	}
	for _, other := range []string{
		TaskSubmitSubject("tse", "read", SubmitKindChild, "t1"),
		TaskSubmitSubject("tse", "unattended", SubmitKindTop, "t1"),
		TaskSubmitSubject("other", "read", SubmitKindTop, "t1"),
		TaskInSubject("tse", "t1"),
	} {
		if subjectMatches(filter, other) {
			t.Errorf("%s matches %s", filter, other)
		}
	}
	lease := ProfileLeaseKeyFilter("tse", "read", "t1")
	if !subjectMatches(lease, ProfileLeaseKey("tse", "read", "tse-read-7f9c", "t1")) {
		t.Errorf("%s does not find the task's lease", lease)
	}
	if subjectMatches(lease, ProfileLeaseKey("tse", "read", "tse-read-7f9c", "t2")) {
		t.Errorf("%s finds another task's lease", lease)
	}
}

// subjectMatches is NATS token matching for the * wildcard, enough for these
// names (none uses >).
func subjectMatches(pattern, subject string) bool {
	p, s := strings.Split(pattern, "."), strings.Split(subject, ".")
	if len(p) != len(s) {
		return false
	}
	for i := range p {
		if p[i] != "*" && p[i] != s[i] {
			return false
		}
	}
	return true
}

func validOffer() DispatchMessage {
	return DispatchMessage{
		Type: DispatchOffer, TaskID: "t1", Kind: SubmitKindTop,
		Envelope:     json.RawMessage(`{"taskId":"t1"}`),
		Deadline:     time.Date(2026, 10, 28, 18, 0, 0, 0, time.UTC),
		LeaseSeconds: 90, RenewSeconds: 20, AcceptSeconds: 10,
	}
}

// An offer and an abandon survive the wire, and each malformed one is
// refused with a reason that names what is wrong.
func TestADispatchMessageIsCheckedBothWays(t *testing.T) {
	for _, m := range []DispatchMessage{validOffer(), {Type: DispatchConfirm, TaskID: "t1"}, {Type: DispatchAbandon, TaskID: "t1", Reason: "stopped by the conversation"}} {
		raw, err := json.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		var back DispatchMessage
		if err := json.Unmarshal(raw, &back); err != nil {
			t.Fatal(err)
		}
		if err := back.Validate(); err != nil {
			t.Errorf("%s did not survive the wire: %v (%s)", m.Type, err, raw)
		}
	}
	for name, tc := range map[string]struct {
		edit func(*DispatchMessage)
		want string
	}{
		"unknown type":       {func(m *DispatchMessage) { m.Type = "steal" }, "type"},
		"task id with a dot": {func(m *DispatchMessage) { m.TaskID = "a.b" }, "subject token"},
		"bad kind":           {func(m *DispatchMessage) { m.Kind = "grandchild" }, "kind"},
		"no envelope":        {func(m *DispatchMessage) { m.Envelope = nil }, "envelope"},
		"no deadline":        {func(m *DispatchMessage) { m.Deadline = time.Time{} }, "deadline"},
		"no renewal":         {func(m *DispatchMessage) { m.RenewSeconds = 0 }, "renewals"},
		"lease too short":    {func(m *DispatchMessage) { m.LeaseSeconds = 59 }, "renewals"},
		"no accept window":   {func(m *DispatchMessage) { m.AcceptSeconds = 0 }, "accept window"},
		"abandon no reason":  {func(m *DispatchMessage) { *m = DispatchMessage{Type: DispatchAbandon, TaskID: "t1"} }, "reason"},
	} {
		m := validOffer()
		tc.edit(&m)
		if err := m.Validate(); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: Validate = %v, want an error saying %q", name, err, tc.want)
		}
	}
}

// A decline says why; an accept and a renewal need only the task.
func TestAnAcceptAndARenewalAreChecked(t *testing.T) {
	if err := (DispatchAccept{TaskID: "t1", Accepted: true}).Validate(); err != nil {
		t.Errorf("accept: %v", err)
	}
	if err := (DispatchAccept{TaskID: "t1", Reason: "busy"}).Validate(); err != nil {
		t.Errorf("decline with a reason: %v", err)
	}
	if err := (DispatchAccept{TaskID: "t1"}).Validate(); err == nil {
		t.Error("a decline with no reason was accepted")
	}
	if err := (DispatchAccept{TaskID: "t1", Reason: strings.Repeat("x", DispatchReasonCap+1)}).Validate(); err == nil {
		t.Error("a decline with an over-long reason was accepted")
	}
	if err := (LeaseRenewal{TaskID: "t1"}).Validate(); err != nil {
		t.Errorf("renewal: %v", err)
	}
	var final LeaseRenewal
	if err := json.Unmarshal([]byte(`{"taskId":"t1","final":true}`), &final); err != nil || !final.Final || final.Validate() != nil {
		t.Errorf("a final renewal = %+v, %v", final, err)
	}
	if raw, _ := json.Marshal(LeaseRenewal{TaskID: "t1"}); strings.Contains(string(raw), "final") {
		t.Errorf("an ordinary renewal carries final: %s", raw)
	}
	if err := (LeaseRenewal{TaskID: ""}).Validate(); err == nil {
		t.Error("a renewal naming no task was accepted")
	}
}

// A lease is expired only once ExpiresAt plus the grace has passed.
func TestALeaseExpiresAfterItsGrace(t *testing.T) {
	at := time.Date(2026, 10, 28, 18, 0, 0, 0, time.UTC)
	l := LeaseRecord{ExpiresAt: at}
	if l.Expired(at, 0) {
		t.Error("expired at ExpiresAt itself")
	}
	if !l.Expired(at.Add(time.Second), 0) {
		t.Error("not expired a second past ExpiresAt with no grace")
	}
	if l.Expired(at.Add(30*time.Second), time.Minute) {
		t.Error("expired inside the grace")
	}
	if !l.Expired(at.Add(61*time.Second), time.Minute) {
		t.Error("not expired past the grace")
	}
}

// A card with the routing fields round-trips, and one without them (what
// the operator published before) still parses as an agent card.
func TestAProfileCardCarriesItsRoutingFields(t *testing.T) {
	full := `{"name":"tse","description":"Diagnoses GKE cluster problems.","tiers":["read"],` +
		`"parameters":[{"name":"project","required":true},{"name":"location","required":true},{"name":"cluster","required":true}]}`
	var c AgentCard
	if err := json.Unmarshal([]byte(full), &c); err != nil {
		t.Fatal(err)
	}
	if c.Name != "tse" || len(c.Tiers) != 1 || len(c.Parameters) != 3 || !c.Parameters[2].Required || c.ClusterRef != nil {
		t.Errorf("card = %+v", c)
	}
	pinned := `{"name":"prod-a","clusterRef":{"projectId":"p","location":"us-central1","cluster":"a"}}`
	if err := json.Unmarshal([]byte(pinned), &c); err != nil || c.ClusterRef == nil || c.ClusterRef.Cluster != "a" {
		t.Errorf("pinned card = %+v, %v", c, err)
	}
	for _, payload := range []string{full, pinned, `{"name":"old"}`} {
		if err := validatePayload(KindAgentCard, "", json.RawMessage(payload)); err != nil {
			t.Errorf("%s: %v", payload, err)
		}
	}
}

func validDispatchConfig() DispatchConfig {
	return DispatchConfig{
		Version: "v1", Namespace: "kubeagents-system", ProfileNamespace: "kubeagents-system-profiles",
		Ceiling: 6, InteractiveReserve: 2, ChildReserve: 2,
		LeaseSeconds: 90, RenewSeconds: 20, RestartGraceSeconds: 60, AcceptTimeoutSeconds: 10,
		Profiles: []DispatchProfile{{
			Name: "tse", Tiers: []string{"read"}, Concurrency: 2, QueueTimeoutSeconds: 3600, ActiveDeadlineSeconds: 1800,
			Parameters: []ProfileParameter{{Name: "cluster", Required: true}},
			Durables: []DispatchDurable{
				{Tier: "read", Kind: SubmitKindTop, Name: ProfileDurableName("tse", "read", SubmitKindTop)},
				{Tier: "read", Kind: SubmitKindChild, Name: ProfileDurableName("tse", "read", SubmitKindChild)},
			},
		}},
	}
}

// The dispatcher refuses a config it can't run on, and each refusal names
// the field.
func TestADispatchConfigIsChecked(t *testing.T) {
	if err := validDispatchConfig().Validate(); err != nil {
		t.Fatalf("a good config: %v", err)
	}
	one := validDispatchConfig()
	one.Ceiling, one.ChildReserve, one.InteractiveReserve = 1, 0, 0
	if err := one.Validate(); err != nil {
		t.Errorf("a ceiling of 1 with no child reserve: %v", err)
	}
	for name, tc := range map[string]struct {
		edit func(*DispatchConfig)
		want string
	}{
		"no namespace":              {func(c *DispatchConfig) { c.Namespace = "" }, "namespace"},
		"no ceiling":                {func(c *DispatchConfig) { c.Ceiling = 0 }, "ceiling"},
		"reserve at ceiling":        {func(c *DispatchConfig) { c.ChildReserve = 6 }, "childReserve"},
		"reserve of a 1 ceiling":    {func(c *DispatchConfig) { c.Ceiling, c.ChildReserve, c.InteractiveReserve = 1, 0, 1 }, "interactiveReserve"},
		"reserves fill the ceiling": {func(c *DispatchConfig) { c.Ceiling, c.ChildReserve, c.InteractiveReserve = 4, 2, 2 }, "reserved for neither"},
		"short lease":               {func(c *DispatchConfig) { c.LeaseSeconds = 59 }, "three renewals"},
		"short grace":               {func(c *DispatchConfig) { c.RestartGraceSeconds = 39 }, "two renewals"},
		"no accept timeout":         {func(c *DispatchConfig) { c.AcceptTimeoutSeconds = 0 }, "acceptTimeoutSeconds"},
		"duplicate profile":         {func(c *DispatchConfig) { c.Profiles = append(c.Profiles, c.Profiles[0]) }, "twice"},
		"no concurrency":            {func(c *DispatchConfig) { c.Profiles[0].Concurrency = 0 }, "concurrency"},
		"misnamed durable":          {func(c *DispatchConfig) { c.Profiles[0].Durables[0].Name = "other" }, "durable"},
	} {
		c := validDispatchConfig()
		c.Profiles = append([]DispatchProfile(nil), c.Profiles...)
		c.Profiles[0].Durables = append([]DispatchDurable(nil), c.Profiles[0].Durables...)
		tc.edit(&c)
		if err := c.Validate(); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: Validate = %v, want an error saying %q", name, err, tc.want)
		}
	}
}

// The pool selector names the worker component, the profile and the tier, so
// it can't pick up a ServiceAccount's labels or another tier's pods.
func TestThePoolSelectorNamesTheWorkerComponentProfileAndTier(t *testing.T) {
	want := "kubeagents.x-k8s.io/a2a-component=agentprofile-worker,kubeagents.x-k8s.io/agentprofile=tse,kubeagents.x-k8s.io/agentprofile-tier=read"
	if got := ProfilePoolSelector("tse", "read"); got != want {
		t.Errorf("selector = %q, want %q", got, want)
	}
}

// The dispatch config the operator renders (its fixture, written by the
// operator's tests) decodes into DispatchConfig and passes the dispatcher's
// own checks: the operator module can't import this one, so this is what
// holds the two sets of JSON names equal.
func TestTheOperatorsDispatchConfigDecodesAndValidates(t *testing.T) {
	raw, err := os.ReadFile("testdata/rendered-dispatch-config.json")
	if err != nil {
		t.Fatalf("%v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var cfg DispatchConfig
	if err := dec.Decode(&cfg); err != nil {
		t.Fatalf("the operator's dispatch config does not decode strictly: %v", err)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("the operator's dispatch config fails the dispatcher's checks: %v", err)
	}
	if len(cfg.Profiles) != 1 || cfg.Profiles[0].Name != "tse" || len(cfg.Profiles[0].Parameters) != 3 || len(cfg.Profiles[0].Durables) != 2 {
		t.Errorf("profiles = %+v", cfg.Profiles)
	}
	if cfg.Version == "" || cfg.Namespace == "" || cfg.ProfileNamespace != cfg.Namespace+"-profiles" {
		t.Errorf("version %q, namespace %q, profile namespace %q", cfg.Version, cfg.Namespace, cfg.ProfileNamespace)
	}
}

// The durable the operator makes and the pool labels it renders (its
// fixture, written by the operator's tests) agree with this package's
// constants, and the durable passes SubscribeHeld's config checks (its
// intake check still takes the .in class on this tree; the dispatcher's PR
// moves it to the submission shape). The operator
// can't import lib, so this is what holds its copies of the held mark, the
// AckWait and the labels equal to these. The two tamper lists aren't
// compared; a drift makes the dispatcher refuse to bind, which fails safe.
func TestTheOperatorsHeldDurableIsOneSubscribeHeldBinds(t *testing.T) {
	raw, err := os.ReadFile("testdata/rendered-held-durable.json")
	if err != nil {
		t.Fatalf("%v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", err)
	}
	var doc struct {
		Stream        string            `json:"stream"`
		Durable       string            `json:"durable"`
		Filter        string            `json:"filter"`
		AckPolicy     string            `json:"ackPolicy"`
		AckWait       string            `json:"ackWait"`
		MaxAckPending int               `json:"maxAckPending"`
		Metadata      map[string]string `json:"metadata"`
		PoolLabels    map[string]string `json:"poolLabels"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		t.Fatal(err)
	}
	if doc.Stream != ProfileSubmitStream || doc.Durable != ProfileDurableName("tse", "read", SubmitKindTop) || doc.Filter != ProfileDurableFilter("tse", "read", SubmitKindTop) {
		t.Errorf("stream %q durable %q filter %q, want lib's names", doc.Stream, doc.Durable, doc.Filter)
	}
	ackWait, err := time.ParseDuration(doc.AckWait)
	if err != nil || ackWait != DefaultHeldAckWait {
		t.Errorf("the operator's AckWait %q is not DefaultHeldAckWait %v", doc.AckWait, DefaultHeldAckWait)
	}
	if doc.Metadata[heldMarkerKey] != heldMarkerValue || len(doc.Metadata) != 1 {
		t.Errorf("the operator's mark %v is not %s=%s", doc.Metadata, heldMarkerKey, heldMarkerValue)
	}
	cc := jetstream.ConsumerConfig{
		Durable: doc.Durable, FilterSubject: doc.Filter, AckPolicy: jetstream.AckExplicitPolicy,
		MaxAckPending: doc.MaxAckPending, AckWait: ackWait, Metadata: doc.Metadata,
		DeliverPolicy: jetstream.DeliverAllPolicy, ReplayPolicy: jetstream.ReplayInstantPolicy,
	}
	if doc.AckPolicy != "explicit" || cc.MaxAckPending < 1 || heldConfigTampered(cc) != "" {
		t.Errorf("the operator's durable fails SubscribeHeld's checks: ack %q, cap %d, tampered %q", doc.AckPolicy, cc.MaxAckPending, heldConfigTampered(cc))
	}
	want := map[string]string{ProfileComponentLabel: ProfileWorkerComponent, ProfileNameLabel: "tse", ProfileTierLabel: "read"}
	for k, v := range want {
		if doc.PoolLabels[k] != v {
			t.Errorf("pool label %s = %q, want %q (lib.ProfilePoolSelector selects on it)", k, doc.PoolLabels[k], v)
		}
	}
	if len(doc.PoolLabels) != len(want) {
		t.Errorf("pool labels %v, want exactly %v", doc.PoolLabels, want)
	}
}
