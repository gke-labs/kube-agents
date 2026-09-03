package lib

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	mrand "math/rand/v2"
)

// Identifier minting. taskId travels in the subject as a token, so every id
// here is lowercase hex behind a lowercase prefix - a dot-free DNS-1123 label
// that ValidateEmit accepts. nuid, which the envelope id uses, is base62 and
// mints uppercase; that is fine inside a payload field and wrong in a subject
// token, so subject-bound ids do not come from it.
func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand does not fail on any platform we run on, and an id
		// minter that returns an error would push that non-condition into
		// every call site.
		panic(fmt.Sprintf("crypto/rand: %v", err))
	}
	return hex.EncodeToString(b)
}

// NewTaskID mints a task id. The publisher mints it - a deviation from HTTP
// A2A, where the server does, because the subject has to exist before anyone
// can answer on it.
func NewTaskID() string { return "task-" + randomHex(16) }

// NewContextID mints a context id: one per backend conversation, minted at
// first contact and persistent across pod incarnations.
func NewContextID() string { return "ctx-" + randomHex(16) }

// NewCorrelationID mints a correlation id. Minted once, at the user
// interaction that starts a task or at the top of a scheduled run, and copied
// verbatim on every hop after that.
func NewCorrelationID() string { return "corr-" + randomHex(16) }

// SessionNameWords is the session-name pool, carried over from the demo.
// Session names get typed into chat ("what is otter doing?"), read aloud, and
// embedded in pod names, so every entry is a short lowercase word. The space
// is small on purpose: memorable beats unique, and the minting caller rejects
// collisions against live sessions.
var SessionNameWords = []string{
	"otter", "lynx", "wren", "tapir", "newt", "ibis", "mole", "crab",
	"heron", "stoat", "puffin", "marmot", "gecko", "koala", "bison", "raven",
	"finch", "egret", "gull", "tern", "lark", "swan", "hare", "vole",
	"shrew", "badger", "beaver", "weasel", "ferret", "martin", "osprey", "falcon",
	"magpie", "toucan", "iguana", "turtle", "salmon", "cicada", "beetle", "moth",
	"wasp", "mantis", "urchin", "limpet", "squid", "skink", "viper", "adder",
}

// NewSessionName mints a session name for one run of a profile:
// <profile>-<animal>, per the 8/24 ruling. The structure lives in
// from.profile; this name is for humans, and callers treat it as opaque.
func NewSessionName(profile string) string {
	animal := SessionNameWords[mrand.N(len(SessionNameWords))]
	if profile == "" {
		return animal
	}
	return profile + "-" + animal
}
