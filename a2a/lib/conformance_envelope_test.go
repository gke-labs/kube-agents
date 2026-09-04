package lib

// Conformance assertions 1-8 from docs/designs/spec-a2a-payloads.md, envelope and
// payload sections. Table-driven; subtests are named by assertion number. Assertions
// that need a running server (4, 5, the size half of 8) live in
// conformance_bus_test.go; lifecycle assertions 12-15 and 18 in
// conformance_lifecycle_test.go.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

// validMessagePayload is shared across test files under many different
// envelope taskIds, so it leaves the payload's optional taskId/contextId
// unset - assertion 7's agreement clause refuses a payload naming a task its
// envelope does not carry.
func validMessagePayload() json.RawMessage {
	return json.RawMessage(`{
		"role": "user",
		"parts": [{"kind": "text", "text": "hello"}],
		"messageId": "msg-1"
	}`)
}

func validEnvelopeJSON(mutate func(map[string]any)) []byte {
	m := map[string]any{
		"protocol":      "a2a-jetstream/0.4",
		"envelopeId":    "env-1",
		"correlationId": "corr-1",
		"taskId":        "task-1",
		"contextId":     "ctx-1",
		"ts":            "2026-08-24T17:00:00Z",
		"from":          map[string]any{"session": "worker-brisk-otter", "agentType": "claude-code"},
		"identity":      nil,
		"authority":     nil,
		"kind":          "message",
	}
	var payload any
	if err := json.Unmarshal(validMessagePayload(), &payload); err != nil {
		panic(err)
	}
	m["payload"] = payload
	if mutate != nil {
		mutate(m)
	}
	b, err := json.Marshal(m)
	if err != nil {
		panic(err)
	}
	return b
}

// Assertion 1: an envelope with an unknown protocol major is rejected. Same-major
// envelopes with unknown fields are accepted and the unknown fields ignored.
func TestAssertion01_ProtocolMajor(t *testing.T) {
	cases := []struct {
		name    string
		raw     []byte
		wantErr bool
	}{
		{
			name: "unknown_major_rejected",
			raw: validEnvelopeJSON(func(m map[string]any) {
				m["protocol"] = "a2a-jetstream/1.0"
			}),
			wantErr: true,
		},
		{
			name: "same_major_higher_minor_accepted",
			raw: validEnvelopeJSON(func(m map[string]any) {
				m["protocol"] = "a2a-jetstream/0.9"
			}),
			wantErr: false,
		},
		{
			name: "unknown_envelope_field_ignored",
			raw: validEnvelopeJSON(func(m map[string]any) {
				m["someFutureField"] = "surprise"
			}),
			wantErr: false,
		},
		{
			name:    "current_protocol_accepted",
			raw:     validEnvelopeJSON(nil),
			wantErr: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseEnvelope(tc.raw)
			if tc.wantErr && err == nil {
				t.Fatalf("ParseEnvelope accepted %s, want protocol error", tc.name)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("ParseEnvelope rejected %s: %v", tc.name, err)
			}
		})
	}
}

// Assertion 2: the library never emits an envelope missing protocol, envelopeId,
// correlationId, ts, from, or kind, nor one missing taskId/contextId for the kinds
// that require them.
func TestAssertion02_RequiredFields(t *testing.T) {
	from := Party{Session: "test-session", AgentType: "test"}

	t.Run("builder_fills_required_fields", func(t *testing.T) {
		env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", validMessagePayload())
		if err != nil {
			t.Fatalf("NewMessageEnvelope: %v", err)
		}
		if env.Protocol != Protocol {
			t.Errorf("protocol = %q, want %q", env.Protocol, Protocol)
		}
		if env.EnvelopeID == "" {
			t.Error("envelopeId empty")
		}
		if env.CorrelationID != "corr-1" {
			t.Errorf("correlationId = %q", env.CorrelationID)
		}
		if env.TS.IsZero() {
			t.Error("ts zero")
		}
		if env.From.Session == "" {
			t.Error("from.session empty")
		}
		if env.Kind != KindMessage {
			t.Errorf("kind = %q", env.Kind)
		}
	})

	// ValidateEmit is the gate Publish runs; an envelope hand-built with a missing
	// required field must not pass it.
	missing := []struct {
		name   string
		mutate func(*Envelope)
	}{
		{"missing_protocol", func(e *Envelope) { e.Protocol = "" }},
		{"missing_envelopeId", func(e *Envelope) { e.EnvelopeID = "" }},
		{"missing_correlationId", func(e *Envelope) { e.CorrelationID = "" }},
		{"missing_ts", func(e *Envelope) { e.TS = time.Time{} }},
		{"missing_from", func(e *Envelope) { e.From = Party{} }},
		{"missing_kind", func(e *Envelope) { e.Kind = "" }},
		{"message_missing_taskId", func(e *Envelope) { e.TaskID = "" }},
		{"message_missing_contextId", func(e *Envelope) { e.ContextID = "" }},
		// Dot-free clause: taskId is a subject token; a dot changes the
		// subject's token count under every wildcard filter.
		{"dotted_taskId", func(e *Envelope) { e.TaskID = "task.7" }},
		{"non_dns1123_taskId", func(e *Envelope) { e.TaskID = "Task_7" }},
	}
	for _, tc := range missing {
		t.Run(tc.name, func(t *testing.T) {
			env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", validMessagePayload())
			if err != nil {
				t.Fatalf("NewMessageEnvelope: %v", err)
			}
			tc.mutate(env)
			if err := env.ValidateEmit(); err == nil {
				t.Fatalf("ValidateEmit passed with %s", tc.name)
			}
		})
	}

	// taskId/contextId required per kind: required for message, status-update,
	// artifact-update, cancel; absent for agent-card, agent-closed.
	t.Run("agent_card_needs_no_taskId", func(t *testing.T) {
		env, err := NewAgentCardEnvelope(from, "corr-1", json.RawMessage(`{"name": "test-agent"}`))
		if err != nil {
			t.Fatalf("NewAgentCardEnvelope: %v", err)
		}
		if err := env.ValidateEmit(); err != nil {
			t.Fatalf("ValidateEmit: %v", err)
		}
	})
}

// Assertion 3 (emit side): the library never populates identity, and populates
// authority only on the gateway's ingress path. The passthrough half runs on the bus
// in conformance_bus_test.go.
func TestAssertion03_IdentityAuthorityEmit(t *testing.T) {
	from := Party{Session: "test-session"}

	t.Run("identity_never_populated", func(t *testing.T) {
		env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", validMessagePayload())
		if err != nil {
			t.Fatal(err)
		}
		b, err := json.Marshal(env)
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]json.RawMessage
		if err := json.Unmarshal(b, &m); err != nil {
			t.Fatal(err)
		}
		if got, ok := m["identity"]; ok && !bytes.Equal(got, []byte("null")) {
			t.Errorf("identity = %s, want null", got)
		}
		// And an envelope with identity set must be refused at emit.
		env.Identity = json.RawMessage(`{"sub": "forged"}`)
		if err := env.ValidateEmit(); err == nil {
			t.Error("ValidateEmit passed with identity populated")
		}
	})

	t.Run("authority_null_by_default", func(t *testing.T) {
		env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", validMessagePayload())
		if err != nil {
			t.Fatal(err)
		}
		if env.Authority != nil && !bytes.Equal(env.Authority, []byte("null")) {
			t.Errorf("authority = %s, want null", env.Authority)
		}
	})

	t.Run("authority_set_only_via_ingress_option", func(t *testing.T) {
		auth := json.RawMessage(`{"requester": "discord:1234", "audience": ["adam"]}`)
		env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", validMessagePayload(),
			WithAuthority(auth))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(env.Authority, auth) {
			t.Errorf("authority = %s, want %s", env.Authority, auth)
		}
		if err := env.ValidateEmit(); err != nil {
			t.Fatalf("ValidateEmit: %v", err)
		}
	})
}

// Assertion 6: every payload survives a parse and re-serialize with semantics
// preserved, including unknown A2A object fields.
func TestAssertion06_PayloadRoundTrip(t *testing.T) {
	payload := json.RawMessage(`{
		"role": "agent",
		"parts": [{"kind": "text", "text": "hi", "futurePartField": {"deep": [1, 2, 3]}}],
		"messageId": "msg-2",
		"taskId": "task-1",
		"contextId": "ctx-1",
		"unknownA2AField": "must-survive"
	}`)
	raw := validEnvelopeJSON(func(m map[string]any) {
		var p any
		if err := json.Unmarshal(payload, &p); err != nil {
			panic(err)
		}
		m["payload"] = p
		m["kind"] = "message"
	})

	env, err := ParseEnvelope(raw)
	if err != nil {
		t.Fatalf("ParseEnvelope: %v", err)
	}
	out, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("re-serialize: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatal(err)
	}
	var got, want any
	if err := json.Unmarshal(m["payload"], &got); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(payload, &want); err != nil {
		t.Fatal(err)
	}
	if !jsonEqual(got, want) {
		t.Errorf("payload semantics changed:\n got: %s\nwant: %s", m["payload"], payload)
	}
}

func jsonEqual(a, b any) bool {
	ab, _ := json.Marshal(a)
	bb, _ := json.Marshal(b)
	return bytes.Equal(ab, bb)
}

// Assertion 7: a kind/payload type mismatch is surfaced as a protocol error, never
// passed through.
func TestAssertion07_KindPayloadMismatch(t *testing.T) {
	cases := []struct {
		name    string
		kind    string
		payload string
		wantErr bool
	}{
		{"message_with_status_payload", "message",
			`{"taskId": "task-1", "contextId": "ctx-1", "status": {"state": "working"}, "final": false}`, true},
		{"status_update_with_message_payload", "status-update",
			`{"role": "user", "parts": [{"kind": "text", "text": "x"}], "messageId": "m1"}`, true},
		{"cancel_with_nonempty_payload", "cancel",
			`{"role": "user"}`, true},
		{"cancel_with_empty_payload", "cancel", `{}`, false},
		{"status_update_valid", "status-update",
			`{"taskId": "task-1", "contextId": "ctx-1", "status": {"state": "working"}, "final": false}`, false},
		{"artifact_update_valid", "artifact-update",
			`{"taskId": "task-1", "contextId": "ctx-1", "artifact": {"artifactId": "a1", "name": "result", "parts": [{"kind": "text", "text": "done"}]}}`, false},
		{"status_update_bogus_state", "status-update",
			`{"taskId": "task-1", "contextId": "ctx-1", "status": {"state": "percolating"}, "final": false}`, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw := validEnvelopeJSON(func(m map[string]any) {
				m["kind"] = tc.kind
				var p any
				if err := json.Unmarshal([]byte(tc.payload), &p); err != nil {
					panic(err)
				}
				m["payload"] = p
			})
			_, err := ParseEnvelope(raw)
			if tc.wantErr {
				var perr *ProtocolError
				if !errors.As(err, &perr) {
					t.Fatalf("want ProtocolError, got %v", err)
				}
			} else if err != nil {
				t.Fatalf("ParseEnvelope: %v", err)
			}
		})
	}
}

// Assertion 8 (client-side half): a FilePart with inline bytes over the threshold is
// refused with an A2A error. The max-message-size half needs a server and lives in
// conformance_bus_test.go.
func TestAssertion08_InlineFilePartThreshold(t *testing.T) {
	big := strings.Repeat("A", (InlineFileThreshold/3+1)*4) // base64 of > threshold bytes
	payload := `{
		"role": "user",
		"parts": [{"kind": "file", "file": {"name": "blob.bin", "mimeType": "application/octet-stream", "bytes": "` + big + `"}}],
		"messageId": "msg-3",
		"taskId": "task-1",
		"contextId": "ctx-1"
	}`
	from := Party{Session: "test-session"}
	env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", json.RawMessage(payload))
	if err == nil {
		err = env.ValidateEmit()
	}
	var a2aErr *A2AError
	if !errors.As(err, &a2aErr) {
		t.Fatalf("oversized inline FilePart: want A2AError, got %v", err)
	}

	t.Run("uri_filepart_accepted", func(t *testing.T) {
		payload := `{
			"role": "user",
			"parts": [{"kind": "file", "file": {"name": "blob.bin", "mimeType": "application/octet-stream", "uri": "objstore://tasks/task-1/blob.bin"}}],
			"messageId": "msg-4",
			"taskId": "task-1",
			"contextId": "ctx-1"
		}`
		env, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", json.RawMessage(payload))
		if err != nil {
			t.Fatalf("NewMessageEnvelope: %v", err)
		}
		if err := env.ValidateEmit(); err != nil {
			t.Fatalf("ValidateEmit: %v", err)
		}
	})
}

// Assertion 7, continued: an A2A Message riding inside a status-update (the
// input-required prompt) is validated like any other Message.
func TestAssertion07_StatusUpdateMessageParts(t *testing.T) {
	cases := []struct {
		name    string
		status  string
		wantErr bool
	}{
		{"input_required_with_valid_message",
			`{"taskId": "task-1", "contextId": "ctx-1", "status": {"state": "input-required", "message": {"role": "agent", "parts": [{"kind": "text", "text": "which cluster?"}], "messageId": "m-q"}}, "final": false}`,
			false},
		{"input_required_with_unknown_part_kind",
			`{"taskId": "task-1", "contextId": "ctx-1", "status": {"state": "input-required", "message": {"role": "agent", "parts": [{"kind": "hologram"}], "messageId": "m-q"}}, "final": false}`,
			true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw := validEnvelopeJSON(func(m map[string]any) {
				m["kind"] = "status-update"
				var p any
				if err := json.Unmarshal([]byte(tc.status), &p); err != nil {
					panic(err)
				}
				m["payload"] = p
			})
			_, err := ParseEnvelope(raw)
			if tc.wantErr {
				var perr *ProtocolError
				if !errors.As(err, &perr) {
					t.Fatalf("want ProtocolError, got %v", err)
				}
			} else if err != nil {
				t.Fatalf("ParseEnvelope: %v", err)
			}
		})
	}
}

// Assertion 8, continued: the inline threshold also covers FileParts inside a
// status-update's message, and the boundary is exact - 128KiB inline is legal,
// one byte more is not.
func TestAssertion08_ThresholdEdges(t *testing.T) {
	from := Party{Session: "test-session"}

	t.Run("status_update_message_over_threshold", func(t *testing.T) {
		big := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0xAB}, InlineFileThreshold+1))
		payload := `{"taskId": "task-1", "contextId": "ctx-1", "status": {"state": "input-required", "message": {"role": "agent", "parts": [{"kind": "file", "file": {"name": "b.bin", "bytes": "` + big + `"}}], "messageId": "m-f"}}, "final": false}`
		_, err := NewStatusUpdateEnvelope(from, "task-1", "ctx-1", "corr-1", json.RawMessage(payload))
		var a2aErr *A2AError
		if !errors.As(err, &a2aErr) {
			t.Fatalf("want A2AError, got %v", err)
		}
	})

	t.Run("exactly_at_threshold_accepted", func(t *testing.T) {
		exact := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0xAB}, InlineFileThreshold))
		payload := `{"role": "user", "parts": [{"kind": "file", "file": {"name": "b.bin", "bytes": "` + exact + `"}}], "messageId": "m-e", "taskId": "task-1", "contextId": "ctx-1"}`
		if _, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", json.RawMessage(payload)); err != nil {
			t.Fatalf("a FilePart of exactly the threshold is legal (spec: only above must use uri): %v", err)
		}
	})

	t.Run("one_byte_over_refused", func(t *testing.T) {
		over := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0xAB}, InlineFileThreshold+1))
		payload := `{"role": "user", "parts": [{"kind": "file", "file": {"name": "b.bin", "bytes": "` + over + `"}}], "messageId": "m-o", "taskId": "task-1", "contextId": "ctx-1"}`
		_, err := NewMessageEnvelope(from, "task-1", "ctx-1", "corr-1", json.RawMessage(payload))
		var a2aErr *A2AError
		if !errors.As(err, &a2aErr) {
			t.Fatalf("want A2AError one byte over the threshold, got %v", err)
		}
	})
}

// The library emits only the protocol version it speaks - a hand-built
// envelope claiming a different minor within the major is refused at emit
// (inbound tolerance for same-major minors is assertion 1's job).
func TestValidateEmit_PinnedProtocol(t *testing.T) {
	env, err := NewMessageEnvelope(Party{Session: "s"}, "task-1", "ctx-1", "corr-1", validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	env.Protocol = "a2a-jetstream/0.9"
	if err := env.ValidateEmit(); err == nil {
		t.Fatal("ValidateEmit accepted a protocol version the library does not speak")
	}
}

// Assertion 7, taskId-agreement clause: a payload that names a different task
// than its envelope is poison to the fold, so it is a protocol error at both
// ends - the constructor/emit path refuses to build it, and ParseEnvelope
// refuses it inbound, which is what lets the replay screen skip it.
func TestAssertion07_PayloadTaskIDAgreement(t *testing.T) {
	_, err := NewStatusUpdateEnvelope(Party{Session: "worker-a"}, "task-a", "ctx-1", "corr-1",
		json.RawMessage(`{"taskId":"task-b","contextId":"ctx-1","status":{"state":"working"}}`))
	var perr *ProtocolError
	if !errors.As(err, &perr) {
		t.Fatalf("emit with mismatched payload taskId: want ProtocolError, got %v", err)
	}

	raw := `{"protocol":"` + Protocol + `","envelopeId":"env-1","correlationId":"corr-1",` +
		`"ts":"2026-09-01T00:00:00Z","from":{"session":"w"},"identity":null,"authority":null,` +
		`"kind":"artifact-update","taskId":"task-a","contextId":"ctx-1",` +
		`"payload":{"taskId":"task-b","contextId":"ctx-1","artifact":{"artifactId":"r","name":"result","parts":[{"kind":"text","text":"x"}]}}}`
	_, err = ParseEnvelope([]byte(raw))
	if !errors.As(err, &perr) {
		t.Fatalf("parse with mismatched payload taskId: want ProtocolError, got %v", err)
	}
}
