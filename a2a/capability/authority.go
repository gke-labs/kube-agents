package capability

import "encoding/json"

// Reading a capability reference out of an envelope's `authority` block.
//
// This lives in the capability package rather than in either executor because
// there are two of them — the session executor (a2a/worker-adapter) and the
// Hermes bridge (a2a/hermes-bridge) — and a security parser with two copies is
// a security parser with two behaviours. It is deliberately NOT the gateway's
// AuthorityGrants type: importing the gateway into an executor would put the
// minter on the executor's dependency graph, and the only thing crossing the
// wire is the one field below.

// authorityGrants is the shape an executor reads out of the envelope.
type authorityGrants struct {
	Capability Ref `json:"capability"`
}

type authorityBlock struct {
	Grants *authorityGrants `json:"grants"`
}

// RefFromAuthority pulls the capability reference out of an envelope's
// authority block. present is false when the block carries no capability at
// all, which is the pre-arming gateway's envelope and the rollout window an
// executor's CapabilityOptional governs; err is non-nil when a block is there
// but does not parse, which is never a rollout state and always a refusal.
func RefFromAuthority(raw json.RawMessage) (ref Ref, present bool, err error) {
	if len(raw) == 0 {
		return Ref{}, false, nil
	}
	var block authorityBlock
	if err := json.Unmarshal(raw, &block); err != nil {
		return Ref{}, false, err
	}
	if block.Grants == nil {
		return Ref{}, false, nil
	}
	return block.Grants.Capability, true, nil
}
